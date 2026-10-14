"""RTU over TCP 从站（E7）：真主站读/写 + 帧形状 + 归属。

现场形状：以太网转串口的网关那一侧是裸 TCP，跑的是 RTU 字节流（单元号 + PDU + CRC）。
主站侧就是 `ModbusTcpClient(framer=FramerType.RTU)`——**这条用例打的就是这条路**。

三个必须验到的点（写错了都会"看着能跑"）：

1. 帧切得对（RTU 没有长度字段，靠功能码 + 字节数算）；
2. **PDU 偏移 = 1**（不是 TCP 的 7）：偏移写错 → "主站写的是哪个点"认到别处，静默停一段曲线；
3. 归属：主站写过的那一格切成 manual，没写过的仍在波形名下。
"""

from __future__ import annotations

import socket

from pymodbus import FramerType
from pymodbus.client import ModbusTcpClient

from simbench.backend import make_backend
from simbench.backend.modbus_rtutcp import RtuOverTcpBackend
from simbench.project.model import Device, Point, TcpChannel

_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def channel(port: int) -> TcpChannel:
    return TcpChannel("gw-1", type="rtutcp", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("曲线", "hr", 0, "uint16", initial="0", waveform="const",
                  wave_param={"value": "7"}),
            Point("被主站写", "hr", 4, "uint16", initial="1", waveform="const",
                  wave_param={"value": "9"}),
        ])])


def master(port: int) -> ModbusTcpClient:
    return ModbusTcpClient("127.0.0.1", port=port, framer=FramerType.RTU, timeout=2.0)


def test_the_factory_builds_the_rtu_over_tcp_backend():
    assert isinstance(make_backend(channel(free_port())), RtuOverTcpBackend)


def test_the_pdu_offset_is_the_rtu_one():
    """PDU 偏移按传输给：RTU 帧里它就是 1（TCP 是 7）。"""
    assert RtuOverTcpBackend(channel(5020))._pdu_offset == 1


def test_a_real_master_reads_and_writes_rtu_frames_over_tcp(engine_runner):
    port = free_port()
    backend = make_backend(channel(port))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "rtutcp start")
    try:
        client = master(port)
        try:
            assert client.connect()
            assert client.read_holding_registers(0, count=1, device_id=11).registers == [0]
            backend._tick_once(now=1.0)
            assert client.read_holding_registers(0, count=1, device_id=11).registers == [7]
            client.write_register(4, 4242, device_id=11)
            assert client.read_holding_registers(4, count=1, device_id=11).registers == [4242]
        finally:
            client.close()
        # 归属：写过的那一格是 manual，曲线还在的那一格不是
        assert backend.source_is_waveform(11, "hr", 4) is False
        assert backend.source_is_waveform(11, "hr", 0) is True
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtutcp stop")


def test_the_frames_in_the_window_look_like_rtu_frames(engine_runner):
    """报文窗里的摘要与 RTU 那一支同形（现场不必学第二种读法）。"""
    port = free_port()
    seen: list[tuple] = []
    backend = make_backend(channel(port), packet_sink=lambda *args: seen.append(args))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "rtutcp start")
    try:
        client = master(port)
        try:
            assert client.connect()
            client.read_holding_registers(0, count=1, device_id=11)
        finally:
            client.close()
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "rtutcp stop")
    assert seen, "一帧都没进报文窗"
    assert any("unit 11 fc 3" in payload[4] for payload in seen), seen
