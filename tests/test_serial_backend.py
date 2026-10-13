"""RTU 后端（M5 Task 4）：串口探针的拒绝形状、起停、真主站在 socket:// 虚拟串口上的读写。

三条口径：

1. **探针必须先自己开一次串口**：不探的话现场只看到 pymodbus 那句
   `Could not start listen, please check address.`，而真实原因（pyserial 的原话）连一条
   warning 都被我们自己的日志级别压掉了（实测见 `docs/superpowers/notes/`）。
2. **`socket://` 是验收路径**：从站那一侧会自建 TCP 监听、主站那一侧是 pyserial 的串口
   对象，两端跑真 RTU framer（单元号 + PDU + CRC）——不需要任何硬件。
3. **接管与波形在 RTU 上与 TCP 同口径**：主站写下来的那一格切成 manual、tick 不再顶它、
   交还后 tick 又接手；这条同时证明"按 RTU 帧形状切帧 + PDU 偏移 = 1"是对的
   （写死 7 的那一版会把"写哪个点"认到错的偏移上，静默停掉一段曲线）。
"""

import pathlib
import socket
import time

import pytest
from pymodbus import FramerType
from pymodbus.client import ModbusSerialClient

from simbench.backend import UnsupportedTransportError, make_backend
from simbench.backend.modbus_rtu import (ModbusRtuBackend, SerialOpenError,
                                         check_serial_is_free)
from simbench.backend.modbus_tcp import ModbusTcpBackend
from simbench.project.model import Device, Point, SerialChannel, TcpChannel

_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def socket_url(port: int) -> str:
    """pyserial 的 `socket://` 当"虚拟串口"：从站那侧自建 TCP 监听（M5 计划 §2）。"""
    return f"socket://127.0.0.1:{port}"


def channel(port: str) -> SerialChannel:
    return SerialChannel("com-485-1", port=port, baudrate=9600, devices=[
        Device("COVI-1", 11, [
            Point("曲线", "hr", 0, "uint16", initial="0", waveform="const",
                  wave_param={"value": "7"}),
            Point("被主站写", "hr", 4, "uint16", initial="1", waveform="const",
                  wave_param={"value": "9"}),
        ])])


def master(port: int) -> ModbusSerialClient:
    return ModbusSerialClient(port=socket_url(port), framer=FramerType.RTU,
                              baudrate=9600, timeout=2.0)


# ---------- 工厂与探针 ----------

def test_the_factory_builds_both_backends():
    assert isinstance(make_backend(TcpChannel("tcp-x", host="127.0.0.1", port=5020)),
                      ModbusTcpBackend)
    assert isinstance(make_backend(channel("COM3")), ModbusRtuBackend)


def test_an_unknown_transport_is_still_refused():
    with pytest.raises(UnsupportedTransportError) as excinfo:
        make_backend(TcpChannel("x", type="udp"))
    assert "x" in str(excinfo.value)


def test_a_serial_port_that_cannot_be_opened_is_refused_with_the_real_reason():
    """探针要拿到 pyserial 的原话：串口名 + "打不开" + 该做什么，三样齐。"""
    with pytest.raises(SerialOpenError) as excinfo:
        check_serial_is_free("COM999", "com-485-1")
    message = str(excinfo.value)
    assert "COM999" in message and "com-485-1" in message
    assert "could not open port" in message
    assert "another simulator" in message


def test_the_probe_closes_the_port_again():
    """探针只借这一路问一句就还回去：同一个 socket:// 端口随后还能再绑一次。

    `socket://` 那一路的探针走的是 TCP 的 bind/connect 两级（见 `check_serial_is_free`），
    两个探针的套接字都必须关掉——留一个就等于"还没启动就把自己的端口占了"。
    """
    port = free_port()
    check_serial_is_free(socket_url(port))
    with socket.socket() as probe:            # 还占着就会 10048/10013
        probe.bind(("127.0.0.1", port))


# ---------- 起停与真主站 ----------

