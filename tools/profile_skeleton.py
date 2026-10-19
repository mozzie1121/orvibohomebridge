"""Generate a declarative custom-device profile skeleton from observed evidence.

Two callers use this module:

* ``tools/generate_device_profile.py`` -- logs into the ORVIBO cloud, listens for
  real state pushes while the user operates the device, and writes a profile.
* ``tests/test_profile_skeleton.py`` -- pure unit tests, no network.

Design rule: **only write what the observation supports.**  Anything that cannot
be derived from the payloads (active-low vs active-high, brightness scale,
mired vs Kelvin) is emitted as a working placeholder plus an explicit item on
the checklist for the user to confirm on the real device.  The module never
claims ``hardware_verified``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# --- import the pure integration modules without touching the filesystem ------
_COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "orvibohomebridge"
if str(_COMPONENT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR.parent))


def _pure_module(name: str):
    """Import ``orvibohomebridge.<name>`` without executing the HA-bound package.

    ``orvibohomebridge/__init__.py`` imports Home Assistant, which is not
    installed in a plain CLI environment.  Installing an empty package module in
    ``sys.modules`` keeps the namespace package mechanism working so the pure
    submodules import normally -- without rewriting any file on disk (which is
    what the developer probe script does).
    """

    import importlib
    import types

    package_name = "orvibohomebridge"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(_COMPONENT_DIR)]
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.{name}")


# ── evidence collection ───────────────────────────────────────────────────────


@dataclass
class Observations:
    """Values seen for one device, accumulated across snapshots and pushes."""

    #: field path -> ordered distinct values (JSON-ish scalars)
    values: dict[str, list[Any]] = field(default_factory=dict)
    #: field path -> how many payloads carried it
    counts: dict[str, int] = field(default_factory=dict)
    #: number of payloads merged
    samples: int = 0
    #: raw payloads, kept for the "show me the packets" debug flag
    raws: list[dict[str, Any]] = field(default_factory=list)

    def add_payload(self, payload: Mapping[str, Any], *, keep_raw: bool = True) -> None:
        """Record every scalar found in one payload, at every nesting level."""

        self.samples += 1
        if keep_raw:
            self.raws.append(dict(payload))
        for path, value in _iter_scalars(payload):
            self.counts[path] = self.counts.get(path, 0) + 1
            bucket = self.values.setdefault(path, [])
            if value not in bucket:
                bucket.append(value)

    def add_snapshot(self, device: Mapping[str, Any]) -> None:
        """Record the readtable snapshot (device object + its status row)."""

        for key in ("value1", "value2", "value3", "value4"):
            if device.get(key) is not None:
                self.add_payload({key: device[key]}, keep_raw=False)
        properties = device.get("properties")
        if isinstance(properties, Mapping) and properties:
            self.add_payload({"properties": properties}, keep_raw=False)

    # -- accessors used by the field guessers ---------------------------------

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    def distinct(self, path: str) -> list[Any]:
        return self.values.get(path, [])

    def count(self, path: str) -> int:
        return self.counts.get(path, 0)

    def numbers(self, path: str) -> list[float]:
        out: list[float] = []
        for value in self.distinct(path):
            number = _as_number(value)
            if number is not None:
                out.append(number)
        return out

    def seen(self, path: str) -> bool:
        return path in self.values


def _iter_scalars(payload: Mapping[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield ``(dotted_path, scalar)`` for every leaf in a payload."""

    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            # Keep the container itself addressable too: a profile can read
            # ``properties.brightness.percent`` as well as ``brightness.percent``.
            yield from _iter_scalars(value, path)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    yield from _iter_scalars(item, f"{path}.{index}")
                elif _is_scalar(item):
                    yield f"{path}.{index}", item
        elif _is_scalar(value):
            yield path, value


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_number(value)
    return int(number) if number is not None else None


# ── field guessing ────────────────────────────────────────────────────────────

