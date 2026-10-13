"""S7 后端（Task 4）：起停、真主站读写、区域缓冲、事件（报文行 + 归属）、依赖缺省的报告。

两条纪律：

1. **缓冲区按点表算**、只装点表声明过的字节：主站读没声明过的地址要得到"未定义地址"，
   不是一个看着合法的 0（与 Modbus 那条"未定义地址回异常码 2"同一个姿态）。
2. 主站的读/写走 snap7 的**事件**（拿不到原始报文，口径 5）：写事件要顺手把这一段标成
   manual（spec §7 的归属唯一入口），被拒的请求（`EvtRetCode != 0`）不标。
"""

import socket
import time

import pytest
from pyS7 import S7Client

from simbench.backend import make_backend
from simbench.backend.modbus_base import BackendStartError
from simbench.backend.s7_server import S7Backend
from simbench.project.model import Device, Point, Project, S7Channel, TcpChannel

_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def channel(port: int) -> S7Channel:
    """一个 DB1（real/word/位）+ 一个 M 区（位）：够覆盖读、写、未定义地址与两个区。"""
    return S7Channel("s7-1", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("温度", "db1", 0, "real", initial="12.5",
                  waveform="const", wave_param={"value": "20"}),
            Point("状态字", "db1", 4, "word", initial="11",
                  waveform="const", wave_param={"value": "42"}),
            Point("启动", "db1", 6, "bool", initial="true", bit=3),
            Point("心跳", "mk", 0, "bool", initial="false", bit=0),
        ])])


def master(port: int) -> S7Client:
    return S7Client("127.0.0.1", rack=0, slot=1, port=port, timeout=3.0)


