"""后台取数（`engine/rows_pump.py`）：界面那一跳永远不等引擎。

判据分两层：多数用例用**桩 Session**（数调用次数、造失败），一条端到端用例用**真从站 +
真主站**（证明后台线程确实按节拍把主站写的值捞回来了）。
"""

from __future__ import annotations

import contextlib
import pathlib
import socket
import time

import pytest

from simbench.csv_io_entry import load_point_table
from simbench.engine.rows_pump import RowsPump
from simbench.engine.session import Session, Row
from simbench.project.model import Device, Point, Project, TcpChannel

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def row(name: str, value: str = "1") -> Row:
    return Row("tcp-1", "D1", 11, name, "hr", 0, "uint16", "ABCD", value, (1,),
               False, "const")


class StubSession:
    """`RowsPump` 只认这几个成员：`project` / `rows` / `write` / `closed` / `revision` /
    `publish_log`。桩会话让"取了几次"变成可数的判据。"""

    def __init__(self, rows=None) -> None:
        self.project = Project(channels=[TcpChannel("tcp-1", port=5020)])
        self.closed = False
        self.revision = 1
        self.calls = 0
        self.fail_now: Exception | None = None
        self.rows_result = rows if rows is not None else [row("A")]
        self.writes: list[tuple] = []
        self.logs: list[tuple[str, str]] = []

    def rows(self, channel):
        self.calls += 1
        if self.fail_now is not None:
            raise self.fail_now
        return self.rows_result

    def write(self, channel, unit_id, area, address, text, bit=None):
        self.writes.append((channel, unit_id, area, address, text, bit))
        return (True, "")

    def publish_log(self, level, text):
        self.logs.append((level, text))


@pytest.fixture
def pump_factory():
    built: list[RowsPump] = []

    def make(session, interval=0.02):
        pump = RowsPump(session, interval=interval)
        built.append(pump)
        return pump

    yield make
    for pump in built:
        pump.stop()


def quickly(predicate, timeout=3.0):
    """有界轮询：后台线程的事不许用裸 `sleep` 去赌时间。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------- 缓存语义 ----------

def test_repeated_reads_of_one_channel_hit_the_cache(pump_factory):
    """同一路的第二次读**不许**再取一次：界面 10Hz 调它，取数在后台那条线上。"""
    session = StubSession()
    pump = pump_factory(session)
    first = pump.rows("tcp-1")
    assert session.calls == 1
    assert pump.rows("tcp-1") is first
    assert session.calls == 1, "同一路读第二次又去取了一遍"


def test_switching_channel_fetches_synchronously(pump_factory):
    """换路要同步取一次：操作员按下"看这一路"，表里立刻就该有内容。"""
    session = StubSession()
    pump = pump_factory(session)
    assert pump.rows("tcp-1")[0].name == "A"
    assert session.calls == 1
    session.rows_result = [row("B")]
    assert pump.rows("tcp-2")[0].name == "B"
    assert session.calls == 2, "换路没有同步取——表会先空一拍"


def test_a_structural_edit_makes_the_pump_refetch(pump_factory):
    """装载/改结构/增删通道之后必须重取：缓存里那份行是上一份点表的形状。"""
    session = StubSession()
    pump = pump_factory(session)
    pump.rows("tcp-1")
    session.rows_result = [row("A"), row("B")]
    session.revision = 2                       # 结构变了（Session 那边会 +1）
    assert len(pump.rows("tcp-1")) == 2
    assert session.calls == 2


def test_a_write_refreshes_right_away(pump_factory):
    """写是用户动作：改完那一眼就该看见新值，不等下一个节拍。"""
    session = StubSession()
    pump = pump_factory(session)
    pump.rows("tcp-1")
    session.rows_result = [row("A", "42")]
    ok, error = pump.write("tcp-1", 11, "hr", 0, "42")
    assert (ok, error) == (True, "")
    assert session.writes == [("tcp-1", 11, "hr", 0, "42", None)]
    assert pump.rows("tcp-1")[0].value_text == "42"


def test_a_failed_write_does_not_refetch(pump_factory):
    """被拒的写没有改变任何东西：多取一次只是白花钱。"""
    session = StubSession()
    pump = pump_factory(session)
    pump.rows("tcp-1")

    class Refusing(StubSession):
        def write(self, *args, **kwargs):
            return (False, "nope")

    session.write = Refusing().write
    assert pump.write("tcp-1", 11, "hr", 0, "x") == (False, "nope")
    assert session.calls == 1


# ---------- 失败与收工 ----------

def test_a_failing_fetch_keeps_the_last_snapshot_and_says_so_once(pump_factory):
    """"读不到"不许把表清空（那就是演"设备没值"）；报一次，别 10Hz 刷日志。"""
    session = StubSession()
    pump = pump_factory(session)
    good = pump.rows("tcp-1")
    session.fail_now = RuntimeError("engine did not answer")
    assert pump.fetch_now() is good, "取失败之后表里的内容被清掉了"
    assert pump.fetch_now() is good
    assert pump.failures == 2 and "engine did not answer" in pump.last_error
    assert len(session.logs) == 1, "同一次故障报了不止一次"
    assert session.logs[0][0] == "warning"


def test_an_empty_channel_never_fetches(pump_factory):
    """没有通道可显示（空工程）时一次都不许发。"""
    session = StubSession()
    pump = pump_factory(session)
    assert pump.rows("") == []
    assert session.calls == 0


def test_the_background_loop_exits_when_the_session_is_closed(pump_factory):
    """Session 收工（关窗/换工程）之后这条线程要自己退场，否则每跳留一条错。"""
    session = StubSession()
    pump = pump_factory(session)
    pump.rows("tcp-1")
    pump.start()
    assert quickly(lambda: session.calls > 1), "后台没按节拍取"
    session.closed = True
    assert quickly(lambda: not pump.alive()), "Session 收工了，泵还在跑"


# ---------- 端到端：真从站 + 真主站 ----------

def free_port() -> int:
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_pump_picks_up_a_master_write_in_the_background(engine_runner):
    """真主站写一个值，背景里那条线程自己把它捞回缓存——问的是界面看到的那一刻。"""
    from pymodbus.client import ModbusTcpClient

    port = free_port()
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = port
    project.channels[0].host = "127.0.0.1"
    session = Session(runner=engine_runner)
    session.load(project)
    assert not session.start_all()
    pump = RowsPump(session, interval=0.02)
    try:
        # 先读一次初值，把缓存和"这一路"定下来
        assert [r.value_text for r in pump.rows("tcp-5020") if r.name == "风机状态"] == ["11"]
        pump.start()
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=2.0)
        try:
            assert client.connect()
            client.write_register(4, 77, device_id=11)
        finally:
            client.close()

        def shown() -> bool:
            return any(r.name == "风机状态" and r.value_text == "77"
                       for r in pump.rows("tcp-5020"))

        assert quickly(shown, timeout=5.0), "主站写的值没有被后台那条线捞回缓存"
    finally:
        pump.stop()
        session.shutdown()