#: Candidate source paths per normalized HA field, most specific first.
#: Built-in parsers in ``parsers/`` are the reference for these spellings.
_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "state": (
        "properties.onoff.status",
        "onoff.status",
        "properties.onoff_status",
        "properties.state",
        "state",
        "value1",
    ),
    "brightness": (
        "properties.brightness.percent",
        "properties.brightness.value",
        "brightness.percent",
        "brightness",
        "value2",
    ),
    "color_temp": (
        "properties.colorTemp.value",
        "properties.colortemp.value",
        "properties.colorTemp",
        "colortemp",
        "value3",
    ),
    "position": (
        "properties.percent",
        "properties.position",
        "percent",
        "position",
        "value1",
    ),
    "temperature": (
        "properties.temperature",
        "temperature",
        "value1",
        "value2",
    ),
    "humidity": (
        "properties.humidity",
        "humidity",
        "value2",
    ),
    "battery": (
        "properties.battery",
        "properties.battery_level",
        "battery",
        "dry_battery_level",
        "lithium_battery_level",
    ),
}

#: A brightness-ish source must plausibly carry a brightness-like range.
_BRIGHTNESS_RANGES = ((0, 100), (0, 255), (0, 1000))

_TODO_ACTIVE_LOGIC = (
    "state 的开关方向（active-low/high）无法从数据推断：脚本按内置惯例写的"
    "（value1=0 视为“开”）。请在真机上按一次开关并把日志里的 value 与实体状态对照，"
    "如果反了就把 true_values/false_values 对调"
)
_TODO_BRIGHTNESS_RANGE = (
    "亮度量纲无法推断（0-100 还是 0-255）：脚本按观察到的范围写了 clamp，"
    "请确认后调整（量纲错了会导致亮度只有一半或直接溢出）"
)
_TODO_COLOR_TEMP_UNIT = (
    "色温单位无法推断（mired 还是 Kelvin）：当前按 mired 写了 input_unit，"
    "请确认；若是 Kelvin 请改成 input_unit: kelvin 或删掉该字段"
)
_TODO_PLATFORM_ENTITIES = (
    "climate/fan 平台暂不创建自定义实体；若目标能力是空调/新风，请改 platform "
    "或用 switch/sensor/binary_sensor 表达"
)


@dataclass(frozen=True)
class FieldGuess:
    """One picked source path for a normalized field."""

    field: str
    path: str
    spec: Mapping[str, Any]
    note: str = ""


#: Which normalized fields each platform actually consumes.  Guessing a field
#: no entity reads is worse than useless: it invites the user to trust a mapping
#: that can never work (e.g. ``value1`` guessed as both on/off and position).
_PLATFORM_FIELDS: dict[str, tuple[str, ...]] = {
    "light": ("state", "brightness", "color_temp"),
    "switch": ("state",),
    "cover": ("position",),
    "binary_sensor": ("state", "position"),
    "sensor": ("temperature", "humidity", "battery"),
    "climate": (),
    "fan": (),
}

#: Plausible value ranges for read-only numeric fields.  A field whose observed
#: values fall outside its range is not that field.
_FIELD_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (-40.0, 125.0),
    "humidity": (0.0, 100.0),
    "battery": (0.0, 100.0),
}


def guess_field(field: str, observations: Observations) -> Optional[FieldGuess]:
    """Pick the best observed source path for ``field``, or None."""

    best: Optional[FieldGuess] = None
    for path in _FIELD_CANDIDATES.get(field, ()):
        if not observations.seen(path):
            continue
        guess = _build_guess(field, path, observations)
        if guess is None:
            continue
        if best is None or _better(guess, best, field):
            best = guess
        if field != "state" and best is not None and not best.note:
            break
    return best


def _better(candidate: FieldGuess, current: FieldGuess, field: str) -> bool:
    """Prefer an unambiguous guess over a fallback one."""

    if not current.note and candidate.note:
        return False
    if current.note and not candidate.note:
        return True
    # Both equal: prefer the more specific path (deeper == more explicit).
    return candidate.path.count(".") > current.path.count(".")


