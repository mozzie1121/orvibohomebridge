# 常见范式配方

本页是"照着改"的配方集合。每个**完整 profile** 都可以直接复制到 `<HA config>/orvibohomebridge/devices/` 使用（改掉 `match` 里的型号条件），代码块里的片段则需要拼进自己的 profile。

先读 [01-schema.md](01-schema.md) 了解字段，平台细节见 [02-platforms.md](02-platforms.md)。

## 配方 1：先只登记，确认命中后再开控制

最稳妥的第一步：先只让集成"看见"设备并解析状态，不写 `control`（此时自动 `status_only`），确认 `match` 条件准确后再加控制段。

```yaml
profile_version: 1
id: unknown_plug_stage1
display_name: 未知设备（只登记）
platform: switch
match:
  device_type: 9010
hardware_verified: false
capabilities: [onoff]
state:
  state:
    from: [properties.onoff.status, value1]
    true_values: ["on", 0]
    false_values: ["off", 1]
```

- 没有 `control` 段 → `status_only` 自动为真，控制通道为空；
- 在选项页/诊断里确认 `matched_devices` 变成 1，说明 `match` 写对了；
- 之后再补 `control` 并设置 `hardware_verified: true`。

## 配方 2：属性型开关（`set property`）

适用于用 `onoff.status` 属性报文的老设备（内置 `type=501/503` 就是这种）。

```yaml
profile_version: 1
id: sp_switch
display_name: 属性型开关
platform: switch
match:
  device_type: 9013
hardware_verified: true
capabilities: [onoff]
state:
  state:
    from: properties.onoff.status
    true_values: ["on"]
    false_values: ["off"]
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

- `properties` 里 `status` 的布尔会被自动归一化成 `"on"`/`"off"`，但显式加引号最稳妥；
- 需要"开关 + 亮度"属性报文时，亮度可写 `"param:brightness"` 占位符，或用 `value` 型命令，见 [01-schema.md](01-schema.md#properties-与动态数值)。

## 配方 3：0-255 亮度 + mired 色温（active-low）

适用于 `order: on/off` + `value1 = 0/1`、`value2 = 0-255`、`value3 = mired` 的灯（内置 `type=38` 语义）。

```yaml
profile_version: 1
id: value_light_255
display_name: 0-255 调光调色灯
platform: light
match:
  device_type: 9014
hardware_verified: true
capabilities: [onoff, brightness, color_temp]
state:
  state:
    from: value1
    true_values: [0]
    false_values: [1]
  brightness:
    from: value2
    clamp: [0, 255]
  color_temp:
    from: value3
    input_unit: mired
    clamp: {min: 2700, max: 6500}
control:
  on:
    order: on
    value1: 0
    value2: {from: brightness, default: 255}
  off:
    order: off
    value1: 1
  brightness:
    order: on
    value1: 0
    value2: "param:brightness"
  color_temp:
    order: fast color temperature
    value2: {from: brightness, default: 255}
    value3: {param: color_temp, input_unit: kelvin}
```

- `input_unit: mired` 时 `output_unit` 默认就是 `kelvin`，不用重复写；
- `clamp` 用列表或映射都行：`clamp: [0, 255]` 与 `clamp: {min: 0, max: 255}` 等价。

## 配方 4：0-100 百分比亮度 + Kelvin 色温

适用于亮度用百分比、色温直接用 Kelvin 的设备（内置 `type=502/503` 语义）。

```yaml
profile_version: 1
id: percent_light
display_name: 百分比调光灯
platform: light
match:
  device_type: 9015
hardware_verified: true
capabilities: [onoff, brightness]
state:
  state:
    from: properties.onoff.status
    true_values: ["on"]
    false_values: ["off"]
  brightness:
    from: properties.brightness.percent
    scale: {min: 0, max: 100, to_min: 0, to_max: 255}
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
    order: move to level
    value2: "param:brightness"
```

- 状态侧把设备百分比换算成 HA 的 `0..255`；控制侧 `move to level` 的 `value2` 本身就是 `0..255`；
- 只声明了 `brightness`（没有 `color_temp`）→ 灯实体只显示亮度滑块，不会出现色温滑块。

## 配方 5：门窗磁 / 布尔状态（文本枚举用 `map`）

```yaml
profile_version: 1
id: map_text_state
display_name: 文本状态设备
platform: binary_sensor
match:
  device_type: 9011
status_only: true
capabilities: []
state:
  state:
    from: properties.door.status
    map:
      "open": true
      "closed": false
```

- `map` 的**键一定要加引号**：裸写 `on:` 会被 YAML 解析成布尔 `true`，键会变成字符串 `"true"`，永远匹配不到载荷里的 `"on"`；
- `map` 命中后直接返回映射值（布尔、字符串、数字都可以），不再做 `scale`/`clamp`/`round`。

如果状态字段本身就是数字，用 `true_values`/`false_values` 更直观：

```yaml
state:
  state:
    from: value1
    true_values: [0]      # 0 = 触发
    false_values: [1]
```

## 配方 6：只读温湿度 + 电量（保留小数）

```yaml
profile_version: 1
id: th_sensor_recipe
display_name: 温湿度传感器
platform: sensor
match:
  device_type: 9016
status_only: true
capabilities: []
state:
  temperature:
    from: value2
    scale: {min: -400, max: 1250, to_min: -40, to_max: 125}
    round: false
  humidity:
    from: value3
    clamp: [0, 100]
  battery:
    from: value4
    clamp: [0, 100]
