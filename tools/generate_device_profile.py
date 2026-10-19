#!/usr/bin/env python3
"""生成自定义设备 Profile 骨架：从真实抓包得到 match / state / control 模板。

用户拿到的是"这个 deviceType 是多少、状态在哪个字段、控制大概发什么"的答案，
而不是一堆原始报文。

用法::

    # 1. 列出家庭内所有设备，看哪些还没有内置支持
    python tools/generate_device_profile.py <账号> <密码> --list

    # 2. 选中一台，边监听边在 App/物理开关上操作，然后生成 profile
    python tools/generate_device_profile.py <账号> <密码> \\
        --device 客厅插座 --listen 90 --out my_plug.yaml

    # 3. 已有一份 readtable JSON 时，完全离线生成
    python tools/generate_device_profile.py --from-json readtable.json \\
        --device 客厅插座 --observations samples.json

参数从哪来
----------
* ``match`` 字段（deviceType / subDeviceType / classId / uiModel / model）
  来自云端 readtable 快照，脚本直接填好。
* ``state`` 的字段路径来自监听期间捕获的 ``cmd=42`` 推送（Snapshot+Pushes）。
* ``control`` 只能给骨架：``order`` 一定落在已真机验证的白名单内，
  但 value1..4 / properties 的语义必须你对照抓包确认。

脚本**不会**把 ``hardware_verified`` 写成 true，也不会替你判断 active-low/high、
亮度量纲、色温单位——这些只能真机确认，会列在生成文件顶部的待办清单里。

安全说明：本脚本只读取你账号下的设备列表和状态推送，不发送任何控制命令，
也不会修改仓库里的任何文件。凭据建议用环境变量传入（见 --help）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Optional

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
COMPONENT_DIR = PROJECT_ROOT / "custom_components" / "orvibohomebridge"
sys.path.insert(0, str(TOOLS_DIR))

import profile_skeleton as skeleton  # noqa: E402  (path set above)

# Import the pure integration modules through the non-destructive shim.
const = skeleton._pure_module("const")
packet_mod = skeleton._pure_module("packet")
protocol = skeleton._pure_module("protocol")
device_types = skeleton._pure_module("device_types")
capabilities = skeleton._pure_module("capabilities")

import aiohttp  # noqa: E402

_LOGGER = logging.getLogger("generate_device_profile")


# ── cloud HTTP (read-only: login + readtable) ─────────────────────────────────

class CloudClient:
    """Minimal HTTPS client: login and readtable only."""

    def __init__(self, username: str, password: str, host: Optional[str] = None):
        self.username = username
        self.password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
        self.host = host or const.HTTPS_HOST
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.family_id: Optional[str] = None
        self.family_name: str = ""
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_or_new(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def login(self) -> bool:
        session = await self._session_or_new()
        url = (
            f"https://{self.host}/getOauthToken"
            f"?userName={self.username}&type=0&password={self.password_md5}"
        )
        async with session.get(
            url, headers={**const.HTTP_HEADERS, "Accept": "*/*"}, ssl=False
        ) as response:
            payload = json.loads(await response.text())
        if payload.get("status") == 0:
            self.token = payload["data"]["access_token"]
            self.user_id = payload["data"]["user_id"]
            return True
        _LOGGER.error("登录失败：%s", payload.get("message") or payload.get("status"))
        return False

    async def families(self) -> list[dict[str, Any]]:
        request = packet_mod.HomemateJsonData.get_family_statistics_users(
            self.user_id, self.token, api_host=self.host
        )
        session = await self._session_or_new()
        async with session.post(
            request["url"], data=request["data"], headers=const.HTTP_HEADERS, ssl=False
        ) as response:
            payload = await response.json()
        families = payload.get("data", [])
        if isinstance(families, list):
            for family in families:
                if self.family_id is None:
                    self.family_id = family.get("familyId", "")
                    self.family_name = family.get("familyName", "")
            return families
        return []

    async def readtable(self, family_id: str) -> list[dict[str, Any]]:
        """Read the raw readtable device records for one family."""

        session = await self._session_or_new()
        for device_flag in (1, 0):
            request = packet_mod.HomemateJsonData.get_devices_status(
                self.token,
                "",
                self.user_id,
                self.username,
                family_id,
                device_flag=device_flag,
                api_host=self.host,
            )
            async with session.post(
                request["url"], data=request["data"], headers=const.HTTP_HEADERS, ssl=False
            ) as response:
                payload = await response.json()
            data = payload.get("data", {})
            devices = data.get("device", []) or []
            if isinstance(devices, dict):
                devices = [
                    {"deviceId": key, **(value if isinstance(value, dict) else {})}
                    for key, value in devices.items()
                ]
            statuses = data.get("deviceStatus", []) or []
            if isinstance(statuses, dict):
                statuses = list(statuses.values())
            by_id = {
                item.get("deviceId", ""): item
                for item in statuses
                if isinstance(item, dict) and item.get("deviceId")
            }
            for device in devices:
                status = by_id.get(device.get("deviceId", ""))
                if status:
                    merged = dict(device)
                    merged.update(status)
                    properties = device.get("properties")
                    status_properties = status.get("properties")
                    if isinstance(properties, dict) and isinstance(status_properties, dict):
                        merged["properties"] = {**properties, **status_properties}
                    device.clear()
                    device.update(merged)
            if devices:
                return [item for item in devices if isinstance(item, dict)]
        return []


# ── cloud push listener (read-only) ───────────────────────────────────────────

class PushListener:
    """Connect to the ORVIBO realtime channel and collect cmd=42 state pushes."""

    def __init__(self, username: str, password: str, family_id: str, host: Optional[str] = None):
        self.username = username
        self.password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
        self.family_id = family_id
        self.host = host or const.SSL_HOST
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.session_id: Optional[str] = None
        self.session_key: Optional[bytes] = None

    async def connect(self) -> bool:
        cert_dir = COMPONENT_DIR / "certs"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(
            certfile=str(cert_dir / "client_cert.pem"),
            keyfile=str(cert_dir / "client_key.pem"),
        )
        context.load_verify_locations(cafile=str(cert_dir / "server_ca.pem"))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.host, const.SSL_PORT, ssl=context, server_hostname=self.host
            ),
            timeout=20,
        )
        await self._send(packet_mod.HomemateJsonData.ssl_get_session(), const.DEFAULT_KEY.encode())
        hello = await self._read_packet()
        if not isinstance(hello, dict) or hello.get("cmd") != const.CMD_HELLO:
            _LOGGER.error("未收到 HELLO 响应，无法建立实时通道")
            return False
        key = hello.get("key")
        if not key:
            _LOGGER.error("HELLO 响应缺少会话密钥")
            return False
        self.session_key = str(key).encode()

        await self._send(
            packet_mod.HomemateJsonData.ssl_login(
                self.username, self.password_md5, self.family_id
            ),
            self.session_key,
        )
        await asyncio.sleep(2.0)
        await self._send(
            {
                "cmd": const.CMD_GET_DEVICE_LIST,
                "familyId": self.family_id,
                "serial": packet_mod.generate_serial(),
                "clientType": 1,
                "uniSerial": packet_mod.generate_serial(use_time=True),
                "ver": const.SOFTWARE_VER,
                "debugInfo": const.DEBUG_INFO,
            },
            self.session_key,
        )
        await asyncio.sleep(1.0)
        return True

    async def _read_packet(self) -> Optional[dict[str, Any]]:
        assert self.reader is not None
        header = await asyncio.wait_for(self.reader.readexactly(42), timeout=15)
        length = packet_mod.HomematePacket.parse_length(header)
        body = await asyncio.wait_for(self.reader.readexactly(length - 42), timeout=15)
        packet = packet_mod.HomematePacket(header + body, {"": const.DEFAULT_KEY.encode()})
        raw_session = bytes(packet.session_id)
        keys = {raw_session: self.session_key or const.DEFAULT_KEY.encode()}
        return packet_mod.HomematePacket(header + body, keys).json_payload

    async def _send(self, payload: dict[str, Any], key: bytes) -> None:
        assert self.writer is not None
        if key == const.DEFAULT_KEY.encode():
            packet_type = bytes([0x70, 0x6B])
            session_id = bytes(const.ID_UNSET)
        else:
            packet_type = bytes([0x64, 0x6B])
            session_id = (
                (self.session_id or bytes(const.ID_UNSET).decode()).encode()
                if isinstance(self.session_id, str)
                else bytes(const.ID_UNSET)
            )
        self.writer.write(
            packet_mod.HomematePacket.build_packet(packet_type, key, session_id, payload)
        )
        await self.writer.drain()

    async def close(self) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def collect(
        self, device_ids: set[str], duration: float, on_sample=None
    ) -> skeleton.Observations:
        """Read pushes until ``duration`` elapses, keeping this device's payloads."""

        observations = skeleton.Observations()
        deadline = time.monotonic() + duration
        last_heartbeat = time.monotonic()
        _LOGGER.info(
            "开始监听 %.0f 秒：请在 App 或物理开关上操作设备（开/关/调亮度/调色温）",
            duration,
        )
        while time.monotonic() < deadline:
            remaining = max(1.0, deadline - time.monotonic())
            try:
                payload = await asyncio.wait_for(self._read_packet(), timeout=min(10.0, remaining))
            except asyncio.TimeoutError:
                payload = None
            except Exception as error:  # noqa: BLE001
                _LOGGER.debug("读取推送失败：%s", type(error).__name__)
                break

            if time.monotonic() - last_heartbeat > 20:
                try:
                    await self._send(
                        {"cmd": const.CMD_HEARTBEAT, "serial": packet_mod.generate_serial()},
                        self.session_key or const.DEFAULT_KEY.encode(),
                    )
                except Exception:  # noqa: BLE001
                    pass
                last_heartbeat = time.monotonic()

            if not isinstance(payload, dict):
                continue
            device_id = _match_device_id(payload, device_ids)
            if device_id is None:
                continue
            body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            observations.add_payload(body)
            if on_sample is not None:
                on_sample(body)
        return observations


