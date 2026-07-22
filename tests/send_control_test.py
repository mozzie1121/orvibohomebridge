"""通过 SSL 发送控制命令到 COCO 智能插线板 (type=43)"""
import asyncio, hashlib, json, ssl, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "custom_components"))

_init = Path(sys.path[0]) / "orvibohomebridge" / "__init__.py"
_orig = _init.read_text()
if "homeassistant" in _orig: _init.write_text("#")

from orvibohomebridge.const import SSL_HOST, SSL_PORT, CMD_HELLO, CMD_LOGIN, CMD_CONTROL, CMD_HEARTBEAT, CMD_HANDSHAKE
from orvibohomebridge.const import SOFTWARE_NAME, SOFTWARE_VERSION, SYS_VERSION, HARDWARE_VERSION, LANGUAGE, PHONE_NAME, DEBUG_INFO
from orvibohomebridge.const import SOFTWARE_VER, DEFAULT_KEY, ID_UNSET, MAGIC, HTTPS_HOST, HTTP_HEADERS
from orvibohomebridge.packet import HomematePacket, HomemateJsonData
from orvibohomebridge.functions import generate_serial, generate_uuid

if _orig: _init.write_text(_orig)

USERNAME = "65261217@qq.com"
PASSWORD = "Sunjian21"
FAMILY_ID = "00000000000018111433753460517481"  # 我的家庭
DEVICE_ID = "834a9801ba2d4b729126648329c3473b"
DEVICE_UID = "accf23852d1c"

pw_md5 = hashlib.md5(PASSWORD.encode()).hexdigest().upper()

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cert_dir = PROJECT_ROOT / "custom_components" / "orvibohomebridge" / "certs"
    ctx.load_cert_chain(str(cert_dir / "client_cert.pem"), str(cert_dir / "client_key.pem"))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    r, w = await asyncio.wait_for(
        asyncio.open_connection(SSL_HOST, SSL_PORT, ssl=ctx, server_hostname=SSL_HOST), timeout=15
    )
    print("✅ SSL 连接成功")

    async def send(data: dict, key: bytes):
        if key == DEFAULT_KEY.encode():
            ptype = bytes([0x70, 0x6b])
            sid = bytes(ID_UNSET)
        else:
            ptype = bytes([0x64, 0x6b])
            sid = session_id.encode() if session_id else bytes(ID_UNSET)
        w.write(HomematePacket.build_packet(ptype, key, sid, data))
        await w.drain()

    async def recv(key) -> dict:
        hdr = await asyncio.wait_for(r.readexactly(42), timeout=10)
        ln = HomematePacket.parse_length(hdr)
        body = await asyncio.wait_for(r.readexactly(ln - 42), timeout=10)
        pkt = HomematePacket(hdr + body, {session_id: key})
        return pkt.json_payload

    # HELLO
    session_id = ""
    await send(HomemateJsonData.ssl_get_session(), DEFAULT_KEY.encode())
    hello_resp = await recv(DEFAULT_KEY.encode())
    if hello_resp and hello_resp.get("cmd") == CMD_HELLO:
        session_key = str(hello_resp["key"]).encode()
        session_id = hello_resp.get("sessionId", str(hello_resp))
        print(f"✅ HELLO: session_id 获取成功")

    # LOGIN
    login_payload = HomemateJsonData.ssl_login(USERNAME, pw_md5, FAMILY_ID)
    await send(login_payload, session_key)
    await asyncio.sleep(2)
    print("✅ LOGIN 已发送")

    # 发送控制命令 - 先 off (关)
    serial = generate_serial()
    uni_serial = generate_serial(use_time=True)
    # 测试多种控制格式 - 重点试 endpoint 相关参数
    test_commands = [
        # order=on/off 各种 value 组合
        ("on", 0, 0, 0, 0, None),
        ("on", 1, 0, 0, 0, None),
        ("off", 1, 0, 0, 0, None),
        ("off", 0, 0, 0, 0, None),
        # set property 格式
        ("set property", 0, 0, 0, 0, {"onoff": {"status": "on"}}),
        ("set property", 0, 0, 0, 0, {"onoff": {"status": "off"}}),
        # 尝试 endpoint=1 (gdid=1)
        ("on", 0, 0, 0, 0, {"endpoint": 1}),
        ("off", 0, 0, 0, 0, {"endpoint": 1}),
        # 尝试 endpoint=0
        ("on", 0, 0, 0, 0, {"endpoint": 0}),
        ("off", 0, 0, 0, 0, {"endpoint": 0}),
        # 尝试 order=open/close (老款设备)
        ("open", 100, 0, 0, 0, None),
        ("close", 0, 0, 0, 0, None),
        # 尝试 value2=1 或 value3=1 (老款可能不一样)
        ("on", 1, 1, 0, 0, None),
        ("off", 0, 1, 0, 0, None),
    ]
    
    for order, v1, v2, v3, v4, props in test_commands:
        serial = generate_serial()
        uni_serial = generate_serial(use_time=True)
        control_payload = {
            "uid": DEVICE_UID, "userName": USERNAME,
            "deviceId": DEVICE_ID, "groupId": "",
            "order": order, "value1": v1, "value2": v2, "value3": v3, "value4": v4,
            "delayTime": 0, "qualityOfService": 1, "defaultResponse": 1, "propertyResponse": 0,
            "cmd": CMD_CONTROL, "serial": serial, "clientType": 1,
            "uniSerial": uni_serial, "serverRecord": False,
            "ver": SOFTWARE_VER, "debugInfo": DEBUG_INFO,
        }
        if props:
            control_payload["properties"] = props
        print(f"📤 发送: order={order}, v1={v1}, props={props}")
        await send(control_payload, session_key)
        await asyncio.sleep(2)
        try:
            resp = await recv(session_key)
            print(f"📥 响应: {json.dumps(resp, ensure_ascii=False)}")
        except Exception as e:
            print(f"📥 无响应: {e}")

    w.close()
    await asyncio.sleep(0.5)

asyncio.run(main())
