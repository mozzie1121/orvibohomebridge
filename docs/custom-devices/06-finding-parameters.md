# 怎么拿到写 profile 需要的参数

写 profile 要填三类东西，来源完全不同。这篇讲清每一类**从哪来、怎么拿、哪些必须真机确认**。

| 要填的 | 从哪来 | 能不能自动 |
|---|---|---|
| `match`：`device_type` / `sub_device_type` / `class_id` / `ui_model` / `model` | 云端 readtable 一条记录 | ✅ 脚本直接填好 |
| `state` 的 `from:` 路径（`properties.onoff.status`、`value2`…） | 状态推送（`cmd=42`）或快照 | ✅ 脚本能列出**实际出现过**的路径 |
| 单位与方向：亮度量纲、色温单位、开关 active-low/high | 真机操作 + 观察数值变化 | ⚠️ **只能给猜测 + 待确认清单** |

第三类是重点：**没有任何工具能从一份数据里推断出 active-low**。`value1 = 0` 可能是"开"也可能是"关"，取决于设备。所以工具只负责把你不用猜的部分填好，剩下明确的列出来让你验证——不会假装全自动，也不会替你写 `hardware_verified: true`。

## 首选：用骨架生成器

仓库自带：

```text
python tools/generate_device_profile.py <账号> <密码> --list
python tools/generate_device_profile.py <账号> <密码> --device 客厅插座 --listen 90 --out my_plug.yaml
```

凭据也可以用环境变量，避免写进 shell 历史：

```powershell
$env:ORVIBO_USERNAME = "你的账号"
$env:ORVIBO_PASSWORD = "你的密码"
python tools/generate_device_profile.py --list
```

国际区账号加 `--cloud-host homemate.orvibo.com`。

### 第一步：`--list` 看清全局

```text
共 5 台设备：

  1. 客厅插座
     match: device_type=9999 sub_device_type=-2
     当前识别: 未识别设备(unknown)   平台: -
  2. 已有支持的灯
     match: device_type=38 sub_device_type=-2
     当前识别: 调光调色灯(dim_color_light)   平台: light
```

- `match` 那一行就是要填进 profile 的条件；
- **`当前识别` 不是"未识别设备"的设备，说明内置已经认识它**——这时 profile 要被实体层接管必须写 `override: true`（生成器会自动加上并在待确认清单里提示）。
- 想一次看到所有可用标识字段，加 `--match full`。

### 第二步：`--listen` 抓状态推送

```text
python tools/generate_device_profile.py <账号> <密码> --device 客厅插座 --listen 90 --out my_plug.yaml
```

监听期间**你在 App 或物理开关上操作设备**：

- 开一次、关一次；
- 有亮度就调到最低、最高；
- 有色温就调到最冷、最暖；
- 窗帘就全开、全关、中间停一次。

脚本把这段时间里属于这台设备的推送全部收集起来，然后生成文件。操作越全，`state` 里能确定的字段越多，待确认项越少。

### 第三步：读生成文件顶部的待确认清单

生成的文件顶部是一份 checklist，形如：

```yaml
# ⚠️ 待确认清单（确认一项删一行）：
#   [ ] hardware_verified 保持 false：真机确认控制动作无误后再改成 true
#   [ ] state 的开关方向（active-low/high）无法从数据推断：脚本按内置惯例写的
#       （value1=0 视为“开”）。请在真机上按一次开关并把日志里的 value 与实体状态对照…
#   [ ] 亮度量纲无法推断（0-100 还是 0-255）…
#   [ ] control 是按 switch 平台猜的骨架，order/value 必须对照真机抓包确认
#
# 观察到的字段路径（次数）：
#   properties.onoff.status  ×6  值=[on, off]
#   value1  ×6  值=[0, 1]
```

清单下面的“观察到的字段路径”是**证据**：路径、出现次数、见过的所有取值。填 `state` 时可以照着它写。

## 这些参数也可以手工拿（不用脚本）

