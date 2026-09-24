"""Declarative custom-device profiles.

Allows supporting devices that are not hard-coded in this integration by
dropping a YAML/JSON profile into one of the profile directories:

* ``<HA config>/orvibohomebridge/devices/*.yaml`` (survives HACS updates)
* ``custom_components/orvibohomebridge/custom_devices/*.yaml`` (shipped samples)

Design contract (see ``docs/custom-devices/DESIGN.md``):

* A profile only *declares* capabilities.  The implementation of a capability is
  always an existing, real-device-verified transport (``on``/``off``,
  ``set property``, ``move to level``, ``fast move to level``,
  ``fast color temperature``, ``open``/``stop``).  Profiles can never introduce
  new ``cmd`` numbers or new transports.
* A profile never silently overrides a device that this integration already
  classifies.  Built-in recognition wins unless the profile explicitly sets
  ``override: true``.
* Unverified profiles stay registration-only: entities are created and state is
  parsed, but no control command is ever sent until the author sets
  ``hardware_verified: true``.

This module is intentionally free of Home Assistant imports so it can be unit
tested without a Home Assistant installation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

_LOGGER = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 1

#: Profile file suffixes scanned in the profile directories.
PROFILE_SUFFIXES = (".yaml", ".yml", ".json")

#: Directory name used inside the Home Assistant configuration directory.
CONFIG_DIR_NAME = "orvibohomebridge"
CONFIG_DIR_SUBDIR = "devices"

#: Directory shipped inside the component (for maintainer-provided samples).
PACKAGE_DIR_NAME = "custom_devices"

#: Category label prefix used to keep custom categories in their own namespace.
CATEGORY_PREFIX = "custom:"

#: Home Assistant platforms a profile may target.
KNOWN_PLATFORMS = frozenset(
    {"light", "switch", "cover", "sensor", "binary_sensor", "climate", "fan"}
)

#: Declarative state fields a profile may write.  These are the normalized field
#: names the existing Home Assistant platforms already read.
KNOWN_STATE_FIELDS = frozenset(
    {
        "state",
        "brightness",
        "color_temp",
        "position",
        "temperature",
        "humidity",
        "battery",
        "angle",
    }
)

#: Control actions a profile may declare, mapped to the platforms that use them.
KNOWN_CONTROL_ACTIONS = frozenset(
    {
        "on",
        "off",
        "brightness",
        "color_temp",
        "position",
        "stop",
        "open",
        "close",
    }
)

#: Transport commands verified against real devices.  ``order`` strings outside
#: this set are rejected so a profile cannot invent protocol semantics.
KNOWN_ORDERS = frozenset(
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

#: Parameter names available to ``from: param`` inside a control action.
_KNOWN_PARAMS = frozenset(
    {"brightness", "color_temp", "position", "value", "on", "off"}
)


class ProfileError(ValueError):
    """Raised when a profile file is syntactically or semantically invalid."""


def _normalise_action_key(key: Any) -> str:
    """Map YAML 1.1 boolean keys back to their protocol spelling.

    ``on:`` / ``off:`` / ``yes:`` / ``no:`` are parsed as booleans by
    ``yaml.safe_load`` (YAML 1.1), which would silently turn the action names
    ``on``/``off`` into ``True``/``False``.  Users write the obvious thing, so
    normalise instead of failing.
    """

    if key is True:
        return "on"
    if key is False:
        return "off"
    return str(key).strip().lower().replace(" ", "_")


#: Spellings accepted for the two power actions, normalised to on/off.
_ACTION_ALIASES = {
    "turn_on": "on",
    "turn_off": "off",
    "power_on": "on",
    "power_off": "off",
    "yes": "on",
    "no": "off",
    "true": "on",
    "false": "off",
}


def _normalise_literal(value: Any) -> str:
    """Render a YAML scalar as the string a device payload would carry."""

    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "none"
    return str(value).strip().lower()


#: YAML 1.1 parses bare ``on``/``off``/``yes``/``no`` as booleans, so a user
#: writing ``true_values: [on]`` ends up with ``True``.  Accept both spellings
#: rather than silently never matching the payload's ``"on"``.
_LITERAL_ALIASES: dict[str, tuple[str, ...]] = {
    "true": ("true", "on", "yes", "open", "1"),
    "false": ("false", "off", "no", "close", "closed", "0"),
}


def _literal_variants(value: Any) -> tuple[str, ...]:
    literal = _normalise_literal(value)
    return _LITERAL_ALIASES.get(literal, (literal,))


def _variants(raw: Any) -> tuple[str, ...]:
    """Expand a ``true_values``/``false_values`` list, including YAML aliases."""

    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    expanded: list[str] = []
    for item in raw:
        expanded.extend(_literal_variants(item))
    return tuple(dict.fromkeys(expanded))


def _normalise_properties(raw: Any, *, path: str) -> Optional[dict[str, Any]]:
    """Normalise a ``properties`` payload emitted by a profile.

    YAML 1.1 parses a bare ``on``/``off`` as a boolean, so the obvious

        properties: {onoff: {status: on}}

    would otherwise put ``True`` on the wire where the protocol expects the
    string ``"on"``.  Booleans under an ``onoff.status``-style path are spelled
    ``on``/``off``; other booleans stay booleans.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ProfileError(f"{path}: properties 必须是映射")

    normalised: dict[str, Any] = {}
    for key, value in raw.items():
        key_text = str(key)
        if isinstance(value, Mapping):
            normalised[key_text] = _normalise_properties(
                value, path=f"{path}.{key_text}"
            )
        elif isinstance(value, bool) and _is_onoff_path(key_text):
            normalised[key_text] = "on" if value else "off"
        else:
            normalised[key_text] = value
    return normalised