def test_a_real_master_reads_and_writes_over_the_virtual_serial_pair(engine_runner):
    port = free_port()
    backend = ModbusRtuBackend(channel(socket_url(port)), tick_ms=0)
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "rtu start")
    client = master(port)
    try:
        assert client.connect()
        # tick_ms=0（不起 tick 任务）：读到的是点表初值，不是曲线算出来的值
        assert client.read_holding_registers(0, count=1, device_id=11).registers == [0]
        backend._tick_once(now=1.0)           # 曲线那一拍算出来的才是 7
        assert client.read_holding_registers(0, count=1, device_id=11).registers == [7]
        client.write_register(0, 4242, device_id=11)
        assert client.read_holding_registers(0, count=1, device_id=11).registers == [4242]
        # 未定义地址回异常码 2：与 TCP 同一份协议行为
        response = client.read_holding_registers(99, count=1, device_id=11)
        assert getattr(response, "exception_code", None) == 2
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")
    with socket.socket() as probe:            # 停了之后端口真的松开
        probe.bind(("127.0.0.1", port))


def test_a_master_write_takes_the_point_over_and_restore_hands_it_back(engine_runner):
    """RTU 上的值归属与 TCP 同口径：写下来 -> manual -> tick 不顶 -> 交还 -> tick 接手。

    这一条同时是"PDU 偏移 = 1"的证据：写死 7 的那版认不出主站写了哪一格，
    `source_is_waveform` 会一直是 True（曲线把主站写的值顶回去，正是 spec §7 防的那件事）。
    """
    port = free_port()
    backend = ModbusRtuBackend(channel(socket_url(port)), tick_ms=0)
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "rtu start")
    client = master(port)
    try:
        assert client.connect()
        client.write_register(4, 1234, device_id=11)
        assert backend.source_is_waveform(11, "hr", 4) is False, "主站写的这一格没被接管"
        assert backend.source_is_waveform(11, "hr", 0) is True, "别的点被连坐接管了"

        backend._tick_once(now=1.0)           # 直接调：tick 任务没起（tick_ms=0）
        assert backend.read_registers(11, "hr", 4, 1) == [1234], "曲线把主站写的顶回去了"
        assert backend.read_registers(11, "hr", 0, 1) == [7], "波形那一格没被写"

        assert backend.restore_waveform(11, "hr", 4) is True
        backend._tick_once(now=2.0)
        assert backend.read_registers(11, "hr", 4, 1) == [9], "交还之后曲线没接手"
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_a_failed_start_reports_instead_of_crashing(engine_runner):
    """串口不存在：探针抛出，通道保持没起（`Session` 那一层据此把界面标红）。"""
    backend = ModbusRtuBackend(channel("COM999"))
    with pytest.raises(SerialOpenError) as excinfo:
        engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "rtu start fails")
    assert "COM999" in str(excinfo.value)


def test_split_frames_walks_two_writes_in_one_chunk():
    """一块里挤两条写请求时两条都要认（TCP 那条实测过 24 字节；串口一次轮询能读上百字节）。"""
    backend = ModbusRtuBackend(channel("COM3"))
    write_one = bytes([0x0B, 0x06, 0x00, 0x04, 0x04, 0xD2, 0x4A, 0x3C])
    write_two = bytes([0x0B, 0x06, 0x00, 0x05, 0x00, 0x2A, 0x00, 0x00])
    frames = list(backend._split_frames(write_one + write_two + b"\\x0b"))
    assert frames == [write_one, write_two], "多帧或半帧没切对"


def test_split_frames_stops_at_a_frame_it_cannot_measure():
    """量不出来的帧型（诊断回显 0x08）就整块停手：猜长度会把后面的真帧切歪。"""
    backend = ModbusRtuBackend(channel("COM3"))
    assert list(backend._split_frames(bytes([0x0B, 0x08, 0x00, 0x00]))) == []


# ---------- 验收矩阵（M5 计划 §6，真主站 + 真 tick，socket:// 虚拟串口对） ----------

