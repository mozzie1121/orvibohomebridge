# ORVIBO 双项目融合设计：LAN 优先 + 云兜底

> 状态：设计定稿（2026-08-05）。主干 = `orvibohomebridge`；`orvibo-lan-control` 冻结。

## 1. 背景与目标

现有两个 HA 集成：

- `orvibohomebridge`：云端通道（SSL 长连 + REST），设备面广，门锁富功能（事件/截图/录像/临时密码/卡片）成熟。
- `orvibo-lan-control`：局域网通道（网关 UDP 发现 + TCP 8088），控制实时、无云依赖，工程加固完善（日志脱敏、推送校验、OAuth 回退）。

目标：合并为一个集成，**本地（LAN）控制优先，本地不适配或不支持的走云端**；统一维护、统一实体、统一配置。

## 2. 决策记录（ADR）

| 编号 | 决策 | 理由 |
|---|---|---|
| ADR-1 | 主干 = `orvibohomebridge`，保留 domain/实体 ID/配置条目 | HA 现有安装无需迁移；门锁富功能与卡片都在此 |
| ADR-2 | 新增传输模式开关：`自动` / `仅云`（options flow，可运行时切换） | LAN 优先是内置默认行为；门锁/晾衣机等必须走云，强制"仅 LAN"没有意义 |
| ADR-3 | 控制协议**以 homebridge 现有方法为归一基准**，LAN 侧只做适配翻译 | 避免维护两套控制语义；单一 `control_executor` 入口 |
| ADR-4 | 门锁一律 `cloud_only`（状态/事件/媒体/临时密码全走云） | 多数门锁是 WiFi 直连协议，LAN 走不通；云通道已实测完备 |
| ADR-5 | `orvibo-lan-control` 冻结归档，不再加功能 | 合并后单点维护 |
| ADR-6 | LAN 加固全部随移植带入（deviceId 变体、网关隔离、畸形值校验、脱敏、OAuth POST→GET 回退） | 安全与健壮性不因融合回退 |

## 3. 目标架构

```
orvibohomebridge（融合后）
├── transport/
│   ├── lan_gateway.py      # 移植：discovery / gateway_connection / lan_controller / gateway_manager
│   ├── cloud_ssl.py        # 现有 ssl_transport / ssl_client
│   └── rest.py             # 现有 https_client / cloud.py；合入 OAuth POST→GET 回退
├── capabilities.py         # 统一能力表（原 profiles.py + device_types.py）
├── models.py               # TransportMode、StateSource、路由决策结构
├── state_store.py          # StateSource 合并 LAN > SSL > CLOUD > OPTIMISTIC > INITIAL
├── control_router.py       # 传输路由：capability × mode × 网关可达性
├── control_executor.py     # 唯一控制入口；LAN 适配层只翻译 payload
├── parsers/                # 统一状态解析（LAN/SSL 原始包都进同一解析链）
├── lock/（现有）           # 门锁云策略：lock_media / temp_password / history / card
├── config_flow.py          # 账号→区域→家庭→网关发现/认证→设备选择→锁用户映射
└── options_flow.py         # 传输模式开关（自动/仅云）
```

## 4. 传输模式与路由

### 模式定义

```python
class TransportMode(str, Enum):
    AUTO = "auto"        # 默认：LAN 优先，失败/不支持降级云
    CLOUD_ONLY = "cloud_only"
```

### 设备能力表字段

每个设备类型（deviceType/subDeviceType）记录：

- `platforms`：注册哪些 HA 平台
- `control_channel`：`lan` / `ssl` / `none`（只读）
- `status_only`：只读设备（沿用门锁只读语义）
- `cloud_only`：LAN 不接管（门锁 107/522、晾衣机 52 等 WiFi 直连；
  type 300 按子类型走 homebridge 实测定义：481 地暖可云控 / 491 温湿度只读）
- `verified`：真机验证标记（沿用现有 known_device_models）

### 路由决策

