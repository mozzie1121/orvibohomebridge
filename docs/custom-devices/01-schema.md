# Profile 字段全表

本页是 `profile_version: 1` 的**字段契约**：每个字段的取值、默认值、校验规则和当前实现里的实际行为。所有内容都对照实现代码逐条核对，示例可直接复制。

- 只想知道怎么快速跑起来：看 [README.md](README.md)
- 想看各平台的必填字段和完整示例：看 [02-platforms.md](02-platforms.md)
- 想看常见范式：看 [03-recipes.md](03-recipes.md)

## 文件与目录

| 项 | 值 |
|---|---|
| 允许的扩展名 | `.yaml`、`.yml`、`.json` |
| 编码 | UTF-8 |
| 一个文件一个 profile | 是（顶层必须是映射） |
| 目录 1（内置样例） | `custom_components/orvibohomebridge/custom_devices/` |
| 目录 2（用户配置） | `<HA config>/orvibohomebridge/devices/` |
| 目录扫描顺序 | 先内置样例目录，再 HA 配置目录（同一 `id` 时，后加载的必须写 `override: true` 才生效，见下） |

内置样例目录里有一个模板文件 `example_demo_light.yaml`，它匹配一个几乎不会出现的 `device_type: 9901`，**不会命中任何真实设备**，只作为"全字段模板"参考，请复制到自己的目录后修改。

## 顶层字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `profile_version` | int | **是** | — | 只能为 `1`。缺失或其它值都会拒绝加载（`profile_version 必须为 1`）。旧写法 `version: 1` 也能被识别，但请使用 `profile_version` |
| `id` | str | **是** | — | 只能匹配 `[a-z0-9][a-z0-9_\-]*`（小写字母、数字、下划线、连字符，且首字符不能是 `_`/`-`） |
| `display_name` | str | 否 | 等于 `id` | 诊断信息和选项页里显示的名字，支持中文 |
| `platform` | str | **是** | — | `light` / `switch` / `cover` / `sensor` / `binary_sensor` / `climate` / `fan` 之一；其中 `climate`、`fan` 目前不会创建自定义实体，见 [02-platforms.md](02-platforms.md) |
| `match` | map | **是** | — | 至少要有一个条件，见下 |
| `override` | bool | 否 | `false` | 命中"内置已识别设备"时必须显式设为 `true`，否则该 profile 被跳过 |
| `capabilities` | list[str] | 否 | `[]` | 目前只有 `light` 会读取（`onoff`/`brightness`/`color_temp` 决定 HA 颜色模式），其余平台仅作为诊断展示 |
| `hardware_verified` | bool | 否 | `false` | `false` 时实体只登记、只解析状态，**任何控制命令都不会下发** |
| `status_only` | bool | 否 | `false` | 只读设备；另外**未声明 `control` 时会被自动置为 `true`**（`status_only = 声明值 or 没有控制动作`） |
| `cloud_only` | bool | 否 | `false` | 强制只走云端（SSL 通道），并把 `channels` 强制为 `[ssl]` |
| `channels` | list[str] 或 str | 否 | `[lan, ssl]`（`cloud_only: true` 时为 `[ssl]`） | 只能包含 `lan`、`ssl`；写字符串（`channels: ssl`）也接受 |
| `state` | map | 否 | `{}` | 状态字段映射，键只能是 8 个已知字段，见下 |
| `control` | map | 否 | `{}` | 控制动作映射，键只能是 8 个动作，见下 |
| `constraints` | map | 否 | `{}` | 只接受 `brightness_range`、`color_temp_range`，值必须是 `[min, max]` 且 `min < max` |
| `optimistic` | map | 否 | `{}` | 动作 → 状态字段 → 值规格；用于回包缺失时的本地状态 |
| `state_defaults` | map | 否 | `{}` | 初始状态兜底，键只能是 8 个状态字段或 `online`；在设备初始化时合并进状态 |
| `hidden` | bool | 否 | `false` | 会被解析并保存，但当前实现不使用它来隐藏设备 |
| `author` / `notes` | str | 否 | `""` | 仅供人阅读 |

**未识别的顶层键会被静默忽略**：例如把 `priority: 10` 写在顶层不会报错，但也不会生效——优先级必须写成 `match.priority`。写错层级时不会得到任何提示，请以本表为准。

## `match`：命中规则

