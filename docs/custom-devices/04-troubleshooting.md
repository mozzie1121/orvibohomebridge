# 分步排查

按顺序走一遍，99% 的"profile 不生效"都能定位。每一步都给出**看哪里**和**日志关键字**。

## 第 0 步：确认改文件之后重载过

profile 是在集成启动时读取的：

1. 设置 → 设备与服务 → ORVIBO HomeBridge → **配置** → **自定义设备 Profile**；
2. 该页面会列出两个目录、已加载的 profile、每个 profile 命中的设备数量、以及加载失败的文件；
3. 勾选 **"重新加载 profile 并重载集成"** → 提交（等价于重载集成）；
4. **设备分类/实体创建的变化需要重载集成或重启 Home Assistant 才会生效**：设备类别在配置条目启动时就固定了，只改文件不重载不会看到任何变化。

## 第 1 步：文件是否被读到

| 检查项 | 期望 |
|---|---|
| 目录 | `<HA config>/orvibohomebridge/devices/`（推荐，HACS 升级不会丢）或 `custom_components/orvibohomebridge/custom_devices/` |
| 扩展名 | `.yaml`、`.yml`、`.json` 之一 |
| 编码 | UTF-8；文件名不要用奇怪字符 |
| 内容 | 顶层是映射（不是列表） |

日志关键字：

| 日志 | 含义 | 处理 |
|---|---|---|
| `已加载 N 个自定义设备 profile（目录：...）` | 成功加载了 N 个 | 数量不对就看下面"加载失败" |
| `自定义设备 profile 加载失败：<path>: ...` | 该文件被跳过，其它 profile 和内置设备不受影响 | 按报错信息修 |

## 第 2 步：校验报错逐条对照

