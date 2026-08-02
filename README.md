[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

一个用于 Home Assistant 的欧瑞博（ORVIBO）智能设备集成。通过 SSL 长连接和 MQTT 状态推送，实现对欧瑞博智能家居设备的实时控制和状态监控。

## 📦 安装

### HACS 安装（推荐）

[![在 Home Assistant 中打开 HACS 仓库](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mozzie1121&repository=orvibohomebridge&category=integration)

1. 打开 Home Assistant → HACS → 集成
2. 点击右上角 "⋮" → **自定义存储库**
3. 添加：`https://github.com/mozzie1121/orvibohomebridge`，类别：**集成**
4. 搜索 "ORVIBO HomeBridge" 并点击安装
5. 重启 Home Assistant

### 手动安装

将仓库中的 `custom_components/orvibohomebridge` 文件夹复制到 Home Assistant 的配置目录：

```
<ha-config>/custom_components/orvibohomebridge/
```

重启 Home Assistant。

## ✨ 功能特性

- ✅ **实时状态同步**：通过 SSL 长连接和 MQTT 推送，设备状态实时更新
- ✅ **灯光控制**：开关、亮度、色温调节
- ✅ **窗帘控制**：开合控制、位置调节、停止
- ✅ **空调控制**：开关、温度、模式、风速
- ✅ **新风系统**：开关、预设模式（停/慢/快）
- ✅ **传感器支持**：人体传感器、门窗传感器、温湿度传感器、烟雾传感器、可燃气体探测器、紧急按钮、水浸探测器
- ✅ **智能门锁**：门磁状态、锁状态、门铃事件、开锁事件、电池电量监控
- ✅ **智能晾衣机**：照明、消毒、风干、热干、升降
- ✅ **自动发现**：自动识别区域服务器和家庭 ID

## 🔧 支持的设备

### 灯光设备
| 设备类型 | 支持功能 |
|---------|---------|
| S2 智能防眩射灯 | 开关、亮度、色温调节 |
| S3 系列射灯 | 开关、亮度、色温调节 |
| S5 系列射灯 | 开关、亮度、色温调节 |
| S10 系列射灯 | 开关、亮度、色温调节 |
| 柔光 系列射灯 | 开关、亮度调节 |
| 磁吸轨道系列 | 开关、亮度、色温调节 |
| 智能灯带控制器 | 开关、亮度、色温调节 |
| 0-10V 调光模块（调光模式） | 开关、亮度调节 |
| 0-10V 调光模块（色温模式） | 开关、亮度、色温调节 |
| MixSwitch系列开关（一二三四开）| 开关控制 |
| TouchClassic系列开关（一二三开）| 开关控制|
| Gauss系列开关（一二三开）| 开关控制 |
| Defy系列开关（一二三开）| 开关控制 |
| BACH系列开关（一二三开）| 开关控制 |
| 单色灯 (deviceType=102/501) | 开关控制 |
| 可调光灯 (deviceType=502) | 开关、亮度调节 |
| 调光调色灯 (deviceType=38) | 开关、亮度、色温 |
| 色温灯带 (deviceType=503) | 开关、亮度、色温 |

### 窗帘设备
| 设备类型 | 支持功能 |
|---------|---------|
| Zigbee 窗帘 (deviceType=34) | 开合控制、位置调节、停止 |

### 空调设备
| 设备类型 | 支持功能 |
|---------|---------|
| 风机盘管空调 (deviceType=36) | 开关、温度、模式、风速 |

### 新风系统
| 设备类型 | 支持功能 |
|---------|---------|
| 新风系统 (deviceType=516) | 开关、预设模式（停/慢/快） |

### 传感器设备
| 设备类型 | 支持功能 |
|---------|---------|
| 人体传感器 (deviceType=26) | 人体检测、电池电量 |
| 门窗传感器 (deviceType=46) | 门磁状态、电池电量 |
| 温湿度传感器 (deviceType=300) | 温度、湿度、电池电量 |
| 烟雾传感器 (deviceType=27) | 烟雾检测、电池电量 |
| 可燃气体探测器 (deviceType=25) | 气体检测（长供电，无电量传感器） |
| 紧急按钮 (deviceType=56) | 按钮触发状态、电池电量、3分钟自动恢复 |
| 水浸探测器 (deviceType=54) | 水浸检测、电池电量 |

### 智能门锁
| 设备类型 | 支持功能 |
|---------|---------|
| 智能门锁 (deviceType=522) | 门磁状态、锁状态、门铃事件、开锁事件、干电池电量、锂电池电量 |

**门锁截图实体**：每把门锁还会生成一个 `camera` 实体（`门锁截图`），事件到达时
自动更新为最新截图（门铃抓图/撬锁告警图），门锁卡片上直接显示，无需额外配置。
注意这是事件快照，不是实时视频（猫眼实时流走 SEP2P 私有协议，暂不支持）。

### 其他设备
| 设备类型 | 支持功能 |
|---------|---------|
| 智能晾衣机 (deviceType=52) | 照明、消毒、风干、热干、升降 |

## 🔧 配置

### 通过 UI 配置

1. 在 Home Assistant 中，进入 **设置** → **设备与服务** → **添加集成**
2. 搜索 **ORVIBO HomeBridge**
3. 输入您的欧瑞博账号（手机号）和密码
4. 选择家庭（如果有多个）
5. 完成配置，所有支持的设备将自动添加

### 配置参数

| 参数 | 说明 |
|------|------|
| username | 欧瑞博账号（手机号） |
| password | 欧瑞博密码 |
| family_id | 家庭 ID（可选，自动获取） |

## 📱 使用说明

### 设备控制

- **灯光**：在 Home Assistant 中可以控制开关、亮度、色温
- **窗帘**：支持开合控制和位置调节（0-100%）
- **空调**：支持开关、温度调节、模式切换、风速调节
- **新风**：支持开关和风速模式切换（停/慢/快）

### 传感器状态

- **人体传感器**：检测到人体时触发，30秒后自动恢复
- **门窗传感器**：实时监测门/窗的开关状态
- **温湿度传感器**：实时监测温度和湿度
- **烟雾传感器**：检测到烟雾时触发报警
- **可燃气体探测器**：检测到可燃气体时触发报警

### 智能门锁

- **门磁状态**：监测门的开关状态
- **锁状态**：上锁 / 未上锁 / 门内反锁 / 异常（门磁开但锁上锁，异常关门或测试状态）
- **门铃事件**：有人按门铃时触发，5秒后自动恢复
- **开锁事件**：记录开锁方式（指纹、密码等），5秒后自动恢复
- **电池电量**：分别显示干电池和锂电池的电量百分比

#### 开锁事件属性

**开锁事件** 传感器（状态显示 `张三开门` / `用户2开门` / `无`）附带以下属性：

| 属性 | 说明 |
|------|------|
| `unlock_type` | 开锁方式，见下方取值表 |
| `unlock_user_id` | 用户 ID |
| `unlock_user_name` | 用户名称（在配置 → 锁用户映射 中设置后才有） |
| `unlock_time` | 事件时间戳（Unix 秒） |

`unlock_type` 取值：

| 值 | 含义 |
|------|------|
| `fingerprint` | 指纹 |
| `password` | 密码 |
| `face` | 人脸 |
| `card` | 卡片 |

#### 事件总线（自动化推荐）

所有门锁状态/事件都会发布到 HA 事件总线 `orvibohomebridge_lock_event`，开锁事件（`kind=unlock`）字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | string | `unlock` |
| `device_id` | string | 锁设备 ID |
| `unlock_type` | string | 开锁方式：`fingerprint` 指纹 / `password` 密码 / `face` 人脸 / `card` 卡片 |
| `unlock_user_id` | int | 用户 ID |
| `unlock_user_name` | string | 用户名称（配置映射后才有） |
| `time` | int | 事件时间戳（Unix 秒） |

其他事件类型（`kind`）：`error_unlock` 开锁失败、`picklock` 撬锁、`door_unclose` 门未关、
`leave_home` 离家防护报警、`ring` 门铃、`message` 门锁文本消息。

**媒体字段**：带图片/视频的事件（撬锁告警、门铃抓图、门锁消息配图）附带以下临时 URL
（通过门锁专用 COS 凭证签名，默认 10 分钟有效，可放进通知或前端直接访问）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `media_url` | string | 事件视频地址（如撬锁告警 h264） |
| `pic_media_url` | string | 事件图片地址（如撬锁告警截图/门锁消息配图） |
| `doorbell_media_url` | string | 门铃抓图地址 |

集成启动后会在后台预取门锁 COS 凭证（36 小时有效），事件到达时即时签名。

**事件录像**：带 `video_url` 的事件（撬锁/离家告警录像，`.h264` 裸流）会在后台自动
下载并转封装为 MP4，事件附带：

| 字段 | 类型 | 说明 |
|------|------|------|
| `video_file` | string | 本地 MP4 路径（`config/media/orvibohomebridge/...`，转码完成后可播放） |
| `media_id` | string | HA 媒体浏览器引用 ID（`media-source://...`） |

也可通过服务 `orvibohomebridge.fetch_video` 主动拉取任意录像（返回 `video_file` /
`media_id` / `mp4_file`）：

```yaml
action: orvibohomebridge.fetch_video
data:
  device_id: w-77c139c4d27f4fa6a20e1f459849aa47
  object_key: "{{ trigger.event.data.video_url }}"
response_variable: video_result
```

视频转封装使用 ffmpeg（`-c copy` 无损，秒级完成）；HA 环境通常自带，缺失时录像
仍会以 `.h264` 原文件保存（`h264_file` 字段）。

#### 事件历史回溯

门铃/撬锁/离家等事件触发时，截图与录像会自动归档到
`config/media/orvibohomebridge/<设备>/`（文件名为 `<事件类型>_<时间戳>`）。
HA 侧边栏 → **媒体** → 媒体浏览器打开即可按时间倒序浏览全部历史记录，
点击截图/视频即可回溯查看。

也可通过服务 `orvibohomebridge.list_events` 查询结构化历史：

```yaml
action: orvibohomebridge.list_events
data:
  device_id: w-77c139c4d27f4fa6a20e1f459849aa47
  limit: 50
response_variable: history
```

返回每条记录含 `kind`（ring/picklock/leave_home 等）、`time`、`type`
（image/video）、`file`（本地路径）与 `media_id`（媒体浏览器引用）。
有"有人逗留"等新事件类型时也会自动归档，无需额外配置。

**自动清理**：历史记录默认保留 7 天，集成启动时清理一次，之后每周自动执行
（删除超过 7 天的截图/录像及空目录）。也可手动清理或调整保留策略：

```yaml
action: orvibohomebridge.cleanup_history
data:
  keep_days: 7
  max_entries: 500   # 可选：每个设备最多保留 500 条，按时间裁剪
```

## 🔑 临时密码

支持下发临时密码到门锁（实测通过：type=1 限时 / type=2 临时，最多 4 个有效密码，
可选短信通知、自动回收过期密码）。

### 下发临时密码（自动化/手动）

```yaml
action: orvibohomebridge.grant_temp_password
data:
  device_id: w-77c139c4d27f4fa6a20e1f459849aa47   # 可选，留空自动选第一把门锁
  type: 2              # 1=限时 2=临时（默认）
  minutes: 1440        # 有效期（分钟），type=1 可用 start_time/end_time 指定绝对时间
  number: 1            # 可用次数（0=不限）
  name: 快递员          # 可选
  phone: "13800138000" # 可选，同步短信通知该手机号
response_variable: temp
```

返回：`password`（6 位临时密码）、`authorized_id`、`start_time`/`end_time`、`number` 等。
同时触发事件 `orvibohomebridge_temp_password_event`（自动化可直接在通知里用密码）。

### 管理

```yaml
# 删除（authorized_id 来自下发响应或查询）
action: orvibohomebridge.revoke_temp_password
data:
  device_id: w-77c139c4d27f4fa6a20e1f459849aa47
  authorized_id: 101

# 查询当前有效密码（含过期状态）
action: orvibohomebridge.list_temp_passwords
data:
  device_id: w-77c139c4d27f4fa6a20e1f459849aa47
```

**自动回收**：每 6 小时检查一次，已过期（结束时间到）或次数用尽的临时密码自动删除。
门锁设备下还有"临时密码"传感器，显示最近一次下发的密码及详情属性。

**自动化示例**（按门铃自动生成并通知）：

```yaml
triggers:
  - trigger: event
    event_type: orvibohomebridge_lock_event
    event_data:
      kind: ring
actions:
  - action: orvibohomebridge.grant_temp_password
    data:
      minutes: 30
      number: 1
      name: 门铃访客
    response_variable: temp
  - action: notify.mobile_app_phone
    data:
      title: 访客临时密码
      message: "访客临时密码：{{ temp.password }}，30 分钟内有效"
```

自动化示例（按用户过滤）：

```yaml
triggers:
  - trigger: event
    event_type: orvibohomebridge_lock_event
    event_data:
      kind: unlock
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.unlock_user_id == 2 }}"
actions:
  - action: notify.mobile_app_phone
    data:
      title: 开门通知
      message: >-
        {{ trigger.event.data.unlock_user_name
           | default('用户' ~ trigger.event.data.unlock_user_id) }} 开门了
```

## 📷 界面预览

### 智能晾衣机控制页面

![智能晾衣机控制页面](screenshots/clothes_horse.png)

## 🏗️ 工作原理

```
┌──────────────────┐       HTTPS        ┌─────────────────────┐
│   Config Flow     │◄──────────────────►│  Orvibo REST API    │
│   (配置发现)       │  OAuth + family    │  (port 443)         │
└─────────┬─────────┘                     └─────────────────────┘
          │
          │  配置完成后:
          ▼
┌──────────────────┐     TLS 1.2        ┌─────────────────────┐
│   Coordinator     │◄──────────────────►│  Orvibo Binary API  │
│   (状态推送 +      │   双向认证          │  (port 10002)       │
│    命令控制)       │   AES-ECB JSON     │                     │
└──────────────────┘                     └─────────────────────┘
```

1. 通过欧瑞博 REST API 发现区域服务器和家庭 ID
2. 通过双向 TLS 认证建立二进制协议长连接
3. 通过推送（SSL 通道上的 MQTT）实时接收设备状态更新
4. 按需发送控制命令

## 🏗️ 项目结构

```
orvibohomebridge/
├── custom_components/
│   └── orvibohomebridge/     # HACS 自定义集成
│       ├── __init__.py       # 集成入口，平台注册
│       ├── manifest.json     # 集成元数据
│       ├── config_flow.py    # 配置流程
│       ├── coordinator.py    # 数据协调器，状态管理
│       ├── const.py          # 常量定义
│       ├── device_types.py   # 设备分类
│       ├── https_client.py   # HTTP API 客户端
│       ├── ssl_client.py     # SSL 连接客户端
│       ├── packet.py         # 数据包构造
│       ├── functions.py      # 工具函数
│       ├── light.py          # 灯光平台
│       ├── cover.py          # 窗帘平台
│       ├── climate.py        # 空调平台
│       ├── switch.py         # 开关平台
│       ├── sensor.py         # 传感器平台
│       ├── binary_sensor.py  # 二元传感器平台
│       ├── fan.py            # 新风系统平台
│       └── certs/            # SSL 证书
├── hacs.json                 # HACS 配置
├── brand/                    # 品牌图标
├── screenshots/              # 界面截图
└── README.md
```

## 📝 协议说明

本项目通过以下方式与欧瑞博云服务通信：

1. **HTTP API**：获取设备列表、家庭信息、初始状态
2. **SSL 长连接**：实时接收设备状态推送和事件通知
3. **MQTT 推送**：通过 SSL 通道接收设备状态变化

## 🐛 已知问题

- 部分设备类型可能未完全支持

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

## 🙏 致谢

yecao@hassbian 提供coco插座插排测试

https://github.com/jzgods/ORVIBO_Device_Control

https://github.com/abb3421/orvibo_switch

https://github.com/kjanko/orvibo-homeassistant-curtains