```yaml
match:
  priority: 10          # 可选；数字越大越先被匹配
  any_of:               # 可选；分支之间是 OR
    - device_type: 9001
      sub_device_type: 12   # 同一分支内的条件是 AND
    - model: "re:^orb_wifi_"
  # 扁平简写也支持：直接写条件键 = 一个 AND 分支
  # device_type: 9001
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `match.priority` | int | `0` | 越大越先匹配；同优先级按 `id` 字典序排序。非整数会报错 |
| `match.any_of` | list[map] | — | 每个元素是一个非空映射；分支之间 OR，分支内部 AND |
| 扁平简写 | map | — | `match` 里除 `priority`/`any_of`/`override` 之外的键会被当成**一个额外的 AND 分支** |
| `match.override` | — | — | **无效**：`override` 是顶层键，写在 `match` 里不会生效 |

可用的匹配键（写在 `match` 本身或 `any_of` 的每个分支里）：

| 键 | 取值 | 说明 |
|---|---|---|
| `device_type` | int / list[int] | 读归一化后的 `device_type_raw`；原始记录的 `deviceType` 也接受 |
| `sub_device_type` | int / list[int] | 读 `sub_device_type`（原始 `subDeviceType`） |
| `class_id` | int / list[int] | 读 `class_id`（原始 `classId`） |
| `status_type` | int / list[int] | 读 `status_type`（原始 `statusType`）。注意：当前归一化设备字典里通常没有这个字段，条件会恒不成立 |
| `ui_model` | str / list[str] | 精确匹配；`re:` 前缀表示正则 |
| `model` | str / list[str] | 同上 |
| `class_name` | str / list[str] | 同上（来自云端 `className`） |
| `product_name` | str / list[str] | 优先读 `product_name`，缺失时回退到设备名 `device_name`，因此设备改名会让该条件失效 |
| `property_present` | str / list[str] | 点号路径；先按顶层路径查找，找不到再进 `properties` 里找。所有给出的路径都必须存在 |
| `property_equals` | map[路径, 值] | 字符串比较；若取到的值是 `{"status": ...}` 或 `{"value": ...}` 形态，会先取出其中的值再比较 |

文本类键（`ui_model`、`model`、`class_name`、`product_name`）的匹配规则：

- 值是字符串时精确相等（区分大小写）；
- 以 `re:` 开头时用 `re.search` 做正则匹配，例如 `model: "re:^orb_wifi_"`；
- 正则语法错误会在**加载时**就报 `无效正则`，而不是等到运行时。

## `override`：内置识别优先

本集成已经能识别的设备（例如 `deviceType=38` 的调光调色灯、`501/426` 的单色灯）即使被 profile 命中，也**不会**交给 profile：

- 未设置 `override: true` 时，profile 被跳过，日志打印
  `自定义 profile <id> 命中已被内置识别的设备 <device_id>，未设置 override: true，已跳过（内置行为保持不变）`；
- 设置 `override: true` 后，profile 接管该设备的平台、状态解析和控制；
- 命中内置不认识的设备（本功能的主要场景）**不需要** `override`。

设计原因：内置类别是维护者用真机验证过的行为，一个只凭字段猜测的 profile 不应该悄悄改掉已经正常工作的设备。`override` 保护在所有解析路径上都生效（分类、能力解析、实体选择，以及 `platform_for`/`capabilities_for` 等辅助函数都会先做内置识别判断），只有测试里显式传 `builtin_recognised=False` 才会绕过。

发现阶段会覆盖设备的 `device_type`（改成 profile 的 `platform`），但内置识别结论会单独记录下来供实体创建阶段复用，所以"profile 命中一个内置不认识的新设备"这一常见场景不需要 `override`。

## `hardware_verified` / `status_only` / `cloud_only` / `channels`

这四个字段共同决定设备**能不能被控制**、以及**走哪条通道**。

| 组合 | 有效 `status_only` | 控制通道 `channels` | 结果 |
|---|---|---|---|
| 有 `control` + `hardware_verified: true`，未限制通道 | `false` | `lan`、`ssl` | 可控制，LAN 优先、失败回退云端 |
| 有 `control` + `hardware_verified: false`（或不写） | `false` | 空 | **只登记**：实体和状态正常，但不下发任何控制 |
| `status_only: true` + `hardware_verified: true` | `true` | 空 | 只读，即使已验证也不下发 |
| 没有 `control` 段 | `true`（自动） | 空 | 只读 |
| `channels: [lan]` | 按上面规则 | `lan` | 只允许 LAN（云端模式下会显示为不可用） |
| `cloud_only: true` | 按上面规则 | `ssl` | 强制云端；LAN 推送也不会用来更新该设备的状态 |
| `cloud_only: true` + `status_only: true` | `true` | 空 | 只读 + 只走云端状态 |

补充说明：

- 只登记的设备在控制被触发时，日志会打印
  `未知设备仅注册展示，拒绝下发<控制名>: <device_id>`，并不会发送命令；
- 未验证但声明了 `control` 的 profile 在加载时会有一条警告日志
  `hardware_verified 未设置：实体只读，控制命令不会下发（真机验证后请设为 true）`；
- 完全没有 `state` 段时会警告 `未声明 state 映射：实体状态只能来自云端快照`；
- `cloud_only` 的含义是"该设备只能通过云端控制/取状态"（例如 WiFi 直连设备），此时即使 `channels` 写了 `lan` 也会被强制改回 `ssl`。

## `state`：状态字段与值规格

`state` 的键只能是下面 8 个字段，其它名字会在加载时报错并列出可用字段。

| 字段 | HA 侧单位/范围 | 谁在用 |
|---|---|---|
| `state` | bool | `light`/`switch` 的开关态；`binary_sensor` 的默认字段 |
| `brightness` | `0..255`（HA 亮度） | `light` 的亮度；`sensor` 的"亮度"数值实体 |
| `color_temp` | **Kelvin** | `light` 的色温（HA 内部按 Kelvin 展示） |
| `position` | `0..100` | `cover` 的位置；`binary_sensor` 的第二候选字段；`sensor` 的"位置"数值实体 |
| `temperature` | °C | `sensor` 的"温度" |
| `humidity` | % | `sensor` 的"湿度" |
| `battery` | % | `sensor` 的"电量" |
| `angle` | 数值 | 会写入状态字典，但当前没有自定义实体读取它（梦幻帘角度是内置类别的能力） |

值规格可以写成**标量简写**或**映射**：

```yaml
state:
  # 1) 标量 = 常量
  state: true
  # 2) 字符串 "param:xxx" 只在 optimistic 里有意义（state 解析没有调用参数）
  brightness: "param:brightness"
  # 3) 映射 = 完整规则
  color_temp:
    from: [value3, properties.colorTemp.value]
    input_unit: mired
    clamp: {min: 2700, max: 6500}
    default: 2700
