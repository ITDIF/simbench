# Modbus 从站模拟器（modslave-sim）设计

> 本文与 `plans/` 下两份计划里的 `modslave-sim`/`modslave` 是定稿时的历史称谓，保留不改；
> 项目已于 2026-10-12 更名 `simbench`（用户可见名「设备模拟台」），代码包名即 `simbench`。

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
- 点表：CSV 导入为主；GUI 里既能改（双击改值、属性面板改地址/类型/波形），也能**从零搭**
  （新建工程 + 增删通道/设备/点位，2026-10-13 补，见 §7）；工程文件保存
- 数据类型与字序：int16/uint16/int32/uint32/int64/uint64/float32/float64/string/reg16
  × 字序 ABCD/CDAB/BADC/DCBA（字序仅多寄存器类型有效）
- 点位值：GUI 双击直接改，写入即生效到寄存器
- 预设波形：const / ramp / sine / random / step
- 报文监视窗（原始 hex + 解析摘要）、通道启停独立、计数与连接状态
- 交付：源码 + venv requirements.txt；打包（PyInstaller）在 v1 之后单列里程碑做完了（§11 第 8 条）

v1 明确不做（接口留缝，见 §9）：

- 故障注入：响应延迟、不回包、异常码注入、CRC 错、断线重连模拟
- Python 表达式驱动点位
- 报文录制/回放
- 主站（客户端）角色
- 私有 TCP/串口协议
- **S7（ISO-on-TCP / S7comm）通道**：v1 之后作为独立里程碑补做（2026-10-13，见 §11 第 7 条）。
  点表用现有 12 列表达 S7 的区/地址/类型，协议栈用 snap7 的原生服务端（`snap7.dll`，
  随 `python-snap7==1.3` 的 wheel 来，LGPL-3.0）
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
exe 打包（PyInstaller）不在 v1，作为独立里程碑在 v1 之后做完（§11 第 8 条、§13 最后一条）。

## 4. 架构

```
gui/        PySide6：设备树 + 点位表 + 报文窗 + 启停控制 + CSV/工程对话框；
            2026-10-15 拆分：`main_window.py`（组装/布局/心跳）+ `window_menus.py` / `window_edit.py` /
            `window_io.py`（三个 mixin）+ `window_consts.py`（常量）+ `window_common.py`（小纯函数）
            + `icons.py`（图标现画）+ `channel_state.py`（通道四态的符号 / 中文，**一处定义**）
            + `deps.py`（与外界的那几个接缝：会弹框的对话框、端口探针）
engine/     Session：唯一对 GUI 暴露的门面（启停/改值/拉取行 + 事件订阅），线程由 EngineRunner 持有；
            2026-10-15 拆分：`session.py`（生命周期/索引/事件）+ `reads.py`（取数）+ `writes.py`（写入/接管/恢复）
            + `records.py`（Row/WriteResult/ChannelRuntime）+ `render.py`（文案与小判定）
            + `session_host.py`（两个 mixin 的宿主契约，**只给类型检查器看**，运行时不加行为）
backend/    三条传输的后端：ModbusTcpBackend / ModbusRtuBackend（pymodbus）与 S7Backend（snap7）；
            与传输无关的那一半（波形 tick、值归属、报文/日志钩子、启停顺序）在
            `base.py` 的 `BackendBase`（**不 import pymodbus/snap7**），
            子类只填探针/建 server/等起来/切帧解析/数据面这几个钩子
codec/      point_codec：点位值 ↔ 原始寄存器序列（数据类型 × 字序）；
            s7_codec：S7 点位值 ↔ 字节序列（大端、无字序、bool 一位）
project/    数据模型（Project/Channel/Device/Point）+ project.json + CSV 读写 + 校验
net/        系统网络层（网卡现状/差异/专用网卡/设备 IP）；唯一允许跑 PowerShell 与 netsh 的包（§14）
根下        横切与入口：`events.py`（engine 与 GUI 之间的事件契约，2026-10-16 从 `backend/` 挪出）、
            `swallow.py`（吞异常的唯一出口）、`settings.py`、`diagnostics.py`、`csv_io_entry.py`、`__main__.py`
```

依赖方向单向：`gui → engine → backend → project/codec`，`net` 是旁路（谁都可以调它，它
不 import `pymodbus`/`snap7`/`PySide6`，也不 import `engine`/`gui`）。`codec` 与 `project` 不
import `pymodbus`/`snap7`，也不 import `PySide6`；`backend` 不 import `PySide6`；`subprocess`
只许出现在 `net/`。

**门面约束（2026-10-15 起，2026-10-16 收紧，`tests/test_architecture.py` 守着）**：`engine/` 只许从 `simbench.backend` 门面（再导出的 `make_backend` / `BackendBase` / `check_port_is_free` / `BackendStartError` 等）取东西，不许直接 import `base` / `modbus_tcp` 等实现文件；要对外新增一个名字，先在门面里再导出一行。

**`gui/` 连门面都不许碰**（同一批收紧）：它对 `backend` 的引用数是 0。原先做不到——事件的纯数据类住在 `backend/events.py`（产出方是 backend、消费方是 engine 与 gui），于是这条线必须为它开个口子；现在那几类挪到了根上的 `simbench/events.py`（与 `swallow.py` 同级），口子封掉。

**类型检查的覆盖范围（2026-10-16 扩）**：`mypy` 覆盖 `project` / `codec` / `engine` / `backend` / `master` 五棵子树，外加根下那几个模块（`events.py` / `swallow.py` / `settings.py` / `diagnostics.py` / `csv_io_entry.py` / `__main__.py` / `__about__.py`）。先前只有前三棵，于是 engine 被检查、而它调用的 backend 契约（`make_backend()` 的返回值、`BackendBase` 的钩子签名）不在闸门内——边界单边可信。补齐的成本实测是 15 处报错，无一处需要 `# type: ignore`。仍在闸门外的是 `gui/`（Qt 的动态用法）与 `net/`（ctypes + Windows 结构体）。

线程模型（**2026-10-13 全面审查时按落地形状改写**；此前这一节写的是目标形状，"命令队列"
与 `packet_event / validation_error` 这几个名字在实际代码里都不存在）：Qt 主线程 + 一个引擎
线程（`engine/runner.EngineRunner` 持有 asyncio loop）。

- GUI → 引擎：`EngineRunner.call(fn, timeout, what)`。GUI 能走到的每条等待都走它（不裸用
  `submit(...).result()`），因为超时会把引擎线程当时的栈帧一起写进异常；写侧的状态改动
  （接管/恢复、点值写入）全部经它投回 loop 线程——运行态只有一条线程碰（§13 那条 GC 死锁的
  教训）。
- **回读也只在 loop 线程碰运行态**（2026-10-13 补）：`Session.rows()` / `manual_count()` 在
  跑着时先让引擎线程取一次**整批快照**（寄存器 + 归属），再在 GUI 侧渲染。原来它逐个点直读
  后端缓冲，属于跨线程直读：单个 int 安全，但 float32/uint64 这种跨两格的值在两格之间被 tick
  改写时，界面上会出现一帧"谁都没写过"的数。引擎不回话时整批标 `stale`、**只报一次**错、
  `manual_count` 回 `None`（界面宁可不说那个数，也不报一个假的 0）。
- 引擎 → GUI：`Session.subscribe(sink)`（sink 在 **loop 线程**被调用，只准 emit）+
  `gui/bridge.py` 的 `EngineBridge` 四个 Qt signal（`packet / point_changed / channel_state /
  logged`）→ queued connection 投到主线程。**没有"命令队列"这个对象**，也没有
  `validation_error` 这个 signal：校验清单由调用方自己问 `validate_project()` /
  `Session.issues()`。
- 刷新是**拉的**：引擎不推点位值，GUI 以 10Hz 心跳调 `Session.rows(ch)`（M2 定的口径，实测
  200 行一次 `dataChanged` 0.34ms）
- 不用 qasync：少一个依赖兼容风险，且引擎可脱离 Qt 用 pytest 跑
- GUI 永不直接接触 pymodbus 对象

关键接口：

- `Session.load(project, source=...) / start_channel(name) / stop_channel(name) /
  set_point(ch, unit, area, addr, value) / rows(ch) / restore_waveform(...) / manual_count(ch) /
  issues() / mark_dirty() / subscribe(sink) / shutdown()`（`Session` 是唯一对 GUI 暴露的门面）
- `build_sim_devices(project) -> list[SimDevice]`（`backend/simdev.py`）；一个后端只服务
  一路通道，所以调用方传的是 `Project(channels=[channel])`
- **`backend.make_backend(channel, packet_sink, on_connection, tick_ms, log_sink,
  extra_endpoints)`** 是唯一按 `channel_transport()` 选后端的地方；两条传输共用的那一半
  （波形 tick、值归属、报文/日志钩子、启停顺序）在 `backend/base.py` 的
  `BackendBase`（**不 import pymodbus**），两个子类 `ModbusTcpBackend` /
  `ModbusRtuBackend` 各自只填五个钩子（探针 / 建 server / 等起来 / 切帧 / 解析），
  对外都是 `start()` / `stop()` 两个协程
