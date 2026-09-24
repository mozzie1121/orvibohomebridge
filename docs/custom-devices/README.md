# 自定义设备 Profile

**声明式自定义设备支持：不改代码、不提 PR，只写一个 YAML 文件，就能让本集成支持原本没有内置支持的欧瑞博设备。**

- ✅ 适用：设备能被本集成发现（登录后能在设备列表里看到），只是分类、状态或控制没有内置支持；
- ❌ 不适用：需要新协议命令（新 `cmd`、新传输、WiFi/BLE/红外/摄像头/门锁事件）的设备——profile 不能凭空发明协议，这类必须走代码 PR，见 [05-custom-vs-upstream.md](05-custom-vs-upstream.md)。

原理：profile 只**声明**设备语义（怎么匹配、状态字段在哪、控制发哪条已验证命令），实现部分仍然复用集成里已经真机验证过的传输命令。

## 5 分钟最小可用示例

### 1. 放一个文件

在 Home Assistant 配置目录下新建目录和文件：

```text
<HA config>/orvibohomebridge/devices/my-plug.yaml
```

内容（`device_type` 等参数怎么拿到，见 [06-finding-parameters.md](06-finding-parameters.md)；
也可以直接用 `tools/generate_device_profile.py` 自动生成骨架）：

> ⚠️ 下面 `control:` 里是**猜的占位命令**。工具只能从云端读表和推送里拿到 `match` 与
> `state`，**拿不到 App 实际下发的控制命令**（两条通道都加密）。启用控制前必须按
> [07-reverse-engineering-control.md](07-reverse-engineering-control.md) 用实证探针确认。

