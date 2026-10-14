"""UDP 从站（E7）：真主站读/写 + 归属探针。

口径与 TCP 那一支一致，只差载体：

* 帧格式一样（MBAP），所以主站用 `ModbusUdpClient` 就能读；
* **归属探针只能 bind**（UDP 没有握手，TCP 那种 connect 探针在这里永远"连得上"）；
* 主站写下来的那一格要切成 manual（与另外三条传输同一条口径）。
"""

from __future__ import annotations

import socket
import time

import pytest
from pymodbus.client import ModbusUdpClient

from simbench.backend import make_backend
from simbench.backend.modbus_base import BackendStartError
from simbench.backend.modbus_udp import ModbusUdpBackend, check_udp_port_is_free
from simbench.project.model import Device, Point, TcpChannel

_CALL_TIMEOUT = 10.0


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def channel(port: int) -> TcpChannel:
    return TcpChannel("udp-1", type="udp", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("曲线", "hr", 0, "uint16", initial="0", waveform="const",
                  wave_param={"value": "7"}),
            Point("被主站写", "hr", 4, "uint16", initial="1", waveform="const",
                  wave_param={"value": "9"}),
        ])])


def test_the_factory_builds_the_udp_backend():
    assert isinstance(make_backend(channel(free_udp_port())), ModbusUdpBackend)


def test_a_udp_port_that_is_taken_is_refused():
    """探针必须先自己 bind 一次：不然现场只看到 pymodbus 那句"起不来"。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as hog:
        hog.bind(("127.0.0.1", 0))
        port = hog.getsockname()[1]
        with pytest.raises(BackendStartError) as excinfo:
            check_udp_port_is_free("127.0.0.1", port, "udp-1")
        assert "already in use" in str(excinfo.value)


def test_an_address_that_is_not_on_this_machine_says_so():
    """地址不在本机与端口被占是两件事（一个去跑 net up，一个去杀进程）。"""
    with pytest.raises(BackendStartError) as excinfo:
        check_udp_port_is_free("10.255.255.254", 5020, "udp-1")
    assert "not configured on any local adapter" in str(excinfo.value)


def test_a_real_master_reads_and_writes_over_udp(engine_runner):
    port = free_udp_port()
    backend = make_backend(channel(port))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "udp start")
    try:
        master = ModbusUdpClient("127.0.0.1", port=port, timeout=2.0)
        try:
            assert master.connect()
            # tick_ms=0（不起 tick 任务）：先读到的是点表初值，再手动推一拍才是曲线算出来的
            assert master.read_holding_registers(0, count=1, device_id=11).registers == [0]
            backend._tick_once(now=1.0)
            assert master.read_holding_registers(0, count=1, device_id=11).registers == [7]
            master.write_register(4, 77, device_id=11)
            assert master.read_holding_registers(4, count=1, device_id=11).registers == [77]
        finally:
            master.close()
        # 主站写过的那一格要切成 manual（与另外三条传输同一条口径）
        assert backend.source_is_waveform(11, "hr", 4) is False
        assert backend.source_is_waveform(11, "hr", 0) is True
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "udp stop")


def test_stop_then_start_again_on_the_same_port(engine_runner):
    """停了要能把端口让出来（同一路上再起一次）：UDP 的 bind 探针会验到这件事。"""
    port = free_udp_port()
    for _ in range(2):
        backend = make_backend(channel(port))
        # 闭包按值绑定 `backend`：延迟调用里引到循环变量的话，第二次那一轮会去起第一个对象
        engine_runner.call(lambda b=backend: b.start(), _CALL_TIMEOUT, "udp start")
        engine_runner.call(lambda b=backend: b.stop(), _CALL_TIMEOUT, "udp stop")
        time.sleep(0.05)
