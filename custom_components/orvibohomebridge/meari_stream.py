"""Private, demand-driven XTS video to MPEG-TS bridge for HA's Stream reader.

Only a random path on a loopback socket is exposed. Cloud credentials enter a
disposable worker through stdin; encoded video and FFmpeg output stay in pipes.
Each request owns its children and bounds time, record sizes and backpressure.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib.util import find_spec
import json
from pathlib import Path
import secrets
import sys
import time
from typing import Any

from aiohttp import web

WORKER = Path(__file__).parent / "meari_transport" / "worker.py"
MAX_CONTEXT_BYTES = 65536
MAX_STATUS_BYTES = 4096
MAX_VIDEO_BYTES = 8 * 1024 * 1024
FIRST_FRAME_TIMEOUT = 55.0
MEDIA_TIMEOUT = 30.0
WRITE_TIMEOUT = 10.0
SESSION_TIMEOUT = 3600.0
PREPARED_TIMEOUT = 20.0
RECOVERY_TIMEOUT = 120.0
RECOVERY_BACKOFF = 1.0
RECOVERY_ATTEMPTS = 3
WORKER_STAGES = frozenset({
    "worker_started", "root_discovery", "signaling_connect", "signaling_registered",
    "device_status", "turn_allocate", "sdp_offer", "ice_connect", "kcp_connect",
    "receiving_video", "video_keyframe_received", "no_video_frame", "device_unavailable",
    "probe_failed", "probe_timeout", "dependency_missing", "invalid_context",
    "stream_complete", "stream_idle_timeout", "hdd_playback_start",
    "hdd_playback_acknowledged", "hdd_playback_ended",
})


class _StreamError(RuntimeError):
    """Fixed error with no transport, media, or credential contents."""

    def __init__(self, *, reported: bool = False) -> None:
        super().__init__("video_unavailable")
        self.reported = reported


class _ClientDisconnected(Exception):
    """A closed HA reader is not a camera/source failure."""


async def _finish_uninterrupted(coroutine: Awaitable[Any]) -> Any:
    """Finish resource cleanup even when the owner is cancelled repeatedly."""
    task = asyncio.create_task(coroutine)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class _Session:
    def __init__(self) -> None:
        self.spawns: list[asyncio.Task] = []
        self.tasks: list[asyncio.Task] = []
        self.response: web.StreamResponse | None = None
        self.children: list[_Session] = []

    async def spawn(self, *args: str) -> asyncio.subprocess.Process:
        task = asyncio.create_task(asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=65536,
        ))
        # Track before awaiting, including cancellation during OS process spawn.
        self.spawns.append(task)
        return await asyncio.shield(task)

    def task(self, coroutine: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def close(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(child.close() for child in self.children))
        processes = []
        for task in self.spawns:
            with suppress(Exception):
                processes.append(await task)
        # Kill all producers before draining either pipe.
        for process in processes:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            if process.stdin is not None:
                with suppress(OSError, ValueError):
                    process.stdin.close()

        async def reap(process: asyncio.subprocess.Process) -> None:
            if process.stdout is not None:
                with suppress(OSError, ValueError):
                    while await process.stdout.read(65536):
                        pass
            await process.wait()

        await asyncio.gather(*(reap(process) for process in processes))


@dataclass
class _Prepared:
    session: _Session
    ffmpeg: asyncio.subprocess.Process
    feed: asyncio.Task
    first: bytes


async def _record(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    header = await reader.readexactly(5)
    kind, size = header[:1], int.from_bytes(header[1:], "big")
    maximum = MAX_STATUS_BYTES if kind == b"J" else MAX_VIDEO_BYTES if kind == b"V" else 0
    if not 0 < size <= maximum:
        raise _StreamError()
    return kind, await reader.readexactly(size)


def _status(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise _StreamError() from None
    if not isinstance(value, dict):
        raise _StreamError()
    return value


def _ffmpeg_args(binary: str, codec: str) -> tuple[str, ...]:
    return (
        binary, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-probesize", "32768", "-analyzeduration", "0",
        "-use_wallclock_as_timestamps", "1", "-f", codec, "-i", "pipe:0",
        "-an", "-sn", "-dn", "-c:v", "libx264", "-preset", "ultrafast",
        "-tune", "zerolatency", "-threads", "2", "-pix_fmt", "yuv420p",
        "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-bf", "0",
        "-fps_mode", "vfr", "-flush_packets", "1", "-mpegts_flags", "+resend_headers",
        "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1",
    )


class MeariVideoBridge:
    """One source for HA Stream; Stream shares its output among HA viewers."""

    def __init__(
        self, ffmpeg_binary: str,
        context_factory: Callable[[], Awaitable[Mapping[str, Any]]], *,
        source_mode: str = "live",
        _worker_path: Path | None = None,
    ) -> None:
        if source_mode not in ("live", "playback"):
            raise ValueError("invalid_source_mode")
        self._source_mode = source_mode
        self._ffmpeg_binary = ffmpeg_binary
        self._context_factory = context_factory
        self._worker_path = _worker_path or WORKER
        self._path = "/" + secrets.token_urlsafe(32) + ".ts"
        self._url: str | None = None
        self._runner: web.AppRunner | None = None
        self._active: asyncio.Task | None = None
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._warming: asyncio.Task | None = None
        self._prepared: _Prepared | None = None
        self._expiry: asyncio.Task | None = None
        self._recovery: asyncio.Task | None = None
        self._ever_streamed = False
        self._playback_consumed = False
        self._worker_stage: str | None = None
        self._source_codec: str | None = None
        self._last_frame_at: float | None = None
        self._attempts = 0
        self._failures = 0
        self._timeouts = 0
        self._frames = 0
        self._video_bytes = 0
        self._output_bytes = 0
        self._last_error_stage: str | None = None
        self._last_error_reason: str | None = None
        self.last_stage = "idle"

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return only fixed stages, counters and elapsed time, never IDs/URLs."""
        now = time.monotonic()
        return {
            "stage": self.last_stage, "worker_stage": self._worker_stage,
            "source_mode": self._source_mode, "source_codec": self._source_codec,
            "playback_consumed": self._playback_consumed,
            "output_codec": "h264", "connection_attempts": self._attempts,
            "reconnect_count": max(0, self._attempts - 1), "failure_count": self._failures,
            "timeout_count": self._timeouts, "video_frames": self._frames,
            "video_bytes": self._video_bytes, "output_bytes": self._output_bytes,
            "last_frame_age_seconds": None if self._last_frame_at is None else round(max(0, now - self._last_frame_at), 1),
            "last_error_stage": self._last_error_stage,
            "last_error_reason": self._last_error_reason,
            "recovery_in_progress": self._recovery is not None and not self._recovery.done(),
        }

    def _observe(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("probe_stage"), str) and event["probe_stage"] in WORKER_STAGES:
            self._worker_stage = event["probe_stage"]
        if event.get("codec") in ("h264", "hevc"):
            self._source_codec = event["codec"]

    def _video(self, payload: bytes) -> None:
        self._frames += 1
        self._video_bytes += len(payload)
        self._last_frame_at = time.monotonic()

    def _report(self, error: Exception) -> None:
        if isinstance(error, _StreamError) and error.reported:
            return
        self._failures += 1
        self._last_error_stage = self._worker_stage if self.last_stage == "connecting" else self.last_stage
        self._last_error_reason = "timeout" if isinstance(error, TimeoutError) else "source_error"
        if isinstance(error, TimeoutError):
            self._timeouts += 1
            self.last_stage = "stream_timeout"
        else:
            self.last_stage = "stream_failed"

    def _ensure_warm(self) -> asyncio.Task:
        if self._warming is None:
            self._warming = asyncio.create_task(self._warm())
            # A cancelled waiter may leave no one to retrieve a later failure.
            self._warming.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        return self._warming

    async def async_prepare(self) -> None:
        """Warm a shared pipeline before HA's short source-open timeout starts.

        Cancelling an image request leaves this bounded warm-up available for the
        next caller. An unclaimed pipeline expires without opening another one.
        """
        if self._closed:
            raise RuntimeError("video_bridge_closed")
        if self._active is not None:
            return
        if self._source_mode == "playback" and self._playback_consumed:
            raise RuntimeError("playback_selection_required")
        ready = await self._take_ready()
        if ready is not None:
            self._prepared = ready
            self._expiry = asyncio.create_task(self._expire(ready))
            return
        await asyncio.shield(self._ensure_warm())

    async def _warm(self) -> None:
        session = _Session()
        handed_off = False
        try:
            self.last_stage = "starting"
            ready = await self._prepare(session)
            if self._closed:
                raise _StreamError()
            self._prepared = ready
            handed_off = True
            self._expiry = asyncio.create_task(self._expire(ready))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._report(error)
            raise _StreamError(reported=True) from None
        finally:
            try:
                if not handed_off:
                    await _finish_uninterrupted(session.close())
            finally:
                self._warming = None

    async def _expire(self, ready: _Prepared) -> None:
        timer = asyncio.create_task(asyncio.sleep(PREPARED_TIMEOUT))
        try:
            if not ready.feed.done():
                await asyncio.wait((ready.feed, timer), return_when=asyncio.FIRST_COMPLETED)
            if ready.feed.done() and not ready.feed.cancelled() and ready.feed.exception() is not None:
                if self._prepared is ready:
                    self._prepared = None
                    self._report(ready.feed.exception())
                    await _finish_uninterrupted(ready.session.close())
                return
            await timer
        except asyncio.CancelledError:
            return
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        if self._prepared is ready:
            self._prepared = None
            await _finish_uninterrupted(ready.session.close())
            if self._active is None and not self._closed:
                self.last_stage = "idle"

    def _claim(self) -> _Prepared | None:
        ready, self._prepared = self._prepared, None
        if ready is not None and self._expiry is not None:
            self._expiry.cancel()
        return ready

    async def _take_ready(self) -> _Prepared | None:
        ready = self._claim()
        if ready is None:
            return None
        failure = ready.feed.cancelled() or (
            ready.feed.done() and ready.feed.exception() is not None
        ) or ready.ffmpeg.returncode not in (None, 0)
        if failure:
            error = None if ready.feed.cancelled() or not ready.feed.done() else ready.feed.exception()
            self._report(error or _StreamError())
            await _finish_uninterrupted(ready.session.close())
            return None
        return ready

    def _schedule_recovery(self) -> None:
        if (self._closed or self._source_mode != "live" or not self._ever_streamed
                or self._recovery is not None and not self._recovery.done()):
            return
        self._recovery = asyncio.create_task(self._recover())
        self._recovery.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )

    async def _recover(self) -> None:
        """Warm a replacement for HA's next HTTP retry, with finite backoff."""
        deadline = time.monotonic() + RECOVERY_TIMEOUT
        for attempt in range(RECOVERY_ATTEMPTS):
            delay = min(8.0, RECOVERY_BACKOFF * 2 ** attempt)
            if time.monotonic() + delay + FIRST_FRAME_TIMEOUT > deadline:
                return
            await asyncio.sleep(delay)
            if self._closed or self._prepared is not None:
                return
            # An active retry already obtains the same shared warm-up below.
            if self._active is not None:
                return
            try:
                await asyncio.shield(self._ensure_warm())
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def async_start(self) -> str:
        """Bind lazily without opening a device connection until the first GET."""
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("video_bridge_closed")
            if self._url is not None:
                return self._url
            app = web.Application(client_max_size=1024)
            app.router.add_get(self._path, self._handle, allow_head=False)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1, keepalive_timeout=5)
            self._runner = runner
            try:
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                address = runner.addresses[0]
                self._url = f"http://127.0.0.1:{address[1]}{self._path}"
                return self._url
            except BaseException:
                await _finish_uninterrupted(runner.cleanup())
                self._runner = None
                raise

    async def async_close(self) -> None:
        """Stop an active source and reap children before releasing the socket."""
        self._closed = True

        async def close() -> None:
            async with self._start_lock:
                recovery, self._recovery = self._recovery, None
                if recovery is not None:
                    recovery.cancel()
                    await asyncio.gather(recovery, return_exceptions=True)
                warming = self._warming
                if warming is not None:
                    warming.cancel()
                    await asyncio.gather(warming, return_exceptions=True)
                task = self._active
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                ready = self._claim()
                expiry, self._expiry = self._expiry, None
                if expiry is not None:
                    expiry.cancel()
                    await asyncio.gather(expiry, return_exceptions=True)
                if ready is not None:
                    await ready.session.close()
                runner, self._runner = self._runner, None
                if runner is not None:
                    await runner.cleanup()
                self._url = None
                self.last_stage = "bridge_closed"

        await _finish_uninterrupted(close())

    async def _watch_disconnect(self, request: web.Request) -> None:
        while True:
            transport = request.transport
            if transport is None or transport.is_closing():
                return
            await asyncio.sleep(0.25)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if (self._closed or self._active is not None
                or self._source_mode == "playback" and self._playback_consumed):
            return web.Response(status=503, text="Video unavailable")
        self._active = asyncio.current_task()
        session = _Session()
        recover = False
        try:
            self.last_stage = "starting"
            run = session.task(self._run(request, session))
            disconnected = session.task(self._watch_disconnect(request))
            done, _ = await asyncio.wait((run, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if run in done:
                response = await run
                transport = request.transport
                recover = transport is not None and not transport.is_closing()
                return response
            # Client disconnect must also interrupt a silent source/bootstrap.
            self.last_stage = "source_closed"
        except asyncio.CancelledError:
            raise
        except _ClientDisconnected:
            self.last_stage = "source_closed"
        except TimeoutError as error:
            self._report(error)
            recover = True
        except Exception as error:
            # Never expose subprocess errors, private factory failures or media.
            self._report(error)
            recover = True
        finally:
            try:
                await _finish_uninterrupted(session.close())
            finally:
                self._active = None
                if recover:
                    self._schedule_recovery()
        if session.response is not None:
            session.response.force_close()
            return session.response
        return web.Response(status=503, text="Video unavailable")

    async def _prepare(self, session: _Session) -> _Prepared:
        async with asyncio.timeout(FIRST_FRAME_TIMEOUT):
            self._attempts += 1
            self._worker_stage = None
            context = await self._context_factory()
            try:
                body = json.dumps(context, separators=(",", ":"), allow_nan=False).encode()
            except (ValueError, TypeError, RecursionError):
                raise _StreamError() from None
            if not isinstance(context, Mapping) or len(body) > MAX_CONTEXT_BYTES:
                raise _StreamError()
            dependency_args: tuple[str, ...] = ()
            spec = find_spec("cryptography")
            if spec is not None and spec.origin is not None:
                dependency_args = ("--dependency-root", str(Path(spec.origin).parent.parent))
            self.last_stage = "connecting"
            worker = await session.spawn(
                sys.executable, "-I", "-u", "-B", str(self._worker_path),
                "--stream", *dependency_args,
            )
            worker.stdin.write(body + b"\n")
            await worker.stdin.drain()
            worker.stdin.close()
            codec: str | None = None
            for _ in range(128):
                kind, payload = await _record(worker.stdout)
                if kind == b"V":
                    if codec is None:
                        raise _StreamError()
                    break
                event = _status(payload)
                self._observe(event)
                if event.get("final") is True:
                    raise _StreamError()
                if event.get("probe_stage") == "video_keyframe_received":
                    if (event.get("codec") not in ("h264", "hevc")
                            or event.get("keyframe_received") is not True
                            or event.get("parameter_sets_received") is not True):
                        raise _StreamError()
                    codec = event["codec"]
            else:
                raise _StreamError()
            ffmpeg = await session.spawn(*_ffmpeg_args(self._ffmpeg_binary, codec))
            self.last_stage = "transcoding"
            self._video(payload)
            ffmpeg.stdin.write(payload)
            await ffmpeg.stdin.drain()
            feed = session.task(self._feed(worker.stdout, ffmpeg.stdin, codec))
            # The HTTP response starts only after FFmpeg produces output.
            output = asyncio.create_task(ffmpeg.stdout.read(65536))
            session.tasks.append(output)
            done, _ = await asyncio.wait((feed, output), return_when=asyncio.FIRST_COMPLETED)
            if feed in done and feed.exception() is not None:
                await feed
            first = await output
            if not first:
                raise _StreamError()
            return _Prepared(session, ffmpeg, feed, first)

    async def _run(self, request: web.Request, session: _Session) -> web.StreamResponse:
        async with asyncio.timeout(SESSION_TIMEOUT):
            if self._warming is not None:
                await asyncio.shield(self._warming)
            ready = await self._take_ready()
            if ready is None:
                if self._ever_streamed and self._source_mode == "live":
                    # HA retries the saved URL without calling camera.prepare.
                    # Keep a slow retry warm-up alive across its 30s open limit.
                    await asyncio.shield(self._ensure_warm())
                    ready = await self._take_ready()
                    if ready is None:
                        raise _StreamError()
                    session.children.append(ready.session)
                else:
                    ready = await self._prepare(session)
            else:
                session.children.append(ready.session)
            ffmpeg, feed = ready.ffmpeg, ready.feed
            if feed.done() and feed.exception() is not None:
                await feed
            response = web.StreamResponse(headers={
                "Content-Type": "video/mp2t", "Cache-Control": "no-store",
            })
            session.response = response
            try:
                await response.prepare(request)
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await response.write(ready.first)
            except OSError:
                raise _ClientDisconnected() from None
            self._output_bytes += len(ready.first)
            self._ever_streamed = True
            if self._source_mode == "playback":
                self._playback_consumed = True
            self.last_stage = "streaming"
            copy = session.task(self._copy(ffmpeg.stdout, response))
            done, _ = await asyncio.wait((feed, copy), return_when=asyncio.FIRST_COMPLETED)
            if feed in done:
                await feed
                # An orderly worker EOF closes FFmpeg's input and flushes output.
                await copy
            else:
                await copy
                if not feed.done():
                    raise _StreamError()
            if ffmpeg.returncode not in (None, 0):
                raise _StreamError()
            self.last_stage = "source_closed"
            return response

    async def _feed(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, codec: str) -> None:
        last_video = asyncio.get_running_loop().time()
        statuses = 0
        try:
            while True:
                remaining = MEDIA_TIMEOUT - (asyncio.get_running_loop().time() - last_video)
                async with asyncio.timeout(max(0, remaining)):
                    try:
                        kind, payload = await _record(reader)
                    except asyncio.IncompleteReadError as err:
                        if err.partial:
                            raise _StreamError() from None
                        return
                if kind == b"J":
                    statuses += 1
                    if statuses > 4096:
                        raise _StreamError()
                    event = _status(payload)
                    self._observe(event)
                    if event.get("final") is True:
                        if event.get("probe_stage") == "stream_complete":
                            return
                        if event.get("probe_stage") == "stream_idle_timeout":
                            raise TimeoutError()
                        raise _StreamError()
                    if "codec" in event and event["codec"] != codec:
                        raise _StreamError()
                    continue
                last_video = asyncio.get_running_loop().time()
                self._video(payload)
                writer.write(payload)
                async with asyncio.timeout(WRITE_TIMEOUT):
                    await writer.drain()
        finally:
            with suppress(OSError, ValueError):
                writer.close()

    async def _copy(self, reader: asyncio.StreamReader, response: web.StreamResponse) -> None:
        while True:
            async with asyncio.timeout(MEDIA_TIMEOUT):
                payload = await reader.read(65536)
            if not payload:
                return
            async with asyncio.timeout(WRITE_TIMEOUT):
                try:
                    await response.write(payload)
                except OSError:
                    raise _ClientDisconnected() from None
            self._output_bytes += len(payload)
