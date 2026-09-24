# 控制命令怎么逆向（最难的一步）

前面几篇文档讲的是"字段怎么填"。但有一件事**必须单独说清楚**：

> `readtable` + 状态推送能给你 **`match`** 和 **`state`**，
> **但给不了 `control`。**

控制命令是**官方 App 单向下发**的——设备收到后只上报新的状态，不回显"App 发了什么命令"。所以如果你只抓读表和推送，你能知道设备"是什么、现在怎样"，但不知道"该怎么命令它"。

## 为什么不能直接抓包

| 通道 | 加密 | 能不能被动嗅探 |
|---|---|---|
| 云端长连接（SSL 10002） | 全程 AES；HELLO 之后换成服务端下发的会话密钥（`packet.py` 的 `encrypt_payload`） | ❌ 抓到的是密文 |
| 网关 LAN（TCP 8088） | 同样走 `build_packet(..., key or session_key or DEFAULT_KEY)`，会话密钥握手后才得到（`lan/gateway_connection.py`） | ❌ 同样是密文 |
| App 自身的 HTTPS | 双向 TLS + 客户端证书，且 App 一般做证书固定 | ❌ MITM 基本被挡 |

三者都拿不到明文。所以"装个抓包工具看 App 发了什么"这条路，在这个协议上是走不通的。

## 两条可行路线

### 路线 A：实证验证（推荐，不需要 root/模拟器）

既然拿不到"App 发了什么"，就换个问法：**枚举候选命令，看哪一条真的让设备动了。**

判定依据**只能是设备上报的状态**——服务端 ACK 只说明云端收下了请求，不代表设备执行了。

仓库自带工具：

```text
# 0. 先确认状态推送链路是通的（不发送任何命令）
python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --observe-only --observe 15
#    这 15 秒内去 App 或物理开关上操作这台设备，看是否打印出推送

# 1. 实发候选命令，逐条判定
python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --intent on --observe 6
```

输出形如：

```
设备：客厅插座  platform=switch  动作=on  候选=8 条
  ❌ ineffective    order=on value1=0
  ❌ ineffective    order=on value1=0 value2=255
  ✅ verified       set property onoff.status=on
  ✅ verified       set property onoff_status=on
  ...
── 判定结果 ──────────────────────────────────
测试前基线：False

── 验证通过的控制块（可直接替换进 profile）──
  control:
    on:
      order: set property
      properties: {onoff: {status: on}}
```

判定有三种，含义严格区分：

| 判定 | 含义 |
|---|---|
| ✅ `verified` | 收到推送，且推送里确实变成了目标状态 —— **这条命令有效** |
| ❌ `ineffective` | 收到了推送，但状态不是目标值 —— 这条命令没用 |
| ❓ `inconclusive` | 没收到推送，或状态字段是裸 `0/1` 无法判断方向 —— **不算数，需要人工确认** |

工具**绝不会**因为服务端 ACK 就判 `verified`。

自动枚举的候选包括（两个方向都试，因为方向正是不知道的那个）：

- `order=on/off` × `value1 ∈ {0,1}`（active-low / active-high 两种约定）
- 带 `value2=255` 的变体（有些灯具开灯必须带满亮度）
- `set property {onoff: {status: on/off}}` 与 `onoff_status` 扁平写法
- 亮度：`move to level` / `fast move to level` / `on`+`value2` / `set property brightness.percent`，并且 0-255 与 0-100 两种量纲都试
- 色温：mired 与 Kelvin 两种编码
- 位置：`open` + `value1` 与 `set property percent`

### 路线 B：反编译 App 拿真实帧（最准，工作量大）

`_ref/` 下有多个版本的 jadx 反编译产物（`5.2.2.307`、`5.1.9.303` 等）。思路是在 App 里找到构造 `cmd=15` 报文的地方，看它是怎么按 deviceType 组装 `order` / `value1..4` / `properties` 的。

拿到帧之后，**不要直接相信它**——用路线 A 的工具验证：

```text
python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --intent on --payloads candidates.json
```

`candidates.json` 直接接受 `cmd=15` 的原始字段：

```json
[
  {"order": "on", "value1": 0, "value2": 255, "label": "App 反编译得到的开灯帧"},
  {"order": "set property", "properties": {"onoff": {"status": "on"}}, "label": "App 的属性帧"}
]
```

`order` 仍会被白名单校验（`on`/`off`/`open`/`stop`/`set property`/`move to level`/`fast move to level`/`fast color temperature`）；如果 App 用的是白名单之外的命令，说明这个能力**当前无法用 profile 表达**，需要提 PR 在集成里加传输方法，见 [05-custom-vs-upstream.md](05-custom-vs-upstream.md)。

## 完整工作流

```text
① 生成骨架（拿到 match + state，control 是占位骨架）
   python tools/generate_device_profile.py <账号> <密码> --device 客厅插座 --listen 90 --out my_plug.yaml

② 放进 HA，重载，确认设备被识别、状态会变（此时 hardware_verified: false，控制不会下发）

③ 验证控制命令
   python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --intent on --observe 6
   python tools/probe_device_control.py <账号> <密码> --device 客厅插座 --intent off  --observe 6
   # 需要的话再验 brightness / color_temp / position

④ 把 ③ 输出的 control 块替换进 ② 的 profile，并改成：
   hardware_verified: true

⑤ 重载集成，在 HA 里点一下开关，确认真机动作
```

第 ③ 步是关键：**没有它，profile 里的 `control:` 就只是猜的**，改 `hardware_verified: true` 也是拿未验证的命令去打真实设备。

## 安全须知

- **只操作你明确指定的那一台设备**：`--device` 是精确选择，工具不会碰其他设备。
- **风险类别会拦你**：门锁（522/107）、晾衣机（52）、窗帘/卷帘（34/35）等带电机的设备，工具会先要求确认（`--yes` 可跳过，仅用于非交互）。**门锁不要盲目枚举命令**——后果是真实开门/锁门。
- **每条候选之间有间隔**（`--gap`，默认 1s），避免连环触发。
- **先 `--observe-only`**：确认推送链路通了再发命令，否则你会把"收不到推送"误判成"命令无效"。
- 工具**只读取**设备列表和状态推送；只有 `--out` 会写你指定的文件。

## 常见困惑

**Q：`--observe-only` 期间操作设备，什么都没打印？**
推送链路不通，跟控制命令无关。常见原因：设备离线、账号区域选错（国际区要加 `--cloud-host homemate.orvibo.com`）、或在纯 LAN 模式下云端本来就没有这条设备的实时推送。

**Q：候选全都 `ineffective`？**
说明命令确实没被设备接受。先确认 `--observe` 够长（有些设备回推慢），再考虑这条路走不通 → 需要反编译 App 拿真实帧（路线 B）。

**Q：全都 `inconclusive`？**
状态字段是裸 `0/1`（`value1`），工具不敢判断方向。解决办法：手动在真机上开一次关一次，看 `value1` 分别是多少，就有答案了。

**Q：一条 intent 有多条 `verified`？**
有可能（服务端对多种写法都接受）。工具默认取枚举顺序里的第一条作为主选，其余会在输出里列为"备选"。

## 相关页面

- 先把 `match`/`state` 拿到：[06-finding-parameters.md](06-finding-parameters.md)
- 字段与 `order` 白名单：[01-schema.md](01-schema.md)
- 什么时候必须提 PR：[05-custom-vs-upstream.md](05-custom-vs-upstream.md)