- `codec/point_codec.encode(value, datatype, word_order) -> list[int]` /
  `decode(registers, datatype, word_order) -> value`（`PointCodec` 这个类不存在，是一组模块函数）
- 波形（`codec/waveform.py`）：`waveform_value(name, params, t) -> value` 纯函数（可注入时间戳
  做确定性测试），注册表 `WAVE_BUILDERS` 按名字装配；`clamp_to_datatype(value, datatype) ->
  (value, clamped)` 负责把值钳进类型区间
- 网络（`net/`，见 §14）：`plan_addresses(adapter, desired, present, owned) -> NetPlan` 与
  `foreign_owners(desired, others)` 是"要不要动系统"的唯一判据（纯函数）；
  `startup.prepare(point_table, apply, project)` / `startup.status(point_table, project)` 是
  启动挂钩与网络页的入口；`endpoints.for_channel(channel, point_table)` 算这一路要额外监听
  哪些地址；`ModbusTcpBackend(channel, …, extra_endpoints=…)` 把它们挂上

数据流：

- 录入：CSV/GUI → `project` 模型 → `validate()` → 生成 `SimDevice` 列表 → `Backend`
- 运行：backend 的 tick（默认 100ms，`backend/base.py` 的 `_tick_loop`/`_tick_once`，
  两条传输共用一份；不在 `engine/`，为的是无头 CLI 也驱动曲线）→ `waveform_value` 算值 → `clamp_to_datatype`
  钳位 → `PointCodec.encode` →
  **直接改 `SimRuntime.block[..][2]` 的寄存器列表**（`backend/raw_writer.py`；已实测不能用
  `SimCore.async_setValues`，它把 `readonly` 点上的引擎自写一起拒，见 §8/§12）→
  归 `manual` 的点整轮跳过（值归属表在 `backend/ownership.py`：`SourceStore`/`write_targets`/
  `LogDedupe`，见 §7）→ 主站读时 pymodbus 直接出寄存器，不等 GUI
- 监视：`trace_packet(is_outgoing, bytes)` → `PacketEvent` → 环形缓冲（5000 条）→ 报文窗。
  钩子拿到的是**到达的字节块**，一个块里可能挤了几帧；切帧按传输各一份（TCP 按 MBAP 的
  length、RTU 按 单元号+PDU+CRC，见 `_split_frames`），值归属要靠它认出主站在写哪个点
- 网络：点表旁的 `x.net.json` → `net up`（提权）把设备 IP 配到专用网卡 → 同一份声明交给
  `endpoints.for_channel`（**只对 TCP 通道**：串口没有"设备 IP:端口"这回事，回空）→
  backend 在主监听之外多挂那几个 `(ip, port)` 监听（共用一份
  datastore）→ 主站按 `设备IP:端口` 连上，读到的就是那台模拟设备（§14）

## 5. 数据模型与 CSV

CSV 固定表头，一行一个点位：

```
channel, device, unit_id, name, area, address, datatype,
word_order, initial, waveform, wave_param, readonly
```

- `channel`：通道名。TCP 通道 = 一个 host:port 监听；RTU 通道 = 一个串口。
  CSV 的表头是固定的 12 列、**没有串口列**，所以串口通道用**通道名本身**表述：
  `通道名@串口名[ 波特率[ 帧格式]]`——`com-485-1@COM3`、`com-485-1@COM3 19200`、
  `com-485-1@COM3 19200 8E1`（段之间是**空格**：逗号在 CSV 里会让整格必须加引号，
  Excel 里手改一格就会多出一对引号）。不带 `@` 的一律按 TCP 处理（并保留"按尾数猜端口
  + 记一条 note"的既有口径）。因此 `@` 是**保留字符**：通道名里不许出现它，
  串口名里不许出现它或空格（§8 两条判据），否则"导出再导入"会解析成另一路设备。
  导出时把参数按同一语法烘回名字里（全默认时只写 `com-485-1@COM3`），
  **CSV 往返不丢串口参数**；`@`、波特率与帧格式的解析/渲染各只此一份
  （`project/csv_io.parse_serial_name` / `_csv_channel_name`），界面不另抄一套。
- **TCP 通道也写进通道名（2026-10-13 补）**：`name@tcp:host:port`（例
  `tcp-5020@tcp:0.0.0.0:5020`）。为什么带一个 `tcp:` 前缀：`name@host:port` 那个形状**已经是
  S7 的**，两种传输共用同一个写法就分不出来。为什么 TCP 也要写：不写的话端口只能靠"通道名
  尾数"带过去、host 则无处可放——自定义名字（`网关-1`）导出再导入会变成端口 1、host 变回
  `0.0.0.0`，那是**静默改配置**。导入按 `@tcp:` → `@…://` → `@…:…` → `@…` 的顺序问；
  **不带 `@` 的老写法仍然能读**（端口按尾数猜 + 一条 note）。四种写法互不重叠，理由与判据
  都在 `project/csv_io.parse_tcp_name` 的 docstring 里。
- **S7 通道**同样写在通道名里：`name@host:port[ rack/slot]`（例 `s7-1@192.168.0.10:102 0/1`，
  `rack/slot` 省略时是 `0/1`）。与串口那条共用 `@`，靠"第一段里有没有 `://`、有没有 `:`"
  分开（pyserial 的 URL 与纯端口名是串口，`host:port` 是 S7）；导出时把 host:port 与
  rack/slot 按同一语法烘回名字，**往返不丢参数**。点了 S7 的名字却漏了端口（`s7-1@host`）
  配上一行的 S7 区/类型时带行号拒掉——它与"串口名叫 host"语法一模一样，只问通道名分不出来。
- **传输有六种**（2026-10-14 起，见 §11 第 13 条）：通道名那一格就是"这台设备在哪儿 +
  走哪种传输"，写法互不重叠：`name@tcp:host:port`（Modbus TCP）、`name@udp:host:port`
  （Modbus UDP）、`name@rtutcp:host:port`（TCP 上跑 RTU 帧，以太网转串口网关）、
  `name@COM3 19200 8E1`（串口 RTU）、`name@COM3 19200 8E1 ascii`（串口 ASCII，第四段是帧类型）、
  `name@host:port 0/1`（S7）。导出一律写全，所以导出再导入逐字一致。
- **S7 的点位**：`area` = `db<n>` / `mk` / `pe` / `pa`；`address` 是**字节偏移**，`bool`
  写成 `字节.位`（`6.3` = `DBX6.3`，模型里存成 `address` + `bit` 两个字段）；
  `datatype` 用 S7 的名字 `bool/byte/char/word/int/dword/dint/real/string`；
  **类型必须与传输匹配**（S7 通道上写 `uint16`、Modbus 通道上写 `real` 都是错误）。
  S7 没有字序：`word_order` 非 `ABCD` 只给一条"会被忽略"的警告。`string` 是**裸 ASCII 定长**
  （长度按点表初值算），与真 S7 STRING（前两字节是最大/当前长度）不同——这一点在使用说明里写明。
- 串口通道的帧格式写作 `8E1`（数据位 / 校验 N|E|O / 停止位），
  `project/model.parse_frame` 只做**转换**（形状不对回 None），"9N1 这种形状对、值不合法"
  留给 §8 的白名单报。
- `unit_id`：1–247。同 `channel + unit_id` 的行合并为一个从站的点位；重复地址在校验阶段报错，
  **不静默覆盖**
- `area`：`co / di / hr / ir`
- `address`：**裸协议地址，0-based**。不做 40001 偏置。厂商点表给的是 4x 形式时，
  用导入对话框的"整体偏移"输入框处理，模型里不藏隐式规则；点位表另开一列显示 `4x/3x/1x/0x`
  （列头叫**惯例**，不叫 `4x`——那一列的值随区变，S7 上根本算不出来（显示 `—`））
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

`type` 决定用哪个通道类：`"rtu"` -> `SerialChannel`（字段就是上面那 6 个串口键，
一个不多一个不少；类本身就是事实，`type` 由 `channel_transport()` 在写盘时补上），
其余 -> `TcpChannel`。键序按上面的顺序写，**同一份工程反复存盘产出同样的字节**；
`port`/`parity` 原样收下（合不合法由 §8 判），`baudrate`/`bytesize`/`stopbits` 必须是
JSON 整数、`timeout` 必须是数字（`int(1.9)=1` 那种静默截断会让现场看到"波特率变成 9600 了"）。

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

### 5.1 多种导入导出格式（2026-10-14 补）

三种格式（点表 CSV / 点表 Excel `.xlsx` / 工程 `project.json`）只有**一张登记表**：
`project/formats.py` 的 `FORMATS`——`Format(后缀, 对话框里那一行文字, 读, 写, kind)`。
四个分派点（`csv_io_entry.load_point_table`、`dialogs.open_project` / `save_project_to` /
`save_project_as`，导出那条走 `formats.write_project`）与三个文件对话框过滤器都问这张表，
**加一种格式 = 加一行 + 一个模块**。原来那套"`.json` 与其余按 CSV"在四处各写一遍，加格式要改
六处，漏一处的表现是"打开能读、保存却写成了另一种格式"。