```
mode=cloud_only        → 一律 ssl（含状态）
mode=auto（严格语义）：
  device.cloud_only    → ssl
  其余设备             → 一律 lan（无云兜底；网关不可达则控制失败/设备不可用）
```

自动模式即"仅本地 + 云专属"：非 `cloud_only` 设备一律走 LAN（状态与控制都由本地
维护，云端轮询只同步 online，不覆盖本地状态）；`cloud_only` 设备（门锁、晾衣机 52
等 WiFi 直连）必须走云。

模式切换时：清空旧传输源的状态修订（或整体重建 StateStore），避免残留高优先级值。

## 5. 控制协议归一（ADR-3 细则）

- 规范层：以 homebridge 现有控制方法为唯一 API（`send_control_switch/light/curtain/ac/fan/...`）。
- LAN 适配：`lan_adapter` 把规范调用翻译成 LAN payload（cmd=15 + value1~4/properties），并做字段语义映射。
- 已知差异（实现阶段逐项比对、以真机/抓包为准）：
  - 开关/灯：云 `value1=0 开 / 1 关` 语义 vs LAN 侧可能反转，需归一化；
  - 窗帘：云 `percent` vs LAN 位置字段；
  - 空调：模式/温度枚举差异；
  - 响应：云控制等 cmd=42 回显，LAN 等 cmd=15 回执 + cmd=42 推送。
- **禁止两套状态解析**：LAN 与 SSL 的原始包统一进 `parsers/`，归一后写 StateStore；控制响应只做回执确认，不做二次解析。

## 6. 状态合并

```python
class StateSource(IntEnum):
    INITIAL = 0
    OPTIMISTIC = 10
    CLOUD = 20      # REST/readtable 轮询、云端快照
    SSL = 30        # 云端实时推送
    LAN = 40        # 网关实时推送
```

沿用现有字段级 `priority_guard_seconds` 防回滚；LAN 值在 guard 窗口内不被稍旧的云轮询覆盖。

### 6.1 响应优先级（自动模式）

回答"网关和云端都会响应，是否优先 LAN"：**是，但分两层处理**：

1. **状态合并（谁的值生效）**：LAN > SSL > CLOUD > OPTIMISTIC > INITIAL。
   - LAN 与云同时推送同一设备：LAN 先到先更新，字段级 guard 内低优先级（SSL/CLOUD）不覆盖；
   - 双通道重复推送同值：StateStore 字段级去重，不触发实体重复更新；
   - 控制后的乐观更新（OPTIMISTIC=10）优先级最低，任何真实推送都可覆盖。
2. **控制响应（谁的回执有效）**：以发起方为准，transport 隔离：
   - LAN 控制 → 只等网关 cmd=15 回执 + 对应 cmd=42 推送；
   - 云控制（仅 cloud_only/仅云模式）→ 只等 SSL 回显；
   - 非 cloud_only 设备不做云兜底，**绝不跨 transport 匹配**（两者 serial 空间独立）；
   - 回执只做成功确认，最终状态一律由 StateStore 按上述优先级合并。

## 7. 门锁策略（ADR-4）

- 能力表：107/300/522 全部 `status_only=True, cloud_only=True`；
- 状态来源：云 SSL 推送 + readtable 初始化/轮询（现状不变）；
- 事件/截图/录像/临时密码/卡片：沿用现有 lock 模块，不加 LAN 分支；
- LAN 网关即使发现门锁设备，也不注册其控制/状态实体（避免双源混淆）。

## 8. 配置流

1. 账号 + 密码 → 区域探测 → SSL 登录探针（现有）；
2. 家庭选择（现有）；
3. **网关发现 + 认证探针**（移植 lan-control）：UDP 发现 MixPad → TCP 登录校验 → 网关 UID 绑定；
4. 设备选择（现有，增加"可用通道"标注：LAN/云/只读）；
5. 锁用户映射（现有）；
6. options flow：传输模式开关（自动/仅云，默认自动）。

配置条目升级：`CONF_TRANSPORT_MODE` 默认 `auto`，旧条目自动迁移。