def _match_device_id(payload: dict[str, Any], device_ids: set[str]) -> Optional[str]:
    """Return the target device id if this packet belongs to it."""

    for candidate in (
        payload.get("deviceId"),
        payload.get("device_id"),
        (payload.get("data") or {}).get("deviceId") if isinstance(payload.get("data"), dict) else None,
    ):
        if isinstance(candidate, str) and candidate:
            if candidate in device_ids:
                return candidate
            for known in device_ids:
                if known.startswith("w-") and known[2:] == candidate:
                    return known
    return None


# ── device presentation / selection ───────────────────────────────────────────

def describe(device: dict[str, Any], match_mode: str = "minimal") -> tuple[str, str, str, str]:
    """Return (name, match summary, category, platform) for the list view."""

    name = str(device.get("deviceName") or device.get("name") or "?")
    normalized = {
        "device_id": device.get("deviceId", ""),
        "device_type_raw": _int(device.get("deviceType")),
        "sub_device_type": _int(device.get("subDeviceType")),
        "class_id": _int(device.get("classId")),
        "model": device.get("model") or "",
        "ui_model": (device.get("ui") or {}).get("model", "")
        if isinstance(device.get("ui"), dict)
        else device.get("ui_model", ""),
        "properties": device.get("properties") or {},
    }
    try:
        profile = device_types.get_device_profile(normalized)
        category = profile.info.label or profile.category.value
        builtin = "unknown" if profile.category.value in ("unknown", "other") else profile.category.value
    except Exception:  # noqa: BLE001
        category, builtin = "?", "unknown"
    try:
        platform = "、".join(sorted(capabilities.capability_for(normalized).platforms)) or "-"
    except Exception:  # noqa: BLE001
        platform = "-"
    match = skeleton.build_match(device, match_mode)
    summary = " ".join(f"{key}={value}" for key, value in match.items()) or "-"
    return name, summary, f"{category}({builtin})" if builtin != category else category, platform


