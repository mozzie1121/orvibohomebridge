"""门锁事件录像归档：下载 .h264 事件录像并转封装为 MP4。

事件录像（撬锁/离家告警等）的 videoUrl 是 COS 对象键，可通过预签名 URL
直接下载（URL 自带 x-cos-security-token）。裸 H.264 无法被浏览器/HA
直接播放，用 ffmpeg 无损转封装（-c copy，不改编码）成 MP4 后存到
HA 标准 media 目录，媒体浏览器与通知即可引用。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# 事件录像对象键形如 /uid/videoPicklockEvent/picklockEvent_<ts>.h264
_EVENT_PATTERN = re.compile(
    r"/(?:video|picture)([A-Za-z]+)Event/(?:[A-Za-z]+)_(\d+)\.\w+$"
)


def _sanitize(name: str) -> str:
    """文件名安全化：只保留字母数字与 _-。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "device"


def infer_event_name(object_key: str) -> Optional[tuple[str, str]]:
    """从对象键推断事件名与时间戳，如 ('picklock', '1785652830')。"""
    m = _EVENT_PATTERN.search(object_key.replace("\\", "/"))
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def build_media_paths(
    media_root: Path,
    device_id: str,
    object_key: str,
) -> Optional[tuple[Path, Path]]:
    """返回 (h264 原始路径, mp4 目标路径)；无法推断事件名时返回 None。"""
    info = infer_event_name(object_key)
    if not info:
        return None
    kind, ts = info
    device_dir = media_root / "orvibohomebridge" / _sanitize(device_id)
    base = device_dir / f"{kind}_{ts}"
    return base.with_suffix(".h264"), base.with_suffix(".mp4")


def find_ffmpeg() -> Optional[str]:
    """查找 ffmpeg 可执行文件（HA 环境通常自带）。"""
    return shutil.which("ffmpeg")


def download(url: str, dest: Path, timeout: int = 60) -> bool:
    """下载预签名 URL 到目标路径（同步阻塞，调用方应放线程池）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "orvibohomebridge"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        tmp.replace(dest)
        return True
    except (urllib.error.URLError, OSError) as e:
        _LOGGER.warning("录像下载失败: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def transcode_to_mp4(src: Path, dest: Path, ffmpeg: Optional[str]) -> bool:
    """ffmpeg -c copy 无损转封装 h264 → mp4；无 ffmpeg 时返回 False。

    转码成功后删除原始 h264（避免重复占用）；失败保留 h264 供手动处理。
    """
    if not ffmpeg or not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _LOGGER.warning("ffmpeg 转码失败: %s", e)
        return False
    if proc.returncode != 0 or not tmp.exists():
        _LOGGER.warning("ffmpeg 转码返回 %s: %s", proc.returncode, proc.stderr.strip()[:300])
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    tmp.replace(dest)
    try:
        src.unlink(missing_ok=True)
    except OSError:
        pass
    return True


class VideoArchiver:
    """事件录像下载 + 转码 + 文件管理。"""

    def __init__(
        self,
        media_root: Path,
        ffmpeg: Optional[str] = None,
    ) -> None:
        self._media_root = Path(media_root)
        self._ffmpeg = ffmpeg if ffmpeg else find_ffmpeg()

    @property
    def ffmpeg_available(self) -> bool:
        return self._ffmpeg is not None

    def paths_for(self, device_id: str, object_key: str) -> Optional[tuple[Path, Path]]:
        """按对象键计算确定性文件路径（h264 原始, mp4 目标）。"""
        return build_media_paths(self._media_root, device_id, object_key)

    def media_source_id(self, mp4_path: Path) -> str:
        """生成 HA media_source 引用 ID（config/media 为根）。"""
        rel = mp4_path.resolve().relative_to(self._media_root.resolve())
        return f"media-source://media_source/{rel.as_posix()}"

    def archive(
        self,
        device_id: str,
        object_key: str,
        url: str,
    ) -> Optional[dict[str, str]]:
        """下载并转码录像，返回 {h264_file, mp4_file, media_id} 或 None。"""
        paths = self.paths_for(device_id, object_key)
        if paths is None:
            return None
        h264_path, mp4_path = paths
        if mp4_path.exists() and mp4_path.stat().st_size > 0:
            # 已归档过（同一事件去重）
            return self._result(h264_path, mp4_path)
        if not download(url, h264_path):
            return None
        transcode_to_mp4(h264_path, mp4_path, self._ffmpeg)
        return self._result(h264_path, mp4_path)

    def _result(self, h264_path: Path, mp4_path: Path) -> dict[str, str]:
        video_file = str(mp4_path if mp4_path.exists() else h264_path)
        media_id = (
            self.media_source_id(mp4_path)
            if mp4_path.exists()
            else ""
        )
        return {
            "h264_file": str(h264_path),
            "mp4_file": str(mp4_path),
            "video_file": video_file,
            "media_id": media_id,
        }
