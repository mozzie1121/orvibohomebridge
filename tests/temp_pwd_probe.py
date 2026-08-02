"""临时密码下发探针（cmd=246，SSL 长连接）。

对照 lock6.log 实抓 + APK 逆向（core.c.X → cmd 246）实现：
发送 type/effectTime/startTime/endTime/number/phone/userName 到门锁，
服务器返回 code/password 即 6 位临时密码，可在门锁上直接输入验证。

用法：
  python tests/temp_pwd_probe.py                     # type=2 临时密码，24h，限 1 次
  python tests/temp_pwd_probe.py --type 1 --hours 48 # 限时密码 48 小时
  python tests/temp_pwd_probe.py --type 2 --minutes 30 --number 1 --name "测试临时密码"

环境变量：ORVIBO_USERNAME / ORVIBO_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orvibo_probe import Https, Ssl, _is_lock_device  # noqa: E402
from orvibohomebridge.const import (  # noqa: E402
    CMD_HEARTBEAT,
    CMD_HELLO,
    CMD_HANDSHAKE,
    CMD_LOGIN,
    DEFAULT_KEY,
    DEBUG_INFO,
    SOFTWARE_VER,
)
from orvibohomebridge.functions import (  # noqa: E402
    generate_serial,
    generate_uuid,
)
from orvibohomebridge.packet import HomematePacket  # noqa: E402

LOGGER = logging.getLogger("temp_pwd_probe")
CMD_TEMP_PWD = 246
CMD_DELETE_AUTH = 247
CMD_QUERY_LIST = 171


class TempPwdSsl(Ssl):
    """在现有 Ssl 基础上增加 cmd=246 下发与单次响应等待。"""

    async def request_temp_password(
        self,
        device_id: str,
        device_uid: str,
        auth_type: int,
        minutes: int,
        number: int,
        name: str,
        start_offset: int = 0,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        phone: str = "",
        timeout: float = 20.0,
    ) -> Optional[Dict[str, Any]]:
        now = int(time.time()) + start_offset
        # type=1 限时密码：可用 --start/--end 指定绝对时间段，否则按 minutes 计算；
        # type=2 临时密码按 effectTime 相对时长（endTime=0）
        if auth_type == 1 and start_ts and end_ts:
            start_time, end_time = start_ts, end_ts
        else:
            start_time = now
            end_time = now + minutes * 60 if auth_type == 1 else 0
        payload = {
            "deviceId": device_id,
            "uid": device_uid,
            "userName": name,
            "type": auth_type,
            "effectTime": minutes,
            "startTime": start_time,
            "endTime": end_time,
            "number": number,
            "phone": phone,
            "cmd": CMD_TEMP_PWD,
            "serial": generate_serial(),
            "clientType": 1,
            "uniSerial": generate_serial(use_time=True),
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        LOGGER.info(
            "cmd=246 已发送: device=%s type=%s minutes=%s number=%s name=%s start=%s end=%s",
            device_id,
            auth_type,
            minutes,
            number,
            name,
            payload["startTime"],
            payload["endTime"],
        )
        LOGGER.debug("payload=%s", json.dumps(payload, ensure_ascii=False))
        await self._send(payload, self.key)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hdr = await asyncio.wait_for(self.r.readexactly(42), timeout=5)
                ln = HomematePacket.parse_length(hdr)
                body = await asyncio.wait_for(self.r.readexactly(ln - 42), timeout=10)
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                LOGGER.error("SSL 连接中断")
                return None
            try:
                pkt = HomematePacket(
                    hdr + body, {(self.sid or ""): self.key or DEFAULT_KEY.encode()}
                )
            except Exception:
                continue
            d = pkt.json_payload
            if not isinstance(d, dict):
                continue
            cmd = d.get("cmd")
            if cmd == CMD_HELLO:
                kv = d.get("key")
                if kv:
                    self.key, self.sid = str(kv).encode(), bytes(pkt.session_id).decode()
                continue
            if cmd in (CMD_LOGIN, CMD_HEARTBEAT, CMD_HANDSHAKE):
                continue
            if cmd == CMD_TEMP_PWD:
                LOGGER.info("cmd=246 响应收到")
                return d
            LOGGER.debug("忽略中间包 cmd=%s", cmd)
        LOGGER.error("等待 cmd=246 响应超时")
        return None

    async def delete_authorization(
        self,
        device_id: str,
        device_uid: str,
        authorized_id: int,
        timeout: float = 15.0,
    ) -> Optional[Dict[str, Any]]:
        """删除指定授权（cmd=247，authorizedId 来自下发响应）。"""
        payload = {
            "uid": device_uid,
            "deviceId": device_id,
            "userId": authorized_id,
            "cmd": CMD_DELETE_AUTH,
            "serial": generate_serial(),
            "clientType": 1,
            "uniSerial": generate_serial(use_time=True),
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        LOGGER.info("cmd=247 已发送: device=%s authorizedId=%s", device_id, authorized_id)
        await self._send(payload, self.key)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hdr = await asyncio.wait_for(self.r.readexactly(42), timeout=5)
                ln = HomematePacket.parse_length(hdr)
                body = await asyncio.wait_for(self.r.readexactly(ln - 42), timeout=10)
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                LOGGER.error("SSL 连接中断")
                return None
            try:
                pkt = HomematePacket(
                    hdr + body, {(self.sid or ""): self.key or DEFAULT_KEY.encode()}
                )
            except Exception:
                continue
            d = pkt.json_payload
            if not isinstance(d, dict):
                continue
            cmd = d.get("cmd")
            if cmd == CMD_HELLO:
                kv = d.get("key")
                if kv:
                    self.key, self.sid = str(kv).encode(), bytes(pkt.session_id).decode()
                continue
            if cmd in (CMD_LOGIN, CMD_HEARTBEAT, CMD_HANDSHAKE):
                continue
            if cmd == CMD_DELETE_AUTH:
                return d
            LOGGER.debug("忽略中间包 cmd=%s", cmd)
        LOGGER.error("等待 cmd=247 响应超时")
        return None

    async def query_authorization_list(
        self,
        family_id: str,
        timeout: float = 15.0,
    ) -> Optional[Dict[str, Any]]:
        """发送 cmd=171 查询授权/消息列表，返回原始响应（供确认结构）。"""
        payload = {
            "familyId": family_id,
            "lastUpdateTime": 0,
            "start": 0,
            "limit": 50,
            "cmd": CMD_QUERY_LIST,
            "serial": generate_serial(),
            "clientType": 1,
            "uniSerial": generate_serial(use_time=True),
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        LOGGER.info("cmd=171 已发送: family=%s", family_id)
        await self._send(payload, self.key)

        deadline = time.time() + timeout
        collected = []
        while time.time() < deadline:
            try:
                hdr = await asyncio.wait_for(self.r.readexactly(42), timeout=5)
                ln = HomematePacket.parse_length(hdr)
                body = await asyncio.wait_for(self.r.readexactly(ln - 42), timeout=10)
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                LOGGER.error("SSL 连接中断")
                break
            try:
                pkt = HomematePacket(
                    hdr + body, {(self.sid or ""): self.key or DEFAULT_KEY.encode()}
                )
            except Exception:
                continue
            d = pkt.json_payload
            if not isinstance(d, dict):
                continue
            cmd = d.get("cmd")
            if cmd == CMD_HELLO:
                kv = d.get("key")
                if kv:
                    self.key, self.sid = str(kv).encode(), bytes(pkt.session_id).decode()
                continue
            if cmd in (CMD_LOGIN, CMD_HEARTBEAT, CMD_HANDSHAKE):
                continue
            if cmd == CMD_QUERY_LIST or "authorizedUnlock" in json.dumps(d):
                collected.append(d)
                if cmd == CMD_QUERY_LIST:
                    break
        if not collected:
            LOGGER.error("未收到 cmd=171 响应")
            return None
        return collected


def extract_password(resp: Dict[str, Any]) -> Optional[str]:
    """从响应里提取临时密码（code 或 authorizedUnlock.password）。"""
    code = resp.get("code")
    if isinstance(code, str) and code:
        return code
    auth = resp.get("authorizedUnlock")
    if isinstance(auth, dict):
        pwd = auth.get("password")
        if isinstance(pwd, str) and pwd:
            return pwd
    return None


def parse_time(value: str) -> int:
    """解析时间：支持 'yyyy-MM-dd HH:mm'（或带秒）或 Unix 秒时间戳。"""
    v = value.strip()
    if v.isdigit():
        return int(v)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(v, fmt).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"无法解析时间: {value}（支持 yyyy-MM-dd HH:mm 或 Unix 秒时间戳）")


async def main() -> int:
    parser = argparse.ArgumentParser(description="临时密码下发探针（cmd=246）")
    parser.add_argument("--type", type=int, default=2, help="授权类型：1=限时 2=临时 3=周期（默认 2）")
    parser.add_argument("--minutes", type=int, default=1440, help="有效期（分钟，默认 1440=24h）")
    parser.add_argument("--number", type=int, default=None, help="可用次数（type=2 默认 1；type=1 默认 0 不限）")
    parser.add_argument("--name", default="", help="临时用户名（默认自动生成）")
    parser.add_argument("--start-offset", type=int, default=0, help="开始时间偏移（秒，type=1 可设未来时间）")
    parser.add_argument("--start", default="", help="开始时间 yyyy-MM-dd HH:mm（type=1 用，替代默认 now）")
    parser.add_argument("--end", default="", help="结束时间 yyyy-MM-dd HH:mm（type=1 用，替代默认 now+minutes）")
    parser.add_argument("--phone", default="", help="下发时同步短信通知的手机号（可选）")
    parser.add_argument("--delete", type=int, default=0, help="删除指定 authorizedId 的授权（不下发）")
    parser.add_argument("--list-auth", action="store_true", help="查询授权/消息列表（cmd=171，打印原始响应）")
    parser.add_argument(
        "--list", action="store_true", help="仅列出门锁信息（预留，暂未实现 cmd171 查询）"
    )
    parser.add_argument("--family-id", help="家庭 ID（默认第一个家庭）")
    parser.add_argument("--device-id", help="门锁 deviceId（w- 前缀；默认自动识别）")
    parser.add_argument("--device-uid", help="门锁 uid（无 w- 前缀；默认自动识别）")
    args = parser.parse_args()

    username = os.environ.get("ORVIBO_USERNAME") or ""
    password = os.environ.get("ORVIBO_PASSWORD") or ""
    if not username or not password:
        print("请设置环境变量 ORVIBO_USERNAME / ORVIBO_PASSWORD")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    api = Https(username, password)
    if not await api.login():
        print("登录失败")
        return 1
    print(f"[1/4] 登录成功 user_id={api.uid[:8]}...")

    fs = await api.families()
    if not fs:
        print("账号下没有家庭")
        return 1
    fam = next((f for f in fs if f.get("familyId") == args.family_id), None) if args.family_id else fs[0]
    if not fam:
        print(f"未找到家庭 {args.family_id}")
        return 1
    fid = fam.get("familyId", "")
    print(f"[2/4] 家庭: {fam.get('familyName', '?')} ({fid[:8]}...)")

    devs = await api.devices(fid)
    if args.device_id and args.device_uid:
        dev = {"deviceId": args.device_id, "uid": args.device_uid}
    else:
        dev = next((d for d in devs if _is_lock_device(d)), None)
        if not dev:
            print("未识别到门锁，请用 --device-id / --device-uid 指定")
            return 1
    device_id, device_uid = dev["deviceId"], dev["uid"]
    print(f"[3/4] 门锁: {dev.get('deviceName', '?')} deviceId={device_id} uid={device_uid}")

    if args.delete:
        sslc = TempPwdSsl(api.username, api.password, fid)
        if not await sslc.connect():
            print("SSL 连接/登录失败")
            await api.close()
            return 1
        resp = await sslc.delete_authorization(device_id, device_uid, args.delete)
        await sslc.close()
        await api.close()
        if not resp:
            print("未收到 cmd=247 响应")
            return 1
        print(f"删除响应: {json.dumps(resp, ensure_ascii=False)[:400]}")
        if resp.get("status") in (None, 0, "0"):
            print(f"删除成功 authorizedId={args.delete}")
            return 0
        print(f"删除失败 status={resp.get('status')}")
        return 1

    if args.list_auth:
        sslc = TempPwdSsl(api.username, api.password, fid)
        if not await sslc.connect():
            print("SSL 连接/登录失败")
            await api.close()
            return 1
        result = await sslc.query_authorization_list(fid)
        await sslc.close()
        await api.close()
        if not result:
            print("无响应")
            return 1
        for item in result:
            print(json.dumps(item, ensure_ascii=False)[:1200])
        return 0

    name = args.name or f"临时用户 {time.strftime('%m%d%H%M')}"
    sslc = TempPwdSsl(api.username, api.password, fid)
    if not await sslc.connect():
        print("SSL 连接/登录失败")
        await api.close()
        return 1
    number = args.number if args.number is not None else (1 if args.type == 2 else 0)
    if args.type == 3:
        print("type=3 周期密码需要 day（星期）参数，暂未实现，请先用 type=1/2")
        await sslc.close()
        await api.close()
        return 1
    start_ts = parse_time(args.start) if args.start else None
    end_ts = parse_time(args.end) if args.end else None
    if args.type == 1 and (start_ts or end_ts) and not (start_ts and end_ts):
        print("type=1 指定时间段需要同时提供 --start 和 --end")
        await sslc.close()
        await api.close()
        return 1
    if start_ts and end_ts and end_ts <= start_ts:
        print("结束时间必须晚于开始时间")
        await sslc.close()
        await api.close()
        return 1
    resp = await sslc.request_temp_password(
        device_id,
        device_uid,
        args.type,
        args.minutes,
        number,
        name,
        start_offset=args.start_offset,
        start_ts=start_ts,
        end_ts=end_ts,
        phone=args.phone,
    )
    await sslc.close()
    await api.close()
    if not resp:
        print("未收到 cmd=246 响应")
        return 1

    print(f"[4/4] cmd=246 响应: {json.dumps(resp, ensure_ascii=False)[:600]}")
    if resp.get("status") not in (None, 0, "0"):
        print(f"下发失败 status={resp.get('status')} msg={resp.get('msg')}")
        return 1
    pwd = extract_password(resp)
    if not pwd:
        print("响应里未找到临时密码")
        return 1
    auth = resp.get("authorizedUnlock") or {}
    print("\n========================================")
    print(f"  临时密码: {pwd}")
    print(f"  用户名:   {name}")
    print(f"  类型:     {args.type}（1=限时 2=临时 3=周期）")
    print(f"  有效期:   {args.minutes} 分钟")
    print(f"  次数:     {args.number if args.number else '不限'}")
    print(f"  短信通知: {'发送到 ' + args.phone if args.phone else '未开启'}")
    if resp.get("startTime"):
        print(
            "  开始时间: %s (%s)",
            datetime.fromtimestamp(int(resp["startTime"])).strftime("%Y-%m-%d %H:%M"),
            resp["startTime"],
        )
    if resp.get("endTime"):
        print(
            "  结束时间: %s (%s)",
            datetime.fromtimestamp(int(resp["endTime"])).strftime("%Y-%m-%d %H:%M"),
            resp["endTime"],
        )
    if auth:
        print(f"  authorizedId: {auth.get('authorizedId')}")
        print(f"  unlockNum:    {auth.get('unlockNum')}")
    print("========================================")
    print("请在门锁上输入该 6 位密码验证")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
