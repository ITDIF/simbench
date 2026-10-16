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

from simbench.backend import BackendStartError, make_backend
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

# ---------- 声明里的额外地址：UDP 的判据与 TCP 不同 ----------

def test_a_wildcard_bind_already_covers_the_declared_addresses(engine_runner):
    """host = 0.0.0.0 本来就在所有本机地址上听：声明里的设备 IP 也就都在里面了，
    所以**不用**再挂额外监听，而且**不许**为此说一句警告（那是无话找话）。"""
    warnings: list[str] = []
    port = free_udp_port()
    channel_with_wildcard = TcpChannel("udp-1", type="udp", host="0.0.0.0", port=port,
                                       devices=channel(port).devices)
    backend = make_backend(channel_with_wildcard,
                           extra_endpoints=[("127.0.0.1", port)],
                           log_sink=lambda level, text: warnings.append(text))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "udp wildcard start")
    try:
        assert backend.endpoints == [("0.0.0.0", port)]
        assert warnings == [], warnings
        master = ModbusUdpClient("127.0.0.1", port=port, timeout=2.0)
        try:
            assert master.connect()
            # 通配绑定：从环回地址连上去照样读得到（这就是"已经覆盖"的意思）
            backend._tick_once(now=1.0)
            assert master.read_holding_registers(0, count=1, device_id=11).registers == [7]
        finally:
            master.close()
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "udp wildcard stop")


def test_a_specific_bind_says_what_it_cannot_do(engine_runner):
    """绑死某块网卡 + 声明里另有地址：那一种组合真覆盖不到，**要说出来**。"""
    warnings: list[str] = []
    port = free_udp_port()
    backend = make_backend(channel(port),                 # 这个 channel 绑的是 127.0.0.1
                           extra_endpoints=[("127.0.0.1", 5020)],
                           log_sink=lambda level, text: warnings.append(text))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "udp specific start")
    try:
        assert backend.endpoints == [("127.0.0.1", port)], "说不支持却挂上了？"
        assert warnings and "没有挂上" in warnings[0], warnings
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "udp specific stop")