三条口径：

* 后缀不认识**拒绝**（`FormatError`，`ValueError` 的子类），消息里列出支持哪些——猜一个格式
  等于把 JSON 内容当 CSV 解析，报出来的话离真原因很远；
* 读：读不懂抛 `ValueError`、文件读不到抛 `OSError`（与既有两条路同形，调用方那套处理照旧）；
* `read()` 一律回 `(Project, notes)`：点表那两种会带"端口按名字尾数猜的""表头不在第一行"
  这类提醒，`project.json` 没有（它自带全部信息）。

**Excel 那条不另写一套解析**：`project/xlsx_io.py` 把第一张工作表转成 CSV 文本再交给
`csv_io.read_csv`——通道名里 `name@tcp:host:port` 那套语法、"猜了要说"、逐行的列数/整数/布尔
判定、带行号的报错一个字都不重写，两边的语义不可能漂。两条现场处理：**表头找**（第一行 12 格
与表头完全一致的那一行当表头；找到就用**空行补齐**前面几行，报错里的行号因此与 Excel 里看到的
行号一致；找不到就原样交给 `csv_io` 报它那句 `header mismatch`）、**单元格最小转换**（数字按
数字读、Excel 的布尔读成 `true`/`false`、空单元格读成空串）。写出去时只有**逐字往返相同**的才
按数字写（`str(int(t)) == t`），`0008` / `12.50` / `1e5` 一律落成文本——否则 Excel 一存就静默
改点表。

**已知代价（写在这里免得现场当成 bug）**：Excel 的数值只有 15 位有效数字，17 位的 uint64
（累计电量那种）**存不精确**；读进来时若发现那一格是数字，给一条 note 点明原因。要精确传这种
值用 CSV 或 `project.json`。`openpyxl` 是**延迟 import** 的（只用 CSV/JSON 的人不被它拦住，
与 `snap7` 同一个姿态），`requirements.txt` 里已列。

