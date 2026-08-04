# Changelog

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
