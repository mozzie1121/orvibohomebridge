"""Finite HDD queries and entity registry, separate from private transport data."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from importlib.util import find_spec
import json
from pathlib import Path
import sys
from typing import Any

from .meari_probe import WORKER, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, STAGES, _reap
from .meari_stream import _finish_uninterrupted
from .meari_transport.hdd import validate_request

DATA_KEY = "orvibohomebridge_hdd"
HDD_STAGES = frozenset({"hdd_search_received", "hdd_search_start",
                         "hdd_search_failed", "hdd_search_timeout"})


def playback_entity(hass: Any, entity_id: object) -> Any:
    """Only registered playback entities can receive HDD service commands."""
    if not isinstance(entity_id, str):
        raise ValueError("hdd_entity_required")
    entity = hass.data.get(DATA_KEY, {}).get(entity_id)
    if entity is None:
        raise ValueError("hdd_entity_unavailable")
    return entity


def _public_search(message: object, request: dict) -> dict:
    """Validate child output again; exclude all server strings and credentials."""
    if (not isinstance(message, Mapping) or message.get("probe_stage") != "hdd_search_received"
            or message.get("hdd_operation") != request["operation"]
            or message.get("calendar_date") != request["date"]):
        raise ValueError("hdd_result_invalid")
    result = {"stage": "hdd_search_received", "operation": request["operation"],
              "date": request["date"], "device_calendar_time": True}
    if request["operation"] == "month":
        days = message.get("recording_days")
        if not isinstance(days, list) or len(days) > 31:
            raise ValueError("hdd_result_invalid")
        for day in days:
            validate_request({"operation": "day", "date": day})
            if not day.startswith(request["date"]):
                raise ValueError("hdd_result_invalid")
        if len(days) != len(set(days)):
            raise ValueError("hdd_result_invalid")
        return {**result, "recording_days": days, "recording_count": len(days)}
    rows = message.get("fragments")
    count = message.get("recording_count")
    truncated = message.get("fragments_truncated")
    if (not isinstance(rows, list) or len(rows) > 256
            or type(count) is not int or not len(rows) <= count <= 65535
            or type(truncated) is not bool or truncated != (count > len(rows))):
        raise ValueError("hdd_result_invalid")
    fragments = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("hdd_result_invalid")
        start, end = row.get("start"), row.get("end")
        for date in (start, end):
            validate_request({"operation": "playback", "date": date})
            if not date.startswith(request["date"]):
                raise ValueError("hdd_result_invalid")
        if end <= start:
            raise ValueError("hdd_result_invalid")
        fragments.append({"start": start, "end": end})
    return {**result, "fragments": fragments, "recording_count": count,
            "fragments_truncated": truncated}


async def async_query_hdd(payload: dict, *, timeout_seconds: float = 60,
                          _worker_path: Path | None = None) -> dict:
    """Run a bounded child and reap it on success, error, timeout or cancellation."""
    request = validate_request(payload.get("hdd"))
    if request["operation"] not in ("month", "day"):
        raise ValueError("hdd_query_required")
    body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > MAX_INPUT_BYTES or not 0 < timeout_seconds <= 90:
        raise ValueError("hdd_query_invalid")
    dependency_args = []
    spec = find_spec("cryptography")
    if spec is not None and spec.origin:
        dependency_args = ["--dependency-root", str(Path(spec.origin).parent.parent)]
    process = spawn = None
    stage = "worker_started"
    try:
        async with asyncio.timeout(timeout_seconds):
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", "-u", "-B", str(_worker_path or WORKER), *dependency_args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=32768,
            ))
            process = await asyncio.shield(spawn)
            process.stdin.write(body + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            total = 0
            for _ in range(128):
                line = await process.stdout.readline()
                if not line:
                    break
                total += len(line)
                if total > MAX_OUTPUT_BYTES or len(line) > 32768:
                    raise ValueError("hdd_output_limit")
                message = json.loads(line)
                if not isinstance(message, dict) or not isinstance(message.get("probe_stage"), str):
                    raise ValueError("hdd_result_invalid")
                candidate = message["probe_stage"]
                if candidate not in STAGES and candidate not in HDD_STAGES:
                    raise ValueError("hdd_result_invalid")
                stage = candidate
                if message.get("final") is True:
                    return _public_search(message, request)
            raise ValueError("hdd_no_result")
    except TimeoutError:
        return {"stage": "hdd_query_timeout", "last_worker_stage": stage}
    except (OSError, ValueError, RuntimeError, RecursionError):
        return {"stage": "hdd_query_failed", "last_worker_stage": stage}
    finally:
        async def finish():
            nonlocal process
            if process is None and spawn is not None:
                with suppress(Exception):
                    process = await spawn
            if process is not None:
                await _reap(process)
        await _finish_uninterrupted(finish())