**"要不要一个协议适配器"的答案：已经有了。** `project/model.channel_transport()` 是唯一的
传输判据（全包 29 处 / 13 个文件都问它，没有第二套 isinstance 判定）、`backend.make_backend()`
是唯一的装配口（界面 / 命令行 / 会话三条路都走它）、`BackendBase` 的钩子是与传输无关的
那一半（S7 接上去时 Modbus 两条路的既有用例一个断言没改）。真正缺的是**"注册"**：每种协议的
事实（通道类、区清单、默认区、默认点位类型、名字语法、校验函数）散在 15 个文件里各写一遍，
第四种协议要动 **16 处**——清单在 `plans/2026-10-14-import-export-formats.md` §4。**本轮不抽
注册表**：等第四种协议知道形状之后再按它定表，否则抽出来的是一层用不上的间接。唯一先动的是
**布尔 `s7` 改成传输串**（`engine/session.py`、`project/edit.py`）：布尔只有两态，第四种协议
加不进来，这一处是硬卡点；**行为一个字不变**（既有用例逐条钉住）。

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
┌ 工具栏: 全部启动/停止 │ 新建工程 │ 导入CSV │ 导出CSV │ 打开/保存工程 ┐
├────────┬──────────────────────────┴────────────────────┤
│ 设备树  │ 点位表（实时值，双击直接改；改值立即写进寄存器）  │
│ 通道→从站│ 名称|区|地址|惯例|类型|字序|当前值|原始寄存器|波形  │
├────────┼────────────────────────────────────────────────┤
│ 通道属性 │ [标签页] 报文监视(hex+解析) │ 日志 │ 计数        │
└────────┴────────────────────────────────────────────────┘
```

- **表头与数据都居中**（2026-10-17 现场口径）：表头本来就是居中的（Qt 实测
  `defaultAlignment` = `AlignHCenter|AlignVCenter`），数据原先却是"地址 / 惯例两列右对齐、
  其余走 Qt 默认左对齐"——两边各占一边，看着不齐。现在十列统一居中，判定只有一处
  （`point_table.data()`），`tests/test_gui_point_table.py` 钉住"每列与表头必须是同一个值"。
  居中**不动任何几何**：列宽是定宽的（见 `COLUMNS` 逐列量宽的注释），对齐只改文字在格子里的位置。
- **弹窗上的标准按钮说中文**：`OK` / `Cancel` / `Yes` / `No` / `Save` / `Discard` 的文字来自
  Qt 自己的翻译文件（`qtbase_zh_CN.qm`），**不装就是英文**——界面上其余字都是我们写的中文，
  只有这几个按钮是英文（2026-10-17 现场反馈）。安装点在主窗构造期，与主题同一处
  （`gui/i18n.py`）；固定 `zh_CN` 而不是跟随 `QLocale.system()`：英文系统的机器上跟随系统
  只会得到"中文界面 + 英文按钮"，同一个毛病换个方向。
- 通道可独立启停（现场常常只接了一路 485）
- **串口行（M5）**：通道面板每通道一行、那几格是定宽槽（口径 3/4）。TCP 行 =
  `名称 | 监听地址 | 端口 | (空) | 启动 | 停止 | 状态`；串口行把前两格换成
  **串口名**（`COM3`）与**波特率**（`19200`），第三格是**帧格式**（`8E1`），
  TCP 行那一格**留空并置灰**（置灰不隐藏——隐藏会让后面的格子集体左移，那正是
  "状态一变布局就跳"里最难看的一种）。面板只做**转换**（`8E1` → 数据位/校验/停止位、
  `9600` → int），白名单、"两个通道抢同一个串口"这类**判定**全在 §8 那一份里：
  先写回工程再验，验出新增的错就整体回滚（连输入框一起退回，`gui/channel_panel.py`）。
  "连接数"那一栏对串口不显示（串口没有"连接"这件事，显示它就是演戏）
- **S7 行（2026-10-13）**：与串口行同一套定宽槽——前两格是 **host / port**（惯例 102），
  第三格是 **rack/slot**（`0/1`），TCP 行那一格留空并置灰。写回沿用"面板只做转换、判定全在
  validate、验不过连输入框一起退回"那条路。
- **S7 的报文窗**：只有**主站请求**那一行（方向列是 `RX`：它是主站发下来的），
  "原始字节"列**留空**（snap7 的原生服务端不把原始报文交出来），摘要写
  `读 db1 4B @0` / `写 db1 2B @4` 这样"读/写 + 区 + 长度 + 偏移"。列宽与列序一律不动。
- **点表的"原始寄存器"列**在 S7 行上打的是**字节**（两位十六进制，`41 48 00 00`）：
  `Row.registers` 在 S7 这一路装的是字节序列；判据用 `datatype`（S7 的类型名与 Modbus 的
  不相交，且校验强制"类型与传输匹配"），列头不改。
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
- **值归属规则（v1 唯一一条，M4 已实现）**：每个点位有 `source = waveform | manual`，归属的
  **单位是点位**而不是寄存器格（身份与归属的键都是 `(通道, unit, 区, 地址, 位)`；S7 的
  `DBX6.2` 与 `DBX6.3` 同占一个字节但**是两个点**，见 §12 那条实测）。GUI 双击改值、或主站用 FC05/06/15/16 写入，都把该点切成
  `manual`，波形引擎**不再覆盖**它——识别发生在 backend 的 rx 报文侧，与写值同线程、同一次
  到达落地（不是界面侧的"我记得写过"）。点位表"波形"那一列显示 `已停`（占原来那一格的文案位，
  列宽列序不动）。两条实测边界：**主站写只读点被协议拒绝（异常码 2）时不切 manual**——
  一次没成功的写不该把曲线停了，而引擎自己的 `raw_writer` 写得进只读点，混起来更难查；
  **右键"恢复波形"按点位跨度整段放行**——占 4 格的 uint64 只恢复基础格时那 3 格地址此后再
  没有东西去动它们，而按**点位**计的状态栏（`Session.manual_count` 问的是各点的基础格）恰好
  看不见这件事：那一格会正常回到 0。
  这样测平台下发命令时不会出现"值被波形顶掉、误判下发没生效"。证据：
  `tests/test_integration_tcp.py::test_a_master_write_survives_the_waveform_tick`、
  `::test_a_master_write_of_a_coil_survives_the_waveform_tick`（FC05 那条走 `write_bits`，
  与 `write_registers` 是两个入口）、`::test_a_write_the_protocol_refused_does_not_take_the_waveform_off`、
  `::test_a_master_write_at_a_middle_register_takes_the_whole_point_over`。

- **文件菜单与保存（2026-10-13）**：菜单栏的"文件"收六个动作（新建 / 打开 / 导入CSV / 保存 /
  另存为 / 导出CSV，Ctrl+N/O/I/S、Ctrl+Shift+S、Ctrl+E），菜单项与工具栏按钮是**同一批
  `QAction` 对象**。**"保存"与"另存为"是两件事**：有路径时保存**直接写回**（状态栏一句
  "已保存 xxx"），没有路径（新建的工程）才弹对话框；另存为总是问。原来"保存"一律弹另存为
  ——一天存十次就问十次，人开始点错（`xx (2).json`）。
- **默认端口先探一次系统（2026-10-13）**：新建工程 / 添加通道算默认端口时用
  `engine.ports.is_free` 绑一次看看，被占就顺延并在提示框里说明（`tcp-5020` 这个名字自带端口，
  顺延之后名字也跟着变，不解释一句会让人以为工具搞错了）。探针是**注入**的（`project.edit`
  的 `probe=` 参数、`main_window.PORT_PROBE` 常量）：`edit` 是纯逻辑层，不许 import backend；
  注入之后用例也能换掉它（否则"新建 TCP 工程得到 tcp-5020"会取决于机器上 5020 空不空）。
- **从零建一份工程（2026-10-13）**：`GUI 手工录入` 落到这一组动作上——工具栏**新建工程**
  （空白，或 TCP / 串口 / S7 骨架：一条通道 + 一个设备 + 一个点位，名字是 `tcp-5020` /
  `设备1` / `点位1` 这类占位符、参数是该传输的默认值），设备树右键
  **添加通道 / 添加设备 / 删除通道 / 删除设备**，点位表右键**添加点位… / 删除点位…**。
  四条口径：
  1. **默认值一律避开已有的**（`project/edit.py` 的分配器）：通道名与 TCP/S7 端口下一个没用过的、
     设备名与 unit 取最小空号、点位地址取该设备该区"已占末尾之后"（S7 的 bool 落在新字节），
     所以"加一个"不会一加就撞车；S7 的默认端口仍是惯例的 **102**（不是 TCP 那个 5020 的邻居）。
  2. **目标通道在跑就整体拒绝并点名**（"先停止再改点表"）；但**别的通道一律不许被带停**
     （2026-10-13 修正）：加/删设备点位走 `Session.reindex(这一路)`；加/删**通道**走
     `Session.add_channel` / `remove_channel`（只动一个键）——所以**在跑的通道上加/删另一路是
     允许的**，跑着的那一路不受影响（会停掉所有在跑通道的只有 `load()`，那条路只留给"换一份
     工程"）。删一条正在跑的通道：先停它自己（确认框里说清），停不掉就拒绝。
  3. **删除先问一次**（带名字与"连同 N 个设备、M 个点位"），删掉不可撤销。
  4. **换工程先问未保存改动**："保存 / 丢弃 / 取消"三选一，**新建 / 打开 / 导入 / 关窗**四个
     动作共用（原来只有关窗问，另外三个会静默丢改动——实测确认过的缺陷）。
  改动一律走"改 → 全量 `validate_project` → 与改前做差集 → 有新增 error 就整体回滚"，
  与通道面板、点位属性面板**同一条管线**（`simbench/project/edit.py`，判定一条都不在界面层）。
  证据：`tests/test_edit.py`（33 条）、`tests/test_gui_new_project.py`（34 条，含"空白工程一路
  建到真主站读得到"与"S7 骨架加两个点再真上位机读写"两条端到端）；界面合同（无 `QComboBox`、换类型几何恒定、被拒时面板不关）
  在同一份里逐格量。

## 8. 错误处理

三档，不混：

- **启动前校验，一次报全**：CSV 解析错误带行号；地址重叠（位区按位判、寄存器区按寄存器判，
  两边口径实测不同，见 §12）；类型与区不匹配（如 co 上放 float32）；`wave_param` 缺参或非法
  （`period=0`、`min>max`、类型不符）；`unit_id` 越界（只认 1..247，且不建 `id=0` 兜底设备）；
  `host` 不是非空字符串；**点位名为空**（M4 Task 7 补：`name` 是身份列，加这条之前属性面板
  能把它一路应用到运行态，而这份工程导出的 CSV 再也读不回来；判据在 `validate_project`，
  CSV 那侧带行号的另一句在 `csv_io._require_text`，两个来源两种说法）；**`port=0`**
  （"让系统随便给一个"在 M1 也算 error——要读回实际端口
  得从 pymodbus 内部摸 transport 对象，版本一变就碎）；**同一个 `(host, port)` 被两个通道
  占用**（不等启动第二阶段炸 bind：Windows 只回 10048 且不带通道名，现场看不出是点表配置错）；
  字序与数据类型冲突——**64 位类型填 `CDAB`/`BADC`、`string` 填非 `ABCD` 才是拦截项**
  （语义有分歧，不许猜），而单寄存器类型（bool/int16/uint16/reg16）填了 `ABCD` 之外的值
  只记一条 warning（§5）；端口被占（bind 阶段的 Windows 10048）；**串口那一族（M5）**：
  串口名为空/不是字符串、串口名含 `@` 或空格（会把 §5 的 CSV 通道名语法拆坏）、
  波特率不在白名单（1200/2400/4800/9600/19200/38400/57600/115200——写 9500 这种"看着也像数"
  的值进来，真跑起来是收发全乱、报文窗一片 CRC 错）、`parity` 不在 N/E/O、`bytesize` 不是
  7/8、`stopbits` 不是 1/2、`timeout` 不是正数、**两个通道配同一个串口**（现象是"一路能收
  一路全错帧"，而 Windows 只会说 Access is denied）、通道名含 `@`（§5 的保留字符）；
  以及**运行期**的串口打不开（不存在 / 被别的程序占着 / 驱动没装好）；**S7 那一族**：
  host 不许含 `:`/`/`/`@`/空格（会把 §5 的通道名语法拆坏）、port 1..65535、
  rack 0..7、slot 1..31、与 TCP 抢同一个 `host:port` 判重复；点位：区名（`db<n>` 的
  n 在 1..4095 / mk / pe / pa）、S7 类型、字节地址 0..65535、`bool` 必须带位且位在 0..7
  （非 bool 不许带位）、**重叠按位判**（同一字节里的两个 bool 合法，`word@0` 与 `bool@0.0`
  撞上）；`word_order` 非 ABCD 只给警告。
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
- **GUI**：不做 UI 自动化（YAGNI）。用例造主窗时把「与外界的那几个接缝」（会弹框的对话框、
  端口探针）经 `gui/deps.py` 的 `WindowDeps` 注入假件——2026-10-16 之前是
  `monkeypatch.setattr(main_window, ...)` 那种改模块全局的写法，代价是混在别的模块里的方法
  必须运行时去 `main_window` 上现查那个名字（`_mw()`），读源码看不出这个方法到底依赖什么。
  共享的假件在 `tests/gui_support.py`（不放 conftest：那是所有用例都会 import 的，而它要
  import PySide6，内核用例不该被装上 Qt）。
- **RTU**：端到端测不了（需真串口）。走 pymodbus 现成 `ModbusSerialServer`，
  交付时明确标注"需在笔记本 + USB-485 做一次人工冒烟"，不假称已验证。

## 11. 里程碑

每个里程碑都能单独跑起来。

> **分支说明（2026-10-13）**：M1 到打包这一段全部在**本地分支 `m1-kernel`** 上做（下面
> 各条与 M2 计划抬头写的“分支 `m1-kernel`”就是它）。2026-10-13 该分支改名为 `main`
> 并推到 `origin/main`——**改的只是名字，里程碑内容一个字符没动**，历史记录保持原样。

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
   入口 `simbench/gui/app.py` + `python -m simbench <点表> --gui`。全量
   `.venv/Scripts/python.exe -m pytest` **773 passed in 63.27s**（无 skip、无 xfail，串行连跑 4 轮
   全绿）。此前那条"整轮挂死"的根因与修法见 §12/§13：自动 GC 在非 GUI 线程销毁界面丢弃的
   `QWidget`，与正等引擎回话的主线程互锁；现已定口径**回收只许发生在 GUI 线程**。
   等人的还是人工那两条：计划 Task 9 Step 2（真界面 6 项）与 Step 3（真界面 + 真主站）。
3. **M3 CSV 导入导出 + 报文监视窗 + 多通道独立启停**。
4. **M4 波形引擎 + 值归属 + 点位属性面板（已完成）**（const/ramp/sine/random/step）——
   纯逻辑、可全自动测，不依赖硬件，所以排在串口之前。落地在计划
   `docs/superpowers/plans/2026-10-11-modslave-m4-waveform.md`、分支 `m4-waveform`，
   提交 `7442a49`…`3b79559`（Task 1–6 各一轮评审 + 两轮修复；全分支评审打回一次，两条跨任务
   缺陷与一处 feature gap 修在 `7279b83`，剩下的措辞与 `_serve` 的默认值修在 `3b79559`）。
   全量 `.venv/Scripts/python.exe -m pytest` **917 passed in 89.91s**（无 skip、无 xfail，**串行**跑，
   这套件绑真实临时端口，两份并行结果不可信）。三条真主站验收（读一条动起来的 `sine`、
   主站写 `hr` 与 `co` 各活过十个 tick）在 `tests/test_integration_tcp.py`；无头 CLI 这一路
   现在也自己驱动曲线（`__main__._serve` 收 `project.tick_ms`，运行期报错一句进 stderr），
   命令行与界面起同一个工程行为一致。执行期与计划文本不一致的地方全部记在那份计划末尾的
   "执行期已裁决的偏离"表里（D1–D17），其中一条是新增的启动前判据：**点位名不许为空**
   （属性面板清得空、CSV 那边 `csv_io._require_text` 只认得行号），判据住在 `validate_project`，
   面板与 `start_channel` 一起生效；另一条是**改值不许超出点位跨度**（`len(registers) > pt.span`
   在进引擎之前拒，跨度归点表的初值管）。
   等人的仍是人工那一条：Task 7 Step 5 的界面冒烟清单。
5. **M5 RTU backend（已完成）**：串口通道（模型/校验/CSV 与 JSON 往返/界面那一行）+
   `ModbusRtuBackend`。落地在计划 `docs/superpowers/plans/2026-10-12-modslave-m5-rtu.md`
   （三段执行，偏离表 D1–D12），新增 `simbench/backend/base.py`（2026-10-16 从 `modbus_base.py` 改名）（TCP 与 RTU 共用的
   那一半：波形 tick、值归属、报文/日志钩子、启停顺序；**不 import pymodbus**）与
   `simbench/backend/modbus_rtu.py`（串口探针 + `ModbusSerialServer` + 按 RTU 帧切帧）。
   验收**不需要硬件**：`ModbusSerialServer(port="socket://host:port")` 会自建 TCP 监听、
   主站那一侧是 pyserial 的串口对象，两端跑真 RTU framer（CRC、单元号、静默切帧都在）
   —— 这条路来自对 pymodbus 3.15 源码分支的实测（`transport/transport.py:151-160`），
   脚本与输出留档在 `docs/superpowers/notes/2026-10-12-m5-rtu-probes.md`。
   §10 那张 12 行验收表在 `tests/test_serial_backend.py` 上逐行成立（四区读值逐字对齐 TCP、
   寄存器/线圈写各"两个 tick 后仍是我写的值"、被协议拒的只读点不切 manual、异常码 2/4、
   钳位到边界 + 60s 去重日志、报文窗六位置合同、一 TCP 一 RTU 独立起停）；无头命令行也能起
   串口通道（`python -m simbench x.csv --list` 与真主站在 `socket://` 上读写）。
   全量 `.venv/Scripts/python.exe -m pytest` **1131 passed**（无 skip、无 xfail，**串行**跑）。
   等人的只有人工那五条（真 USB-485：读四区、写值看钳位、拔线、串口被占、COM10+ 与高 DPI
   观感）——自动化替不了，见那份计划 Task 8.3 的步骤表。