```yaml
profile_version: 1
id: my_plug
display_name: 我的插座
platform: switch
match:
  device_type: 9001
hardware_verified: false
capabilities: [onoff]
state:
  state:
    from: [properties.onoff.status, value1]
    true_values: ["on", 0]
    false_values: ["off", 1]
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

### 2. 重新加载

**设置 → 设备与服务 → ORVIBO HomeBridge → 配置 → 自定义设备 Profile → 勾选"重新加载 profile 并重载集成" → 提交。**

该页面同时会显示：扫描的两个目录、已加载的 profile（含命中设备数量）、加载失败的文件与原因。

### 3. 看日志确认

搜索日志关键字：

- `已加载 N 个自定义设备 profile` → 文件被读到了；
- `自定义设备 profile 加载失败：<路径>: ...` → 该文件被跳过（不影响其它 profile 与内置设备）；
- `自定义 profile <id> 命中已被内置识别的设备 ...已跳过` → 该设备内置已支持，要接管必须加 `override: true`。

### 4. 确认设备被识别

在选项页的"选择设备"里找到这台设备，它会显示为 **"（已识别：我的插座，暂未支持）"**，勾选它，然后重载集成。之后：

- 设备会出现，状态按你写的 `state` 映射更新；
- 因为 `hardware_verified: false`，**控制命令不会下发**，点开关时日志会打印
  `未知设备仅注册展示，拒绝下发控制: <device_id>`。

### 5. 真机验证后打开控制

确认开关动作在真机上确实有效后，把 `hardware_verified` 改成 `true`，再重载一次：

```yaml
hardware_verified: true
```

此时设备可控制（默认 LAN 优先、失败回退云端）。

> 需要重载集成或**重启 HA** 才会重新分类设备：设备类别在配置条目启动时就确定了，只改文件不重载看不到变化。

## Profile 放哪里

| 目录 | 用途 | 升级是否会被覆盖 |
|---|---|---|
| `<HA config>/orvibohomebridge/devices/` | **你自己的设备 profile（推荐）** | 不会 |
| `custom_components/orvibohomebridge/custom_devices/` | 随集成发布的模板/样例（如 `example_demo_light.yaml`） | HACS 升级会覆盖 |

扫描顺序是"先内置样例目录、后 HA 配置目录"；两个目录里出现**同一个 `id`** 时，后加载的文件必须写 `override: true` 才能替换先加载的定义，否则会在日志/选项页报
`id 重复：已由 <path> 定义；如需覆盖请在该 profile 设置 override: true`。不同 `id` 的 profile 会同时生效。

支持 `.yaml`、`.yml`、`.json`（UTF-8）。

## 三条硬规则

1. **`profile_version: 1` 必填**，写别的值（或漏写）会直接拒绝加载；
2. **`hardware_verified: true` 之前不下发任何控制**：实体、状态正常，但控制被主动拒绝——这是为了不让"猜出来的"命令打到真实设备上；
3. **命中内置已识别的设备必须写 `override: true`**：否则 profile 被跳过，内置行为保持不变。

## 能表达什么 / 不能表达什么

| 能（配置即可） | 不能（必须提 PR） |
|---|---|
| 开关、亮度、色温、窗帘位置/停止 | 新 `cmd` 号、新 `order`、新传输通道（BLE/红外/WiFi 直连新协议） |
| 单位换算、上下限、枚举映射、布尔判定 | 空调模式/风速、新风档位、晾衣机各功能、多路开关各通道 |
| 8 个标准状态字段（`state`/`brightness`/`color_temp`/`position`/`temperature`/`humidity`/`battery`/`angle`） | 这 8 个之外的任何新状态字段 |
| 只读传感器、二元传感器、窗帘、灯、开关 | 摄像头/视频、门锁事件与临时密码、新实体类型 |
| 云端限定、仅 LAN、优先级、覆盖内置 | `climate`/`fan` 的自定义实体（schema 接受但未实现） |

判断细节和理由见 [05-custom-vs-upstream.md](05-custom-vs-upstream.md)；已知限制汇总见 [03-recipes.md](03-recipes.md#暂时做不到的配方已知限制)。

## 文档索引

| 页面 | 内容 |
|---|---|
| [01-schema.md](01-schema.md) | 字段全表：取值范围、默认值、校验规则、值规格语法、`order` 白名单 |
| [02-platforms.md](02-platforms.md) | 每个平台（light/switch/cover/sensor/binary_sensor）的必填字段与完整示例；`climate`/`fan` 的现状 |
| [03-recipes.md](03-recipes.md) | 常见范式配方（属性型开关、0-255/mired 灯、只读传感器、窗帘、乐观状态……）与做不到的配方 |
| [04-troubleshooting.md](04-troubleshooting.md) | 分步排查、日志关键字对照、诊断信息位置、已知限制 |
| [05-custom-vs-upstream.md](05-custom-vs-upstream.md) | 配置够用 vs 必须提 PR，以及从 profile 到 PR 的路径 |
| [06-finding-parameters.md](06-finding-parameters.md) | **`device_type`、状态路径这些参数从哪来**：骨架生成器用法、手工查法、哪些必须真机确认 |
| [07-reverse-engineering-control.md](07-reverse-engineering-control.md) | **控制命令怎么逆向**：为什么抓包拿不到、实证探针用法、反编译 App 的真实帧怎么验证 |

完整字段模板可以直接看随集成发布的
`custom_components/orvibohomebridge/custom_devices/example_demo_light.yaml`（它是模板，匹配 `device_type: 9901`，**不会命中任何真实设备**）。

## 隐私提醒

- profile 文件放在你的 HA 配置目录里，**不会**被提交到本仓库；
- 但它通常包含真实设备标识（`deviceType`、`model`、`ui_model` 等）。在 Issue/PR 里贴 profile、日志或诊断信息前，请按 [CONTRIBUTING.md](../../CONTRIBUTING.md) 的脱敏要求处理：删除或替换设备 ID、UID、家庭 ID、房间名、MAC、IP 等；诊断 JSON 不要整份粘贴；
- 用明显的占位符（`REDACTED_DEVICE`），不要用另一串看起来真实的十六进制值。

## 相关文档

- 贡献代码 / 新增内置设备支持：[CONTRIBUTING.md](../../CONTRIBUTING.md)
- 集成总览：[README.md](../../README.md)
