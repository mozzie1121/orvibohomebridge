#!/usr/bin/env python3
"""
ORVIBO 交互式远程控制测试工具
功能：HTTPS 登录 → 选家庭 → 列出设备 → SSL 控制开/关

使用 orvibohomebridge 的 payload 格式和 mTLS 证书。
"""

import ssl
import socket
import json
import struct
import hashlib
import uuid
import time
import zlib
import sys
import select
import urllib.request
import urllib.error
import hmac
import argparse
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend

# ═══════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════
CERT_DIR = "/root/orvibohomebridge/custom_components/orvibohomebridge/certs"
CLIENT_CERT = f"{CERT_DIR}/client_cert.pem"
CLIENT_KEY = f"{CERT_DIR}/client_key.pem"
SERVER_CA = f"{CERT_DIR}/server_ca.pem"

HTTPS_HOST = "china.orvibo.com"
SSL_HOST = "china.orvibo.com"
SSL_PORT = 10002

SOFTWARE_NAME = "ZhiJia365"
SOFTWARE_VERSION = "50103309"
SOFTWARE_VER = "5.1.3.309"
SYS_VERSION = "Android14_34"
HARDWARE_VERSION = "Google Pixel 8"
LANGUAGE = "zh"
PHONE_NAME = "Pixel 8"
DEBUG_INFO = "Android_ZhiJia365_34_5.1.3.309"
SIGN_KEY = "nQ45RjPtOws96jmH"
DEFAULT_KEY = "khggd54865SNJHGF"
CMD_HELLO = 0
CMD_LOGIN = 2
CMD_CONTROL = 15

HTTP_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "okhttp/3.12.8",
}

# ═══════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

# ═══════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════
def generate_serial():
    return int(str(uuid.uuid4().int)[:9])

def generate_timestamp():
    return int(time.time() * 1000)

def generate_uuid():
    return str(uuid.uuid4()).replace("-", "")

def md5_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest().upper()

def hmac_sha256(key, message):
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest().upper()

def create_sign(params, key=SIGN_KEY):
    sorted_keys = sorted(params.keys())
    sb = []
    for k in sorted_keys:
        v = params[k]
        if v is not None and str(v).strip() != "":
            sb.append(f"{k}={v}&")
    sb.append(f"key={key}")
    sign_str = "".join(sb)
    return hmac_sha256(key, sign_str)

def aes_encrypt(key, plaintext):
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()

def aes_decrypt(key_hex, ciphertext):
    key = key_hex.encode("utf-8")[:16].ljust(16, b"\x00") if isinstance(key_hex, str) else key_hex[:16]
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    dec = cipher.decryptor()
    data = dec.update(ciphertext) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    unpad = unpadder.update(data) + unpadder.finalize()
    if unpad and unpad[-1] == 0x00:
        unpad = unpad[:-1]
    return unpad

def build_packet(payload, key, session_id, use_dynamic=False):
    pt = b"\x64\x6b" if use_dynamic else b"\x70\x6b"
    magic = b"\x68\x64"
    payload_str = json.dumps(payload, separators=(",", ":"))
    encrypted = aes_encrypt(key.encode("utf-8") if isinstance(key, str) else key, payload_str.encode("utf-8"))
    crc = struct.pack(">I", zlib.crc32(encrypted) & 0xFFFFFFFF)
    length = struct.pack(">H", len(encrypted) + 42)
    return magic + length + pt + crc + session_id + encrypted