6. **M6 系统网络层（已完成）**：专用网卡（`netloop.inf` 新建 + 账本认领）、设备 IP 的
   "只加不猜/只删自己的"差异判定、`net show|check|apply|up|clean|init|device` 子命令与
   提权、启动挂钩（起从站/开界面时自动配，失败不拦启动）、按声明多挂监听、GUI 网络页。
   落地在 `simbench/net/`（`plan.py`/`spec.py`/`state.py`/`ledger.py`/`device.py`/`win32.py`/
   `endpoints.py`/`startup.py`/`cli.py`，外加叶子模块 `errors.py`）与 `tests/test_net_*.py`/`test_backend_listeners.py`/
   `test_gui_net_panel.py`，口径见 §14。全量 `.venv/Scripts/python.exe -m pytest`
   **1031 passed**（无 skip、无 xfail，**串行**跑：这套件绑真实临时端口，两份并行结果不可信）。
   **没有单独的计划文档**：这一段是用户在一次会话里直接提的功能，落地口径全部记在 §14 与
   代码注释里。手动冒烟那一条（真网卡 + 真主站按 `设备IP:端口` 连）仍留在人手上：本机
   `simbench 模拟网卡` 已建好（ifIndex 67），声明示例见 §14.1。

7. **S7（ISO-on-TCP / S7comm）通道（已完成，2026-10-13）**：路线是 snap7 的原生服务端
   （`snap7.dll` 随 `python-snap7==1.3` 的 wheel 来，LGPL-3.0；1.3 会 import 已被
   setuptools>=81 移除的 `pkg_resources`，所以钉 `setuptools<81`）。落地：`project` 层的
   `S7Channel`/CSV 语法/JSON 键/校验、`codec/s7_codec.py`、`backend/s7_server.py`
   （多 DB/区缓冲、事件 → 报文事件行与值归属）、`make_backend` 的 s7 分支、
   `Session` 的读写按传输分派、通道面板的 S7 行与属性面板的 S7 选项。计划与偏离表
   （D1–D12）在 `docs/superpowers/plans/2026-10-13-s7-server.md`，实测记录（含事件码布局、
   `register_area` 的两套区代码、只用 Modbus 也能跑）在
   `docs/superpowers/notes/2026-10-13-s7-probes.md`。验收**不需要硬件**：`pys7`（纯 Python）
   + snap7 自带客户端两个独立主站跑同一张表。**注意**：snap7 的服务端不答 SZL 那类请求
   （`pys7.get_cpu_status()` 会超时，snap7 客户端的 `get_cpu_state()` 正常）；真上位机若探
   SZL 会看到超时，这是"不做 SZL 全表"那条口径的直接后果。


8. **打包（PyInstaller）（已完成，2026-10-13）**：v1 之后最后一块工程尾巴（§13 那条"v1 不验证
   打包会留一个尾巴"）。落地 `packaging/`：`simbench.spec`（一次分析、两个 EXE + 一个 onefile）、
   `entry.py`（两个 exe 共用，按有没有控制台分派）、`build.ps1`（一条命令打全套）、
   `smoke.py`（拿真主站打产物的十条冒烟）。产物：`dist/simbench/simbench.exe`（控制台）、
   `dist/simbench/simbench-gui.exe`（无控制台，两个共用 `_internal/`）、
   `dist/simbench-onefile.exe`（单文件，**不是**发布形态）。冻结后只有两处会碎，都修了：
   ① 提权重跑自己的 `-m simbench`（改成 `sys.frozen` 分派，`net/cli.py::_self_command_prefix`）；
   ② `snap7.dll` 漏了时的一行报错（snap7 是"用到才加载"，import 期的闸门拦不到）。
   验收：`packaging/smoke.py` **11/11 通过**（--check / --list / 缺文件 / net show / TCP / RTU /
   S7 / GUI 两条 / onefile 两条），三条通道各走**真主站**；冷启动 onedir 0.42–0.57s、
   onefile 1.7–1.9s，体积 124.0 MB / 48.7 MB。计划在
   `docs/superpowers/plans/2026-10-13-packaging.md`，实测记录在 `packaging/SMOKE.md`。
   留在人手上的：真 UAC 弹窗点一次、真上位机连冻结包、双击 GUI 的观感（M2 那份清单）。

已确认按 M1→M2→M3→M4→M5→M6 顺序（M6 与 M5 互不依赖，谁先都能做）；S7 是 v1 之后的独立里程碑
（与 M5/M6 之后落地，见上面第 7 条）。


9. **新建工程与增删（已完成，2026-10-13）**：补 spec §2 那句"GUI 手工录入为辅"——原来只做成了
   "改值 + 改属性"，没有"从零搭一份"。新增 `simbench/project/edit.py`（分配器 + 增删六条 +
   新建工程；改 → 验 → 差集 → 回滚，与两条既有编辑路同一条管线）、`gui/channel_form.py`
   （类型 + 名字 + 参数的表单，新建与添加共用）、`gui/new_project.py`（三个对话框）、
   设备树与点位表的右键菜单、工具栏"新建工程"。顺手修了三条既有缺陷：**打开/导入/新建会静默
   丢掉未保存改动**、**切到空工程后点位表还留着上一份的行**、**新建的工程导不出 CSV**。
   验收：`tests/test_edit.py` 与 `tests/test_gui_new_project.py` 全绿（含"空白 → 加通道 →
   加设备 → 加点位 → 起从站 → 真主站读写"的端到端）；全量串行 **1259 → 1329 passed**。
   计划与偏离表在 `docs/superpowers/plans/2026-10-13-new-project.md`。
   不做：重命名通道/设备、点位进树的第三级、拖拽排序、撤销/重做、在跑的通道上增删。


10. **第二轮深度审查的 12 条（已完成，2026-10-13）**：CSV 的 TCP 通道带上 host/port
    （`name@tcp:host:port`）、点位属性面板的"位"（界面里也能建 S7 bool 点位）、点位表显示
    `6.3`、保存就地写回 + 文件菜单、日志窗上限、回读收成一次引擎线程快照、依赖拆两份、
    exe 图标与版本资源、提权临时文件唯一化、增量加/删通道（在跑也能加另一路）、文档索引、
    建通道时探端口占用。计划与偏离表（D1–D4）在
    `docs/superpowers/plans/2026-10-13-review-round2.md`；全量 **1354 → 1366 passed**。


