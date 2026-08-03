"""Home Assistant orchestration for lock snapshots, videos, and history."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path
import time
from typing import Any, Dict, MutableMapping, Optional
import urllib.error
import urllib.request

from .cos_media import CosMediaManager
from .device_types import DeviceCategory, classify_device
from .history import history_dir, save_snapshot
from .redact import fingerprint
from .video_archive import VideoArchiver, normalize_event_object_key


_LOGGER = logging.getLogger(__name__)


def _download_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    """Download a pre-signed URL from an executor thread."""

    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "orvibohomebridge"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read(600).decode("utf-8", "replace")
        _LOGGER.warning("下载失败 HTTP %s: %s", error.code, body[:500])
        return None
    except Exception as error:  # noqa: BLE001
        _LOGGER.warning("下载失败: %r", error)
        return None


class LockMediaManager:
    """Own lock media credentials, camera updates, archives, and cleanup."""

    HISTORY_KEEP_DAYS = 7

    def __init__(
        self,
        hass: Any,
        devices: MutableMapping[str, dict[str, Any]],
        redaction_salt: bytes,
    ) -> None:
        self.hass = hass
        self.devices = devices
        self._redaction_salt = redaction_salt
        self.cos: Optional[CosMediaManager] = None
        self.archiver: Optional[VideoArchiver] = None
        self._cameras: Dict[str, Any] = {}
        self._snapshot_pending: set[tuple[str, str]] = set()
        self._history_cleanup_unsub = None

    def configure(self, ssl_client: Any, user_id: str, family_id: str) -> None:
        """Configure media helpers after the SSL client has been created."""

        self.cos = CosMediaManager(ssl_client, user_id, family_id)
        self.archiver = VideoArchiver(
            media_root=self.hass.config.path("media"),
        )

    def attach_urls(
        self,
        device_id: str,
        raw_status: dict[str, Any],
        event: dict[str, Any],
    ) -> Dict[str, str]:
        """Attach cached signed URLs and schedule background media work."""

        cos = self.cos
        if cos is None or not event:
            return {}
        uid = raw_status.get("uid") or event.get("uid") or ""
        if not uid:
            uid = (self.devices.get(device_id) or {}).get("uid", "")
        event_ts = event.get("time") or int(time.time())
        kind = event.get("snapshot_kind") or event.get("kind", "event")
        out: Dict[str, str] = {}
        for field, target in (
            ("video_url", "media_url"),
            ("pic_url", "pic_media_url"),
            ("doorbell_url", "doorbell_media_url"),
        ):
            object_key = event.get(field)
            if not object_key:
                continue
            url = cos.try_signed_url(device_id, uid, object_key)
            if url:
                out[target] = url
            elif cos.cached_credentials(device_id) is None:
                self.hass.async_create_task(cos.get_credentials(device_id, uid))
            if field == "video_url":
                self._schedule_video_archive(
                    device_id, object_key, url, out, kind, event_ts
                )

        snapshot_key = event.get("pic_url") or event.get("doorbell_url")
        if snapshot_key:
            self.hass.async_create_task(
                self._update_snapshot(device_id, uid, snapshot_key, kind, event_ts)
            )
        return out

    def register_camera(self, device_id: str, camera: Any) -> None:
        self._cameras[device_id] = camera
        _LOGGER.debug(
            "门锁截图实体已注册 device=%s",
            fingerprint(device_id, self._redaction_salt),
        )

    async def _update_snapshot(
        self,
        device_id: str,
        device_uid: str,
        object_key: str,
        kind: str,
        ts: int | str,
    ) -> None:
        dedup_key = (device_id, object_key)
        if dedup_key in self._snapshot_pending:
            return
        self._snapshot_pending.add(dedup_key)
        camera = self._cameras.get(device_id)
        cos = self.cos
        if camera is None or cos is None:
            self._snapshot_pending.discard(dedup_key)
            return
        image: Optional[bytes] = None
        try:
            url = await cos.signed_url(device_id, device_uid, object_key)
            if not url:
                _LOGGER.warning(
                    "门锁截图签名 URL 获取失败 device=%s",
                    fingerprint(device_id, self._redaction_salt),
                )
                return
            for attempt, delay in enumerate((8, 10, 20, 25)):
                if delay:
                    await asyncio.sleep(delay)
                image = await self.hass.async_add_executor_job(_download_bytes, url)
                if image:
                    break
                _LOGGER.debug(
                    "截图下载重试 %s/4 device=%s",
                    attempt + 1,
                    fingerprint(device_id, self._redaction_salt),
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "门锁截图更新异常 device=%s",
                fingerprint(device_id, self._redaction_salt),
            )
            return
        finally:
            self._snapshot_pending.discard(dedup_key)

        if not image:
            _LOGGER.warning(
                "门锁截图下载为空 device=%s key=%s",
                fingerprint(device_id, self._redaction_salt),
                object_key,
            )
            return
        try:
            await self.hass.async_add_executor_job(
                lambda: save_snapshot(
                    history_dir(self.hass.config.path("media"), device_id),
                    kind,
                    ts,
                    image,
                )
            )
        except OSError as error:
            _LOGGER.warning("截图落盘失败: %s", error)
        await camera.async_set_image(image)

    def _schedule_video_archive(
        self,
        device_id: str,
        object_key: str,
        signed_url: Optional[str],
        out: Dict[str, str],
        kind: str,
        ts: int | str,
    ) -> None:
        archiver = self.archiver
        if archiver is None:
            return
        _h264, mp4_path = archiver.event_paths(device_id, kind, ts)
        if mp4_path.exists() and mp4_path.stat().st_size > 0:
            out["video_file"] = str(mp4_path)
            out["media_id"] = archiver.media_source_id(mp4_path)
            return
        out["video_file"] = str(mp4_path)
        if signed_url:
            self.hass.async_create_task(
                self._archive_video(device_id, kind, ts, signed_url)
            )

    async def _archive_video(
        self, device_id: str, kind: str, ts: int | str, signed_url: str
    ) -> None:
        archiver = self.archiver
        if archiver is None:
            return
        try:
            result = await self.hass.async_add_executor_job(
                archiver.archive_event, device_id, kind, ts, signed_url
            )
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning(
                "录像归档失败 device=%s kind=%s: %s",
                fingerprint(device_id, self._redaction_salt),
                kind,
                error,
            )
            return
        if result and result.get("mp4_file") and Path(result["mp4_file"]).exists():
            _LOGGER.info(
                "录像已归档 device=%s -> %s",
                fingerprint(device_id, self._redaction_salt),
                result["mp4_file"],
            )

    async def fetch_video(
        self, device_id: str, object_key: str, device_uid: str = ""
    ) -> Dict[str, Any]:
        archiver = self.archiver
        cos = self.cos
        if archiver is None or cos is None:
            return {"error": "录像归档未就绪"}
        device = self.devices.get(device_id)
        if device is None or classify_device(device) != DeviceCategory.DOOR_LOCK:
            return {"error": "设备不存在或不是门锁"}
        object_key = normalize_event_object_key(object_key) or ""
        if not object_key:
            return {"error": "无效的事件录像对象键"}
        device_uid = device_uid or device.get("uid", "")
        url = await cos.signed_url(device_id, device_uid, object_key)
        if not url:
            return {"error": "无法获取 COS 凭证或签名 URL"}
        result = await self.hass.async_add_executor_job(
            archiver.archive, device_id, object_key, url
        )
        return result or {"error": "录像下载失败"}

    async def list_events(
        self, device_id: str = "", limit: int = 100
    ) -> list[dict[str, str]]:
        from .history import list_history

        return await self.hass.async_add_executor_job(
            list_history,
            self.hass.config.path("media"),
            device_id or None,
            limit,
        )

    async def prewarm(self) -> None:
        cos = self.cos
        if cos is None:
            return
        for device_id, device in self.devices.items():
            if classify_device(device) != DeviceCategory.DOOR_LOCK:
                continue
            try:
                await cos.get_credentials(device_id, device.get("uid", ""))
            except Exception as error:  # noqa: BLE001
                _LOGGER.warning(
                    "门锁媒体凭证预热失败 device=%s: %s",
                    fingerprint(device_id, self._redaction_salt),
                    error,
                )

    def start_cleanup(self) -> None:
        if self._history_cleanup_unsub is not None:
            return
        from homeassistant.helpers.event import async_track_time_interval
        from .history import cleanup

        async def run(_now=None) -> None:
            try:
                removed = await self.hass.async_add_executor_job(
                    cleanup,
                    self.hass.config.path("media"),
                    self.HISTORY_KEEP_DAYS,
                )
                if removed:
                    _LOGGER.info("历史清理完成，删除 %s 个过期文件", removed)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("历史清理异常", exc_info=True)

        self.hass.async_create_task(run())
        self._history_cleanup_unsub = async_track_time_interval(
            self.hass, run, timedelta(days=7)
        )

    def stop_cleanup(self) -> None:
        if self._history_cleanup_unsub is not None:
            self._history_cleanup_unsub()
            self._history_cleanup_unsub = None

    async def cleanup_history(
        self,
        keep_days: int = 7,
        device_id: str = "",
        max_entries: Optional[int] = None,
    ) -> int:
        from .history import cleanup

        return await self.hass.async_add_executor_job(
            cleanup,
            self.hass.config.path("media"),
            keep_days,
            device_id or None,
            max_entries,
        )
