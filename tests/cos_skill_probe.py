"""门锁媒体 Skill 授权探针（协议 cmd=313 + COS 签名下载）。

背景（APK jadx 逆向确认）：
- REST 接口 v2/cosToken/getCosToken 只签发日志上传桶（applog-*）的凭证，
  用它读门锁媒体会 403 AccessDenied（此前实测验证）；
- 门锁媒体下载走 Skill.GetCOSAuthorization（协议 cmd=313），请求体为
  {"request": "<内层 JSON 字符串>"}，内层含 namespace/requestId/version/
  userId/deviceId/familyId/uid/type=lock；
- 响应 QueryTxAuthResponse.security 携带 bucketName/region/endpoint/
  accessKeyId/accessKeySecret/securityToken/systemCurrentTime/expiration；
- 拿到凭证后按 COS 签名 v5（HMAC-SHA1）拼 Authorization 头下载对象。

本脚本复用 tests/orvibo_probe.py 的 REST 登录与 SSL 长连接实现。
凭据只从环境变量读取：ORVIBO_USERNAME / ORVIBO_PASSWORD

用法：
  python tests/cos_skill_probe.py --list
  python tests/cos_skill_probe.py --object-key /uid/picturePicklockEvent/x.jpg --save out.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

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

from cos_probe import cos_authorization, redact  # noqa: E402

LOGGER = logging.getLogger("cos_skill_probe")
CMD_COS_AUTH = 313


class CosSsl(Ssl):
    """在现有 Ssl 基础上增加 cmd=313 授权请求与单次响应等待。"""

    async def request_cos_auth(
        self,
        user_id: str,
        device_id: str,
        device_uid: str,
        timeout: float = 20.0,
    ) -> Optional[Dict[str, Any]]:
        inner = {
            "namespace": "Skill.GetCOSAuthorization",
            "requestId": generate_uuid(),
            "version": 1,
            "userId": user_id,
            "deviceId": device_id,
            "familyId": self.family_id,
            "uid": device_uid,
            "type": "lock",
        }
        payload = {
            "request": json.dumps(inner, separators=(",", ":")),
            "cmd": CMD_COS_AUTH,
            "serial": generate_serial(),
            "clientType": 1,
            "uniSerial": generate_serial(use_time=True),
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        LOGGER.info("cmd=313 已发送: deviceId=%s uid=%s", device_id, device_uid)
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
            if cmd == CMD_COS_AUTH:
                LOGGER.info("cmd=313 响应收到")
                return d
            LOGGER.debug("忽略中间包 cmd=%s", cmd)
        LOGGER.error("等待 cmd=313 响应超时")
        return None


def build_cos_token(security: Dict[str, Any]) -> Dict[str, Any]:
    """把 QueryTxAuthResponse.Security 归一化为 cos_probe 的 token 结构。"""
    if not security:
        raise RuntimeError("security 为空")
    return {
        "secretId": security.get("accessKeyId") or "",
        "secretKey": security.get("accessKeySecret") or "",
        "sessionToken": security.get("securityToken") or "",
        "bucket": security.get("bucketName") or "",
        "region": security.get("region") or "ap-guangzhou",
        "expiration": security.get("expiration") or 1800,
        "systemCurrentTime": security.get("systemCurrentTime"),
        "endpoint": security.get("endpoint") or "",
    }


def build_download(
    token: Dict[str, Any],
    object_key: str,
    end_delta: Optional[int] = None,
) -> tuple[str, Dict[str, str]]:
    """构造 COS GET 请求：虚拟主机风格 {bucket}.cos.{region}.myqcloud.com。

    服务端返回的 endpoint 只是区域域名（如 cos.ap-guangzhou.myqcloud.com），
    直接用它访问会被 COS 拒绝（Missing required header Appid）。桶名
    p20-doorlock-1251222210 已是 bucketname-appid 完整格式，虚拟主机名即桶名。
    """
    region = token["region"]
    host = f"{token['bucket']}.cos.{region}.myqcloud.com"
    path = object_key if object_key.startswith("/") else "/" + object_key

    sys_time = token.get("systemCurrentTime")
    start = int(sys_time / 1000) if sys_time else int(time.time())
    duration = int(token.get("expiration") or 1800)
    if end_delta is not None:
        duration = min(duration, end_delta)
    # 本地时间与系统时间偏差较大时，以两者较大值为起点，避免签名落在窗口外
    start = max(start, int(time.time()))
    auth = cos_authorization(
        token["secretId"], token["secretKey"], "get", path, host, start, start + duration
    )
    url = f"https://{host}{path}"
    headers = {"Authorization": auth, "x-cos-security-token": token["sessionToken"]}
    return url, headers


def build_signed_url(token: Dict[str, Any], object_key: str, ttl: int = 600) -> str:
    """生成浏览器可直接访问的预签名 URL（含 x-cos-security-token query）。

    与集成 cos_media.signed_media_url 同算法：STS 临时凭证必须放进 URL，
    浏览器无法携带自定义 header。
    """
    host = f"{token['bucket']}.cos.{token['region']}.myqcloud.com"
    path = object_key if object_key.startswith("/") else "/" + object_key
    sys_time = token.get("systemCurrentTime")
    now = max(int(time.time()), int(sys_time / 1000) if sys_time else 0)
    auth = cos_authorization(
        token["secretId"], token["secretKey"], "get", path, host, now, now + ttl
    )
    token_part = f"&x-cos-security-token={quote(token['sessionToken'], safe='')}"
    return f"https://{host}{path}?{auth}{token_part}"


def download_url(url: str, timeout: int = 30) -> bytes:
    """模拟浏览器直接访问（不带任何自定义 header）。"""
    print(f"      尝试[浏览器直连] {url[:120]}...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download(token: Dict[str, Any], object_key: str, save: str) -> None:
    print(f"[5/5] 下载 {object_key}")
    variants = [
        ("原始", build_download(token, object_key)),
        ("缩短签名窗口", build_download(token, object_key, end_delta=600)),
    ]
    body: bytes = b""
    for label, (url, headers) in variants:
        print(f"      尝试[{label}] {url}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
            print(f"      [{label}] 成功: {len(body)} 字节")
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            print(f"      [{label}] HTTP {e.code}: {err_body[:400]}")
            if e.code != 403:
                raise
    else:
        raise SystemExit("所有变体均被拒绝，见上方错误详情")
    with open(save, "wb") as f:
        f.write(body)
    print(
        f"      保存: {len(body)} 字节 -> {save} "
        f"(magic={body[:8].hex() if body else 'empty'})"
    )


def pick_lock_device(devs: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    locks = [d for d in devs if _is_lock_device(d)]
    if not locks:
        return None
    return locks[0]


async def main() -> int:
    parser = argparse.ArgumentParser(description="门锁媒体 Skill 授权 + COS 下载探针")
    parser.add_argument("--object-key", help="要下载的 COS 对象键（如 /uid/picturePicklockEvent/x.jpg）")
    parser.add_argument("--save", default="cos_download.bin", help="下载保存路径")
    parser.add_argument("--family-id", help="家庭 ID（默认自动取第一个家庭）")
    parser.add_argument("--device-id", help="门锁 deviceId（w- 前缀；默认自动识别）")
    parser.add_argument("--device-uid", help="门锁 uid（无 w- 前缀；默认自动识别）")
    parser.add_argument("--list", action="store_true", help="仅列出门锁设备（不下载）")
    parser.add_argument(
        "--url", action="store_true", help="生成浏览器可直连的预签名 URL 并自测（不保存文件）"
    )
    args = parser.parse_args()

    username = os.environ.get("ORVIBO_USERNAME") or ""
    password = os.environ.get("ORVIBO_PASSWORD") or ""
    if not username or not password:
        print("❌ 请设置环境变量 ORVIBO_USERNAME / ORVIBO_PASSWORD")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    api = Https(username, password)
    if not await api.login():
        print("❌ 登录失败")
        return 1
    print(f"[1/5] 登录成功 user_id={redact(api.uid, 6)}")

    fs = await api.families()
    if not fs:
        print("❌ 账号下没有家庭")
        return 1
    if args.family_id:
        fam = next((f for f in fs if f.get("familyId") == args.family_id), None)
        if not fam:
            print(f"❌ 未找到家庭 {args.family_id}")
            return 1
    else:
        fam = fs[0]
    fid = fam.get("familyId", "")
    fname = fam.get("familyName", "?")
    print(f"[2/5] 家庭: {fname} ({redact(fid, 6)})")

    devs = await api.devices(fid)
    if args.device_id and args.device_uid:
        dev = {"deviceId": args.device_id, "uid": args.device_uid}
        print(f"[3/5] 使用指定门锁: deviceId={dev['deviceId']} uid={dev['uid']}")
    else:
        dev = pick_lock_device(devs)
        if not dev:
            print("❌ 未识别到门锁设备，请用 --device-id / --device-uid 指定")
            return 1
        print(f"[3/5] 门锁: {dev.get('deviceName', '?')} deviceId={dev.get('deviceId')} uid={dev.get('uid')}")
    if args.list:
        print(json.dumps(dev, ensure_ascii=False, default=str))
        return 0
    if not args.object_key:
        print("❌ 请用 --object-key 指定要下载的对象键（或用 --list 仅列设备）")
        return 2

    sslc = CosSsl(api.username, api.password, fid)
    if not await sslc.connect():
        print("❌ SSL 连接/登录失败")
        await api.close()
        return 1

    resp = await sslc.request_cos_auth(
        api.uid, dev.get("deviceId", ""), dev.get("uid", "")
    )
    await sslc.close()
    await api.close()
    if not resp:
        print("❌ 未收到 cmd=313 响应")
        return 1

    print(f"[4/5] cmd=313 响应: {json.dumps(resp, ensure_ascii=False)[:500]}")
    response_json = resp.get("response")
    if isinstance(response_json, str):
        try:
            q = json.loads(response_json)
        except json.JSONDecodeError:
            q = None
    elif isinstance(response_json, dict):
        q = response_json
    else:
        q = None
    if not q:
        print(f"❌ 响应缺少 response 字段: status={resp.get('status')} errorCode={resp.get('errorCode')}")
        return 1
    if q.get("namespace") != "Skill.GetCOSAuthorization":
        print(f"❌ namespace 不符: {q.get('namespace')}")
        return 1
    if q.get("status") not in (None, 0, "0"):
        print(f"❌ 授权失败 status={q.get('status')} errorCode={q.get('errorCode')}")
        return 1

    security = q.get("security") or {}
    token = build_cos_token(security)
    print(
        "[4/5] COS 凭证 "
        f"bucket={token['bucket']} region={token['region']} "
        f"endpoint={token['endpoint'] or '-'} "
        f"secretId={redact(token['secretId'])} expiration={token['expiration']}"
    )
    if not token["secretId"] or not token["bucket"]:
        print("❌ security 字段不完整")
        return 1

    if args.url:
        signed = build_signed_url(token, args.object_key)
        print(f"[5/5] 预签名 URL（浏览器可直接打开）:\n{signed}")
        body = download_url(signed)
        print(f"      直连成功: {len(body)} 字节 (magic={body[:8].hex()})")
        return 0

    download(token, args.object_key, args.save)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