def tunnel(port: str) -> SerialChannel:
    """一行点表覆盖 §6 要的形状：CDAB 浮点、uint64、string、位区、只读点。

    每个寄存器/位值都与 `tests/test_integration_tcp.py` 那份 TCP 验收用同一个字面量——
    两条传输读到的必须是同一批数，否则就是"串口这一路和 TCP 不是同一台设备"。
    """
    return SerialChannel("com-485-1", port=port, baudrate=9600, devices=[
        Device("COVI-1", 11, [
            Point("CO浓度", "hr", 0, "float32", initial="12.5", word_order="CDAB",
                  readonly=True),
            Point("状态字", "hr", 4, "uint16", initial="11"),
            Point("累计电量", "hr", 5, "uint64", initial="72623859790382856"),
            Point("铭牌", "hr", 9, "string", initial="FAN-01"),
            Point("启动", "co", 0, "bool", initial="false"),
            Point("运行状态", "di", 0, "bool", initial="true"),
            Point("设定", "ir", 0, "uint16", initial="7"),
        ])])


def open_rtu(engine_runner, channel, *, tick_ms: int = 0, packet_sink=None,
             log_sink=None) -> ModbusRtuBackend:
    """在引擎 loop 上起一路 RTU 后端（生产同一条 `make_backend` 路径的等价物）。"""
    backend = ModbusRtuBackend(channel, tick_ms=tick_ms, packet_sink=packet_sink,
                               log_sink=log_sink)
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "rtu start")
    return backend


def test_the_value_semantics_match_tcp_regardless_of_transport(engine_runner):
    """§6 #1/#2：读四个区，CDAB 浮点 / uint64 / string / 位区的值与 TCP 那份逐字一致。"""
    port = free_port()
    backend = open_rtu(engine_runner, tunnel(socket_url(port)))
    client = master(port)
    try:
        assert client.connect()
        # 03：float32 CDAB 12.5、uint16 11、uint64 0x0102030405060708、string FAN-01
        assert client.read_holding_registers(0, count=2, device_id=11).registers \
            == [0x0000, 0x4148]
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
        assert client.read_holding_registers(5, count=4, device_id=11).registers \
            == [0x0102, 0x0304, 0x0506, 0x0708]
        assert client.read_holding_registers(9, count=3, device_id=11).registers \
            == [0x4641, 0x4E2D, 0x3031]
        # 01/02/04：位区与输入区
        assert client.read_coils(0, count=1, device_id=11).bits[0] is False
        assert client.read_discrete_inputs(0, count=1, device_id=11).bits[0] is True
        assert client.read_input_registers(0, count=1, device_id=11).registers == [7]
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_a_master_register_write_survives_the_rtu_waveform_tick(engine_runner):
    """§6 #3：给 hr4 配 0.4s 一圈的 sine，主站写 333，0.5s（10 个 tick）后必须还是 333。

    这条红了就是 M4 的核心承诺在串口上破了：平台下发成功、值被模拟器顶掉。
    """
    port = free_port()
    channel = tunnel(socket_url(port))
    point = channel.devices[0].points[1]                     # 状态字 hr4 uint16
    point.waveform, point.wave_param = "sine", {"base": "0", "amp": "10",
                                                "period": "0.4"}
    backend = open_rtu(engine_runner, channel, tick_ms=50)
    client = master(port)
    try:
        assert client.connect()
        client.write_register(4, 333, device_id=11)
        time.sleep(0.5)
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [333]
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_a_master_coil_write_survives_the_rtu_waveform_tick(engine_runner):
    """§6 #4：线圈单独走一条（`write_registers` 写 co 会写错位）。写 1 后 0.5 秒连读 5 次都是 1。"""
    port = free_port()
    channel = tunnel(socket_url(port))
    coil = next(pt for pt in channel.devices[0].points if pt.area == "co")
    coil.waveform, coil.wave_param = "step", {"values": "1,0", "interval": "0.2"}
    backend = open_rtu(engine_runner, channel, tick_ms=50)
    client = master(port)
    try:
        assert client.connect()
        client.write_coil(coil.address, True, device_id=11)
        seen = [client.read_coils(coil.address, count=1, device_id=11).bits[0]
                for _ in range(5)]
        assert all(bit is True for bit in seen), f"下发被波形顶回去过：{seen}"
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_a_write_the_rtu_protocol_refused_does_not_take_the_waveform_off(engine_runner):
    """§6 #5：只读点被异常码 2 拒了，但 rx 报文照样解成"写"——不过滤就把曲线停了。"""
    port = free_port()
    backend = open_rtu(engine_runner, tunnel(socket_url(port)))
    client = master(port)
    try:
        assert client.connect()
        refused = client.write_registers(0, [1, 2], device_id=11)     # CO浓度 readonly
        assert refused.isError() and refused.exception_code == 2
        assert backend.source_is_waveform(11, "hr", 0)
        assert backend._sources.cleared() == 0, "被拒的写往状态里塞了键"
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_undefined_address_and_unit_answer_the_same_codes_on_rtu(engine_runner):
    """§6 #6：未定义地址回异常码 2、未定义 unit 回异常码 4，且从站不因此崩。"""
    port = free_port()
    backend = open_rtu(engine_runner, tunnel(socket_url(port)))
    client = master(port)
    try:
        assert client.connect()
        assert getattr(client.read_holding_registers(99, count=1, device_id=11),
                       "exception_code", None) == 2
        assert getattr(client.read_holding_registers(0, count=1, device_id=12),
                       "exception_code", None) == 4
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_a_clamped_rtu_waveform_is_clamped_and_logged_naming_the_point(engine_runner):
    """§6 #7：uint16 点配 const=70000，值必须钳到 65535，且日志点名是哪个点。"""
    port = free_port()
    channel = tunnel(socket_url(port))
    point = channel.devices[0].points[1]                     # 状态字 hr4 uint16
    point.waveform, point.wave_param = "const", {"value": "70000"}
    seen: list[tuple[str, str]] = []
    backend = open_rtu(engine_runner, channel, tick_ms=0,
                       log_sink=lambda level, text: seen.append((level, text)))
    client = master(port)
    try:
        assert client.connect()
        backend._tick_once(now=0.0)
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [65535]
        assert seen and seen[0][0] == "warning" and "状态字" in seen[0][1], seen
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")


