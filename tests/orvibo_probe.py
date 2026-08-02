"""
ORVIBO 设备协议诊断工具

用法：
  python3 tests/orvibo_probe.py 用户名 密码 [mode]

模式:
  list      - 列出所有设备（默认）
  listen    - 监听模式（捕获App操作时的云端推送）
  control   - 原始控制模式（手动发送命令）
  lock      - 门锁事件抓包（自动识别门锁设备，动作标记 + 脱敏 JSONL）
              用法: python3 tests/orvibo_probe.py 用户名 密码 lock [家庭索引] [时长秒]
"""

import asyncio
import datetime
import hashlib
import json
import logging
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
_LOGGER = logging.getLogger("orvibo_probe")

_LOCK_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "access_token",
        "authorization",
        "cookie",
        "dynamickey",
        "key",
        "passwd",
        "password",
        "passwordmd5",
        "phone",
        "phonenumber",
        "secret",
        "sessionid",
        "sessionkey",
        "sign",
        "token",
    }
)


def _normalize_key(key) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _scrub(value):
    """本地抓包脱敏：凭据类字段打码，保留设备/家庭标识便于后续分析。"""
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            key = str(k)
            result[k] = "<redacted>" if _normalize_key(key) in _LOCK_SECRET_KEYS else _scrub(v)
        return result
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _is_lock_device(dev: dict) -> bool:
    """识别门锁设备：分类、deviceType（522/107）、classId（462/463）或名称兜底。"""
    if classify_device(dev) == DeviceCategory.DOOR_LOCK:
        return True
    dt = dev.get("deviceType")
    if isinstance(dt, str):
        try:
            dt = int(dt)
        except (TypeError, ValueError):
            dt = None
    if dt in (522, 107):
        return True
    cid = dev.get("classId") or dev.get("class_id")
    if isinstance(cid, str):
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            cid = None
    if cid in (462, 463):
        return True
    name = str(dev.get("deviceName", dev.get("name", "")))
    return "锁" in name or "lock" in name.lower()

# 加载项目模块
PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "custom_components"))
_tmp_init = Path(sys.path[0]) / "orvibohomebridge" / "__init__.py"
_orig_init = _tmp_init.read_bytes()
if b"homeassistant" in _orig_init:
    _tmp_init.write_bytes(b"# patched")

from orvibohomebridge.functions import generate_serial, generate_uuid
from orvibohomebridge.const import (
    SSL_HOST, SSL_PORT, HTTPS_HOST,
    SOFTWARE_NAME, SOFTWARE_VERSION, SYS_VERSION,
    HARDWARE_VERSION, LANGUAGE, PHONE_NAME, DEBUG_INFO,
    SOFTWARE_VER, DEFAULT_KEY, ID_UNSET, MAGIC,
    HTTP_HEADERS,
    CMD_HELLO, CMD_LOGIN, CMD_CONTROL, CMD_HEARTBEAT, CMD_HANDSHAKE,
    CMD_STATE_UPDATE, CMD_CLOTHES_HORSE_CONTROL, CMD_CLOTHES_HORSE_STATE, CMD_CLOTHES_HORSE_QUERY,
)
from orvibohomebridge.packet import HomematePacket, HomemateJsonData
from orvibohomebridge.device_types import DeviceCategory, classify_device, is_hidden_category

_tmp_init.write_bytes(_orig_init)


# ── HTTPS ──

