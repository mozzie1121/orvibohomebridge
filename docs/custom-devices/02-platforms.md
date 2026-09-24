# 各平台必填字段与完整示例

`platform` 决定这个 profile 会被哪个 HA 平台创建实体。本页只写**当前确实会创建自定义实体**的 5 个平台：`light`、`switch`、`cover`、`sensor`、`binary_sensor`。

字段含义和取值规则见 [01-schema.md](01-schema.md)；`climate` / `fan` 的现状见本页最后一节。

## 通用必填项

无论哪个平台，这些字段都必填或强烈建议填写：

| 字段 | 说明 |
|---|---|
| `profile_version: 1` | 必填，只能是 1 |
| `id` | 必填，全局唯一，`[a-z0-9][a-z0-9_\-]*` |
| `platform` | 必填 |
| `match` | 必填，至少一个条件 |
| `state` | 强烈建议填写，否则实体状态只能来自云端快照 |
| `control` | 需要控制就写；不写则自动 `status_only`，永远不下发命令 |
| `hardware_verified` | 真机验证通过后才写 `true`，否则设备只登记 |

平台与"动作/能力"的对应关系（写漏了会在点击时抛 `ControlNotDeclared`）：

| 平台 | 实体类 | 必须声明的动作 | 建议的 capabilities |
|---|---|---|---|
| `light` | `OrviboCustomLight` | `on`、`off`；声明了 `brightness` 能力就要有 `brightness` 动作；声明了 `color_temp` 能力就要有 `color_temp` 动作 | `onoff`、`brightness`、`color_temp` |
| `switch` | `OrviboCustomSwitch` | `on`、`off` | `onoff` |
| `cover` | `OrviboCustomCover` | **`position`**（开关/设位置都走它）、`stop` | `position`、`stop` |
| `sensor` | `OrviboCustomSensor` | 不需要（只读） | — |
| `binary_sensor` | `OrviboCustomBinarySensor` | 不需要（只读） | — |

## `light`

**必填**：`platform: light`、`match`、`control.on`、`control.off`。

**实体行为**（`light.py::OrviboCustomLight`）：

- `capabilities` 决定颜色模式：含 `color_temp` → `COLOR_TEMP`；否则含 `brightness` → `BRIGHTNESS`；否则 `ONOFF`（一个都没声明时也按 `ONOFF` 处理）；
- 色温范围取 `constraints.color_temp_range`，未写时默认 `2700–6500 K`；
- 亮度按 HA 的 `0..255` 读取（`state.brightness` 超出会被裁剪到 `0..255`）；
- 色温按 **Kelvin** 读取（`state.color_temp`）；
- `is_on` 读 `state.state`，`available` 读 `online`；
- 点击"打开"且**不带**亮度/色温时调用 `on` 动作；只调亮度时调用 `brightness` 动作；调色温时调用 `color_temp` 动作（同时会把亮度作为参数一起传入）；"关闭"调用 `off` 动作。

### 示例 1：属性型（`set property`）调光调色灯

