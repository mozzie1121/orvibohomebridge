"""Bounded video probes; credentials travel only through a private child pipe.

The synchronous wire implementation runs in a disposable Python process. Killing
and reaping that process also stops DNS and socket work after timeout/cancel.
An encoded keyframe is separate from successful decoding or HA playback.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
import json
from importlib.util import find_spec
from pathlib import Path
import sys
from typing import Any

WORKER = Path(__file__).parent / "meari_transport" / "worker.py"
MAX_INPUT_BYTES = 65536
MAX_OUTPUT_BYTES = 65536
MAX_LINES = 128
STAGES = frozenset({
    "worker_started", "root_discovery", "signaling_connect",
    "signaling_registered", "device_status", "turn_allocate", "sdp_offer",
    "ice_connect", "kcp_connect", "receiving_video", "video_keyframe_received",
    "no_video_frame", "device_unavailable", "probe_failed", "dependency_missing",
    "invalid_context", "probe_timeout",
})
_ACTIVE: dict[int, asyncio.Task] = {}


def _event(value: object) -> dict[str, Any]:
    """Filter every child message again at the HA boundary."""
    if (not isinstance(value, Mapping) or not isinstance(value.get("probe_stage"), str)
            or value["probe_stage"] not in STAGES):
        raise ValueError("invalid_worker_message")
    public: dict[str, Any] = {"probe_stage": value["probe_stage"]}
    for key in ("video_frames", "video_bytes", "received_packets"):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 1_000_000_000:
            public[key] = number
    for key in ("keyframe_received", "parameter_sets_received"):
        if type(value.get(key)) is bool:
            public[key] = value[key]
    if value.get("codec") in ("h264", "hevc", "unknown"):
        public["codec"] = value["codec"]
    return public


async def _reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    # Drain after kill: a full pipe can otherwise keep Process.wait pending.
    if process.stdout is not None:
        with suppress(OSError, ValueError):
            while await process.stdout.read(65536):
                pass
    await process.wait()
    if process.stdin is not None:
        process.stdin.close()


async def async_run_video_probe(
    payload: Mapping[str, Any], *, timeout_seconds: float = 35,
    _worker_path: Path | None = None,
) -> dict[str, Any]:
    """Run one finite probe, always reaping the child before returning."""
    base: dict[str, Any] = {
        "probe_implemented": True, "video_playback_implemented": True,
        "real_device_video_verified": False, "decoded": False,
        "encoded_video_verified": False,
    }
    try:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > MAX_INPUT_BYTES or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid_context")
    except (ValueError, TypeError, RecursionError):
        return {**base, "probe_stage": "invalid_context", "worker_stopped": True}
    dependency_args = []
    # HA may install requirements into /config/deps. Isolated Python needs the
    # actual installed package root; this path never comes from service input.
    spec = find_spec("cryptography")
    if spec is not None and spec.origin is not None:
        dependency_args = ["--dependency-root", str(Path(spec.origin).parent.parent)]
    process = None
    spawn = None
    public: dict[str, Any] = {}
    history: list[str] = []
    final_received = False
    try:
        async with asyncio.timeout(timeout_seconds):
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", "-u", "-B", str(_worker_path or WORKER), *dependency_args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4096,
            ))
            # Cancellation must not abandon a process still being spawned.
            process = await asyncio.shield(spawn)
            process.stdin.write(body + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            total = 0
            for _ in range(MAX_LINES):
                line = await process.stdout.readline()
                if not line:
                    break
                total += len(line)
                if total > MAX_OUTPUT_BYTES:
                    raise ValueError("worker_output_limit")
                message = json.loads(line)
                public.update(_event(message))
                stage = public["probe_stage"]
                if not history or stage != history[-1]:
                    if len(history) >= 32:
                        raise ValueError("worker_stage_limit")
                    history.append(stage)
                if message.get("final") is True:
                    final_received = True
                    break
            if not final_received:
                raise ValueError("worker_exited_without_result")
    except TimeoutError:
        public.update(probe_stage="probe_timeout")
    except (OSError, ValueError, RecursionError, RuntimeError):
        # Error text, raw stdout and raw credentials must never reach HA logs.
        public.update(probe_stage="probe_failed")
    finally:
        async def finish() -> None:
            nonlocal process
            if process is None and spawn is not None:
                with suppress(Exception):
                    process = await spawn
            if process is not None:
                await _reap(process)

        cleanup = asyncio.create_task(finish())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
    verified = (
        final_received and public.get("probe_stage") == "video_keyframe_received"
        and public.get("keyframe_received") is True
        and public.get("parameter_sets_received") is True
        and public.get("codec") in ("h264", "hevc")
        and public.get("video_frames", 0) > 0 and public.get("video_bytes", 0) > 0
    )
    return {
        **public, **base, "encoded_video_verified": verified,
        "probe_stage_history": history,
        **({"last_probe_stage": history[-1]} if history else {}),
        "worker_stopped": True,
    }


async def async_probe_inventory(
    raw_session: object, inventory: object, *, device_index: int,
    channel_id: int, timeout_seconds: int, country_code: str = "CN",
) -> dict[str, Any]:
    from .meari_probe_context import build_probe_context

    try:
        payload = build_probe_context(
            raw_session, inventory, device_index=device_index,
            channel_id=channel_id, timeout_seconds=timeout_seconds, country_code=country_code,
        )
    except (ValueError, TypeError, OverflowError):
        return {"probe_implemented": True, "probe_stage": "invalid_context",
                "encoded_video_verified": False, "worker_stopped": True}
    result = await async_run_video_probe(payload, timeout_seconds=timeout_seconds)
    return {**result, "probe_device_index":device_index, "probe_channel_id":channel_id,
            "client_profile_emulated":True, "default_host_key_auth":True}


async def async_probe_video_stream(
    coordinator: Any, *, http_session: Any, device_index: int, channel_id: int,
    timeout_seconds: int = 35, country_code: str = "CN", phone_code: str = "86",
    language: str = "zh",
) -> dict[str, Any]:
    """Obtain fresh session/inventory, allowing only one probe per entry."""
    from .meari_session import async_inspect_video_session

    key = id(coordinator)
    if key in _ACTIVE:
        return {"probe_stage": "probe_in_progress", "probe_implemented": True,
                "video_playback_implemented": True, "real_device_video_verified": False}
    task = asyncio.current_task()
    _ACTIVE[key] = task
    try:
        # Includes cloud bootstrap as well as the media child. The child has its
        # own shorter deadline, and is reaped if this outer budget expires.
        async with asyncio.timeout(timeout_seconds + 45):
            return await async_inspect_video_session(
                coordinator, http_session=http_session, country_code=country_code,
                phone_code=phone_code, language=language,
                probe_options={"device_index": device_index, "channel_id": channel_id,
                               "timeout_seconds": timeout_seconds, "country_code":country_code},
            )
    except TimeoutError:
        return {"probe_stage": "probe_timeout", "probe_implemented": True,
                "video_playback_implemented": True, "real_device_video_verified": False,
                "encoded_video_verified": False, "worker_stopped": True}
    finally:
        _ACTIVE.pop(key, None)


async def async_cancel_video_probes(coordinator: Any = None) -> None:
    """Used on entry unload and HA shutdown; wait for child cleanup."""
    tasks = list(_ACTIVE.values()) if coordinator is None else [_ACTIVE.get(id(coordinator))]
    tasks = [task for task in tasks if task is not None and task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
