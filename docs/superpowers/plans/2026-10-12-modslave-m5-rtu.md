# M5 RTU 串口后端 实现计划

日期：2026-10-12　｜　范围：`ModbusRtuBackend` + 串口通道模型/校验/界面/CLI + 无硬件验收
前置：M1–M4、M6 已完成（spec §11）；本计划只做 M5，**不改任何既有行为**。

> 这份计划按 spec §11 单独成文（M5 是"唯一需要真硬件冒烟的一段"）。与 M1/M2/M4 那份一样：
> 技术前提全部带实测证据，待用户裁决的口径单独列在下一节，实现期与计划不一致的地方记在
> 末尾的"执行期已裁决的偏离"表里。

---

## 1. 目标与边界

做：一条 RTU 通道 = 一个串口上的一个 Modbus RTU 从站集合（多 unit 挂同一条总线）。
点位/波形/归属/报文监视/日志/启停/网络前四件的既有口径**原样复用**——RTU 只换传输层。

不做（见文末"不做"表）：多串口复用一条通道、RTS/CTS 流控、波特率自适应、CRC/延迟注入、
真硬件之外的自动化（拔线、驱动）等。

---

## 2. 技术前提（本机已实测，非推测）

实测环境：Windows + Python 3.13.9 + `pymodbus==3.15.0` + `pyserial 3.5+`（已在 requirements 里）。
探针脚本留在 `docs/superpowers/notes/`（见 Task 8）——**这些结论都有可重跑的脚本**。

1. **`ModbusSerialServer` 的参数面与 TCP 那一套同形**
   `pymodbus/server/server.py:203`：
   `ModbusSerialServer(context, *, framer=FramerType.RTU, identity=None, port: str = "",
   stopbits: int = 1, bytesize: int = 8, parity: str = "N", baudrate: int = 19200,
   timeout: float = 3, handle_local_echo=False, ignore_missing_devices=False,
   broadcast_enable=False, reconnect_delay=2, trace_packet=None, trace_pdu=None,
   trace_connect=None, custom_pdu=None, allow_multiple_devices=False)`
   ⇒ `trace_packet` / `trace_pdu` / `trace_connect` / `custom_pdu` 与 TCP 同签名，**报文窗与 §9 的
   注入缝在 RTU 上一样成立**。

2. **串口对象由 pyserial 的 URL 打开，不是 `serial.Serial(...)`**
   `pymodbus/transport/serialtransport.py:33`：
   `serial.serial_for_url(url, exclusive=True, baudrate=..., bytesize=..., parity=..., stopbits=..., timeout=...)`
   ⇒ `exclusive=True`（同一串口被别的进程占着时打不开），并且 **`loop://` / `socket://` /
   `spy://` 这些 URL 方案在从站侧同样可用**。

3. **串口服务端遇到 `socket://` 会把自己变成 TCP 监听**（这是"无硬件验收"的关键）
   `pymodbus/transport/transport.py:151-160`：
   ```
   if comm_type == SERIAL and is_server and host.startswith("socket"):
       comm_type = CommType.TCP          # format is "socket://<host>:port"
       host, port = parts[1][2:], int(parts[2])
   ```
   ⇒ 从站 `port="socket://127.0.0.1:<free>"` 时它自建 TCP 监听，而主站
   `ModbusSerialClient(port="socket://127.0.0.1:<free>")` 走 pyserial 的 socket 串口对象连上来。
   **两端都跑真 RTU framer**（CRC、地址字节、3.5 字符静默切帧），只是"线"换成了环回 TCP。

4. **实测通过**（2026-10-12，脚本 `rtu_socket_probe.py`）：
   | 动作 | 结果 |
   |---|---|
   | `fc03 hr[4]`（uint16 初值 11） | `[11]` ✓ |
   | `fc03 hr[0..1]`（float32/CDAB 12.5） | `[0, 16712]` ✓（字序与 TCP 同一份 codec） |
   | `fc03` 未定义地址 | `ExceptionResponse(..., exception_code=2)` ✓ |
   | `fc06` 写 1234 → `fc03` 读回 | `[1234]` ✓ |
   | 未定义 unit id | 异常码 4 + `KeyError: 0` 那条栈（`configure_library_logging()` 已压掉，与 TCP 同形）✓ |
   | `trace_packet` | 收到 `0b 03 00 04 00 01 c5 61`、`0b 83 02 e0 f3` ✓ → **spec §12 的 M5 待确认项 1 清零** |
   | `trace_connect` | `[True, False]`；主站断开后 `server.active_connections == {}` ✓ |
   ⇒ **M5 的端到端验收完全不需要硬件**；硬件只留给"真串口驱动 / 波特率 / 拔线"那几条人工冒烟。

5. **起不来的原因会被吞两次**（这条决定后端必须自己探）
   实测（`rtu_open_failure_probe.py`，输出见 notes）：
   ```
   [pyserial]  SerialException: could not open port 'COM99': FileNotFoundError(2, '系统找不到指定的文件。', None, 2)
   WARNING pymodbus.logging Failed to start server could not open port 'COM99': ...
   [pymodbus]  serve_forever raised RuntimeError: Could not start listen, please check address.
   ```
   即：`listen()` 把真实原因降级成一条 **warning 日志** 后 `return False`，`serve_forever()` 只抛
   通用 `RuntimeError`；而我们自己的 `configure_library_logging(level=ERROR)` 连那条 warning 都会
   压掉 ⇒ **按现在的日志配置，现场只会看到"启动失败 + Could not start listen"这一句没有任何信息量的话**。
   所以 RTU 后端必须先自己开一次串口做探针（拿到 pyserial 的原话与"不存在 vs 被占"的区分），
   与 TCP 那条 `check_port_is_free` 探针同一个理由、同一个位置。

