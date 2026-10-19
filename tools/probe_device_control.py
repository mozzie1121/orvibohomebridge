#!/usr/bin/env python3
"""控制命令实证探针：发出候选命令，靠设备状态反馈判断哪条真的生效。

为什么需要它
------------
``readtable`` 只能告诉你"设备是什么"，实时推送只能告诉你"设备现在怎样"。
**官方 App 发的控制命令两条通道都拿不到明文**：云端 SSL 用会话密钥 AES 加密，
网关 TCP 同样加密，App 自身双向 TLS 且固定证书 —— 被动嗅探拿不到东西。

所以只剩实证路线：把候选命令真的发出去，看设备是否按预期变化。
本工具把仓库里原本"只发不等结果"的探针补成闭环：

    读基线 → 发一条候选 → 在观察窗口内读该设备的状态推送 → 判定生效/无效/无法判断

判定只可能来自设备反馈，**服务端 ACK 不算数**。

安全性
------
* 只对你用 ``--device`` 明确指定的那一台设备发送命令；
* 门锁/晾衣机/窗帘等有机械或安全风险的类别需要二次确认（``--yes`` 跳过，用于非交互）；
* 每条候选之间固定 ``--gap`` 秒，避免连环触发；
* 默认只**读取**当前状态与推送，不修改任何文件；``--out`` 才写你指定的文件。

用法::

    # 看看有哪些设备，以及每台当前的状态字段
    python tools/probe_device_control.py <账号> <密码> --list

    # 验证"开"这个动作：枚举常见约定，逐条发、逐条验
    python tools/probe_device_control.py <账号> <密码> \\
        --device 客厅插座 --intent on --observe 6

    # 用反编译 App 得到的真实帧来验证（最准）
    python tools/probe_device_control.py <账号> <密码> \\
        --device 客厅插座 --payloads candidates.json --intent on
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
from typing import Any, Mapping, Optional

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
COMPONENT_DIR = PROJECT_ROOT / "custom_components" / "orvibohomebridge"
sys.path.insert(0, str(TOOLS_DIR))

import control_probe as probe  # noqa: E402
import profile_skeleton as skeleton  # noqa: E402

const = skeleton._pure_module("const")
packet_mod = skeleton._pure_module("packet")
device_types = skeleton._pure_module("device_types")
capabilities = skeleton._pure_module("capabilities")

import aiohttp  # noqa: E402

_LOGGER = logging.getLogger("probe_device_control")


# ── cloud: readtable (read-only) ──────────────────────────────────────────────

class CloudClient:
    """Login + readtable only; never sends control frames."""

    def __init__(self, username: str, password: str, host: Optional[str] = None):
        self.username = username
        self.password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
        self.host = host or const.HTTPS_HOST
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.family_id: Optional[str] = None
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
            return families
        return []

    async def readtable(self, family_id: str) -> list[dict[str, Any]]:
        session = await self._session_or_new()
        for device_flag in (1, 0):
            request = packet_mod.HomemateJsonData.get_devices_status(
                self.token, "", self.user_id, self.username, family_id,
                device_flag=device_flag, api_host=self.host,
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
                    device.clear()
                    device.update(merged)
            if devices:
                return [item for item in devices if isinstance(item, dict)]
        return []


# ── SSL control session ───────────────────────────────────────────────────────

class ControlSession:
    """One short-lived realtime connection used to send a candidate and observe."""

    def __init__(self, username: str, password: str, family_id: str, host: Optional[str] = None):
        self.username = username
        self.password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
        self.family_id = family_id
        self.host = host or const.SSL_HOST
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.session_id: bytes = bytes(const.ID_UNSET)
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
        hello = await self._recv()
        if not isinstance(hello, dict) or hello.get("cmd") != const.CMD_HELLO:
            return False
        key = hello.get("key")
        if not key:
            return False
        self.session_key = str(key).encode()
        raw_session = hello.get("sessionId")
        if isinstance(raw_session, str) and raw_session:
            self.session_id = raw_session.encode()
        await self._send(
            packet_mod.HomemateJsonData.ssl_login(
                self.username, self.password_md5, self.family_id
            ),
            self.session_key,
        )
        await asyncio.sleep(2.0)
        return True

    async def _send(self, payload: dict[str, Any], key: bytes) -> None:
        assert self.writer is not None
        if key == const.DEFAULT_KEY.encode():
            packet_type = bytes([0x70, 0x6B])
            session_id = bytes(const.ID_UNSET)
        else:
            packet_type = bytes([0x64, 0x6B])
            session_id = self.session_id
        self.writer.write(
            packet_mod.HomematePacket.build_packet(packet_type, key, session_id, payload)
        )
        await self.writer.drain()

    async def _recv(self, timeout: float = 10.0) -> Optional[dict[str, Any]]:
        assert self.reader is not None
        try:
            header = await asyncio.wait_for(self.reader.readexactly(42), timeout=timeout)
            length = packet_mod.HomematePacket.parse_length(header)
            body = await asyncio.wait_for(self.reader.readexactly(length - 42), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None
        keys = {
            bytes(const.ID_UNSET): const.DEFAULT_KEY.encode(),
            self.session_id: self.session_key or const.DEFAULT_KEY.encode(),
        }
        return packet_mod.HomematePacket(header + body, keys).json_payload

    async def send_control(
        self, device_id: str, device_uid: str, candidate: probe.Candidate
    ) -> bool:
        """Send one candidate payload and wait for the server's acknowledgement."""

        payload = {
            "uid": device_uid,
            "userName": self.username,
            "deviceId": device_id,
            "groupId": "",
            **candidate.payload_kwargs(),
            "delayTime": 0,
            "qualityOfService": 1,
            "defaultResponse": 1,
            "propertyResponse": 0,
            "cmd": const.CMD_CONTROL,
            "serial": packet_mod.generate_serial(),
            "clientType": 1,
            "uniSerial": packet_mod.generate_serial(use_time=True),
            "serverRecord": False,
            "ver": const.SOFTWARE_VER,
            "debugInfo": const.DEBUG_INFO,
        }
        await self._send(payload, self.session_key or const.DEFAULT_KEY.encode())
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            response = await self._recv(timeout=3.0)
            if response is None:
                return False
            if response.get("cmd") == const.CMD_CONTROL:
                status = response.get("status")
                return status in (0, "0", None)
        return False

    async def observe(
        self, device_id: str, seconds: float
    ) -> list[dict[str, Any]]:
        """Collect this device's state pushes for ``seconds``."""

        self._subscribe(device_id)
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            remaining = max(0.5, deadline - time.monotonic())
            packet = await self._recv(timeout=min(2.0, remaining))
            if packet is None:
                continue
            body = packet.get("data") if isinstance(packet.get("data"), dict) else packet
            if _belongs_to(body, device_id):
                collected.append(dict(body))
        return collected

    def _subscribe(self, device_id: str) -> None:
        """Ask the cloud to push this device's state (best effort, fire and forget)."""

        payload = {
            "cmd": const.CMD_GET_DEVICE_LIST,
            "familyId": self.family_id,
            "serial": packet_mod.generate_serial(),
            "clientType": 1,
            "uniSerial": packet_mod.generate_serial(use_time=True),
            "ver": const.SOFTWARE_VER,
            "debugInfo": const.DEBUG_INFO,
        }
        assert self.writer is not None
        self.writer.write(
            packet_mod.HomematePacket.build_packet(
                bytes([0x64, 0x6B]),
                self.session_key or const.DEFAULT_KEY.encode(),
                self.session_id,
                payload,
            )
        )

    async def close(self) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


