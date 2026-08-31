# Changelog

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
