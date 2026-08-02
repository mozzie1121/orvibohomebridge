"""门锁事件历史归档：截图/视频统一落盘到 media 目录，供回溯浏览。

目录结构：
  config/media/orvibohomebridge/<device_id>/<kind>_<时间戳>.(jpg|h264|mp4)

HA 媒体浏览器按目录/文件名自然形成时间倒序的历史列表；list_events
服务动态扫描目录返回结构化记录，供自动化与自定义卡片使用。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

_FILE_PATTERN = re.compile(r"^([A-Za-z0-9_]+)_(\d+)\.(\w+)$")


def history_dir(media_root: Path, device_id: str) -> Path:
    """返回设备归档目录（自动创建）。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(device_id)) or "device"
    d = Path(media_root) / "orvibohomebridge" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_path(history_dir: Path, kind: str, ts: int | str) -> Path:
    """事件截图目标路径（<kind>_<ts>.jpg）。"""
    kind = re.sub(r"[^A-Za-z0-9_]", "_", str(kind)) or "event"
    return Path(history_dir) / f"{kind}_{ts}.jpg"


def save_snapshot(
    history_dir: Path,
    kind: str,
    ts: int | str,
    image_bytes: bytes,
) -> Path:
    """保存事件截图；同事件已存在则跳过，返回实际路径。"""
    dest = snapshot_path(history_dir, kind, ts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.write_bytes(image_bytes)
    return dest


def media_source_id(media_root: Path, file_path: Path) -> str:
    """生成 HA media_source 引用 ID（config/media 为根）。"""
    rel = Path(file_path).resolve().relative_to(Path(media_root).resolve())
    return f"media-source://media_source/{rel.as_posix()}"


def list_history(
    media_root: Path,
    device_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, str]]:
    """扫描归档目录，按时间倒序返回历史记录。

    每条记录：{device_id, kind, time, type, file, media_id}。
    """
    root = Path(media_root) / "orvibohomebridge"
    if not root.is_dir():
        return []
    entries: list[dict[str, str]] = []
    dirs = [root / device_id] if device_id else sorted(root.iterdir(), reverse=True)
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir(), reverse=True):
            if not f.is_file():
                continue
            m = _FILE_PATTERN.match(f.name)
            if not m:
                continue
            kind, ts, ext = m.groups()
            media_type = (
                "video" if ext.lower() in ("mp4", "h264") else "image"
            )
            entries.append(
                {
                    "device_id": d.name,
                    "kind": kind,
                    "time": ts,
                    "type": media_type,
                    "file": str(f),
                    "media_id": media_source_id(media_root, f),
                }
            )
    entries.sort(key=lambda e: int(e["time"]), reverse=True)
    return entries[: max(1, int(limit))]


def cleanup(
    media_root: Path,
    keep_days: int = 7,
    device_id: Optional[str] = None,
    max_entries: Optional[int] = None,
) -> int:
    """清理历史归档：删除超过 keep_days 天的文件，可选按条数裁剪。

    按文件名时间戳判断（<kind>_<ts>.<ext>）；空目录一并删除。
    返回删除的文件数。
    """
    root = Path(media_root) / "orvibohomebridge"
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(0, int(keep_days)) * 86400
    dirs = [root / device_id] if device_id else list(root.iterdir())
    removed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        files = [f for f in d.iterdir() if f.is_file()]
        # 按文件名时间戳分批处理
        entries: list[tuple[int, Path]] = []
        for f in files:
            m = _FILE_PATTERN.match(f.name)
            if m:
                try:
                    entries.append((int(m.group(2)), f))
                except ValueError:
                    continue
        # 超过 keep_days 的删除
        for ts, f in entries:
            if ts < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # 按条数裁剪（保留最近 max_entries 条）
        if max_entries and len(entries) > max_entries:
            for ts, f in sorted(entries, key=lambda e: e[0])[: len(entries) - max_entries]:
                if not f.exists():
                    continue
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # 删除空目录
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed
