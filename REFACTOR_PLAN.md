# ORVIBO HomeBridge 改造计划

## 目标
参照 orvibo-cloud 的架构，取长补短，不破坏现有功能。

## 改造轮次

### 第1轮：抽 protocol.py（纯数据模型 + REST 解析）
- 创建 `protocol.py`：OrviboDevice/OrviboFamily dataclass + `parse_readtable_devices()` + 签名函数
- 纯 Python 标准库，零外部依赖
- https_client.py 瘦身：协议解析逻辑移到 protocol.py，只保留 HTTP 调用

### 第2轮：抽 control.py（命令映射）
- 创建 `control.py`：OrviboControlCommand dataclass + 各种控制命令函数
- 现有 ssl_client.py 的控制 payload 构造迁移过来

### 第3轮：搭测试目录
- 创建 `tests/` 目录，参照 orvibo-cloud 模式（每个测试文件直接用 importlib.util 加载模块）
- `test_protocol.py`：测试设备解析、签名
- `test_control.py`：测试命令映射
- `test_binary_protocol.py`：测试二进制帧构造/解析

### 第4轮：config_flow 升级（选设备+区域映射）
- 创建 `selection.py`：设备选择/区域映射辅助函数
- 升级 config_flow.py：三步配置（登录→选家庭→选设备+设区域）
- 参照 orvibo-cloud 的 selection.py + config_flow.py 逻辑

### 第5轮：测试全覆盖（可选）
- 逐步给 coordinator 状态解析加测试
- 确保所有协议层函数有测试覆盖

### 第6轮：reauth 与凭据安全（已完成 2026-08-02）
- config_flow 增加 `reauth` / `reauth_confirm` 流程，凭据失效由 HA 自动触发重新认证
- 新增 `redact.py`（移植 orvibo-cloud 的脱敏方案）：日志严格打码、诊断保留可读值但指纹化标识
- coordinator / ssl_client 移除会泄露 `session_key`、原始推送 payload 的 debug 日志
- `https_client` 恢复 TLS 证书校验（移除 `ssl=False`）
- diagnostics 改为脱敏输出，`_cmd42_log` 仅存内存、绝不写日志

### 第7轮：发布自动化（已完成 2026-08-02）
- 新增 `validate.yml`（unittest + hacs + hassfest）
- 新增 `hacs-release.yml`（每周二/五 beta、v tag 正式版、`orvibohomebridge.zip` 契约）
- `hacs.json` 启用 `zip_release`，资产固定为 `orvibohomebridge.zip`

### 第8轮：SSL 层认证校验（已完成 2026-08-02）
- 验证结论：REST `getOauthToken` 不校验密码（任意密码返回有效 token，可拉取家庭/设备）；
  真正的密码校验在 10002 端口二进制 SSL 登录（假密码返回 `status=12`）。
- coordinator：SSL 登录被服务器明确拒绝时抛 `ConfigEntryAuthFailed`，触发 HA 重新认证；
  网络类失败保持后台重试，不误报。
- config_flow / reauth：新增 `_probe_ssl_login()` 轻量探针（单次握手、快速超时），
  在配置阶段就把错误密码拦截为 `auth_failed`。

### 第9轮：门锁事件走通（代码已完成 2026-08-02，待实机样本校准）

发现并修复两个"事件永远到不了实体"的根因：
- `ssl_client._handle_state_update` / `_handle_device_status_report` 未把 `cmd`、`action`、
  `event` 透传到 raw_status 顶层，`on_status_update` 的 `cmd==352` 分支和
  `_parse_doorbell_event` 永远匹配不上。
- `respByAcc` 过滤把缺失该字段的主动推送（门锁状态、门铃/开锁事件）整个丢弃。

代码改动：
- 新增 `lock_status.py`（纯标准库）：两种门锁属性形态归一化
  （`doorLock.lockState/doorState/insideLockState` 与 `door_status/reverse_lock/handle/clild_lock`）、
  双电池解析（batteryManager/batteryManager1）、cmd=352 事件解析
  （unlockEvent / ring / answered / bye）。