```

| 键 | 类型 | 默认值 | 行为 |
|---|---|---|---|
| `from` | str 或 list[str] | 无 | 点号路径。先按顶层路径取，取不到再进 `properties` 里取；列表表示依次尝试，取到第一个非空值。路径段可以是列表下标，如 `deviceStatus.0.value1` |
| `value` | 任意 | 无 | 常量。**显式写 `value: false` / `value: 0` 是真实值**；只有不写 `value` 或写 `value: null` 才表示"无值" |
| `param` | str | 无 | 从调用参数取值，只有控制动作/`optimistic` 会传入参数；合法名字见 `control` 小节 |
| `default` | 任意 | `None` | 上面三项都取不到时使用；`None` 表示"本次不更新该字段" |
| `map` | map | 无 | 值映射表。命中后**直接返回映射结果**（数字、布尔、字符串都可以），不再做 scale/clamp/round；未命中返回 `default`。键会被归一化成小写字符串，所以**键建议加引号**：裸写 `on:` 会变成 `"true"` |
| `scale` | map | 见右 | `{min, max, to_min, to_max}`，缺失时 `min=0`、`max=255`、`to_min=min`、`to_max=max`；公式 `目标 = to_min + (值-min)/(max-min) * (to_max-to_min)`，`max == min` 时返回 `default` |
| `clamp` | map 或 `[min, max]` | 无 | 上下限裁剪；`{min: 10}` 表示只设下限（上限为 +∞） |
| `round` | bool | `true` | `true` 时四舍五入为整数；需要小数（如 25.3°C）时写 `round: false` |
| `true_values` | list | `[]` | 值归一化后命中列表 → `true`；未命中任何列表 → `default` |
| `false_values` | list | `[]` | 命中 → `false` |
| `as_bool` | bool | `false` | 用通用布尔解析（`1/true/on/yes/open/detected` → true，`0/false/off/no/close/closed/idle` → false） |
| `input_unit` / `unit` | `mired` / `kelvin` | 无 | 源数据单位；只写 `input_unit` 时 `output_unit` 自动取相反单位（mired↔kelvin 用 `1000000 / 值` 换算，值 ≤ 0 时返回 `default`） |
| `output_unit` | `mired` / `kelvin` | 见左 | 目标单位 |

解析顺序（决定了组合效果）：取 `from` → `param` → `value` → 布尔处理（`as_bool`/`true_values`/`false_values`）→ 原始值本身是布尔就直接返回 → `map` → 单位换算 → `scale` → `clamp` → `round`。

几个容易踩的点：

- `from` 里写的路径和**推送载荷**的键一致（例如 `value1`、`properties.onoff.status`），不是 HA 状态名；
- 取到的值是 `{"percent": 60}`、`{"status": "on"}`、`{"value": 42}`、`{"level": x}`、`{"brightness": x}` 这类形态时，会自动拆出里面的值；
- 布尔字段建议用 `true_values`/`false_values`（列表会自动扩展 YAML 别名，见下），或用 `map`；
- `true_values: [on]` 在 YAML 1.1 里是布尔 `true`，实现会把布尔扩展成 `("true","on","yes","open","1")`，所以它同时能匹配载荷里的字符串 `"on"` 和 `"true"`。

## `control`：控制动作

`control` 的键只能是以下 8 个动作名（大小写、空格会被归一化，`turn_on`/`power_on`/`yes`/`true` 等别名也会归一到 `on`/`off`）：

| 动作 | 谁调用它 | 典型 `order` |
|---|---|---|
| `on` | 开关/灯的"打开"（不带亮度色温时） | `on`、`set property` |
| `off` | 开关/灯的"关闭" | `off`、`set property` |
| `brightness` | 灯的亮度变化（HA 亮度 `0..255`） | `move to level`、`fast move to level`、`on`、`set property` |
| `color_temp` | 灯的色温变化（参数是 **Kelvin**） | `fast color temperature`、`set property` |
| `position` | 窗帘"打开/关闭/设定位置"（参数是 `0..100`） | `open` |
| `stop` | 窗帘"停止" | `stop` |
| `open` | 预留动作名，目前自定义窗帘实体不会调用它 | `open` |
| `close` | 预留动作名，目前自定义窗帘实体不会调用它 | `off`、`open` |

每个动作的写法：

```yaml
control:
  on:                      # 也可以用字符串简写： on: set property
    order: on              # 必须是已验证命令，见下表
    value1: 0
    value2: {from: brightness, default: 255}
  brightness:
    order: move to level
    value1: 0
    value2: "param:brightness"
  off:
    order: set property
    properties:            # 只有 order: set property 允许 properties
      onoff: {status: "off"}