```

- 原始温度是"摄氏度 ×10"（-400 ~ 1250）时，用 `scale` 一次换算到 -40 ~ 125；
- `round: false` 保留一位小数，否则会被四舍五入成整数；
- 只有 `temperature`/`humidity`/`battery`/`brightness`/`position` 会产生数值实体。

## 配方 7：窗帘位置 + 停止

```yaml
profile_version: 1
id: curtain_recipe
display_name: 开合窗帘
platform: cover
match:
  device_type: 9017
hardware_verified: true
capabilities: [position, stop]
state:
  position:
    from: value1
    clamp: [0, 100]
control:
  position:
    order: open
    value1: "param:position"
  stop:
    order: stop
```

- 必须声明 `position`：HA 的"打开/关闭"按钮分别以 `position=100`/`position=0` 调用它；
- 想额外暴露一个"状态"二元传感器（开/关），可以再加 `state` 字段（见配方 5 的写法）。

## 配方 8：接管一个内置已识别的设备（`override: true`）

只有在明确要**替换**内置行为时才使用。它会关闭内置保护，让 profile 接管该设备的平台、状态解析和控制。

```yaml
profile_version: 1
id: override_builtin_38
display_name: 接管 type=38 灯
platform: light
override: true
match:
  device_type: 38
hardware_verified: false
capabilities: [onoff, brightness]
state:
  state:
    from: value1
    true_values: [0]
    false_values: [1]
  brightness:
    from: value2
    clamp: [0, 255]
control:
  on:
    order: on
    value1: 0
    value2: {from: brightness, default: 255}
  off:
    order: off
    value1: 1
  brightness:
    order: on
    value1: 0
    value2: "param:brightness"
```

- `hardware_verified: false` 表示"先只观察"，不会下发任何控制；
- 覆盖内置设备属于高风险操作（会影响原本正常的设备），建议先在独立测试环境验证。

## 配方 9：覆盖内置样例的同名 profile

内置样例目录里如果已经有同名 `id`，HA 配置目录里的同名文件**必须**写 `override: true` 才会替换它：

```yaml
profile_version: 1
id: example_demo_light        # 与内置样例同名
override: true                # 缺少这行会报 "id 重复"，内置样例继续生效
display_name: 我的示例灯
platform: light
match:
  device_type: 9902
hardware_verified: false
state:
  state:
    from: value1
    true_values: [0]
    false_values: [1]
```

见 [01-schema.md](01-schema.md#加载失败与错误处理)。

## 配方 10：乐观状态（回包慢/无回包）

命令下发后设备回包很慢时，先本地更新状态，等真实推送再纠正。

```yaml
profile_version: 1
id: optimistic_switch
display_name: 带乐观状态的开关
platform: switch
match:
  device_type: 9012
hardware_verified: true
capabilities: [onoff]
state:
  state:
    from: value1
    true_values: [0]
    false_values: [1]
control:
  on:
    order: on
    value1: 0
  off:
    order: off
    value1: 1
optimistic:
  on:
    state: true
  off:
    state: false
state_defaults:
  state: false
```

- 你声明的值一定生效（包括 `false`），只在没声明该字段时才用隐式默认（`on → state: true`、`off → state: false`）；
- 想沿用本次请求的数值，可以写 `brightness: "param:brightness"`。

## 配方 11：一个 profile 命中多个型号（`any_of` + `priority`）

```yaml
match:
  priority: 10
  any_of:
    - device_type: 9018
    - device_type: [9019, 9020]
      sub_device_type: 12
    - model: "re:^orb_myplug_"
```

- 分支之间是 OR，分支内部是 AND；
- `priority` 越大越先匹配，适合"通用规则 + 型号特例"的组合：通用规则写 `priority: 0`，特例写 `priority: 10`；
- 同优先级时按 `id` 字典序决定先后。

## 暂时做不到的配方（已知限制）

| 想做的事 | 现状 | 替代方案 |
|---|---|---|
| 属性型窗帘（`curtain.action` 开/关/暂停，且要能设定位置） | 位置可以动态下发（`value1: "param:position"` 或 `properties` 里的 `"param:position"` 占位符），但实体的开/关按钮都走 `position` 动作，属性型协议需要自己用 `action` 表达开/关 | 用 `order: open` + `value1: "param:position"` 的值型协议；属性型可写 `properties: {curtain: {action: "open"}}` 作为 `open`/`close` 动作 |
| `platform: climate` / `fan` 的自定义实体 | schema 接受、设备能识别、状态能解析，但这两个平台没有自定义实体类，**不会创建任何实体** | 能用 `switch`/`sensor`/`binary_sensor` 表达的部分先用这些平台，其余见 [02-platforms.md](02-platforms.md#climate-与-fan-的现状) |
| 新 `cmd`、新传输、WiFi/BLE/红外/摄像头、门锁事件 | profile 无法表达 | 必须提 PR，见 [05-custom-vs-upstream.md](05-custom-vs-upstream.md) |
| 场景/多路开关（一个设备多个子实体）、空调模式/风速 | 没有对应动作与实体 | 提 PR，见 [05-custom-vs-upstream.md](05-custom-vs-upstream.md) |

## 相关页面

- 字段与默认值：[01-schema.md](01-schema.md)
- 平台必填字段：[02-platforms.md](02-platforms.md)
- 排查：[04-troubleshooting.md](04-troubleshooting.md)
