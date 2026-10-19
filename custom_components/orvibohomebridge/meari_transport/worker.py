"""Disposable XTS media receiver. Run by absolute path, never inside HA.

Credentials arrive once on private stdin. All stdout is suppressed except the
fixed records written by emit(); stderr and logging are disabled before vendor
imports. The parent MUST enforce a hard timeout and kill/wait, including DNS.
"""
from __future__ import annotations

import contextlib
import argparse
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

MAX_INPUT_BYTES = 65536
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_MEDIA_BYTES = 24 * 1024 * 1024
MAX_PACKETS = 100000
MAX_CANDIDATES = 8
MAX_STREAM_FRAME_BYTES = 8 * 1024 * 1024
MAX_STATUS_BYTES = 4096
MAX_STREAM_SECONDS = 3600
STREAM_IDLE_SECONDS = 30
TURN_REFRESH_SECONDS = 120
MAX_STREAM_FRAMES = 1000000
MAX_STREAM_MEDIA_BYTES = 64 * 1024 * 1024 * 1024
MAX_STREAM_PACKETS = 100000000
_DEPENDENCY_ROOT: str | None = None
STAGES = frozenset({
    "worker_started", "root_discovery", "signaling_connect",
    "signaling_registered", "device_status", "turn_allocate", "sdp_offer",
    "ice_connect", "kcp_connect", "receiving_video", "video_keyframe_received",
    "no_video_frame", "device_unavailable", "probe_failed", "probe_timeout",
    "dependency_missing", "invalid_context", "stream_complete",
    "stream_idle_timeout",
    "hdd_search_start", "hdd_search_received", "hdd_search_failed",
    "hdd_search_timeout", "hdd_playback_start", "hdd_playback_acknowledged",
    "hdd_playback_timeout", "hdd_playback_ended",
})


class OutputClosed(Exception):
    """Stop transport retries when the owned stdout consumer goes away."""


class BinaryOutput:
    """Bounded J status / V Annex-B records: ASCII kind + uint32 BE length."""

    def __init__(self, output) -> None:
        self.output = output

    def write(self, kind: bytes, payload: bytes) -> None:
        limit = MAX_STATUS_BYTES if kind == b"J" else MAX_STREAM_FRAME_BYTES
        if kind not in (b"J", b"V") or not payload or len(payload) > limit:
            raise ValueError("output_limit")
        try:
            for body in (kind + struct.pack(">I", len(payload)), payload):
                remaining = memoryview(body)
                while remaining:
                    written = self.output.write(remaining)
                    if not isinstance(written, int) or written <= 0:
                        raise OutputClosed()
                    remaining = remaining[written:]
            self.output.flush()
        except (OSError, ValueError) as err:
            raise OutputClosed() from err


def _text(value: object, maximum: int = 256) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid_context")
    value = str(value)
    if not value or len(value) > maximum or not value.isascii() or any(not 32 <= ord(c) <= 126 for c in value):
        raise ValueError("invalid_context")
    return value


def _endpoint(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = _text(value, 2048)
    url = urlsplit(text if "://" in text else "https://" + text)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("invalid_context")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname) or url.port not in (None, 443):
        raise ValueError("invalid_context")
    return text