def _build_guess(field: str, path: str, observations: Observations) -> Optional[FieldGuess]:
    values = observations.distinct(path)

    if field == "state":
        if _looks_boolean(values):
            numeric = all(_as_number(item) is not None for item in values if not isinstance(item, bool))
            if numeric and not any(isinstance(item, str) for item in values):
                # 0/1 carries no on/off polarity: on is 0 for some Orvibo types
                # and 1 for others.  Emit a working placeholder and flag it.
                return FieldGuess(field, path, {"from": path, "as_bool": True}, note=_TODO_ACTIVE_LOGIC)
            spec: dict[str, Any] = {
                "from": path,
                "true_values": [_normalise_text(item) for item in values if _truthy(item)] or ["on"],
                "false_values": [_normalise_text(item) for item in values if not _truthy(item)] or ["off"],
            }
            return FieldGuess(field, path, spec, note="")
        return FieldGuess(field, path, {"from": path, "as_bool": True}, note=_TODO_ACTIVE_LOGIC)

    numbers = observations.numbers(path)
    if not numbers:
        return None

    low, high = min(numbers), max(numbers)

    if field == "brightness":
        if not any(start <= low and high <= end for start, end in _BRIGHTNESS_RANGES):
            return None
        spec = {"from": path}
        if high <= 255:
            spec["clamp"] = {"min": 0, "max": 255}
        # A snapshot (or a single sample) cannot distinguish 0-100 from 0-255.
        note = "" if len(numbers) >= 2 and (low, high) != (0.0, 0.0) else _TODO_BRIGHTNESS_RANGE
        return FieldGuess(field, path, spec, note=note)

    if field == "color_temp":
        if high > 1000:  # already Kelvin
            return FieldGuess(
                field, path,
                {"from": path, "input_unit": "kelvin", "output_unit": "kelvin"},
                note="",
            )
        if not (150 <= low and high <= 400):
            return None
        return FieldGuess(
            field, path, {"from": path, "input_unit": "mired"}, note=_TODO_COLOR_TEMP_UNIT
        )

    if field == "position":
        if not (0 <= low and high <= 100):
            return None
        return FieldGuess(field, path, {"from": path, "clamp": {"min": 0, "max": 100}}, note="")

    if field in _FIELD_RANGES:
        floor, ceiling = _FIELD_RANGES[field]
        if floor <= low and high <= ceiling:
            spec = {"from": path}
            if all(float(item).is_integer() for item in numbers):
                spec["round"] = True
            return FieldGuess(field, path, spec, note="")
        # Many Orvibo records report temperature/humidity in tenths of a unit.
        # Recognise that shape and emit a scaled mapping plus a warning.
        tenths_low, tenths_high = floor * 10, ceiling * 10
        if field in ("temperature", "humidity") and tenths_low <= low and high <= tenths_high:
            return FieldGuess(
                field,
                path,
                {"from": path, "scale": {"min": 0, "max": 100, "to_min": 0, "to_max": 10}},
                note=(
                    f"{field} 的观察值（{low:g}~{high:g}）看起来放大了 10 倍，"
                    "已按 ×0.1 写 scale；请确认真机读数与实体显示一致"
                ),
            )
        return None

    return None


def _looks_boolean(values: list[Any]) -> bool:
    if not values or len(values) > 3:
        return False
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value in (0, 1):
            continue
        if isinstance(value, str) and value.strip().lower() in (
            "on", "off", "true", "false", "1", "0", "open", "close", "closed", "yes", "no",
        ):
            continue
        return False
    return True


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    number = _as_number(value)
    if number is not None:
        return number == 0  # built-in convention for value1: 0 == on
    return str(value).strip().lower() in ("on", "true", "open", "yes", "1")