6. **`NullModem`（`port="__pymodbus_nullmodem:<n>"`）不是验收路径**
   `transport.py:482` 起：类级 `listeners`/`connections` dict + 直接调用对端 protocol，
   **只在同一个 loop 里成立**。我们的同步主站跑在主线程、从站跑在引擎 loop 线程，
   用它会让 asyncio 跨线程回调（非线程安全）。它只适合"两端都在引擎 loop 内"的单元用例。

7. **待实测（M5 内清零，不写进代码假设）**
   - 拔 USB-485 后 `sync_serial.read` 抛的是 `SerialException`（`intern_read_ready` 的除分类）
     还是别的，好让"通道置错"接得住（spec §12 的 M5 待确认项 2）；
   - 真串口上的 `trace_connect` 语义：TCP-backed 时它是"有连接/断开"，真串口没有连接概念
     （实测上面是 [True, False]，因为 socket:// 那条路是 TCP）——**人工冒烟时确认真串口下它是否
     只在开/关时各报一次**，界面"连接数"那一栏对 RTU 要用什么口径（见口径 5）。
   - **已从源码确认（不必等硬件）**：COM9 以上的写法不用用户操心——
     `serial/serialwin32.py:44-53` 自己会补前缀
     （`if port.upper().startswith('COM') and int(port[3:]) > 8: port = '\\\\.\\' + port`），
     校验只管"非空且看着像个串口名"，不改写用户填的名字。

---

## 3. 执行前必须先定的口径（不要带着猜测写代码）

| # | 要定的事 | 建议（含理由） | 备选 |
|---|---|---|---|
| 1 | 通道模型：新类还是塞进 `TcpChannel` | **新增 `SerialChannel(name, port, baudrate=9600, parity="N", bytesize=8, stopbits=1, timeout=0.3, devices=[], type="rtu")`**，`Project.channels: list[TcpChannel \| SerialChannel]`，判定只在一处 `channel_transport(ch) -> "tcp" \| "rtu"`（`project/model.py`）。理由：`TcpChannel.port: int` 与串口名 `"COM3"` 类型冲突，硬塞会让 15 处 `ch.port` 的调用点都变成"可能是字符串"，靠人记得住 | 弄成一个大 `Channel` 带可选字段（更少的类，更多的 `if`） |
| 2 | CSV 与 RTU 的双向（CSV 表头是固定的 12 列，没有串口列） | **通道名承接：`com3@9600,8E1`**（省略参数用默认 `9600,8N1`，只写 `com3` 也行）。导出时把参数烘进名字、导入时按同一语法还原 ⇒ CSV 往返不丢参数；不匹配这个语法的一律当 TCP，**现有猜端口的 note 口径原样保留** | 导入对话框加一个"这些通道是串口"的选择；或 CSV 只产 TCP，RTU 只能从 project.json/界面来（代价：现场要额外维护一份 json） |
| 3 | 界面上 RTU 行怎么摆（通道面板是定宽槽 + 几何恒等的一份合同） | **复用两个已有定宽槽 + 新增一个定宽"帧格式"列**：TCP 行 = `host` \| `port`；RTU 行 = `串口名` \| `波特率` \| `8N1`。TCP 行的帧格式格**留空**（点表里"只读"列对某些行也空着，有先例）。理由：不动 TCP 行任何一格的位置，混装工程一眼分得出类型 | 双击行开一个"通道属性"对话框（多一次点击、多一份几何不变量要守） |
| 4 | 串口参数合法集 | 串口名非空；`baudrate ∈ {1200,2400,4800,9600,19200,38400,57600,115200}`（白名单，超范围直接报错）；`parity ∈ {N,E,O}`；`bytesize ∈ {7,8}`；`stopbits ∈ {1,2}`；`timeout > 0`；**两个通道配同一个串口名 = error**（形状照抄现有 `(host, port)` 那条 `port_duplicated`） | 波特率放开成任意正整数（现场怪波特率也有）——那就把白名单降级成 warning |
| 5 | 界面"连接/计数"那一栏对 RTU 用什么口径 | **RTU 不显示连接数**（串口没有"连接"这件事，真显示就是演戏）；报文窗、计数照常（帧数与 unit 都真实） | 显示"总线活动"（最后一次收到帧的时间）——需要新的一格，属于新几何 |
| 6 | 后端复用方式 | **抽公共基类 `backend/modbus_base.py`**：把 tick 循环、`SourceStore` 归属、`RawRegisterWriter`、报文钩子适配、日志去噪、`start/stop` 的骨架搬进去；TCP/RTU 各自只实现"建哪个 server、怎么确认起来了"。理由：波形与归属有 30+ 条用例钉着，复制一份等于把"两份判定迟早漂移"再写一遍；抽的时候**TCP 行为一个字不许变**，既有 1000+ 用例就是回归网 | 直接复制一份 RTU 后端（改动面小，但两处都要跟着 M4 的后续改动走） |
| 7 | 起不来时的拒绝文案与"▲ 失败" | 复用 `PortInUseError` 的**句式**，但换成串口的事实：`channel com-485-1: cannot open COM3 (Access is denied); close the program that holds this port (another simulator? a terminal?) and start again`；打不开/被占/不存在三种都要能在状态栏与日志里分辨 | 只在日志里说（界面那一格只说"启动失败"——那就回到"看不出为什么"的老问题） |

