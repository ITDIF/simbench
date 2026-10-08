# Modbus 从站模拟器（modslave-sim）设计

日期：2026-10-08
状态：已与用户逐节确认（架构批次 1、点表与界面批次 2、错误处理/测试/里程碑批次 3）；
自审阶段有四处调整，需你复核：

1. `bits` 按位点位移出 v1（与"地址重叠即报错"冲突）——§2、§5、§6
2. 里程碑对调：波形引擎 M4、RTU M5；第一份实现计划覆盖 M1–M4 —— §11
3. 64 位类型的字序收窄为 ABCD/DCBA 两种 —— §5、§6、§8、§10
4. 界面里的"冻结刷新"改成值归属规则 `source = waveform | manual` —— §7

## 1. 目标

给隧道机电平台的南向联调提供一个桌面版 Modbus 从站模拟工具，角色类似 mThings：
在一台笔记本上起若干个假从站，让平台/网关当成真实设备来轮询和下发，用于验证采集、
控制、超时与脏数据路径。

首要用户是开发者本人（现场/机房联调）。次要交付目标是可拷贝到同事机器上运行。

## 2. 范围

v1 做：

- Modbus TCP 从站（多通道、多监听端口、单端口多 unit id）
- Modbus RTU 从站（串口，多从站挂同一条总线）
- 点表：CSV 导入为主，GUI 手工录入为辅；工程文件保存
- 数据类型与字序：int16/uint16/int32/uint32/int64/uint64/float32/float64/string/reg16
  × 字序 ABCD/CDAB/BADC/DCBA（字序仅多寄存器类型有效）
- 点位值：GUI 双击直接改，写入即生效到寄存器
- 预设波形：const / ramp / sine / random / step
- 报文监视窗（原始 hex + 解析摘要）、通道启停独立、计数与连接状态
- 交付：源码 + venv requirements.txt，M1–M5 期间不做 exe

v1 明确不做（接口留缝，见 §9）：

- 故障注入：响应延迟、不回包、异常码注入、CRC 错、断线重连模拟
- Python 表达式驱动点位
- 报文录制/回放
- 主站（客户端）角色
- 私有 TCP/串口协议
- 状态字按位编辑：v1 用 `reg16` 手填整值，不做"一个寄存器拆成 16 个按位点位"
  （纯界面增量，见 §9；拆位会与"地址重叠即报错"的校验规则冲突）

## 3. 技术前提（已实测核对，非推测）

依赖：`pymodbus==3.15.0`（2026-08-13 发布，requires-python >=3.10，classifier 覆盖 3.10–3.14，
除 pyserial 外无其他依赖）+ `PySide6==6.9.2`（本机已装，Python 3.13.9）。

以下事实来自把 wheel 下载到临时目录读源码，**未安装进本机环境**：

- `pymodbus.server.__init__` 导出 `ModbusTcpServer` / `ModbusSerialServer` /
  `ModbusTlsServer` / `ModbusUdpServer`、`StartAsyncTcpServer` 等，以及 `ModbusSimulatorServer`。
- `ModbusBaseServer.__init__` 的 `context` 参数类型是
  `ModbusServerContext | SimDevice | list[SimDevice]`；若非 `ModbusServerContext`，
  内部直接 `SimCore(context)`（`server/base.py:49-53`）。
- **`ModbusServerContext`、`ModbusDeviceContext`、`ModbusSequentialDataBlock`、
  `ModbusSparseDataBlock` 均已 deprecated，v4 将移除**（构造时 `Log.warning`），
  且它们的 `async_getValues/async_setValues` 对非 `ModbusSimulatorContext` 一律返回
  `ExcCodes.DEVICE_BUSY`（`datastore/context.py:139-172`）。
  → 结论：**点表模型必须映射到 `SimData` / `SimDevice`**，不碰 deprecated 路径。
- `SimData` 字段：`address, count, values, datatype, string_encoding, readonly`
  （`simulator/simdata.py:108-137`）。
- `SimDevice` 字段：`id, simdata, use_bit_addressing, identity, action`（`simulator/simdevice.py:25+`）。
  `simdata` 可为共享块 `list[SimData]`，或四区独立
  `(<coils>, <discrete inputs>, <holding>, <input>)`。
