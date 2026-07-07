import logging
from typing import Optional

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, DEVICE_TYPE_LIGHT, DEVICE_TYPE_CLOTHES_HORSE
from .coordinator import OrviboMeshCoordinator
from .device_types import classify_device, DeviceCategory

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OrviboMeshCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for device_id, device in coordinator.devices.items():
        if device.get("device_type") == DEVICE_TYPE_LIGHT:
            entities.append(OrviboLight(coordinator, device))
        elif device.get("device_type") == DEVICE_TYPE_CLOTHES_HORSE:
            entities.append(OrviboClothesHorseLight(coordinator, device))

    async_add_entities(entities)


class OrviboLight(CoordinatorEntity, LightEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"orvibohomebridge_light_{self._device_id}"
        self._attr_name = device.get("device_name", self._device_id)

        ui_model = device.get("ui_model", "")
        device_type_raw = device.get("device_type_raw")
        class_id = device.get("class_id")
        model = device.get("model", "")
        properties = device.get("properties", {})
        
        # 转换为整数
        try:
            device_type_raw = int(device_type_raw) if device_type_raw is not None else None
            class_id = int(class_id) if class_id is not None else None
        except (TypeError, ValueError):
            device_type_raw = None
            class_id = None
        
        sub_device_type = device.get("sub_device_type")
        status = device.get("status", {})
        _LOGGER.info(f"灯光设备初始化: name={self._attr_name}, device_type_raw={device_type_raw} (type={type(device_type_raw)}), class_id={class_id} (type={type(class_id)}), sub_device_type={sub_device_type}, status={status}, device={device}")
        
        # 通过 sub_device_type=-2 和 status 中的 value2（亮度值）判断为调光灯
        if device_type_raw is None and sub_device_type == -2 and isinstance(status, dict):
            if "value2" in status:
                device_type_raw = 0
                _LOGGER.info(f"通过 sub_device_type=-2 和 value2 推断 device_type_raw=0")
        
        # 优先通过 classify_device 判断（最准确）
        category = classify_device(device)
        
        # type=503 色温灯带亮度范围 0-100，type=502 可调光灯亮度范围 0-100，type=38 调光调色灯亮度范围 0-255，type=0 调光灯亮度范围 0-255
        # FAST_MOVE_DIM_COLOR_LIGHT (subDeviceType=6) 亮度范围 0-255
        self._brightness_is_percent = (device_type_raw in (502, 503))
        if category == DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT:
            self._brightness_is_percent = False

        _LOGGER.info(f"灯光设备: {self._attr_name}, device_id={self._device_id}, ui_model={ui_model}, deviceType={device_type_raw} (type={type(device_type_raw)}), classId={class_id} (type={type(class_id)}), model={model}")

        is_dimmable = False

        if category in (DeviceCategory.DIM_COLOR_LIGHT, DeviceCategory.DIMMABLE_LIGHT, DeviceCategory.CCT_LIGHT, DeviceCategory.ZIGBEE_DIMMABLE_LIGHT, DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT):
            is_dimmable = True
            _LOGGER.info(f"通过 classify_device 判断为调光灯: category={category}")
        # 通过 ui.model 判断
        elif ui_model in ("light_colortemp", "light_dimmable", "light_color") or "colortemp" in ui_model or "dimmable" in ui_model:
            is_dimmable = True
            _LOGGER.info(f"通过 ui_model 判断为调光灯: {ui_model}")
        # deviceType=38 调光调色灯，deviceType=502 可调光灯（仅亮度），deviceType=503 色温灯带，deviceType=0 调光灯（仅亮度）
        elif device_type_raw in (38, 502, 503, 0):
            is_dimmable = True
            _LOGGER.info(f"通过 deviceType={device_type_raw} 判断为调光灯")
        # 通过 properties 中是否有 brightness 或 colortemp 字段判断
        elif isinstance(properties, dict):
            has_brightness = "brightness" in properties
            has_colortemp = "colortemp" in properties
            if has_brightness or has_colortemp:
                is_dimmable = True
                _LOGGER.info(f"通过 properties 判断为调光灯: brightness={has_brightness}, colortemp={has_colortemp}")
        # classId=426/436 调光灯
        elif class_id in (426, 436):
            is_dimmable = True
            _LOGGER.info(f"通过 classId={class_id} 判断为调光灯")

        if is_dimmable:
            if category in (DeviceCategory.DIMMABLE_LIGHT, DeviceCategory.ZIGBEE_DIMMABLE_LIGHT):
                self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
                self._attr_color_mode = ColorMode.BRIGHTNESS
                _LOGGER.info(f"设置为 BRIGHTNESS 模式: category={category}")
            elif category == DeviceCategory.FAST_MOVE_DIM_COLOR_LIGHT:
                self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._min_mireds = 167  # 6000K
                self._max_mireds = 370  # 2700K
                self._attr_min_color_temp_kelvin = 2700
                self._attr_max_color_temp_kelvin = 6000
                _LOGGER.info(f"设置为 COLOR_TEMP 模式 (Fast Move): category={category}")
            else:
                self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
                self._attr_color_mode = ColorMode.COLOR_TEMP
                self._min_mireds = 154  # 6500K
                self._max_mireds = 370  # 2700K
                self._attr_min_color_temp_kelvin = 2700
                self._attr_max_color_temp_kelvin = 6500
                _LOGGER.info(f"设置为 COLOR_TEMP 模式: category={category}")
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF
            _LOGGER.info(f"设置为 ONOFF 模式")

        _LOGGER.info(f"创建灯光实体: {self._attr_name}, supported_modes={self._attr_supported_color_modes}, is_dimmable={is_dimmable}, brightness={device.get('brightness')}, color_temp={device.get('color_temp')}")

    @property
    def is_on(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("state", False) if state else False

    @property
    def available(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("online", False) if state else False

    @property
    def brightness(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state and state.get("state", False):
            brightness = state.get("brightness")
            if brightness is not None:
                brightness = int(brightness)
                if self._brightness_is_percent:
                    # type=503 亮度范围 0-100，转换为 HA 的 0-255
                    return min(int(brightness * 255 / 100), 255)
                # type=38 亮度范围 0-255，直接返回
                return min(brightness, 255)
        return None

    @property
    def color_temp(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state and state.get("state", False):
            color_temp = state.get("color_temp")
            if color_temp is not None and color_temp > 0:
                # state 中 color_temp 单位为 Kelvin，HA 需要 mireds
                return int(1000000 / color_temp)
        return None

    @property
    def color_temp_kelvin(self) -> Optional[int]:
        state = self.coordinator.get_device_state(self._device_id)
        if state and state.get("state", False):
            color_temp = state.get("color_temp")
            if color_temp is not None and color_temp > 0:
                return int(color_temp)
        return None

    @property
    def min_mireds(self) -> int:
        return self._min_mireds

    @property
    def max_mireds(self) -> int:
        return self._max_mireds

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", self._device_id),
            "manufacturer": MANUFACTURER,
            "model": self._device.get("model", "Orvibo Light"),
            "sw_version": "1.0",
        }

    async def async_turn_on(self, **kwargs) -> None:
        state = self.coordinator.get_device_state(self._device_id)
        current_state = state.get("state", False) if state else False

        brightness = kwargs.get("brightness")
        # HA 2024+ 优先传 color_temp_kelvin（开尔文），旧版本传 color_temp（mireds）
        color_temp_mired = kwargs.get("color_temp")
        color_temp_kelvin = kwargs.get("color_temp_kelvin")

        # 统一转换为开尔文（设备协议使用开尔文）
        color_temp_k = None
        if color_temp_kelvin is not None:
            color_temp_k = int(color_temp_kelvin)
        elif color_temp_mired is not None and color_temp_mired > 0:
            color_temp_k = int(1000000 / color_temp_mired)

        _LOGGER.info(f"async_turn_on: kwargs={kwargs}, current_state={current_state}, color_temp_k={color_temp_k}")

        if brightness is not None and color_temp_k is not None:
            # HA brightness 范围 0-255
            brightness_value = min(int(brightness), 255)
            if brightness_value == 0:
                brightness_value = 1
            # type=503 设备亮度范围 0-100，需要转换
            if self._brightness_is_percent:
                device_brightness = max(1, int(brightness_value * 100 / 255))
            else:
                device_brightness = brightness_value
            _LOGGER.info(f"同时设置亮度和色温: brightness={device_brightness}, color_temp={color_temp_k}K")
            # ssl_control_light_brightness 使用 order="on" 已经开灯，无需再调用 async_turn_on
            # 否则 async_turn_on 不带参数会下发 value2=0/value3=0 覆盖刚设置的亮度
            await self.coordinator.async_set_brightness(self._device_id, device_brightness)
            await self.coordinator.async_set_color_temp(self._device_id, color_temp_k)
        elif brightness is not None:
            brightness_value = min(int(brightness), 255)
            if brightness_value == 0:
                brightness_value = 1
            # type=503 设备亮度范围 0-100，需要转换
            if self._brightness_is_percent:
                device_brightness = max(1, int(brightness_value * 100 / 255))
            else:
                device_brightness = brightness_value
            _LOGGER.info(f"设置亮度: HA={brightness} -> 设备={device_brightness}")
            # ssl_control_light_brightness 使用 order="on" 已经开灯，无需再调用 async_turn_on
            await self.coordinator.async_set_brightness(self._device_id, device_brightness)
        elif color_temp_k is not None:
            _LOGGER.info(f"设置色温: {color_temp_k}K")
            # ssl_control_light_colortemp 使用 order="fast color temperature" 已包含当前亮度
            # 与 standalone 行为一致，不额外调用 async_turn_on
            await self.coordinator.async_set_color_temp(self._device_id, color_temp_k)
        else:
            await self.coordinator.async_turn_on(self._device_id)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_turn_off(self._device_id)


class OrviboClothesHorseLight(CoordinatorEntity, LightEntity):
    """晾衣架照明 Light 实体（仅 on/off，无亮度色温）。"""
    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, coordinator: OrviboMeshCoordinator, device: dict):
        super().__init__(coordinator)
        self._device = device
        self._device_id = device["device_id"]
        self._attr_unique_id = f"orvibohomebridge_clothes_horse_light_{self._device_id}"
        self._attr_name = "照明"
        self._attr_icon = "mdi:lightbulb"

    @property
    def is_on(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("lighting_state", False) if state else False

    @property
    def available(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id)
        return state.get("online", False) if state else False

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("device_name", self._device_id),
            "manufacturer": MANUFACTURER,
            "model": self._device.get("model", "Orvibo Clothes Horse"),
            "sw_version": "1.0",
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_clothes_horse_control(self._device_id, "lighting", "on")

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_clothes_horse_control(self._device_id, "lighting", "off")