def decode_packet(data, keys):
    if len(data) < 42 or data[:2] != b"\x68\x64":
        return None
    encrypted = data[42:]
    if not encrypted or len(encrypted) % 16:
        return None
    for k in keys:
        key = k.encode("utf-8")[:16] if isinstance(k, str) else k[:16]
        try:
            decrypted = aes_decrypt(k, encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            continue
    return None

# ═══════════════════════════════════════════
# HTTPS 客户端（同步版，不依赖 aiohttp）
# ═══════════════════════════════════════════
def https_request(url, data=None):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    if data is not None:
        log(f"HTTP POST {url.split('?')[0]}")
        if isinstance(data, str):
            data = data.encode("utf-8")
        req.data = data
    else:
        log(f"HTTP GET {url.split('?')[0]}")
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    resp = urllib.request.urlopen(req, timeout=15, context=ctx)
    body = resp.read().decode("utf-8")
    return json.loads(body)

def get_access_token(username, password):
    url = f"https://{HTTPS_HOST}/getOauthToken?userName={username}&type=0&password={password}"
    log("正在获取 access_token ...")
    resp = https_request(url)
    log(f"access_token 响应: code={resp.get('code')}, has_data={'data' in resp}")
    if "data" not in resp:
        raise Exception(f"获取 access_token 失败: {resp.get('message', resp)}")
    data = resp["data"]
    return data.get("access_token"), data.get("user_id")

def get_family_list(user_id, access_token):
    url = f"https://{HTTPS_HOST}/v2/family/statistics/users"
    timestamp = generate_timestamp()
    random_str = generate_uuid()
    req_data = {
        "accessToken": access_token,
        "random": random_str,
        "userId": user_id,
        "sign": "1234567890",
        "timestamp": timestamp,
        "requestId": generate_uuid()
    }
    params = {
        "requestId": req_data["requestId"],
        "userId": req_data["userId"],
        "accessToken": req_data["accessToken"],
        "random": req_data["random"],
        "timestamp": req_data["timestamp"]
    }
    req_data["sign"] = create_sign(params)
    
    log("获取家庭列表 ...")
    resp = https_request(url, json.dumps(req_data))
    log(f"家庭列表响应: code={resp.get('code')}")
    
    if "data" not in resp:
        raise Exception(f"获取家庭列表失败: {resp.get('message', resp)}")
    return resp["data"]

def get_devices(access_token, user_id, username, family_id):
    url = f"https://{HTTPS_HOST}/v2/cmd/app/readtable"
    timestamp = generate_timestamp()
    random_str = generate_uuid()
    serial = generate_serial()
    
    req_data = {
        "accessToken": access_token,
        "random": random_str,
        "serial": serial,
        "userId": user_id,
        "userName": username,
        "lastUpdateTime": 0,
        "ver": SOFTWARE_VER,
        "sign": "1234567890",
        "timestamp": timestamp,
        "sessionId": "",
        "deviceFlag": 1,
        "familyId": family_id,
        "pageIndex": 0,
        "dataType": "all"
    }
    params = {
        "accessToken": req_data["accessToken"],
        "dataType": req_data["dataType"],
        "deviceFlag": req_data["deviceFlag"],
        "familyId": req_data["familyId"],
        "lastUpdateTime": req_data["lastUpdateTime"],
        "pageIndex": req_data["pageIndex"],
        "random": req_data["random"],
        "serial": req_data["serial"],
        "sessionId": req_data["sessionId"],
        "timestamp": req_data["timestamp"],
        "userId": req_data["userId"],
        "userName": req_data["userName"],
        "ver": req_data["ver"]
    }
    req_data["sign"] = create_sign(params)
    
    log(f"获取设备列表 (familyId={family_id}) ...")
    resp = https_request(url, json.dumps(req_data))
    
    if "data" not in resp:
        raise Exception(f"获取设备列表失败: {resp.get('message', resp)}")
    
    data = resp["data"]
    devices = []
    
    # device 可能是 dict 或 list
    raw_devices = data.get("device", [])
    if isinstance(raw_devices, dict):
        for device_id, item in raw_devices.items():
            if not isinstance(item, dict):
                continue
            devices.append({
                "device_id": device_id,
                "device_name": item.get("deviceName", ""),
                "device_type": item.get("deviceType", 0),
                "uid": item.get("uid", ""),
                "room_name": item.get("roomName", ""),
                "online": item.get("online", 0),
            })
    elif isinstance(raw_devices, list):
        for item in raw_devices:
            if not isinstance(item, dict):
                continue
            device_id = item.get("deviceId", "")
            if not device_id:
                continue
            devices.append({
                "device_id": device_id,
                "device_name": item.get("deviceName", ""),
                "device_type": item.get("deviceType", 0),
                "uid": item.get("uid", ""),
                "room_name": item.get("roomName", ""),
                "online": item.get("online", 0),
            })
    
    return devices

# ═══════════════════════════════════════════
# SSL 控制
# ═══════════════════════════════════════════
def ssl_control_device(username, password_md5, device_id, device_uid, state_on, family_id="", host=SSL_HOST, port=SSL_PORT):
    """通过 SSL 长连接控制设备开关。返回 (success, response_dict)"""
    
    log(f"SSL 连接到 {host}:{port} ...")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(CLIENT_CERT, CLIENT_KEY)
    
    sock = None
    try:
        raw_sock = socket.create_connection((host, port), timeout=15)
        raw_sock.settimeout(15)
        sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        log(f"  ✓ SSL 连接成功 cipher={sock.cipher()}")
    except Exception as e:
        log(f"  ✗ SSL 连接失败: {e}")
        return False, {"error": str(e)}
    
    buf = bytearray()
    session_id = b"0" * 32
    dynamic_key = None
    
    def send(payload, key_str, use_dynamic=False):
        nonlocal session_id
        key = key_str.encode("utf-8")[:16].ljust(16, b"\x00") if isinstance(key_str, str) else key_str[:16]
        pkt = build_packet(payload, key_str, session_id, use_dynamic)
        sock.sendall(pkt)
    
    def recv_until_idle(timeout=10, idle_timeout=0.5):
        nonlocal buf, dynamic_key, session_id
        deadline = time.monotonic() + timeout
        packets = []
        last_data = None
        while time.monotonic() < deadline:
            if last_data is not None and time.monotonic() - last_data >= idle_timeout:
                break
            wait = min(0.25, max(0.0, deadline - time.monotonic()))
            readable, _, _ = select.select([sock], [], [], wait)
            if not readable:
                continue
            try:
                data = sock.recv(65536)
            except Exception:
                break
            if not data:
                break
            last_data = time.monotonic()
            buf.extend(data)
            while True:
                start = buf.find(b"\x68\x64")
                if start < 0:
                    if buf.endswith(b"\x68"):
                        buf[:] = b"\x68"
                    else:
                        buf.clear()
                    break
                if start:
                    del buf[:start]
                if len(buf) < 4:
                    break
                length = struct.unpack(">H", buf[2:4])[0]
                if length < 42:
                    del buf[:2]
                    continue
                if len(buf) < length:
                    break
                frame = bytes(buf[:length])
                del buf[:length]
                keys_to_try = [DEFAULT_KEY]
                if dynamic_key:
                    keys_to_try.insert(0, dynamic_key)
                decoded = decode_packet(frame, keys_to_try)
                if decoded is not None:
                    sid_from_pkt = frame[10:42].decode("ascii", errors="ignore").strip("\x00")
                    if sid_from_pkt and sid_from_pkt != "0" * 32:
                        session_id = sid_from_pkt.ljust(32, "0")[:32].encode("ascii")
                    packets.append(decoded)
        return packets
    
    try:
        # ── Step 1: Hello ──
        log("Step 1: 发送 Hello (cmd=0) ...")
        hello_payload = {
            "source": SOFTWARE_NAME,
            "softwareVersion": SOFTWARE_VERSION,
            "sysVersion": SYS_VERSION,
            "hardwareVersion": HARDWARE_VERSION,
            "language": LANGUAGE,
            "identifier": generate_uuid()[:12],
            "phoneName": PHONE_NAME,
            "cmd": CMD_HELLO,
            "serial": generate_serial(),
            "clientType": 1,
            "uniSerial": int(time.time() * 1000),
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        send(hello_payload, DEFAULT_KEY)
        hello_responses = recv_until_idle(timeout=10, idle_timeout=0.5)
        
        hello_key = None
        for p in hello_responses:
            log(f"  Hello 响应: cmd={p.get('cmd')}, key={'***' if p.get('key') else 'None'}, status={p.get('status')}")
            if p.get("cmd") == 0 and p.get("key"):
                hello_key = str(p["key"])
                sid = p.get("sessionId")
                if sid:
                    session_id = str(sid).ljust(32, "0")[:32].encode("ascii")
                break
        
        if not hello_key:
            cmds = [p.get("cmd") for p in hello_responses]
            log(f"  ✗ Hello 未返回密钥, cmd列表={cmds}")
            return False, {"stage": "hello", "error": f"no_key cmd_list={cmds}"}
        
        dynamic_key = hello_key
        log(f"  ✓ 获取动态密钥: {dynamic_key}")
        
        # ── Step 2: Login ──
        log("Step 2: 发送 Login (cmd=2) ...")
        login_payload = {
            "userName": username,
            "password": password_md5,
            "cmd": CMD_LOGIN,
            "serial": generate_serial(),
            "clientType": 1,
            "source": SOFTWARE_VER,
            "familyId": family_id,
            "type": 4,
            "needAccountDetailError": True,
        }
        send(login_payload, dynamic_key, use_dynamic=True)
        login_responses = recv_until_idle(timeout=10, idle_timeout=0.5)
        
        login_ok = False
        for p in login_responses:
            status = p.get("status")
            msg = p.get("msg", "")
            extra = {k: v for k, v in p.items() if k not in ("cmd", "serial", "status", "msg")}
            log(f"  Login 响应: cmd={p.get('cmd')}, status={status}, msg={msg}, extra_keys={list(extra.keys())}")
            if p.get("cmd") == 2 and status in (None, 0, "0"):
                login_ok = True
                break
            if p.get("cmd") == 2 and status not in (None, 0, "0"):
                log(f"  ✗ Login 被拒绝 status={status} msg={msg} extra={extra}")
                return False, {"stage": "login", "error": f"rejected status={status} msg={msg} extra={extra}"}
        
        if not login_ok:
            cmds = [p.get("cmd") for p in login_responses]
            log(f"  ✗ Login 未收到有效响应, cmd列表={cmds}")
            return False, {"stage": "login", "error": f"no_response cmd_list={cmds}"}
        
        log("  ✓ SSL 登录成功")
        
        # ── Step 3: Control ──
        log(f"Step 3: 发送控制命令 (cmd=15) deviceId={device_id} state={'on' if state_on else 'off'} ...")
        state_str = "on" if state_on else "off"
        control_payload = {
            "uid": device_uid,
            "userName": username,
            "deviceId": device_id,
            "groupId": "",
            "order": "set property",
            "value1": 0,
            "value2": 0,
            "value3": 0,
            "value4": 0,
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "properties": {"onoff": {"status": state_str}},
            "cmd": CMD_CONTROL,
            "serial": generate_serial(),
            "clientType": 1,
            "uniSerial": int(time.time() * 1000),
            "serverRecord": False,
            "ver": SOFTWARE_VER,
            "debugInfo": DEBUG_INFO,
        }
        send(control_payload, dynamic_key, use_dynamic=True)
        log("  等待控制响应 (cmd=42) ...")
        control_responses = recv_until_idle(timeout=8, idle_timeout=0.5)
        
        control_ok = False
        for p in control_responses:
            extra = {k: v for k, v in p.items() if k not in ("cmd", "serial", "status", "msg")}
            log(f"  控制响应: cmd={p.get('cmd')}, status={p.get('status')}, value1={p.get('value1')}, extra_keys={list(extra.keys())}")
            if p.get("cmd") == 42 and p.get("status") in (None, 0, "0"):
                control_ok = True
        
        if control_ok:
            log(f"  ✓ 控制成功 device={device_id} -> {state_str}")
        else:
            cmds = [p.get("cmd") for p in control_responses]
            log(f"  ⚠ 未收到 cmd=42 确认, 收到命令={cmds}")
        
        return control_ok, {"stage": "control", "success": control_ok, "responses": control_responses}
    
    except Exception as e:
        log(f"  ✗ SSL 异常: {e}")
        return False, {"stage": "exception", "error": str(e)}
    finally:
        if sock:
            sock.close()

# ═══════════════════════════════════════════
# 交互式主流程
# ═══════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="ORVIBO 远程控制测试工具")
    parser.add_argument("--username", help="用户名（手机号）")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--family-id", help="直接指定家庭 ID")
    parser.add_argument("--device-id", help="直接指定设备 ID")
    parser.add_argument("--device-uid", help="直接指定设备 UID")
    parser.add_argument("--action", choices=["on", "off"], help="控制动作")
    args = parser.parse_args()

    print("╔══════════════════════════════════════╗")
    print("║   ORVIBO 远程控制测试工具 v1.0       ║")
    print("╚══════════════════════════════════════╝")
    print()

    # ── Step 1: 获取用户名密码 ──
    print("【Step 1】账号信息")
    username = args.username or input("  用户名 (手机号): ").strip()
    password = args.password or input("  密码: ").strip()
    if not username or not password:
        print("  用户名和密码不能为空")
        sys.exit(1)

    password_md5 = md5_hex(password)
    log(f"  密码 MD5: {password_md5}")
    print()
    
    # ── Step 2: HTTPS 登录 → 选家庭 ──
    print("【Step 2】HTTPS 获取家庭列表")
    direct_mode = args.family_id and args.device_id and args.device_uid and args.action
    try:
        access_token, user_id = get_access_token(username, password)
        log(f"  access_token={access_token[:16]}... user_id={user_id}")
        
        families = get_family_list(user_id, access_token)
        log(f"  获取到 {len(families)} 个家庭")
        
        if not families:
            print("  ✗ 未找到任何家庭")
            sys.exit(1)

        if direct_mode:
            family_id = args.family_id
            family_name = next((f.get("familyName","") for f in families if f.get("familyId") == family_id), "(直接指定)")
            log(f"  直接指定家庭: {family_name} ({family_id})")
            device_id = args.device_id
            device_uid = args.device_uid
            device_name = "(直接指定)"
            state_on = args.action == "on"
            log(f"  直接指定设备: deviceId={device_id}, uid={device_uid}, action={args.action}")
        else:
            print("\n  ┌─────┬──────────────────────────┬──────────────────────────┐")
            print("  │ 编号 │ 家庭名称                  │ 家庭 ID                   │")
            print("  ├─────┼──────────────────────────┼──────────────────────────┤")
            for i, f in enumerate(families):
                fid = f.get("familyId", "")
                fname = f.get("familyName", "未知")
                print(f"  │ {i+1:<3} │ {fname:<24} │ {fid:<24} │")
            print("  └─────┴──────────────────────────┴──────────────────────────┘")
            print()

            choice = input("  选择家庭编号 [1]: ").strip()
            if not choice:
                choice = "1"
            idx = int(choice) - 1
            if idx < 0 or idx >= len(families):
                print("  ✗ 无效编号")
                sys.exit(1)

            selected_family = families[idx]
            family_id = selected_family["familyId"]
            family_name = selected_family["familyName"]
            log(f"  选中家庭: {family_name} ({family_id})")
            print()

            # ── Step 3: 获取设备列表 ──
            print("【Step 3】获取设备列表")
            devices = get_devices(access_token, user_id, username, family_id)
            log(f"  获取到 {len(devices)} 个设备")

            if not devices:
                print("  ✗ 未找到任何设备")
                sys.exit(1)

            print("\n  ┌─────┬──────────────────────────┬──────────┬──────────────────────────┐")
            print("  │ 编号 │ 设备名称                  │ 在线      │ 设备 ID                   │")
            print("  ├─────┼──────────────────────────┼──────────┼──────────────────────────┤")
            for i, d in enumerate(devices):
                online = "✓在线" if d.get("online") else "离线"
                name = d.get("device_name", "") or "(未命名)"
                did = d.get("device_id", "")
                print(f"  │ {i+1:<3} │ {name:<24} │ {online:<8} │ {did:<24} │")
            print("  └─────┴──────────────────────────┴──────────┴──────────────────────────┘")
            print()

            # ── Step 4: 选择设备控制 ──
            print("【Step 4】选择要控制的设备")
            choice = input(f"  选择设备编号 [1-{len(devices)}]: ").strip()
            if not choice:
                print("  未选择，退出")
                sys.exit(0)
            idx = int(choice) - 1
            if idx < 0 or idx >= len(devices):
                print("  ✗ 无效编号")
                sys.exit(1)

            selected_device = devices[idx]
            device_id = selected_device["device_id"]
            device_uid = selected_device.get("uid", "")
            device_name = selected_device.get("device_name", "(未命名)")

            log(f"  选中设备: {device_name} (deviceId={device_id}, uid={device_uid})")

            if not device_uid:
                print("  ⚠ 该设备缺少 uid，可能无法控制")

            print("\n  请选择操作:")
            print("    1. 打开")
            print("    2. 关闭")
            action = input("  操作 [1]: ").strip() or "1"
            state_on = action == "1"
        print()
        
        # ── Step 5: SSL 执行控制 ──
        print("【Step 5】SSL 执行控制")
        print(f"  {'='*50}")
        success, detail = ssl_control_device(
            username=username,
            password_md5=password_md5,
            device_id=device_id,
            device_uid=device_uid,
            state_on=state_on,
            family_id=family_id,
        )
        print(f"  {'='*50}")
        
        print(f"\n  {'✅ 控制成功' if success else '❌ 控制失败'}")
        print()
    
    except KeyboardInterrupt:
        print("\n  用户中断")
        sys.exit(0)
    except Exception as e:
        log(f"  ✗ 异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
