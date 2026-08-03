"""电池推送探针：监听 SSL 推送，记录每个包的 cmd 与 batteryManager 字段。

用途：确认门锁电池状态通过什么命令、以什么频率推送（App 端实测电池
反馈接近实时，需要核对服务器推送规律）。

用法：
  python tests/battery_probe.py [监听秒数，默认 180]

环境变量：ORVIBO_USERNAME / ORVIBO_PASSWORD
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orvibo_probe import Https, Ssl  # noqa: E402
from orvibohomebridge.const import DEFAULT_KEY  # noqa: E402
from orvibohomebridge.packet import HomematePacket  # noqa: E402

LOGGER = logging.getLogger("battery_probe")


async def main() -> int:
    username = os.environ.get("ORVIBO_USERNAME") or ""
    password = os.environ.get("ORVIBO_PASSWORD") or ""
    if not username or not password:
        print("请设置环境变量 ORVIBO_USERNAME / ORVIBO_PASSWORD")
        return 2
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 180

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    api = Https(username, password)
    if not await api.login():
        print("登录失败")
        return 1
    fs = await api.families()
    if not fs:
        print("无家庭")
        return 1
    fid = fs[0].get("familyId", "")
    print(f"家庭: {fid}")

    sslc = Ssl(username, password, fid)
    if not await sslc.connect():
        print("SSL 连接失败")
        await api.close()
        return 1

    print(f"监听 {duration} 秒，请在 App 打开门锁电量页面 / 操作门锁触发推送...")
    start = time.time()
    battery_seen = 0
    while time.time() - start < duration:
        try:
            hdr = await asyncio.wait_for(sslc.r.readexactly(42), timeout=5)
            ln = HomematePacket.parse_length(hdr)
            body = await asyncio.wait_for(sslc.r.readexactly(ln - 42), timeout=10)
        except asyncio.TimeoutError:
            continue
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            print("连接断开")
            break
        try:
            pkt = HomematePacket(
                hdr + body, {(sslc.sid or ""): sslc.key or DEFAULT_KEY.encode()}
            )
        except Exception:
            continue
        d = pkt.json_payload
        if not isinstance(d, dict):
            continue
        cmd = d.get("cmd")
        if cmd in (0, 2, 32, 6):
            continue
        props = d.get("properties") or {}
        bat = props.get("batteryManager") if isinstance(props, dict) else None
        bat1 = props.get("batteryManager1") if isinstance(props, dict) else None
        ts = f"{time.time() - start:.1f}s"
        if bat or bat1:
            battery_seen += 1
            print(f"[{ts}] 🪫 cmd={cmd} batteryManager={bat} batteryManager1={bat1}")
        else:
            print(f"[{ts}] cmd={cmd} action={d.get('action', '')} props_keys={list(props.keys()) if isinstance(props, dict) else '-'}")

    await sslc.close()
    await api.close()
    print(f"监听结束，共 {battery_seen} 个包含电池的包")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