```

`order` 允许值（**没有其它命令**，写别的会在加载时报错）：

| `order` | 语义（对照内置实现） | 常见字段 |
|---|---|---|
| `on` | 开；也可用于"带亮度/色温的开关报文" | `value1`（0/1 常作 active-low 开关位）、`value2`（亮度）、`value3`（色温，按设备约定 mired 或 Kelvin） |
| `off` | 关 | 同上 |
| `open` | 窗帘移动到某位置 | `value1` = `0..100` |
| `stop` | 窗帘停止 | `value1`/`value2` 一般为 0 |
| `set property` | 属性型报文，走 `properties` | `properties`（必需）、`value1..4` 仍会一起下发 |
| `move to level` | 调光（0-10V 调光灯协议） | `value2` = 亮度 `0..255` |
| `fast move to level` | Fast Move 调光 | `value2` = 亮度 `0..255`，`value3` = 色温 mired |
| `fast color temperature` | 色温（type=38 / Fast Move 协议） | `value2` = 亮度 `0..255`，`value3` = 色温 mired |

动作映射内的其它键：

| 键 | 类型 | 说明 |
|---|---|---|
| `order` | str | **必填**；未加引号的 `on`/`off` 会被 YAML 解析成布尔，实现会自动还原为 `"on"`/`"off"` |
| `value1` ~ `value4` | 值规格 | 只允许这四个键名，其它键会报错；最终都被转成整数（取不到时按 `0` 下发） |
| `properties` | map | **只能与 `order: set property` 搭配**，否则报错。静态字面量会原样进入 `cmd=15` 报文；字符串 `"param:<名字>"` 会替换成本次调用的参数值 |

### `properties` 与动态数值

`properties` 本身是静态映射，但其中任何字符串 `"param:<名字>"` 会被替换为**本次调用**的参数值，因此属性型命令也可以下发动态数值：

```yaml
control:
  # 静态开关属性（推荐给开关类）
  on:
    order: set property
    properties:
      onoff: {status: "on"}

  # 动态数值：percent 会被替换成本次请求的亮度（整数）
  brightness:
    order: set property
    properties: {brightness: {percent: "param:brightness"}}
