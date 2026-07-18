"""Config flow for Orvibo HomeBridge.

支持三步配置：登录 → 选家庭 → 选设备（可选设区域）。
向下兼容现有已配置条目。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import ATTR_AREA_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import area_registry as ar, selector

from .const import DOMAIN, CONF_FAMILY_ID
from .https_client import HttpsClient
from .selection import (
    CONF_DEVICE_AREAS,
    CONF_SELECTED_DEVICE_IDS,
    configured_device_areas,
    selected_device_ids,
)

_LOGGER = logging.getLogger(__name__)


# ── 设备标签（尽量短，在一行内看得清） ──

def _device_label(device_id: str, name: str, room: str) -> str:
    """简短的设备标签：设备名（房间名）"""
    if room and room != name:
        return f"{name} ({room})"
    return name or device_id


def _device_schema(
    devices: list[dict[str, Any]],
    default_ids: list[str],
) -> vol.Schema:
    """多选设备表单。"""
    options = [
        selector.SelectOptionDict(
            value=dev["device_id"],
            label=_device_label(
                dev["device_id"],
                dev.get("device_name", ""),
                dev.get("room_name", ""),
            ),
        )
        for dev in devices
    ]
    return vol.Schema({
        vol.Required(CONF_SELECTED_DEVICE_IDS, default=default_ids): (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        )
    })


def _area_schema(default_area_id: str | None) -> vol.Schema:
    """单个设备区域选择表单。"""
    key = (
        vol.Optional(ATTR_AREA_ID, default=default_area_id)
        if default_area_id
        else vol.Optional(ATTR_AREA_ID)
    )
    return vol.Schema({key: selector.AreaSelector()})


def _default_area_id(
    hass: HomeAssistant,
    device: dict[str, Any],
    configured_areas: dict[str, str | None],
) -> str | None:
    """根据 ORVIBO 房间名确定默认 HA 区域。"""
    registry = ar.async_get(hass)
    device_id = device["device_id"]
    if device_id in configured_areas:
        area_id = configured_areas[device_id]
        if area_id is None or area_id in registry.areas:
            return area_id
    room_name = device.get("room_name", "")
    if not room_name:
        return None
    return registry.async_get_or_create(room_name).id


# ── ConfigFlow（首次配置） ──

class OrviboMeshConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._https_client: HttpsClient | None = None
        self._pending_data: dict[str, Any] = {}
        self._pending_devices: list[dict[str, Any]] = []
        self._pending_selected_ids: list[str] = []
        self._pending_device_areas: dict[str, str | None] = {}
        self._area_index = 0
        self._devices_loaded = False

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OrviboMeshOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            if not username or not password:
                errors["base"] = "empty_username_or_password"
            elif not re.match(r'^1[3-9]\d{9}$', username) and not re.match(r'^[^@]+@[^@]+\.[^@]+$', username):
                errors[CONF_USERNAME] = "invalid_username"
            else:
                try:
                    client = HttpsClient(username=username, password=password)
                    success = await client.ensure_login()
                    if success:
                        self._https_client = client
                        self._pending_data = {
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        }
                        if len(client.family_list) <= 1:
                            if client.family_list:
                                client.set_family(client.family_list[0]["familyId"])
                            self._pending_data[CONF_FAMILY_ID] = client.family_id
                            return await self.async_step_devices()
                        return await self.async_step_select_family()
                    else:
                        errors["base"] = "auth_failed"
                except Exception as e:
                    _LOGGER.error(f"登录验证失败: {e}")
                    errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_select_family(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._https_client is None:
            return self.async_abort(reason="unknown")

        family_choices = {
            f["familyId"]: f"{f['familyName']} ({f['familyId'][:8]}...)"
            for f in self._https_client.family_list
        }

        if user_input is not None:
            family_id = user_input.get(CONF_FAMILY_ID)
            if family_id:
                self._https_client.set_family(family_id)
                self._pending_data[CONF_FAMILY_ID] = family_id
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="select_family",
            data_schema=vol.Schema({
                vol.Required(CONF_FAMILY_ID): vol.In(family_choices),
            }),
            description_placeholders={
                "family_count": str(len(family_choices)),
            },
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if self._https_client is None:
            return self.async_abort(reason="unknown")

        if not self._devices_loaded:
            try:
                device_data = await self._https_client.fetch_device_status()
                if device_data:
                    self._pending_devices = self._https_client.parse_device_status_list(device_data)
                    self._devices_loaded = True
            except Exception as e:
                _LOGGER.error(f"获取设备列表失败: {e}")
                errors["base"] = "cannot_connect"

        if not self._pending_devices:
            if not errors:
                errors["base"] = "no_devices"
        elif user_input is not None and CONF_SELECTED_DEVICE_IDS in user_input:
            available = {dev["device_id"] for dev in self._pending_devices}
            requested = {
                str(device_id)
                for device_id in user_input[CONF_SELECTED_DEVICE_IDS]
            }
            self._pending_selected_ids = [
                dev["device_id"]
                for dev in self._pending_devices
                if dev["device_id"] in requested & available
            ]
            if not self._pending_selected_ids:
                errors["base"] = "no_devices_selected"
            else:
                self._area_index = 0
                self._pending_device_areas = {}
                return await self.async_step_area()

        defaults = list(
            selected_device_ids(
                {},
                (dev["device_id"] for dev in self._pending_devices),
            )
        )
        return self.async_show_form(
            step_id="devices",
            data_schema=_device_schema(self._pending_devices, defaults),
            errors=errors,
        )

    async def async_step_area(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """逐个设备设置区域。"""
        selected = {
            dev["device_id"]: dev
            for dev in self._pending_devices
            if dev["device_id"] in self._pending_selected_ids
        }
        devices = [selected[device_id] for device_id in self._pending_selected_ids]
        if not devices or self._area_index >= len(devices):
            return self._finish()

        device = devices[self._area_index]
        if user_input is not None:
            self._pending_device_areas[device["device_id"]] = user_input.get(ATTR_AREA_ID)
            self._area_index += 1
            if self._area_index >= len(devices):
                return self._finish()
            device = devices[self._area_index]

        default_area_id = _default_area_id(self.hass, device, self._pending_device_areas)

        device_name = device.get("device_name", "") or device["device_id"]
        room_name = device.get("room_name", "") or "-"

        return self.async_show_form(
            step_id="area",
            data_schema=_area_schema(default_area_id),
            description_placeholders={
                "device_name": device_name,
                "room": room_name,
                "position": str(self._area_index + 1),
                "total": str(len(devices)),
            },
        )

    def _finish(self) -> FlowResult:
        """创建配置条目。"""
        client = self._https_client
        title = f"{client.username}" if client else "ORVIBO"
        if client and client.family_name:
            title = f"{client.username} - {client.family_name}"

        # 只保存选中的设备和区域映射
        selected_set = set(self._pending_selected_ids)
        filtered_areas = {
            device_id: area_id
            for device_id, area_id in self._pending_device_areas.items()
            if device_id in selected_set
        }

        return self.async_create_entry(
            title=title,
            data=self._pending_data,
            options={
                CONF_SELECTED_DEVICE_IDS: self._pending_selected_ids,
                CONF_DEVICE_AREAS: filtered_areas,
            },
        )


# ── OptionsFlow（配置→选项） ──

class OrviboMeshOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._devices: list[dict[str, Any]] = []
        self._selected_ids: list[str] = []
        self._device_areas: dict[str, str | None] = {}
        self._area_index = 0

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._device_areas = configured_device_areas(self.config_entry.options)
        return await self.async_step_devices(user_input)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if not self._devices:
            coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
            if coordinator is not None:
                self._devices = list(coordinator.devices.values())
            if not self._devices:
                try:
                    client = HttpsClient(
                        username=self.config_entry.data[CONF_USERNAME],
                        password=self.config_entry.data[CONF_PASSWORD],
                    )
                    if await client.ensure_login():
                        device_data = await client.fetch_device_status()
                        if device_data:
                            self._devices = client.parse_device_status_list(device_data)
                except Exception as e:
                    _LOGGER.error(f"获取设备列表失败: {e}")
                    errors["base"] = "cannot_connect"

        if user_input is not None and CONF_SELECTED_DEVICE_IDS in user_input:
            requested = {
                str(device_id)
                for device_id in user_input[CONF_SELECTED_DEVICE_IDS]
            }
            self._selected_ids = [
                dev["device_id"] for dev in self._devices if dev["device_id"] in requested
            ]
            if not self._selected_ids:
                errors["base"] = "no_devices_selected"
            else:
                self._area_index = 0
                return await self.async_step_area()

        defaults = sorted(
            selected_device_ids(
                self.config_entry.options,
                (dev["device_id"] for dev in self._devices),
            )
        )
        return self.async_show_form(
            step_id="devices",
            data_schema=_device_schema(self._devices, defaults),
            errors=errors,
        )

    async def async_step_area(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """逐个设备更新区域。"""
        selected = {
            dev["device_id"]: dev
            for dev in self._devices
            if dev["device_id"] in self._selected_ids
        }
        devices = [selected[device_id] for device_id in self._selected_ids]
        if not devices or self._area_index >= len(devices):
            return self._finish()

        device = devices[self._area_index]
        if user_input is not None:
            self._device_areas[device["device_id"]] = user_input.get(ATTR_AREA_ID)
            self._area_index += 1
            if self._area_index >= len(devices):
                return self._finish()
            device = devices[self._area_index]

        default_area_id = _default_area_id(self.hass, device, self._device_areas)

        device_name = device.get("device_name", "") or device["device_id"]
        room_name = device.get("room_name", "") or "-"

        return self.async_show_form(
            step_id="area",
            data_schema=_area_schema(default_area_id),
            description_placeholders={
                "device_name": device_name,
                "room": room_name,
                "position": str(self._area_index + 1),
                "total": str(len(devices)),
            },
        )

    def _finish(self) -> FlowResult:
        selected_set = set(self._selected_ids)
        filtered_areas = {
            device_id: area_id
            for device_id, area_id in self._device_areas.items()
            if device_id in selected_set
        }
        return self.async_create_entry(
            title="",
            data={
                CONF_SELECTED_DEVICE_IDS: self._selected_ids,
                CONF_DEVICE_AREAS: filtered_areas,
            },
        )
