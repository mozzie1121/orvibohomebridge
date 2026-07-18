"""纯协议层：Orvibo REST API 数据模型与签名/解析函数。

零外部依赖（仅 Python 标准库），可在无 Home Assistant 环境下独立测试。

包含：
- OrviboDevice dataclass（标准化设备描述）
- OrviboFamily dataclass（标准化家庭描述）
- 签名函数（password_hash / sign_request）
- 请求构建（build_family_request / build_readtable_request）
- 响应解析（parse_families / parse_readtable_devices）
- 二进制协议设备提取（extract_devices）
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
from typing import Any, Final, Mapping, Sequence

_HMAC_SECRET: Final = "nQ45RjPtOws96jmH"


@dataclass(frozen=True, slots=True)
class OrviboFamily:
    """一个 Orvibo 家庭。"""

    family_id: str
    name: str


@dataclass(frozen=True, slots=True)
class OrviboDevice:
    """标准化设备描述（从 readtable 或 binary 协议提取）。"""

    uid: str                     # deviceId
    name: str                    # deviceName
    model: str                   # model / modelName
    device_type: str             # deviceType (字符串)
    sub_device_type: str         # subDeviceType (字符串)
    room: str                    # 房间名（从 roomId 映射）
    parent_uid: str              # 网关 parentId
    online: bool | None          # 在线状态
    cloud_uid: str = ""          # uid（硬件 UID，与 deviceId 不同）
    value1: int | None = None
    value2: int | None = None
    value3: int | None = None
    value4: int | None = None
    class_id: int | None = None  # classId（部分设备通过 classId 识别）
    ui_model: str = ""           # ui.model
    room_name: str = ""          # 直接从设备条目读取的 roomName（备选）
    ext_addr: str = ""           # extAddr（Zigbee 扩展地址）


def password_hash(password: str) -> str:
    """返回 Orvibo 要求的大写 MD5 密码哈希。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest().upper()  # noqa: S324