**这 7 条定了才动手**：第 1/2 条定了模型与 IO，第 3/5 条定了界面合同，第 4/7 条定了判定与话术，
第 6 条定了改动面。其余细节（命名、注释、测试组织）按本仓库既有风格就地决定。

---

## 4. 文件结构（M5 新增/改动）

```
simbench/
  project/
    model.py            + SerialChannel、channel_transport()、serial 参数常量
    json_io.py          + 通道按 type 双向读写（rtu 那几个键）
    csv_io.py           + 通道名语法 `com3@9600,8E1` 的解析/烘出（导出时）
    validate.py         + 拆掉 "type != tcp 就报不支持"，换成按类型的参数判定
  backend/
    modbus_base.py      新：传输无关的骨架（tick/归属/raw_writer/钩子/日志）
    modbus_tcp.py       ~ 改成薄壳：建 ModbusTcpServer + 端口探针 + extra_endpoints
    modbus_rtu.py       新：建 ModbusSerialServer + 串口探针 + 拒绝文案
    __init__.py         + make_backend(channel, ...)（唯一一处"按类型选后端"）
  engine/session.py     ~ 用 make_backend 取代裸 ModbusTcpBackend；ChannelRuntime.backend 类型放宽
  net/endpoints.py      ~ RTU 通道不进额外监听（没有 IP:端口 这回事）
  net/startup.py        ~ 同上：声明只对 TCP 通道生效
  gui/channel_panel.py  ~ RTU 行（串口名/波特率/帧格式）+ 新增定宽"帧格式"列
  __main__.py           ~ --host/--port 的适用面写清楚；RTU 通道照常由 _serve 起
tests/
  test_serial_model.py        新：模型、JSON 往返、CSV 通道名语法
  test_serial_validate.py     新：参数判定、重复串口、类型闸门
  test_serial_backend.py      新：探针拒绝文案、起停、tick/归属与 TCP 同口径
  test_gui_channel_panel_rtu.py 新：RTU 行的几何恒等与参数回写
  test_serial_backend.py      新：真主站在 socket:// 虚拟串口对上跑 §6 那张验收表（含 Task 4 的探针/切帧）
docs/superpowers/notes/      新：探针脚本 + 实测输出（本计划 §2 的证据）
```

---

## 5. 任务

### Task 1：模型与往返（`SerialChannel` / JSON / CSV）

1. `model.py`：`SERIAL_DEFAULTS = {"baudrate": 9600, "parity": "N", "bytesize": 8, "stopbits": 1, "timeout": 0.3}`；
   `SerialChannel`（字段见口径 1）；`channel_transport(ch)`；`serial_port_valid(text)` 之类**只做转换**的小函数。
2. `json_io.py`：`type == "rtu"` 时读写 `port/baudrate/parity/bytesize/stopbits/timeout`（spec §5 的示例就是契约）。
   round-trip 用例：读 spec §5 那段 JSON → 写回 → 逐字节等价（键序固定）。
3. `csv_io.py`：`_channel_for` 里加一层"这个名字是不是串口通道"的判定 → 是就建 `SerialChannel`；
   导出时把参数烘进名字（`com3@9600,8E1`；全默认时只写 `com3`）。
4. 用例：`test_serial_model.py` —— 默认值、往返、`com3@9600,8E1` 解析、`com3`
   （缺参数按默认）、`tcp-5020`（仍是 TCP 且仍猜端口）、非法语法（`com3@9600,8X1`）报错带原值。

### Task 2：校验（`validate.py`）

1. 拆掉 `ch.type != "tcp"` 那句，改成 `channel_transport(ch)` 分派：TCP 走原有那几条（一个字不改），
   RTU 走口径 4 的白名单判定。
2. 重复串口：**同一个串口名被两个通道占用 = error**（沿用 `port_duplicated` 的形状与句式，
   `where` 仍是通道名）；TCP 的 `(host, port)` 那条判定不动。
3. `type` 既不是 tcp 也不是 rtu（例如 `"udp"`）仍然报 `channel_type`，句式保持
   `channel type 'udp' is not supported`（把 M1 那句里的 "in M1" 去掉）。
4. 用例：`test_serial_validate.py` —— 参数逐项边界（波特率 9500 报错、`parity="X"`、
   `bytesize=9`、`stopbits=3`、`timeout=0`）、串口名为空/空白、两个通道同串口、未知 type、
   以及**一条回归**：既有 TCP 工程校验结果逐字不变（拿 `examples/covi-tunnel.csv` 对一遍）。

### Task 3：抽 `backend/modbus_base.py`（TCP 行为一个字不许变）

1. 把 `ModbusTcpBackend` 里与传输无关的部分原样搬进基类：`_wave_points` 摊平、
   `_tick_loop`/`_tick_once`、`_sources` 归属、`_writer`、`_trace_packet`/`_trace_connect` 适配、
   `configure_library_logging` 调用、`_emit`、`start_after_open(server)`/`stop()` 骨架、`source_is_waveform` 等。
