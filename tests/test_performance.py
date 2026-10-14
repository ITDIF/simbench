"""性能回归（2026-10-14 深度审查的 O3/O4）。

判据都尽量做成**确定性**的：对象身份（缓存有没有生效）、通知次数（有没有整表重画）。
时间只做**宽松**上限（机器忙的时候会抖，而"缓存没生效"这类回归用身份一眼就看得出来）。

实测数字（本机，见计划 §3）：2000 点的表上一跳 `Session.rows()` 从 17.98ms 降到 7.77ms
（缓存生效后引擎整批回读占掉 5.81ms，几乎就是全部）；4000 点从 36.57ms 降到 16.34ms。
报文窗缓冲写满后"每跳新增 10 帧"的重绘从 65.60ms 降到增量通知（见 `test_gui_packet_view`）。
"""

from __future__ import annotations

import contextlib
import socket
import time

from simbench.engine.session import Session
from simbench.project.model import Device, Point, Project, TcpChannel

POINTS = 2000


def free_port() -> int:
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def big_project(points: int = POINTS) -> Project:
    device = Device("D1", 11, [Point(f"P{i}", "hr", i, "uint16", initial=str(i % 1000))
                              for i in range(points)])
    return Project(channels=[TcpChannel("tcp-big", host="127.0.0.1", port=free_port(),
                                       devices=[device])])


@contextlib.contextmanager
def running(engine_runner, points: int = POINTS):
    """起一路真从站（真端口、真 datastore），跑完就收。"""
    session = Session(runner=engine_runner)
    session.load(big_project(points))
    failures = session.start_all()
    assert not failures, failures
    try:
        yield session
    finally:
        session.shutdown()


def test_a_second_tick_reuses_the_very_same_rows(engine_runner):
    """值没变的那一跳**复用上一跳的 `Row` 对象**：不重解码、不重建 dataclass。

    这是缓存生效最直接的证据（比时间稳）：对象身份相同 → 模型那边 `==` 一比就过，
    一个 `dataChanged` 都不会发。
    """
    with running(engine_runner) as session:
        first = session.rows("tcp-big")
        second = session.rows("tcp-big")
        assert len(first) == POINTS
        assert all(a is b for a, b in zip(first, second)), "第二跳重建了行：缓存没生效"


def test_only_the_row_that_changed_comes_back_as_a_new_object(engine_runner):
    """改一个点只重算那一行：其余行的对象身份必须原样（"改一处 = 全表重算"是回到老样子）。"""
    with running(engine_runner) as session:
        before = session.rows("tcp-big")
        ok, error = session.write("tcp-big", 11, "hr", 5, "4242")
        assert (ok, error) == (True, "")
        after = session.rows("tcp-big")
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a is not b]
        assert len(changed) == 1, f"改了 1 个点却重建了 {len(changed)} 行"
        assert after[changed[0]].value_text == "4242"


def test_renaming_a_point_rebuilds_that_row(engine_runner):
    """live 改动（改名）必须让那一行重算：键里没带名字的话表里还是旧名字。

    这条是**回归**：第一版缓存键只带身份与类型，改名之后表里一行都没变
    （`test_a_live_only_apply_from_the_menu_says_next_tick_not_restart` 逮住了它，
    这里把根因也钉住——不依赖界面）。
    """
    with running(engine_runner, points=20) as session:
        before = session.rows("tcp-big")[3]
        session.project.channels[0].devices[0].points[3].name = "改过名的点"
        after = session.rows("tcp-big")[3]
        assert after is not before and after.name == "改过名的点"


def test_a_full_tick_on_two_thousand_points_stays_well_under_the_budget(engine_runner):
    """一跳的**宽松**上限：实测 7.8ms，这里给 60ms（8 倍余量）。

    时间断言只用来逮"整数量级的回归"（比如缓存被绕开、每跳又去解一次码），
    不追单机抖动——所以阈值给得很宽，红了先看别处。
    """
    with running(engine_runner) as session:
        session.rows("tcp-big")                     # 预热
        start = time.perf_counter()
        for _ in range(5):
            session.rows("tcp-big")
        per_tick = (time.perf_counter() - start) / 5 * 1000
        assert per_tick < 60.0, f"2000 点的一跳 {per_tick:.1f}ms，缓存可能没生效"
