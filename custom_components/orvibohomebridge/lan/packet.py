#!/usr/bin/env python3
"""Orvibo 局域网协议封包/解包层。

封包格式：42字节头 + AES-ECB 加密的 JSON payload
头：hd(2B) + 包长度(2B) + 包类型(2B) + CRC32(4B) + sessionId(32B)
"""

import binascii
import json
import logging
import struct
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

# === 常量 ===
MAGIC = b"\x68\x64"  # "hd"
DEFAULT_KEY = "khggd54865SNJHGF"
UDP_PORT = 10000
TCP_PORT = 8088
UDP_BROADCAST = "255.255.255.255"
SOFTWARE_VER = "5.1.3.309"
SOFTWARE_NAME = "ZhiJia365"
SOFTWARE_VERSION = "50103309"
SYS_VERSION = "Android14_34"
HARDWARE_VERSION = "Google Pixel 8"
LANGUAGE = "zh"
PHONE_NAME = "Pixel 8"
DEBUG_INFO = "Android_ZhiJia365_34_5.1.3.309"
SIGN_KEY = "nQ45RjPtOws96jmH"
HTTPS_HOST = "china.orvibo.com"

# 命令常量
CMD_HELLO = 0
CMD_LOGIN = 2
CMD_CONTROL = 15
CMD_STATE_UPDATE = 42
CMD_HEARTBEAT = 32

ID_UNSET = b"\x20" * 32
PK_TYPE = b"\x70\x6b"  # plain key (默认key加密)
DK_TYPE = b"\x64\x6b"  # derived key (session key加密)


def _crc32(data: bytes) -> bytes:
    return struct.pack(">I", binascii.crc32(data) & 0xFFFFFFFF)


# AES cipher 单例（密钥固定不变，无需每次重新创建 cipher 对象 -- 高频控制场景下可显著减少临时对象）
_AES_BACKEND = default_backend()
_AES_CIPHER_CACHE: dict[bytes, Cipher] = {}


def _get_cipher(key: bytes, mode: modes.Mode) -> Cipher:
    """获取或创建 AES cipher 单例。key 相同时复用 cipher 对象。"""
    if key not in _AES_CIPHER_CACHE:
        _AES_CIPHER_CACHE[key] = Cipher(algorithms.AES(key), mode, backend=_AES_BACKEND)
    return _AES_CIPHER_CACHE[key]


def _encrypt_aes_ecb(key: bytes, plaintext: str) -> bytes:
    data = plaintext.encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = _get_cipher(key, modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _decrypt_aes_ecb(key: bytes, encrypted: bytes) -> str:
    decryptor = _get_cipher(key, modes.ECB()).decryptor()
    data = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    unpad = unpadder.update(data) + unpadder.finalize()
    return unpad.decode("utf-8").rstrip("\x00")


def build_packet(packet_type: bytes, key: bytes, session_id: bytes, payload: dict) -> bytes:
    """构造协议包。packet_type: b'pk'(0x706b) 或 b'dk'(0x646b)"""
    payload_str = json.dumps(payload, separators=(",", ":"))
    encrypted = _encrypt_aes_ecb(key, payload_str)
    crc = _crc32(encrypted)
    length = 2 + 2 + 2 + 4 + 32 + len(encrypted)
    length_bytes = struct.pack(">H", length)
    return MAGIC + length_bytes + packet_type + crc + session_id + encrypted


def parse_packet(
    data: bytes,
    keys: Mapping[bytes, bytes],
) -> dict[str, Any] | None:
    """解析协议包。keys: {session_id_bytes: aes_key_bytes}"""
    if len(data) < 42:
        return None
    if data[0:2] != MAGIC:
        return None
    length = struct.unpack(">H", data[2:4])[0]
    if len(data) != length:
        return None
    ptype = data[4:6]
    crc_recv = data[6:10]
    session_id = data[10:42]
    encrypted = data[42:]

    if _crc32(encrypted) != crc_recv:
        _LOGGER.debug("CRC mismatch, ignoring")
        return None

    if ptype == PK_TYPE:
        key = DEFAULT_KEY.encode()
    else:
        key = keys.get(session_id, DEFAULT_KEY.encode())

    try:
        plain = _decrypt_aes_ecb(key, encrypted)
        payload = json.loads(plain)
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        _LOGGER.error(f"解密/解析失败: {e}")
        return None