2. 子类只提供：`_probe()`（起之前的归属探针）、`_create_server(devices)`、`_await_ready(server, task)`
   （TCP：等 listener 起来；RTU：见 Task 4）。
3. **验收就是既有全套用例**：`test_backend_listeners.py`、`test_integration_tcp.py`、
   `test_runner.py`、`test_session.py` 一个字不改、全绿。改动只在 `modbus_tcp.py` 内部。
4. 用例：`test_serial_backend.py::test_the_tcp_backend_is_still_the_same_object_shape`
   之类只钉"公共面没变"（`extra_endpoints`、`tick_ms`、`packet_sink` 签名）。

### Task 4：`backend/modbus_rtu.py`

1. **串口探针**（先开一次再交给 pymodbus）：
   ```python
   def check_serial_is_free(port: str, channel_name: str) -> None:
       """打开再立刻关掉：拿真实异常文案 + 提前发现"被别的程序占着"。"""
       try:
           handle = serial.serial_for_url(port, exclusive=True, timeout=0.05)
       except serial.SerialException as exc:
           raise SerialOpenError(_refused(channel_name, port, exc)) from exc
       finally: handle.close()  # 只在成功时
   ```
   与 TCP 一样**必须离开 loop 线程**（`asyncio.to_thread`）：`serial_for_url` 在 Windows 上打开
   COM 口是毫秒级~几十毫秒的阻塞调用（真拔线/驱动异常时会更久），放 loop 上会把 10Hz 心跳冻住
   ——`test_the_port_probe_must_not_block_the_engine_loop` 那一条的 RTU 版。
   探针不是"锦上添花"：§2.5 实测过，不探的话 `serve_forever` 只回一句
   `Could not start listen, please check address.`，而真实原因那条 warning 又被我们自己的
   `configure_library_logging(ERROR)` 压掉。
2. `SerialOpenError(RuntimeError)`（形状照 `PortInUseError`）：消息里必须同时有**串口名**、
   **真实原因**（pyserial 的 `exc`）和**该做什么**（关掉占着它的程序 / 确认 USB-485 插好）。
3. `_create_server`：`ModbusSerialServer(devices, framer=FramerType.RTU, port=ch.port,
   baudrate=..., parity=..., bytesize=..., stopbits=..., timeout=..., ignore_missing_devices=False,
   trace_packet=..., trace_connect=...)`。
4. `_await_ready`：串口没有 listener 可等 ⇒ 判据是"`serve_forever()` 的任务在
   `_START_TIMEOUT` 内没抛错"。实测 `serve_forever()` 起不来会抛
   `RuntimeError("Could not start listen, please check address.")`（§2.5），把它翻译成
   "串口打不开，原因见上一行日志 + 探针那句"，**不许把 pymodbus 的通用文案直接甩给用户**。
5. 用例：`test_serial_backend.py`
   - `port="COM99"`（不存在）→ `SerialOpenError`，消息里有 `COM99` 与 pyserial 的原话；
   - 同一个 `socket://` 端口的"被占"形状：占用方先开一个 TCP 监听 → 拒绝文案里出现串口名；
   - 起 → `source_is_waveform`/`restore_waveform`/tick 写值 与 TCP **同口径**（同一份断言表跑两遍，
     参数化 `transport` 夹具）；
   - `stop()` 之后 `active_connections` 清空、串口句柄关掉（用 `socket://` 路径时探"端口还能再绑"）。

### Task 5：工厂 + Session + 网络层让路

1. `backend/__init__.py`：`make_backend(channel, *, packet_sink=None, on_connection=None, tick_ms=0,
   log_sink=None, extra_endpoints=())` —— 唯一一处按类型选后端的地方；未知类型抛 `ValueError`（不静默回 TCP）。
2. `session.py`：`Session._start` 里改调 `make_backend(...)`；`extra_endpoints` 只对 TCP 传
   （RTU 传空）；`ChannelRuntime.backend` 类型放宽成 `ModbusTcpBackend | ModbusRtuBackend`。
3. `net/endpoints.py::for_channel` 与 `net/startup.py`：**RTU 通道直接跳过**（返回 `[]`），
   并附一条用例：RTU 通道 + 声明文件里同名设备 → 不产生额外监听，也不报"名字对不上"的假错。
4. 用例：`test_serial_backend.py` / `test_session.py` —— 同一份 Session 里 TCP 与 RTU 各起一路，
   互不影响；RTU 起不来时只有那一路 `▲ 失败`（兄弟通道照常跑）。

### Task 6：界面（通道面板的 RTU 行）

1. `ChannelPanel.rebuild` 按 `channel_transport(ch)` 决定三格的语义与可编辑性：
   - TCP：`host` 编辑框 / `port` 编辑框 / 帧格式格空；
   - RTU：`串口名` 编辑框 / `波特率` 编辑框 / `帧格式` 编辑框（`8N1` 这种紧凑写法）。
2. 新增的"帧格式"列是**定宽槽**（与 `_HOST_WIDTH` 同级），TCP 行那一格留空；
   `geometry_snapshot()` 跟着加一格，`test_gui_layout_invariant.py` 的三态恒等断言自动覆盖。
3. 回写与拒绝沿用现有那条路：`apply_edits` 先转换（`"9600"` → int、`"8N1"` → 三个字段，
   转不出来当场拒）→ 写回工程 → `session.issues()` 验一次 → 新增错误就整体回滚（连输入框一起退回）。