- `SimDevice.action` 是 async 回调，签名
  `(function_code, start_address, address, count, current_registers, set_values) -> ExcCodes | None`，
  可就地修改 `current_registers`、改写 `set_values`、返回异常码（`simdevice.py` 字段注释）。
  → 结论：将来做故障注入不需要改库，接一个 `action` 即可。
- `ModbusBaseServer` 提供 `trace_packet: Callable[[bool, bytes], bytes] | None`、
  `trace_pdu`、`trace_connect` 三个钩子（`server/base.py:34-36`），并在
  `callback_new_connection()` 里传给 `ServerRequestHandler`（`server/base.py:72-74`）。
  → 结论：报文监视窗有官方挂点，不 fork 库。
- `SimCore` 暴露 `async_getValues/async_setValues(device_id, func_code, address, count)`
  （`simulator/simcore.py:45-67`）——这是运行期改寄存器的入口。
- `DataType` 的字序不可配：`SimUtils.bytesToRegisters` 硬用 `int.from_bytes(..., "big")`
  （`simulator/simutils.py:117-124`），我在 3.15.0 源码里没找到任何 word/byte swap 选项。
  → 结论：字序转换自己实现（§6）；寄存器区（hr/ir）一律按原始 reg16 数组喂 pymodbus，
  位区（co/di）按 bool 列表喂，pymodbus 的 DataType 类型转换一律不参与取值——所以即便
  后续版本加了字序选项，我们也不依赖它。

环境与依赖：本机是 Anaconda 全局 Python 3.13.9（已装 PySide6 6.9.2，**未装 pymodbus /
pyserial**）。工程建独立 venv，`requirements.txt`：

```
pymodbus==3.15.0
pyserial>=3.5
PySide6==6.9.2
pytest
pytest-asyncio
```

M1 第一步是建 venv + 装依赖 + 跑一个最小 TCP 从站脚本确认 pymodbus 起得来，再动模型代码。
exe 打包（PyInstaller）不在 v1，风险见 §13。

## 4. 架构

```
gui/        PySide6：设备树 + 点位表 + 报文窗 + 启停控制 + CSV/工程对话框
engine/     SlaveEngine：唯一对 GUI 暴露的门面（命令队列 + 事件回调）
backend/    Backend 抽象 + ModbusTcpBackend / ModbusRtuBackend（pymodbus 只出现在这里）
codec/      PointCodec：点位值 ↔ 原始寄存器序列（数据类型 × 字序）
project/    数据模型（Project/Channel/Device/Point）+ project.json + CSV 读写 + 校验
```

依赖方向单向：`gui → engine → backend → project/codec`。`codec` 与 `project` 不 import
`pymodbus`，也不 import `PySide6`；`backend` 不 import `PySide6`。

线程模型：Qt 主线程 + 一个引擎线程（内部 asyncio loop）。

- GUI → 引擎：命令队列（`start_channel / stop_channel / set_point / load_project`）
- 引擎 → GUI：Qt signal（`packet_event / point_changed / channel_state / validation_error`）
  通过 queued connection 跨线程投递
- 不用 qasync：少一个依赖兼容风险，且引擎可脱离 Qt 用 pytest 跑
- GUI 永不直接接触 pymodbus 对象

关键接口：

- `SlaveEngine.start(project) / stop() / set_point(ch, unit, area, addr, value) / snapshot()`
- `Backend.build(devices: list[SimDevice]) -> context` / `serve()` / `stop()`
- `PointCodec.encode(value) -> list[int]` / `decode(registers: list[int]) -> value`
- `Waveform.next(t: float) -> value`（纯函数式，可注入时间戳做确定性测试），注册表按名字装配

数据流：

- 录入：CSV/GUI → `project` 模型 → `validate()` → 生成 `SimDevice` 列表 → `Backend`
- 运行：引擎 tick（默认 100ms）→ `Waveform` 算值 → `PointCodec.encode` →
  `SimCore.async_setValues` → 主站读时 pymodbus 直接出寄存器，不等 GUI
- 监视：`trace_packet(is_outgoing, bytes)` → `PacketEvent` → 环形缓冲（5000 条）→ 报文窗

## 5. 数据模型与 CSV

CSV 固定表头，一行一个点位：

```
channel, device, unit_id, name, area, address, datatype,
word_order, initial, waveform, wave_param, readonly
```