def test_the_rtu_packet_sink_carries_the_contract_fields(engine_runner):
    """§6 #9：报文窗那份六位置合同在 RTU 上一样成立——只是 PDU 的起点在单元号之后（偏移 1）。"""
    port = free_port()
    seen: list[tuple] = []
    backend = open_rtu(engine_runner, tunnel(socket_url(port)),
                       packet_sink=lambda *args: seen.append(args))
    client = master(port)
    try:
        assert client.connect()
        client.read_holding_registers(4, count=1, device_id=11)
        client.close()
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtu stop")
    directions = [row[0] for row in seen]
    assert "rx" in directions and "tx" in directions
    rx = next(row for row in seen if row[0] == "rx")
    direction, raw, unit, function, summary, mono = rx
    assert direction == "rx" and isinstance(raw, bytes)
    assert raw[1] == 3, "RTU 的 PDU 从单元号之后开始（偏移 1），不是 MBAP 之后的 7"
    assert (unit, function) == (11, 3)
    assert summary.startswith("unit 11 fc 3")
    assert mono >= 0.0


def test_mixed_tcp_and_rtu_channels_start_and_stop_independently(engine_runner):
    """§6 #12：一 TCP 一 RTU 各自独立起停，一路失败不牵连另一路。"""
    from simbench.engine.session import Session
    from simbench.project.model import Project, TcpChannel

    port_tcp, port_rtu = free_port(), free_port()
    session = Session(runner=engine_runner)
    try:
        session.load(Project(channels=[
            TcpChannel("tcp-x", host="127.0.0.1", port=port_tcp,
                       devices=[Device("D", 11, [Point("x", "hr", 0, "uint16",
                                                       initial="1")])]),
            tunnel(socket_url(port_rtu)),
        ]))
        assert session.start_all() == []
        assert session.channel("tcp-x").running and session.channel("com-485-1").running
        session.stop_all()
        assert not session.channel("tcp-x").running
        assert not session.channel("com-485-1").running
    finally:
        session.stop_all()
        session.shutdown()
