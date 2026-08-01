import logging
import re
from typing import Optional
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from .https_client import HttpsClient
from .const import (
    DOMAIN, CONF_USERNAME, CONF_PASSWORD, CONF_FAMILY_ID,
)

_LOGGER = logging.getLogger(__name__)


class OrviboMeshConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
                    temp_client = HttpsClient(username=username, password=password)
                    success = await temp_client.ensure_login()

                    if success:
                        # 保存数据到 self，后续步骤使用
                        self._username = username
                        self._password = password
                        self._family_list = temp_client.family_list
                        self._family_id = temp_client.family_id
                        self._family_name = temp_client.family_name

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
                            return await self._create_entry()
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
                return await self._create_entry()

        # 构建家庭选择列表
        family_choices = {
            f["familyId"]: f"{f['familyName']} ({f['familyId'][:8]}...)"
            for f in self._family_list
        }
        
        if len(family_choices) == 1:
            # 只有一个家庭，直接使用
            self._family_id = list(family_choices.keys())[0]
            return await self._create_entry()

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
                temp_client = None
                success = False
                try:
                    temp_client = HttpsClient(username=username, password=password)
                    success = await temp_client.ensure_login()
                except Exception:
                    success = False
                finally:
                    if temp_client:
                        await temp_client.close()
                if success:
                    self._username = username
                    self._password = password
                    family_id = str(entry.data.get(CONF_FAMILY_ID, ""))
                    if await self._probe_ssl_login(family_id):
                        return self.async_update_reload_and_abort(
                            entry,
                            data_updates={CONF_PASSWORD: password},
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
        from .ssl_client import SSLClient
        from .const import SSL_HOST, SSL_PORT

        client = SSLClient(
            hass=self.hass,
            ssl_host=SSL_HOST,
            ssl_port=SSL_PORT,
            username=self._username,
            password=self._password,
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
            return status is None or status == 0
        finally:
            await client._disconnect()

    async def _create_entry(self) -> FlowResult:
        """创建配置条目"""
        # 找到家庭列表中的用户ID（临时 client 已关闭，使用暂存数据）
        await self.async_set_unique_id(self._username)
        self._abort_if_unique_id_configured()
        
        return self.async_create_entry(
            title=f"{self._username} - {self._family_name}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_FAMILY_ID: self._family_id,
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OrviboMeshOptionsFlow(config_entry)


class OrviboMeshOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry
        self._https_client: Optional[HttpsClient] = None

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_USERNAME,
                    default=self.config_entry.data.get(CONF_USERNAME)
                ): str,
                vol.Optional(
                    CONF_PASSWORD,
                    default=""
                ): str,
            }),
        )
