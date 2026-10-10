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
  **M1 实测修正**：这条路带 `readonly` 判定，会把引擎自己的写入也拒掉（回异常码 2），
  所以我们只用它读、不用它写，写走 `backend/raw_writer.py` 直接改寄存器列表（§8、§12）。
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
  **直接改 `SimRuntime.block[..][2]` 的寄存器列表**（`backend/raw_writer.py`；已实测不能用
  `SimCore.async_setValues`，它把 `readonly` 点上的引擎自写一起拒，见 §8/§12）→
  主站读时 pymodbus 直接出寄存器，不等 GUI
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
- `word_order`：取值 `ABCD`（默认）/ `CDAB` / `BADC` / `DCBA`。四种全支持的类型只有
  int32/uint32/float32；`string` **只支持 `ABCD`**（已实测调整：4 个字母只描述 4 字节置换，
  8 字节以上没有公认定义，与 64 位同理收窄）；int64/uint64/float64 只支持 `ABCD` 和 `DCBA`
  （§6）；单寄存器类型（bool/int16/uint16/reg16）本来就只有 2 字节，填 `ABCD` 不算错，
  填其它值只给一条 warning，不拦启动（§8）
- `waveform`：`const / ramp / sine / random / step`；`wave_param` 见下方波形参数表
- `readonly`：`true/false`，主站写该点返回异常码。**未识别的值（含空串、`TURE` 这种拼错的）
  一律启动前报错**，不静默当成可写——静默就是把"只读"的保护悄悄关掉。
  已实测：`SimData.readonly` 同时挡主站**和我们自己的引擎**（`SimCore.async_setValues`
  也回异常码 2），所以运行期写寄存器一律走"直接改 `SimRuntime.block[..][2]` 的列表"
  那条路（`backend/raw_writer.py`，见 §8），`readonly` 只用来对外撒谎成真设备的样子

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
占用的寄存器数由长度推出。**字序只支持 `ABCD`**（§5）：两两打包的顺序就是"先到的字节在高字节"，
给它配 `CDAB` 之类不是"另一种厂商接法"而是语义分歧，填了按启动前校验错误拦（§8）。

**已实测（M1）：`SimData.count` 是"重复倍数"，不是地址跨度。** 实际跨度 =
`len(values) × count`，写 `SimData(0, 10, [0]*10, REGISTERS)` 会占 100 个寄存器并把 20 号
点位判成重叠（pymodbus 直接 `TypeError: SimData address 20 is overlapping!`）。
→ 本项目**一律 `count=1`**，一个点位一条 `SimData`，占几个寄存器完全由值列表长度决定
（钉在 `tests/test_simdev.py::test_hr_simdata_carries_encoded_initial_and_count_is_one`）。

**64 位类型（int64/uint64/float64）只支持 `ABCD` 与 `DCBA`**：前者是 8 字节按大端原样切 reg，
后者是 **8 字节整体反转**后切 reg（不是"字序反转"——只反转 4 个 reg 的顺序等价于 `CDAB`，
是另一回事，实测值见 `tests/test_codec_64bit.py`）。业界对 64 位的 `CDAB`/`BADC` 没有公认
扩展定义（各家厂商含义不一致），我们不自己发明；CSV 里给 64 位类型填这两种值属启动前校验
错误（§8）。

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
- **已实现口径（M2，别再按布局图猜）**：报文过滤是 **4 个平铺可勾选标签**
  `全部/请求/响应/异常`（`gui/packet_view.py` 里**一个 `QComboBox` 都不许有**，换个地方塞下拉
  等于没改），默认 **"全部"**；三态（`○ 停止`/`● 运行`/`▲ 失败`）只换文字与颜色，
  控件矩形与 `isVisible()` 序列逐项恒定（`tests/test_gui_layout_invariant.py`、
  `tests/test_gui_packet_view.py` 各钉一份）；可伸缩区块不写死高度。
- **停止失败这一态**：`▲ 失败` 且**停止按钮保持可点**。`runner.call` 超时意味着引擎线程没回话，
  监听大概率还在应答，此时界面若显示 `○ 停止` 就是"说停了其实还在服务"——那是本项目最防的
  一类演出。口径：后端句柄留住、`running` 不动、再点一次"停止"能真的关掉
  （`tests/test_session.py::test_a_failed_stop_keeps_the_handle_so_the_next_stop_can_close_it`）。
- **回收（GC）归 GUI 线程**：入口关掉自动回收，破引用环只由主线程心跳按点做（每 5s 至多一次）。
  界面 + 引擎线程并存的工具里，让引擎线程去回收等于让它销毁界面丢弃的 widget，而它等的主线程
  正在等它回话——整个软件冻死（实测根因与证据在 §12）。这条不改任何可见行为，但决定了
  "点一下启动之后界面卡住"这类现象会不会再出现。
