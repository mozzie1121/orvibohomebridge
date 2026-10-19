"""Serve bundled cards and register versioned Lovelace module resources."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import logging
from pathlib import Path
from urllib.parse import urlsplit

_LOGGER = logging.getLogger(__name__)
DATA_KEY = "orvibohomebridge_frontend"
BASE_URL = "/orvibohomebridge/www"
CARD_FILES = ("orvibohomebridge-door-lock-card.js", "orvibohomebridge-nvr-card.js")


def _card_urls(directory: Path) -> list[str]:
    """Content hashes also invalidate caches when ZIP timestamps are preserved."""
    return [f"{BASE_URL}/{name}?v={hashlib.sha256((directory / name).read_bytes()).hexdigest()[:16]}"
            for name in CARD_FILES]


def _same_resource(existing: object, wanted: str) -> bool:
    if not isinstance(existing, str):
        return False
    parsed = urlsplit(existing)
    return not parsed.scheme and not parsed.netloc and parsed.path == urlsplit(wanted).path


async def _async_lovelace_resources(hass, urls: list[str]) -> bool:
    """Use HA collection APIs; never edit .storage or a user's YAML files."""
    data = hass.data.get("lovelace")
    if isinstance(data, Mapping):
        mode = data.get("resource_mode", data.get("mode"))
        collection = data.get("resources")
    else:
        mode = getattr(data, "resource_mode", getattr(data, "mode", None))
        collection = getattr(data, "resources", None)
    if mode == "yaml":
        return True  # Global module registration supports YAML dashboards too.
    if collection is None or not callable(getattr(collection, "async_create_item", None)):
        return False
    # Storage collections are lazy. async_items() before loading would miss
    # existing entries, creating duplicates or overwriting stored resources.
    await collection.async_get_info()
    for url in urls:
        matches = [row for row in collection.async_items() if _same_resource(row.get("url"), url)]
        if matches:
            for row in matches:
                if row.get("url") != url or row.get("type") != "module":
                    await collection.async_update_item(row["id"], {"url": url, "res_type": "module"})
        else:
            await collection.async_create_item({"url": url, "res_type": "module"})
    return True


async def async_register_card_resources(hass) -> None:
    """One failed card registration must not skip the other bundled card."""
    state = hass.data.setdefault(DATA_KEY, {"lock": asyncio.Lock(), "static": False})
    async with state["lock"]:
        # Reconcile once startup has settled, even if the first pass succeeded.
        # HA's lazy resource load can restore an older disk snapshot afterwards;
        # browsers reconnecting during startup can also miss the first JS event.
        # Install this before static setup so its early failures are retried too.
        if not state.get("startup_retry") and not getattr(hass, "is_running", False):
            state["startup_retry"] = True

            async def retry(_event):
                await async_register_card_resources(hass)

            hass.bus.async_listen_once("homeassistant_started", retry)
        directory = Path(__file__).parent / "www"
        try:
            urls = await hass.async_add_executor_job(_card_urls, directory)
            if not state["static"]:
                if hasattr(hass.http, "async_register_static_paths"):
                    from homeassistant.components.http import StaticPathConfig
                    await hass.http.async_register_static_paths([
                        StaticPathConfig(url_path=BASE_URL, path=str(directory), cache_headers=True),
                    ])
                else:
                    hass.http.register_static_path(BASE_URL, str(directory), cache_headers=True)
                state["static"] = True
        except Exception:
            _LOGGER.exception("欧瑞博卡片静态资源注册失败")
            return

        from homeassistant.components import frontend
        for url in urls:
            try:
                frontend.add_extra_js_url(hass, url)
            except Exception:
                _LOGGER.exception("欧瑞博卡片前端模块注册失败: %s", urlsplit(url).path)
        try:
            await _async_lovelace_resources(hass, urls)
        except Exception:
            _LOGGER.exception("欧瑞博卡片仪表盘资源注册失败；可在仪表盘资源中手动添加 JavaScript 模块")