def _normalise_text(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    return value


# ── platform + control scaffolding ────────────────────────────────────────────


def infer_platform(device: Mapping[str, Any], state_fields: Iterable[str]) -> tuple[str, str]:
    """Guess the HA platform from the device record; returns (platform, why)."""

    device_type = _as_int(device.get("deviceType") or device.get("device_type_raw"))
    fields = set(state_fields)

    if "position" in fields and not fields & {"brightness", "color_temp"}:
        return "cover", f"deviceType={device_type} 且只观察到位置类字段"
    if fields & {"brightness", "color_temp"}:
        return "light", f"deviceType={device_type} 且观察到亮度/色温类字段"
    if fields & {"temperature", "humidity", "battery"}:
        return "sensor", f"deviceType={device_type} 且只观察到只读数值类字段"
    if fields & {"state"}:
        return "switch", f"deviceType={device_type} 且只观察到开关状态"
    return "sensor", f"deviceType={device_type} 未观察到可用状态字段，先按只读传感器输出"


def build_control(platform: str, state_fields: Iterable[str]) -> dict[str, Any]:
    """Scaffold a control block for ``platform`` using only verified orders."""

    fields = set(state_fields)
    if platform == "switch":
        return {
            "on": {"order": "set property", "properties": {"onoff": {"status": "on"}}},
            "off": {"order": "set property", "properties": {"onoff": {"status": "off"}}},
        }
    if platform == "light":
        control: dict[str, Any] = {
            "on": {"order": "on", "value1": 0, "value2": "param:brightness"},
            "off": {"order": "off", "value1": 1},
        }
        if "brightness" in fields:
            control["brightness"] = {
                "order": "fast move to level",
                "value2": "param:brightness",
            }
        if "color_temp" in fields:
            control["color_temp"] = {
                "order": "fast color temperature",
                "value2": "param:brightness",
                "value3": "param:color_temp",
            }
        return control
    if platform == "cover":
        control = {"position": {"order": "open", "value1": "param:position"}}
        control["stop"] = {"order": "stop", "value1": 0}
        return control
    return {}


def capabilities_for(platform: str, state_fields: Iterable[str]) -> list[str]:
    """Capabilities to declare, aligned with the emitted action/state blocks."""

    fields = set(state_fields)
    if platform == "switch":
        return ["onoff"]
    if platform == "cover":
        return ["position", "stop"]
    if platform == "light":
        caps = ["onoff"]
        if "brightness" in fields:
            caps.append("brightness")
        if "color_temp" in fields:
            caps.append("color_temp")
        return caps
    return []


# ── skeleton rendering ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Skeleton:
    """A rendered profile plus what the user still has to confirm."""

    yaml_text: str
    profile_id: str
    platform: str
    match: Mapping[str, Any]
    state_fields: tuple[str, ...]
    todo: tuple[str, ...]
    builtin_category: str = ""


def slugify_device_id(name: str, device_type: Any) -> str:
    """Build a valid profile id (``[a-z0-9][a-z0-9_-]*``) from a device name."""

    ascii_name = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    ascii_name = re.sub(r"_+", "_", ascii_name)
    if not ascii_name or not ascii_name[0].isalnum():
        suffix = _as_int(device_type)
        ascii_name = f"device_{suffix}" if suffix is not None else "device"
    return f"custom_{ascii_name}"[:48].strip("_")


def build_match(device: Mapping[str, Any], mode: str = "full") -> dict[str, Any]:
    """Match keys that actually exist on the device record.

    ``mode`` trades specificity against brittleness:

    * ``full``    -- every available identifier (tightest, but a firmware change
      to ``ui_model``/``model`` silently stops the profile matching);
    * ``minimal`` -- ``device_type`` (+ ``sub_device_type``), the most stable
      identifiers; recommended default for a device you own;
    * ``type``    -- ``device_type`` only (broadest).
    """

    match: dict[str, Any] = {}
    device_type = _as_int(device.get("deviceType") or device.get("device_type_raw"))
    if device_type is not None:
        match["device_type"] = device_type
    if mode == "type":
        return match

    sub_type = _as_int(device.get("subDeviceType") or device.get("sub_device_type"))
    if sub_type is not None:
        match["sub_device_type"] = sub_type
    if mode == "minimal":
        return match

    class_id = _as_int(device.get("classId") or device.get("class_id"))
    if class_id is not None:
        match["class_id"] = class_id
    ui = device.get("ui")
    ui_model = device.get("ui_model")
    if not ui_model and isinstance(ui, Mapping):
        ui_model = ui.get("model")
    if isinstance(ui_model, str) and ui_model.strip():
        match["ui_model"] = ui_model.strip()
    model = device.get("model") or device.get("modelName")
    if isinstance(model, str) and model.strip():
        match["model"] = model.strip()
    return match


def build_skeleton(
    device: Mapping[str, Any],
    observations: Observations,
    *,
    platform: Optional[str] = None,
    profile_id: Optional[str] = None,
    builtin_category: str = "",
    builtin_platform: str = "",
    match_mode: str = "minimal",
) -> Skeleton:
    """Render a profile skeleton from the device record and its observations."""

    if match_mode not in ("full", "minimal", "type"):
        raise ValueError(f"unknown match_mode: {match_mode!r}")

    # ``state`` and ``position`` both fall back to value1; the platform decides
    # which one is meaningful, and fields no entity consumes are dropped.
    if platform:
        chosen_platform = platform
    else:
        chosen_platform, _why = infer_platform(device, observations.values)
    allowed = _PLATFORM_FIELDS.get(chosen_platform, ())

    guesses: dict[str, FieldGuess] = {}
    for field_name in allowed:
        guess = guess_field(field_name, observations)
        if guess is not None:
            guesses[field_name] = guess

    # One source path cannot feed two normalized fields.  When several fields
    # fall back to the same slot (e.g. temperature and humidity both landing on
    # ``value2``), keep only the first field the platform lists.
    claimed: dict[str, str] = {}
    dropped: list[str] = []
    for field_name in allowed:
        guess = guesses.get(field_name)
        if guess is None:
            continue
        owner = claimed.get(guess.path)
        if owner is not None:
            dropped.append(
                f"{field_name} 与 {owner} 观察值都落在 {guess.path}，"
                f"已只保留 {owner}；若实际是 {field_name}，请手动改过来"
            )
            guesses.pop(field_name, None)
            continue
        claimed[guess.path] = field_name

    state_fields = tuple(sorted(guesses))
    match = build_match(device, match_mode)
    resolved_id = profile_id or slugify_device_id(
        str(device.get("deviceName") or device.get("name") or "device"),
        match.get("device_type"),
    )
    capabilities = capabilities_for(chosen_platform, state_fields)

    todo: list[str] = ["hardware_verified 保持 false：真机确认控制动作无误后再改成 true"]
    todo.extend(dropped)
    if match_mode == "full" and len(match) > 1:
        extra = ", ".join(sorted(set(match) - {"device_type", "sub_device_type"}))
        todo.append(
            f"match 含 {extra}：字段一旦变化 profile 就不再命中；"
            "只想按类型匹配可以删掉这些条件或重新用 --match minimal 生成"
        )
    needs_override = bool(builtin_category) and builtin_category not in ("unknown", "other")
    if needs_override:
        todo.append(
            f"该设备已被内置分类识别为 {builtin_category}"
            + (f"（平台 {builtin_platform}）" if builtin_platform else "")
            + "：要在实体层接管，需要显式写 override: true，否则本 profile 不生效"
        )
    for guess in guesses.values():
        if guess.note:
            todo.append(guess.note)
    if "state" in guesses and not guesses["state"].spec.get("true_values"):
        todo.append(_TODO_ACTIVE_LOGIC)
    control = build_control(chosen_platform, state_fields)
    if control:
        todo.append(
            f"control 是按 {chosen_platform} 平台猜的骨架，order/value 必须对照真机抓包确认"
        )
    else:
        todo.append("未生成 control：平台/能力不明确，确认真机能力后再补")
    if chosen_platform in ("climate", "fan"):
        todo.append(_TODO_PLATFORM_ENTITIES)
    if not guesses:
        todo.append("没有观察到任何状态字段：请重新用 --listen 抓一次推送（期间操作设备）")

    yaml_text = _render(
        resolved_id=resolved_id,
        device=device,
        platform=chosen_platform,
        match=match,
        guesses=guesses,
        control=control,
        todo=todo,
        observations=observations,
        needs_override=needs_override,
        capabilities=capabilities,
    )
    return Skeleton(
        yaml_text=yaml_text,
        profile_id=resolved_id,
        platform=chosen_platform,
        match=match,
        state_fields=state_fields,
        todo=tuple(todo),
        builtin_category=builtin_category,
    )


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", text) or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _render_spec(spec: Mapping[str, Any], indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for key, value in spec.items():
        if isinstance(value, list):
            rendered = ", ".join(_yaml_scalar(item) for item in value)
            lines.append(f"{pad}{key}: [{rendered}]")
        elif isinstance(value, Mapping):
            rendered = ", ".join(f"{k}: {_yaml_scalar(v)}" for k, v in value.items())
            lines.append(f"{pad}{key}: {{{rendered}}}")
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    return lines


def _render_control(control: Mapping[str, Any], indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for action, body in control.items():
        lines.append(f"{pad}{action}:")
        lines.extend(_render_spec(body, indent + 2))
    return lines


def _render(
    *,
    resolved_id: str,
    device: Mapping[str, Any],
    platform: str,
    match: Mapping[str, Any],
    guesses: Mapping[str, FieldGuess],
    control: Mapping[str, Any],
    todo: list[str],
    observations: Observations,
    needs_override: bool,
    capabilities: Iterable[str],
) -> str:
    name = device.get("deviceName") or device.get("name") or resolved_id
    lines: list[str] = [
        "# 由 tools/generate_device_profile.py 生成的骨架 —— 请逐项确认后再启用",
        "#",
        "# 生成依据：readtable 快照 + 监听期间捕获的状态推送",
        f"# 设备：{name}",
        f"# 样本数：{observations.samples}",
        "#",
        "# ⚠️ 待确认清单（确认一项删一行）：",
    ]
    for item in todo:
        lines.append(f"#   [ ] {item}")
    if observations.paths():
        lines.append("#")
        lines.append("# 观察到的字段路径（次数）：")
        for path in observations.paths():
            values = observations.distinct(path)
            shown = ", ".join(str(item) for item in values[:6])
            more = "…" if len(values) > 6 else ""
            lines.append(
                f"#   {path}  ×{observations.count(path)}  值=[{shown}{more}]"
            )
    lines.extend(
        [
            "",
            "profile_version: 1",
            f"id: {resolved_id}",
            f"display_name: {_yaml_scalar(str(name))}",
            "author: \"\"",
            "notes: |",
            "  由骨架生成器产出，未真机验证。请按文件顶部清单逐项确认。",
            "",
            f"platform: {platform}",
            "",
        ]
    )
    capability_list = list(capabilities)
    if capability_list:
        lines.append("capabilities: [" + ", ".join(capability_list) + "]")
        lines.append("")
    lines.append("match:")
    for key, value in match.items():
        lines.append(f"  {key}: {_yaml_scalar(value)}")
    lines.extend(
        [
            "",
            "override: " + ("true" if needs_override else "false"),
            "hardware_verified: false   # 真机验证通过后再改 true",
            "status_only: " + ("false" if control else "true"),
        ]
    )
    if not match:
        lines.append("# ⚠️ 没有可用的匹配条件：请手动补 match，否则不会命中任何设备")

    lines.append("")
    lines.append("state:")
    if guesses:
        for field_name in sorted(guesses):
            guess = guesses[field_name]
            lines.append(f"  {field_name}:")
            lines.extend(_render_spec(guess.spec, 4))
    else:
        lines.append("  # 未观察到状态字段，示例：")
        lines.append("  # state:")
        lines.append("  #   from: [properties.onoff.status]")
        lines.append("  #   true_values: [\"on\"]")
        lines.append("  #   false_values: [\"off\"]")

    if control:
        lines.append("")
        lines.append("# 下面是按平台猜的命令骨架，order 均在已验证白名单内，")
        lines.append("# 但 value1..4 / properties 的语义必须对照真机抓包确认。")
        lines.append("control:")
        lines.extend(_render_control(control, 2))

    lines.append("")
    return "\n".join(lines) + "\n"


def format_report(skeleton: Skeleton, device: Mapping[str, Any]) -> str:
    """Human-readable summary printed next to the generated file."""

    name = device.get("deviceName") or device.get("name") or skeleton.profile_id
    lines = [
        "",
        "─" * 68,
        f"设备：{name}",
        f"建议 platform：{skeleton.platform}",
        f"匹配条件：{json.dumps(dict(skeleton.match), ensure_ascii=False)}",
    ]
    if skeleton.builtin_category:
        lines.append(f"内置分类：{skeleton.builtin_category}")
    lines.append(f"可写状态字段：{', '.join(skeleton.state_fields) or '（无）'}")
    lines.append("")
    lines.append("还需要你确认：")
    for index, item in enumerate(skeleton.todo, 1):
        lines.append(f"  {index}. {item}")
    lines.append("─" * 68)
    return "\n".join(lines)