- **值归属规则（v1 唯一一条）**：每个点位有 `source = waveform | manual`。GUI 手动改值、
  或主站用 FC05/06/15/16 写入，都把该点切成 `manual`，波形引擎**不再覆盖**它，
  点位表波形列显示 `已停`，右键"恢复波形"可切回。这样测平台下发命令时不会出现
  "值被波形顶掉、误判下发没生效"。

## 8. 错误处理

三档，不混：

- **启动前校验，一次报全**：CSV 解析错误带行号；地址重叠（位区按位判、寄存器区按寄存器判，
  两边口径实测不同，见 §12）；类型与区不匹配（如 co 上放 float32）；`wave_param` 缺参或非法
  （`period=0`、`min>max`、类型不符）；`unit_id` 越界（只认 1..247，且不建 `id=0` 兜底设备）；
  `host` 不是非空字符串；**`port=0`**（"让系统随便给一个"在 M1 也算 error——要读回实际端口
  得从 pymodbus 内部摸 transport 对象，版本一变就碎）；**同一个 `(host, port)` 被两个通道
  占用**（不等启动第二阶段炸 bind：Windows 只回 10048 且不带通道名，现场看不出是点表配置错）；
  字序与数据类型冲突——**64 位类型填 `CDAB`/`BADC`、`string` 填非 `ABCD` 才是拦截项**
  （语义有分歧，不许猜），而单寄存器类型（bool/int16/uint16/reg16）填了 `ABCD` 之外的值
  只记一条 warning（§5）；端口被占（bind 阶段的 Windows 10048）；串口不存在或打不开。
  任一条不满足就**整个工程不启动**，把所有错误一次性列成清单，不做第一个错就崩。
- **运行期**：USB-485 被拔 → 该通道状态置错、树节点标红、日志一条，**不自动重连**
  （现场拔插常是人有意的，静默重连会让人误判设备在线），提供"重启通道"按钮。
  TCP 服务异常同样处理。通道置错后，已缓冲的报文仍可翻阅（不清空环形缓冲），便于看断前最后几条。
- **唯一保留的异常响应**：主站写 `readonly` 点 → 已实测返回**异常码 2（`ILLEGAL_ADDRESS`，
  与越界读同码）**。真实设备本来就会做这件事，不算注入。但同一个标志位**也挡我们自己的引擎**
  （`SimCore.async_setValues` 写只读点同样回 2 且值不变），所以运行期改寄存器一律不走
  `SimCore`，改由 `backend/raw_writer.py` **直接改 `SimRuntime.block[..][2]` 的寄存器列表**
  （`readonly` 因此只用于对外挡主站；用例：
  `tests/test_raw_writer.py::test_write_into_readonly_block_succeeds_for_engine_but_master_is_refused`、
  `tests/test_integration_tcp.py::test_readonly_point_blocks_master_but_not_engine`）。
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

1. **M1 地基（已完成）**：project 模型 + codec + TCP backend + 上述集成测试全绿。
   验收：命令行起一个从站，第三方主站（QModMaster / 平台网关）能读到值。
   落地在计划 `docs/superpowers/plans/2026-10-08-modslave-m1-kernel.md`、分支 `m1-kernel`，
   命令行入口 commit `c134474`；全量 `.venv/Scripts/python.exe -m pytest` **556 passed**
   （无 skip、无 xfail），其中 `tests/test_integration_tcp.py` 是真主站互测（八个功能码 +
   异常码 + 字序 + 多 unit + 总线扫描日志），`tests/test_cli.py` 是命令行入口验收。
   **还等人的只有人工那一条**：QModMaster 手连一遍计划 Task 12 Step 8 的 6 项。
2. **M2 GUI 最小可用（代码已全部落地，人工那两条清单还没跑）**：设备树 + 点位表 + 双击改值 +
   通道启停，外加报文监视窗与多通道独立启停（原本排在 M3 的两项一起做掉了，因为界面一次装齐
   比拆两次更省返工）。落地在计划 `docs/superpowers/plans/2026-10-08-modslave-m2-gui.md`，
   入口 `modslave/gui/app.py` + `python -m modslave <点表> --gui`。全量
   `.venv/Scripts/python.exe -m pytest` **773 passed in 63.27s**（无 skip、无 xfail，串行连跑 4 轮
   全绿）。此前那条"整轮挂死"的根因与修法见 §12/§13：自动 GC 在非 GUI 线程销毁界面丢弃的
   `QWidget`，与正等引擎回话的主线程互锁；现已定口径**回收只许发生在 GUI 线程**。
   等人的还是人工那两条：计划 Task 9 Step 2（真界面 6 项）与 Step 3（真界面 + 真主站）。