def select_device(devices: list[dict[str, Any]], wanted: str) -> Optional[dict[str, Any]]:
    """Match ``wanted`` against device name / id, case-insensitively and by substring."""

    exact = [
        device
        for device in devices
        if wanted in (str(device.get("deviceName") or ""), str(device.get("deviceId") or ""))
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        device
        for device in devices
        if wanted.lower() in str(device.get("deviceName") or device.get("deviceId") or "").lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        _LOGGER.error("“%s”匹配到多台设备，请用更精确的名字或 deviceId：", wanted)
        for device in partial:
            _LOGGER.error("  - %s (%s)", device.get("deviceName"), device.get("deviceId"))
    return None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成自定义设备 profile 骨架（只读：不改任何文件，只生成你指定的输出）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "凭据也可以用环境变量：ORVIBO_USERNAME / ORVIBO_PASSWORD\n"
            "示例：\n"
            "  python tools/generate_device_profile.py $env:ORVIBO_USERNAME $env:ORVIBO_PASSWORD --list\n"
            "  python tools/generate_device_profile.py <账号> <密码> --device 客厅插座 --listen 90 --out my_plug.yaml\n"
        ),
    )
    parser.add_argument("username", nargs="?", default=os.environ.get("ORVIBO_USERNAME"))
    parser.add_argument("password", nargs="?", default=os.environ.get("ORVIBO_PASSWORD"))
    parser.add_argument("--list", action="store_true", help="列出所有设备及其 match 字段")
    parser.add_argument("--device", help="目标设备（名称或 deviceId，支持子串）")
    parser.add_argument("--listen", type=float, default=0.0, help="监听秒数（默认 0=不监听，仅用快照）")
    parser.add_argument("--out", help="输出 profile 文件路径（默认打印到标准输出）")
    parser.add_argument("--platform", help="覆盖平台推断（light/switch/cover/sensor/binary_sensor）")
    parser.add_argument(
        "--match",
        dest="match_mode",
        choices=("minimal", "full", "type"),
        default="minimal",
        help=(
            "match 条件的松紧：minimal=device_type+sub_device_type（默认，最稳）；"
            "full=所有标识字段（最精确但字段一变就失效）；type=只用 device_type"
        ),
    )
    parser.add_argument("--profile-id", help="指定 profile id（默认按设备名生成）")
    parser.add_argument("--family", help="家庭索引（多家庭账号；默认 0）")
    parser.add_argument("--cloud-host", help="云端域名（国际区用 homemate.orvibo.com）")
    parser.add_argument("--show-paths", action="store_true", help="额外打印所有观察到的字段路径")
    parser.add_argument("--from-json", help="离线模式：直接读 readtable JSON 文件，不登录")
    parser.add_argument(
        "--observations",
        help="离线模式：读一份此前 --capture 保存的观测 JSON（{路径: [值]} 或原始 payload 列表）",
    )
    parser.add_argument("--capture", help="把本次观察到的原始 payload 存成 JSON，便于离线复用")
    parser.add_argument("--verbose", action="store_true", help="打印调试日志")
    return parser