- `ssl_client.py`：透传 `cmd/action/event`；`respByAcc=false` 且非事件才过滤。
- `coordinator.py`：门锁解析改用归一化；门锁状态/事件发布到 HA 事件总线
  `orvibohomebridge_lock_event`（device_id/uid/locked/door_open/unlock_type 等，日志脱敏）；
  门铃/开锁复位改为按 (device, kind) 单任务管理（`LOCK_RESET_DELAY=5`）。
- 测试：`tests/test_lock_status.py` 13 例，全套 108 例通过。
- 安全：5 个 tests/ 脚本的硬编码账号密码改为读取环境变量 `ORVIBO_USERNAME` /
  `ORVIBO_PASSWORD`。

待办：
- 用 `tests/orvibo_probe.py ... lock` 实机抓包（动作标记 + 脱敏 JSONL），固化 fixtures。
- 根据真实样本校准归一化键名与事件字段。

### 第9轮补充：实机抓包校准（已完成 2026-08-02）

基于 V5 Eyes 门锁（type=522, subDeviceType=463）实机抓包校准：

确认的报文形态：
- 锁状态：cmd=42 `properties.doorLock.{lockState,doorState,insideLockState}`（on=锁定/门开/内锁）。
- `insideLockState` 会**单独推送**（properties 中只有该字段），必须按字段增量更新。
- 开锁事件：cmd=352 `unlockEvent {type: fingerprint|password, userId}`。
- 开锁失败告警：cmd=352 `errorUnlockEvent {type}` + cmd=82（infoType=39, isAlarm=1）
  "开锁身份多次验证失败，门锁暂时被锁定"。
- 门铃：cmd=352 `doorbell ring`（含抓拍图 url）+ cmd=82（infoType=68）"有客人来访"。
- 解锁文本消息：cmd=82（infoType=12）"XX 用密码/指纹打开门锁"。
- 门未关提醒：cmd=352 `doorUnclose`（无 value）+ cmd=82（infoType=39, isAlarm=1）
  "门未关，请及时关门"。
- 撬锁/非法侵入：cmd=352 `picklockEvent`（value 可含 videoUrl/url，也可能为空对象）+
  cmd=82（infoType=39, isAlarm=1）"有人撬开门锁非法侵入，请确认现场情况！"。
- 离家防护报警：cmd=352 `leaveHomeEvent`（value 可含 videoUrl/url，也可能为空）+
  cmd=82（infoType=39, isAlarm=1）"离家防护模式下从门内打开门锁"。
- 离家布防状态：`doorLock.leaveHomeAlarmCfg`（on/off）独立推送，纳入 `leave_home_armed` 状态。
- 电池状态：cmd=42 独立推送 `batteryManager`（干电池）/ `batteryManager1`（锂电池）。
  `isSetupBattery=off`（未安装电池）时 level=0 无意义，置为未知。
- 云端会重复推送同一事件（同一 unlockEvent 出现两次），需按签名去重。

代码更新：
- `lock_status.py`：新增 `errorUnlockEvent`；消息解析扩展 `pic_url`/`time`；状态归一化支持部分字段。
- 后续新增 `doorUnclose`（门未关）、`picklockEvent`（撬锁）与 `leaveHomeEvent`（离家防护），
  均含 video_url/pic_url 且兼容空 value。
- `coordinator.py`：门锁状态按字段增量更新（避免部分推送误重置）；
  事件总线按 (kind/type/userId/time 或 state 组合) 签名去重。
- fixtures：`tests/fixtures/lock_v5eyes_samples.jsonl`（脱敏真实样本 11 条）。
- 测试：新增 errorUnlock/部分更新/门铃消息/告警消息/样本冒烟，全套 117 例通过。

## 设计原则
1. **协议层零依赖** — protocol.py、control.py 只有 Python 标准库
2. **不破坏现有功能** — 重构过程中现有 import 保持兼容
3. **测试先写** — 每轮先写测试再重构代码
4. **逐步迁移** — 不是一次性大重构，可分批上