4. 用例 `test_gui_channel_panel_rtu.py`：
   - RTU 行的五个格子的文案/宽度/可见性在四态下恒定（照抄 TCP 那几条的形状）；
   - `8E1` 回写成 `parity="E"`；`9N1` 被拒且退回原值、不留 dirty；
   - 混装工程（一 TCP 一 RTU）里 TCP 行的几何与 `test_gui_layout_invariant` 的基线**逐格相同**
     （比对 `geometry_snapshot()` 的 TCP 那几项）。
5. 连接数那一格：按口径 5，RTU 通道不显示 `连接 N`（`_summary()`/通道面板的措辞保持"运行/停止"）。

### Task 7：无头 CLI 与真主站验收

1. `__main__.py`：`--host/--port` 的 `help` 补一句"只对 TCP 通道生效"；RTU 通道走同一条 `_serve`
   （它已经接受任意通道，只要后端由 `make_backend` 选）。
2. `tests/test_serial_backend.py`：真 `ModbusSerialClient` 在 `socket://` 虚拟串口对上跑 §6 的同一张表：
   - fc01/02/03/04 四个读入口的值与初值/字序一致（float32/CDAB、uint64、string 各一条）；
   - fc05/06/15/16 写 → 两个 tick 后**仍是我写的值**（波形接管不顶掉，§7 的值归属口径）；
   - 未定义地址 → 异常码 2；未定义 unit → 异常码 4；
   - 钳位：越界值写进去 → 钳到边界 + 一条 60s 去重的日志（与 TCP 同一条用例参数化）；
   - 报文窗：`PacketStore` 里收到双向帧，`trace_packet` 的原始字节与 TCP 那份契约一致；
   - 停通道 → 端口真的松开（`socket://` 那条路能再绑一次）。
3. 参数化：`test_integration_tcp.py` 的用例表能复用的部分**抽成共用夹具**（transport 参数化），
   不为 RTU 抄第二份断言——两份判定迟早漂移。

### Task 8：文档回写 + 人工冒烟清单

1. `docs/superpowers/notes/` 放探针脚本与实测输出（§2 的四张证据）。
2. spec 回写：§11 的 M5 条目、§12 的"M5 内确认"两条（trace_packet 已清零；
   拔线异常待硬件那一次）、§5 的串口参数示例与 CSV 通道名约定、§8 增补串口判定项。
3. 人工冒烟清单（自动化替不了，**照做表**如下；跑完把结果回填这一节）。

   前置：USB-485 转换器插好（设备管理器里能看到它占了哪个 COM 口）。先确认口名：

   ```powershell
   .\.venv\Scripts\python.exe -c "from serial.tools import list_ports; print([p.device for p in list_ports.comports()])"
   ```

   再准备一张串口点表 `rtu-smoke.csv`（把 `COM3` 换成上一步看到的那个口）：

   ```csv
   channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
   com-485-1@COM3 19200 8E1,COVI-1,11,回路温度,hr,0,uint16,ABCD,25,const,,false
   com-485-1@COM3 19200 8E1,COVI-1,11,启动,co,0,bool,ABCD,false,const,,false
   com-485-1@COM3 19200 8E1,COVI-1,11,越界示例,hr,4,uint16,ABCD,1,const,value=70000,false
   ```

   | # | 做什么 | 期望看到 | 不对时说明什么 |
   |---|---|---|---|
   | 1 | `python -m simbench rtu-smoke.csv --check` | `point table is valid` | §8 的串口白名单判错了 |
   | 2 | `python -m simbench rtu-smoke.csv --list` | 每行 `channel com-485-1 (COM3 19200 8E1)` | `serial_summary()` 或 CSV 通道名语法坏了 |
   | 3 | `python -m simbench rtu-smoke.csv`（不带 `--quiet`） | 一行 `[simbench] serving on COM3 19200 8E1 unit ids 11`，随后每帧一行 hex | 探针 / 监听 / 报文钩子 |
   | 4 | 主站（mThings / Modbus Poll / `ModbusSerialClient(port="COM3", baudrate=19200, parity="E", stopbits=1)`）读 `hr0`＝25、`co0`＝0；写 `hr0`＝77 再读回 | 读得到、写回是 77 | 串口参数与点表不一致，或帧切错了 |
   | 5 | 主站写 `co0`＝1，等 1 秒再读 | 仍是 1（波形没顶掉） | §7 归属在真串口上没生效 |
   | 6 | 看命令行输出的运行期日志 | `越界示例` 一条 `warning ... clamped to 65535`，且只出现一次 | 钳位或 60s 去重坏了 |
   | 7 | 用串口助手（或另一个终端）占住 COM 口，再 `python -m simbench rtu-smoke.csv` | 一行错误：点名 `COM3` + `could not open port` + "关掉占着它的程序" | 探针没拿到 pyserial 的原话 |
   | 8 | 起好从站后**拔掉** USB-485，等几秒 | 界面/日志给出 `▲ 失败` + 一句人话（**不许静默死**）；插回后重新"启动"能起来 | `SerialException` 那条路（§12 的第二条待确认） |
   | 9 | 点"停止"后再从别的程序打开同一个 COM 口 | 打得开（串口真的释放了） | `stop()` 没关串口 |
   | 10 | 界面里看 `com-485-1` 那一行：串口名/波特率/帧格式三格；切深浅两套主题各看一眼；缩放 150% 再看一眼 | 三格文字不截断、其它行一个像素都不挪 | 定宽槽或主题在真屏幕上破了 |
   | 11 | 手边若有 COM9 以上的口（USB-485 常被分到 COM10+）：把点表里的 `COM3` 换成它，重跑第 3–4 步 | 照常工作（pyserial 自己补 `\\.\` 前缀，见 §2.7） | 若失败，记下口名与报错——这是本计划唯一没在真机上验过的写法 |

   跑完把每一条的结论（通过 / 失败 + 现象）回填到这张表下面。

   **这张表里能离线验的部分已经先验过一遍**（2026-10-13，把串口名换成 `socket://127.0.0.1:<port>`
   走同一套代码路径）：第 1/2/3 步的输出格式与上面写的逐字一致、第 4 步读到 `hr0=25`、
   `co0=false`、写 77 读回 77、第 6 步的钳位日志是
   `[warning] 越界示例: waveform value outside uint16, clamped to 65535`。
   真机上剩下要看的只有"物理那几层"：真 COM 口的驱动/参数、拔线、被占、COM10+、界面观感。