async def run(args: argparse.Namespace) -> int:
    # ---- offline mode -------------------------------------------------------
    if args.from_json:
        devices = _load_json_devices(Path(args.from_json))
        if not devices:
            _LOGGER.error("--from-json 里没有解析到设备记录")
            return 2
        if args.list or not args.device:
            _print_device_table(devices)
            if not args.device:
                print("\n用 --device <名称> 生成指定设备的 profile。")
            return 0
        observations = skeleton.Observations()
        if args.observations:
            _load_observations(Path(args.observations), observations)
        return _emit(args, devices, observations)

    if not args.username or not args.password:
        _LOGGER.error(
            "缺少账号密码：请作为位置参数传入，或设置 ORVIBO_USERNAME / ORVIBO_PASSWORD"
        )
        return 2

    cloud = CloudClient(args.username, args.password, host=args.cloud_host)
    listener: Optional[PushListener] = None
    try:
        if not await cloud.login():
            return 3
        families = await cloud.families()
        if not families:
            _LOGGER.error("该账号下没有家庭")
            return 3
        index = int(args.family or 0)
        if index >= len(families):
            _LOGGER.error("家庭索引 %s 超出范围（共 %s 个）", index, len(families))
            return 3
        family_id = families[index].get("familyId", "")

        devices = await cloud.readtable(family_id)
        if not devices:
            _LOGGER.error("readtable 没有返回设备")
            return 3

        if args.list or not args.device:
            _print_device_table(devices)
            if not args.device:
                print("\n用 --device <名称> --listen 90 生成指定设备的 profile。")
                return 0
            return 0

        target = select_device(devices, args.device)
        if target is None:
            _LOGGER.error("找不到设备“%s”。用 --list 查看可用设备。", args.device)
            return 4

        observations = skeleton.Observations()
        observations.add_snapshot(_normalise_for_snapshot(target))

        if args.listen > 0:
            listener = PushListener(
                args.username, args.password, family_id, host=args.cloud_host
            )
            if not await listener.connect():
                _LOGGER.error("实时通道连接失败，改为只用 readtable 快照生成")
            else:
                device_ids = {str(target.get("deviceId", ""))}
                observed = await listener.collect(
                    device_ids,
                    args.listen,
                    on_sample=lambda body: print(".", end="", flush=True),
                )
                print()
                # merge: keep snapshot evidence and add pushes
                for path, values in observed.values.items():
                    for value in values:
                        observations.add_payload(_path_to_payload(path, value), keep_raw=False)
                for raw in observed.raws:
                    observations.add_payload(raw)
                _LOGGER.info("监听结束：捕获 %s 条目标设备推送", observed.samples)

        if args.capture:
            _save_capture(Path(args.capture), observations)

        return _emit(args, [target], observations)
    finally:
        if listener is not None:
            await listener.close()
        await cloud.close()