| 需要的信息 | 在哪看 |
|---|---|
| `device_type` / `sub_device_type` / `class_id` / `ui_model` / `model` | HA 里下载**集成诊断信息**：设备条目里有 `device_type_raw`、`model`，`custom_device_profiles` 段有加载状态；同时 `recent_cmd42_push` 里是原始推送 |
| 状态字段路径 | 同上：`recent_cmd42_push` 的 `raw` 就是推送原文，路径照着 JSON 层级写 |
| 内置已识别与否 | 诊断信息里每台设备的 `recognized_as`；`未识别设备` = 不需要 override |
| 控制的 value 语义 | 需要抓包比对：开发者可用 `tests/orvibo_probe.py`（`list` / `listen` / `control` 三种模式），但它面向协议取证，输出是原始 dump，且会临时改写组件文件；普通用户建议用上面的生成器 |

> ⚠️ `tests/orvibo_probe.py` 为了绕开 Home Assistant 依赖，会**临时把 `custom_components/orvibohomebridge/__init__.py` 覆盖成 `# patched`** 再还原。它不在集成运行时路径上，但如果中途异常退出，请用 `git checkout -- custom_components/orvibohomebridge/__init__.py` 确认文件完好。`tools/generate_device_profile.py` **不这么做**（用 `sys.modules` 垫片），也不会写任何你没指定的文件。

## 离线复用：抓一次，反复生成

```text
# 把原始推送存下来（含设备 ID，不要提交到公开仓库）
python tools/generate_device_profile.py <账号> <密码> --device 客厅插座 --listen 90 --capture packets.json

# 之后不登录也能反复生成
python tools/generate_device_profile.py --from-json readtable.json --device 客厅插座 --observations packets.json
```

`--from-json` 接受 readtable 的原始返回（`{"data": {"device": [...]}}`）、裸设备数组，或 `deviceId` 为键的字典。

## 常用开关

| 参数 | 作用 |
|---|---|
| `--match minimal` | 默认。`match` 只用 `device_type` + `sub_device_type`，最稳 |
| `--match full` | `match` 用上所有标识字段（`class_id`/`ui_model`/`model`）：最精确，但字段一变 profile 就不再命中 |
| `--match type` | 只用 `device_type`，最宽 |
| `--platform` | 覆盖平台推断（推断依据是观察到的字段形态） |
| `--profile-id` | 指定 profile id（默认按设备名生成；中文名会退化成 `custom_device_<device_type>`） |
| `--show-paths` | 额外打印所有观察到的字段路径与取值 |
| `--family N` | 多家庭账号选第 N 个家庭（默认 0） |
| `--verbose` | 打印调试日志 |

### 关于 `--match` 怎么选

**先用 `minimal`。** 只有一台设备同一个 `device_type` 下存在多种行为、需要精确区分时，才用 `full` 把 `sub_device_type`/`ui_model` 一起写进条件。条件写多了会变脆：固件更新换个 `ui_model`，profile 就静默不再命中，而设备会回到"未识别"——排查起来很费时间。生成器在 `full` 模式下会在清单里提示这一点。

## 工具做不到的事

- **不会判断 active-low/high**：需要你在真机上开一次关一次，对照实体状态与 `value1`；
- **不会判断亮度量纲**：观察到 `0..255` 只能说明"不像百分比"，不能证明设备就是 255 制；
- **不会判断色温单位**：`300` 像 mired、`4000` 像 Kelvin，但都能是别的含义；
- **不会写 `hardware_verified: true`**：这是"我真机验证过了"的承诺，只能由你做；
- **不发送任何控制命令**：它只读设备列表与状态推送，绝不碰控制通道，所以不会误操作你的设备。

## 相关页面

- 字段含义与取值范围：[01-schema.md](01-schema.md)
- 各平台必填字段与完整示例：[02-platforms.md](02-platforms.md)
- 抄现成配方：[03-recipes.md](03-recipes.md)
- 出问题怎么查：[04-troubleshooting.md](04-troubleshooting.md)
