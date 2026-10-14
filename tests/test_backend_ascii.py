"""串口 ASCII 从站（E7）：真主站读/写 + 帧形状。

与 RTU 同一套验收路径（`socket://` 当虚拟串口，主站那侧是 pyserial 对象），只把 framer
换成 `FramerType.ASCII`：帧变成 `:` + 十六进制 + CRLF + LRC。

必须验到的两点：

1. 切帧：`:` 起、CRLF 止（切歪了 LRC 校验会失败，主站读回异常码）；
2. "主站写了哪个点"要把十六进制解回二进制再认（不解就是静默不接管：曲线把主站写的顶回去）。
"""

from __future__ import annotations

import socket

from pymodbus import FramerType
from pymodbus.client import ModbusSerialClient

from simbench.backend import make_backend
from simbench.backend.modbus_ascii import (
    ModbusAsciiBackend,
    ascii_to_binary,
    parse_ascii_frame,
    split_ascii_frames,
)
from simbench.project.model import Device, Point, SerialChannel

_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def socket_url(port: int) -> str:
    return f"socket://127.0.0.1:{port}"


def channel(port: int) -> SerialChannel:
    return SerialChannel("com-485-1", port=socket_url(port), baudrate=9600, framing="ascii",
                         devices=[Device("D1", 11, [
                             Point("曲线", "hr", 0, "uint16", initial="0", waveform="const",
                                   wave_param={"value": "7"}),
                             Point("被主站写", "hr", 4, "uint16", initial="1", waveform="const",
                                   wave_param={"value": "9"}),
                         ])])


def master(port: int) -> ModbusSerialClient:
    return ModbusSerialClient(port=socket_url(port), framer=FramerType.ASCII,
                              baudrate=9600, timeout=2.0)


# ---------- 帧的切与解（纯函数，先钉死再谈端到端）----------

def test_frames_are_split_on_the_colon_and_crlf():
    raw = b":010300000001FB\r\n:010300010001FA\r\n"
    frames = list(split_ascii_frames(raw))
    assert frames == [b":010300000001FB\r\n", b":010300010001FA\r\n"]


def test_a_half_frame_is_left_for_the_next_read():
    """半个帧不许猜：留到下一次（trace 会把累计缓冲整段重放）。"""
    assert list(split_ascii_frames(b":010300000001FB\r")) == []


def test_hex_is_decoded_back_to_unit_and_pdu():
    assert ascii_to_binary(b":010300000001FB\r\n") == bytes([1, 3, 0, 0, 0, 1, 0xFB])
    assert ascii_to_binary(b":01") is None            # 奇数个十六进制字符
    assert ascii_to_binary(b":zz\r\n") is None       # 不是十六进制


def test_the_summary_matches_the_other_transports():
    unit, function, summary = parse_ascii_frame(b":010300000001FB\r\n")
    assert (unit, function) == (1, 3)
    assert summary.startswith("unit 1 fc 3 "), summary


# ---------- 端到端 ----------

def test_the_factory_builds_the_ascii_backend():
    backend = make_backend(channel(free_port()))
    assert isinstance(backend, ModbusAsciiBackend)
    assert backend.TRANSPORT == "ascii"


def test_a_real_master_reads_and_writes_ascii_frames(engine_runner):
    port = free_port()
    backend = make_backend(channel(port))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "ascii start")
    try:
        client = master(port)
        try:
            assert client.connect()
            # 初值是初值，推一拍才是曲线算出来的
            backend._tick_once(now=1.0)
            assert client.read_holding_registers(0, count=1, device_id=11).registers == [7]
            client.write_register(4, 4242, device_id=11)
            assert client.read_holding_registers(4, count=1, device_id=11).registers == [4242]
        finally:
            client.close()
        # 接管：主站写的那一格（十六进制解回来才认得出来）切成 manual
        assert backend.source_is_waveform(11, "hr", 4) is False
        assert backend.source_is_waveform(11, "hr", 0) is True
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "ascii stop")