def _normalise_for_snapshot(device: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw readtable record like the integration's device dict."""

    return {
        "value1": _int(device.get("value1")),
        "value2": _int(device.get("value2")),
        "value3": _int(device.get("value3")),
        "value4": _int(device.get("value4")),
        "properties": device.get("properties") or {},
    }


def _path_to_payload(path: str, value: Any) -> dict[str, Any]:
    """Rebuild a nested payload from a dotted path (used when merging evidence)."""

    parts = path.split(".")
    payload: Any = value
    for part in reversed(parts):
        payload = {part: payload}
    return payload


def _emit(args: argparse.Namespace, devices: list[dict[str, Any]], observations: skeleton.Observations) -> int:
    target = devices[0] if len(devices) == 1 else select_device(devices, args.device or "")
    if target is None:
        _LOGGER.error("--device 未指定或匹配到多台设备，请明确指定")
        return 4

    # The raw record is what the user's device actually looks like; seed the
    # evidence from it so an offline run still produces a useful state block.
    observations.add_snapshot(target)

    normalized = {
        "device_id": target.get("deviceId", ""),
        "device_type_raw": _int(target.get("deviceType")),
        "sub_device_type": _int(target.get("subDeviceType")),
        "class_id": _int(target.get("classId")),
        "model": target.get("model") or "",
        "ui_model": (target.get("ui") or {}).get("model", "") if isinstance(target.get("ui"), dict) else "",
        "properties": target.get("properties") or {},
    }
    builtin_category, builtin_platform = "", ""
    try:
        profile = device_types.get_device_profile(normalized)
        builtin_category = profile.category.value
        builtin_platform = "、".join(sorted(capabilities.capability_for(normalized).platforms))
    except Exception:  # noqa: BLE001
        pass

    result = skeleton.build_skeleton(
        target,
        observations,
        platform=args.platform,
        profile_id=args.profile_id,
        builtin_category=builtin_category,
        builtin_platform=builtin_platform,
        match_mode=args.match_mode,
    )

    if args.show_paths:
        print("观察到的字段路径：")
        for path in observations.paths():
            values = observations.distinct(path)
            shown = ", ".join(str(item) for item in values[:8])
            print(f"  {path}  ×{observations.count(path)}  值=[{shown}]")
        print()

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(result.yaml_text, encoding="utf-8")
        print(f"已写入 {out_path}")
    else:
        print(result.yaml_text)

    print(skeleton.format_report(result, target))
    print(
        "\n下一步：把文件放到 <HA config>/orvibohomebridge/devices/ 下，"
        "重启或重载集成，然后在“配置 → 自定义设备 Profile”里确认命中设备数。"
    )
    return 0


def _print_device_table(devices: list[dict[str, Any]]) -> None:
    print(f"\n共 {len(devices)} 台设备：\n")
    for index, device in enumerate(devices, 1):
        name, match, category, platform = describe(device)
        print(f"{index:3}. {name}")
        print(f"     match: {match}")
        print(f"     当前识别: {category}   平台: {platform}")
    print()


def _load_json_devices(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: Any = payload
    if isinstance(payload, dict):
        for key in ("device", "devices", "deviceDescList"):
            if isinstance(payload.get(key), (list, dict)):
                records = payload[key]
                break
        else:
            data = payload.get("data")
            if isinstance(data, dict):
                records = data.get("device", data)
            elif isinstance(data, list):
                records = data
    if isinstance(records, dict):
        return [
            {"deviceId": key, **(value if isinstance(value, dict) else {})}
            for key, value in records.items()
        ]
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    return []


def _load_observations(path: Path, observations: skeleton.Observations) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                observations.add_payload(entry, keep_raw=False)
    elif isinstance(payload, dict):
        if "payloads" in payload and isinstance(payload["payloads"], list):
            for entry in payload["payloads"]:
                if isinstance(entry, dict):
                    observations.add_payload(entry, keep_raw=False)
        else:
            for key, value in payload.items():
                values = value if isinstance(value, list) else [value]
                observations.values[key] = list(values)
                observations.counts[key] = len(values)
            observations.samples = max(observations.counts.values(), default=0)
    _LOGGER.info("离线观测载入完成：%s 个路径", len(observations.values))


def _save_capture(path: Path, observations: skeleton.Observations) -> None:
    path.write_text(
        json.dumps({"payloads": observations.raws}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"原始观测已保存到 {path}（含设备 ID，请勿直接提交到公开仓库）")


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    # aiohttp/ssl noise is not useful for end users
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as error:  # noqa: BLE001
        _LOGGER.error("执行失败：%s", error)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