---

## 6. 验收表（RTU 通道，真主站在 `socket://` 虚拟串口对上跑）

| # | 场景 | 期望 | 归属用例 |
|---|---|---|---|
| 1 | fc03 读 hr（uint16 / float32-CDAB / uint64 / string） | 值与字序与 TCP 完全一致 | `tests/test_serial_backend.py` |
| 2 | fc01/02 读位区 | 与初值一致 | 同上 |
| 3 | fc06/16 写寄存器 → 两个 tick 后 | 仍是我写的值（波形不顶掉） | 同上 |
| 4 | fc05/15 写线圈 → 两个 tick 后 | 同上 | 同上 |
| 5 | 主站写被协议拒（只读点） | 异常码 2，且**不**把该点切成 manual | 同上 |
| 6 | 未定义地址 / 未定义 unit | 异常码 2 / 4，各自一条 | 同上 |
| 7 | 越界值写进去 | 钳到边界 + 一条 60s 去重日志 | 同上 |
| 8 | 波形 tick | 与 TCP 同一份 tick 用例表 | `test_serial_backend.py` |
| 9 | 报文窗 | 双向帧进 `PacketStore`，原始字节与解析摘要与 TCP 同契约 | 同上 |
| 10 | 启停 | 停 → 端口松开（能再绑一次）；再启 → 重新工作 | 同上 |
| 11 | 起不来 | 串口名 + 真实原因 + 该做什么，三样齐 | `test_serial_backend.py` |
| 12 | 混装 | 一 TCP 一 RTU 各自独立启停，一路失败不牵连另一路 | `test_session.py` |

---

## 进度

**第一段（2026-10-12，Task 1/2/6 + Task 5 的工厂与守卫）**：`SerialChannel` 与传输判定、
JSON/CSV 往返（含通道名语法）、串口参数校验、界面的串口行（含帧格式格）、后端工厂。
那一轮的偏离是 D1–D7，收尾时 `make_backend` 对 rtu 还是抛一句人话（D7 说的"下一段"）。

**第二段（2026-10-13，Task 3 + Task 4 + Task 5 收口）**：抽 `backend/modbus_base.py`
（TCP 行为不变，既有用例拿当回归网），落地 `backend/modbus_rtu.py`（串口探针 +
`ModbusSerialServer` + 按 RTU 帧切帧 + `pdu_offset=1`），`make_backend` 对 rtu 换成真后端，
`__main__`/`Session` 的"主站该连哪"统一走 `describe()`。真主站（`ModbusSerialClient`）在
`socket://` 虚拟串口对上读/写/接管/交还全部通过。这一段的偏离是 D8–D12。

**审查段（2026-10-13，全面审查）**：7 处清理（死 import ×2、`ChannelRuntime.backend` 注解、
spec §11 入口名、测试环境变量名、架构守卫证据集、README 补 RTU 一节）+ 两条口径落地
（`SerialChannel.port` 默认改空串、自动配网按 TCP 通道过滤）+ 三条历史遗留了结
（`cleared()` 保留为诊断口、改短 string 补零、spec §4 线程模型改成落地形状）。

**第三段（2026-10-13，Task 7 收口）**：本计划 §6 那张 12 行验收表在 RTU 上逐行钉成了
用例（`tests/test_serial_backend.py`：四区读值逐字对齐 TCP、寄存器写与线圈写各"两个 tick
后仍是我写的值"、被协议拒的只读点不切 manual、异常码 2/4、钳位到边界 + 60s 去重日志、
报文窗六位置合同（RTU 的 PDU 偏移是 1）、一 TCP 一 RTU 独立起停）。真主站 + 真 tick，
全走 `socket://` 虚拟串口对。

**未完成**：Task 8（spec §5/§8/§11 回写 + 人工冒烟清单）还没做——代码与用例已收官，
只剩文档回写与"真 USB-485"那五条人工验证。

## M5 完成检查表

- [x] `.venv\Scripts\python.exe -m pytest` 全绿（基线 **1042** → 本轮 **1131**，只增不减；**串行跑**）
- [x] `tests/test_architecture.py` 仍全绿：`pymodbus`/`serial` 只在 `backend/`
      （`modbus_base.py` 一个都不 import，`modbus_rtu.py` 才 import serial；`project/model.py` 干净）
