"""S7 通道接到 Session（Task 5）：读行、改值、归属、交还、混装启停。

口径与 Modbus 那条一模一样，只是三条底层东西换了单位：回读回的是**字节**（`Row.registers`
装字节、界面按两位十六进制打）、改值按 S7 类型编、归属键固定 0（S7 没有 unit 这一维）。
"""

import socket
import time

import pytest
from pyS7 import S7Client

from simbench.engine.session import Session
from simbench.project.model import Device, Point, Project, S7Channel, TcpChannel

_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def channel(port: int) -> S7Channel:
    return S7Channel("s7-1", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("温度", "db1", 0, "real", initial="12.5",
                  waveform="const", wave_param={"value": "20"}),
            Point("状态字", "db1", 4, "word", initial="11",
                  waveform="const", wave_param={"value": "42"}),
            Point("启动", "db1", 6, "bool", initial="true", bit=3),
            Point("心跳", "mk", 0, "bool", initial="false", bit=0),
        ])])


@pytest.fixture
def session(engine_runner):
    s = Session(runner=engine_runner)
    yield s
    s.shutdown()


def master(port: int) -> S7Client:
    return S7Client("127.0.0.1", rack=0, slot=1, port=port, timeout=3.0)


def wait_for(predicate, timeout: float = 2.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what or predicate}")


def test_rows_show_the_point_table_values_through_the_area_buffer(session):
    port = free_port()
    session.load(Project(channels=[channel(port)]))
    session.start_channel("s7-1")
    rows = {row.name: row for row in session.rows("s7-1")}
    assert rows["温度"].value_text == "12.5"      # 缓冲里按点表初值填过
    assert rows["状态字"].value_text == "11"
    assert rows["启动"].value_text == "true"
    # `registers` 在 S7 这一路装的是**字节**（real 4 个、bool 1 个）
    assert rows["温度"].registers == (0x41, 0x48, 0x00, 0x00)
    assert rows["启动"].registers == (0x01,)
    assert not rows["温度"].stale


def test_a_gui_edit_lands_in_the_area_and_the_master_sees_it(session):
    port = free_port()
    session.load(Project(channels=[channel(port)]))
    session.start_channel("s7-1")
    result = session.set_point("s7-1", 11, "db1", 4, "777")
    assert result.ok, result.error
    assert result.value_text == "777"
    assert result.registers == (0x03, 0x09)       # 777 = 0x0309，大端两个字节
    client = master(port)
    try:
        client.connect()
        assert client.read(["DB1,W4"]) == [777]
    finally:
        client.disconnect()


def test_the_ownership_count_and_restore_work_on_the_s7_key(session):
    """改值 → manual（按**点**计数）→ 交还 → 曲线接手：与 Modbus 同一套语义。"""
    port = free_port()
    session.load(Project(channels=[channel(port)]))
    session.start_channel("s7-1")
    assert session.manual_count("s7-1") == 0
    assert session.set_point("s7-1", 11, "db1", 4, "777").ok
    assert session.manual_count("s7-1") == 1
    row = next(r for r in session.rows("s7-1") if r.name == "状态字")
    assert row.source == "manual"
    assert session.restore_waveform("s7-1", 11, "db1", 4) is True
    assert session.manual_count("s7-1") == 0
    assert session.channel("s7-1").backend._tick_once(now=time.monotonic() + 1.0) >= 1
    client = master(port)
    try:
        client.connect()
        assert client.read(["DB1,W4"]) == [42], "交还之后曲线没接手"
    finally:
        client.disconnect()


def test_a_master_write_takes_the_point_over_for_the_gui_too(session):
    """主站的写（事件）与界面改值（`set_point`）用的是**同一个归属键**。"""
    port = free_port()
    session.load(Project(channels=[channel(port)]))
    session.start_channel("s7-1")
    client = master(port)
    try:
        client.connect()
        client.write(["DB1,W4"], [5])
        wait_for(lambda: session.manual_count("s7-1") == 1,
                 what="主站的写事件被算进归属")
    finally:
        client.disconnect()


def test_an_out_of_range_or_wrong_type_edit_is_refused_with_the_s7_type_name(session):
    port = free_port()
    session.load(Project(channels=[channel(port)]))
    session.start_channel("s7-1")
    bad = session.set_point("s7-1", 11, "db1", 4, "70000")
    assert not bad.ok and "word out of range" in bad.error
    assert session.manual_count("s7-1") == 0, "被拒的改动不许留下接管痕迹"


def test_a_string_that_is_too_long_points_at_the_point_table(session):
    """与 Modbus 那条同一句"该改哪里"：跨度归点表初值管，不在编辑框里。"""
    port = free_port()
    project = Project(channels=[S7Channel("s7-1", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [Point("铭牌", "db1", 0, "string", initial="AB")])])])
    session.load(project)
    session.start_channel("s7-1")
    too_long = session.set_point("s7-1", 11, "db1", 0, "ABCD")
    assert not too_long.ok
    assert "at most" in too_long.error and "初值" in too_long.error


def test_mixed_tcp_rtu_and_s7_channels_start_and_stop_independently(engine_runner):
    """三路混装各自独立启停（S7 与串口/ TCP 在同一份工程里共存）。"""
    from simbench.project.model import SerialChannel
    port_tcp, port_rtu, port_s7 = free_port(), free_port(), free_port()
    session = Session(runner=engine_runner)
    try:
        session.load(Project(channels=[
            TcpChannel("tcp-x", host="127.0.0.1", port=port_tcp, devices=[
                Device("D", 11, [Point("x", "hr", 0, "uint16", initial="1")])]),
            SerialChannel("com-485-1", port=f"socket://127.0.0.1:{port_rtu}",
                          devices=[Device("E", 21, [Point("y", "hr", 0, "uint16",
                                                          initial="2")])]),
            channel(port_s7),
        ]))
        assert session.start_all() == []
        assert all(session.channel(name).running
                   for name in ("tcp-x", "com-485-1", "s7-1"))
        assert [r.name for r in session.rows("s7-1")][0] == "温度"
        session.stop_all()
        assert not any(session.channel(name).running
                       for name in ("tcp-x", "com-485-1", "s7-1"))
    finally:
        session.stop_all()
        session.shutdown()
