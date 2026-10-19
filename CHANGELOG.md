# Changelog

## [Unreleased]

### Added

- **自定义设备 Profile**：不支持的设备现在无需改代码，只要把声明式
  YAML/JSON profile 放进 `<HA config>/orvibohomebridge/devices/` 或
  `custom_components/orvibohomebridge/custom_devices/` 即可支持：
  - 声明匹配规则（`device_type` / `sub_device_type` / `class_id` /
    `status_type` / `ui_model` / `model` / `class_name` / `product_name` /
    `property_present` / `property_equals`，支持 `re:` 正则与 `priority`）后，
    设备会按其声明的 HA 平台创建实体（light / switch / cover / sensor /
    binary_sensor / climate / fan）；
  - 声明式状态映射（`from` 路径、`map`、`scale`、`clamp`、mired↔Kelvin、
    `true_values`/`false_values`）编译为与内置解析器相同的 `StateParser`；
  - 声明式控制（`on`/`off`/`brightness`/`color_temp`/`position`/`stop`）
    编译为 `ControlRoute`，复用已验证命令族（`on`/`off`/`set property`/
    `move to level`/`fast move to level`/`fast color temperature`/
    `open`/`stop`），LAN 与云端同名同参下发；
  - 安全边界：`hardware_verified` 未置位时只登记不下发控制；命中已被内置
    识别的设备默认不生效（需显式 `override: true`）；未声明的动作会报错
    拒绝，不会静默借用内置命令；
  - 选项流程新增「自定义设备 Profile」页（查看已加载 profile、命中设备数与
    加载错误，并可重载）；诊断信息输出 `custom_device_profiles` 段。
  - 已实现自定义实体的平台：`light` / `switch` / `cover` / `sensor` /
    `binary_sensor`。`climate` 与 `fan` 目前 schema 接受、设备可识别、状态可
    解析，但尚未实现自定义实体（跳过并记录 warning，不会落到内置实体）。
  - 文档：`docs/custom-devices/`（总览、字段全表、各平台示例、配方、排查、
    何时需要提 PR、**怎么拿到写 profile 需要的参数**）。
  - 内置模板：`custom_components/orvibohomebridge/custom_devices/example_demo_light.yaml`。
  - **参数获取工具**：`tools/generate_device_profile.py` + `tools/profile_skeleton.py`。
    用户不必手抄原始报文：`--list` 列出所有设备及其 `match` 字段并标出哪些已被
    内置支持，`--listen` 期间在 App/物理开关上操作设备即抓到真实 `cmd=42` 推送，
    然后生成可直接使用的 profile 骨架（自动填 `match`、按观察到的路径填 `state`、
    按平台给 `control` 骨架、附一份"必须真机确认"的清单）。
    工具只读（不发送控制命令、不改仓库文件），`--from-json/--observations` 支持
    离线复现，`--match minimal|full|type` 控制匹配条件松紧。
  - **控制命令实证探针**：`tools/probe_device_control.py` + `tools/control_probe.py`。
    云端长连接与网关 TCP 都是 AES 加密（会话密钥握手后才得到），App 自身双向
    TLS 且固定证书，因此**控制命令无法被动嗅探**——`readtable` 与状态推送也给不了
    `control`。本工具把"只发不等结果"补成闭环：发一条候选 → 读该设备的状态推送
    → 判定 `verified`/`ineffective`/`inconclusive`（服务端 ACK 不算证据）。
    自动枚举 on/off 两种极性、`set property` 属性报文、亮度两种量纲、色温
    mired/Kelvin、位置型；`--payloads` 可灌入反编译 App 得到的真实帧再验证；
    风险设备（门锁/晾衣机/窗帘）强制二次确认；`--observe-only` 先验推送链路。
    验证通过的 `control` 块可直接替换进 profile。
  - 文档：`docs/custom-devices/07-reverse-engineering-control.md`（控制逆向全流程），
    并在 06 与 README 中明确标注"`control` 是占位骨架、启用前必须实证验证"。

### Fixed

- 无（本版本未修复内置设备行为）。

### Notes

- 自定义设备实现期间顺带修掉两处新代码缺陷：`map:` 目标值为布尔/字符串时会
  被数值化丢弃；显式声明的 `optimistic: {state: false}` 会被隐式默认覆盖。
  两者都只影响新功能，未改变内置设备行为。

## [0.6.0] - 2026-08-31

### Added

- 配置流：设备选择按类别分组、分阶段选择（staged category selection），中英文翻译补全。
- 明暗两套品牌资源（brand icons/logos）。
- 传输路由与运行时模式加固：`LAN_ONLY` 模式、设备传输路径诊断传感器
  （`configured_mode` / `lan_control_supported` / `cloud_control_supported` /
  `cloud_only` / `gateway_connected` / `last_control_transport`）。

### Fixed

