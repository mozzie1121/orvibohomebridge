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
                try:
                    # 存储用户名密码用于后续步骤
                    self._https_client = HttpsClient(username=username, password=password)
                    success = await self._https_client.ensure_login()

                    if success:
                        # 如果只有一个家庭，直接使用；否则让用户选择
                        if len(self._https_client.family_list) <= 1:
                            return await self._create_entry()
                        else:
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

    async def async_step_select_family(self, user_input: Optional[dict] = None) -> FlowResult:
        """选择家庭步骤"""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            family_id = user_input.get(CONF_FAMILY_ID)
            if family_id:
                self._https_client.set_family(family_id)
                return await self._create_entry()

        # 构建家庭选择列表
        family_choices = {
            f["familyId"]: f"{f['familyName']} ({f['familyId'][:8]}...)"
            for f in self._https_client.family_list
        }
        
        if len(family_choices) == 1:
            # 只有一个家庭，直接使用
            self._https_client.set_family(list(family_choices.keys())[0])
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

    async def _create_entry(self) -> FlowResult:
        """创建配置条目"""
        await self.async_set_unique_id(self._https_client.user_id)
        self._abort_if_unique_id_configured()
        
        return self.async_create_entry(
            title=f"{self._https_client.username} - {self._https_client.family_name}",
            data={
                CONF_USERNAME: self._https_client.username,
                CONF_PASSWORD: self._https_client.password,
                CONF_FAMILY_ID: self._https_client.family_id,
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
