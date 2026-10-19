"""Bounded ordinary XTS HDD wire builders and recording-list parsers.

Static native evidence: docs/meari-hdd-protocol.md. No record-ID mode, timezone
conversion, remote URL selection, or credential values leave this module.
"""
from __future__ import annotations

import base64
import calendar
from datetime import datetime
import hashlib
import json
import re
import struct

MAGIC = 0x56565099
COMMON_COMMAND = 0x8000
PLAYBACK_COMMAND = 0x131A
MAX_REPLY_BYTES = 262144
MAX_FRAGMENTS = 256
MAX_PUBLIC_BYTES = 49152


def validate_request(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"operation", "date"}:
        raise ValueError("hdd_request_invalid")
    operation, date = value.get("operation"), value.get("date")
    width = {"month": 6, "day": 8, "playback": 14}.get(operation)
    if width is None or not isinstance(date, str) or re.fullmatch(r"[0-9]{%d}" % width, date) is None:
        raise ValueError("hdd_request_invalid")
    try:
        datetime(int(date[:4]), int(date[4:6]), int(date[6:8]) if width >= 8 else 1,
                 int(date[8:10]) if width == 14 else 0,
                 int(date[10:12]) if width == 14 else 0,
                 int(date[12:14]) if width == 14 else 0)
    except ValueError:
        raise ValueError("hdd_request_invalid") from None
    return {"operation": operation, "date": date}


def packet(context: dict, command: int, sequence: int, payload: bytes) -> bytes:
    if not 0 <= sequence <= 0xFFFFFFFF or not 0 <= len(payload) <= MAX_REPLY_BYTES:
        raise ValueError("hdd_packet_invalid")
    host_key = context["device"]["hostKey"]
    password = host_key if context["vvp_auth"]["full_password"] else host_key[:16]
    parts = ["admin", password, str(MAGIC), str(sequence), str(command), str(len(payload)), "meari.p2p.ppcs"]
    if context["vvp_auth"]["include_licence"]:
        serial = context["device"]["snNum"]
        parts.append("00000000000" + serial if len(serial) == 9 else serial)
    signature = hashlib.md5("|".join(parts).encode()).hexdigest().encode("ascii")
    return struct.pack(">IIII", MAGIC, 1, sequence, command) + signature + struct.pack(">I", len(payload)) + payload


def search_packet(context: dict, sequence: int) -> bytes:
    request = validate_request(context["hdd"])
    operation = request["operation"]
    if operation not in ("month", "day"):
        raise ValueError("hdd_request_invalid")
    path = "/devices/record/search_by_" + operation
    body = {"action": "POST", "rec_type": 0, "videoid": context["channel_id"]}
    if operation == "day":
        body["version"] = 0
    body[operation] = request["date"]
    body["deviceurl"] = "http://127.0.0.1" + path
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    basic = base64.b64encode(("admin:" + context["device"]["hostKey"]).encode("ascii")).decode("ascii")
    header = (f"POST {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n"
              "User-Agent: Awesome HTTP Client\r\n"
              "Content-Type: text/plain, charset=us-ascii\r\nConnection: close\r\n"
              f"Authorization: Basic {basic}\r\nContent-Length: {len(encoded)}\r\n\r\n")
    return packet(context, COMMON_COMMAND, sequence, header.encode("ascii") + encoded)


def playback_packet(context: dict, sequence: int, *, stop: bool = False) -> bytes:
    request = validate_request(context["hdd"])
    if request["operation"] != "playback":
        raise ValueError("hdd_request_invalid")
    tail = bytearray(24)
    struct.pack_into("<II", tail, 0, context["channel_id"], 1 if stop else 16)
    if not stop:
        date = request["date"]
        struct.pack_into("<HBB", tail, 12, int(date[:4]), int(date[4:6]), int(date[6:8]))
        tail[17:20] = bytes((int(date[8:10]), int(date[10:12]), int(date[12:14])))
        tail[20:22] = b"\x01\x02"
    return packet(context, PLAYBACK_COMMAND, sequence, bytes(tail))


class ReplyBuffer:
    """VVP control replies can arrive separately or fragmented inside IVA data."""

    def __init__(self, sequence: int) -> None:
        self.sequence = sequence
        self.buffer = bytearray()

    def feed(self, payload: bytes) -> bytes | None:
        magic = struct.pack(">I", MAGIC)
        if not self.buffer and not (payload.startswith(magic) or payload and magic.startswith(payload)):
            return None
        if len(self.buffer) + len(payload) > MAX_REPLY_BYTES + 52:
            raise ValueError("hdd_reply_limit")
        self.buffer.extend(payload)
        while len(self.buffer) >= 52:
            if struct.unpack_from(">I", self.buffer)[0] != MAGIC:
                raise ValueError("hdd_reply_invalid")
            sequence, length = struct.unpack_from(">I", self.buffer, 8)[0], struct.unpack_from(">I", self.buffer, 48)[0]
            if length > MAX_REPLY_BYTES:
                raise ValueError("hdd_reply_limit")
            if len(self.buffer) < 52 + length:
                return None
            body = bytes(self.buffer[52:52 + length])
            del self.buffer[:52 + length]
            if sequence < self.sequence:
                continue
            if sequence > self.sequence:
                raise ValueError("hdd_reply_sequence")
            return body
        return None


def parse_search_reply(payload: bytes, request: dict) -> dict:
    request = validate_request(request)
    if len(payload) > MAX_REPLY_BYTES:
        raise ValueError("hdd_reply_limit")
    envelope = json.loads(payload.rstrip(b"\x00"))
    if not isinstance(envelope, dict) or type(envelope.get("http_code")) not in (int, str) or str(envelope["http_code"]) != "200":
        raise ValueError("hdd_http_failed")
    body = envelope.get("http_result")
    if not isinstance(body, str) or len(body) > MAX_REPLY_BYTES:
        raise ValueError("hdd_reply_invalid")
    value = json.loads(body)
    operation, date = request["operation"], request["date"]
    result = {"hdd_operation": operation, "calendar_date": date}
    if operation == "month":
        if not isinstance(value, list) or len(value) > 31:
            raise ValueError("hdd_reply_invalid")
        last_day = calendar.monthrange(int(date[:4]), int(date[4:6]))[1]
        days = [f"{date}{index + 1:02d}" for index, flag in enumerate(value[:last_day]) if type(flag) in (int, str) and str(flag) == "1"]
        return {**result, "recording_days": days, "recording_count": len(days), "fragments_truncated": False}
    if operation != "day" or not isinstance(value, list) or len(value) > 65535:
        raise ValueError("hdd_reply_invalid")
    fragments = []
    invalid = 0
    for item in value:
        if not isinstance(item, str) or re.fullmatch(r"[0-9]{6}-[0-9]{6}", item) is None:
            invalid += 1
            continue
        start, end = date + item[:6], date + item[7:]
        try:
            validate_request({"operation": "playback", "date": start})
            validate_request({"operation": "playback", "date": end})
        except ValueError:
            invalid += 1
            continue
        if end <= start:
            invalid += 1
            continue
        if len(fragments) < MAX_FRAGMENTS:
            fragments.append({"start": start, "end": end})
    result.update(fragments=fragments, recording_count=len(value) - invalid,
                  fragments_truncated=len(value) - invalid > len(fragments), invalid_fragment_count=invalid)
    if len(json.dumps(result, separators=(",", ":"))) > MAX_PUBLIC_BYTES:
        raise ValueError("hdd_result_limit")
    return result