- **可用性误判**（成片 unavailable / 时间长了陆续变灰 / #11 / #12）：
  - 可用性改为正向证据模型（实时推送新鲜 / LAN 网关在线 / 云端记录新鲜），
    `get_device_state` 纯函数化，不再"600 秒无推送即判离线"；
  - 云端记录增加 `cloud_online` / `cloud_online_time` 元数据，按 `updateTimeSec`
    新鲜度判定，陈旧记录不再把在线设备判离线或覆盖真实状态（nan_nan 场景）。
- **控制链路**：
  - `_send_packet` 返回真实发送结果，`send_control_*` 不再无条件"假成功"；
    发送失败不写乐观状态（顺带修复 LAN→SSL 降级失效）；
  - 控制回显携带状态时并入状态管线（不再丢弃），回显无状态才乐观兜底；
  - 亮度+色温合并单条复合指令（避免两步连发响应错配）。
- **SSL 通道**：
  - 请求-响应式心跳 + 读超时，半开/黑洞连接不再"假死"；
  - 重连循环无限重试（指数退避封顶 60s），不再 5 次失败后永久退出；
  - 重连成功后主动全量重同步（实测服务端重登后不推送设备列表）；
  - 协调器轮询增加 SSL 看门狗。
- **平台语义统一**：binary_sensor / fan 可用性与 light/cover/switch 一致。
- **健壮性**：解析器与实体属性 `int()` 全面保护（`to_int`）、`_apply_generic`
  缺失字段不再映射成"关"、cover 未知位置不显示为"开"、网关记录缺失容错
  （连续 3 轮才清理，不再单次快照断 LAN）。
- **云端值新鲜度门控**：云端快照仅在记录新鲜（或 cloud_only 设备）时覆盖
  运行时状态，消除陈旧快照把"实际已关"显示成"开"（含门锁/晾衣机例外处理）。

### Security

- 保留 v0.5.0 的 LAN 推送校验、日志脱敏；控制发送失败不再伪装成功。

## [0.5.0] - 2026-08-05

### Added

- 融合局域网与云端双通道（LAN 优先 + 云兜底）：
  - LAN 网关 UDP 发现 / TCP 连接 / 实时状态推送（`StateSource.LAN` 最高优先级）；
  - LAN 控制适配层：复用云端 payload 构造器，仅切换发送通道（以 homebridge 协议为归一基准）；
  - 传输模式选项：`自动` / `仅云`（选项流可运行时切换，默认自动）。
- 门锁云策略固化：门锁（107/522）状态/事件/媒体/临时密码一律走云，LAN 不接管。
- 门锁事件总线新增 `source` 字段（`lan` / `ssl`），便于自动化区分来源。

### Security

- LAN 状态推送校验：deviceId 三变体兼容、网关 UID 隔离、畸形数值/派生字段拒绝。
- 日志脱敏（IP/UID/设备 ID 只保留末段）随 LAN 引擎并入。

## [0.4.2] - 2026-08-05

### Changed

- 配置项升级到 v3，仅保存 ORVIBO 协议密码摘要，并自动迁移旧配置。
- 中国区与国际区改为每配置项独立检测和持久化，支持不同区域账号并存。
- TLS transport、响应关联、状态来源对账和 Home Assistant 服务处理器拆分为独立模块。
- 设备状态解析拆分到 `parsers/`，门锁事件编排拆分到 `lock_manager.py`，
  电源/亮度/色温分流拆分到 `control_router.py`。
- SSL 状态分发、门锁媒体、临时密码、设备库存和共用控制执行分别拆分到
  `status_dispatcher.py`、`lock_media_manager.py`、`temp_password_manager.py`、
  `device_inventory.py` 与 `control_executor.py`；协调器保留兼容接口并聚焦 HA 生命周期。
- README 支持表中的设备标记为真机验证；未知设备只注册展示，不发送推测控制命令。
- `list_events` 和 `fetch_video` 不再返回主机绝对文件路径。
- `list_temp_passwords` 不再返回密码；新密码只在 `grant_temp_password` 本次响应中提供。

### Fixed

- 修复控制、COS 与临时密码快速响应可能早于等待器注册的问题。
- 修复多个配置项切换云端区域时互相覆盖 API 主机的问题。
- 修复 SSL 监听任务取消时等待自身，以及所有实例共享重连锁的问题。
- 修复 `deviceType=300` 温湿度传感器未进入统一设备分类的问题。
- 避免稍旧的云端轮询立即覆盖刚收到的 SSL 实时状态。
- type 107 门锁实时状态改用门锁归一化解析；门铃/开锁瞬态事件使用本地匹配后的
  设备 ID 安排复位，避免 ID 形态不一致导致状态无法自动恢复。

### Security

- OAuth 端点按服务端要求使用 HTTPS GET 结构化查询参数，不在代码或日志中拼接/记录
  完整凭据 URL；日志、事件和实体状态不记录临时密码。
- 媒体对象键拒绝 URL、路径穿越、查询串、反斜杠和无效事件格式。
- 录像、历史与临时密码操作会校验目标属于当前配置项且确实为门锁。