def _is_onoff_path(key: str) -> bool:
    leaf = key.lower()
    return leaf in ("status", "onoff", "onoff_status", "state")


_PARAM_PLACEHOLDER = "param:"


def _interpolate_params(value: Any, params: Mapping[str, Any]) -> Any:
    """Substitute ``"param:<name>"`` placeholders inside a properties payload.

    ``control.<action>.properties`` is static by design, but the obvious spelling
    for a dynamic value is a placeholder, and previously that spelling silently
    put the literal string ``"param:brightness"`` on the wire.
    """

    if isinstance(value, str) and value.startswith(_PARAM_PLACEHOLDER):
        name = value[len(_PARAM_PLACEHOLDER) :].strip()
        resolved = params.get(name)
        return resolved if resolved is not None else value
    if isinstance(value, Mapping):
        return {
            key: _interpolate_params(item, params) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_interpolate_params(item, params) for item in value]
    return value


#: Match key → normalized device-dict field, then raw readtable field name.
_DEVICE_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "device_type": ("device_type_raw", "deviceType"),
    "sub_device_type": ("sub_device_type", "subDeviceType"),
    "class_id": ("class_id", "classId"),
    "status_type": ("status_type", "statusType"),
}


def _device_field(device: Mapping[str, Any], key: str) -> Any:
    """Read a match key from the normalized device dict (or raw record)."""

    for name in _DEVICE_FIELD_ALIASES.get(key, (key,)):
        value = device.get(name)
        if value is not None and value != "":
            return value
    return None


# --------------------------------------------------------------------------- #
# Value resolution
# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return None
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "on", "yes", "open", "detected"):
            return True
        if lowered in ("0", "false", "off", "no", "close", "closed", "idle"):
            return False
    return None


def _lookup_path(container: Any, path: str) -> Any:
    """Read ``a.b.c`` from nested mappings, returning None when absent.

    A path segment may address a list index (``deviceStatus.0.value1``).
    """

    if not isinstance(path, str) or not path:
        return None
    current = container
    for segment in path.split("."):
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(segment)
            continue
        if isinstance(current, (list, tuple)):
            index = _as_int(segment)
            if index is None or index < 0 or index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _deep_get(container: Any, path: str) -> Any:
    """Path reader that resolves a bare key against top level and properties."""

    value = _lookup_path(container, path)
    if value is not None:
        return value
    if isinstance(container, Mapping):
        properties = container.get("properties")
        if isinstance(properties, Mapping):
            return _lookup_path(properties, path)
    return None


#: Sentinel distinguishing "no constant declared" from an explicit ``false``/``null``.
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ValueSpec:
    """One ``from``/``scale``/``map`` rule turning raw payload into a value."""

    from_paths: tuple[str, ...] = ()
    constant: Any = _MISSING
    scale: Optional[tuple[float, float, float, float]] = None
    value_map: Optional[Mapping[str, Any]] = None
    input_unit: Optional[str] = None  # "mired" | "kelvin"
    output_unit: Optional[str] = None  # "kelvin" | "mired"
    clamp: Optional[tuple[float, float]] = None
    round_to_int: bool = True
    true_values: tuple[str, ...] = ()
    false_values: tuple[str, ...] = ()
    as_bool: bool = False
    default: Any = None
    param: Optional[str] = None

    def resolve(self, raw: Mapping[str, Any], params: Mapping[str, Any] | None = None) -> Any:
        """Resolve this spec against a raw payload and optional call params.

        ``None`` always means "no value" (the caller then keeps the field
        untouched).  A spec may still declare ``value: null`` to reset a field.
        """

        found = False
        value: Any = None
        for path in self.from_paths:
            value = _deep_get(raw, path)
            if value is not None:
                found = True
                break

        if not found and self.param is not None:
            value = (params or {}).get(self.param)
            found = value is not None
        if not found and self.constant is not _MISSING:
            value = self.constant
            found = True
        if not found:
            return self.default
        if isinstance(value, Mapping):
            # Property payloads such as {"percent": 60} or {"status": "on"}.
            for key in ("value", "status", "percent", "level", "brightness"):
                if key in value:
                    value = value[key]
                    break
            else:
                return self.default

        if self.as_bool or self.true_values or self.false_values:
            if self.true_values or self.false_values:
                text = _normalise_literal(value)
                if text in self.true_values:
                    return True
                if text in self.false_values:
                    return False
                return self.default
            return _as_bool(value)

        if isinstance(value, bool):
            # A boolean is already a resolved field value (``false`` is a value,
            # not "absent"); never push it through numeric coercion.
            return value

        if self.value_map is not None:
            key = _normalise_literal(value)
            if key not in self.value_map:
                return self.default
            mapped = self.value_map[key]
            if isinstance(mapped, bool) or not isinstance(mapped, (int, float)):
                # A map hit is a final mapping (booleans, strings, enums); it is
                # not a number to scale/clamp/round.
                return mapped
            value = mapped

        number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if number is None:
            number = _as_int(value)
        if number is None:
            return self.default

        if self.input_unit == "mired" and self.output_unit == "kelvin":
            if number <= 0:
                return self.default
            number = round(1_000_000 / number)
        elif self.input_unit == "kelvin" and self.output_unit == "mired":
            if number <= 0:
                return self.default
            number = round(1_000_000 / number)

        if self.scale is not None:
            src_min, src_max, dst_min, dst_max = self.scale
            if src_max == src_min:
                return self.default
            ratio = (number - src_min) / (src_max - src_min)
            number = dst_min + ratio * (dst_max - dst_min)

        if self.clamp is not None:
            number = max(self.clamp[0], min(self.clamp[1], number))

        if self.round_to_int:
            number = int(round(number))
        return number