- `channel`：通道名。TCP 通道 = 一个 host:port 监听；RTU 通道 = 一个串口
- `unit_id`：1–247。同 `channel + unit_id` 的行合并为一个从站的点位；重复地址在校验阶段报错，
  **不静默覆盖**
- `area`：`co / di / hr / ir`
- `address`：**裸协议地址，0-based**。不做 40001 偏置。厂商点表给的是 4x 形式时，
  用导入对话框的"整体偏移"输入框处理，模型里不藏隐式规则；点位表另开一列显示 `4x/3x/1x/0x`
  惯例形式便于对照
- `datatype`：`bool int16 uint16 int32 uint32 int64 uint64 float32 float64 string reg16`
- `word_order`：取值 `ABCD`（默认）/ `CDAB` / `BADC` / `DCBA`。四种全支持的类型是
  int32/uint32/float32/string；int64/uint64/float64 只支持 `ABCD` 和 `DCBA`（§6）；
  单寄存器类型（bool/int16/uint16/reg16）该列填了也忽略，校验时给一条 warning 不报错
- `waveform`：`const / ramp / sine / random / step`；`wave_param` 见下方波形参数表
- `readonly`：`true/false`，主站写该点返回异常码

波形参数（`wave_param`，`k=v;k=v`；`t` 一律为引擎启动后的单调时钟秒，避免系统时间跳变影响曲线）：

| 波形 | 必填 | 选填（默认值） | 取值 |
|---|---|---|---|
| `const` | `value` | — | 恒等于 value |
| `ramp` | `from,to,speed` | `hold=0`（到边界后停留秒数） | `speed` 单位=数值/秒，到边界反向（与 tick_ms 无关） |
| `sine` | `base,amp,period` | `phase=0` | `base + amp*sin(2π(t/period)+phase)` |
| `random` | `min,max` | `interval=1.0`（秒，换值间隔）, `seed=空`(不填则不重复) | 均匀分布，按 interval 换值，其余 tick 保持 |
| `step` | `values,interval` | — | 逗号分隔序列按 interval 轮播，循环 |

参数缺失、类型不符、`period=0`、`min>max` 都归入启动前校验错误（§8），不静默取默认。

寄存器区形态：**四区独立** `SimDevice(simdata=(co, di, hr, ir))`，不使用共享块 +
`use_bit_addressing`。理由：隧道设备点表按四区给，共享块的位编址（`addr*16+bit`）与现场点表
对不上，是纯粹的坑。

工程文件 `project.json`（运行态唯一真相，与 CSV 双向导出）：

```json
{
  "version": 1,
  "tick_ms": 100,
  "channels": [
    {
      "name": "tcp-502", "type": "tcp", "host": "0.0.0.0", "port": 502,
      "devices": [
        {"name": "COVI-1", "unit_id": 11,
         "points": [{"name":"CO","area":"hr","address":0,"datatype":"float32",
                      "word_order":"CDAB","initial":"12.5","waveform":"sine",
                      "wave_param":"base=20;amp=15;period=60","readonly":true}]}
      ]
    },
    {
      "name": "com-485-1", "type": "rtu", "port": "COM3", "baudrate": 9600,
      "parity": "E", "bytesize": 8, "stopbits": 1, "timeout": 0.3,
      "devices": []
    }
  ]
}
```

## 6. 字序与编码

`PointCodec` 自研，40 行左右，`struct` + 置换。规则分三步：

1. 值先用 `struct.pack(">...")` 得到大端字节串，记作 `A B C D`（A 是最高有效字节）；
2. 按下表对**字节串整体**做置换；
3. 相邻两字节拼成一个 reg 的 16 位数值（`r0 = 前两字节`）。

协议固定的是"一个 reg 上线时高字节在前"，本节改的是**哪些字节组成哪个 reg**，两者不冲突——
下面表里的 `BA DC` 意思是 reg0 的数值等于 `0xBADC`（字节序被打乱的那个值）。

| 字序 | int32/float32 置换（4 字节 ABCD → reg 序列） |
|---|---|
| ABCD | AB CD |
| CDAB | CD AB |
| BADC | BA DC |
| DCBA | DC BA |

`string` 按 ASCII 两两打包（v1 只支持 ASCII，GBK 等多字节编码留给私有协议阶段），
`count` 由长度推出。

