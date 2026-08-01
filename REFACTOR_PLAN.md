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

## 设计原则
1. **协议层零依赖** — protocol.py、control.py 只有 Python 标准库
2. **不破坏现有功能** — 重构过程中现有 import 保持兼容
3. **测试先写** — 每轮先写测试再重构代码
4. **逐步迁移** — 不是一次性大重构，可分批上
