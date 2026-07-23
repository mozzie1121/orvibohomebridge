#!/usr/bin/env python3
"""
ORVIBO HomeBridge SSL 连接诊断工具

模拟完整的 SSL → Hello → Login → Heartbeat 流程，
在 NAS 上运行，不依赖 HA 环境。

用法:
  python3 tests/diagnose_ssl.py --username your_phone --password your_pw --family-id your_family_id

可选参数:
  --host china.orvibo.com       SSL 服务器地址 (默认 china.orvibo.com)
  --port 10002                  SSL 端口 (默认 10002)
  --cert-dir ./certs            证书目录 (默认集成自带的 certs/)
  --verbose                     详细日志
"""

import os
import sys
import ssl
import json
import hashlib
import asyncio
import logging
import argparse
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.orvibohomebridge.packet import HomematePacket, HomemateJsonData
from custom_components.orvibohomebridge.const import (
    CLIENT_CERT, CLIENT_KEY, SERVER_CA, DEFAULT_KEY, ID_UNSET,
    CMD_HELLO, CMD_LOGIN, CMD_HEARTBEAT,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger("diagnose")


class SslDiagnose:
    """SSL 连接诊断器 — 模拟完整连接流程并汇报每一步结果"""

    STEP_NAMES = {
        0: "TCP连接",
        1: "SSL握手",
        2: "Hello 发送",
        3: "Hello 响应 (session_key)",
        4: "Login 发送",
        5: "Login 响应 (认证)",
        6: "心跳",
    }

    def __init__(self, host: str, port: int, username: str, password: str,
                 family_id: str, cert_dir: str | None = None, verbose: bool = False):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.family_id = family_id
        self.verbose = verbose

        # 证书路径
        if cert_dir:
            self.certfile = Path(cert_dir) / "client_cert.pem"
            self.keyfile = Path(cert_dir) / "client_key.pem"
            self.cafile = Path(cert_dir) / "server_ca.pem"
        else:
            self.certfile = Path(CLIENT_CERT)
            self.keyfile = Path(CLIENT_KEY)
            self.cafile = Path(SERVER_CA)

        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.session_id: str | None = None
        self.session_key: bytes | None = None
        self.results: dict[int, bool] = {}
        self.errors: dict[int, str] = {}

    def _result(self, step: int, ok: bool, msg: str = ""):
        self.results[step] = ok
        name = self.STEP_NAMES[step]
        if ok:
            _LOGGER.info(f"  ✅ [{name}] 成功")
        else:
            _LOGGER.error(f"  ❌ [{name}] 失败: {msg}")
            self.errors[step] = msg

    async def _send_packet(self, data: dict, key: bytes):
        """发送加密数据包"""
        if key == DEFAULT_KEY.encode("utf-8"):
            packet_type = bytes([0x70, 0x6b])  # pk
            sid = bytes(ID_UNSET)
        else:
            packet_type = bytes([0x64, 0x6b])  # dk
            sid = self.session_id.encode("utf-8") if self.session_id else bytes(ID_UNSET)

        ciphertext = HomematePacket.build_packet(
            packet_type=packet_type,
            key=key,
            session_id=sid,
            payload=data,
        )
        self.writer.write(ciphertext)
        await self.writer.drain()

    async def _read_packet(self, timeout: float = 10) -> dict | None:
        """从 SSL 流读取一个完整的加密包并解密"""
        try:
            header = await asyncio.wait_for(self.reader.readexactly(42), timeout=timeout)
        except asyncio.IncompleteReadError as e:
            _LOGGER.error(f"  读取头部不完整: {e}")
            return None
        except asyncio.TimeoutError:
            _LOGGER.error(f"  读取头部超时 ({timeout}s)")
            return None

        length = HomematePacket.parse_length(header)
        try:
            body = await asyncio.wait_for(self.reader.readexactly(length - 42), timeout=timeout)
        except asyncio.IncompleteReadError as e:
            _LOGGER.error(f"  读取body不完整: {e}")
            return None
        except asyncio.TimeoutError:
            _LOGGER.error(f"  读取body超时 ({timeout}s)")
            return None

        try:
            key_map = {self.session_id or "": self.session_key or DEFAULT_KEY.encode("utf-8")}
            packet = HomematePacket(header + body, key_map)
            return packet.json_payload
        except Exception as e:
            _LOGGER.error(f"  包解析/解密失败: {e}")
            return None

    async def run(self) -> dict:
        """执行完整诊断流程"""
        _LOGGER.info("=" * 50)
        _LOGGER.info("ORVIBO HomeBridge SSL 诊断工具")
        _LOGGER.info("=" * 50)
        _LOGGER.info(f"  服务器: {self.host}:{self.port}")
        _LOGGER.info(f"  用户名: {self.username}")
        _LOGGER.info(f"  FamilyID: {self.family_id}")
        _LOGGER.info(f"  证书目录: {self.certfile.parent}")
        _LOGGER.info("=" * 50)

        # 第一步：TCP 连接
        _LOGGER.info("\n[1/6] TCP 连接...")
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10
            )
            self._result(0, True)
        except Exception as e:
            self._result(0, False, str(e))
            return self.summary()

        # 第二步：SSL 握手
        _LOGGER.info("\n[2/6] SSL 握手...")
        try:
            for f in [self.certfile, self.keyfile, self.cafile]:
                if not f.exists():
                    raise FileNotFoundError(f"证书文件不存在: {f}")

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_cert_chain(str(self.certfile), str(self.keyfile))
            ctx.load_verify_locations(str(self.cafile))
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.check_hostname = False

            # 包装 SSL
            self.reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(self.reader)
            transport, _ = await asyncio.get_event_loop().create_connection(
                lambda: protocol, sock=self.writer.transport.get_extra_info("socket"),
                ssl=ctx, server_hostname=self.host,
            )
            self.writer = asyncio.StreamWriter(transport, protocol, self.reader, asyncio.get_event_loop())
            self._result(1, True)
            _LOGGER.info(f"  SSL 版本: {transport.get_extra_info('ssl_object').version()}")
            _LOGGER.info(f"  加密套件: {transport.get_extra_info('ssl_object').cipher()}")
        except Exception as e:
            self._result(1, False, str(e))
            return self.summary()

        # 第三步：发送 Hello (cmd=0)
        _LOGGER.info("\n[3/6] 发送 Hello...")
        try:
            hello_payload = HomemateJsonData.ssl_get_session()
            await self._send_packet(hello_payload, DEFAULT_KEY.encode("utf-8"))
            self._result(2, True)

            # 等待 Hello 响应（cmd=0, 带 key）
            resp = await self._read_packet()
            if resp is None:
                self._result(3, False, "无响应")
                return self.summary()
            cmd = resp.get("cmd")
            key = resp.get("key")
            if cmd != CMD_HELLO:
                self._result(3, False, f"期望 cmd=0, 收到 cmd={cmd}, data={resp}")
                return self.summary()
            if not key:
                self._result(3, False, f"Hello 响应无 key 字段: {resp}")
                return self.summary()
            self.session_key = str(key).encode("utf-8")
            self.session_id = resp.get("sessionId", self.session_id or "")
            _LOGGER.info(f"  会话ID: {self.session_id}")
            _LOGGER.info(f"  会话密钥: {key} (hex={self.session_key.hex()}, len={len(self.session_key)})")
            self._result(3, True)
        except Exception as e:
            self._result(2, False, str(e))
            self._result(3, False, str(e))
            return self.summary()

        # 第四步：发送 Login (cmd=2)
        _LOGGER.info("\n[4/6] 发送 Login...")
        try:
            pw_md5 = hashlib.md5(self.password.encode()).hexdigest().upper()
            login_payload = HomemateJsonData.ssl_login(
                username=self.username, password_md5=pw_md5, family_id=self.family_id
            )
            await self._send_packet(login_payload, self.session_key)
            self._result(4, True)
        except Exception as e:
            self._result(4, False, str(e))
            return self.summary()

        # 第五步：等待 Login 响应
        _LOGGER.info("\n[5/6] 等待 Login 响应...")
        try:
            resp = await self._read_packet()
            if resp is None:
                self._result(5, False, "无响应")
                return self.summary()
            cmd = resp.get("cmd")
            status = resp.get("status")
            user_id = resp.get("userId")
            _LOGGER.info(f"  Login 响应: cmd={cmd}, status={status}, userId={user_id}")
            if cmd != CMD_LOGIN:
                self._result(5, False, f"期望 cmd=2, 收到 cmd={cmd}")
                return self.summary()
            if status == 0 or user_id:
                self._result(5, True)
            else:
                self._result(5, False, f"status={status}, msg={resp.get('msg')}")
                return self.summary()
        except Exception as e:
            self._result(5, False, str(e))
            return self.summary()

        # 第六步：发送心跳 (cmd=32) 并保持观察
        _LOGGER.info("\n[6/6] 发送心跳...")
        try:
            hb_payload = HomemateJsonData.ssl_heartbeat()
            await self._send_packet(hb_payload, self.session_key)
            self._result(6, True)
            _LOGGER.info("  心跳发送成功，等待5秒观察是否有异常断开...")
            await asyncio.sleep(2)
            # 再试一次读包确认连接没断
            try:
                resp2 = await asyncio.wait_for(self.reader.readexactly(42), timeout=3)
                _LOGGER.info(f"  连接仍存活，收到后续数据 ({len(resp2)} bytes)")
            except asyncio.TimeoutError:
                _LOGGER.info("  连接正常保持（3秒无数据，正常）")
                self._result(6, True)
        except Exception as e:
            self._result(6, False, str(e))

        return self.summary()

    def summary(self) -> dict:
        """输出诊断总结"""
        _LOGGER.info("\n" + "=" * 50)
        _LOGGER.info("诊断结果汇总")
        _LOGGER.info("=" * 50)
        all_ok = True
        for step in sorted(self.results.keys()):
            name = self.STEP_NAMES[step]
            ok = self.results[step]
            mark = "✅" if ok else "❌"
            err = f" — {self.errors[step]}" if step in self.errors else ""
            _LOGGER.info(f"  {mark} {name}{err}")
            if not ok:
                all_ok = False

        _LOGGER.info("=" * 50)
        if all_ok:
            _LOGGER.info("🎉 所有步骤通过！SSL 连接正常。")
        else:
            failed = [self.STEP_NAMES[s] for s, ok in self.results.items() if not ok]
            _LOGGER.error(f"⚠️ 以下步骤失败: {', '.join(failed)}")
        _LOGGER.info("=" * 50)

        return {
            "all_ok": all_ok,
            "results": {self.STEP_NAMES[s]: ok for s, ok in self.results.items()},
            "errors": {self.STEP_NAMES[s]: e for s, e in self.errors.items()},
        }

    async def cleanup(self):
        try:
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(description="ORVIBO HomeBridge SSL 连接诊断")
    parser.add_argument("--host", default="china.orvibo.com", help="SSL 服务器地址")
    parser.add_argument("--port", type=int, default=10002, help="SSL 端口")
    parser.add_argument("--username", required=True, help="欧瑞博账号 (手机号)")
    parser.add_argument("--password", required=True, help="密码")
    parser.add_argument("--family-id", required=True, help="家庭 ID")
    parser.add_argument("--cert-dir", default=None, help="证书目录 (默认使用集成自带)")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    if not args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    diag = SslDiagnose(
        host=args.host, port=args.port,
        username=args.username, password=args.password,
        family_id=args.family_id, cert_dir=args.cert_dir,
        verbose=args.verbose,
    )
    try:
        result = await diag.run()
        sys.exit(0 if result["all_ok"] else 1)
    finally:
        await diag.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