def _parse_value_spec(
    raw: Any,
    *,
    path: str,
    field_name: str,
    extra_params: Iterable[str] = (),
) -> ValueSpec:
    """Parse a value spec from either a scalar shorthand or a mapping."""

    if isinstance(raw, (str, int, float, bool)):
        text = str(raw)
        if isinstance(raw, str) and text.startswith("param:"):
            param = text.split(":", 1)[1].strip()
            if param not in set(extra_params) and param not in _KNOWN_PARAMS:
                raise ProfileError(f"{path}: 未知参数名 param:{param}")
            return ValueSpec(param=param)
        return ValueSpec(constant=raw)

    if not isinstance(raw, Mapping):
        raise ProfileError(f"{path}: {field_name} 必须是标量或映射")

    from_paths: tuple[str, ...] = ()
    if "from" in raw:
        from_paths = _as_path_tuple(raw["from"], f"{path}.from")

    value_map = raw.get("map")
    if value_map is not None and not isinstance(value_map, Mapping):
        raise ProfileError(f"{path}.map 必须是映射")
    if isinstance(value_map, Mapping):
        # YAML 1.1 turns bare on/off/yes/no keys into booleans; keep them
        # addressable as the strings users wrote in their payloads.
        value_map = {_normalise_literal(key): item for key, item in value_map.items()}

    scale = None
    if "scale" in raw:
        scale_raw = raw["scale"]
        if not isinstance(scale_raw, Mapping):
            raise ProfileError(f"{path}.scale 必须是映射，例如 {{min: 0, max: 255}}")
        try:
            src_min = float(scale_raw.get("min", 0))
            src_max = float(scale_raw.get("max", 255))
            dst_min = float(scale_raw.get("to_min", src_min))
            dst_max = float(scale_raw.get("to_max", src_max))
        except (TypeError, ValueError) as err:
            raise ProfileError(f"{path}.scale 数值无效: {err}") from err
        scale = (src_min, src_max, dst_min, dst_max)

    clamp = None
    if "clamp" in raw:
        clamp_raw = raw["clamp"]
        if isinstance(clamp_raw, Mapping):
            clamp = (
                float(clamp_raw.get("min", float("-inf"))),
                float(clamp_raw.get("max", float("inf"))),
            )
        elif isinstance(clamp_raw, (list, tuple)) and len(clamp_raw) == 2:
            clamp = (float(clamp_raw[0]), float(clamp_raw[1]))
        else:
            raise ProfileError(f"{path}.clamp 必须是 {{min, max}} 或 [min, max]")

    input_unit = raw.get("input_unit") or raw.get("unit")
    output_unit = raw.get("output_unit")
    if input_unit not in (None, "mired", "kelvin"):
        raise ProfileError(f"{path}.input_unit 只能是 mired 或 kelvin")
    if output_unit not in (None, "mired", "kelvin"):
        raise ProfileError(f"{path}.output_unit 只能是 mired 或 kelvin")
    if input_unit == "mired" and output_unit is None:
        output_unit = "kelvin"
    if input_unit == "kelvin" and output_unit is None:
        output_unit = "mired"

    param = raw.get("param")
    if param is not None:
        param = str(param).strip()
        if param not in set(extra_params) and param not in _KNOWN_PARAMS:
            raise ProfileError(f"{path}.param: 未知参数名 {param}")

    constant = raw["value"] if "value" in raw else _MISSING
    if not from_paths and param is None and constant is _MISSING and "default" not in raw:
        raise ProfileError(
            f"{path}: 需要 from / value / param / default 中的至少一项"
        )

    return ValueSpec(
        from_paths=from_paths,
        constant=constant,
        scale=scale,
        value_map=value_map,
        input_unit=input_unit,
        output_unit=output_unit,
        clamp=clamp,
        round_to_int=bool(raw.get("round", True)),
        true_values=_variants(raw.get("true_values", ())),
        false_values=_variants(raw.get("false_values", ())),
        as_bool=bool(raw.get("as_bool", False)),
        default=raw.get("default"),
        param=param,
    )