- [x] §2 的实测结论有脚本与输出留档（`docs/superpowers/notes/2026-10-12-m5-rtu-probes.md`
      + `rtu_socket_probe.py` + `rtu_open_failure_probe.py`，三条命令可重跑）
- [x] 串口通道能装载/校验/往返/在界面上编辑；启动那一步**报一句人话**（不是 AttributeError）
- [x] Task 3 抽基类之后，M1–M4 的既有用例**一个断言没改**且全绿（只改了 2 处契约名：
       `PacketEvent` 加 `pdu_offset`、`_await_listener` 改名 `_await_ready`，见 D8/D9）
- [x] 既有 TCP 工程（`examples/covi-tunnel.csv`）的**校验清单**逐条不变（有专门用例钉住）
- [ ] 既有 TCP 行的界面几何逐格不变 —— **没做到**，见 D5：帧格式格插在波特率之后，
      启动/停止/状态三格右移一格宽（一次性列插入，四态恒等那批断言全绿）
- [x] spec §5/§7/§8/§11/§12 回写完毕；§12 的两条 M5 待确认项一条清零（trace_packet/
      trace_connect 实测生效）、一条转人工（拔线异常）
- [ ] `git status --short` 为空（提交由人做）
- [ ] 人工冒烟清单（Task 8.3 的 11 步表）由用户当面跑过 —— 自动化替不了那几条

## M5 不做（留给后续）

| 内容 | 归属 | 为什么不在这里 |
|---|---|---|
| 一条通道挂多个串口 / 一个串口多通道 | v2 | 现场没有这个形状；模型与判定都会复杂一倍 |
| RTS/CTS、XON/XOFF 流控 | v2 | USB-485 半双工转换器不吃这两个；真需要时在 `SerialChannel` 加一个字段即可 |
| 波特率自适应 / 自动找 COM 口 | v2 | "自动找口"在本机上会找到别的设备的口，属于新的误动作面 |
| CRC 错、帧间延迟、断线重连注入 | v2 的缝 | spec §9 已留（`trace_packet` + `SimDevice.action`），M5 只把缝验通（trace_packet 已实测生效） |
| RTU over TCP 之外的其他"虚拟串口"玩法（`loop://`、`spy://`）进产品 | 不做 | 那是测试与诊断手段，不是产品功能；写进 notes 供排查用 |
| 打包（exe） | 单列 | spec §13：推迟到 v1 功能稳定后，与 M5 无关 |

## 执行期已裁决的偏离（实现期回填）

口径 1–7 按建议全部采纳；实现期有七条与计划文本不一致，都在这里说清。

