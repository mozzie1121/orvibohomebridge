import logging
import re
from typing import Optional
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .https_client import HttpsClient
from .cloud import CHINA_CLOUD, CloudEndpoint, cloud_for_region
from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_PASSWORD_HASH,
    CONF_CLOUD_REGION,
    CONF_FAMILY_ID,
    CONF_LOCK_USER_NAMES,
    CONF_TRANSPORT_MODE,
    CONF_USE_INDEPENDENT_LAN_CREDENTIALS,
    CONF_LAN_USERNAME,
    CONF_LAN_PASSWORD,
    CONF_LAN_PASSWORD_HASH,
    CONF_POLL_INTERVAL_MINUTES,
    DEFAULT_POLL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MINUTES,
    MAX_POLL_INTERVAL_MINUTES,
)
from .device_types import (
    DeviceCategory,
    classify_device,
    get_device_profile,
    is_hidden_category,
)
from .device_selection import device_selection_groups
from .lock_status import format_lock_user_names, parse_lock_user_names
from .capabilities import TransportMode
from .selection import CONF_SELECTED_DEVICE_IDS, selected_device_ids
from .protocol import password_hash

_LOGGER = logging.getLogger(__name__)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _device_label(device_id: str, name: str, room: str) -> str:
    """设备标签：名称 + 房间。"""
    if room and room != name:
        return f"{name} [{room}]"
    return name or device_id[-8:]


def _device_option_label(device: dict) -> str:
    """Add catalogue identity without implying unsupported control capability."""
    label = _device_label(
        str(device["device_id"]),
        str(device.get("device_name") or ""),
        str(device.get("room_name") or ""),
    )
    profile = get_device_profile(device)
    if profile.category == DeviceCategory.OTHER or (
        profile.registration_only
        and profile.category != DeviceCategory.UNKNOWN
    ):
        return f"{label}（已识别：{profile.info.label}，暂未支持）"
    if profile.category == DeviceCategory.UNKNOWN:
        return f"{label}（未识别，暂未支持）"
    return label


DEVICE_GROUP_MODE_ALL = "all"
DEVICE_GROUP_MODE_NONE = "none"
DEVICE_GROUP_MODE_CUSTOM = "custom"
CUSTOM_GROUP_FIELD_PREFIX = "custom_device_group_"


def _device_group_mode_schema(
    devices: list[dict], selected_ids: set[str]
) -> vol.Schema:
    """Select all, none, or custom for every broad device category."""

    fields: dict[object, object] = {}
    for group in device_selection_groups(devices):
        ids = set(group.device_ids)
        selected = ids & selected_ids
        if selected == ids:
            default = DEVICE_GROUP_MODE_ALL
        elif not selected:
            default = DEVICE_GROUP_MODE_NONE
        else:
            default = DEVICE_GROUP_MODE_CUSTOM
        fields[vol.Required(group.field, default=default)] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        DEVICE_GROUP_MODE_ALL,
                        DEVICE_GROUP_MODE_NONE,
                        DEVICE_GROUP_MODE_CUSTOM,
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                    translation_key="device_group_mode",
                )
            )
        )
    return vol.Schema(fields)


def _custom_device_schema(
    devices: list[dict], group_keys: set[str], selected_ids: set[str]
) -> vol.Schema:
    """Show concrete devices only for categories configured as custom."""

    fields: dict[object, object] = {}
    for group in device_selection_groups(devices):
        if group.key not in group_keys:
            continue
        field = f"{CUSTOM_GROUP_FIELD_PREFIX}{group.key}"
        default = [
            device_id for device_id in group.device_ids if device_id in selected_ids
        ]
        fields[vol.Optional(field, default=default)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=str(device["device_id"]),
                        label=_device_option_label(device),
                    )
                    for device in group.devices
                ],
                multiple=True,
                mode=selector.SelectSelectorMode.LIST,
            )
        )
    return vol.Schema(fields)


