"""Pure logic for the control-command evidence probe.

The gap this closes: ``readtable`` tells you what a device *is*, the realtime
channel tells you what it *reports*, but neither reveals the control command the
official App actually sends.  Since both transports are AES-encrypted (cloud SSL
uses a session key, the MixHub TCP channel too) and the App pins its certificates,
passive sniffing does not work.  The remaining practical route is empirical:

    send one candidate command -> observe the resulting state push -> judge

This module owns everything that can be decided without a network:

* :func:`generate_candidates` -- enumerate the plausible payloads for an intent
  (including both active-low and active-high conventions), or accept a
  hand-written list from ``--payloads``.
* :func:`baseline_from_payload` -- the pre-test state snapshot.
* :func:`judge` -- decide ``verified`` / ``ineffective`` / ``inconclusive`` from
  the observed pushes, without guessing.
* :func:`render_control_block` -- emit a ``control:`` block that only contains
  verified candidates, ready to paste into a profile.

It never sends anything itself and never writes a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Reasons a candidate may not be safe/meaningful to test.
RISKY_DEVICE_TYPES = {
    522: "智能门锁",
    107: "WiFi 门锁",
    52: "智能晾衣机（带电机的动作有机械风险）",
    34: "窗帘（部分型号行程保护，反复开合有风险）",
    35: "卷帘",
}

#: Orders that exist in the verified allow-list and make sense as candidates.
STATE_ON_OFF_ORDERS = ("on", "off")
PROPERTY_ORDER = "set property"


@dataclass(frozen=True)
class Candidate:
    """One control payload to try."""

    label: str
    order: str
    value1: int = 0
    value2: int = 0
    value3: int = 0
    value4: int = 0
    properties: Optional[Mapping[str, Any]] = None
    intent: str = ""
    expected: Any = None
    note: str = ""

    def payload_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "order": self.order,
            "value1": self.value1,
            "value2": self.value2,
            "value3": self.value3,
            "value4": self.value4,
        }
        if self.properties:
            kwargs["properties"] = dict(self.properties)
        return kwargs


@dataclass
class Verdict:
    """Judgement for one candidate."""

    candidate: Candidate
    status: str  # verified | ineffective | inconclusive
    baseline: Any = None
    observed: list[Any] = field(default_factory=list)
    pushes: int = 0
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def as_row(self) -> dict[str, Any]:
        return {
            "label": self.candidate.label,
            "order": self.candidate.order,
            "values": (
                self.candidate.value1,
                self.candidate.value2,
                self.candidate.value3,
                self.candidate.value4,
            ),
            "properties": dict(self.candidate.properties or {}),
            "intent": self.candidate.intent,
            "status": self.status,
            "baseline": self.baseline,
            "observed": self.observed,
            "pushes": self.pushes,
            "detail": self.detail,
        }


# ── candidate generation ──────────────────────────────────────────────────────

def generate_candidates(
    *,
    platform: str,
    intent: str,
    capabilities: Iterable[str] = (),
    target: Optional[int] = None,
    include_active_high: bool = True,
    max_candidates: int = 40,
) -> list[Candidate]:
    """Enumerate plausible control payloads for one intent.

    Both on/off polarities are emitted because the polarity is exactly what is
    unknown: built-in Orvibo types disagree (``value1=0`` is "on" for type 1/38/503
    and "off" for type 135/136).
    """

    intent = intent.lower()
    caps = {str(item).lower() for item in capabilities}

    if intent == "on":
        return _on_off_candidates("on", max_candidates)
    if intent == "off":
        return _on_off_candidates("off", max_candidates)
    if intent == "brightness":
        return _brightness_candidates(target, max_candidates)
    if intent == "color_temp":
        return _color_temp_candidates(target, max_candidates)
    if intent == "position":
        return _position_candidates(target, max_candidates)

    raise ValueError(
        f"未知 intent {intent!r}；支持 on / off / brightness / color_temp / position"
    )


def _on_off_candidates(intent: str, limit: int) -> list[Candidate]:
    is_on = intent == "on"
    candidates: list[Candidate] = []

    # value1 convention: 0 == on (active-low) and 1 == on (active-high) are both
    # real in this ecosystem, so both are legal candidates.  The other slots get
    # the values the built-in senders use.
    for v1 in (0, 1):
        candidates.append(
            Candidate(
                label=f"order={intent} value1={v1}",
                order=intent,
                value1=v1,
                intent=intent,
                expected=is_on,
                note="value1 约定（0=开 或 1=开）",
            )
        )
        candidates.append(
            Candidate(
                label=f"order={intent} value1={v1} value2=255",
                order=intent,
                value1=v1,
                value2=255,
                intent=intent,
                expected=is_on,
                note="部分灯具开灯必须带满亮度，否则亮度为 0 即熄灭",
            )
        )

    # set property envelope (used by switch/light/dimmable/floor heating)
    status = "on" if is_on else "off"
    for key in ("onoff",):
        candidates.append(
            Candidate(
                label=f"set property {key}.status={status}",
                order=PROPERTY_ORDER,
                properties={key: {"status": status}},
                intent=intent,
                expected=is_on,
                note="属性型报文（type=501/503/135/136 等）",
            )
        )
    candidates.append(
        Candidate(
            label=f"set property onoff_status={status}",
            order=PROPERTY_ORDER,
            properties={"onoff_status": status},
            intent=intent,
            expected=is_on,
            note="扁平属性写法",
        )
    )
    return candidates[:limit]


def _brightness_candidates(target: Optional[int], limit: int) -> list[Candidate]:
    """Brightness has two unknown dimensions: the unit and the carrier slot."""

    levels: list[tuple[int, str]] = []
    if target is None:
        levels = [(255, "0-255 量纲上限"), (100, "0-100 百分比上限")]
    else:
        levels = [(max(1, min(255, int(target))), "用户给定的 0-255 值")]
        if int(target) <= 100:
            levels.append((int(target), "用户给定的值按 0-100 百分比解释"))

    candidates: list[Candidate] = []
    for level, why in levels:
        candidates.append(
            Candidate(
                label=f"move to level value2={level}",
                order="move to level",
                value1=0,
                value2=level,
                intent="brightness",
                expected=level,
                note=why,
            )
        )
        candidates.append(
            Candidate(
                label=f"fast move to level value2={level}",
                order="fast move to level",
                value1=0,
                value2=level,
                intent="brightness",
                expected=level,
                note=why,
            )
        )
        candidates.append(
            Candidate(
                label=f"on value2={level}",
                order="on",
                value1=0,
                value2=level,
                intent="brightness",
                expected=level,
                note="老协议把亮度放在 on 命令的 value2",
            )
        )
        candidates.append(
            Candidate(
                label=f"set property brightness.percent={level}",
                order=PROPERTY_ORDER,
                properties={"brightness": {"percent": level}},
                intent="brightness",
                expected=level,
                note="属性型亮度（0-100）",
            )
        )
    return candidates[:limit]


def _color_temp_candidates(target: Optional[int], limit: int) -> list[Candidate]:
    candidates: list[Candidate] = []
    if target is None:
        # Two very different encodings of "warm end".
        pairs = [(370, "mired 370 (≈2700K)"), (2700, "Kelvin 2700")]
    else:
        kelvin = int(target)
        pairs = [(round(1_000_000 / max(1, kelvin)), f"mired（由 {kelvin}K 换算）"),
                 (kelvin, "Kelvin")]

    for value, why in pairs:
        candidates.append(
            Candidate(
                label=f"fast color temperature value3={value}",
                order="fast color temperature",
                value1=0,
                value2=255,
                value3=value,
                intent="color_temp",
                expected=value,
                note=why,
            )
        )
        candidates.append(
            Candidate(
                label=f"set property colorTemp.value={value}",
                order=PROPERTY_ORDER,
                properties={"colorTemp": {"value": value}},
                intent="color_temp",
                expected=value,
                note=why,
            )
        )
    return candidates[:limit]


def _position_candidates(target: Optional[int], limit: int) -> list[Candidate]:
    value = 100 if target is None else max(0, min(100, int(target)))
    return [
        Candidate(
            label=f"open value1={value}",
            order="open",
            value1=value,
            intent="position",
            expected=value,
            note="窗帘位置型（0-100）",
        ),
        Candidate(
            label=f"set property percent={value}",
            order=PROPERTY_ORDER,
            properties={"percent": value},
            intent="position",
            expected=value,
            note="属性型位置",
        ),
    ][:limit]


# ── hand-written candidates ───────────────────────────────────────────────────

_ALLOWED_ORDERS = frozenset(
    {
        "on",
        "off",
        "open",
        "stop",
        "set property",
        "move to level",
        "fast move to level",
        "fast color temperature",
    }
)


def load_candidates(raw: Any) -> list[Candidate]:
    """Parse ``--payloads`` input: a list of payload objects.

    Accepts either the probe's own shape or a raw ``cmd=15`` payload dump, so a
    frame recovered by decompiling the App can be pasted straight in.
    """

    if isinstance(raw, Mapping) and isinstance(raw.get("candidates"), list):
        raw = raw["candidates"]
    if not isinstance(raw, list):
        raise ValueError("--payloads 必须是 JSON 数组，或 {\"candidates\": [...]}")

    candidates: list[Candidate] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"第 {index} 条候选不是对象")
        order = item.get("order")
        if not isinstance(order, str) or order not in _ALLOWED_ORDERS:
            raise ValueError(
                f"第 {index} 条候选的 order 非法（{order!r}）；"
                f"只允许：{', '.join(sorted(_ALLOWED_ORDERS))}"
            )
        properties = item.get("properties")
        if properties is not None and not isinstance(properties, Mapping):
            raise ValueError(f"第 {index} 条候选的 properties 必须是对象")
        if properties and order != PROPERTY_ORDER:
            raise ValueError(
                f"第 {index} 条候选：properties 只能与 order='set property' 搭配"
            )
        candidates.append(
            Candidate(
                label=str(item.get("label") or f"候选 {index}: order={order}"),
                order=order,
                value1=_int(item.get("value1")) or 0,
                value2=_int(item.get("value2")) or 0,
                value3=_int(item.get("value3")) or 0,
                value4=_int(item.get("value4")) or 0,
                properties=dict(properties) if properties else None,
                intent=str(item.get("intent") or ""),
                expected=item.get("expected", item.get("intent")),
                note=str(item.get("note") or ""),
            )
        )
    return candidates


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── state normalisation + judging ─────────────────────────────────────────────

#: Payload paths that carry the on/off truth, in priority order.
_STATE_PATHS = (
    "properties.onoff.status",
    "onoff.status",
    "properties.onoff_status",
    "properties.state",
    "state",
    "value1",
)
_POSITION_PATHS = ("properties.percent", "percent", "properties.position", "position", "value1")
_BRIGHTNESS_PATHS = (
    "properties.brightness.percent",
    "properties.brightness.value",
    "brightness.percent",
    "brightness",
    "value2",
)


def _lookup(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
        if current is None:
            return None
    if isinstance(current, Mapping):
        for key in ("status", "value", "percent"):
            if key in current:
                return current[key]
    return current


def observed_state(payload: Mapping[str, Any], intent: str) -> Optional[Any]:
    """Extract the value an intent cares about from one payload."""

    if intent in ("on", "off"):
        return _observed_bool(payload)
    if intent == "position":
        return _observed_number(payload, _POSITION_PATHS)
    if intent == "brightness":
        return _observed_number(payload, _BRIGHTNESS_PATHS)
    if intent == "color_temp":
        return _observed_number(
            payload, ("value3", "properties.colorTemp.value", "colorTemp")
        )
    return None


def _observed_bool(payload: Mapping[str, Any]) -> Optional[bool]:
    for path in _STATE_PATHS:
        value = _lookup(payload, path)
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            # 0/1 carries no polarity: ``value1=0`` is "on" for some Orvibo types
            # and "off" for others.  ``None`` means "present but ambiguous".
            return None
        text = str(value).strip().lower()
        if text in ("on", "true", "open", "1"):
            return True
        if text in ("off", "false", "close", "closed", "0"):
            return False
    return None


def _carries_state_field(payload: Mapping[str, Any]) -> bool:
    """Whether the payload mentions an on/off-ish field at all."""

    return any(_lookup(payload, path) is not None for path in _STATE_PATHS)


def _observed_number(payload: Mapping[str, Any], paths: Sequence[str]) -> Optional[float]:
    for path in paths:
        value = _lookup(payload, path)
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except ValueError:
            continue
    return None


def baseline_from_payload(payload: Mapping[str, Any], intent: str) -> Any:
    """Best-effort pre-test value; None when the payload does not carry it."""

    return observed_state(payload, intent)


def judge(
    candidate: Candidate,
    baseline: Any,
    observations: Sequence[Mapping[str, Any]],
    *,
    tolerance: float = 2.0,
) -> Verdict:
    """Decide whether ``candidate`` actually moved the device to the intent.

    * ``verified``     -- an observed push shows the intended value;
    * ``ineffective``  -- pushes arrived but never showed the intended value;
    * ``inconclusive`` -- no usable observation (no push, or an ambiguous value).

    A candidate is never called verified from the server ACK alone: the ACK only
    proves the cloud accepted the request.
    """

    intent = candidate.intent or ""
    observed: list[Any] = []
    for payload in observations:
        value = observed_state(payload, intent)
        if value is not None:
            observed.append(value)
    carries_field = any(
        _carries_state_field(payload) for payload in observations
    ) if intent in ("on", "off") else bool(observed)

    verdict_args = {
        "candidate": candidate,
        "baseline": baseline,
        "observed": observed,
        "pushes": len(observations),
    }

    if not observations:
        return Verdict(
            status="inconclusive", detail="观察窗口内没有收到该设备的任何状态推送", **verdict_args
        )
    if not observed and not carries_field:
        return Verdict(
            status="inconclusive",
            detail=f"收到 {len(observations)} 条推送，但都不含 {intent} 相关字段",
            **verdict_args,
        )

    expected = candidate.expected
    if intent in ("on", "off"):
        want = intent == "on"
        if any(isinstance(value, bool) and value == want for value in observed):
            return Verdict(status="verified", detail="推送显示已到达目标状态", **verdict_args)
        if not observed and carries_field:
            # The device reports the field but as a bare 0/1, which encodes
            # polarity we cannot know.  Never guess here.
            return Verdict(
                status="inconclusive",
                detail=(
                    "状态字段是数值（value1），无法判断开/关方向。请在真机上对照："
                    "点一次开关后 value1 变成什么，再决定用 0=开 还是 1=开"
                ),
                **verdict_args,
            )
        if all(isinstance(value, bool) for value in observed):
            return Verdict(
                status="ineffective",
                detail=f"推送显示状态为 {observed}，与目标 {'开' if want else '关'} 不符",
                **verdict_args,
            )
        return Verdict(
            status="inconclusive",
            detail="状态字段无法解读，请人工核对原始推送",
            **verdict_args,
        )

    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        want = float(expected)
        if any(abs(float(value) - want) <= tolerance for value in observed):
            return Verdict(status="verified", detail="推送显示已到达目标数值", **verdict_args)
        return Verdict(
            status="ineffective",
            detail=f"推送显示 {observed}，与目标 {want:g} 不符（容差 {tolerance:g}）",
            **verdict_args,
        )

    return Verdict(
        status="inconclusive",
        detail="候选没有声明 expected，无法自动判定；请人工比对下面的推送值",
        **verdict_args,
    )


# ── rendering ─────────────────────────────────────────────────────────────────

def render_control_block(verdicts: Sequence[Verdict], platform: str) -> dict[str, Any]:
    """Build a ``control:`` block from verified candidates only.

    When several candidates verify for the same intent, the first one wins (the
    enumeration order puts the most common convention first) and the rest are
    reported as alternatives by :func:`render_alternatives`.
    """

    action_for_intent = {
        "on": "on",
        "off": "off",
        "brightness": "brightness",
        "color_temp": "color_temp",
        "position": "position",
    }
    control: dict[str, Any] = {}
    for verdict in verdicts:
        if not verdict.verified:
            continue
        action = action_for_intent.get(verdict.candidate.intent)
        if action is None or action in control:
            continue
        body: dict[str, Any] = {"order": verdict.candidate.order}
        for slot in ("value1", "value2", "value3", "value4"):
            value = getattr(verdict.candidate, slot)
            # Explicitly allow 0: ``value1: 0`` is the common "on" encoding and
            # a truthiness check would silently drop the whole command's meaning.
            if value is not None:
                body[slot] = value
        if verdict.candidate.properties:
            body["properties"] = dict(verdict.candidate.properties)
        control[action] = body
    return control


def render_alternatives(verdicts: Sequence[Verdict]) -> list[str]:
    """Verified candidates that lost to an earlier one for the same intent."""

    seen: set[str] = set()
    lines: list[str] = []
    for verdict in verdicts:
        if not verdict.verified:
            continue
        intent = verdict.candidate.intent
        if intent in seen:
            lines.append(f"#   备选（同样生效）：{verdict.candidate.label}")
            continue
        seen.add(intent)
    return lines


def render_yaml(control: Mapping[str, Any], indent: int = 0) -> str:
    """Render a control block as YAML text (no external dependency)."""

    pad = " " * indent
    lines: list[str] = [f"{pad}control:"]
    for action, body in control.items():
        lines.append(f"{pad}  {action}:")
        for key, value in body.items():
            if isinstance(value, Mapping):
                inner = ", ".join(f"{k}: {_yaml(v)}" for k, v in value.items())
                lines.append(f"{pad}    {key}: {{{inner}}}")
            else:
                lines.append(f"{pad}    {key}: {_yaml(value)}")
    return "\n".join(lines) + "\n"


def _yaml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if re.search(r"[:#\[\]{}&*!|>'\"%@`]", text) or text.strip() != text:
        return '"' + text.replace('"', '\\"') + '"'
    return text


def summarise(verdicts: Sequence[Verdict]) -> str:
    """Compact table for the terminal."""

    if not verdicts:
        return "（没有候选可测）"
    width = max(len(verdict.candidate.label) for verdict in verdicts)
    icons = {"verified": "✅", "ineffective": "❌", "inconclusive": "❓"}
    lines = []
    for verdict in verdicts:
        lines.append(
            f"{icons.get(verdict.status, '?')} {verdict.candidate.label:<{width}}  "
            f"{verdict.status:<12} {verdict.detail}"
        )
    return "\n".join(lines)