def _hdd_protocol():
    root = str(Path(__file__).resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import hdd
    return hdd


def validate_context(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("invalid_context")
    account, device, auth = value.get("account"), value.get("device"), value.get("vvp_auth")
    if not isinstance(account, dict) or not isinstance(device, dict) or not isinstance(auth, dict):
        raise ValueError("invalid_context")
    if type(auth.get("full_password")) is not bool or type(auth.get("include_licence")) is not bool:
        raise ValueError("invalid_context")
    if type(device.get("iotType")) is not int or device["iotType"] != 3:
        raise ValueError("invalid_context")
    if type(device.get("devTypeID")) is not int or device["devTypeID"] not in (2, 11, 17, 21):
        raise ValueError("invalid_context")
    sn = _text(device.get("snNum"), 63)
    if len(sn) < 5:
        raise ValueError("invalid_context")
    _text(device.get("hostKey"), 63)
    channel = value.get("channel_id")
    if type(channel) is not int or not 0 <= channel <= 0xFFFFFF:
        raise ValueError("invalid_context")
    if device.get("nvrChannelId", channel) != channel:
        raise ValueError("invalid_context")
    timeout = value.get("timeout_seconds", 35)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 5 <= timeout <= 60:
        raise ValueError("invalid_context")
    _text(account.get("user_id"))
    _text(account.get("brand"), 32)
    country = _text(account.get("country_code"), 2)
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("invalid_context")
    version = _text(account.get("app_version"), 64)
    if not re.fullmatch(r"5\.2\.6\.307a[0-9.]{1,12}", version):
        raise ValueError("invalid_context")
    for key in ("api_server", "openapi_server", "platform_domain"):
        _endpoint(account.get(key), optional=key == "platform_domain")
    if type(value.get("camera_tags", 0)) is not int or value.get("camera_tags", 0) != 0:
        # Only the ordinary single-preview path is currently ported.
        raise ValueError("invalid_context")
    result = {**value, "timeout_seconds": float(timeout)}
    if "hdd" in value:
        result["hdd"] = _hdd_protocol().validate_request(value["hdd"])
        # Ordinary XTS IPC recordings use channel 0; NVRs retain their channel.
        # Do not enable unverified base-station/other device playback modes.
        if not (device["devTypeID"] == 11 or device["devTypeID"] == 2 and channel == 0):
            raise ValueError("invalid_context")
    return result


def load_protocol() -> SimpleNamespace:
    # -I deliberately excludes the script directory; use only this known root.
    root = str(Path(__file__).resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    if _DEPENDENCY_ROOT and _DEPENDENCY_ROOT not in sys.path:
        # Supplied by the parent from find_spec('Crypto'), not from user data.
        # Keep standard-library locations ahead of the HA --target directory.
        sys.path.append(_DEPENDENCY_ROOT)
    from vendor import budget
    from vendor.meari_signaling import MsgSvrClient
    from vendor.turn_client import BINDING_REQUEST, BINDING_RESPONSE, TurnClient
    from vendor.kcp_tunnel import KcpTunnel, parse_iva_frame, parse_kcp_segments
    from vendor.p2p_streamer import codec, codecs, protocol
    from vendor.p2p_streamer.network import _build_ice_response, recv_peer_packets
    from vendor.p2p_streamer.root_discovery import discover_msgsvr_endpoints
    from vendor.p2p_streamer.sdp import candidates_from_response, parse_sdp_answer
    return SimpleNamespace(**locals())


class Observation:
    """Decode vendor framing; optionally forward bounded Annex-B access units."""

    def __init__(self, library: SimpleNamespace, frame_output=None) -> None:
        self.library = library
        self.frame_output = frame_output
        self.video_frames = 0
        self.video_bytes = 0
        self.received_packets = 0
        self.received_bytes = 0
        self.codec = "unknown"
        self.keyframe_received = False
        self.parameter_sets_received = False
        self.parameters = library.codecs.CodecParameterCache()
        self.sequence = library.codec.FrameSequenceTracker()
        self.last_frame_at: float | None = None
        self.hdd_result: dict | None = None
        self.playback_requested = False
        self.playback_acknowledged = False
        self.playback_end_received = False

    def summary(self) -> dict:
        result = {
            "video_frames": self.video_frames, "video_bytes": self.video_bytes,
            "received_packets": self.received_packets, "codec": self.codec,
            "keyframe_received": self.keyframe_received,
            "parameter_sets_received": self.parameter_sets_received, "decoded": False,
        }
        if self.hdd_result is not None:
            result.update(self.hdd_result)
        if self.playback_requested:
            result.update(playback_requested=True, playback_acknowledged=self.playback_acknowledged,
                          playback_end_received=self.playback_end_received)
        return result

    def plausible_nals(self, payload: bytes) -> bytes:
        # This is a bounded header/length check, never a full bitstream decoder.
        # Reject header-only SPS/PPS/IDR and invalid forbidden/temporal bits.
        units = []
        for unit in self.library.codecs.annexb_units(payload):
            nal = self.library.codecs.nal_payload(unit)
            if not nal or nal[0] & 0x80:
                continue
            if self.codec == "h264":
                kind = nal[0] & 0x1f
                minimum = {7:5, 8:2, 5:2}.get(kind, 2)
            else:
                if len(nal) < 2 or not nal[1] & 7:
                    continue
                kind = (nal[0] >> 1) & 0x3f
                minimum = {32:6, 33:15, 34:3}.get(kind, 3)
            if len(nal) >= minimum and any(nal[1:] if self.codec == "h264" else nal[2:]):
                units.append(unit)
        return b"".join(units)

    def feed(self, data: bytes) -> bool:
        lib = self.library
        frame_limit = MAX_STREAM_FRAME_BYTES if self.frame_output is not None else MAX_FRAME_BYTES
        if len(data) > frame_limit:
            raise ValueError("frame_limit")
        if data.startswith(b"\xff\x01"):
            iva = lib.parse_iva_frame(data)
            if not iva or iva[0] == 0x7012:
                return False
            data = iva[3]
        # The ordinary video splitter recognizes FC/FD/FA but not native FE
        # alternate frames. Accept an FE message only for its playback end
        # notification below; it never becomes encoded video output.
        chunks = [data] if data.startswith(b"\x00\x00\x01\xfe") else lib.codec.split_stream_frames(data)
        for chunk in chunks:
            if len(chunk) > frame_limit or len(chunk) < 4:
                continue
            if self.playback_requested and chunk[3] in (0xFC, 0xFD, 0xFA, 0xFE):
                type_offset, format_offset = (23, 28) if chunk[3] in (0xFC, 0xFE) else (15, 20)
                header_size = 60 if chunk[3] in (0xFC, 0xFE) else 52
                # Native checks 0xffb before validating live/playback mode.
                # End notifications may leave mode unspecified; require an
                # acknowledged request and the complete p2p2 extended header.
                if (self.playback_acknowledged and len(chunk) >= header_size
                        and struct.unpack_from("<I", chunk, format_offset)[0] == 0xFFB):
                    self.playback_end_received = True
                    return False
                if len(chunk) <= type_offset or chunk[type_offset] != 2:
                    # Ordinary media must still be playback type2, before
                    # DES3 decryption, so delayed live frames cannot pass.
                    continue
            if chunk[3] not in (0xFC, 0xFD):
                continue
            options = []
            for candidate in (chunk, bytes(lib.codec.decrypt_stream_frame(bytearray(chunk)))):
                parsed = lib.codec.parse_stream_frame(candidate)
                if parsed and parsed.payload.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
                    identified = lib.codecs.identify_codec(parsed.payload)
                    if identified is None:
                        # Some devices send each parameter set separately.
                        # The reference scoring needs multiple NALs in one
                        # frame, so recognize unique parameter headers here.
                        for unit in lib.codecs.annexb_units(parsed.payload)[:64]:
                            nal = lib.codecs.nal_payload(unit)
                            if nal and not nal[0] & 0x80:
                                h264_kind = nal[0] & 0x1f
                                if h264_kind in (7, 8) and len(nal) >= {7:5, 8:2}[h264_kind]:
                                    identified = lib.codecs.CodecName.H264
                                elif len(nal) >= 3 and not nal[0] & 1 and not nal[1] & 0xf8 and nal[1] & 7:
                                    # Ordinary HEVC parameter sets use layer 0.
                                    # H264 delta slices beginning 0x41 must not
                                    # be mistaken for an isolated HEVC VPS.
                                    kind = (nal[0] >> 1) & 0x3f
                                    if len(nal) >= {32:6, 33:15, 34:3}.get(kind, frame_limit + 1):
                                        identified = lib.codecs.CodecName.HEVC
                            if identified is not None:
                                break
                    options.append((identified is not None, identified, parsed))
            if not options:
                continue
            _, identified, parsed = max(options, key=lambda item: item[0])
            self.video_frames += 1
            self.video_bytes += len(parsed.payload)
            max_frames = MAX_STREAM_FRAMES if self.frame_output is not None else 256
            max_bytes = MAX_STREAM_MEDIA_BYTES if self.frame_output is not None else MAX_MEDIA_BYTES
            if self.video_frames > max_frames or self.video_bytes > max_bytes:
                raise ValueError("media_limit")
            if identified is not None:
                if self.codec != "unknown" and self.codec != identified.value and self.frame_output is not None:
                    # A decoder process consumes one codec for its lifetime.
                    # Close rather than feed a different bitstream into it.
                    raise ValueError("codec_changed")
                self.codec = identified.value
            if self.codec not in ("h264", "hevc"):
                continue
            checked = self.plausible_nals(parsed.payload)
            self.parameters.update(self.codec, checked)
            self.parameter_sets_received = self.parameters.ready(self.codec)
            # Do not treat the vendor I-frame label alone as a keyframe.
            is_keyframe = parsed.frame_type == 0xFC and lib.codecs.is_keyframe(self.codec, checked)
            if is_keyframe and self.parameter_sets_received:
                self.keyframe_received = True
                if self.frame_output is None:
                    return True
            if self.frame_output is not None and checked and self.keyframe_received:
                # Parameter-only frames prime the cache without incrementing
                # the decoder clock. VCL access units begin at random access.
                kinds = lib.codecs.nal_types(self.codec, checked)
                has_picture = any(1 <= kind <= 5 for kind in kinds) if self.codec == "h264" else any(kind <= 31 for kind in kinds)
                if not has_picture:
                    continue
                recovery = is_keyframe and self.parameter_sets_received
                if self.sequence.should_drop(parsed.sequence, recovery=recovery):
                    continue
                output = self.parameters.with_params(self.codec, checked) if recovery else checked
                if len(output) > MAX_STREAM_FRAME_BYTES:
                    raise ValueError("frame_limit")
                self.frame_output(output, self.summary())
                self.last_frame_at = time.monotonic()
        return False


def _vvp(lib: SimpleNamespace, context: dict, command: int, sequence: int) -> bytes:
    """Use explicit native auth choices, never the reference length heuristic."""
    packet = bytearray(lib.protocol.build_vvp_packet(
        command, sequence, context["device"]["hostKey"],
        channel=context["channel_id"], stream_id=0, stream_flag=0,
    ))
    # Native auth finalizes the initially zero header word as network-order 1.
    host_key = context["device"]["hostKey"]
    password = host_key if context["vvp_auth"]["full_password"] else host_key[:16]
    parts = ["admin", password, str(lib.protocol.VVP_MAGIC), str(sequence), str(command), "8", "meari.p2p.ppcs"]
    if context["vvp_auth"]["include_licence"]:
        parts.append(lib.protocol.format_licence_id(context["device"]["snNum"]))
    packet[0x10:0x30] = hashlib.md5("|".join(parts).encode()).hexdigest().encode("ascii")
    return bytes(packet)


def _candidates(values: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            address = ipaddress.IPv4Address(value.get("ip", ""))
            port = int(value.get("port", 0))
        except (ValueError, TypeError, ipaddress.AddressValueError):
            continue
        if address.is_unspecified or address.is_multicast or address.is_loopback or not 1 <= port <= 65535:
            continue
        endpoint = (str(address), port)
        if endpoint not in seen:
            seen.add(endpoint)
            result.append({"ip": endpoint[0], "port": port, "type": value.get("type", "relay")})
            if len(result) >= MAX_CANDIDATES:
                break
    return result


def _bounded_kcp(kcp, frame_limit: int = MAX_FRAME_BYTES) -> None:
    if len(kcp.recv_buf) > 4096 or len(kcp.recv_queue) > 256 or len(kcp.recv_frag_buf) > 4096:
        raise ValueError("kcp_limit")
    if sum(len(item) for item in kcp.recv_frag_buf) > frame_limit:
        raise ValueError("kcp_limit")
    if sum(len(item[1]) for item in kcp.recv_buf.values()) > 4 * frame_limit:
        raise ValueError("kcp_limit")


def stream_probe(lib: SimpleNamespace, context: dict, sig, turn, device_uuid: str, observation: Observation, emit, *, stream_deadline=None) -> str:
    lib.budget.remaining()
    emit("sdp_offer")
    ufrag, password = os.urandom(4).hex(), os.urandom(12).hex()
    sdp = "\n".join([
        "n=0 0 0 0 0", "a=transport:auto", f"a=ice-ufrag:{ufrag}",
        f"a=ice-pwd:{password}", f"m=audio {turn.relay_port} RTP / AVP 0",
        f"c=IN IP4 {turn.relay_ip}", "",
    ])
    answer = sig.send_offer(device_uuid, sdp)
    remote_ufrag, remote_password, values = lib.parse_sdp_answer(answer.get("sdp", ""))
    # A short, bounded trickle window, followed by completion, matches the
    # reference remote path without any relay-probe or wake thread.
    old_timeout = sig.sock.gettimeout()
    try:
        sig.sock.settimeout(lib.budget.remaining(0.25))
        for _ in range(8):
            try:
                extra = sig.recv_webrtc_content()
            except socket.timeout:
                break
            if isinstance(extra, dict):
                values.extend(lib.candidates_from_response(extra))
    finally:
        sig.sock.settimeout(lib.budget.remaining(old_timeout or 3.0))
    complete = sig.send_candidate_complete(device_uuid)
    if isinstance(complete, dict):
        values.extend(lib.candidates_from_response(complete))
    candidates = _candidates(values)
    if not candidates or not remote_ufrag or not remote_password:
        return "probe_failed"
    emit("ice_connect")
    for candidate in candidates:
        lib.budget.remaining()
        turn.create_permission(candidate["ip"])
        turn.channel_bind(candidate["ip"], candidate["port"])
    peer = None

    def send_udp(data: bytes) -> None:
        lib.budget.remaining()
        if peer is not None:
            turn.send_to_peer(peer[0], peer[1], data)
        else:
            for candidate in candidates:
                turn.send_to_peer(candidate["ip"], candidate["port"], data)

    kcp = lib.KcpTunnel(send_udp)
    sequence = 0
    hdd = _hdd_protocol() if context.get("hdd") else None
    operation = context.get("hdd", {}).get("operation")
    search = operation in ("month", "day")
    playback = operation == "playback"
    observation.playback_requested = playback
    reply = None
    request_deadline = None

    def send_vvp(command: int) -> None:
        nonlocal sequence
        kcp.send_iva_data(_vvp(lib, context, command, sequence))
        sequence += 1

    emit("kcp_connect")
    started = False
    try:
        kcp.send_handshake()
        # Native sends ordinary commands on the established IVA connection.
        # A HDD connection must not also open a live-preview stream.
        if hdd is not None:
            emit("hdd_search_start" if search else "hdd_playback_start")
            reply = hdd.ReplyBuffer(sequence)
            command = hdd.search_packet(context, sequence) if search else hdd.playback_packet(context, sequence)
            kcp.send_iva_data(command)
            sequence += 1
            request_deadline = time.monotonic() + 10
        else:
            send_vvp(lib.protocol.VVP_CMD_START_LIVE)
        started = True
        emit("receiving_video")
        now = time.monotonic()
        next_ice = now
        next_heartbeat = now + 5
        next_signal = now + max(5, min(float(sig.heartbeat_interval_s) * 2 / 3, 20))
        next_retransmit = now + 0.5
        next_turn_refresh = now + TURN_REFRESH_SECONDS
        continuous_started = False
        while True:
            now = time.monotonic()
            if request_deadline is not None and now >= request_deadline:
                return "hdd_search_timeout" if search else "hdd_playback_timeout"
            if stream_deadline is not None and continuous_started:
                if now >= stream_deadline:
                    return "stream_complete"
                if observation.last_frame_at is not None and now - observation.last_frame_at >= STREAM_IDLE_SECONDS:
                    return "stream_idle_timeout"
            lib.budget.remaining()
            if now >= next_ice:
                for candidate in candidates:
                    if candidate["type"] != "relay":
                        turn.send_ice_binding(candidate["ip"], candidate["port"], ufrag, remote_ufrag, remote_password)
                next_ice = now + (0.25 if peer is None else 10)
            if now >= next_heartbeat:
                kcp.send_handshake()
                if request_deadline is None:
                    send_vvp(lib.protocol.VVP_CMD_HEARTBEAT)
                next_heartbeat = now + 5
            if now >= next_signal:
                sig.send_heartbeat(read_response=False)
                next_signal = now + max(5, min(float(sig.heartbeat_interval_s) * 2 / 3, 20))
            if now >= next_retransmit:
                kcp.retransmit_unacked()
                kcp.send_gap_nudge()
                next_retransmit = now + 0.5
            if stream_deadline is not None and continuous_started and now >= next_turn_refresh:
                # TURN permissions and channel bindings expire independently
                # of signaling/VVP heartbeats. Renew using the same thread and
                # the existing bounded transactions; KCP recovers any packets
                # arriving while a TURN control response is being matched.
                if not turn.refresh():
                    return "probe_failed"
                endpoints = [peer] if peer is not None else [(c["ip"], c["port"]) for c in candidates]
                for endpoint in endpoints:
                    if not turn.channel_bind(endpoint[0], endpoint[1]):
                        return "probe_failed"
                next_turn_refresh = time.monotonic() + TURN_REFRESH_SECONDS
            packets = lib.recv_peer_packets(turn, timeout=lib.budget.remaining(0.02), max_packets=128)
            for packet in packets:
                lib.budget.remaining()
                observation.received_packets += 1
                observation.received_bytes += len(packet.data)
                packet_limit = MAX_STREAM_PACKETS if stream_deadline is not None else MAX_PACKETS
                media_limit = MAX_STREAM_MEDIA_BYTES if stream_deadline is not None else MAX_MEDIA_BYTES
                if observation.received_packets > packet_limit or observation.received_bytes > media_limit:
                    return "no_video_frame"
                endpoint = packet.peer or packet.source
                if packet.stun:
                    if packet.stun["type"] == lib.BINDING_REQUEST and packet.peer:
                        turn.send_to_peer(endpoint[0], endpoint[1], lib._build_ice_response(packet.stun, password, endpoint[0], endpoint[1]))
                    elif packet.stun["type"] == lib.BINDING_RESPONSE:
                        kcp.retransmit_unacked()
                    continue
                # A TURN-wrapped peer or a declared candidate is required.
                if not packet.via_turn and not any((c["ip"], c["port"]) == endpoint for c in candidates):
                    continue
                if lib.parse_kcp_segments(packet.data) or packet.data.startswith(b"\xff\x01"):
                    peer = endpoint
                response = kcp.process_input(packet.data)
                _bounded_kcp(kcp, MAX_STREAM_FRAME_BYTES if stream_deadline is not None else MAX_FRAME_BYTES)
                payloads = []
                if response and response[0] in ("data", "iva_data", "iva") and response[1]:
                    payloads.append(response[1])
                for _ in range(256):
                    queued = kcp.poll_data()
                    if queued is None:
                        break
                    payloads.append(queued)
                for payload in payloads:
                    if hdd is not None:
                        if payload.startswith(b"\xff\x01"):
                            iva = lib.parse_iva_frame(payload)
                            if not iva or iva[0] == 0x7012:
                                continue
                            payload = iva[3]
                        is_media = payload.startswith(b"\x00\x00\x01")
                        if reply is not None and not is_media:
                            body = reply.feed(payload)
                            if body is not None:
                                if search:
                                    observation.hdd_result = hdd.parse_search_reply(body, context["hdd"])
                                    return "hdd_search_received"
                                observation.playback_acknowledged = True
                                emit("hdd_playback_acknowledged")
                                reply, request_deadline = None, None
                            continue
                        if search or not observation.playback_acknowledged:
                            continue
                    if observation.feed(payload):
                        return "video_keyframe_received"
                    if playback and observation.playback_end_received:
                        emit("hdd_playback_ended")
                        return "stream_complete"
                    if stream_deadline is not None and observation.last_frame_at is not None and not continuous_started:
                        continuous_started = True
                        # Discovery and first-keyframe remain under the short
                        # startup budget. Only verified media earns a session.
                        lib.budget.configure(max(0, stream_deadline - time.monotonic()))
                if kcp.ack_flush_due():
                    kcp.flush_acks()
            kcp.flush_acks()
    except TimeoutError:
        if request_deadline is not None:
            return "hdd_search_timeout" if search else "hdd_playback_timeout"
        if stream_deadline is not None and observation.last_frame_at is not None:
            return "stream_complete" if time.monotonic() >= stream_deadline else "probe_timeout"
        return "no_video_frame" if started else "probe_timeout"
    finally:
        if started:
            # Same-thread best effort; no wait for a device acknowledgement.
            try:
                if playback:
                    kcp.send_iva_data(hdd.playback_packet(context, sequence, stop=True))
                elif not search:
                    send_vvp(lib.protocol.VVP_CMD_STOP)
            except (OSError, ValueError, RuntimeError):
                pass


def run_probe(context: dict, emit, *, library=None, frame_output=None) -> tuple[str, dict]:
    lib = library or load_protocol()
    lib.budget.configure(float(context["timeout_seconds"]) - 2)
    stream_deadline = time.monotonic() + MAX_STREAM_SECONDS - 2 if frame_output is not None else None
    observation = Observation(lib, frame_output)
    account, device = context["account"], context["device"]
    # SDK iotType=3 always derives uuid from SN, overriding deviceUUID.
    serial = device["snNum"]
    device_uuid = "0000000" + serial if len(serial) == 9 else serial[4:]
    sig = turn = None
    outcome = "probe_failed"
    try:
        emit("root_discovery")
        candidates = lib.discover_msgsvr_endpoints(
            platform_domain_hint=account.get("platform_domain"),
            openapi_server_hint=account["openapi_server"],
            api_server_hint=account["api_server"], client_id_hint=None,
        )
        for ip, port in candidates[:4]:
            lib.budget.remaining()
            emit("signaling_connect")
            try:
                sig = lib.MsgSvrClient(ip, port, session_index=1)
                sig.connect(timeout_s=3)
                sig.register(client_id=str(account["user_id"]), brand=str(account["brand"]), app_ver=account["app_version"], country=account["country_code"], client_uuid=None)
                emit("signaling_registered")
                sig.webrtc_hello_full()
                emit("device_status")
                status = sig.query_device_status(device_uuid)
                if not isinstance(status, dict) or status.get("status") != "online":
                    outcome = "device_unavailable"
                    continue
                emit("turn_allocate")
                relay = sig.request_coturn(device_uuid)
                if not isinstance(relay, dict) or not relay.get("coturn_ip"):
                    continue
                turn = lib.TurnClient(relay["coturn_ip"], int(relay.get("coturn_port", 9100)), relay.get("username", ""), relay.get("pwd", ""))
                turn.connect()
                if not turn.allocate():
                    continue
                outcome = stream_probe(lib, context, sig, turn, device_uuid, observation, emit, stream_deadline=stream_deadline)
                break
            except OutputClosed:
                raise
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, struct.error):
                lib.budget.remaining()
                if context.get("hdd"):
                    return "hdd_search_failed" if context["hdd"]["operation"] in ("month", "day") else "probe_failed", observation.summary()
                if observation.last_frame_at is not None:
                    # An active bitstream must not silently restart and emit
                    # another codec or timestamp epoch into its consumer.
                    return "probe_failed", observation.summary()
            finally:
                if turn is not None:
                    turn.close()
                    turn = None
                if sig is not None:
                    # Never send logout for a device that was not offered.
                    sig.close()
                    sig = None
        return outcome, observation.summary()
    except OutputClosed:
        raise
    except TimeoutError:
        return "probe_timeout", observation.summary()
    except Exception:
        # Never serialize exception strings, request data or vendor diagnostics.
        return "probe_failed", observation.summary()
    finally:
        if turn is not None:
            turn.close()
        if sig is not None:
            sig.close()


def main() -> int:
    global _DEPENDENCY_ROOT
    logging.disable(sys.maxsize)
    output = sys.stdout
    binary_output = BinaryOutput(output.buffer) if "--stream" in sys.argv[1:] else None
    with open(os.devnull, "w", encoding="utf-8") as sink:
        # fd-level stderr closes the uncaught-exception/native diagnostics path.
        os.dup2(sink.fileno(), 2)
        sys.stderr = sink
        count = 0

        def emit(stage: str, *, final=False, summary=None) -> None:
            nonlocal count
            if stage not in STAGES or count >= 64:
                return
            count += 1
            record = {"probe_stage": stage, "final": bool(final), "decoded": False}
            if summary:
                for key in ("video_frames", "video_bytes", "received_packets"):
                    value = summary.get(key)
                    if type(value) is int and 0 <= value <= 2**31 - 1:
                        record[key] = value
                record["codec"] = summary.get("codec") if summary.get("codec") in ("h264", "hevc") else "unknown"
                for key in ("keyframe_received", "parameter_sets_received"):
                    record[key] = summary.get(key) is True
                for key in ("playback_requested", "playback_acknowledged", "playback_end_received"):
                    if key in summary:
                        record[key] = summary[key] is True
                if stage == "hdd_search_received":
                    for key in ("hdd_operation", "calendar_date", "recording_days", "fragments", "recording_count", "fragments_truncated", "invalid_fragment_count"):
                        if key in summary:
                            record[key] = summary[key]
            body = json.dumps(record, separators=(",", ":")).encode("ascii")
            if binary_output is not None:
                binary_output.write(b"J", body)
            else:
                output.write(body.decode("ascii") + "\n")
                output.flush()

        first_video = True

        def emit_video(payload: bytes, summary: dict) -> None:
            nonlocal first_video
            if first_video:
                emit("video_keyframe_received", summary=summary)
                first_video = False
            binary_output.write(b"V", payload)

        with contextlib.redirect_stdout(sink):
            emit("worker_started")
            try:
                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("--dependency-root")
                parser.add_argument("--stream", action="store_true")
                options = parser.parse_args()
                if options.dependency_root:
                    dependency_root = Path(options.dependency_root)
                    if not dependency_root.is_absolute() or not (dependency_root / "Crypto" / "__init__.py").is_file() or not (dependency_root / "Crypto" / "Cipher").is_dir():
                        raise ValueError("invalid_context")
                    _DEPENDENCY_ROOT = str(dependency_root.resolve())
                body = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
                if len(body) > MAX_INPUT_BYTES:
                    raise ValueError("invalid_context")
                context = validate_context(json.loads(body))
                if options.stream and context.get("hdd", {}).get("operation") in ("month", "day"):
                    raise ValueError("invalid_context")
                if not options.stream and context.get("hdd", {}).get("operation") == "playback":
                    raise ValueError("invalid_context")
            except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
                emit("invalid_context", final=True)
                return 0
            try:
                stage, summary = run_probe(context, emit, frame_output=emit_video if options.stream else None)
                emit(stage, final=True, summary=summary)
            except OutputClosed:
                return 0
            except ImportError:
                emit("dependency_missing", final=True)
            except Exception:
                emit("probe_failed", final=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