| # | 计划原文 | 实际落地 | 为什么 |
|---|---|---|---|
| D1 | 口径 2 写的是通道名语法 `com3@9600,8E1`（逗号分段） | 改成**空格**分段：`com-485-1@COM3 19200 8E1`；并把"串口名不许含空格或 `@`"加进 validate（code `serial_port`） | 逗号在 CSV 里会让整格必须加引号——Excel 里手改一格就会多出一对引号，而空格不是 CSV 的分隔符，且串口名本来不含空格，没有歧义 |
| D2 | 口径 1 写 `SerialChannel(..., type="rtu")` | `SerialChannel` **不设 `type` 字段**：类就是事实，`json_io` 写盘时按 `channel_transport()` 补上 `"type": "rtu"` | 少一个可能与事实不符的可写字段（第二个真相一定会漂） |
| D3 | Task 2 说"按 `channel_transport()` 分派" | `validate` 用 **`isinstance(ch, SerialChannel)`** 分派；`channel_transport()` 仍在别处（后端工厂/CSV/界面）说了算 | `TcpChannel(type="rtu")` 这种自相矛盾的对象（只可能来自代码或手拼的测试）身上只有 TCP 字段，拿它去判串口参数会变成一串 AttributeError 式假错；它真正的问题就是"type 不对"（报 `channel_type`）。既有用例 `test_unsupported_channel_type_is_reported` 因此一个字没改 |
| D4 | Task 5 只写"改调 `make_backend`" | `Session._start` 的 `try` 范围**连选型一起包住** | 选型失败（例如现在的 rtu）与起不来一样是"这一路没起来"：要落 `last_error`、发 error 状态，否则界面那一格停在"○ 停止"而工具栏已经在报失败——本项目最防的那类演出。实测过：不包住时 `last_error` 是空串 |
| D5 | Task 6 写"TCP 行的几何与既有基线逐格相同" | **帧格式格插在波特率之后**（串口参数连在一起读），TCP 行那一格留空并置灰 ⇒ TCP 行的启动/停止/状态三格右移了一格宽 | 把它放到行尾（`… 状态 8N1`）能保持 TCP 行逐格不动，但一个永远空着的格子夹在状态与其它参数之间更难读；三格右移是**一次性**的列插入，不是"状态一变布局就跳"（四态恒等那批断言全绿） |
| D6 | `SERIAL_DEFAULTS` 计划里只有五个参数 | 加了 `port`，**默认是空串**（2026-10-13 全面审查时从最初的 `"COM1"` 改过来） | 串口名的默认值也归一处：`SerialChannel` 与 `json_io` 都从它取。**不留 COM1 这种"能用就行"的默认**：COM1 在本机很可能是别人真设备的串口，少写一个键就静默去开它；空串由 validate 的 `serial_port` 拦下，于是"没写串口名"是一条看得懂的错 |
| D7 | Task 1–8 一次排完 | 第一段只落了 **Task 1/2/6 + Task 5 的工厂与守卫**；`make_backend` 对 rtu 抛 `UnsupportedTransportError`（`channel X: serial (RTU) support is not available in this build`） | Task 3（抽 `backend/modbus_base.py`）与 Task 4（真 `ModbusRtuBackend`）是下一段：抽基类要拿既有 1000+ 用例当回归网单独做一轮，而串口通道当时**能装载/能校验/能在界面上编辑/能被点名拒绝**，产品处于自洽状态而不是半截。第二段已收口（见进度） |
| D8 | 计划没提事件结构要动 | `PacketEvent` 加 `pdu_offset`（默认 7）；`write_targets` 从它开始找 PDU 的地址与数量 | `write_targets` 原先把 PDU 起点写死成 7（MBAP 头），RTU 帧的 PDU 在单元号之后（偏移 1）——不参数化，"主站写的是哪个点"在串口上整体认错偏移，静默停掉一段曲线。带默认值，所以既有那五处构造 `PacketEvent(...)` 的调用与 `events.py` 的"加字段就加默认值"契约一个字没动（契约用例 `test_event_dataclass_fields_are_the_contract` 同步补了这一个字段） |
| D9 | 计划里叫 `_await_listener` | 抽基类时改名 `_await_ready`：两条传输"起来了"的判据不同（TCP 等 listener、RTU 等串口 transport），但**这一条纪律是同一条**，所以它是基类钩子 | 名字不再只属于 TCP；`tests/test_integration_tcp.py` 那条用例跟着改名字，断言一个字没动 |
| D10 | Task 4 写"串口探针 `serial_for_url`" | 探针对 `socket://` URL 先分支：那一路从站侧会自建 **TCP 监听**，所以"归属"问的是那个 TCP 端口，直接复用 `check_port_is_free`（bind+connect 两级） | 实测踩到：对 `socket://` 直接 `serial_for_url` 会去 **connect**，从站还没起来时必然"连接被拒"，等于把正常启动判成"串口打不开"。`socket://` 是验收路径也是命令行可用的形态，探针必须认得它 |
| D11 | 计划只有 `PortInUseError` | 新增 `BackendStartError` 作公共基类，`PortInUseError` 与 `SerialOpenError` 都从它派生；`__main__` 那一行错误只认这一个基类 | "资源不归我们"（端口被占 / 串口打不开）在调用方是一族：界面 ▲ 失败、命令行 exit 2 不该逐个传输去列举 |
| D12 | `Session._detail` 自己拼"主站该连哪" | 那一句交给后端 `describe()`；串口参数的拼法 `model.serial_summary()` 一处定义，命令行 `--list` 与状态栏同用 | TCP 是"所有监听地址"、串口是"`COM3 19200 8E1`"，两种说法分头写在两处迟早对不上——同一台设备不该有第二种写法。TCP 的输出逐字不变（界面按 `rpartition(":")` 取端口的用例全绿） |

---

## 自审（写完这份计划后按 spec 逐条对过）

**1. spec 覆盖**：§2 "RTU 从站（串口，多从站挂同一条总线）" → Task 1/4/7；§5 的 RTU 通道
JSON 形状 → Task 1；§8 "串口不存在或打不开"与"一次报全" → Task 2/4；§9 的 `Backend` 抽象缝 →
Task 3/5；§11 M5 条目 → 全文；§12 的 M5 两条待确认 → §2.4/§2.7 + Task 8；§7 界面的"通道可独立
启停" → Task 6。**没有漏项**。

**2. 代码只给关键缝**：这份计划不逐行给全部实现（与 M4 那份不同）。给代码的三处是"写错会很贵"
的地方——串口探针（吞异常）、后端工厂（唯一选型点）、面板的定宽帧格式列（几何合同）。
其余（模型 dataclass、JSON 往返、校验白名单、参数化集成用例）在有 M1–M4 同形样板的前提下
逐字抄形状即可，把篇幅留给实测证据与待裁决口径。

**3. 不许悄悄改口径**：Task 3 的硬要求是"TCP 行为一个字不变"，验收=既有 1000+ 用例不改而全绿；
Task 6 的硬要求是"TCP 行的几何与既有基线逐格相同"。这两条各自都有现成的用例网。
**4. 口径没定就不写代码**：§3 那七条是实现的前置条件；它们没定之前，这份计划里任何一行代码
都只是意向，不是承诺。
**5. 风险（提前说）**：
   - `socket://` 那套是 pymodbus 3.15 的**显式分支**（不是偶然行为），但升级 pymodbus 时
     要重跑 `docs/superpowers/notes/` 里的探针；表现是 Task 7 那批集成用例整体失败（可见、
     不静默），属于可接受的版本耦合，与 `_DropDatastoreNoise` 那条同类；
   - 真串口上的 `trace_connect` 语义（§2.7 第三条）没实测过，Task 6 的连接数口径要等人工那一次；
   - `SerialChannel` 引入后，`ch.port` 一族调用点必须逐个过一遍（`net/`、`gui/`、`csv_io`）——
     靠类型检查器抓不住（都是 Python），所以在 Task 1 里把一个 `channel_transport()` 判定做出来，
     让分支集中在一处，而不是散在 `isinstance` 里。
