#!/usr/bin/env python3
"""Audit device support across every family visible to an ORVIBO account.

The tool deliberately does not persist raw readtable responses.  Reports contain
only protocol/type descriptors, field names, counts, and salted fingerprints for
correlation.  Credentials, family names, room names, and device names never enter
the output files.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
import csv
from datetime import datetime, timezone
import getpass
import importlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import types
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = PROJECT_ROOT / "custom_components" / "orvibohomebridge"
STATE_VERSION = 1
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "audit-output"
DEFAULT_DEVICE_CATALOG = PROJECT_ROOT / "tools" / "device_catalog.json"
_SAFE_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _load_component_modules() -> tuple[Any, Any, Any, Any]:
    """Load integration modules without importing Home Assistant's package init."""

    package_name = "orvibohomebridge_audit_runtime"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT_PATH)]
        sys.modules[package_name] = package
    return (
        importlib.import_module(f"{package_name}.cloud"),
        importlib.import_module(f"{package_name}.const"),
        importlib.import_module(f"{package_name}.device_types"),
        importlib.import_module(f"{package_name}.protocol"),
    )


CLOUD, CONST, DEVICE_TYPES, PROTOCOL = _load_component_modules()


def _https_client_module() -> Any:
    """Load the network client only when a live audit starts."""

    return importlib.import_module("orvibohomebridge_audit_runtime.https_client")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(value: object, salt: bytes, prefix: str) -> str:
    import hashlib

    digest = hashlib.sha256(
        salt + str(value).encode("utf-8", errors="replace")
    ).hexdigest()
    return f"{prefix}-{digest[:12]}"


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _first_value(item: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def _bounded_text(value: object, limit: int = 120) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _table_rows(table: object) -> list[tuple[str, Mapping[str, Any]]]:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(table, list):
        for item in table:
            if not isinstance(item, Mapping):
                continue
            device_id = _bounded_text(
                _first_value(
                    item,
                    ("deviceId", "deviceID", "deviceUid", "deviceUUID", "uid"),
                )
            )
            rows.append((device_id, item))
    elif isinstance(table, Mapping):
        for key, item in table.items():
            if not isinstance(item, Mapping):
                continue
            device_id = _bounded_text(
                _first_value(
                    item,
                    ("deviceId", "deviceID", "deviceUid", "deviceUUID", "uid"),
                )
                or key
            )
            rows.append((device_id, item))
    return rows


def _status_by_device(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        device_id: item
        for device_id, item in _table_rows(data.get("deviceStatus", []))
        if device_id
    }


def _descriptor(
    item: Mapping[str, Any],
    status: Mapping[str, Any],
    normalized: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return only non-personal fields used to identify a protocol shape."""

    properties = item.get("properties")
    descriptor = properties.get("Descriptor") if isinstance(properties, Mapping) else None
    ui = item.get("ui")
    class_id = _first_value(item, ("classId", "classID"))
    if class_id in (None, "") and isinstance(descriptor, Mapping):
        class_id = _first_value(descriptor, ("classId", "classID"))

    result = {
        "device_type": _safe_int(
            (normalized or {}).get("device_type_raw")
            or _first_value(item, ("deviceType", "devType", "type"))
        ),
        "sub_device_type": _safe_int(
            (normalized or {}).get("sub_device_type")
            or _first_value(item, ("subDeviceType", "subDevType"))
        ),
        "class_id": _safe_int(
            class_id if normalized is None else normalized.get("class_id") or class_id
        ),
        "status_type": _safe_int(
            _first_value(status, ("statusType", "status_type"))
            or _first_value(item, ("statusType", "status_type"))
        ),
        "ui_model": _bounded_text(
            ui.get("model") if isinstance(ui, Mapping) else item.get("uiModel")
        ),
        "model": _bounded_text(
            _first_value(
                item,
                (
                    "model",
                    "modelName",
                    "modelId",
                    "productName",
                    "productId",
                ),
            )
        ),
        "protocol": _bounded_text(
            _first_value(
                item,
                (
                    "protocol",
                    "protocolType",
                    "communicationType",
                    "networkType",
                ),
            )
        ),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_field_names(values: Iterable[object], salt: bytes) -> list[str]:
    names: set[str] = set()
    for value in values:
        name = str(value)
        if _SAFE_FIELD.fullmatch(name):
            names.add(name)
        else:
            names.add(_fingerprint(name, salt, "field"))
    return sorted(names)


def _shape(
    item: Mapping[str, Any], status: Mapping[str, Any], salt: bytes
) -> dict[str, list[str]]:
    properties = item.get("properties")
    status_properties = status.get("properties")
    return {
        "device_fields": _safe_field_names(item.keys(), salt),
        "status_fields": _safe_field_names(status.keys(), salt),
        "device_property_fields": _safe_field_names(
            properties.keys() if isinstance(properties, Mapping) else (), salt
        ),
        "status_property_fields": _safe_field_names(
            status_properties.keys()
            if isinstance(status_properties, Mapping)
            else (),
            salt,
        ),
    }


def _merge_name_lists(left: list[str], right: list[str]) -> list[str]:
    return sorted(set(left).union(right))


def _expected_platform(descriptor: Mapping[str, Any]) -> str | None:
    device_type = _safe_int(descriptor.get("device_type"))
    sub_type = _safe_int(descriptor.get("sub_device_type"))
    model = _bounded_text(descriptor.get("model"), 256)
    if device_type == 0:
        return "light"
    if device_type == 102:
        return "light"
    if device_type == 10086:
        return None
    if device_type == 112:
        return (
            "climate"
            if sub_type == -2
            and model == "2ac836760da10748856a7e4eafb91efa"
            else None
        )
    if device_type == 300:
        return "climate" if sub_type == 481 else "sensor" if sub_type == 491 else None
    if device_type == 506:
        return "cover" if sub_type == 408 else None
    if device_type == 501:
        return "light" if sub_type in (426, 429) else None
    if device_type == 502:
        return "light" if sub_type == 431 else None
    if device_type == 503:
        return "light" if sub_type in (436, 461) else None
    if device_type == 522:
        return "sensor" if sub_type == 463 else None
    if device_type in CONST.DEVICE_TYPE_MAP:
        return CONST.DEVICE_TYPE_MAP[device_type]
    class_id = _safe_int(descriptor.get("class_id"))
    if class_id in CONST.CLASS_ID_MAP:
        return CONST.CLASS_ID_MAP[class_id]
    return None


def _support_state(
    profile: Any,
    *,
    parsed: bool,
    hidden: bool,
    expected_platform: str | None,
    actual_platform: str,
) -> str:
    if hidden:
        return "hidden"
    if not parsed:
        return "parser_gap"
    if profile.registration_only:
        return "registration_only"
    if expected_platform is None:
        return "recognized_only"
    if actual_platform != expected_platform:
        return "platform_mismatch"
    if profile.hardware_verified:
        return "supported_verified"
    return "supported_unverified"


def analyze_family(
    data: Mapping[str, Any],
    parser: Callable[[dict[str, Any]], list[dict[str, Any]]],
    *,
    salt: bytes,
    family_tag: str,
    max_examples: int,
) -> dict[str, Any]:
    """Compare one readtable response with the integration's real parser/profile."""

    normalized_devices = parser(dict(data))
    normalized_by_id = {
        _bounded_text(item.get("device_id")): item
        for item in normalized_devices
        if isinstance(item, Mapping) and item.get("device_id")
    }
    statuses = _status_by_device(data)
    rows = _table_rows(data.get("device", []))
    counts: Counter[str] = Counter()
    signatures: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()

    for device_id, raw in rows:
        if raw.get("delFlag") in (1, "1"):
            continue
        normalized = normalized_by_id.get(device_id)
        status = statuses.get(device_id, {})
        profile_input = dict(normalized or raw)
        if status.get("statusType") not in (None, ""):
            profile_input.setdefault("status_type", status.get("statusType"))
        descriptor = _descriptor(raw, status, normalized)
        profile = DEVICE_TYPES.get_device_profile(profile_input)
        hidden = DEVICE_TYPES.is_hidden_category(profile.category)
        state = _support_state(
            profile,
            parsed=normalized is not None,
            hidden=hidden,
            expected_platform=_expected_platform(descriptor),
            actual_platform=_bounded_text((normalized or {}).get("device_type")),
        )
        counts[state] += 1
        if device_id:
            seen_ids.add(device_id)

        signature_key = json.dumps(
            {
                "descriptor": descriptor,
                "category": profile.category.value,
                "support_state": state,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        signature = signatures.setdefault(
            signature_key,
            {
                "descriptor": descriptor,
                "category": profile.category.value,
                "category_label": profile.info.label,
                "support_state": state,
                "count": 0,
                "family_examples": [],
                "device_examples": [],
                "shape": {
                    "device_fields": [],
                    "status_fields": [],
                    "device_property_fields": [],
                    "status_property_fields": [],
                },
            },
        )
        signature["count"] += 1
        if family_tag not in signature["family_examples"]:
            signature["family_examples"].append(family_tag)
            signature["family_examples"] = signature["family_examples"][:max_examples]
        if device_id:
            device_tag = _fingerprint(device_id, salt, "device")
            if device_tag not in signature["device_examples"]:
                signature["device_examples"].append(device_tag)
                signature["device_examples"] = signature["device_examples"][:max_examples]
        shape = _shape(raw, status, salt)
        for key, values in shape.items():
            signature["shape"][key] = _merge_name_lists(
                signature["shape"][key], values
            )

    normalized_only = set(normalized_by_id).difference(seen_ids)
    if normalized_only:
        counts["normalized_only"] += len(normalized_only)
    return {
        "counts": dict(sorted(counts.items())),
        "raw_device_count": sum(counts.values()) - counts.get("normalized_only", 0),
        "normalized_device_count": len(normalized_devices),
        "table_names": _safe_field_names(data.keys(), salt),
        "signatures": signatures,
    }


def _new_state(salt: bytes) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "salt": salt.hex(),
        "account_tag": "",
        "cloud_region": "",
        "families_discovered": 0,
        "processed_family_tags": [],
        "families": [],
        "totals": {},
        "signatures": {},
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_state(secrets.token_bytes(32))
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") != STATE_VERSION:
        raise ValueError(
            f"unsupported state version: {state.get('version')!r}; use a new output directory"
        )
    bytes.fromhex(state["salt"])
    # Older audit runs recorded an empty family as a normal successful row.
    # Relabel it in place so existing checkpoints need not be discarded.
    for family in state.get("families", []):
        if (
            family.get("status") == "ok"
            and family.get("raw_device_count") == 0
        ):
            family["status"] = "empty"
    return state


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _load_device_catalog(path: Path) -> dict[str, dict[str, Any]]:
    """Build a model lookup without carrying catalog identifiers into reports."""

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    descriptions = payload.get("deviceDescList", [])
    languages = payload.get("deviceLanguageList", [])
    if not isinstance(descriptions, list) or not isinstance(languages, list):
        raise ValueError("device catalog has an unsupported structure")

    names_by_description: dict[str, set[str]] = {}
    for item in languages:
        if not isinstance(item, Mapping):
            continue
        description_id = _bounded_text(item.get("dataId"), 256)
        product_name = _bounded_text(item.get("productName"), 256)
        if description_id and product_name:
            names_by_description.setdefault(description_id, set()).add(product_name)

    mutable: dict[str, dict[str, Any]] = {}
    for item in descriptions:
        if not isinstance(item, Mapping):
            continue
        model = _bounded_text(item.get("model"), 256)
        if not model:
            continue
        entry = mutable.setdefault(
            model,
            {
                "catalog_device_desc_count": 0,
                "product_names": set(),
                "internal_models": set(),
            },
        )
        entry["catalog_device_desc_count"] += 1
        description_id = _bounded_text(item.get("deviceDescId"), 256)
        entry["product_names"].update(
            names_by_description.get(description_id, set())
        )
        internal_model = _bounded_text(item.get("internalModel"), 256)
        if internal_model:
            entry["internal_models"].add(internal_model)

    return {
        model: {
            "catalog_device_desc_count": entry["catalog_device_desc_count"],
            "product_names": sorted(entry["product_names"]),
            "internal_models": sorted(entry["internal_models"]),
        }
        for model, entry in mutable.items()
    }


def _catalog_fields(
    signature: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    descriptor = signature.get("descriptor", {})
    model = (
        _bounded_text(descriptor.get("model"), 256)
        if isinstance(descriptor, Mapping)
        else ""
    )
    entry = catalog.get(model)
    if entry is None:
        return {
            "catalog_match": False,
            "catalog_ambiguous": False,
            "catalog_device_desc_count": 0,
            "product_names": [],
            "internal_models": [],
        }
    product_names = list(entry.get("product_names", []))
    internal_models = list(entry.get("internal_models", []))
    description_count = int(entry.get("catalog_device_desc_count", 0))
    return {
        "catalog_match": True,
        "catalog_ambiguous": (
            description_count > 1
            or len(product_names) > 1
            or len(internal_models) > 1
        ),
        "catalog_device_desc_count": description_count,
        "product_names": product_names,
        "internal_models": internal_models,
    }


def merge_family_result(
    state: dict[str, Any],
    family_record: dict[str, Any],
    analysis: Mapping[str, Any],
    *,
    max_examples: int,
) -> None:
    totals = Counter(state.get("totals", {}))
    totals.update(analysis["counts"])
    state["totals"] = dict(sorted(totals.items()))
    state["families"].append(family_record)
    state["processed_family_tags"].append(family_record["family_tag"])

    for key, incoming in analysis["signatures"].items():
        current = state["signatures"].get(key)
        if current is None:
            state["signatures"][key] = incoming
            continue
        current["count"] += incoming["count"]
        for example_key in ("family_examples", "device_examples"):
            current[example_key] = list(
                dict.fromkeys(current[example_key] + incoming[example_key])
            )[:max_examples]
        for shape_key, values in incoming["shape"].items():
            current["shape"][shape_key] = _merge_name_lists(
                current["shape"][shape_key], values
            )


def _public_report(
    state: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    catalog_source: str = "",
) -> dict[str, Any]:
    signatures = sorted(
        state.get("signatures", {}).values(),
        key=lambda item: (
            item["support_state"],
            -item["count"],
            json.dumps(item["descriptor"], sort_keys=True),
        ),
    )
    if catalog is not None:
        signatures = [
            {**item, **_catalog_fields(item, catalog)} for item in signatures
        ]
    families = [
        {
            key: value
            for key, value in family.items()
            if key
            in {
                "family_index",
                "family_tag",
                "status",
                "counts",
                "raw_device_count",
                "normalized_device_count",
                "error_type",
            }
        }
        for family in state.get("families", [])
    ]
    report = {
        "schema_version": STATE_VERSION,
        "generated_at": _utc_now(),
        "cloud_region": state.get("cloud_region", ""),
        "summary": {
            "families_discovered": state.get("families_discovered", 0),
            "families_processed": len(state.get("processed_family_tags", [])),
            "families_empty": sum(
                1
                for item in state.get("families", [])
                if item.get("status") == "empty"
            ),
            "families_failed": sum(
                1
                for item in state.get("families", [])
                if item.get("status") == "error"
            ),
            "totals": state.get("totals", {}),
            "signature_count": len(signatures),
        },
        "families": families,
        "signatures": signatures,
        "privacy": {
            "raw_readtable_persisted": False,
            "identifiers": "salted SHA-256 fingerprints",
            "excluded": [
                "credentials",
                "family names",
                "room names",
                "device names",
                "raw identifiers",
            ],
        },
    }

    if catalog is not None:
        total_devices = sum(int(item.get("count", 0)) for item in signatures)
        matched_devices = sum(
            int(item.get("count", 0))
            for item in signatures
            if item.get("catalog_match")
        )
        unique_models = {
            _bounded_text(item.get("descriptor", {}).get("model"), 256)
            for item in signatures
            if isinstance(item.get("descriptor"), Mapping)
            and item.get("descriptor", {}).get("model") not in (None, "")
        }
        matched_models = {
            _bounded_text(item.get("descriptor", {}).get("model"), 256)
            for item in signatures
            if item.get("catalog_match")
        }
        report["catalog"] = {
            "source": catalog_source,
            "catalog_model_count": len(catalog),
            "audit_model_count": len(unique_models),
            "matched_model_count": len(matched_models),
            "unmatched_model_count": len(unique_models - matched_models),
            "matched_device_records": matched_devices,
            "unmatched_device_records": total_devices - matched_devices,
            "device_record_match_rate": (
                round(matched_devices * 100 / total_devices, 2)
                if total_devices
                else 0.0
            ),
            "ambiguous_device_records": sum(
                int(item.get("count", 0))
                for item in signatures
                if item.get("catalog_ambiguous")
            ),
            "named_device_records": sum(
                int(item.get("count", 0))
                for item in signatures
                if item.get("product_names")
            ),
        }
    return report


def _write_csv(
    path: Path,
    report: Mapping[str, Any],
    *,
    include_catalog: bool = False,
) -> None:
    rows = [
        item
        for item in report["signatures"]
        if item["support_state"]
        in {
            "parser_gap",
            "platform_mismatch",
            "recognized_only",
            "registration_only",
        }
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "support_state",
            "category",
            "category_label",
            "count",
            "descriptor",
            "family_examples",
            "device_examples",
            "device_fields",
            "status_fields",
            "device_property_fields",
            "status_property_fields",
        ]
        if include_catalog:
            fieldnames.extend(
                (
                    "catalog_match",
                    "catalog_ambiguous",
                    "catalog_device_desc_count",
                    "product_names",
                    "internal_models",
                )
            )
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
        )
        writer.writeheader()
        for item in rows:
            row = {
                    "support_state": item["support_state"],
                    "category": item["category"],
                    "category_label": item["category_label"],
                    "count": item["count"],
                    "descriptor": json.dumps(
                        item["descriptor"], ensure_ascii=False, sort_keys=True
                    ),
                    "family_examples": ",".join(item["family_examples"]),
                    "device_examples": ",".join(item["device_examples"]),
                    "device_fields": ",".join(item["shape"]["device_fields"]),
                    "status_fields": ",".join(item["shape"]["status_fields"]),
                    "device_property_fields": ",".join(
                        item["shape"]["device_property_fields"]
                    ),
                    "status_property_fields": ",".join(
                        item["shape"]["status_property_fields"]
                    ),
                }
            if include_catalog:
                row.update(
                    {
                        "catalog_match": item.get("catalog_match", False),
                        "catalog_ambiguous": item.get(
                            "catalog_ambiguous", False
                        ),
                        "catalog_device_desc_count": item.get(
                            "catalog_device_desc_count", 0
                        ),
                        "product_names": " | ".join(
                            item.get("product_names", [])
                        ),
                        "internal_models": " | ".join(
                            item.get("internal_models", [])
                        ),
                    }
                )
            writer.writerow(row)


def _write_outputs(
    output_dir: Path,
    state: dict[str, Any],
    catalog: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    catalog_source: str = "",
) -> None:
    state["updated_at"] = _utc_now()
    _atomic_json(output_dir / "state.json", state)
    report = _public_report(
        state, catalog, catalog_source=catalog_source
    )
    _atomic_json(output_dir / "report.json", report)
    _write_csv(output_dir / "unsupported.csv", report)
    if catalog is not None:
        _write_csv(
            output_dir / "unsupported-enriched.csv",
            report,
            include_catalog=True,
        )


def _catalog_for_args(
    args: argparse.Namespace,
    *,
    required: bool = False,
) -> tuple[dict[str, dict[str, Any]] | None, str]:
    if args.no_device_catalog:
        if required:
            raise ValueError("--enrich-existing requires a device catalog")
        return None, ""
    path = args.device_catalog.resolve()
    if not path.exists():
        if required:
            raise ValueError(f"device catalog does not exist: {path}")
        print(f"提示：未找到设备目录，跳过产品名关联：{path}")
        return None, ""
    return _load_device_catalog(path), path.name


def enrich_existing(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    state_path = output_dir / "state.json"
    if not state_path.exists():
        raise ValueError(f"audit state does not exist: {state_path}")
    catalog, source = _catalog_for_args(args, required=True)
    assert catalog is not None
    state = _load_state(state_path)
    _write_outputs(
        output_dir,
        state,
        catalog,
        catalog_source=source,
    )
    report = _public_report(state, catalog, catalog_source=source)
    coverage = report["catalog"]
    print(
        f"目录关联完成：设备记录={coverage['matched_device_records']}/"
        f"{coverage['matched_device_records'] + coverage['unmatched_device_records']} "
        f"匹配率={coverage['device_record_match_rate']}%"
    )
    print(f"增强清单：{output_dir / 'unsupported-enriched.csv'}")
    return 0


def _password_hash(args: argparse.Namespace) -> str:
    digest = os.environ.get(args.password_hash_env, "").strip()
    if digest:
        return PROTOCOL.normalize_password_hash(digest)
    password = os.environ.get(args.password_env)
    if password is None:
        password = getpass.getpass("ORVIBO password: ")
    if not password:
        raise ValueError("password is empty")
    return PROTOCOL.password_hash(password)


def _username(args: argparse.Namespace) -> str:
    username = (args.username or os.environ.get(args.username_env, "")).strip()
    if not username:
        username = input("ORVIBO username: ").strip()
    if not username:
        raise ValueError("username is empty")
    return username


async def _read_family(
    client: Any,
    family_id: str,
    *,
    retries: int,
    retry_delay: float,
) -> Mapping[str, Any] | None:
    for attempt in range(1, retries + 1):
        client.set_family(family_id)
        if await client.ensure_login():
            data = await client._readtable(device_flag=0)
            if isinstance(data, Mapping):
                if _table_rows(data.get("device", [])):
                    return data

                # Some accounts expose device tables only with deviceFlag=1.
                # If neither response has devices, this is a valid empty home.
                fallback = await client._readtable(device_flag=1)
                if isinstance(fallback, Mapping):
                    merged = dict(data)
                    for key, value in fallback.items():
                        if (
                            key in merged
                            and isinstance(merged[key], list)
                            and isinstance(value, list)
                        ):
                            merged[key] = merged[key] + value
                        elif key not in merged or value:
                            merged[key] = value
                    merged.setdefault("device", [])
                    return merged
        if attempt < retries:
            # A long audit can outlive an access token.  Force the public
            # ensure_login path to obtain a fresh token on the next attempt.
            client.access_token = None
            client.user_id = None
            await asyncio.sleep(retry_delay * attempt)
    return None


async def run(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "state.json"
    state = _load_state(state_path)
    salt = bytes.fromhex(state["salt"])
    catalog, catalog_source = _catalog_for_args(args)

    username = _username(args)
    password_hash = _password_hash(args)
    preferred = (
        CLOUD.CHINA_CLOUD
        if args.cloud in {"auto", "china"}
        else CLOUD.GLOBAL_CLOUD
    )
    https_client = _https_client_module()
    client = https_client.HttpsClient(username, password_hash, cloud=preferred)

    try:
        logged_in = (
            await client.async_detect_cloud()
            if args.cloud == "auto"
            else await client.ensure_login()
        )
        if not logged_in:
            print("登录失败：请检查账号、密码和云端区域。", file=sys.stderr)
            return 2

        state_region = state.get("cloud_region")
        active_region = client.cloud.region.value
        account_tag = _fingerprint(username, salt, "account")
        if state.get("account_tag") and state["account_tag"] != account_tag:
            raise ValueError(
                "output directory belongs to another account; use a new directory"
            )
        if state_region and state_region != active_region:
            raise ValueError(
                "output directory belongs to another cloud region; use a new directory"
            )
        state["cloud_region"] = active_region
        state["account_tag"] = account_tag
        families = list(client.family_list)
        state["families_discovered"] = len(families)
        if args.max_families is not None:
            families = families[: args.max_families]
        completed = set(state.get("processed_family_tags", []))

        print(
            f"区域={active_region} 家庭数={len(families)} "
            f"已完成={len(completed)} 输出={output_dir}"
        )
        for index, family in enumerate(families):
            family_id = _bounded_text(family.get("familyId"), 256)
            if not family_id:
                continue
            family_tag = _fingerprint(
                f"{active_region}:{family_id}", salt, "family"
            )
            if family_tag in completed:
                print(f"[{index + 1}/{len(families)}] 已跳过 {family_tag}")
                continue

            # A previously failed family is retried on resume; retain only its
            # latest result so the public report has one row per family.
            state["families"] = [
                item
                for item in state["families"]
                if item.get("family_tag") != family_tag
            ]

            print(f"[{index + 1}/{len(families)}] 拉取 {family_tag} ...", end=" ", flush=True)
            try:
                data = await _read_family(
                    client,
                    family_id,
                    retries=args.retries,
                    retry_delay=args.retry_delay,
                )
                if data is None:
                    raise RuntimeError("readtable returned no data")
                analysis = analyze_family(
                    data,
                    client.parse_device_status_list,
                    salt=salt,
                    family_tag=family_tag,
                    max_examples=args.max_examples,
                )
                family_record = {
                    "family_index": index,
                    "family_tag": family_tag,
                    "status": (
                        "empty" if analysis["raw_device_count"] == 0 else "ok"
                    ),
                    "counts": analysis["counts"],
                    "raw_device_count": analysis["raw_device_count"],
                    "normalized_device_count": analysis["normalized_device_count"],
                }
                merge_family_result(
                    state,
                    family_record,
                    analysis,
                    max_examples=args.max_examples,
                )
                if family_record["status"] == "empty":
                    print("空家庭，已跳过")
                else:
                    print(
                        f"设备={analysis['raw_device_count']} "
                        f"未知/仅登记={analysis['counts'].get('registration_only', 0)} "
                        f"解析遗漏={analysis['counts'].get('parser_gap', 0)}"
                    )
            except Exception as exc:  # keep the multi-family audit moving
                logging.getLogger(__name__).exception(
                    "family audit failed: %s", family_tag
                )
                family_record = {
                    "family_index": index,
                    "family_tag": family_tag,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "counts": {},
                    "raw_device_count": 0,
                    "normalized_device_count": 0,
                }
                state["families"].append(family_record)
                print(f"失败={type(exc).__name__}")

            _write_outputs(
                output_dir,
                state,
                catalog,
                catalog_source=catalog_source,
            )
            if family_record["status"] in {"ok", "empty"}:
                completed.add(family_tag)
            if args.delay:
                await asyncio.sleep(args.delay)

        _write_outputs(
            output_dir,
            state,
            catalog,
            catalog_source=catalog_source,
        )
        totals = state.get("totals", {})
        empty_families = sum(
            1 for item in state["families"] if item.get("status") == "empty"
        )
        print(
            "完成："
            f"家庭={len(state['processed_family_tags'])} "
            f"空家庭={empty_families} "
            f"已验证={totals.get('supported_verified', 0)} "
            f"未验证={totals.get('supported_unverified', 0)} "
            f"隐藏={totals.get('hidden', 0)} "
            f"仅识别={totals.get('recognized_only', 0)} "
            f"平台不匹配={totals.get('platform_mismatch', 0)} "
            f"未知/仅登记={totals.get('registration_only', 0)} "
            f"解析遗漏={totals.get('parser_gap', 0)}"
        )
        print(f"报告：{output_dir / 'report.json'}")
        print(f"待支持清单：{output_dir / 'unsupported.csv'}")
        if catalog is not None:
            print(
                f"目录增强清单：{output_dir / 'unsupported-enriched.csv'}"
            )
        return 0
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自动切换所有家庭并审计 ORVIBO 设备支持情况。"
    )
    parser.add_argument("--username", help="账号；建议改用环境变量")
    parser.add_argument(
        "--username-env", default="ORVIBO_USERNAME", help="账号环境变量名"
    )
    parser.add_argument(
        "--password-env", default="ORVIBO_PASSWORD", help="明文密码环境变量名"
    )
    parser.add_argument(
        "--password-hash-env",
        default="ORVIBO_PASSWORD_HASH",
        help="32 位协议密码摘要环境变量名，存在时优先使用",
    )
    parser.add_argument(
        "--cloud", choices=("auto", "china", "global"), default="auto"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR
    )
    parser.add_argument(
        "--device-catalog",
        type=Path,
        default=DEFAULT_DEVICE_CATALOG,
        help="device_catalog.json 路径；存在时自动关联产品名",
    )
    parser.add_argument(
        "--no-device-catalog",
        action="store_true",
        help="禁用设备目录关联",
    )
    parser.add_argument(
        "--enrich-existing",
        action="store_true",
        help="仅补全已有输出，不登录或重新拉取家庭",
    )
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--max-examples", type=int, default=3)
    parser.add_argument(
        "--max-families",
        type=int,
        help="仅用于小规模试跑；默认处理全部家庭",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.retries < 1:
        raise SystemExit("--retries must be at least 1")
    if args.max_examples < 1:
        raise SystemExit("--max-examples must be at least 1")
    if args.delay < 0 or args.retry_delay < 0:
        raise SystemExit("delay values cannot be negative")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        if args.enrich_existing:
            return enrich_existing(args)
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断；下次使用同一输出目录会从断点继续。", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except ModuleNotFoundError as exc:
        if exc.name == "aiohttp":
            print(
                "错误：缺少 aiohttp。请在用于运行脚本的 Python 环境中安装项目依赖："
                " python -m pip install aiohttp",
                file=sys.stderr,
            )
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