class Https:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.pw_md5 = hashlib.md5(password.encode()).hexdigest().upper()
        self.token: Optional[str] = None
        self.uid: Optional[str] = None
        self.fid: Optional[str] = None
        self.fname: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _s(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def login(self) -> bool:
        s = await self._s()
        url = f"https://{HTTPS_HOST}/getOauthToken?userName={self.username}&type=0&password={self.pw_md5}"
        _LOGGER.info(f"登录 {url.replace(self.pw_md5,'***')}")
        async with s.get(url, headers={**HTTP_HEADERS, "Accept": "*/*"}, ssl=False) as r:
            text = await r.text()
            j = json.loads(text)
            if j.get("status") == 0:
                self.token = j["data"]["access_token"]
                self.uid = j["data"]["user_id"]
                return True
        return False

    async def families(self) -> list:
        ret = HomemateJsonData.get_family_statistics_users(self.uid, self.token)
        s = await self._s()
        async with s.post(ret["url"], data=ret["data"], headers=HTTP_HEADERS, ssl=False) as r:
            j = await r.json()
            fs = j.get("data", [])
            if isinstance(fs, list):
                for f in fs:
                    if self.fid is None:
                        self.fid = f.get("familyId", "")
                        self.fname = f.get("familyName", "")
                return fs
            return []

    async def devices(self, family_id: str) -> list:
        ret = HomemateJsonData.get_devices_status(
            self.token, "", self.uid, self.username, family_id, device_flag=1,
        )
        s = await self._s()
        async with s.post(ret["url"], data=ret["data"], headers=HTTP_HEADERS, ssl=False) as r:
            j = await r.json()
            data = j.get("data", {})
            devs = data.get("device", []) or []
            sts = data.get("deviceStatus", []) or []
            sm = {x.get("deviceId", ""): x for x in sts if x.get("deviceId")}
            for d in devs:
                if d["deviceId"] in sm:
                    d.update(sm[d["deviceId"]])
            if devs:
                return devs
        ret0 = HomemateJsonData.get_devices_status(
            self.token, "", self.uid, self.username, family_id, device_flag=0,
        )
        async with s.post(ret0["url"], data=ret0["data"], headers=HTTP_HEADERS, ssl=False) as r:
            j = await r.json()
            d2 = j.get("data", {})
            dl = d2.get("device", []) or []
            sl = d2.get("deviceStatus", []) or []
            sm = {x.get("deviceId", ""): x for x in sl if x.get("deviceId")}
            for d in dl:
                if d["deviceId"] in sm:
                    d.update(sm[d["deviceId"]])
            return dl


# ── SSL ──

class Ssl:
    def __init__(self, username: str, password: str, family_id: str):
        self.username = username
        self.pw_md5 = hashlib.md5(password.encode()).hexdigest().upper()
        self.family_id = family_id
        self.r: Optional[asyncio.StreamReader] = None
        self.w: Optional[asyncio.StreamWriter] = None
        self.sid: Optional[str] = None
        self.key: Optional[bytes] = None

    async def connect(self) -> bool:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # 加载项目证书
            cert_dir = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge" / "certs"
            cert_file = cert_dir / "client_cert.pem"
            key_file = cert_dir / "client_key.pem"
            ca_file = cert_dir / "server_ca.pem"
            if cert_file.exists() and key_file.exists():
                ctx.load_cert_chain(str(cert_file), str(key_file))
            if ca_file.exists():
                ctx.load_verify_locations(str(ca_file))
            else:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self.r, self.w = await asyncio.wait_for(
                asyncio.open_connection(SSL_HOST, SSL_PORT, ssl=ctx, server_hostname=SSL_HOST), timeout=15
            )
            _LOGGER.info("✅ SSL 连接成功")
            # 发送 HELLO（必须用完整的 ssl_get_session）
            await self._send(HomemateJsonData.ssl_get_session(), DEFAULT_KEY.encode())
            _LOGGER.info("HELLO 已发送，等待响应...")
            # 等待 HELLO 响应获取 session_key
            try:
                hdr = await asyncio.wait_for(self.r.readexactly(42), timeout=10)
                ln = HomematePacket.parse_length(hdr)
                body = await asyncio.wait_for(self.r.readexactly(ln - 42), timeout=10)
                pkt = HomematePacket(hdr + body, {"": DEFAULT_KEY.encode()})
                d = pkt.json_payload
                if d and d.get("cmd") == CMD_HELLO:
                    kv = d.get("key")
                    if kv:
                        self.key = str(kv).encode()
                        self.sid = bytes(pkt.session_id).decode()
                        _LOGGER.info(f"✅ HELLO 响应: session_id={self.sid}")
            except Exception as e:
                _LOGGER.error(f"❌ HELLO 响应异常: {e}")
                return False

            if self.key:
                await self._send(HomemateJsonData.ssl_login(self.username, self.pw_md5, self.family_id), self.key)
                _LOGGER.info("LOGIN 已发送")
                await asyncio.sleep(2)

                # 发送一次 device list 查询，订阅设备推送
                from orvibohomebridge.const import CMD_GET_DEVICE_LIST
                query = {
                    "cmd": CMD_GET_DEVICE_LIST, "familyId": self.family_id,
                    "serial": generate_serial(), "clientType": 1,
                    "uniSerial": generate_serial(use_time=True),
                    "ver": SOFTWARE_VER, "debugInfo": DEBUG_INFO,
                }
                await self._send(query, self.key)
                _LOGGER.info("📋 设备列表查询已发送")
                await asyncio.sleep(1)
                return True
            return False
        except Exception as e:
            _LOGGER.error(f"❌ SSL: {e}")
            return False

    async def _send(self, data: dict, key: bytes):
        if key == DEFAULT_KEY.encode():
            ptype = bytes([0x70, 0x6b])
            sid = bytes(ID_UNSET)
        else:
            ptype = bytes([0x64, 0x6b])
            sid = (self.sid or bytes(ID_UNSET).decode()).encode()
        self.w.write(HomematePacket.build_packet(ptype, key, sid, data))
        await self.w.drain()

    async def send_control(self, did: str, uid: str, order: str, v1=0, v2=0, v3=0, v4=0, props=None):
        p = {
            "uid": uid, "userName": self.username, "deviceId": did, "groupId": "",
            "order": order, "value1": v1, "value2": v2, "value3": v3, "value4": v4,
            "delayTime": 0, "qualityOfService": 1, "defaultResponse": 1, "propertyResponse": 0,
            "cmd": CMD_CONTROL, "serial": generate_serial(), "clientType": 1,
            "uniSerial": generate_serial(use_time=True), "serverRecord": False,
            "ver": SOFTWARE_VER, "debugInfo": DEBUG_INFO,
        }
        if props: p["properties"] = props
        await self._send(p, self.key)
        _LOGGER.info(f"控制已发送: {did} order={order} v1={v1}")

    async def listen(self, duration=180) -> list:
        _LOGGER.info(f"🎧 监听 {duration}s，请在 App 操作设备...")
        start = time.time()
        msgs = []
        while time.time() - start < duration:
            try:
                hdr = await asyncio.wait_for(self.r.readexactly(42), timeout=5)
                ln = HomematePacket.parse_length(hdr)
                body = await asyncio.wait_for(self.r.readexactly(ln - 42), timeout=10)
                k = self.key or DEFAULT_KEY.encode()
                try:
                    pkt = HomematePacket(hdr + body, {(self.sid or ""): k})
                except Exception:
                    continue
                d = pkt.json_payload
                if d is None: continue
                cmd = d.get("cmd")
                if cmd == CMD_HELLO:
                    kv = d.get("key")
                    if kv: self.key, self.sid = str(kv).encode(), bytes(pkt.session_id).decode()
                    continue
                if cmd in (CMD_LOGIN, CMD_HEARTBEAT, CMD_HANDSHAKE): continue

                ts = f"{time.time()-start:.1f}s"
                act = d.get("action", "")
                did = d.get("deviceId", d.get("data", {}).get("deviceId", "?"))
                tag = "📡" if act == "deviceStatusReport" else "🔄" if cmd == CMD_STATE_UPDATE else "❓"
                _LOGGER.info(f"[{ts}] {tag} {did} cmd={cmd} act={act}")
                _LOGGER.info(f"  {json.dumps(d, ensure_ascii=False)}")
                msgs.append(d)
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
        _LOGGER.info(f"停止监听，捕获 {len(msgs)} 条")
        return msgs

    async def close(self):
        if self.w and not self.w.is_closing():
            self.w.close()
            try: await asyncio.wait_for(self.w.wait_closed(), timeout=2)
            except: pass


# ── Commands ──

async def cmd_list(api: Https):
    fs = await api.families()
    print(f"\n📋 {len(fs)} 个家庭")
    for f in fs:
        fid, fn = f.get("familyId","?"), f.get("familyName","?")
        print(f"\n🏠 {fn} (ID: {fid})")
        devs = await api.devices(fid)
        print(f"  {'✅/❓':<5} {'deviceId':<24} {'名称':<16} {'type':<6} {'分类':<20}")
        print(f"  {'-'*71}")
        for d in devs:
            did = d.get("deviceId","")
            dn = d.get("deviceName",d.get("name","?"))
            dt = d.get("deviceType","")
            cat = classify_device(d)
            mk = "❓" if cat == DeviceCategory.UNKNOWN else "✅"
            print(f"  {mk:<5} {did:<24} {dn:<16} {dt:<6} {cat.value:<20}")
            if cat == DeviceCategory.UNKNOWN:
                info = {k:d.get(k) for k in ("deviceType","deviceName","uid","model","properties","value1","value2","value3","value4","online","subDeviceType","classId") if d.get(k)}
                print(f"       {json.dumps(info, ensure_ascii=False)}")

async def cmd_listen(api: Https):
    fs = await api.families()
    if not fs: return
    fid = fs[0].get("familyId","")
    sslc = Ssl(api.username, api.password, fid)
    if await sslc.connect():
        await sslc.listen(180)
        await sslc.close()


async def cmd_lock(api: Https, family_index: int = 0, duration: int = 300):
    """门锁事件抓包：连接 SSL 订阅推送，动作前按 Enter 打标记，输出脱敏 JSONL。"""
    fs = await api.families()
    if not fs:
        print("❌ 账号下没有家庭")
        return
    if family_index >= len(fs):
        print(f"❌ 家庭索引 {family_index} 越界（共 {len(fs)} 个）")
        return
    fid = fs[family_index].get("familyId", "")
    fname = fs[family_index].get("familyName", "?")
    devs = await api.devices(fid)
    locks = [d for d in devs if _is_lock_device(d)]
    print(f"\n🔒 家庭[{family_index}] {fname} 门锁设备:")
    if not locks:
        print("  （未识别到门锁设备）")
        print("  提示：若门锁在其他家庭，加参数指定家庭索引；仍不行就把 list 模式输出发我。")
        return
    for i, d in enumerate(locks):
        info = {
            k: d.get(k)
            for k in ("deviceId", "deviceName", "deviceType", "subDeviceType", "classId", "model", "uiModel")
            if d.get(k)
        }
        print(f"  [{i}] {json.dumps(info, ensure_ascii=False)}")

    out_path = Path(f"lock_capture_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    sslc = Ssl(api.username, api.password, fid)
    if not await sslc.connect():
        print("❌ SSL 连接失败")
        return

    print(f"\n🎧 监听 {duration}s（输入 q 可提前结束）。")
    print(f"   每个动作前按一次 Enter 记录标记；输出文件: {out_path}")
    start = time.time()
    write_lock = asyncio.Lock()

    async def write_rec(rec: dict):
        async with write_lock:
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    await write_rec(
        {
            "type": "session",
            "family": fname,
            "family_id": fid,
            "lock_device_ids": sorted(d.get("deviceId", "") for d in locks),
            "duration": duration,
        }
    )

    stop = asyncio.Event()

    async def listener():
        while time.time() - start < duration and not stop.is_set():
            try:
                hdr = await asyncio.wait_for(sslc.r.readexactly(42), timeout=5)
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
            try:
                ln = HomematePacket.parse_length(hdr)
                body = await asyncio.wait_for(sslc.r.readexactly(ln - 42), timeout=10)
                k = sslc.key or DEFAULT_KEY.encode()
                pkt = HomematePacket(hdr + body, {(sslc.sid or ""): k})
                d = pkt.json_payload
                if d is None:
                    continue
                cmd = d.get("cmd")
                if cmd == CMD_HELLO:
                    kv = d.get("key")
                    if kv:
                        sslc.key, sslc.sid = str(kv).encode(), bytes(pkt.session_id).decode()
                    continue
                if cmd in (CMD_LOGIN, CMD_HEARTBEAT, CMD_HANDSHAKE):
                    continue
                rec = {"elapsed": round(time.time() - start, 2), "type": "packet", "packet": _scrub(d)}
                await write_rec(rec)
                data_field = d.get("data")
                data_did = data_field.get("deviceId", "?") if isinstance(data_field, dict) else "?"
                did = d.get("deviceId") or data_did
                act = d.get("action", "")
                tag = "📡" if act == "deviceStatusReport" else "🔄" if cmd == CMD_STATE_UPDATE else "❓"
                print(f"[{rec['elapsed']:>7.1f}s] {tag} {did} cmd={cmd} act={act}")
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                continue
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
            except Exception as e:
                print(f"  ⚠️ 单个包处理异常（已跳过，不会中断监听）: {e}")
        stop.set()

    task = asyncio.create_task(listener())
    try:
        while time.time() - start < duration and not stop.is_set():
            line = await asyncio.to_thread(input, "  按 Enter 标记下一个动作（q=结束）: ")
            if line.strip().lower() in ("q", "quit"):
                break
            await write_rec(
                {
                    "elapsed": round(time.time() - start, 2),
                    "type": "marker",
                    "action": line.strip() or "next",
                }
            )
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await sslc.close()
    print(f"\n✅ 抓包完成，已保存到 {out_path}")

async def cmd_control(api: Https):
    fs = await api.families()
    if not fs: return
    fid = fs[0].get("familyId","")
    sslc = Ssl(api.username, api.password, fid)
    if not await sslc.connect(): return
    print("\n🔧 原始控制: deviceId uid order [v1 v2 v3 v4]")
    print("  order: on/off/open/close/stop/set property\n")
    while True:
        try:
            line = input(">>> ").strip()
            if line.lower() in ("q","quit","exit"): break
            parts = line.split()
            if len(parts) < 3: continue
            await sslc.send_control(parts[0], parts[1], parts[2],
                int(parts[3]) if len(parts)>3 else 0,
                int(parts[4]) if len(parts)>4 else 0,
                int(parts[5]) if len(parts)>5 else 0,
                int(parts[6]) if len(parts)>6 else 0)
            await asyncio.sleep(2)
        except KeyboardInterrupt: break
    await sslc.close()

async def main():
    if len(sys.argv) < 3:
        print(__doc__); return
    api = Https(sys.argv[1], sys.argv[2])
    try:
        if not await api.login():
            print("❌ 登录失败"); return
        mode = sys.argv[3] if len(sys.argv)>3 else "list"
        if mode == "lock":
            family_index = int(sys.argv[4]) if len(sys.argv) > 4 else 0
            duration = int(sys.argv[5]) if len(sys.argv) > 5 else 300
            await cmd_lock(api, family_index, duration)
        else:
            cmd_fn = {"list":cmd_list,"listen":cmd_listen,"control":cmd_control}.get(mode)
            if cmd_fn:
                await cmd_fn(api)
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(main())