## 9. 分阶段实施计划

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| 0 | 能力表合一（capabilities.py）、常量收敛、packet 协议核对（LAN/SSL 帧格式差异确认） | 现有 0.4.2 行为不变，测试全绿 |
| 1 | LAN 传输层移植 + StateStore 增加 LAN 源 + 统一解析链 | HA 可见网关，LAN 设备状态实时更新，云状态不回滚 |
| 2 | 控制路由融合：lan/ssl 双 scope、自动降级、LAN payload 适配层 | 灯/帘/空调 LAN 直控；拔网关自动走云；仅云模式行为与现状一致 |
| 3 | 门锁云策略固化 + 事件总线加 `source` 字段 | 门锁功能与 v0.4.2 完全一致 |
| 4 | 配置流/options 开关 + 实体平台去重 + 卸载 orvibo_lan 指引 | 新装/升级/改模式三条路径测试通过 |
| 5 | 测试/CI/文档/发布 0.5.0（ruff/mypy/pytest/hassfest/HACS） | 全套通过，README 更新，发布 PR |

预计每阶段 2~4 天，合计约 2 周。

## 10. 兼容与风险

- 实体 ID、device registry、配置条目全程不变（domain 不变）；
- HA 中卸载 `orvibo_lan` 后，其实体需手动清理或保留历史；文档给指引；
- 无网关环境（纯云用户）：默认 auto 等效纯云，无感知；
- LAN 安全校验（网关隔离/畸形值/脱敏）随移植保留；
- 控制协议差异以"真机 + 抓包"逐项校准，禁止凭猜测改语义。

## 11. 阶段 0 结论（2026-08-05）

### 11.1 协议核对

- **帧格式同构**：LAN 与 SSL 使用同一封包格式（`MAGIC=hd`、2B 大端长度、2B 包类型、
  CRC32、32B sessionId、AES-ECB JSON），封包层可共用/收敛为一个模块。
- **握手命令号一致**：网关与云端均为 `CMD_HELLO=0` / `CMD_LOGIN=2`；
  `CMD_CONTROL=15` / `CMD_STATE_UPDATE=42` / `CMD_HEARTBEAT=32` 一致。
- **死代码清理点**：`orvibo-lan-control/lib/device_control.py` 里的
  `CMD_HELLO=1 / CMD_LOGIN=3 / SOFTWARE_VER=5.1.5.302` 从未被运行时使用
  （握手走 `gateway_connection` + `packet.py`），融合时删除，常量一律以
  homebridge `const.py` 为准（`SOFTWARE_VER=5.1.3.309`、`SIGN_KEY`、
  `DEFAULT_KEY` 两项目一致）。

### 11.2 能力表（capabilities.py）

- 新增 `capabilities.py`：类型级 `status_only` / `cloud_only`、LAN 可控类型集合、
  分类→平台快照、`registration_only`（未知/未验证设备）不给控制通道。
- 门锁 107/522：`status_only + cloud_only`，无设备控制通道（临时密码/媒体
  是服务不是设备控制）；晾衣机 52：`cloud_only` 但可云控；
  type 300 不设类型级云专属，按 homebridge 实测子类型解析（481 地暖 / 491 温湿度）。
- LAN 可控类型（自动模式下双通道）：0/1/34/35/36/38/81/102/501/502/503/516。
- 单测 7 个通过；全量回归 **307 通过**（阶段 0 不改变现有行为）。

### 11.3 遗留核对项（阶段 1 处理）

- ~~type 300 云专属语义~~ 已按 homebridge 实测定义定稿：300/481 地暖（云控、
  非只读）、300/491 温湿度（只读、非云专属），能力表与测试已覆盖；
- type 81 平台差异：lan 注册 climate+fan，homebridge 把风速折入 climate，
  融合以 homebridge 为准；
- 501/502/503/522 的分类依赖 subType/classId，能力解析须走完整
  `classify_device`（真实设备字段齐全，测试已按真实字段覆盖）。