def _belongs_to(payload: Mapping[str, Any], device_id: str) -> bool:
    for key in ("deviceId", "device_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            if value == device_id:
                return True
            if device_id.startswith("w-") and device_id[2:] == value:
                return True
    return False


# ── probe orchestration ───────────────────────────────────────────────────────

async def probe_candidates(
    *,
    client: CloudClient,
    target: dict[str, Any],
    candidates: list[probe.Candidate],
    intent: str,
    observe: float,
    gap: float,
    observe_only: bool,
    verbose: bool,
) -> tuple[list[probe.Verdict], Any]:
    """Run every candidate through send -> observe -> judge."""

    device_id = str(target.get("deviceId", ""))
    device_uid = str(target.get("uid", "") or device_id)
    family_id = client.family_id or ""
    baseline = _baseline_from_device(target, intent)

    verdicts: list[probe.Verdict] = []
    total = len(candidates)
    for index, candidate in enumerate(candidates, 1):
        session = ControlSession(client.username, _password_of(client), family_id)
        try:
            if not await session.connect():
                _LOGGER.error("实时通道连接失败，终止后续候选")
                break
            if observe_only:
                _LOGGER.info("[%s/%s] 仅观察（不发送命令）%s", index, total, candidate.label)
            else:
                ack = await session.send_control(device_id, device_uid, candidate)
                _LOGGER.info(
                    "[%s/%s] 已发送 %s（服务端%s）",
                    index, total, candidate.label,
                    "已确认" if ack else "无确认",
                )
            observations = await session.observe(device_id, observe)
            verdict = probe.judge(candidate, baseline, observations)
            verdicts.append(verdict)
            icon = {"verified": "✅", "ineffective": "❌", "inconclusive": "❓"}[verdict.status]
            print(f"  {icon} {verdict.status:<12} {candidate.label}")
            if verbose and observations:
                for payload in observations:
                    print(f"       推送: {json.dumps(payload, ensure_ascii=False)[:180]}")
        finally:
            await session.close()
        if index < total:
            await asyncio.sleep(gap)
    return verdicts, baseline


def _password_of(client: CloudClient) -> str:
    # The digest is already computed; the CLI keeps the raw password separately.
    return getattr(client, "_raw_password", "") or ""


def _baseline_from_device(device: Mapping[str, Any], intent: str) -> Any:
    payload: dict[str, Any] = {}
    properties = device.get("properties")
    if isinstance(properties, Mapping):
        payload["properties"] = dict(properties)
    for key in ("value1", "value2", "value3", "value4", "state"):
        if device.get(key) is not None:
            payload[key] = device[key]
    return probe.baseline_from_payload(payload, intent)


def _platform_for(device: Mapping[str, Any]) -> tuple[str, list[str]]:
    normalized = {
        "device_id": device.get("deviceId", ""),
        "device_type_raw": _int(device.get("deviceType")),
        "sub_device_type": _int(device.get("subDeviceType")),
        "class_id": _int(device.get("classId")),
        "model": device.get("model") or "",
        "ui_model": (device.get("ui") or {}).get("model", "") if isinstance(device.get("ui"), dict) else "",
        "properties": device.get("properties") or {},
    }
    try:
        capability = capabilities.capability_for(normalized)
        platforms = sorted(capability.platforms)
    except Exception:  # noqa: BLE001
        platforms = []
    if not platforms:
        # Fall back to a shape-based guess so a brand-new device type still works.
        fields = set()
        try:
            obs = skeleton.Observations()
            obs.add_snapshot(device)
            fields = {name for name in ("state", "brightness", "color_temp", "position") if skeleton.guess_field(name, obs)}
        except Exception:  # noqa: BLE001
            pass
        guessed, _why = skeleton.infer_platform(device, fields)
        return guessed, []
    return platforms[0], platforms


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _confirm_risky(device: Mapping[str, Any], assume_yes: bool) -> bool:
    """Refuse to brute-force commands on devices with physical risk."""

    device_type = _int(device.get("deviceType"))
    reason = probe.RISKY_DEVICE_TYPES.get(device_type)
    if reason is None:
        name = str(device.get("deviceName") or "")
        if any(word in name for word in ("锁", "lock", "晾衣", "门")):
            reason = "设备名称看起来是门锁/晾衣机等有机械或安全风险的设备"
    if reason is None:
        return True
    if assume_yes:
        _LOGGER.warning("⚠️  %s：已按 --yes 跳过确认，请自行承担风险", reason)
        return True
    print(f"\n⚠️  这台设备被判定为「{reason}」，不适合盲目枚举控制命令。")
    print("   继续可能导致门锁动作、电机启动等真实物理后果。")
    answer = input("   确认要继续吗？输入 yes 继续：").strip().lower()
    return answer == "yes"


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="控制命令实证探针：发出候选命令并用设备状态反馈判断哪条生效",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "凭据也可用环境变量 ORVIBO_USERNAME / ORVIBO_PASSWORD\n"
            "示例：\n"
            "  python tools/probe_device_control.py <账号> <密码> --list\n"
            "  python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --intent on --observe 6\n"
            "  python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --payloads cand.json --intent on\n"
        ),
    )
    parser.add_argument("username", nargs="?", default=os.environ.get("ORVIBO_USERNAME"))
    parser.add_argument("password", nargs="?", default=os.environ.get("ORVIBO_PASSWORD"))
    parser.add_argument("--list", action="store_true", help="列出设备与当前状态字段")
    parser.add_argument("--device", help="目标设备（名称或 deviceId，支持子串）")
    parser.add_argument(
        "--intent",
        default="on",
        choices=("on", "off", "brightness", "color_temp", "position"),
        help="要验证的动作（默认 on）",
    )
    parser.add_argument("--target", type=int, help="brightness/color_temp/position 的目标值")
    parser.add_argument("--payloads", help="候选命令 JSON（反编译 App 得到的真实帧可直接用）")
    parser.add_argument("--platform", help="覆盖平台判定")
    parser.add_argument("--observe", type=float, default=6.0, help="每条候选后的观察秒数（默认 6）")
    parser.add_argument("--gap", type=float, default=1.0, help="候选之间的间隔秒数（默认 1）")
    parser.add_argument("--max-candidates", type=int, default=12, help="自动枚举的候选上限（默认 12）")
    parser.add_argument(
        "--observe-only",
        action="store_true",
        help="只观察不发送：先确认状态推送链路是否通，再决定要不要真发命令",
    )
    parser.add_argument("--out", help="把验证通过的 control 块写成 YAML 片段")
    parser.add_argument("--family", help="家庭索引（默认 0）")
    parser.add_argument("--cloud-host", help="云端域名（国际区 homemate.orvibo.com）")
    parser.add_argument("--yes", action="store_true", help="跳过风险设备确认（非交互场景）")
    parser.add_argument("--verbose", action="store_true", help="打印原始推送与调试日志")
    return parser