11. **混装工程（2026-10-13 补）**：一张点表里可以有**多种协议**（TCP + 串口 + S7）与多路同协议
    通道，各处都按通道自己的传输走（校验、编解码、后端、界面那一行、CSV 的通道名写法）。
    这一轮补的是**命令行那一路**：原来 `python -m simbench x.csv` 只起**第一个**通道（stderr
    上那句 "M1 serves one channel at a time"），于是"离了界面就用不了混装工程"。现在
    `_serve` 起**全部**通道、逐个打印各自的监听形态（`tcp-x: listening on 127.0.0.1:5020 …` /
    `com-485-1: serving on socket://… 9600 8N1 …` / `s7-x: serving on … rack 0 slot 1 …`）。
    一路起不来**不牵连别的**（与界面"全部启动"同一条口径）：当场在 stderr 上留一行带通道名的
    错，其余照服务；**一路都没起来**才当场退出码 2，部分起不来的情况在收工时回 2（"有通道没
    服务上"脚本要看得见）。证据：`tests/test_cli.py::test_the_command_line_serves_every_channel_
    of_a_mixed_project`（三类真主站各读一次）、`::test_one_channel_that_cannot_start_does_not_
    stop_the_others`、`::test_list_shows_each_protocols_own_address_form`、
    `tests/test_session.py::test_a_mixed_project_runs_every_protocol_together`。

12. **多种导入导出格式（已完成，2026-10-14）**：`.csv` / `.xlsx` / `project.json` 收进
    `project/formats.py` **一张表**（原来"`.json` 与其余按 CSV"在四处各写一遍，加格式要改六处），
    新增 `project/xlsx_io.py`（把工作表转成 CSV 文本再交给同一个解析器：表头可在标题行之下、
    报错行号与 Excel 一致、17 位整数丢精度时给一条 note）；导出那条路也改成**后缀说了算**
    （选 Excel 就真写 Excel，不再"按 CSV 写出去但叫 xlsx"），工具栏那两个字跟着改成
    「导入点表 / 导出点表」。顺手把 `engine/session.py` 与 `project/edit.py` 里的布尔 `s7`
    换成**传输串**（布尔只有两态，第四种协议加不进来，是硬卡点；行为一个字不变），并把
    "加一种协议要改哪 16 处"实测成清单。`openpyxl` 进 `requirements.txt`（延迟 import，
    只用 CSV/JSON 的人不被它拦住）；冻结包那侧 `packaging/smoke.py` 加一条"读 `.xlsx`"。
    验收：`tests/test_formats.py` 全绿、全量串行 **1376 → 1389 passed**（0 失败 0 跳过）、
    `packaging/smoke.py` **11 → 12 通过**。计划在
    `docs/superpowers/plans/2026-10-14-import-export-formats.md`（含协议适配器的结论：**适配器
    已经有了**，缺的是"注册"，而注册表这一轮**不做**——等第四种协议知道形状之后再抽）。
    不做：协议注册表、Excel 多 sheet / 合并单元格 / 公式、别的点表格式（`.mbs` 那些，
    要对着真文件定口径）。

13. **深度审查那一批（已完成，2026-10-14）**：13 条优化 + 2 项扩展，分六批提交，每批都跑
    全量串行用例。计划与逐条实测在 `docs/superpowers/plans/2026-10-14-deep-review-batch.md`。
    值得记住的几条：
    * **数据安全**：保存（点表 / Excel / 工程 / 网络声明）全部改成"临时文件 + 原子替换 + 一份
      `.bak`"；界面版 exe 是无控制台构建，**崩溃与日志现在有落点**
      （`%LOCALAPPDATA%\simbench\logs`，含 `faulthandler` 的原生栈），日志页加"另存日志"。
    * **随规模退化**：4000 点的表一跳 `Session.rows()` 从 36.6ms 降到 16.3ms（行缓存），
      界面那一跳改成后台线程取快照（主线程永不等引擎）；报文窗按事件序号做差集，
      写满 5000 帧后从 65.6ms/跳降到 14.4ms/跳且**不再每 100ms 清一次选中行**。
    * **工程化**：加了 `pyproject.toml`（ruff + mypy，125 条发现全部处理完，mypy 从 42 条到 0）、
      CI（lint → windows 上跑全量 → 打 tag 时打包）、版本单一来源 + `--version` + CHANGELOG、
      依赖全部钉死 + `requirements.lock.txt`。
    * **界面**：设置落盘（主题/几何/最近打开/上次目录）、核对页（主站模式）、
      吞掉的异常一律留痕（`simbench/swallow.py`）。
    * **传输**：从两种扩到**六种**（`tcp` / `udp` / `rtutcp` / `rtu` / `ascii` / `s7`），
      每种都用真主站打过（见 §5 那张表）；`SerialChannel.framing` 区分 RTU 与 ASCII。
    * **主站模式**：`--scan` / `--verify` + 界面"核对"页，只读别人的设备，四类结论
      （对得上 / 对不上 / 没应答 / 读出错）。全量 **1389 → 1535 passed**。

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

M4 内确认（**已清零**，结论都有用例钉住；这些是"看着像常识、实测不成立"的那一类）：

- **已实测：`random.Random(种子)` 是确定的，跨进程也一样**（字符串种子走 sha512 派生，不掺
  pid / 系统熵 / hash 随机化）。→ 计划里那句"`interval` 缺省就 `Random(bucket)`，两次运行不
  重复"**不成立**：同一个 `bucket` 每次 pytest 都给同一个数，正是 §5"不填 `seed` 则不重复"
  不要的形状。所以不填 `seed` 时种子掺一份**每进程**熵（import 时取一次 pid +
  `SystemRandom`，本次运行内恒定，同 `interval` 区间里才不抖），填了 `seed` 就完全可复现。
  证据：`tests/test_waveform.py::test_random_is_stable_within_one_interval_and_seeded_is_reproducible`
  （金标准是常量 `25.14189165270887`，不是"和自己比"的自证式断言）、
  `::test_random_defaults_to_one_second_between_values_when_interval_is_omitted`。
- **已实测：`_tick_once` 每一轮都重新读同一个 `Point` 对象**（点表摊平成 `[(unit_id, Point)]`
  一次，之后按对象取）。→ `name`/`waveform`/`wave_param` 是 **live 安全**的：面板改完下一拍
  就生效，不需要重载会话；而 `area`/`address`/`datatype`/`word_order`/`initial`/`readonly` 烘在
  `start_channel` 时建好的 `SimDevice` 与 `Session._points` 索引里，**必须重启通道**才生效。
  这条区分是点位属性面板整套拒绝口径的根据，也顺手钉住了 `Point.span` 的稳定性（跨度由
  `datatype`+`initial` 算出，跑着时改不动 → 接管键不会陈旧）。证据：
  `tests/test_gui_point_props.py::test_live_safe_fields_apply_while_the_channel_runs_without_reloading`、
  `::test_structural_fields_refuse_an_apply_while_the_owning_channel_runs`。
- **已实测：一张带着 error 的点表必须照样渲染**——区名写成 `holding` 的那一行让
  `QAbstractTableModel.data()` 对**该行每一格**都抛（10/10 格全空白），"带错装载再进去修"
  这条路在表格层是死的（而 §8 的"一次报全"就意味着操作员开的往往是这一行）。→ 算不出的
  惯例地址在显示层给同一个 `—` 标记（`point_table.NO_CONVENTION`，面板共用），
  `conventional()` 继续抛：判定只在 `validate_project` 一份里，标记不许变成"看着合法的错值"。
  证据：`tests/test_gui_point_table.py::test_a_row_with_an_unknown_area_still_renders_every_column`、
  `::test_showing_a_dirty_row_does_not_make_it_valid`。
- **已实测：波形节拍是"先睡一个周期再算"**，所以通道起来之后的头一个 `tick_ms` 里寄存器还是
  点表初值（`tick_ms=50` 时实测首样 4/4 读到 12.5）。→ 主站侧"读到的第一条不是曲线"是预期，
  任何验收都得先等第一拍，否则断言测的是启动时序而不是波形。证据：
  `tests/test_integration_tcp.py::test_a_real_master_reads_a_running_sine_wave` 采样前那句
  `wait_for`。
- **已实测：把归属判定拆掉之后，"写一次、0.5 秒后读一次"这种单点采样会放过变异**——
  `step;1,0;interval=0.2` 的 2.5 倍正好是 0.5 秒，那一拍曲线本来就在 1 上。→ 下发不被顶的
  验收必须把一个周期铺开取样（实测形状 `[True, True, True, False, False]`）。证据：
  `tests/test_integration_tcp.py::test_a_master_write_of_a_coil_survives_the_waveform_tick`。
- **已实测：05/06 的响应帧与请求逐字节相同**（连 transaction id 都一样），15/16 的响应回的是
  同样的 fc+地址+数量，异常响应又被 `parse_packet` 脱掉了 `0x80`。→ 报文侧唯一可靠的
  "这是主站的命令"判据是 `direction == "rx"`，按内容分辨不出来。这条只有单元层钉得住：
  把 `direction` 判掉之后集成用例全绿（响应帧多标一次 manual 是幂等的）。证据：
  `tests/test_ownership.py::test_the_slaves_own_write_response_is_not_a_write`。
- **已实测：位区与寄存器区的两个写入口不许混用**（M1 的 `raw_writer` 说明，M4 的 tick 用到）：
  寄存器路径的"地址 0"是**线圈 0..15 共用的那一整字**，所以
  `write_registers(11,'co',0,[0x0002])` 会回 `True` 而点亮的是**线圈 1**（同一字里的第 1 位），
  并把那一个字其余 15 位一起覆盖掉——"引擎想关风机、写成开"就是这么来的。→ `raw_writer`
  直接按区把两条 API 互相拒掉，tick 按 `area_is_bit` 分派。证据：
  `tests/test_raw_writer.py::test_register_api_refuses_bit_areas`、
  `::test_bit_api_refuses_register_areas`（两条 API 互拒），
  `tests/test_integration_tcp.py::test_one_bad_point_does_not_stop_the_rest_of_the_tick`
  （tick 若给 `co` 走寄存器入口，那个点会因 `ValueError` 不算写掉，`written == 2` 当场红）。

M6 内确认（**已清零**，结论都有用例钉住）：

- **一个 pymodbus server 只按一个 `(host, port)` 建监听**：`ModbusProtocol.listen()` 里是
  `loop.create_server(factory, host, port)`，`host` 只取 `source_address[0]` 这一个值。→ 声明里
  的第二个地址只能自己挂 `loop.create_server(server.handle_new_connection, host, port)`；
  用 pymodbus 自己那个连接工厂，所以每条连接仍登记进 `server.active_connections`、
  handler 读写的还是 `server.context` —— **所有地址共用同一份寄存器**（每个地址一份副本的
  后果是"主站从 A 写、界面按主 server 读回旧值，两边都不报错"）。证据：
  `tests/test_backend_listeners.py::test_both_endpoints_serve_one_datastore` 用两个真主站
  在两个端口上各写一次各读一次。
- **"地址不在本机"与"端口被占"在 bind 上只差一个错误码**（Windows 10049 vs 10048），
  但该做的事相反（去跑 `net up` vs 去停那个进程）。→ 10049 单独成一句带 `net up` 指路的
  原话。证据：`tests/test_backend_listeners.py::test_an_address_that_is_not_on_this_machine_names_the_fix`。
- **提权子进程的参数不能走命令行**：`Start-Process -ArgumentList` 是按空格拼成一条命令行的，
  网卡名"simbench 模拟网卡"里的空格会把参数拆开，子进程 argparse 直接退出 2。→ 参数写进
  JSON 任务文件，命令行只带一个路径。证据：
  `tests/test_net_startup.py::test_device_create_relaunches_with_both_subcommand_words`、
  `::test_device_create_replays_its_job_file`。
- **跑测试必须能关掉启动挂钩**：用例装载的是 `examples/` 下的点表，而现场那份点表旁边早晚
  躺着一份 `.net.json` —— 闸门一松，一次 pytest 就真会去改机器上的网卡地址。→
  `SIMBENCH_NET_AUTO=0`（`tests/conftest.py` 默认关掉），`--no-net` 也落到同一个开关上。

M5 内确认（**第一条已清零，第二条转人工冒烟**）：

- **已实测：`ModbusSerialServer` 的 `trace_packet` / `trace_connect` 都生效。**
  在 `socket://` 虚拟串口对上，一次 `fc03` + 一次异常响应共 10 帧请求/响应都进了钩子
  （`0b 03 00 04 00 01 c5 61` 这类带 CRC 的整帧），`trace_connect` 收到 `(True, False)`、
  主站断开后 `active_connections` 清空。报文窗与连接状态因此在 RTU 上一样成立。
  脚本与输出：`docs/superpowers/notes/2026-10-12-m5-rtu-probes.md`；
  用例：`tests/test_serial_backend.py::test_the_rtu_packet_sink_carries_the_contract_fields`。