3. **M3 CSV 导入导出 + 报文监视窗 + 多通道独立启停**。
4. **M4 波形引擎**（const/ramp/sine/random/step）+ 点位属性面板 —— 纯逻辑、可全自动测，
   不依赖硬件，所以排在串口之前。
5. **M5 RTU backend**（串口；唯一需要真硬件冒烟的一段）。

已确认按 M1→M2→M3→M4→M5 顺序。

**实现计划范围**：第一份计划覆盖 M1–M4，交付一个 TCP 从站 + GUI + CSV + 报文监视 + 预设波形
的完整可用工具，全程可自动化验证。M5（RTU）单独出第二份计划，因为它需要你在笔记本 + USB-485
上做人工冒烟，混进第一份会让"全部通过"无从判定。

**顺序变更（已生效）**：里程碑定稿为 **M4 波形引擎、M5 RTU**；批次 3 里原本排的是 RTU=M4、
波形=M5。改过来的理由是波形不依赖硬件且是 v1 必须项、能全自动回归，RTU 必须真串口 +
人工冒烟——先做波形，硬件那条不挡在前面。本份 M1 计划与后续 M2–M5 都按这个顺序执行。

## 12. 待实测确认（按里程碑各自清零，不写进代码假设）

M1 内必须确认（**已全部清零，下列结论都有对应用例钉住，改行为前先改用例**）：

- **已实测：寄存器区的未定义地址与只读点都回异常码 2（`ILLEGAL_ADDRESS`）。**
  `DataType.REGISTERS` + 值列表 + `count=1` 下，点表里的地址空洞被 `SimRuntime.__create_block`
  填成 `DataType.INVALID`，主站读到就是 2；写 `readonly` 点同样是 2（不是别的码）。
  → **点表不需要把地址铺满**。证据：
  `tests/test_integration_tcp.py::test_undefined_register_address_returns_exception_2`、
  `::test_readonly_point_blocks_master_but_not_engine`，datastore 层同口径见
  `tests/test_simdev.py::test_empty_register_area_reads_illegal_address_at_datastore_layer`。
- **已实测：`SimCore.async_getValues/async_setValues` 的 `func_code` 走的就是
  `SimRuntime._fx_mapper` 那张表**（1/5/15→co、2→di、3/6/16→hr、4→ir），四区独立设备一样。
  但 **`readonly` 会连引擎自己的写入一起拒**：`async_setValues(11, 4, 0, [999])` 回 2 且值不变。
  → 运行期写寄存器**不走 `SimCore`**，改成直接改 `SimRuntime.block[..][2]` 的列表
  （`backend/raw_writer.py`），主站路径的 `readonly` 判定不受影响。证据：
  `tests/test_raw_writer.py::test_write_into_readonly_block_succeeds_for_engine_but_master_is_refused`、
  `func_code` 取值见 `tests/test_simdev.py`（fc=1/2/3/4/5 五种 `async_getValues`/
  `async_setValues` 断言，覆盖 co/di/hr/ir 四个区）。
- **已实测：位区与寄存器区的"空洞"行为不对称，且 pymodbus 禁止 co/di 出现非 `BITS` 条目。**
  同一 16 位寄存器覆盖下的未定义线圈**读得到（False）也写得进去（回 ACK）**，超出块才回 2；
  寄存器区则一律回 2。四区独立形态强制 `use_bit_addressing=True`，**主站按位号寻址**，
  我们不做 ×16 换算（与隧道现场点表一致）。空区必须补占位条目（pymodbus 不接受空 list），
  而位区占位只能是 `BITS` → **占位地址抬到高地址 65520**：放地址 0 实测会让一台压根没有线圈的
  假从站对外 ACK 线圈写（风机启动指令收到"成功"），比显示错值更糟。
  残余代价（位 65520..65535 对外算"已定义"）也被钉住了。证据：
  `tests/test_integration_tcp.py::test_bit_area_gap_is_readable_and_writable`、
  `::test_empty_bit_area_answers_exception_2_to_a_real_master`、
  `::test_placeholder_bit_window_is_the_pinned_residual_cost`，
  datastore 层见 `tests/test_simdev.py::test_empty_bit_area_reads_and_writes_illegal_address_at_datastore_layer`。
  重叠校验因此**位区按位判、寄存器区按寄存器判**。
- **已实测：未定义的 `unit_id` 回异常码 4（`DEVICE_FAILURE`）且服务不崩，但 pymodbus 会往
  `pymodbus.logging` 刷整段栈**（`SimCore.__get_device` 回落 `devices[0]` → `KeyError: 0`；
  一次 1..247 的总线扫描实测 492 行）。协议行为是对的，噪音在 backend 层用日志 filter 压掉
  （`configure_library_logging`）。**不建 `id=0` 兜底设备**：加了它总线扫描会变成 247 台全应答，
  现场看到的是一条根本不存在的总线。证据：
  `tests/test_integration_tcp.py::test_unknown_unit_id_answers_exception_4_and_server_survives`、
  `::test_bus_scan_does_not_flood_the_log`、`tests/test_simdev.py::test_no_id_zero_device`。