```

替换规则：

- 只有 `"param:<名字>"` 这一种形式会替换，`<名字>` 必须是 `brightness` / `color_temp` / `position` / `value` / `on` / `off`；
- 替换值是调用方传入的原始数值（不是字符串），所以 `percent` 会是整数；
- 参数取不到时（例如窗帘命令没有亮度）保留字面量，不会报错——这种情况请改用默认值或值型命令；
- `"from:<路径>"` 在 `properties` 里**不会**替换，只有值规格（`value1..4`）才支持 `from`。

如果设备有值型命令（`move to level`、`fast move to level`、`fast color temperature`），仍然优先用它们，因为那是真机验证过的报文形态：

```yaml
control:
  brightness:
    order: fast move to level
    value2: "param:brightness"
```

### YAML 布尔归一化与 `param`

- `properties` 里，键名为 `status`、`state`、`onoff`、`onoff_status` 时，布尔值会被自动写回字符串：`properties: {onoff: {status: on}}` 下发的是 `"on"`，不需要手写引号；
- 其它位置的布尔保持布尔；
- `map` 的**键**不做这种归一化，裸写 `on:` 会变成 `"true"`，所以 map 键请加引号。

`param` 可用的名字：`brightness`、`color_temp`、`position`、`value`、`on`、`off`。

| 参数 | 由谁传入 | 单位 |
|---|---|---|
| `brightness` | 灯光亮度（`0..255`） | HA 亮度 |
| `color_temp` | 灯光色温 | **Kelvin** |
| `position` | 窗帘位置 | `0..100` |
| `value` | 预留，当前调用方不传 | — |
| `on` / `off` | 每个动作都会带上（`on` 动作为 true，`off` 动作为 true） | 布尔，`value1..4` 只接受整数，所以实际用处有限 |

`from` 与 `param` 的区别：`from` 读的是**设备当前状态/推送载荷**，`param` 读的是**本次调用参数**。所以"打开时沿用当前亮度"要用 `from: brightness`，"把用户刚设定的亮度发下去"要用 `param: brightness`，例如：

```yaml
control:
  on:
    order: on
    value1: 0
    value2: {from: brightness, default: 255}      # 当前状态里的亮度
  brightness:
    order: on
    value1: 0
    value2: "param:brightness"                     # 本次请求的亮度
```

## `optimistic`：回包缺失时的本地状态

回包没有携带状态时，集成会用 `optimistic` 里的声明更新本地状态；没声明就使用隐式默认。**显式声明的值一定生效**（包括 `false`），隐式默认只在没有声明该字段时补位。

| 动作 | 隐式默认 |
|---|---|
| `on` / `open` | `state: true` |
| `off` / `close` | `state: false` |
| `brightness` / `color_temp` | `state: true` |
| `stop` | `cover_action: "stop"` |

```yaml
optimistic:
  on:
    state: true
    brightness: "param:brightness"   # 也可以用 {param: brightness}
  off:
    state: false                     # 显式 false 优先于隐式 true
  brightness:
    brightness: "param:brightness"
```

`optimistic` 的字段同样只能取 8 个状态字段，值规格与 `state` 一致（额外允许 `param`）。

## `constraints` 与 `state_defaults`

```yaml
constraints:
  color_temp_range: [2700, 6500]   # 灯实体的色温上下限（Kelvin），默认就是 2700-6500
  brightness_range: [0, 255]       # 会被校验并保存，但当前没有实体读取它

state_defaults:
  online: true                     # 设备初始化时合并进状态
  state: false
```

- `constraints` 的值必须是两个整数的列表且 `min < max`；
- `color_temp_range` 只影响 `light` 实体的色温滑块范围；
- `state_defaults` 的键只能是 8 个状态字段或 `online`，在设备初始化阶段合并（可用于让只推状态的设备一开始就显示为在线）。

## 加载失败与错误处理

- 单个文件加载失败**不会**影响启动：错误会写日志、出现在选项页和诊断信息里，其它 profile 和所有内置设备继续正常工作；
- 重复 `id`：后加载的文件若未写 `override: true`，会被跳过并报
  `id 重复：已由 <path> 定义；如需覆盖请在该 profile 设置 override: true`；写了 `override: true` 则替换先前定义并打印一条覆盖警告；
- 因此"用 HA 配置目录里的文件覆盖内置样例的同名 profile"需要写 `override: true`；不同 `id` 时两个目录里的 profile 会同时生效。

## 相关页面

- [02-platforms.md](02-platforms.md)：每个平台必填字段与完整示例
- [03-recipes.md](03-recipes.md)：常见范式配方
- [04-troubleshooting.md](04-troubleshooting.md)：分步排查
- [05-custom-vs-upstream.md](05-custom-vs-upstream.md)：什么时候必须提 PR