- **拔 USB-485 后 pymodbus 抛的是哪类异常 —— 转人工那一次确认。** 源码路径上
  `SerialTransport.intern_read_ready` 捕的是 `serial.SerialException` 并走 `close(exc=exc)`，
  但"拔线时底层到底给不给这个异常"本机没有硬件可验；人工冒烟时按计划 Task 8.3 的
  "拔线"那一条确认：通道要置 `▲ 失败` + 日志一句人话，插回后重新"启动"能起来。
- 另一条实测副产品（写进代码注释了）：串口打不开时 pymodbus 只抛通用
  `RuntimeError("Could not start listen, please check address.")`，真实原因（pyserial 的
  `could not open port 'COM99': FileNotFoundError`）只剩一条 warning，而项目自己的
  `configure_library_logging(ERROR)` 连那条也压掉 —— 所以 RTU 后端必须先自己开一次串口
  （`check_serial_is_free`）把原话拿到手。

S7 内确认（**已清零**，结论都有用例钉住）：

- **一个 snap7 服务端能挂多个 DB**：`register_area(srvAreaDB, <DB号>, buffer)`，未注册的 DB
  回 `Item not available`、注册了但越界回 `OUT_OF_RANGE`；MK/PE/PA 各自独立。
- **主站的写落进我们那块缓冲、我们改缓冲主站也读得到** ⇒ 波形 tick 与值归属复用既有机制。
- **拿不到原始报文**：事件回调只给 `EvtCode`（`0x20000`=读、`0x40000`=写）与
  `EvtParam1..4`（区代码/DB/起始字节/长度），所以报文窗只有事件行（§7 那条）。
- **（2026-10-13）回读的线程归属**：`rows()` 原来逐个点直读后端缓冲（跨线程直读，实测
  "跨线程调用 0 次"就说明它没走引擎线程）。现在收成**一次**整批快照（跑着时 1 次、没跑时 0 次），
  与 `ownership.py` 开头那句"运行态只在 loop 线程读写"一致；超时/失败整批标 `stale` 且只报
  一次错。证据：`tests/test_session.py::test_rows_reads_the_whole_table_in_one_cross_thread_call`、
  `::test_a_wedged_engine_marks_the_rows_stale_and_says_so_only_once`。
- **两套区代码**：`register_area` 要的是服务端侧（DB=5/MK=2/PE=0），事件里是客户端侧
  （132/131/129/130）——混用会 `KeyError`（实测踩过）。
- **同字节的两个 bool 是两个身份（2026-10-13 修）**：`DBX6.2` 与 `DBX6.3` 同占一个字节是
  S7 的常规用法（判定按位、明确允许）。起初 `Session._points` 与 `Row.key` 都只到"字节"，
  后注册的点把前一个**覆盖**掉——界面双击一个、写进去的是另一个（数据错位级）。现在身份键
  与归属键都带"位"（五元组），改一个、交还一个都不影响邻居；而主站的**写事件只给字节范围**，
  所以同字节里的另一个 bool **也会**被标成 `manual`（既有的"宁可多标一个"，一次恢复就回来），
  但它的**值**不变。证据：`tests/test_s7_session.py` 三条
  （`test_two_bools_in_one_byte_are_two_identities` 等，含真上位机读到"只有那一位变了"）、
  `tests/test_gui_s7.py::test_the_properties_panel_edits_the_bit_that_was_asked_for`、
  `tests/test_edit.py::test_a_bitless_removal_on_a_shared_byte_is_refused_but_a_par_one_works`。

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
- **（已清零）打包（PyInstaller）这条尾巴**：v1 之后单列的里程碑，2026-10-13 完成。
  落地 `packaging/`（`simbench.spec` + `entry.py` + `build.ps1` + `smoke.py`），产物是
  `dist/simbench/` 下的 `simbench.exe`（控制台）与 `simbench-gui.exe`（无控制台，共用
  `_internal/`）外加单文件 `dist/simbench-onefile.exe`。实测的两处**冻结才会碎**的地方：
  ① 提权重跑自己的 `-m simbench`（冻结后没有这条路，改成按 `sys.frozen` 分派，
  `net/cli.py::_self_command_prefix`）；② `snap7.dll` 是**用到才加载**的，产物漏了它
  时原来是一段 traceback，现在是一行带安装提示的错。冻结冒烟 11 项全绿、三条通道各走
  真主站，记录与冷启动/体积见 `packaging/SMOKE.md`；计划与口径见
  `docs/superpowers/plans/2026-10-13-packaging.md`。

## 14. 系统网络层（专用网卡 + 设备 IP）

现场要的形状：点表里每台设备在一张**真实网卡**上有一个 IP，主站按 `设备IP:端口` 连上去，
读到的是这台模拟设备。这是本工具唯一会**改操作系统**的一层，所以它单独成包
（`simbench/net/`），安全口径全部写在代码注释与本节里，并由 `tests/test_architecture.py`
钉死：`subprocess` 只许出现在这个包（`net/state.py` 跑 PowerShell、`net/win32.py` 造/删
网卡），`gui/`、`engine/` 连 `import subprocess` 都不许有。