M2 内确认（**已全部清零**，探针 `m2-probe/qt_spike.py`，结论都有用例钉住）：

- **已实测：PySide6 6.9.2 在 venv 里可用，`QT_QPA_PLATFORM=offscreen` 下 `show()` +
  `processEvents()` 之后控件几何是真的**（900 宽、工具栏 27 高）。→ "状态变化不许挪布局"
  能写成断言，不需要真显示器，也不需要 pytest-qt。证据：
  `tests/test_gui_layout_invariant.py`、`tests/test_gui_packet_view.py::test_geometry_is_stable_across_filter_and_pause`。
- **已实测：跨线程 Qt signal 直接可用，不需要 qasync**——工作线程 `emit` 200 次，主线程收到
  200 次、顺序严格单调、`received on wrong thread: 0`。→ 引擎线程只往 `gui/bridge.py` 这一个
  "住在主线程的 QObject" 上 emit，投递交给 Qt 的 queued connection。
- **已实测：10Hz 全表刷新的成本不是瓶颈**——200/1000/5000 行模型上每次覆盖 200 行发
  `dataChanged`，最坏 **0.34ms**。→ 刻意不做虚拟滚动与增量 diff（YAGNI），刷新走**拉模型**
  （GUI 每秒问 10 次），不给 backend 加"主站写入"回调。
- **已实测：自动垃圾回收能把整个软件锁死**（本机 `py-spy --native` 抓到的栈，非推断）——
  引擎线程在分配时触发一次回收，回收链里有一个 GUI 线程刚丢弃的 `QWidget` 包装器，shiboken
  于是在**非 GUI 线程**上走 `~QWidget → QWindow::close() → flushWindowSystemEvents() →
  QWaitCondition::wait` 等主线程来收窗口事件；而主线程此刻正卡在 `runner.call(...)` 的
  `future.result(10)` 里等引擎回话。两边互等成环，症状不是"一条用例挂了"，而是整条 loop
  不再回 `_run_once`：心跳停、后面每条起停连片 10s 超时、`runner.stop()` 也失败
  （`loop.stop` 自己就是排进回调的）。
  → **口径：回收只许发生在 GUI 线程。** `gui/app.py` 起来就 `gc.collect(); gc.freeze();
  gc.disable()`，破引用环交给 `MainWindow._maybe_collect()`（心跳 10Hz，每 5s 至多一次）；
  测试侧同一口径（`tests/conftest.py` 关自动回收 + 每条用例结束在主线程 `gc.collect()`）。
  无头 CLI 不碰 Qt，不受影响。证据：`packet_view + integration_tcp` 这一对在回收开着时
  3/3 挂死，关掉后 3/3 跑完；`tests/test_gui_main_window.py::test_the_tick_is_the_only_reclaim_point_and_runs_on_the_gui_thread`
  钉"唯一回收口在 GUI 线程、且不是每跳一收"。
- **已实测：同进程攒多个 asyncio loop 会互相饿死**（每用例各建一对线程时攒到 24 个，表现为偶发
  `.result(10)` 与 `join(5)` 超时）。→ 测试会话共用一条 `EngineRunner`，全量从 45s 降到 21s；
  代价是"上一个用例的后端没关干净"会串到下一个，所以 `tests/conftest.py` 里那条
  监听泄漏检查器是必需的，不是可选的清洁工。

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
- **（已修）GUI 用例之后起从站会整轮冻死**：根因与修法见 §12 那条"已实测：自动垃圾回收能把
  整个软件锁死"。留这一条是为了记住**排查过程中错在哪**，别再照着旧结论改代码：
  ① 栈尾两次落在 `loop.create_server` / `asyncio.to_thread(check_port_is_free)`，看着像探针
  或监听建立慢，实际那只是"回收触发时它正在写哪行"；② `faulthandler` 给主线程标的
  `Garbage-collecting` 是它自己 dump 时触发的一次回收，不是证据；③ 我一度用"整会话 0 次回收"
  排除了 GC——那是**仪表坏了**：这台机器的 `gc.callbacks` 是 `cb(phase, info)` 两参数签名，
  按上游的单参数写，回调每次抛 `TypeError` 被解释器 "Exception ignored" 静默吃掉。
  教训：仪表先拿一次已知事件验准（`gc.collect()` 一次看回调进不进），再谈排除。
- 打包（PyInstaller onefile + pyserial/pymodbus hidden imports）推迟到 v1 功能稳定后，
  但 v1 不验证打包会留一个尾巴，届时单列里程碑。