def _as_path_tuple(raw: Any, label: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        paths = (raw,)
    elif isinstance(raw, (list, tuple)):
        paths = tuple(str(item) for item in raw)
    else:
        raise ProfileError(f"{label} 必须是字符串或字符串列表")
    if not paths:
        raise ProfileError(f"{label} 不能为空")
    return paths


# --------------------------------------------------------------------------- #
# Match rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MatchSpec:
    """All conditions must hold for at least one ``any_of`` branch."""

    priority: int = 0
    any_of: tuple[Mapping[str, Any], ...] = ()
    override: bool = False

    def matches(self, device: Mapping[str, Any]) -> bool:
        if not self.any_of:
            return False
        return any(_branch_matches(branch, device) for branch in self.any_of)


def _branch_matches(branch: Mapping[str, Any], device: Mapping[str, Any]) -> bool:
    for key, expected in branch.items():
        if key in ("device_type", "sub_device_type", "class_id", "status_type"):
            actual = _as_int(_device_field(device, key))
            if actual is None:
                return False
            candidates = (
                [_as_int(item) for item in expected]
                if isinstance(expected, (list, tuple, set))
                else [_as_int(expected)]
            )
            if actual not in [item for item in candidates if item is not None]:
                return False
        elif key == "ui_model":
            actual = device.get("ui_model") or ""
            if not _text_matches(str(actual), expected):
                return False
        elif key == "model":
            actual = device.get("model") or ""
            if not _text_matches(str(actual), expected):
                return False
        elif key == "class_name":
            actual = device.get("class_name") or ""
            if not _text_matches(str(actual), expected):
                return False
        elif key == "property_present":
            for path in expected if isinstance(expected, (list, tuple, set)) else [expected]:
                if _deep_get(device, str(path)) is None:
                    return False
        elif key == "property_equals":
            if not isinstance(expected, Mapping):
                return False
            for path, want in expected.items():
                got = _deep_get(device, str(path))
                if isinstance(got, Mapping):
                    got = got.get("status", got.get("value", got))
                if str(got) != str(want):
                    return False
        elif key == "product_name":
            actual = device.get("product_name") or device.get("device_name") or ""
            if not _text_matches(str(actual), expected):
                return False
        else:
            # Unknown keys are rejected at parse time; ignore defensively.
            return False
    return True


def _text_matches(actual: str, expected: Any) -> bool:
    candidates = expected if isinstance(expected, (list, tuple, set)) else [expected]
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if candidate.startswith("re:"):
            pattern = candidate[3:]
            try:
                if re.search(pattern, actual):
                    return True
            except re.error as err:
                raise ProfileError(f"无效正则 {pattern!r}: {err}") from err
        elif candidate == actual:
            return True
    return False


_KNOWN_MATCH_KEYS = frozenset(
    {
        "device_type",
        "sub_device_type",
        "class_id",
        "status_type",
        "ui_model",
        "model",
        "class_name",
        "product_name",
        "property_present",
        "property_equals",
    }
)


def _parse_match(raw: Any, *, path: str, override: bool) -> MatchSpec:
    if not isinstance(raw, Mapping):
        raise ProfileError(f"{path}: match 必须是映射")

    priority = 0
    if "priority" in raw:
        priority = _as_int(raw["priority"])
        if priority is None:
            raise ProfileError(f"{path}.priority 必须是整数")

    branches_raw: list[Any] = []
    if "any_of" in raw:
        any_of = raw["any_of"]
        if not isinstance(any_of, (list, tuple)):
            raise ProfileError(f"{path}.any_of 必须是列表")
        branches_raw.extend(any_of)

    # Allow the flat shorthand: match: {device_type: 507, sub_device_type: 12}
    flat = {
        key: value
        for key, value in raw.items()
        if key not in ("priority", "any_of", "override")
    }
    if flat:
        branches_raw.append(flat)

    if not branches_raw:
        raise ProfileError(f"{path}: match 至少需要一个条件")

    branches: list[Mapping[str, Any]] = []
    for index, branch in enumerate(branches_raw):
        if not isinstance(branch, Mapping) or not branch:
            raise ProfileError(f"{path}.any_of[{index}] 必须是非空映射")
        for key, value in branch.items():
            if key not in _KNOWN_MATCH_KEYS:
                raise ProfileError(
                    f"{path}.any_of[{index}]: 未知匹配键 {key!r}，"
                    f"可用键：{', '.join(sorted(_KNOWN_MATCH_KEYS))}"
                )
            if key in ("ui_model", "model", "class_name", "product_name"):
                # Validate regular expressions eagerly.
                _text_matches("", value)
        branches.append(dict(branch))

    return MatchSpec(priority=priority or 0, any_of=tuple(branches), override=override)


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ControlAction:
    """One declared control action compiled into an envelope."""

    order: str
    values: Mapping[str, ValueSpec] = field(default_factory=dict)
    properties: Optional[Mapping[str, Any]] = None

    def envelope(
        self,
        raw: Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
    ) -> tuple[str, tuple[int, int, int, int], Optional[dict[str, Any]]]:
        numbers = [0, 0, 0, 0]
        for key, spec in self.values.items():
            index = _as_int(key.replace("value", ""))
            if index is None or not 1 <= index <= 4:
                continue
            resolved = spec.resolve(raw, params)
            numbers[index - 1] = _as_int(resolved) or 0
        properties = (
            _interpolate_params(dict(self.properties), params or {})
            if self.properties
            else None
        )
        return self.order, (numbers[0], numbers[1], numbers[2], numbers[3]), properties


@dataclass(frozen=True, slots=True)
class CustomProfile:
    """A validated custom-device profile."""

    profile_id: str
    display_name: str
    platform: str
    match: MatchSpec
    capabilities: frozenset[str]
    state_specs: Mapping[str, ValueSpec]
    control: Mapping[str, ControlAction]
    hardware_verified: bool
    hidden: bool
    status_only: bool
    cloud_only: bool
    channels: frozenset[str]
    optimistic: Mapping[str, Mapping[str, ValueSpec]]
    constraints: Mapping[str, Any]
    state_overrides: Mapping[str, Any]
    source: str
    author: str = ""
    notes: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def category_key(self) -> str:
        """Stable key used to route parsers and control for this profile."""

        return f"{CATEGORY_PREFIX}{self.profile_id}"

    @property
    def is_controllable(self) -> bool:
        return bool(self.control) and not self.status_only

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def state_parser(self):
        """Return a ``StateParser``-compatible callable."""

        from .custom_control import build_state_parser

        return build_state_parser(self)

    def describe(self) -> str:
        return (
            f"{self.display_name} [{self.profile_id}] "
            f"platform={self.platform} "
            f"channels={','.join(sorted(self.channels)) or '-'} "
            f"verified={'yes' if self.hardware_verified else 'no'} "
            f"source={self.source}"
        )


def _parse_profile(payload: Any, *, source: str) -> CustomProfile:
    if not isinstance(payload, Mapping):
        raise ProfileError(f"{source}: 文件顶层必须是映射")

    version = _as_int(payload.get("profile_version", payload.get("version")))
    if version != PROFILE_SCHEMA_VERSION:
        raise ProfileError(
            f"{source}: profile_version 必须为 {PROFILE_SCHEMA_VERSION}（当前 {version!r}）"
        )

    profile_id = payload.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ProfileError(f"{source}: 缺少 id")
    profile_id = profile_id.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", profile_id):
        raise ProfileError(
            f"{source}: id 只能包含小写字母、数字、下划线和连字符（当前 {profile_id!r}）"
        )

    platform = payload.get("platform")
    if not isinstance(platform, str) or platform not in KNOWN_PLATFORMS:
        raise ProfileError(
            f"{source}: platform 必须是 {', '.join(sorted(KNOWN_PLATFORMS))} 之一"
        )

    match = _parse_match(
        payload.get("match"), path=f"{source}:match", override=bool(payload.get("override"))
    )

    capabilities = payload.get("capabilities") or []
    if isinstance(capabilities, str):
        capabilities = [capabilities]
    if not isinstance(capabilities, (list, tuple)):
        raise ProfileError(f"{source}: capabilities 必须是列表")

    state_raw = payload.get("state") or {}
    if not isinstance(state_raw, Mapping):
        raise ProfileError(f"{source}: state 必须是映射")
    state_specs: dict[str, ValueSpec] = {}
    for name, spec_raw in state_raw.items():
        if name not in KNOWN_STATE_FIELDS:
            raise ProfileError(
                f"{source}: state.{name} 不是受支持的字段，"
                f"可用字段：{', '.join(sorted(KNOWN_STATE_FIELDS))}"
            )
        state_specs[name] = _parse_value_spec(
            spec_raw, path=f"{source}:state.{name}", field_name=str(name)
        )

    control_raw = payload.get("control") or {}
    if not isinstance(control_raw, Mapping):
        raise ProfileError(f"{source}: control 必须是映射")
    control: dict[str, ControlAction] = {}
    for action, action_raw in control_raw.items():
        action_name = _normalise_action_key(action)
        action_name = _ACTION_ALIASES.get(action_name, action_name)
        if action_name not in KNOWN_CONTROL_ACTIONS:
            raise ProfileError(
                f"{source}: control.{action!r} 不是受支持的动作，"
                f"可用动作：{', '.join(sorted(KNOWN_CONTROL_ACTIONS))}"
            )
        control[action_name] = _parse_control_action(
            action_raw, path=f"{source}:control.{action_name}"
        )

    status_only = bool(payload.get("status_only", False)) or not control
    cloud_only = bool(payload.get("cloud_only", False))
    hardware_verified = bool(payload.get("hardware_verified", False))

    channels_raw = payload.get("channels")
    if channels_raw is None:
        channels = frozenset({"ssl"} if cloud_only else {"lan", "ssl"})
    else:
        if isinstance(channels_raw, str):
            channels_raw = [channels_raw]
        if not isinstance(channels_raw, (list, tuple)):
            raise ProfileError(f"{source}: channels 必须是列表")
        normalised = {str(item).strip().lower() for item in channels_raw}
        unknown = normalised - {"lan", "ssl"}
        if unknown:
            raise ProfileError(
                f"{source}: channels 含未知通道 {', '.join(sorted(unknown))}（仅支持 lan/ssl）"
            )
        channels = frozenset(normalised)
    if cloud_only:
        channels = frozenset({"ssl"})

    optimistic_raw = payload.get("optimistic") or {}
    if not isinstance(optimistic_raw, Mapping):
        raise ProfileError(f"{source}: optimistic 必须是映射")
    optimistic: dict[str, Mapping[str, ValueSpec]] = {}
    for action, fields_raw in optimistic_raw.items():
        action_name = _normalise_action_key(action)
        action_name = _ACTION_ALIASES.get(action_name, action_name)
        if action_name not in KNOWN_CONTROL_ACTIONS:
            raise ProfileError(
                f"{source}: optimistic.{action!r} 不是受支持的动作"
            )
        if not isinstance(fields_raw, Mapping):
            raise ProfileError(f"{source}: optimistic.{action_name} 必须是映射")
        resolved: dict[str, ValueSpec] = {}
        for field_name, spec_raw in fields_raw.items():
            field_key = _normalise_action_key(field_name)
            if field_key not in KNOWN_STATE_FIELDS:
                raise ProfileError(
                    f"{source}: optimistic.{action_name}.{field_name} 不是受支持的字段"
                )
            resolved[field_key] = _parse_value_spec(
                spec_raw,
                path=f"{source}:optimistic.{action_name}.{field_key}",
                field_name=field_key,
                extra_params=_KNOWN_PARAMS,
            )
        optimistic[action_name] = resolved

    constraints_raw = payload.get("constraints") or {}
    if not isinstance(constraints_raw, Mapping):
        raise ProfileError(f"{source}: constraints 必须是映射")
    constraints: dict[str, Any] = {}
    for key in ("brightness_range", "color_temp_range"):
        if key not in constraints_raw:
            continue
        value = constraints_raw[key]
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ProfileError(f"{source}: constraints.{key} 必须是 [min, max]")
        low, high = _as_int(value[0]), _as_int(value[1])
        if low is None or high is None or low >= high:
            raise ProfileError(f"{source}: constraints.{key} 范围无效")
        constraints[key] = (low, high)

    warnings: list[str] = []
    if not hardware_verified and control:
        warnings.append(
            "hardware_verified 未设置：实体只读，控制命令不会下发（真机验证后请设为 true）"
        )
    if not state_specs:
        warnings.append("未声明 state 映射：实体状态只能来自云端快照")

    return CustomProfile(
        profile_id=profile_id,
        display_name=str(payload.get("display_name") or profile_id),
        platform=platform,
        match=match,
        capabilities=frozenset(str(item) for item in capabilities),
        state_specs=state_specs,
        control=control,
        hardware_verified=hardware_verified,
        hidden=bool(payload.get("hidden", False)),
        status_only=status_only,
        cloud_only=cloud_only,
        channels=channels,
        optimistic=optimistic,
        constraints=constraints,
        state_overrides=_parse_state_overrides(payload.get("state_defaults"), source),
        source=source,
        author=str(payload.get("author") or ""),
        notes=str(payload.get("notes") or ""),
        warnings=tuple(warnings),
    )


def _parse_state_overrides(raw: Any, source: str) -> Mapping[str, Any]:
    """Optional ``state_defaults`` merged into initial state (e.g. online)."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ProfileError(f"{source}: state_defaults 必须是映射")
    for key in raw:
        if key not in KNOWN_STATE_FIELDS and key != "online":
            raise ProfileError(f"{source}: state_defaults.{key} 不是受支持的字段")
    return dict(raw)


def _parse_control_action(raw: Any, *, path: str) -> ControlAction:
    if isinstance(raw, str):
        raw = {"order": raw}
    if not isinstance(raw, Mapping):
        raise ProfileError(f"{path}: 必须是映射或命令字符串")

    order = raw.get("order")
    # ``order: on`` / ``order: off`` are YAML 1.1 booleans, not strings.
    if isinstance(order, bool):
        order = "on" if order else "off"
    if isinstance(order, str):
        order = order.strip()
    if not isinstance(order, str) or order not in KNOWN_ORDERS:
        raise ProfileError(
            f"{path}: order 必须是已验证命令之一：{', '.join(sorted(KNOWN_ORDERS))}"
        )

    properties = raw.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        raise ProfileError(f"{path}: properties 必须是映射")
    if properties and order != "set property":
        raise ProfileError(f"{path}: properties 只能与 order='set property' 搭配")
    normalised_properties = (
        _normalise_properties(properties, path=f"{path}.properties")
        if properties
        else None
    )

    values: dict[str, ValueSpec] = {}
    for key, value_raw in raw.items():
        if key in ("order", "properties"):
            continue
        if not re.fullmatch(r"value[1-4]", str(key)):
            raise ProfileError(f"{path}: 未知字段 {key!r}（只允许 value1~value4/properties）")
        values[str(key)] = _parse_value_spec(
            value_raw,
            path=f"{path}.{key}",
            field_name=str(key),
            extra_params=_KNOWN_PARAMS,
        )
    return ControlAction(order=order, values=values, properties=normalised_properties)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def default_profile_dirs(hass_config_dir: str | Path | None = None) -> tuple[Path, ...]:
    """Return the profile directories in precedence order (later wins)."""

    dirs: list[Path] = [Path(__file__).with_name(PACKAGE_DIR_NAME)]
    if hass_config_dir:
        dirs.append(Path(hass_config_dir) / CONFIG_DIR_NAME / CONFIG_DIR_SUBDIR)
    else:
        default_config = _default_hass_config_dir()
        if default_config is not None:
            dirs.append(default_config / CONFIG_DIR_NAME / CONFIG_DIR_SUBDIR)
    return tuple(dirs)


def _default_hass_config_dir() -> Optional[Path]:
    try:  # pragma: no cover - depends on the host installation
        from homeassistant.config import get_default_config_dir  # type: ignore

        return Path(get_default_config_dir())
    except Exception:  # noqa: BLE001
        return None


def iter_profile_files(directories: Iterable[Path]) -> list[Path]:
    """List profile files from the given directories, sorted for determinism."""

    files: list[Path] = []
    for directory in directories:
        try:
            path = Path(directory)
        except TypeError:
            continue
        if not path.is_dir():
            continue
        for child in sorted(path.iterdir()):
            if child.is_file() and child.suffix.lower() in PROFILE_SUFFIXES:
                files.append(child)
    return files


def load_profile_file(path: Path) -> CustomProfile:
    """Load and validate a single profile file."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise ProfileError(f"{path}: 无法读取（{err}）") from err

    try:
        if Path(path).suffix.lower() == ".json":
            payload = json.loads(text)
        else:
            payload = yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as err:
        raise ProfileError(f"{path}: YAML/JSON 解析失败（{err}）") from err

    return _parse_profile(payload, source=str(path))


class CustomDeviceRegistry:
    """Holds the active custom-device profiles and resolves devices to them."""

    def __init__(self) -> None:
        self._profiles: dict[str, CustomProfile] = {}
        self._ordered: tuple[CustomProfile, ...] = ()
        self._errors: list[str] = []
        self._skip_reasons: dict[str, str] = {}
        self._match_counts: dict[str, int] = {}
        self._match_cache: dict[tuple[int, bool], Optional[CustomProfile]] = {}
        self._directories: tuple[Path, ...] = ()

    # -- lifecycle -------------------------------------------------------- #

    def load(
        self,
        directories: Iterable[str | Path] | None = None,
        *,
        hass_config_dir: str | Path | None = None,
    ) -> None:
        """(Re)load every profile file.  Invalid files never break startup."""

        if directories is None:
            resolved = default_profile_dirs(hass_config_dir)
        else:
            resolved = tuple(Path(item) for item in directories)

        self._directories = resolved
        profiles: dict[str, CustomProfile] = {}
        errors: list[str] = []
        skip_reasons: dict[str, str] = {}

        for file_path in iter_profile_files(resolved):
            try:
                profile = load_profile_file(file_path)
            except ProfileError as err:
                errors.append(str(err))
                continue
            except Exception as err:  # noqa: BLE001 - never break integration startup
                errors.append(f"{file_path}: 未预期的加载错误（{err}）")
                continue

            existing = profiles.get(profile.profile_id)
            if existing is not None:
                if not profile.match.override:
                    reason = (
                        f"id 重复：已由 {existing.source} 定义；"
                        "如需覆盖请在该 profile 设置 override: true"
                    )
                    skip_reasons[str(file_path)] = reason
                    errors.append(f"{file_path}: {reason}")
                    continue
                _LOGGER.warning(
                    "自定义设备 profile %s 覆盖了 %s 的定义", profile.profile_id, existing.source
                )
            profiles[profile.profile_id] = profile
            for warning in profile.warnings:
                _LOGGER.warning("自定义设备 profile %s：%s", profile.profile_id, warning)

        self._profiles = profiles
        self._ordered = tuple(
            sorted(
                profiles.values(),
                key=lambda item: (-item.match.priority, item.profile_id),
            )
        )
        self._errors = errors
        self._skip_reasons = skip_reasons
        self._match_counts = {}
        self._match_cache = {}

        if self._ordered:
            _LOGGER.info(
                "已加载 %s 个自定义设备 profile（目录：%s）",
                len(self._ordered),
                ", ".join(str(item) for item in resolved) or "-",
            )
        for error in errors:
            _LOGGER.error("自定义设备 profile 加载失败：%s", error)

    # -- inspection ------------------------------------------------------- #

    @property
    def profiles(self) -> tuple[CustomProfile, ...]:
        return self._ordered

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    @property
    def directories(self) -> tuple[Path, ...]:
        return self._directories

    @property
    def match_counts(self) -> Mapping[str, int]:
        return dict(self._match_counts)

    def get(self, profile_id: str) -> Optional[CustomProfile]:
        return self._profiles.get(profile_id)

    def by_category_key(self, key: Any) -> Optional[CustomProfile]:
        if not isinstance(key, str):
            key = getattr(key, "value", None)
        if not isinstance(key, str) or not key.startswith(CATEGORY_PREFIX):
            return None
        return self._profiles.get(key[len(CATEGORY_PREFIX) :])

    def record_match(self, profile_id: str) -> None:
        self._match_counts[profile_id] = self._match_counts.get(profile_id, 0) + 1

    def diagnostics(self) -> list[dict[str, Any]]:
        """Structured report for the diagnostics download and the options flow."""

        report: list[dict[str, Any]] = []
        for profile in self._ordered:
            report.append(
                {
                    "id": profile.profile_id,
                    "display_name": profile.display_name,
                    "platform": profile.platform,
                    "priority": profile.match.priority,
                    "override": profile.match.override,
                    "hardware_verified": profile.hardware_verified,
                    "status_only": profile.status_only,
                    "cloud_only": profile.cloud_only,
                    "channels": sorted(profile.channels),
                    "capabilities": sorted(profile.capabilities),
                    "state_fields": sorted(profile.state_specs),
                    "control_actions": sorted(profile.control),
                    "matched_devices": self._match_counts.get(profile.profile_id, 0),
                    "warnings": list(profile.warnings),
                    "source": profile.source,
                }
            )
        return report

    # -- resolution ------------------------------------------------------- #

    def find_profile(
        self,
        device: Mapping[str, Any],
        *,
        builtin_recognised: bool | None = None,
        device_id: str = "",
    ) -> Optional[CustomProfile]:
        """Return the profile that claims this device, honouring override rules.

        ``builtin_recognised`` may be supplied by the caller (which already
        classified the device); when omitted it is derived here, so the public
        helpers can never accidentally bypass the override guard.

        Results are memoised per loaded generation: classification and
        capability resolution both ask for the same device, and the match
        counter must reflect devices, not lookups.
        """

        if not self._ordered or not isinstance(device, Mapping):
            return None
        if builtin_recognised is None:
            builtin_recognised = _derive_builtin_recognised(device)
        cache_key = (device.get("device_id") or device.get("uid") or id(device), builtin_recognised)
        if cache_key in self._match_cache:
            return self._match_cache[cache_key]

        found: Optional[CustomProfile] = None
        for profile in self._ordered:
            if not profile.match.matches(device):
                continue
            if builtin_recognised and not profile.match.override:
                _LOGGER.info(
                    "自定义 profile %s 命中已被内置识别的设备 %s，"
                    "未设置 override: true，已跳过（内置行为保持不变）",
                    profile.profile_id,
                    device_id or device.get("device_id") or "?",
                )
                continue
            self.record_match(profile.profile_id)
            found = profile
            break

        self._match_cache[cache_key] = found
        return found


#: Process-wide registry used by the platform/parsing/control layers.
_REGISTRY = CustomDeviceRegistry()


def registry() -> CustomDeviceRegistry:
    return _REGISTRY


def load_profiles(
    directories: Iterable[str | Path] | None = None,
    *,
    hass_config_dir: str | Path | None = None,
) -> CustomDeviceRegistry:
    """Reload the process-wide registry and return it."""

    _REGISTRY.load(directories, hass_config_dir=hass_config_dir)
    return _REGISTRY


def load_custom_profiles(hass: Any) -> CustomDeviceRegistry:
    """(Re)load profiles using a Home Assistant instance's config directory.

    Kept here so ``__init__`` and the options flow share one implementation.
    """

    config_dir = None
    try:
        config_dir = hass.config.path()
    except Exception:  # noqa: BLE001 - test stubs may not expose config
        config_dir = None
    return load_profiles(hass_config_dir=config_dir)


def resolve_profile(
    device: Mapping[str, Any], *, builtin_recognised: bool = False
) -> Optional[CustomProfile]:
    """Convenience wrapper used by the integration hooks."""

    return _REGISTRY.find_profile(device, builtin_recognised=builtin_recognised)


def resolve_custom_profile(
    device: Mapping[str, Any], *, builtin_recognised: bool
):
    """Build the ``device_types.DeviceProfile`` for a custom device.

    ``device_types`` imports this lazily inside a function, and this function
    imports ``device_types`` likewise, so neither module needs the other at
    import time.
    """

    profile = _REGISTRY.find_profile(device, builtin_recognised=builtin_recognised)
    if profile is None:
        return None

    from .device_types import CategoryInfo, DeviceCategory, DeviceProfile

    category = _custom_category(profile.category_key, DeviceCategory)
    info = CategoryInfo(
        category=category,
        label=profile.display_name,
        description=(
            f"用户自定义设备 profile（{profile.source}）"
            + ("；真机已验证" if profile.hardware_verified else "；尚未声明真机验证")
        ),
        capabilities=tuple(sorted(profile.capabilities)),
    )
    return DeviceProfile(
        category=category,
        info=info,
        hardware_verified=profile.hardware_verified,
        # Custom profiles carry their own verification flag: an unverified
        # profile stays registration-only (visible, never controllable).
        registration_only=not profile.hardware_verified or profile.status_only,
        custom_profile=profile,
    )


def _custom_category(key: str, category_enum: Any) -> Any:
    """Return (creating once) the synthetic enum member for a profile key.

    Each custom device gets its own category so the existing category-keyed
    tables and entity code keep working, while the built-in taxonomy stays
    untouched.  The member is registered in ``_value2member_map_`` only; it is
    deliberately kept out of ``_member_map_`` so enumeration over the built-in
    taxonomy is unaffected.
    """

    existing = category_enum._value2member_map_.get(key)
    if existing is not None:
        return existing
    member = object.__new__(category_enum)
    member._name_ = f"CUSTOM_{key[len(CATEGORY_PREFIX):].upper()}"
    member._value_ = key
    category_enum._value2member_map_[key] = member
    return member


def platform_for(device: Mapping[str, Any]) -> Optional[str]:
    """Return the custom platform for ``device`` if a profile claims it."""

    profile = _REGISTRY.find_profile(device)
    return profile.platform if profile is not None else None


def capabilities_for(device: Mapping[str, Any]) -> Optional[frozenset[str]]:
    profile = _REGISTRY.find_profile(device)
    return profile.capabilities if profile is not None else None


def state_defaults_for(device: Mapping[str, Any]) -> Mapping[str, Any]:
    profile = _REGISTRY.find_profile(device)
    return profile.state_overrides if profile is not None else {}


def control_action_for(
    profile_id: str, action: str
) -> Optional[ControlAction]:
    """Look up a control action by profile id (used by the control layer)."""

    profile = _REGISTRY.get(profile_id)
    if profile is None:
        return None
    return profile.control.get(action)


def profile_for_category_key(key: Any) -> Optional[CustomProfile]:
    """Resolve a ``custom:<id>`` category key back to its profile."""

    return _REGISTRY.by_category_key(key)


def is_custom_category_key(key: Any) -> bool:
    """Whether ``key`` is a synthetic custom-device category."""

    if not isinstance(key, str):
        key = getattr(key, "value", None)
    return isinstance(key, str) and key.startswith(CATEGORY_PREFIX)


#: Device-dict key recording whether the built-in taxonomy already claimed the
#: device before a profile was applied.  ``device_type`` cannot carry this signal
#: because ``apply_platform`` overwrites it with the profile's platform.
_BUILTIN_RECOGNISED_KEY = "_builtin_recognised"


def apply_platform(
    device: dict[str, Any],
    *,
    builtin_platform: str | None = None,
) -> dict[str, Any]:
    """Stamp a custom platform onto a normalized device dict, in place.

    The entity platforms select entities through ``device["device_type"]``, so a
    profile target platform has to be written there.  Unmatched devices keep the
    built-in value untouched.  The built-in recognition verdict is always
    recorded so the override guard keeps working after ``device_type`` has been
    overwritten -- deriving it from ``device_type`` afterwards is impossible.
    """

    if not isinstance(device, dict):
        return device

    builtin_recognised = _derive_builtin_recognised(device)
    profile = _REGISTRY.find_profile(device, builtin_recognised=builtin_recognised)
    if profile is None:
        return device

    device["device_type"] = profile.platform
    device[_BUILTIN_RECOGNISED_KEY] = builtin_recognised
    device["custom_profile"] = profile
    return device


def profile_for_device(device: Mapping[str, Any]) -> Optional[CustomProfile]:
    """Resolve the profile that claims a normalized device dict.

    Never raises: a broken profile degrades to the built-in behaviour.
    """

    try:
        builtin_recognised = _derive_builtin_recognised(device)
        return _REGISTRY.find_profile(
            device,
            builtin_recognised=builtin_recognised,
            device_id=str(device.get("device_id") or ""),
        )
    except Exception:  # noqa: BLE001
        return None


def _derive_builtin_recognised(device: Mapping[str, Any]) -> bool:
    """Whether the hard-coded taxonomy recognises this device.

    Computed from the raw protocol fields (``device_type_raw`` and friends), never
    from ``device_type``: that field carries the *entity platform* and is
    overwritten by a custom profile.
    """

    try:
        from .device_types import is_builtin_recognised

        return is_builtin_recognised(dict(device))
    except Exception:  # noqa: BLE001 - never block profile resolution
        return False