async def run(args: argparse.Namespace) -> int:
    if not args.username or not args.password:
        _LOGGER.error("缺少账号密码：请用位置参数或 ORVIBO_USERNAME / ORVIBO_PASSWORD 提供")
        return 2

    client = CloudClient(args.username, args.password, host=args.cloud_host)
    client._raw_password = args.password  # type: ignore[attr-defined]
    try:
        if not await client.login():
            return 3
        families = await client.families()
        if not families:
            _LOGGER.error("该账号下没有家庭")
            return 3
        index = int(args.family or 0)
        if index >= len(families):
            _LOGGER.error("家庭索引 %s 超范围（共 %s 个）", index, len(families))
            return 3
        family_id = families[index].get("familyId", "")
        client.family_id = family_id

        devices = await client.readtable(family_id)
        if not devices:
            _LOGGER.error("readtable 没有返回设备")
            return 3

        if args.list or not args.device:
            _print_device_table(devices)
            if not args.device:
                print(
                    "\n接下来：\n"
                    "  1) 先 --observe-only 确认能收到该设备的状态推送；\n"
                    "  2) 再 --intent on 实发候选命令并判定。"
                )
            return 0

        target = _select(devices, args.device)
        if target is None:
            _LOGGER.error("找不到设备“%s”（用 --list 查看）", args.device)
            return 4

        if not _confirm_risky(target, args.yes):
            print("已取消。")
            return 0

        platform, platforms = _platform_for(target)
        if args.platform:
            platform = args.platform
        capabilities = _capabilities_for(target, platform)

        if args.payloads:
            raw = json.loads(Path(args.payloads).read_text(encoding="utf-8"))
            candidates = probe.load_candidates(raw)
            for candidate in candidates:
                if not candidate.intent:
                    object.__setattr__(candidate, "intent", args.intent)
                if candidate.expected is None:
                    object.__setattr__(candidate, "expected", args.intent == "on")
            _LOGGER.info("从 %s 载入 %s 条候选", args.payloads, len(candidates))
        else:
            candidates = probe.generate_candidates(
                platform=platform,
                intent=args.intent,
                capabilities=capabilities,
                target=args.target,
                max_candidates=args.max_candidates,
            )
            if args.observe_only:
                candidates = candidates[:1]

        print(
            f"\n设备：{target.get('deviceName')}  "
            f"platform={platform}  动作={args.intent}  候选={len(candidates)} 条"
        )
        print(f"观察窗口 {args.observe:g}s/条，间隔 {args.gap:g}s")
        if not args.observe_only:
            print("⚠️  即将向真实设备发送控制命令。\n")

        verdicts, baseline = await probe_candidates(
            client=client,
            target=target,
            candidates=candidates,
            intent=args.intent,
            observe=args.observe,
            gap=args.gap,
            observe_only=args.observe_only,
            verbose=args.verbose,
        )

        print("\n── 判定结果 " + "─" * 52)
        print(probe.summarise(verdicts))
        if baseline is not None:
            print(f"\n测试前基线：{baseline}")

        verified = [verdict for verdict in verdicts if verdict.verified]
        if not verified:
            print(
                "\n没有候选被验证生效。可按这个顺序排查：\n"
                "  1) 先跑 --observe-only：确认操作设备时能收到状态推送（收不到就是推送链路问题，"
                "不是命令问题）；\n"
                "  2) 加大 --observe（有些设备回推很慢）或确认云轮询间隔；\n"
                "  3) 状态字段是 value1 数值时工具无法判断方向，需要你人工对照；\n"
                "  4) 都不行就说明这条命令不在已验证白名单里 —— 需要从 App 反编译得到真实帧，"
                "再用 --payloads 灌进来验证。"
            )
            return 6

        control = probe.render_control_block(verdicts, platform)
        yaml_text = probe.render_yaml(control, indent=2)
        alternatives = probe.render_alternatives(verdicts)
        print("\n── 验证通过的控制块（可直接替换进 profile）" + "─" * 20)
        print(yaml_text)
        for line in alternatives:
            print(line)
        print(
            "\n把它替换到 profile 的 control: 位置，然后把 hardware_verified 改成 true 即可启用。"
        )

        if args.out:
            out = Path(args.out)
            header = (
                "# 由 tools/probe_device_control.py 实测验证生成\n"
                f"# 设备：{target.get('deviceName')}   platform={platform}   动作={args.intent}\n"
                f"# 测试前基线：{baseline}\n"
                "# 判定依据：发送候选后设备上报的状态推送\n"
            )
            out.write_text(header + yaml_text + "\n".join(alternatives) + "\n", encoding="utf-8")
            print(f"已写入 {out}")
        return 0
    finally:
        await client.close()


