# M5 RTU 技术前提实测记录（2026-10-12）

跑在：Windows + Python 3.13.9 + `pymodbus==3.15.0` + `pyserial 3.5+`（本仓库 `.venv`）。
用途：`docs/superpowers/plans/2026-10-12-modslave-m5-rtu.md` §2 的每一条结论都能在这里找到出处。
两个脚本都可以原样重跑（`testpaths = tests`，它们不会被 pytest 收集）。

```powershell
.\.venv\Scripts\python.exe -u docs\superpowers\notes\rtu_socket_probe.py          # 端到端
.\.venv\Scripts\python.exe -u docs\superpowers\notes\rtu_open_failure_probe.py    # 打不开串口
```

---

## 1. 端到端：真 RTU 从站 + 真 RTU 主站，不需要硬件

`rtu_socket_probe.py`：从站 `ModbusSerialServer(port="socket://127.0.0.1:<free>")` 起在专用 loop
线程（与产品的 `EngineRunner` 同形），主站 `ModbusSerialClient(port=<同一个 URL>)` 在主线程。

实测输出（2026-10-12，端口是每次随机的）：

```
server up on socket://127.0.0.1:13285
connect -> True
fc03 hr[4] (uint16 11) -> [11]
fc03 hr[0:2] (float32 CDAB 12.5) -> [0, 16712]
fc03 undefined address -> ExceptionResponse(dev_id=11, function_code=131, exception_code=2)
fc06 write 1234 -> WriteSingleRegisterResponse(dev_id=11, transaction_id=0, address=4, count=0,
                  bits=[], registers=[1234], status=1, retries=0)
fc03 read back -> [1234]
trace_packet frames: 10 ['0b 03 00 04 00 01 c5 61', '0b 03 02 00 0b 61 82',
                         '0b 03 00 00 00 02 c4 a1', '0b 03 04 00 00 41 48 60 55',
                         '0b 03 00 63 00 01 74 be', '0b 83 02 e0 f3',
                         '0b 06 00 04 04 d2 4a 3c', '0b 06 00 04 04 d2 4a 3c',
                         '0b 03 00 04 00 01 c5 61', '0b 03 02 04 d2 a2 d8']
trace_connect: [True, False]
active_connections after client.close(): {}
```

读出来的三件事：

1. **两端真跑 RTU 帧**：`0b 03 00 04 00 01 c5 61` 里 `0b` 是 unit、`c5 61` 是 CRC16；
   异常响应 `0b 83 02 e0 f3` 同理。也就是说 CRC 与"地址字节 + 功能码"这一层是真走的，
   只有"物理线"换成了环回 TCP。
2. **值语义与 TCP 完全一致**：float32/CDAB 12.5 落在 `[0, 16712]`，未定义地址回异常码 2，
   fc06 写进去能读回 —— 与 `test_integration_tcp.py` 那张表同一套期望。
   ⇒ M5 的集成用例可以参数化复用那张表，不必为 RTU 抄第二份断言。
3. **spec §12 的 M5 待确认项 1 清零**：`trace_packet` 在 `ModbusSerialServer` 上生效
   （10 帧请求/响应都在），`trace_connect` 也生效（`[True, False]`），
   客户端断开后 `server.active_connections` 清空 ⇒ 报文窗与连接状态在 RTU 上一样能接。

> 版本耦合：`socket://` 这条路是 pymodbus 3.15 的**显式分支**
> （`transport/transport.py:151-160`：串口 + `is_server` + host 以 `socket` 开头 → 当 TCP 监听）。
> 升级 pymodbus 时重跑本脚本；失效的表现是 M5 那批集成用例整体失败（可见，不静默）。

## 2. 串口打不开时，pymodbus 只说一句话，而那句话没有信息量

`rtu_open_failure_probe.py`（`port="COM99"`，本机没有这个串口）：

```
[pyserial] SerialException: could not open port 'COM99': FileNotFoundError(2, '系统找不到指定的文件。', None, 2)
DEBUG  pymodbus.logging Awaiting connections server_listener
WARNING pymodbus.logging Failed to start server could not open port 'COM99': FileNotFoundError(...)
DEBUG  pymodbus.logging Handler for stream [server] has been canceled
[pymodbus] serve_forever raised RuntimeError: Could not start listen, please check address.
```

两条结论：

1. `serve_forever()` 抛的是通用 `RuntimeError`，**真实原因只在一条 WARNING 日志里**；
2. 我们自己的 `configure_library_logging(level=ERROR)`（`backend/modbus_tcp.py`）会把那条
   WARNING 也压掉 ⇒ 不自己探的话，现场只会看到"启动失败 + Could not start listen"。
   **所以 RTU 后端必须先自己 `serial_for_url(port, exclusive=True)` 开一次**，
   把 pyserial 的原话与"不存在 vs 被占"的区分拿到手（M5 计划 §2.5 与 Task 4）。

## 3. 被排掉的方案（省得下次再试）

| 方案 | 实测/源码结论 | 为什么不用 |
|---|---|---|
| `loop://` 当"一对串口" | pyserial 的 loopback 是**自环**：写进去的字节由同一个对象读回 | 从站会把自己的响应当请求读回去；只适合单端单元测试 |
| 进程内 TCP 中继 + 两条 `socket://` | 不需要：从站侧遇到 `socket://` 会**自建监听**，主站直接连它就行（本记录 §1）。早先按"两端各连中继"写的那一版实测报 `Failed to start server ... bind on address ... 10013`（从站想绑中继已占用的端口） | 多一层中继、多一处失败面，收益为零 |
| `NullModem`（`port="__pymodbus_nullmodem:<n>"`） | 源码 `transport/transport.py:482`：类级 `listeners`/`connections` dict + 直接调用对端 protocol | **只在同一个 loop 内成立**；我们的同步主站在主线程、从站在引擎 loop 线程，跨线程用它等于让 asyncio 回调跑错线程。只当"两端都在引擎 loop 内"的单元手段 |
| 真 `COM` 口做自动化 | 本机没有 USB-485，也没有 com0com 之类的虚拟串口对 | 硬件那部分按计划留给人工冒烟（M5 计划 Task 8.3） |