def _selection_plan(
    user_input: dict, devices: list[dict]
) -> tuple[list[str], set[str]]:
    """Resolve all/none categories and return categories needing a detail step."""

    selected: list[str] = []
    custom: set[str] = set()
    for group in device_selection_groups(devices):
        mode = str(user_input.get(group.field, DEVICE_GROUP_MODE_NONE))
        if mode == DEVICE_GROUP_MODE_ALL:
            selected.extend(group.device_ids)
        elif mode == DEVICE_GROUP_MODE_CUSTOM:
            custom.add(group.key)
    return selected, custom


def _merge_custom_selection(
    base_ids: list[str], user_input: dict, devices: list[dict], group_keys: set[str]
) -> list[str]:
    requested = set(base_ids)
    for group in device_selection_groups(devices):
        if group.key not in group_keys:
            continue
        values = user_input.get(f"{CUSTOM_GROUP_FIELD_PREFIX}{group.key}", [])
        if isinstance(values, (list, tuple, set)):
            requested.update(str(value) for value in values)
    return [
        str(device["device_id"])
        for device in devices
        if str(device["device_id"]) in requested
    ]


async def _fetch_devices(
    hass: HomeAssistant,
    username: str,
    password_digest: str,
    family_id: str,
    cloud: CloudEndpoint = CHINA_CLOUD,
) -> list[dict]:
    """拉取设备列表（含房间信息），过滤隐藏类别。"""
    client = None
    try:
        client = HttpsClient(
            username=username,
            password_hash=password_digest,
            session=async_get_clientsession(hass),
            cloud=cloud,
        )
        if family_id:
            client.family_id = family_id
        if not await client.ensure_login():
            return []
        data = await client.fetch_device_status()
        devices = client.parse_device_status_list(data) if data else []
    except Exception as e:
        _LOGGER.debug("获取设备列表失败: %s", e)
        return []
    finally:
        if client:
            await client.close()
    return [
        d for d in devices
        if not is_hidden_category(classify_device(d))
    ]


async def _probe_ssl_credentials(
    hass: HomeAssistant,
    cloud: CloudEndpoint,
    username: str,
    password_digest: str,
    family_id: str,
) -> bool:
    """Validate credentials against the binary SSL login endpoint."""
    from .const import SSL_PORT
    from .ssl_client import SSLClient

    client = SSLClient(
        hass=hass,
        ssl_host=cloud.ssl_host,
        ssl_port=SSL_PORT,
        username=username,
        password_hash=password_digest,
        family_id=family_id,
        on_session_id_obtained=lambda sid: None,
        on_status_update=lambda did, raw: None,
        retry_interval=0,
    )
    try:
        ok = await client.connect_and_login(max_attempts=1, hello_wait=1.0)
        if ok:
            return True
        status = getattr(client, "_login_status", None)
        # Keep the existing behaviour: an explicit non-zero login response is
        # an authentication failure; a network timeout must not lock users out.
        return status is None or status == 0
    finally:
        await client._disconnect()


async def _validate_updated_credentials(
    hass: HomeAssistant,
    username: str,
    password_digest: str,
    family_id: str,
    cloud: CloudEndpoint,
) -> Optional[CloudEndpoint]:
    """Detect the account region and validate a replacement password."""
    client = None
    try:
        client = HttpsClient(
            username=username,
            password_hash=password_digest,
            session=async_get_clientsession(hass),
            cloud=cloud,
        )
        if not await client.async_detect_cloud(family_id or None):
            return None
        detected_cloud = client.cloud
    except Exception:
        _LOGGER.debug("重新登录验证失败", exc_info=True)
        return None
    finally:
        if client:
            await client.close()

    try:
        valid = await _probe_ssl_credentials(
            hass,
            detected_cloud,
            username,
            password_digest,
            family_id,
        )
    except Exception:
        _LOGGER.debug("SSL 重新登录验证失败", exc_info=True)
        return None
    if not valid:
        return None
    return detected_cloud


class OrviboMeshConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._devices: list[dict] = []
        self._pending_selected_ids: list[str] = []
        self._selection_defaults: set[str] = set()
        self._selection_base_ids: list[str] = []
        self._custom_group_keys: set[str] = set()
        self._cloud = CHINA_CLOUD

    async def async_step_user(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            if not username or not password:
                errors["base"] = "empty_username_or_password"
            elif not re.match(r'^1[3-9]\d{9}$', username) and not re.match(r'^[^@]+@[^@]+\.[^@]+$', username):
                errors[CONF_USERNAME] = "invalid_username"

            if not errors:
                # 临时 client 用于验证登录并获取家庭列表
                temp_client = None
                try:
                    self._password_hash = password_hash(password)
                    temp_client = HttpsClient(
                        username=username,
                        password_hash=self._password_hash,
                        session=async_get_clientsession(self.hass),
                    )
                    success = await temp_client.async_detect_cloud()

                    if success:
                        # 保存数据到 self，后续步骤使用
                        self._username = username
                        self._family_list = temp_client.family_list
                        self._family_id = temp_client.family_id
                        self._family_name = temp_client.family_name
                        self._cloud = temp_client.cloud

                        # 前置认证校验：拿到第一个家庭 ID 后立即做 SSL 探针，
                        # 密码错误时在凭据表单直接提示，不再展示家庭列表
                        probe_family_id = (
                            str(self._family_list[0]["familyId"])
                            if self._family_list
                            else ""
                        )
                        if not await self._probe_ssl_login(probe_family_id):
                            errors["base"] = "auth_failed"
                        elif len(self._family_list) <= 1:
                            return await self.async_step_devices()
                        else:
                            return await self.async_step_select_family()
                    else:
                        errors["base"] = "auth_failed"
                except Exception as e:
                    _LOGGER.error(f"登录验证失败: {e}")
                    errors["base"] = "auth_failed"
                finally:
                    if temp_client:
                        await temp_client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_select_family(self, user_input: Optional[dict] = None) -> FlowResult:
        """选择家庭步骤"""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            family_id = user_input.get(CONF_FAMILY_ID)
            if family_id:
                self._family_id = family_id
                for f in self._family_list:
                    if f["familyId"] == family_id:
                        self._family_name = f["familyName"]
                        break
                return await self.async_step_devices()

        # 构建家庭选择列表
        family_choices = {
            f["familyId"]: f"{f['familyName']} ({f['familyId'][:8]}...)"
            for f in self._family_list
        }
        
        if len(family_choices) == 1:
            # 只有一个家庭，直接使用
            self._family_id = list(family_choices.keys())[0]
            return await self.async_step_devices()

        return self.async_show_form(
            step_id="select_family",
            data_schema=vol.Schema({
                vol.Required(CONF_FAMILY_ID): vol.In(family_choices),
            }),
            errors=errors,
            description_placeholders={
                "family_count": str(len(family_choices)),
            }
        )

    async def async_step_devices(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        """选择要接入 Home Assistant 的设备。"""
        errors: dict[str, str] = {}
        if not self._devices:
            self._devices = await _fetch_devices(
                self.hass,
                self._username,
                self._password_hash,
                self._family_id or "",
                self._cloud,
            )
            if not self._devices:
                errors["base"] = "no_devices"

        if user_input is not None:
            self._selection_base_ids, self._custom_group_keys = _selection_plan(
                user_input, self._devices
            )
            if self._custom_group_keys:
                return await self.async_step_custom_devices()
            self._pending_selected_ids = list(self._selection_base_ids)
            if not self._pending_selected_ids:
                errors["base"] = "no_devices_selected"
            else:
                return await self._create_entry()

        default_ids = {str(d["device_id"]) for d in self._devices}
        self._selection_defaults = default_ids
        return self.async_show_form(
            step_id="devices",
            data_schema=_device_group_mode_schema(self._devices, default_ids),
            errors=errors,
        )

    async def async_step_custom_devices(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        """Select individual devices for categories marked as custom."""

        errors: dict[str, str] = {}
        if user_input is not None:
            self._pending_selected_ids = _merge_custom_selection(
                self._selection_base_ids,
                user_input,
                self._devices,
                self._custom_group_keys,
            )
            if self._pending_selected_ids:
                return await self._create_entry()
            errors["base"] = "no_devices_selected"
        return self.async_show_form(
            step_id="custom_devices",
            data_schema=_custom_device_schema(
                self._devices,
                self._custom_group_keys,
                self._selection_defaults,
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Optional[dict] = None
    ) -> FlowResult:
        """开始重新认证（凭据失效时由 Home Assistant 自动触发）。"""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        """输入新密码并更新配置项。"""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")
        username = str(entry.data.get(CONF_USERNAME, ""))

        if user_input is not None:
            password = str(user_input.get(CONF_PASSWORD) or "")
            if not password:
                errors["base"] = "empty_username_or_password"
            else:
                password_digest = password_hash(password)
                detected_cloud = await _validate_updated_credentials(
                    self.hass,
                    username,
                    password_digest,
                    str(entry.data.get(CONF_FAMILY_ID, "")),
                    cloud_for_region(entry.data.get(CONF_CLOUD_REGION)),
                )
                if detected_cloud is not None:
                    data_updates = {
                        CONF_PASSWORD_HASH: password_digest,
                        CONF_CLOUD_REGION: detected_cloud.region.value,
                    }
                    # HA 2026.6+ delegates reload to the entry update listener;
                    # retain the older helper for the declared HA 2024.1 floor.
                    update_and_abort = getattr(
                        self, "async_update_and_abort", None
                    )
                    if update_and_abort is not None:
                        return update_and_abort(
                            entry,
                            data_updates=data_updates,
                        )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=data_updates,
                    )
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": username},
        )

    async def _probe_ssl_login(self, family_id: str) -> bool:
        """用 SSL 二进制登录校验密码真实有效。

        REST OAuth 不校验密码（任意密码都返回 token），真正的校验点在
        10002 端口 SSL 登录。仅当服务器明确拒绝（status 非空且非 0）时
        判定为认证失败；网络/超时类失败不阻塞配置流程。
        """
        return await _probe_ssl_credentials(
            self.hass,
            self._cloud,
            self._username,
            self._password_hash,
            family_id,
        )

    async def _create_entry(self) -> FlowResult:
        """创建配置条目"""
        # 找到家庭列表中的用户ID（临时 client 已关闭，使用暂存数据）
        await self.async_set_unique_id(self._username)
        self._abort_if_unique_id_configured()
        
        return self.async_create_entry(
            title=f"{self._username} - {self._family_name}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD_HASH: self._password_hash,
                CONF_FAMILY_ID: self._family_id,
                CONF_CLOUD_REGION: self._cloud.region.value,
            },
            options={
                CONF_SELECTED_DEVICE_IDS: self._pending_selected_ids,
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OrviboMeshOptionsFlow(config_entry)


class OrviboMeshOptionsFlow(config_entries.OptionsFlow):
    """重新选择接入的设备。"""

    def __init__(self, config_entry):
        # 新版 HA 将 OptionsFlow.config_entry 暴露为只读属性；
        # 用私有字段保存工厂参数，兼容新旧版本。
        self._config_entry = config_entry
        self._devices: list[dict] = []
        self._selection_defaults: set[str] = set()
        self._selection_base_ids: list[str] = []
        self._custom_group_keys: set[str] = set()

    async def async_step_init(self, user_input=None):
        """选项菜单：重新登录 / 选择设备 / 锁用户映射 / 传输模式。"""
        return self.async_show_menu(
            step_id="init",
        menu_options=[
            "transport_mode",
            "lan_credentials",
            "polling",
            "reauth",
            "devices",
            "lock_users",
        ],
        )

    async def async_step_transport_mode(self, user_input=None):
        """选择 LAN 优先、纯 LAN 或纯云端运行模式。"""
        if user_input is not None:
            options = dict(self._config_entry.options)
            options[CONF_TRANSPORT_MODE] = str(
                user_input.get(CONF_TRANSPORT_MODE, TransportMode.AUTO.value)
            )
            return self.async_create_entry(title="", data=options)

        current = self._config_entry.options.get(
            CONF_TRANSPORT_MODE, TransportMode.AUTO.value
        )
        return self.async_show_form(
            step_id="transport_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRANSPORT_MODE, default=current
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                TransportMode.AUTO.value,
                                TransportMode.LAN_ONLY.value,
                                TransportMode.CLOUD_ONLY.value,
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="transport_mode",
                        )
                    )
                }
            ),
        )

    async def async_step_lan_credentials(self, user_input=None):
        """配置仅用于 MixPad 局域网登录的独立凭据。"""
        errors: dict[str, str] = {}
        options = dict(self._config_entry.options)
        if user_input is not None:
            enabled = bool(
                user_input.get(CONF_USE_INDEPENDENT_LAN_CREDENTIALS, False)
            )
            username = str(user_input.get(CONF_LAN_USERNAME) or "").strip()
            password = str(user_input.get(CONF_LAN_PASSWORD) or "")
            digest = (
                password_hash(password)
                if password
                else str(options.get(CONF_LAN_PASSWORD_HASH) or "")
            )
            if enabled and (not username or not digest):
                errors["base"] = "lan_credentials_required"
            else:
                options[CONF_USE_INDEPENDENT_LAN_CREDENTIALS] = enabled
                if enabled:
                    options[CONF_LAN_USERNAME] = username
                    options[CONF_LAN_PASSWORD_HASH] = digest
                else:
                    options.pop(CONF_LAN_USERNAME, None)
                    options.pop(CONF_LAN_PASSWORD_HASH, None)
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="lan_credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USE_INDEPENDENT_LAN_CREDENTIALS,
                        default=bool(options.get(
                            CONF_USE_INDEPENDENT_LAN_CREDENTIALS, False
                        )),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_LAN_USERNAME,
                        default=str(options.get(CONF_LAN_USERNAME)
                                    or self._config_entry.data.get(CONF_USERNAME, "")),
                    ): selector.TextSelector(),
                    vol.Optional(CONF_LAN_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_polling(self, user_input=None):
        """配置非纯 LAN 模式下的云端快照轮询周期。"""
        options = dict(self._config_entry.options)
        if user_input is not None:
            options[CONF_POLL_INTERVAL_MINUTES] = _bounded_int(
                user_input.get(CONF_POLL_INTERVAL_MINUTES),
                DEFAULT_POLL_INTERVAL_MINUTES,
                MIN_POLL_INTERVAL_MINUTES,
                MAX_POLL_INTERVAL_MINUTES,
            )
            return self.async_create_entry(title="", data=options)
        current = _bounded_int(
            options.get(CONF_POLL_INTERVAL_MINUTES),
            DEFAULT_POLL_INTERVAL_MINUTES,
            MIN_POLL_INTERVAL_MINUTES,
            MAX_POLL_INTERVAL_MINUTES,
        )
        return self.async_show_form(
            step_id="polling",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL_MINUTES, default=current
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL_MINUTES,
                            max=MAX_POLL_INTERVAL_MINUTES,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    )
                }
            ),
        )

    async def async_step_reauth(self, user_input=None):
        """主动更新密码，同时保留当前配置项及全部实体设置。"""
        errors: dict[str, str] = {}
        username = str(self._config_entry.data.get(CONF_USERNAME, ""))

        if user_input is not None:
            password = str(user_input.get(CONF_PASSWORD) or "")
            if not password:
                errors["base"] = "empty_username_or_password"
            else:
                password_digest = password_hash(password)
                detected_cloud = await _validate_updated_credentials(
                    self.hass,
                    username,
                    password_digest,
                    str(self._config_entry.data.get(CONF_FAMILY_ID, "")),
                    cloud_for_region(
                        self._config_entry.data.get(CONF_CLOUD_REGION)
                    ),
                )
                if detected_cloud is not None:
                    updated_data = dict(self._config_entry.data)
                    updated_data[CONF_PASSWORD_HASH] = password_digest
                    updated_data[CONF_CLOUD_REGION] = detected_cloud.region.value
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data=updated_data,
                    )
                    # Do not create or replace an entry. The existing options,
                    # selected devices, areas and entity registry stay intact.
                    return self.async_abort(reason="reauth_successful")
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="reauth",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": username},
        )

    async def async_step_lock_users(self, user_input=None):
        """编辑门锁 userId → 名称 映射（每行 用户ID=名称）。"""
        if user_input is not None:
            options = dict(self._config_entry.options)
            options[CONF_LOCK_USER_NAMES] = parse_lock_user_names(
                user_input.get(CONF_LOCK_USER_NAMES, "")
            )
            return self.async_create_entry(title="", data=options)

        current = format_lock_user_names(
            self._config_entry.options.get(CONF_LOCK_USER_NAMES, {})
        )
        return self.async_show_form(
            step_id="lock_users",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LOCK_USER_NAMES,
                        default=current,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            multiline=True,
                            type=selector.TextSelectorType.TEXT,
                        )
                    )
                }
            ),
        )

    async def async_step_devices(self, user_input=None):
        """重新选择要接入的设备。"""
        errors: dict[str, str] = {}
        if not self._devices:
            self._devices = await _fetch_devices(
                self.hass,
                str(self._config_entry.data.get(CONF_USERNAME, "")),
                str(self._config_entry.data.get(CONF_PASSWORD_HASH, "")),
                str(self._config_entry.data.get(CONF_FAMILY_ID, "")),
                cloud_for_region(
                    self._config_entry.data.get(CONF_CLOUD_REGION)
                ),
            )
            if not self._devices:
                errors["base"] = "no_devices"

        if user_input is not None:
            self._selection_base_ids, self._custom_group_keys = _selection_plan(
                user_input, self._devices
            )
            if self._custom_group_keys:
                return await self.async_step_custom_devices()
            selected = list(self._selection_base_ids)
            if not selected:
                errors["base"] = "no_devices_selected"
            else:
                options = dict(self._config_entry.options)
                options[CONF_SELECTED_DEVICE_IDS] = selected
                return self.async_create_entry(
                    title="",
                    data=options,
                )

        current = selected_device_ids(
            self._config_entry.options,
            [str(d["device_id"]) for d in self._devices],
        )
        self._selection_defaults = set(current)
        return self.async_show_form(
            step_id="devices",
            data_schema=_device_group_mode_schema(self._devices, set(current)),
            errors=errors,
        )

    async def async_step_custom_devices(self, user_input=None):
        """Select individual devices for categories marked as custom."""

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = _merge_custom_selection(
                self._selection_base_ids,
                user_input,
                self._devices,
                self._custom_group_keys,
            )
            if selected:
                options = dict(self._config_entry.options)
                options[CONF_SELECTED_DEVICE_IDS] = selected
                return self.async_create_entry(title="", data=options)
            errors["base"] = "no_devices_selected"
        return self.async_show_form(
            step_id="custom_devices",
            data_schema=_custom_device_schema(
                self._devices,
                self._custom_group_keys,
                self._selection_defaults,
            ),
            errors=errors,
        )