def _capabilities_for(device: Mapping[str, Any], platform: str) -> list[str]:
    normalized = {
        "device_id": device.get("deviceId", ""),
        "device_type_raw": _int(device.get("deviceType")),
        "sub_device_type": _int(device.get("subDeviceType")),
        "class_id": _int(device.get("classId")),
        "properties": device.get("properties") or {},
    }
    try:
        profile = device_types.get_device_profile(normalized)
        return list(profile.info.capabilities)
    except Exception:  # noqa: BLE001
        return []


def _select(devices: list[dict[str, Any]], wanted: str) -> Optional[dict[str, Any]]:
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


def _print_device_table(devices: list[dict[str, Any]]) -> None:
    print(f"\n共 {len(devices)} 台设备：\n")
    for index, device in enumerate(devices, 1):
        name, match, category, platform = skeleton_describe(device)
        state = _current_state_summary(device)
        print(f"{index:3}. {name}")
        print(f"     match: {match}")
        print(f"     当前识别: {category}   平台: {platform}")
        if state:
            print(f"     当前状态: {state}")
    print()


def skeleton_describe(device: Mapping[str, Any]) -> tuple[str, str, str, str]:
    name = str(device.get("deviceName") or device.get("name") or "?")
    match = skeleton.build_match(device, "minimal")
    normalized = {
        "device_id": device.get("deviceId", ""),
        "device_type_raw": _int(device.get("deviceType")),
        "sub_device_type": _int(device.get("subDeviceType")),
        "class_id": _int(device.get("classId")),
        "model": device.get("model") or "",
        "ui_model": (device.get("ui") or {}).get("model", "") if isinstance(device.get("ui"), dict) else "",
        "properties": device.get("properties") or {},
    }
    try:
        profile = device_types.get_device_profile(normalized)
        category = profile.info.label or profile.category.value
    except Exception:  # noqa: BLE001
        category = "?"
    try:
        platforms = sorted(capabilities.capability_for(normalized).platforms)
        platform = "、".join(platforms) or "-"
    except Exception:  # noqa: BLE001
        platform = "-"
    summary = " ".join(f"{key}={value}" for key, value in match.items()) or "-"
    return name, summary, category, platform


def _current_state_summary(device: Mapping[str, Any]) -> str:
    parts = []
    for key in ("value1", "value2", "value3", "value4"):
        if device.get(key) is not None:
            parts.append(f"{key}={device[key]}")
    properties = device.get("properties")
    if isinstance(properties, Mapping) and properties:
        try:
            parts.append("properties=" + json.dumps(properties, ensure_ascii=False)[:80])
        except Exception:  # noqa: BLE001
            pass
    return "  ".join(parts)


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
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
