#!/usr/bin/env python3
"""
ORVIBO HomeBridge SSL 诊断工具 — 直接复用集成代码

用法:
  cd /volume1/volume1/hermers/orvibohomebridge
  python3 tests/test_ssl.py --username 17554263486 --password Sunjian21 --family-id 328256
"""

import os, sys, ssl, asyncio, logging, argparse, json, hashlib
from pathlib import Path

# 直接引入集成代码
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

from orvibohomebridge.const import (
    SSL_HOST, SSL_PORT, CLIENT_CERT, CLIENT_KEY, SERVER_CA, DEFAULT_KEY,
    SOFTWARE_VER, DEBUG_INFO,
)
from orvibohomebridge.packet import HomematePacket, HomemateJsonData, CMD_HELLO, CMD_LOGIN

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
_LOGGER = logging.getLogger("test")


async def test_ssl(host, port, username, password, family_id):
    cert_base = Path(CLIENT_CERT).parent
    cert = cert_base / "client_cert.pem"
    key = cert_base / "client_key.pem"
    ca  = cert_base / "server_ca.pem"

    for f in [cert, key, ca]:
        assert f.exists(), f"缺失: {f}"

    _LOGGER.info("=" * 50)
    _LOGGER.info(f"SSL 测试: {host}:{port}")
    _LOGGER.info(f"账号: {username} 家庭: {family_id}")

    # 1-2. TCP + SSL 一步完成
    _LOGGER.info("\n[1] 连接...")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(str(cert), str(key))
    ctx.load_verify_locations(str(ca))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx), timeout=15)
    ssl_obj = writer.transport.get_extra_info("ssl_object")
    _LOGGER.info(f"  ✅ {ssl_obj.version()} / {ssl_obj.cipher()[0]}")

    def send(data):
        writer.write(data)
        return writer.drain()

    async def recv(timeout=10):
        h = await asyncio.wait_for(reader.readexactly(42), timeout=timeout)
        length = HomematePacket.parse_length(h)
        body = await asyncio.wait_for(reader.readexactly(length - 42), timeout=timeout)
        return HomematePacket(h + body, {})

    # 3. Hello (pk)
    _LOGGER.info("\n[2] Hello...")
    hello = HomemateJsonData.ssl_get_session()
    pkt = HomematePacket.build_packet(bytes([0x70, 0x6b]), DEFAULT_KEY.encode(), bytes([0x20]*32), hello)
    await send(pkt)
    _LOGGER.info("  ✅ 已发送")

    resp = await recv()
    _LOGGER.info(f"  ✅ 收到: cmd={resp.json_payload.get('cmd')} key={resp.json_payload.get('key')}")
    session_key = str(resp.json_payload["key"]).encode()
    session_id = resp.json_payload.get("sessionId", "").encode().ljust(32, b" ")[:32]
    _LOGGER.info(f"  session_id={resp.json_payload.get('sessionId')} key_hex={session_key.hex()}")

    # 4. Login (dk)
    _LOGGER.info("\n[3] Login...")
    pw_md5 = hashlib.md5(password.encode()).hexdigest().upper()
    login = HomemateJsonData.ssl_login(username, pw_md5, family_id)
    pkt = HomematePacket.build_packet(bytes([0x64, 0x6b]), session_key, session_id, login)
    await send(pkt)
    _LOGGER.info("  ✅ 已发送")

    resp = await recv(timeout=10)
    data = resp.json_payload
    _LOGGER.info(f"  cmd={data.get('cmd')} status={data.get('status')} userId={data.get('userId')}")
    if data.get("status") == 0 or data.get("userId"):
        _LOGGER.info("  ✅ 登录成功！")
    else:
        _LOGGER.error(f"  ❌ 登录失败: {data}")
        writer.close()
        return

    # 5. 心跳保持
    _LOGGER.info("\n[4] 心跳测试...")
    hb = HomemateJsonData.ssl_heartbeat()
    pkt = HomematePacket.build_packet(bytes([0x64, 0x6b]), session_key, session_id, hb)
    await send(pkt)
    _LOGGER.info("  ✅ 心跳已发送，等待10秒观察连接保持...")
    await asyncio.sleep(10)
    try:
        _ = await asyncio.wait_for(reader.readexactly(42), timeout=2)
        _LOGGER.info("  ✅ 连接正常保持")
    except asyncio.TimeoutError:
        _LOGGER.info("  ✅ 连接正常保持（无推送数据）")

    _LOGGER.info("\n🎉 全部通过！")
    writer.close()


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=SSL_HOST)
    p.add_argument("--port", type=int, default=SSL_PORT)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--family-id", required=True)
    args = p.parse_args()
    await test_ssl(args.host, args.port, args.username, args.password, args.family_id)

if __name__ == "__main__":
    asyncio.run(main())