**64 位类型（int64/uint64/float64）只支持 `ABCD` 与 `DCBA`**：前者是 8 字节按大端原样切 reg，
后者是 8 字节整体反转后切 reg。业界对 64 位的 `CDAB`/`BADC` 没有公认扩展定义（各家厂商含义
不一致），我们不自己发明；CSV 里给 64 位类型填这两种值属启动前校验错误（§8）。

v1 不做按位点位：一个寄存器就是一个点位，状态字手填整值（见 §2）。

hr/ir 一律以**原始 reg16 数组**写入 `SimCore`（`DataType.REGISTERS`），不让 pymodbus 的类型
转换参与字序，避免与它的硬编码大端打架；co/di 以 `list[bool]` 写入，不经 codec 的字序置换。

## 7. 界面

```
┌ 工具栏: 全部启动/停止 │ 导入CSV │ 导出CSV │ 打开/保存工程 ┐
├────────┬──────────────────────────┴────────────────────┤
│ 设备树  │ 点位表（实时值，双击直接改；改值立即写进寄存器）  │
│ 通道→从站│ 名称|区|地址|4x|类型|字序|当前值|原始寄存器|波形    │
├────────┼────────────────────────────────────────────────┤
│ 通道属性 │ [标签页] 报文监视(hex+解析) │ 日志 │ 计数        │
└────────┴────────────────────────────────────────────────┘
```

- 通道可独立启停（现场常常只接了一路 485）
- 报文窗：环形缓冲 5000 条 + 暂停滚动按钮
- **值归属规则（v1 唯一一条）**：每个点位有 `source = waveform | manual`。GUI 手动改值、
  或主站用 FC05/06/15/16 写入，都把该点切成 `manual`，波形引擎**不再覆盖**它，
  点位表波形列显示 `已停`，右键"恢复波形"可切回。这样测平台下发命令时不会出现
  "值被波形顶掉、误判下发没生效"。

## 8. 错误处理

三档，不混：

- **启动前校验，一次报全**：CSV 解析错误带行号；地址重叠；类型与区不匹配（如 co 上放 float32）；
  `wave_param` 缺参或非法（`period=0`、`min>max`、类型不符）；64 位类型填 `CDAB`/`BADC`（§6）；
  端口被占（Windows 10048）；
  串口不存在或打不开；`unit_id` 越界。
  任一条不满足就**整个工程不启动**，把所有错误一次性列成清单，不做第一个错就崩。
  注：单寄存器类型填了 `word_order` 只记 warning，不属拦截项（§5）。
- **运行期**：USB-485 被拔 → 该通道状态置错、树节点标红、日志一条，**不自动重连**
  （现场拔插常是人有意的，静默重连会让人误判设备在线），提供"重启通道"按钮。
  TCP 服务异常同样处理。通道置错后，已缓冲的报文仍可翻阅（不清空环形缓冲），便于看断前最后几条。
- **唯一保留的异常响应**：主站写 `readonly` 点 → 返回异常码（`SimData.readonly` /
  `RunTimeFlag_READONLY`，v1 实测确认可用）。真实设备本来就会做这件事，不算注入。
- 波形算出 NaN / 超 datatype 范围 → 钳位到边界 + 只记一条日志（同点同类错误 60s 内不重复刷屏）。

## 9. 留缝位置（将来加 v2 功能不推翻结构）

| 将来的需求 | 现在的缝 |
|---|---|
| 报文监视、坏 CRC、延迟 | `trace_packet` / `trace_pdu`（Backend 已透传） |
| 异常码/超时/值改写注入 | `SimDevice.action`（Backend 建 SimDevice 时已留参数位） |
| 私有功能码 | `custom_pdu` |
| 私有 TCP/串口协议 | `Backend` 抽象 + `project` 模型不含 Modbus 专有字段 |
| 表达式驱动点位 | `Waveform` 注册表按名字装配，新增一种实现即可 |

## 10. 测试策略

- **单元**（不开 Qt、不占端口、无硬件）：
  `PointCodec` 往返覆盖 11 种 datatype：int32/uint32/float32/string 各跑 4 种 word_order，
  int64/uint64/float64 各跑 ABCD 与 DCBA 两种，bool/int16/uint16/reg16 各跑一次；
  边界值取 0、-1、类型最大/最小、
  超范围值（必须被拒并给出点位名）；地址重叠检测；CSV → 模型 → project.json 往返一致；
  波形注入固定时间戳做纯函数断言（含 §5 表中每个默认值分支）。