### 14.1 一份文件、几条命令

- **`x.net.json`**（点表旁边，`x.csv` → `x.net.json`，`companion_path()`）：`adapter_name` +
  `devices[]`（`name` / `ip` / 可选 `port`(默认 502) / 可选 `prefix`(默认 24)）。故意做成
  扁平的，现场拿记事本也能改。
  为什么不塞进 `project.json`：IP 是**环境相关的**（现场、同事机器、我的机器各不相同），
  与点表（设备定义）生命周期不同。两份分开，才不会"把点表拷给同事"顺手把地址也带过去。
- **`python -m simbench net …`**：
  - 只读（不需要管理员）：`show` 看网卡现状、`check` 把声明/清单与现状对一遍（**有差异回 2**，
    脚本里当"需要处理"用）、`device show` 看专用网卡在不在；
  - 改系统（**提权**）：`up`（没有专用网卡就建，再把缺的地址配上，一次 UAC 办完两件事）、
    `clean`（收回本工程记过账的地址）、`device create|remove`；
  - `init`：从点表读设备名 + 人来给 IP，生成声明骨架（数量对不上会明说，不静默截断）。
- 提权只覆盖这几条子命令：当前不是管理员时用 `Start-Process -Verb RunAs` 重跑自己，**参数走
  JSON 任务文件**（命令行拼参数会被网卡名里的空格拆开），结果也走文件回传（提权进程的
  stdout 抓不到）。GUI 与其余命令始终保持普通权限。

### 14.2 一律新建专用网卡，永不借用机器上已有的

机器上可能早就有别人装的环回适配器（本机就有一块"以太网 5"）。借用它 = 和别人的配置共用
一份可变状态：对方改一个字、我们删一个地址，两边互相踩。所以 `net device create` 用系统自带
的 `C:\WINDOWS\INF\netloop.inf` 造一块 `simbench 模拟网卡`（不需要第三方驱动、不需要重启）。

认一块网卡"是我们的"只有两条证据：账本里记过它的适配器 GUID，或者名字**精确**等于
`simbench 模拟网卡`。**不按描述认**：机器上别的环回适配器描述一模一样（`Microsoft KM-TEST
环回适配器`），按描述认会把它们当成自己的。卸载走设备实例（`pnputil /remove-device`）。

账本（`%LOCALAPPDATA%\simbench\net-ledger.json`）记着"我们加过哪些地址、造过哪块网卡"；
它坏掉时方向必须是安全的：`owned` 一律为空 → 一个地址都不回收。

### 14.3 三条差异口径（纯函数，可脱离真实网卡单测）

判定集中在 `net/plan.py`，因为"要不要动系统"不该由"当时网卡上正好有什么"来决定：

1. **只加不猜**：声明要的地址，网卡上缺就加；已经有了什么都不做（幂等）。
2. **掩码不一致不静默改**：同一个 IP 掩码不同单独报出来让人定——改掩码会把同网段的
   路由行为一起改掉，而且这不是这台模拟器该替现场做的决定。
3. **只删自己的**：能删的只有"账本记过 + 现在真的在网卡上 + 声明已经不要了"三项同时成立。
   网卡上原有地址、DHCP 租约、`169.254.x` 自动地址一律不碰（`PrefixOrigin` 不是 Manual 的
   一律视为"不是我们的"），**只报数**。

另有一条防"撞了别人"的前置检查：同一个 IP 不能同时挂在两块网卡上（不先查出来，直接加只会
得到一句英文的 `The object already exists.`）。想加的地址已经配在别的网卡上时先报出来，
说清它在哪块网卡上，让人决定是迁走还是换 IP。

### 14.4 启动挂钩：起从站/开界面时自动配网络

`net/startup.py`。四条口径，每条都对应一次现场判断：

1. **没有声明文件就一声不吭**（IP 是环境相关的，"这个工程没声明网络"是合法状态）；声明文件
   **坏了**（半截 JSON）报一句原话——那是真写错了，但同样不拦启动。
2. **`SIMBENCH_NET_AUTO=0` 是总闸**（`--no-net` 落到同一个开关上）。测试与 CI 靠它保证
   **绝不碰真网卡**：用例装载的是 `examples/` 下的点表，而现场那份点表旁边早晚躺着一份
   `.net.json`——闸门一松，跑一次 pytest 就会真去改机器上的网卡地址
   （`tests/conftest.py` 默认关掉）。
3. **已经一致时不提权**：先只读对账，只有真要动系统才调 `net up`。每次启动弹一次 UAC，
   用户很快就会把整个功能关掉。
4. **网络这一步永远不许把模拟器拦在门外**：读系统失败、UAC 被取消、地址加不上，都只落一行话，
   不改退出码（`net/startup.py` 里那处 `except Exception` 是全项目唯一一处兜底，理由写在那里）。
   反过来"网卡没配好"和"点表不合法"不是一个量级：后者才该拒绝启动。

无头 CLI 与 GUI 两条启动路径都接（`__main__._prepare_network` / `gui.app._prepare_network`）；
`--check`/`--list` 这类只读命令**不接**（它们的语义就是"绝不改系统"）。

`--no-net` 只管"别自动配"，**不影响监听**：声明里的地址照样要听（那是"照着声明起服务"，
不是"改系统"）。真没配好时会带着指路的话拒绝启动（见 §14.5 末条），而不是安静地只起一个
本机端口——后者现场看到的是"主站连不上，日志里却在 listening"，最难查。

### 14.5 监听：主站到底该连哪

主站连的是**设备那一侧**的地址（声明说 `10.136.107.240:502`，主站就往那儿连），而通道自己的
`host:port` 通常是点表写给本机自测用的（`0.0.0.0:5020`）。所以后端在通道自己的监听之外，
把这一路声明里的地址也听上：

- `net/endpoints.py` 算"额外听哪些"：**按设备名配对**（`net init` 就是按点表的设备名生成的
  声明）。按顺序配会在点表增删一台设备之后把 IP 整体错位一格，而那种错配是静默的，现场只
  看到"设备读数全不对"。名字对不上的不当成错误，但**也不许静默**：启动挂钩与网络页各报一句。
- 三条"不重复听"的口径，否则启动就撞上自己刚绑的地址：通道自己那个端点不重复挂；通道 host
  是通配（`0.0.0.0`/`::`/空）时同端口的声明地址已被它盖住；同一 `(ip, port)` 只挂一次。
- 多挂的监听**共用同一份 datastore**（实现见 §12 那条"一个 server 只按一个地址建监听"）：
  主站从 A 地址写、从 B 地址读得到，界面读回也是同一个值。代价是一路通道里任意地址都能寻址
  到这一路的全部 unit id——v1 接受这条口子（Modbus 本来就是按 unit id 寻址，主站只读自己那台）。
- 地址不在本机（`net up` 还没跑）与端口被占分开报：前者指路去跑 `net up`，后者让人去停那个
  进程。启动失败时把已经支起来的监听全收干净（包括额外那几个），不许留半个后端占着端口。

### 14.6 界面

`gui/net_panel.py`（下栏第三个标签页"网络"）：现状 + 差异 + 「应用（提权）」。三条口径：
文字进只读文本框（会自动换行的标签一换行就改高度，布局恒等那几条断言立刻红）；
**刷新只读**（走 `startup.status()`，总闸关着也照样能看——总闸管的是"启动时别自动动手"，
不是"不许看"）；**切到网络页才读系统**（读一次是几次 PowerShell，秒级，"打开点表"不许
跟着等）。「应用」走 `net up` 那条路，结论一句话落状态栏。收回地址（`net clean`）不放在
界面里：那是要人逐条确认的动作，留在命令行。

### 14.7 未决

- **主站跑在哪台机器上**。专用网卡是系统自带的**环回**适配器（`netloop.inf` 的 KM-TEST），
  它不接任何物理网络，所以这些 IP 只在**本机**可路由：主站（平台 / 网关 / 调试工具）与模拟器
  在同一台机器上时，按 `设备IP:端口` 连得通——本机实测，真主站连 `10.136.107.240:18823`
  读到了 COVI-1 的 CO 浓度 `12.5`（float32 CDAB）与固件版本 `7`，启动那行同时报出
  `listening on 127.0.0.1:18824、10.136.107.240:18823`。要让**另一台**机器连上，得靠现场
  自己的路由或隧道（本项目名字里的 tunnel 就是干这个的），那属于现场的网络设计。
- **"以太网 5"上那 79 个地址要不要迁到专用网卡**。同一个 IP 不能同时挂在两块网卡上，所以
  只要那 79 个里有声明要用的地址，`net up` 就会报"已被占用"而不是硬加。迁不迁是现场决定
  （那是别人手工配的地址，工具不替现场做主）。
- **502 端口与防火墙**：Windows 防火墙拦不拦首次监听 502，取决于现场策略；本机自测用
  `127.0.0.1:5020` 那一套不受影响（见 §13 的同一条）。
- 声明里 `port` 只影响监听，`net up`/`check` 不碰端口（端口不是操作系统的网络配置）。