def _sign_request(body: Mapping[str, Any]) -> str:
    """对 Orvibo REST 请求进行 HMAC-SHA256 签名。"""
    canonical = "&".join(f"{key}={body[key]}" for key in sorted(body))
    canonical = f"{canonical}&key={_HMAC_SECRET}"
    return hmac.new(
        _HMAC_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()


def build_family_request(
    access_token: str,
    user_id: str,
    timestamp_ms: int,
    nonce: int,
) -> dict[str, str]:
    """构建带签名的家庭列表请求体。"""
    body = {
        "accessToken": access_token,
        "userId": user_id,
        "timestamp": str(timestamp_ms),
        "random": str(nonce),
    }
    body["sign"] = _sign_request(body)
    return body


def build_readtable_request(
    access_token: str,
    user_id: str,
    family_id: str,
    session_id: str,
    timestamp_ms: int,
    serial: int,
    nonce: str,
    version: str = "5.2.6.302",
) -> dict[str, Any]:
    """构建 /v2/cmd/app/readtable 请求体。"""
    body: dict[str, Any] = {
        "accessToken": access_token,
        "dataType": "all",
        "deviceFlag": 0,
        "familyId": family_id,
        "lastUpdateTime": 0,
        "pageIndex": 0,
        "random": nonce,
        "serial": serial,
        "sessionId": session_id,
        "timestamp": timestamp_ms,
        "userId": user_id,
        "userName": user_id,
        "ver": version,
    }
    body["sign"] = _sign_request(body)
    return body


def _first_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """按优先级返回第一个非空字符串字段值。"""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_online(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "online", "connected"}:
            return True
        if normalized in {"0", "false", "offline", "disconnected"}:
            return False
    return None


def _parse_room(
    item: Mapping[str, Any],
    room_names: Mapping[str, str],
) -> str:
    """从设备条目中提取房间名（优先 roomName 字段，再按 roomId 映射）。"""
    room_name = _first_text(item, ("roomName", "room"))
    if room_name:
        return room_name
    room_id = _first_text(item, ("roomId", "roomID"))
    if room_id and room_id in room_names:
        return room_names[room_id]
    return ""


def parse_families(payload: Mapping[str, Any]) -> tuple[OrviboFamily, ...]:
    """归一化家庭响应解析（兼容多种字段名）。"""
    raw: Any = payload.get("data", [])
    if isinstance(raw, Mapping):
        raw = raw.get("families") or raw.get("familyList") or raw.get("list") or []
    if not isinstance(raw, list):
        return ()

    families: list[OrviboFamily] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        family_id = str(
            item.get("familyId") or item.get("family_id") or item.get("id") or ""
        ).strip()
        if not family_id or family_id in seen:
            continue
        seen.add(family_id)
        name = str(
            item.get("familyName")
            or item.get("family_name")
            or item.get("name")
            or family_id
        ).strip()
        families.append(OrviboFamily(family_id=family_id, name=name or family_id))
    return tuple(families)


def parse_readtable_devices(
    payload: Mapping[str, Any],
) -> tuple[OrviboDevice, ...]:
    """解析 /v2/cmd/app/readtable 响应，返回归一化的设备列表。

    支持 readtable 的设备 (device[]) / 状态 (deviceStatus[]) / 房间 (room[]) 表联合查询。
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ()

    # --- 房间映射 ---
    raw_rooms = data.get("room", [])
    room_names: dict[str, str] = {}
    if isinstance(raw_rooms, list):
        for item in raw_rooms:
            if not isinstance(item, Mapping) or item.get("delFlag") in (1, "1"):
                continue
            room_id = _first_text(item, ("roomId", "roomID"))
            room_name = _first_text(item, ("roomName",))
            if room_id and room_name:
                room_names[room_id] = room_name

    # --- 设备状态映射（deviceStatus[]）---
    raw_statuses = data.get("deviceStatus", [])
    online_by_device: dict[str, bool | None] = {}
    values_by_device: dict[str, tuple[int | None, ...]] = {}
    if isinstance(raw_statuses, list):
        for item in raw_statuses:
            if not isinstance(item, Mapping) or item.get("delFlag") in (1, "1"):
                continue
            device_id = _first_text(item, ("deviceId", "deviceID"))
            if device_id:
                online_by_device[device_id] = _parse_online(item.get("online"))
                values: list[int | None] = []
                for key in ("value1", "value2", "value3", "value4"):
                    try:
                        values.append(int(item[key]))
                    except (KeyError, TypeError, ValueError):
                        values.append(None)
                values_by_device[device_id] = tuple(values)

    # --- 设备列表（device[]）---
    raw_devices = data.get("device", [])
    if not isinstance(raw_devices, list):
        return ()

    devices: dict[str, OrviboDevice] = {}
    for item in raw_devices:
        if not isinstance(item, Mapping) or item.get("delFlag") in (1, "1"):
            continue
        device_id = _first_text(
            item,
            ("deviceId", "deviceID", "deviceUid", "deviceUUID", "uid"),
        )
        if not device_id:
            continue

        room_id = _first_text(item, ("roomId", "roomID"))
        online = online_by_device.get(device_id)
        if device_id not in online_by_device:
            online = _parse_online(item.get("online"))
        values = values_by_device.get(device_id, (None, None, None, None))

        # 提取 ui.model
        ui = item.get("ui", {})
        ui_model = ui.get("model", "") if isinstance(ui, dict) else ""

        # 提取 classId（可能在 properties.Descriptor 里）
        class_id: int | None = _safe_int(item.get("classId"))
        if class_id is None:
            properties = item.get("properties", {})
            if isinstance(properties, dict):
                descriptor = properties.get("Descriptor", {})
                if isinstance(descriptor, dict):
                    class_id = _safe_int(descriptor.get("classId"))

        devices[device_id] = OrviboDevice(
            uid=device_id,
            name=_first_text(
                item,
                ("deviceName", "devName", "name", "nickName", "nickname"),
            ),
            model=_first_text(
                item,
                (
                    "model",
                    "modelName",
                    "modelId",
                    "modelID",
                    "productName",
                    "productId",
                    "productID",
                ),
            ),
            device_type=_first_text(
                item,
                ("deviceType", "devType", "type", "category", "deviceCategory"),
            ),
            sub_device_type=_first_text(
                item,
                ("subDeviceType", "subDevType"),
            ),
            room=_parse_room(item, room_names),
            parent_uid=_first_text(
                item,
                ("parentUid", "parentId", "parentID", "gatewayUid", "hubUid"),
            ),
            online=online,
            cloud_uid=_first_text(item, ("uid",)),
            value1=values[0],
            value2=values[1],
            value3=values[2],
            value4=values[3],
            class_id=class_id,
            ui_model=ui_model,
        )

    return tuple(sorted(devices.values(), key=lambda d: d.uid))


def extract_devices(payloads: Any) -> tuple[OrviboDevice, ...]:
    """从二进制协议（cmd=147/230）递归提取设备列表。

    处理多种 payload 结构：
    - {cmd: 147, tableNameList: [{tableName: "device", dataList: [...]}]}
    - {cmd: 230, deviceList: [...]}
    - 嵌套根数据
    """
    devices: dict[str, OrviboDevice] = {}
    room_names: dict[str, str] = {}
    id_keys = (
        "deviceId", "deviceID", "deviceUid", "deviceUUID",
        "deviceUuid", "uuid", "uid",
    )
    device_markers = (
        "uid", "extAddr", "deviceName", "devName",
        "deviceType", "devType", "model", "modelName",
        "productId", "productID",
    )

    def collect_room_names(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                collect_room_names(child)
            return
        if not isinstance(value, Mapping):
            return
        room_id = _first_text(value, ("roomId", "roomID"))
        room_name = _first_text(value, ("roomName",))
        if room_id and room_name:
            room_names[room_id] = room_name
        for child in value.values():
            if isinstance(child, (Mapping, list)):
                collect_room_names(child)

    collect_room_names(payloads)

    def visit(value: Any, table_name: str | None = None) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child, table_name)
            return
        if not isinstance(value, Mapping):
            return

        raw_table_name = value.get("tableName")
        if isinstance(raw_table_name, str) and raw_table_name.strip():
            table_name = raw_table_name.strip().lower().replace("_", "")
        is_device_table = table_name in (
            None, "device", "devices", "devicelist",
            "privacydevice", "privacydevices",
        )

        uid = _first_text(value, id_keys)
        is_device_row = any(
            value.get(key) not in (None, "") for key in device_markers
        )
        if len(uid) >= 6 and is_device_row and is_device_table:
            room_id = _first_text(value, ("roomId", "roomID"))
            candidate = OrviboDevice(
                uid=uid,
                name=_first_text(
                    value,
                    ("deviceName", "devName", "name", "nickName", "nickname"),
                ),
                model=_first_text(
                    value,
                    (
                        "model", "modelName", "modelId", "modelID",
                        "productName", "productId", "productID",
                    ),
                ),
                device_type=_first_text(
                    value,
                    ("deviceType", "devType", "type", "category", "deviceCategory"),
                ),
                sub_device_type=_first_text(
                    value, ("subDeviceType", "subDevType"),
                ),
                room=_parse_room(value, room_names),
                parent_uid=_first_text(
                    value,
                    ("parentUid", "parentId", "parentID", "gatewayUid", "hubUid"),
                ),
                online=_parse_online(
                    next(
                        (
                            value[key]
                            for key in ("online", "isOnline", "connected")
                            if key in value
                        ),
                        None,
                    )
                ),
            )
            previous = devices.get(uid)
            if previous is None:
                devices[uid] = candidate
            else:
                devices[uid] = OrviboDevice(
                    uid=uid,
                    name=candidate.name or previous.name,
                    model=candidate.model or previous.model,
                    device_type=candidate.device_type or previous.device_type,
                    sub_device_type=candidate.sub_device_type or previous.sub_device_type,
                    room=candidate.room or previous.room,
                    parent_uid=candidate.parent_uid or previous.parent_uid,
                    online=(
                        candidate.online
                        if candidate.online is not None
                        else previous.online
                    ),
                )

        for child in value.values():
            if isinstance(child, (Mapping, list)):
                visit(child, table_name)

    visit(payloads)
    return tuple(sorted(devices.values(), key=lambda d: d.uid))


# --- 兼容函数：将 OrviboDevice 转换为现有 https_client 的 dict 格式 ---

def device_to_dict(device: OrviboDevice) -> dict[str, Any]:
    """将 OrviboDevice 转为 https_client.parse_device_status_list 兼容的 dict。

    这样 coordinator 和数据平台层无需改动。
    """
    _initial_state = _infer_initial_state(device)
    return {
        "device_id": device.uid,
        "device_name": device.name,
        "device_type": _infer_ha_device_type(device),
        "device_type_raw": _safe_int(device.device_type),
        "sub_device_type": _safe_int(device.sub_device_type),
        "class_id": device.class_id,
        "uid": device.cloud_uid,
        "status_id": "",
        "gateway_id": "",
        "ext_addr": "",
        "model": device.model,
        "ui_model": device.ui_model,
        "room_id": "",
        "room_name": device.room,
        "online": device.online or False,
        "state": _initial_state,
        "position": device.value1 if device.value1 is not None else 0,
        "brightness": device.value2,
        "color_temp": device.value3,
        "fan_speed": "停",
        "temperature": None,
        "properties": {},
        "endpoint": 0,
        "status": {},
    }


def _infer_ha_device_type(device: OrviboDevice) -> str:
    """根据 device_type 推断 HA 平台类型（兼容 const.DEVICE_TYPE_MAP 逻辑）。

    注：这是一个常量映射的内联版，与 const.DEVICE_TYPE_MAP 保持一致。
    需要同步更新 const.py 的 DEVICE_TYPE_MAP 时也更新这里。
    """
    _DEVICE_TYPE_MAP_INLINE = {
        1: "light", 34: "cover", 36: "climate", 38: "light",
        46: "sensor", 52: "clothes_horse", 102: "light",
        25: "sensor", 26: "sensor", 27: "sensor", 56: "sensor",
        54: "sensor", 300: "sensor", 501: "light", 502: "light",
        503: "light", 516: "fan", 522: "sensor", 10086: "light",
        0: "light",
    }
    _CLASS_ID_MAP_INLINE = {
        426: "light", 429: "light", 436: "light", 1114: "fan",
    }

    dt = _safe_int(device.device_type)
    if dt and dt in _DEVICE_TYPE_MAP_INLINE:
        return _DEVICE_TYPE_MAP_INLINE[dt]
    if device.class_id and device.class_id in _CLASS_ID_MAP_INLINE:
        return _CLASS_ID_MAP_INLINE[device.class_id]
    return "light"  # 兜底


def _infer_initial_state(device: OrviboDevice) -> bool:
    """根据 device_type 和 value1 推断初始开关状态。"""
    dt = _safe_int(device.device_type)
    v1 = device.value1
    if v1 is None:
        return False
    if dt in (1, 102, 38, 503, 0):
        return int(v1) == 0  # active-low
    if dt in (135, 136, 137, 143, 518):
        return int(v1) == 1  # active-high
    if dt in (34, 52):
        return int(v1) > 0  # 窗帘位置 >0 为开
    return False


def parse_readtable_to_device_dicts(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """解析 readtable 返回，直接生成兼容 https_client 的 dict 列表。

    这是 https_client.parse_device_status_list() 的纯函数替代。
    """
    devices = parse_readtable_devices(payload)
    return [device_to_dict(d) for d in devices]


def parse_families_to_dicts(
    payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """解析家庭列表，返回兼容 https_client.family_list 的 dict 列表。"""
    families = parse_families(payload)
    return [
        {"familyId": f.family_id, "familyName": f.name} for f in families
    ]


def parse_readtable_rooms(
    payload: Mapping[str, Any],
) -> dict[str, str]:
    """从 readtable 响应中提取 roomId → roomName 映射。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {}
    raw_rooms = data.get("room", [])
    rooms: dict[str, str] = {}
    if isinstance(raw_rooms, list):
        for item in raw_rooms:
            if not isinstance(item, Mapping) or item.get("delFlag") in (1, "1"):
                continue
            room_id = _first_text(item, ("roomId", "roomID"))
            room_name = _first_text(item, ("roomName",))
            if room_id and room_name:
                rooms[room_id] = room_name
    return rooms