| 报错片段 | 原因 | 处理 |
|---|---|---|
| `profile_version 必须为 1（当前 None）` | 忘了写 `profile_version` | 加 `profile_version: 1` |
| `profile_version 必须为 1（当前 2）` | 版本号不是 1 | 只能是 1 |
| `id 只能包含小写字母、数字、下划线和连字符` | `id` 有大写字母或空格 | 改成 `my_device_01` 这种 |
| `platform 必须是 ... 之一` | 平台名拼错 | 见 [02-platforms.md](02-platforms.md) |
| `state.xxx 不是受支持的字段，可用字段：...` | `state` 下写了未知字段 | 只能用 8 个字段 |
| `control.'xxx' 不是受支持的动作，可用动作：...` | 动作名写错 | 只能用 8 个动作 |
| `order 必须是已验证命令之一：...` | `order` 不在允许列表（例如写了 `order: close`、`order: set_property`） | 见 [01-schema.md](01-schema.md#control控制动作) |
| `properties 只能与 order='set property' 搭配` | 把 `properties` 写在了 `on`/`off` 这类命令上 | 改用 `order: set property`，或删掉 `properties` |
| `未知匹配键 'xxx'` | `match` 里写了不存在的键 | 只能用 10 个匹配键 |
| `match 至少需要一个条件` | `match: {}` | 至少写一个条件 |
| `channels 含未知通道 xxx（仅支持 lan/ssl）` | 通道名写错 | 只能 `lan`/`ssl` |
| `scale 必须是映射` / `clamp 必须是 {min, max} 或 [min, max]` | 值规格写法错 | 按 [01-schema.md](01-schema.md#state状态字段与值规格) 改 |
| `无效正则` | `re:` 后面的正则语法错 | 修正则，注意转义 |
| `id 重复：已由 <path> 定义；如需覆盖请在该 profile 设置 override: true` | 两个文件用了同一个 `id` | 改 `id`，或在你自己的文件里加 `override: true` |

## 第 3 步：profile 有没有命中设备

在"自定义设备 Profile"页面看 **命中设备 N 台**，或下载诊断信息看 `custom_device_profiles.loaded[].matched_devices`。

- **命中 0 台**：`match` 条件不对。对照诊断信息里该设备的 `device_type_raw` / `sub_device_type` / `class_id` / `model` / `ui_model` 修正；
  - 常见错误：把 `priority` 写在了顶层（会被静默忽略，必须在 `match.priority`）；
  - `status_type` 条件在当前归一化字典里通常取不到值，条件会恒不成立；
  - `product_name` 在没有该字段时会退回设备名，设备改名会失效。
- **命中但没效果**：继续下一步。

同时确认该设备在选项页的"选择设备"里**已被勾选**——没勾选就不会创建任何实体（未验证的 profile 在列表里显示为"（已识别：<显示名>，暂未支持）"）。

## 第 4 步：是不是被 `override` 保护挡掉了

日志会出现：

```text
自定义 profile <id> 命中已被内置识别的设备 <device_id>，未设置 override: true，已跳过（内置行为保持不变）
```

说明该设备内置分类已经识别，profile 被有意跳过。如果你确实要接管它，在 profile 顶层加 `override: true`；如果只是想支持一个新型号，请换一个内置**不**认识的 `device_type` 条件。

## 第 5 步：状态在动，但控制被拒绝

| 日志 | 含义 | 处理 |
|---|---|---|
| `hardware_verified 未设置：实体只读，控制命令不会下发（真机验证后请设为 true）` | profile 有 `control` 但没声明真机验证 | 真机验证通过后写 `hardware_verified: true` |
| `未知设备仅注册展示，拒绝下发控制: <device_id>` | 只登记设备（未验证或 `status_only`）收到控制请求，被主动拒绝 | 同上；只读设备请改成只展示 |
| `自定义设备 profile <id> 未声明 <action> 控制动作，已拒绝下发` | 调用了 profile 没声明的动作（HA 会把这个异常抛到日志里） | 补上对应动作，或去掉触发它的能力 |
| `未声明 state 映射：实体状态只能来自云端快照` | 没有 `state` 段 | 补 `state`，否则状态只能靠云端轮询 |

最容易踩的"未声明动作"组合：

- 灯声明了 `brightness` 能力但没写 `brightness` 动作 → 拖动亮度滑块时报错；
- 窗帘只写了 `open`/`close` 而没写 `position` → 点"打开/关闭"时报错（实体这两个按钮都走 `position`）；
- 开关少了 `off` → 只有打开能用。

## 第 6 步：状态不对（数值/单位/布尔）

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 亮度一直是 0 或满值 | 设备是 `0-100` 百分比，HA 需要 `0-255` | 加 `scale: {min: 0, max: 100, to_min: 0, to_max: 255}` |
| 色温显示成 2700 或 6500 恒定 | 单位搞反：设备发 mired 却按 Kelvin 读（或反之） | 用 `input_unit: mired`（自动转 Kelvin）；发送侧用 `input_unit: kelvin, output_unit: mired` |
| 温度少了小数 | `round` 默认 `true` | 写 `round: false` |
| 布尔字段一直不更新 | 载荷里是字符串 `"on"`，而条件写的是裸 `on`（YAML 里变成布尔） | 用 `true_values: ["on"]`（引号）或直接写 `true_values: [on]`（实现会扩展成 `"on"/"true"/"yes"/"open"/"1"`） |
| 用 `map` 匹配不到 | `map` 的键被 YAML 解析成了布尔 | 给 `map` 的键加引号：`"open": true` |
| 部分推送把别的字段清空了 | 值取不到时用了 `default: 0` | 去掉 `default`（`None` 表示"本次不更新该字段"），或在 `from` 里用列表兜底 |
| 实体一直是"不可用" | 状态里没有 `online` | 在 `state_defaults` 里写 `online: true`，或确认设备确实在线 |

## 第 7 步：实体没出现 / 实体类型不对

### 症状：写了 `platform: climate` 或 `platform: fan`，看不到实体

这是**已知限制**，不是故障，也不是配置错误：

- `climate`、`fan` 都会被 schema 接受，设备能被识别、状态能被解析，但这两个平台目前**没有自定义实体类**；
- 平台文件检测到自定义 profile 后会主动跳过（不会落到内置的空调/新风实体上，避免用内置协议下发未经 profile 声明的命令），日志会打印：

  ```text
  自定义设备 <设备名> 声明了 climate 平台，但该平台尚未实现自定义实体，已跳过
  ```

- 替代方案：能用 `switch`/`sensor`/`binary_sensor` 表达的部分先用这些平台；需要空调模式/风速等能力请按 [05-custom-vs-upstream.md](05-custom-vs-upstream.md) 提 Issue。

### 症状：什么都对，就是实体没出现

- 设备没在选项页被勾选；
- 改完文件没有重载集成/重启 HA（设备分类在启动时固定）；
- `sensor` 平台只认 `temperature`/`humidity`/`battery`/`brightness`/`position`，声明 `state`/`angle` 不会产生数值实体；
- 平台是 `climate`/`fan`（见上一条）；
- profile 命中失败（回到第 3 步）。

## 诊断信息在哪里

**设置 → 设备与服务 → ORVIBO HomeBridge → 集成条目右侧三个点 → 下载诊断信息**（HA 设备页面 → 集成 → 下载诊断信息）。JSON 里与自定义设备相关的部分：

| 路径 | 内容 |
|---|---|
| `custom_device_profiles.directories` | 本次实际扫描的目录（顺序即优先级顺序） |
| `custom_device_profiles.loaded[]` | 每个已加载 profile 的 `id`、`display_name`、`platform`、`priority`、`override`、`hardware_verified`、`status_only`、`cloud_only`、`channels`、`capabilities`、`state_fields`、`control_actions`、`matched_devices`、`warnings`、`source` |
| `custom_device_profiles.errors[]` | 加载失败的文件与原因 |
| `devices.<device_id>.custom_profile` | 该设备实际命中的 profile id（为 `null` 说明没命中） |
| `devices.<device_id>.category` | 为 `custom:<id>` 说明分类已接管；为内置类别名说明 profile 没生效 |

## 提 Issue 前的脱敏

profile 文件放在 HA 配置目录里，**不会被提交到仓库**，但它常常包含真实设备标识。提交 Issue/PR 时请遵守 [CONTRIBUTING.md](../../CONTRIBUTING.md) 的脱敏要求：

- 删除或替换设备 ID、UID、status ID、extAddr、MAC、家庭 ID、房间名；
- 不要粘贴诊断 JSON 全文（里面含 `uid`、设备名等）；
- 需要贴日志时，只贴报错行，并检查是否夹带设备标识；
- 用明显的占位符（如 `REDACTED_DEVICE`），不要用另一串看起来真实的十六进制值。

## 相关页面

- 字段全表：[01-schema.md](01-schema.md)
- 平台与示例：[02-platforms.md](02-platforms.md)
- 配方：[03-recipes.md](03-recipes.md)
- 什么时候必须提 PR：[05-custom-vs-upstream.md](05-custom-vs-upstream.md)
