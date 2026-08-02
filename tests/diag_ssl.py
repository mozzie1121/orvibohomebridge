"""SSL 长连接连通性诊断：逐段测试 DNS/TCP/TLS/HELLO，定位失败环节。"""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys
import time

sys.path.insert(0, r"D:\orvibo1\dump1\standalone")
from core.packet import HomematePacket, HomemateJsonData  # noqa: E402
from core.const import (  # noqa: E402
    DEFAULT_KEY,
    ID_UNSET,
    SSL_HOST,
    SSL_PORT,
)


async def main() -> None:
    print("1) DNS 解析", SSL_HOST)
    try:
        print("   ->", socket.gethostbyname(SSL_HOST))
    except Exception as e:
        print("   ❌ DNS 失败:", repr(e))
        return

    t0 = time.time()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(SSL_HOST, SSL_PORT), timeout=10
        )
        print(f"2) TCP 连接 OK（{time.time()-t0:.1f}s）")
        w.close()
    except Exception as e:
        print(f"2) ❌ TCP 连接失败（{time.time()-t0:.1f}s）: {e!r}")
        print("   提示：若此处超时，说明本机到 china.orvibo.com:10002 的网络不通")
        return

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cert_dir = r"D:\orvibo1\dump1\standalone\certs"
        ctx.load_cert_chain(
            cert_dir + r"\client_cert.pem", cert_dir + r"\client_key.pem"
        )
        ctx.load_verify_locations(cafile=cert_dir + r"\server_ca.pem")
        t0 = time.time()
        r2, w2 = await asyncio.wait_for(
            asyncio.open_connection(
                SSL_HOST, SSL_PORT, ssl=ctx, server_hostname=SSL_HOST
            ),
            timeout=12,
        )
        print(f"3) TLS 握手 OK（{time.time()-t0:.1f}s）")
    except Exception as e:
        print(f"3) ❌ TLS 握手失败: {e!r}")
        print("   提示：证书/CA 不匹配，或端口被中间设备拦截")
        return

    try:
        payload = HomemateJsonData.ssl_get_session()
        packet = HomematePacket.build_packet(
            bytes([0x70, 0x6B]), DEFAULT_KEY.encode(), bytes(ID_UNSET), payload
        )
        w2.write(packet)
        await w2.drain()
        hdr = await asyncio.wait_for(r2.readexactly(42), timeout=10)
        ln = HomematePacket.parse_length(hdr)
        body = await asyncio.wait_for(r2.readexactly(ln - 42), timeout=10)
        pkt = HomematePacket(hdr + body, {"": DEFAULT_KEY.encode()})
        print("4) HELLO 响应:", pkt.json_payload)
        w2.close()
        print("\n[OK] 全部通过：SSL 长连接可用")
    except Exception as e:
        print(f"4) [FAIL] HELLO 交互失败: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
