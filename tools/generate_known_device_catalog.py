"""Generate the integration's exact-model index from ORVIBO device_catalog."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from typing import Any


def build_catalog(source: dict[str, Any]) -> dict[str, Any]:
    names_by_description: dict[str, set[str]] = defaultdict(set)
    for item in source.get("deviceLanguageList", []):
        if not isinstance(item, dict) or item.get("language") != "zh":
            continue
        data_id = str(item.get("dataId") or "").strip()
        product_name = str(item.get("productName") or "").strip()
        if data_id and product_name:
            names_by_description[data_id].add(product_name)

    mutable: dict[str, dict[str, set[str]]] = {}
    for item in source.get("deviceDescList", []):
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        entry = mutable.setdefault(
            model, {"names": set(), "internal_models": set()}
        )
        description_id = str(item.get("deviceDescId") or "").strip()
        entry["names"].update(names_by_description.get(description_id, ()))
        internal_model = str(item.get("internalModel") or "").strip()
        if internal_model:
            entry["internal_models"].add(internal_model)

    models = {
        model: {
            "names": sorted(value["names"], key=lambda item: (len(item), item)),
            "internal_models": sorted(
                value["internal_models"], key=lambda item: (len(item), item)
            ),
        }
        for model, value in sorted(mutable.items())
    }
    return {
        "schema_version": 1,
        "source": "ORVIBO device_catalog.json",
        "model_count": len(models),
        "models": models,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = json.loads(args.catalog.read_text(encoding="utf-8"))
    generated = build_catalog(source)
    args.output.write_text(
        json.dumps(
            generated,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"generated {generated['model_count']} models -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