开关用 `set property` 属性报文（`onoff.status`），亮度/色温用 `fast move to level` / `fast color temperature`（这两个命令能携带动态数值）；如果设备只认属性报文，也可以在 `properties` 里用 `"param:brightness"` 占位符，见 [01-schema.md](01-schema.md#properties-与动态数值)。

```yaml
profile_version: 1
id: sp_cct_light
display_name: 属性型调光调色灯
platform: light
match:
  device_type: 9001
hardware_verified: true
capabilities: [onoff, brightness, color_temp]
constraints:
  color_temp_range: [2700, 6500]
state:
  state:
    from: properties.onoff.status
    true_values: ["on"]
    false_values: ["off"]
  brightness:
    from: [properties.brightness.percent, value2]
    scale: {min: 0, max: 100, to_min: 0, to_max: 255}
    default: 0
  color_temp:
    from: [properties.colorTemp.value, value3]
    clamp: {min: 2700, max: 6500}
control:
  on:
    order: set property
    properties:
      onoff: {status: "on"}
  off:
    order: set property
    properties:
      onoff: {status: "off"}
  brightness:
    order: fast move to level
    value1: 0
    value2: "param:brightness"
    value3: {from: color_temp, input_unit: kelvin, output_unit: mired, default: 370}
  color_temp:
    order: fast color temperature
    value1: 0
    value2: {from: brightness, default: 255}
    value3: {param: color_temp, input_unit: kelvin, output_unit: mired}
```

要点：

- 设备上报 `properties.brightness.percent`（0-100），HA 需要 0-255，所以状态侧用 `scale` 换算；控制侧把 HA 的 0-255 直接交给 `value2`（`fast move` 协议本身就是 0-255）；
- 设备上报的色温已经是 Kelvin，状态侧 **不需要** `input_unit`；
- 控制报文里的色温字段是 mired，所以控制侧显式写 `input_unit: kelvin, output_unit: mired`；
- `on` 动作里的 `value2: {from: brightness, default: 255}` 表示"沿用当前亮度"，避免开灯时把亮度打到 0。

### 示例 2：传统 `on`/`off` + `value1` 低电平有效的 0-255 / mired 灯

这段镜像内置 `deviceType=38` 的语义：`order: on/off`，`value1` 为 `0=开 / 1=关`，`value2` 是 `0-255` 亮度，`value3` 是 mired 色温。

```yaml
profile_version: 1
id: legacy_value_light
display_name: 传统调光调色灯（value1/2/3）
platform: light
match:
  device_type: 9002
hardware_verified: true
capabilities: [onoff, brightness, color_temp]
constraints:
  color_temp_range: [2700, 6500]
state:
  state:
    from: value1
    true_values: [0]
    false_values: [1]
  brightness:
    from: value2
    clamp: {min: 0, max: 255}
    default: 0
  color_temp:
    from: value3
    input_unit: mired
    output_unit: kelvin
    clamp: {min: 2700, max: 6500}
control:
  on:
    order: on
    value1: 0
    value2: {from: brightness, default: 255}
    value3: {from: color_temp, input_unit: kelvin, output_unit: mired, default: 370}
  off:
    order: off
    value1: 1
  brightness:
    order: on
    value1: 0
    value2: "param:brightness"
    value3: {from: color_temp, input_unit: kelvin, output_unit: mired, default: 370}
  color_temp:
    order: fast color temperature
    value1: 0
    value2: {from: brightness, default: 255}
    value3: {param: color_temp, input_unit: kelvin, output_unit: mired}
```

要点：

- `true_values: [0]` / `false_values: [1]` 就是 active-low：`value1 = 0` 表示开；
- 状态侧 `input_unit: mired` 会自动把 `value3` 换算成 Kelvin（`output_unit` 默认取相反单位），`1000000/0` 这类非法值会返回 `default`（不更新字段）；
- 拉低电平的开关用 `order: on` 带上 `value1: 0`，关闭必须单独写 `order: off` + `value1: 1`。

## `switch`

**必填**：`platform: switch`、`match`、`control.on`、`control.off`。

**实体行为**（`switch.py::OrviboCustomSwitch`）：`is_on` 读 `state.state`，`available` 读 `online`；打开/关闭分别调用 `on`/`off` 动作。

### 示例 5：仅云端的 WiFi 开关

```yaml
profile_version: 1
id: wifi_switch_cloud
display_name: WiFi 开关（仅云端）
platform: switch
match:
  priority: 10
  any_of:
    - model: "re:^orb_wifi_"
    - product_name: 客厅WiFi开关
hardware_verified: true
cloud_only: true
capabilities: [onoff]
state_defaults:
  state: false
state:
  state:
    from: [properties.onoff.status, properties.onoff_status]
    true_values: ["on", true]
    false_values: ["off", false]
control:
  on:
    order: set property
    properties:
      onoff: {status: "on"}
  off:
    order: set property
    properties:
      onoff: {status: "off"}
```

要点：

- `cloud_only: true` 会把控制通道强制为 `ssl`，并阻止用 LAN 推送更新该设备状态；
- `priority: 10` 让它在多个 profile 同时命中时先被选中（写在 `match` 里）；
- `product_name` 条件在设备没有 `product_name` 字段时会退回设备名匹配，设备改名会让这条分支失效；
- `state_defaults.state: false` 让设备初始化时先显示为"关"，等第一次推送再纠正。

## `cover`

**必填**：`platform: cover`、`match`、`control.position`；建议同时写 `control.stop`。

**实体行为**（`cover.py::OrviboCustomCover`）：

- 声明了 `open`/`position`/`on` 中任意一个 → 暴露"打开/关闭"；
- 声明了 `position` → 暴露"设定位置"；
- 声明了 `stop` → 暴露"停止"；
- 位置读 `state.position`（裁剪到 `0..100`），`position == 0` 视为已关闭；
- HA 的"打开"和"关闭"最终都会走 `position` 动作（参数分别是 `100` 和 `0`），所以**只声明 `open`/`close` 而不声明 `position` 会在点击时抛 `ControlNotDeclared`**；
- `device_class` 固定为 `curtain`。

### 示例 4：位置 + 停止

```yaml
profile_version: 1
id: curtain_position
display_name: 开合窗帘（位置 + 停止）
platform: cover
match:
  device_type: 9004
hardware_verified: true
capabilities: [position, stop]
state:
  position:
    from: value1
    clamp: [0, 100]
    default: 0
control:
  position:
    order: open
    value1: "param:position"
  stop:
    order: stop
```

要点：

- `order: open` + `value1 = 位置(0-100)` 就是内置窗帘协议的写法；`stop` 用 `order: stop`；
- 注意 `order: close` **不是**允许值（`close` 只是动作名），关窗同样用 `order: open` 并把 `value1` 设为 0；
- `open`/`close` 这两个动作名目前不会被实体调用，写了也不会生效，可以把动作映射留给 `position`/`stop`。

## `sensor`

**必填**：`platform: sensor`、`match`、`state`（至少一个数值字段）。

**实体行为**（`sensor.py::OrviboCustomSensor`）：

- 只为 profile 里**声明过**的字段创建实体，且只认这 5 个：`temperature`（°C）、`humidity`（%）、`battery`（%）、`brightness`（无单位）、`position`（%）；
- 声明了 `state`/`angle` 不会产生数值实体（`binary_sensor` 平台会用 `state`，见下）；
- 只读设备的推荐写法是 `status_only: true`，并且不写 `control`。

### 示例 3：只读温湿度 + 电量

```yaml
profile_version: 1
id: th_sensor
display_name: 温湿度传感器（只读）
platform: sensor
match:
  device_type: 9003
status_only: true
capabilities: []
state:
  temperature:
    from: [value2, properties.temperature]
    scale: {min: 0, max: 1000, to_min: 0, to_max: 100}
    round: false
  humidity:
    from: [value3, properties.humidity]
    clamp: [0, 100]
  battery:
    from: [properties.battery.percent, value4]
    clamp: [0, 100]
```

要点：

- `round: false` 保留一位小数（默认会把 25.3 变成 25）；
- `from` 是列表时按顺序取第一个非空路径，所以既能吃 `value2` 型推送，也能吃属性型推送；
- `status_only: true` + 没有 `control` → 控制通道为空，永远不会下发命令。

## `binary_sensor`

**必填**：`platform: binary_sensor`、`match`、`state`（需要 `state` 或 `position` 之一）。

**实体行为**（`binary_sensor.py::OrviboCustomBinarySensor`）：

- 字段优先级：先看有没有声明 `state`，没有才用 `position`；
- `state` 字段按真值判断（`bool(值)`）；
- `position` 字段按 `> 0` 判断（`0` 表示关闭）；
- 实体名分别是"状态"、"位置"，`available` 读 `online`。

### 示例 6：门窗磁

```yaml
profile_version: 1
id: door_contact
display_name: 门窗磁（只读）
platform: binary_sensor
match:
  device_type: 9005
status_only: true
capabilities: []
state:
  state:
    from: [properties.onoff.status, value1]
    true_values: ["open", "on", 0]
    false_values: ["closed", "off", 1]
```

要点：

- 注意 `binary_sensor` 平台会为**任何**声明了 `state`/`position` 的 profile 创建一个二元传感器，即使 `platform` 不是 `binary_sensor`。也就是说一个 `platform: light` 的 profile 如果声明了 `state`，除了灯实体之外还会多出一个"状态"二元传感器；这是预期行为，用于观察原始布尔状态。

## climate 与 fan 的现状

| platform | 是否能通过校验 | 实际结果 |
|---|---|---|
| `climate` | 能 | **不会创建任何实体**。设备仍会被识别、状态仍会被解析，但 `climate.py` 只认内置 `DeviceCategory`，并且会在检测到自定义 profile 时主动跳过（不再落到内置风机盘管实体上），日志打印 `自定义设备 <名称> 声明了 climate 平台，但该平台尚未实现自定义实体，已跳过` |
| `fan` | 能 | **不会创建任何实体**（同样的跳过逻辑；`fan.py` 只认内置 `VENTILATION_SYSTEM` 类别）。设备仍会被识别、状态仍会被解析，另外 `sensor` 平台会为它创建一个"传输路径"诊断传感器 |

这两个平台目前属于"schema 已接受、自定义实体尚未实现（计划中）"。写了 profile 却看不到实体是**已知限制，不是配置错误**；需要这两个平台请参考 [05-custom-vs-upstream.md](05-custom-vs-upstream.md) 提 Issue/PR。

## 相关页面

- 字段与取值：[01-schema.md](01-schema.md)
- 更多范式：[03-recipes.md](03-recipes.md)
- 排查：[04-troubleshooting.md](04-troubleshooting.md)