def wait_for(predicate, timeout: float = 2.0, what: str = "") -> None:
    """等一个条件成立。

    为什么需要：主站的读/写在我们这边是**事件**（snap7 在它自己的线程上回调），
    `client.write()` 返回只说明服务端回了响应，回调可能还差一拍。裸 `sleep` 会把
    "慢机器上偶发红"埋进去，所以这里有界轮询、超时就报出等的是什么。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what or predicate}")


def open_s7(engine_runner, ch, **kwargs) -> S7Backend:
    backend = S7Backend(ch, **kwargs)
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "s7 start")
    return backend


def test_the_factory_builds_the_s7_backend():
    assert isinstance(make_backend(S7Channel("s7-1", port=102)), S7Backend)


def test_a_real_master_reads_the_seeded_values_and_writes_back(engine_runner):
    port = free_port()
    backend = open_s7(engine_runner, channel(port))
    client = master(port)
    try:
        client.connect()
        # 初值按点表填进缓冲：主站一上来就该读到点表里的数
        assert client.read(["DB1,R0"]) == [pytest.approx(12.5)]
        assert client.read(["DB1,W4", "DB1,X6.3", "M0.0"]) == [11, True, False]
        # 写回：real / word / 位各一条
        client.write(["DB1,R0"], [3.25])
        client.write(["DB1,W4"], [1234])
        client.write(["DB1,X6.3"], [False])
        client.write(["M0.0"], [True])
        assert client.read(["DB1,R0", "DB1,W4", "DB1,X6.3", "M0.0"]) \
            == [pytest.approx(3.25), 1234, False, True]
        # 位写只动那一位：字节 6 的其它位不受影响
        client.write(["DB1,X6.2"], [True])
        assert client.read(["DB1,X6.2", "DB1,X6.3", "DB1,B6"]) == [True, False, 0x04]
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")


def test_an_address_we_did_not_declare_is_not_answered(engine_runner):
    """缓冲区只装点表声明过的字节：多一个字节都不答（不是"读回 0"）。"""
    port = free_port()
    backend = open_s7(engine_runner, channel(port))
    client = master(port)
    try:
        client.connect()
        assert client.read(["DB1,W4"]) == [11]
        # 两种拒绝要分得开：没注册的 DB 与"注册了但越界"是两件事，现场排查的下一步不同
        with pytest.raises(Exception) as excinfo:
            client.read(["DB2,B0"])
        assert "OBJECT_DOES_NOT_EXIST" in str(excinfo.value), str(excinfo.value)
        with pytest.raises(Exception) as excinfo:
            client.read(["DB1,B60"])
        assert "OUT_OF_RANGE" in str(excinfo.value), str(excinfo.value)
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")


def test_stop_releases_the_port(engine_runner):
    port = free_port()
    backend = open_s7(engine_runner, channel(port))
    engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")
    with socket.socket() as probe:                      # 还占着就会 10048/10013
        probe.bind(("127.0.0.1", port))


def test_the_tick_writes_into_the_area_and_respects_ownership(engine_runner):
    """波形 tick 改的是同一块缓冲（主站读得到），被主站写过的点 tick 一个字都不动。"""
    port = free_port()
    backend = open_s7(engine_runner, channel(port), tick_ms=0)
    client = master(port)
    try:
        client.connect()
        backend._tick_once(now=1.0)                     # 曲线写进缓冲
        assert client.read(["DB1,R0", "DB1,W4"]) == [pytest.approx(20.0), 42]

        client.write(["DB1,W4"], [777])                 # 主站接管这一段
        wait_for(lambda: not backend.source_is_waveform(0, "db1", 4),
                 what="主站的写事件把这一段标成 manual")
        backend._tick_once(now=2.0)
        assert client.read(["DB1,W4"]) == [777], "曲线把主站写的顶回去了"

        assert backend.restore_waveform(0, "db1", 4) is True
        backend._tick_once(now=3.0)
        assert client.read(["DB1,W4"]) == [42], "交还之后曲线没接手"
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")


def test_the_packet_sink_gets_the_master_request_lines(engine_runner):
    """口径 5：S7 这一路只有主站请求那一行（rx、无原始字节），摘要写清读/写与区间。"""
    port = free_port()
    seen: list[tuple] = []
    backend = open_s7(engine_runner, channel(port),
                      packet_sink=lambda *args: seen.append(args))
    client = master(port)
    try:
        client.connect()
        client.read(["DB1,R0"])
        client.write(["DB1,W4"], [5])
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")
    summaries = [row[4] for row in seen]
    assert "读 db1 4B @0" in summaries, summaries
    assert "写 db1 2B @4" in summaries, summaries
    assert all(row[0] == "rx" and row[1] == b"" for row in seen), seen


def test_a_write_the_server_refused_does_not_take_the_point_over(engine_runner):
    """被拒的请求（`EvtRetCode != 0`）不标 manual：一次没成功的写不该把曲线停掉。"""
    port = free_port()
    backend = open_s7(engine_runner, channel(port))
    client = master(port)
    try:
        client.connect()
        with pytest.raises(Exception):
            client.write(["DB1,W60"], [1])              # 越界：服务端拒
        assert backend.source_is_waveform(0, "db1", 4) is True
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")


def test_a_second_master_snap7s_own_client_agrees(engine_runner):
    """第二个**独立实现**的主站（snap7 自己的客户端）跑同一张表：两个主站都通过才算数。

    pys7 是纯 Python 的一个实现、snap7 的客户端是 C 库自带的那一个——只对一个通过的
    "兼容"很容易是"两边恰好都这么写"。这一条同时钉住 `get_cpu_state()` 能拿到（服务端答
    这个请求），而 SZL 那类请求它不答（pys7 的 `get_cpu_status()` 会超时，见计划 D8）。
    """
    from snap7 import client as s7client

    port = free_port()
    backend = open_s7(engine_runner, channel(port))
    client = s7client.Client()
    try:
        client.connect("127.0.0.1", 0, 1, port)     # 成功回 None，失败才抛
        assert client.get_connected() is True
        assert client.get_cpu_state() == "S7CpuStatusRun"
        # 读初值：real 4 字节 + word 2 字节 + 位那一格
        assert client.db_read(1, 0, 4) == bytes([0x41, 0x48, 0x00, 0x00])
        assert client.db_read(1, 4, 2) == bytes([0x00, 0x0B])
        assert client.read_area(s7client.Areas.DB, 1, 6, 1) == bytes([0x08])
        # 写回（寄存器 + 位），再读回来
        client.db_write(1, 4, bytearray(b"\x04\xD2"))
        assert client.db_read(1, 4, 2) == bytes([0x04, 0xD2])
        client.write_area(s7client.Areas.DB, 1, 6, bytearray(b"\x00"))
        assert client.read_area(s7client.Areas.DB, 1, 6, 1) == bytes([0x00])
        # M 区独立
        client.mb_write(0, 1, bytearray(b"\x01"))
        assert client.mb_read(0, 1) == bytes([0x01])
        # 没注册的 DB：服务端拒
        with pytest.raises(Exception) as excinfo:
            client.db_read(2, 0, 1)
        assert "Item not available" in str(excinfo.value), str(excinfo.value)
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7 stop")


def test_describe_and_endpoints():
    backend = S7Backend(S7Channel("s7-1", host="192.168.0.10", port=102, rack=0, slot=2))
    assert backend.describe() == "192.168.0.10:102 rack 0 slot 2"
    assert backend.endpoints == [("192.168.0.10", 102)]


def test_starting_without_the_dependency_reports_one_line(engine_runner, monkeypatch):
    """没装 S7 依赖时：一句人话（含安装提示），不是 ModuleNotFoundError 或 AttributeError。"""
    from simbench.backend import s7_server
    monkeypatch.setattr(s7_server, "_snap7_server", None)
    backend = S7Backend(channel(free_port()))
    with pytest.raises(BackendStartError) as excinfo:
        engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "s7 start fails")
    assert "python-snap7" in str(excinfo.value)