- **集成**（核心，等于把"拿第三方主站互测"做成自动回归）：
  pytest 内在 `127.0.0.1` 随机端口起真实 TCP 从站，用 pymodbus **client 当真实主站**跑
  01/02/03/04/05/06/15/16，并断言：正常读写值一致、越界读返回异常码、写只读点返回异常码、
  float32 在 CDAB 字序下读回数值正确、多 unit_id 路由正确。
- **信号契约**：引擎 → GUI 的事件对象有 schema 测试，保证引擎侧单独跑时事件不缺字段。
- **GUI**：不做 UI 自动化（YAGNI）。
- **RTU**：端到端测不了（需真串口）。走 pymodbus 现成 `ModbusSerialServer`，
  交付时明确标注"需在笔记本 + USB-485 做一次人工冒烟"，不假称已验证。

## 11. 里程碑

每个里程碑都能单独跑起来。

1. **M1 地基**：project 模型 + codec + TCP backend + 上述集成测试全绿。
   验收：命令行起一个从站，第三方主站（QModMaster / 平台网关）能读到值。
2. **M2 GUI 最小可用**：设备树 + 点位表 + 双击改值 + 通道启停。
3. **M3 CSV 导入导出 + 报文监视窗 + 多通道独立启停**。
4. **M4 波形引擎**（const/ramp/sine/random/step）+ 点位属性面板 —— 纯逻辑、可全自动测，
   不依赖硬件，所以排在串口之前。
5. **M5 RTU backend**（串口；唯一需要真硬件冒烟的一段）。

已确认按 M1→M2→M3→M4→M5 顺序。

**实现计划范围**：第一份计划覆盖 M1–M4，交付一个 TCP 从站 + GUI + CSV + 报文监视 + 预设波形
的完整可用工具，全程可自动化验证。M5（RTU）单独出第二份计划，因为它需要你在笔记本 + USB-485
上做人工冒烟，混进第一份会让"全部通过"无从判定。

> 自审调整：本条顺序在批次 3 里原本是 RTU=M4、波形=M5。理由改了——波形不依赖硬件且是 v1
> 必须项，RTU 依赖硬件，所以先做波形。

## 12. 待实测确认（按里程碑各自清零，不写进代码假设）

M1 内必须确认：

- `DataType.REGISTERS` + `readonly` + `SimData(count=N)` 组合下，越界读返回的异常码是否为
  `ILLEGAL_ADDRESS`(0x02)，写只读点返回哪个码；
- `SimCore.async_setValues` 写入四区独立设备时的 `func_code` 取值约定（用 1/2/3/4 还是
  5/6/15/16 —— `ModbusDeviceContext._fx_mapper` 显示 1/5/15→co、2→di、3/6/16→hr、4→ir，
  需实测确认 SimRuntime 走的是同一张表）；
- co/di 四区独立模式下"每个寄存器代表一个线圈"的寻址口径是否与现场点表一致；
- 未定义的地址段是否真的产生 ExceptionResponse（`SimDevice` 文档说"addresses not defined
  are invalid"，这直接决定我们要不要把地址表铺满）。

M5 内确认：

- `ModbusSerialServer` 的 `trace_packet` 是否同样生效（源码看是继承自 base，但需实测）；
- 拔 USB-485 后 pymodbus 抛的是哪类异常，好让"通道置错"接得住。

## 13. 风险

- **pymodbus 版本节奏快**（3.12.0 2026-02-08 → 3.15.0 2026-08-13，半年 6 个版本，
  且 v4 将删掉 deprecated datastore）。
  缓解：`requirements.txt` 锁 `pymodbus==3.15.0`，backend 层是唯一 import 点，升版只改一处。
- **模拟器自身 bug 会被当成平台 bug 调试**。缓解：M1 的第三方主站互测是硬门禁，
  不通过不进 M2。
- Windows 防火墙会拦截首次监听 502 端口（需放行或改用高位端口）。
- 打包（PyInstaller onefile + pyserial/pymodbus hidden imports）推迟到 v1 功能稳定后，
  但 v1 不验证打包会留一个尾巴，届时单列里程碑。
