"""`EngineRunner`：把 asyncio loop 关在一个专属线程里，并把事件按序送出去。

这套契约是 GUI 与测试共同依赖的地基，所以钉死四件事：
submit 收**协程工厂**（不是协程对象）、stop 幂等且真 join 线程、
回调炸了不许带停引擎、两个 runner 之间不共享任何状态。
"""

import threading
import time

import pytest

from modslave.engine.runner import EngineRunner


async def noop():
    return 42


def emit_events(runner: EngineRunner, count: int):
    """生成"往队列里塞 count 条事件"的协程工厂（submit 只收工厂）。"""
    async def emit_all():
        for i in range(count):
            runner.publish(("e", i))
    return emit_all


def test_submit_returns_result_and_runs_on_engine_thread():
    runner = EngineRunner()
    runner.start()
    try:
        assert runner.submit(noop).result(5) == 42
        assert runner.thread_ident != threading.get_ident()
        assert runner.thread_name.startswith("modslave-engine")
    finally:
        runner.stop()


def test_start_and_stop_are_idempotent():
    runner = EngineRunner()
    runner.start(); runner.start()
    runner.stop(); runner.stop()
    assert not runner.alive
    with pytest.raises(RuntimeError, match="not running"):
        runner.submit(noop)


def test_stop_joins_the_thread_so_nothing_leaks():
    runner = EngineRunner()
    runner.start()
    tid = runner.thread_ident
    assert tid is not None
    runner.stop()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        alive = [t for t in threading.enumerate() if t.ident == tid]
        if not alive:
            break
        time.sleep(0.02)
    else:
        pytest.fail("引擎线程没被 join 掉")


def test_a_failing_coroutine_raises_out_not_silently():
    async def boom():
        raise ValueError("inner")

    runner = EngineRunner()
    runner.start()
    try:
        with pytest.raises(ValueError, match="inner"):
            runner.submit(boom).result(5)
    finally:
        runner.stop()


def test_events_reach_the_subscriber_with_ordering():
    """runner 只负责把事件按序交到订阅者手里，不猜 Qt。"""
    runner = EngineRunner()
    seen: list[int] = []
    runner.subscribe(seen.append)
    runner.start()
    try:
        runner.submit(emit_events(runner, 30)).result(5)
    finally:
        runner.stop()
    assert seen == [("e", i) for i in range(30)]


def test_two_runners_do_not_share_state():
    a, b = EngineRunner(), EngineRunner()
    a.start(); b.start()
    try:
        assert a.thread_ident != b.thread_ident
        assert a.submit(noop).result(5) == b.submit(noop).result(5) == 42
    finally:
        a.stop(); b.stop()


def test_a_raising_subscriber_does_not_take_the_engine_down():
    """订阅者（UI 回调）抛异常只算它自己失败：引擎继续跑，别的订阅者继续收。

    真实形状是关窗之后 queued signal 打到已经析构的 model 上。那一刻如果带停 loop 线程，
    操作员看到的是"从站莫名失联"，而平台侧只表现为超时——查起来最贵的一种 bug。
    """
    runner = EngineRunner()
    angry: list[object] = []
    calm: list[object] = []

    def raise_back(event):
        angry.append(event)
        raise RuntimeError("widget already gone")

    runner.subscribe(raise_back)
    runner.subscribe(calm.append)
    runner.start()
    try:
        runner.submit(emit_events(runner, 10)).result(5)
        assert runner.alive, "回调抛异常把引擎线程带停了"
        assert runner.submit(noop).result(5) == 42, "引擎还在但已经不接受提交"
    finally:
        runner.stop()
    assert angry == [("e", i) for i in range(10)], "抛异常的订阅者自己也被跳过了"
    assert calm == [("e", i) for i in range(10)], "后面的订阅者被前一个的异常带走"


def test_drain_only_reports_true_when_the_queue_is_really_empty():
    """`drain()` 是给关窗路径用的：队列没排空就必须返回 False，不许"等一下算排空"。"""
    runner = EngineRunner()
    gate = threading.Event()

    def block_first(_event):
        gate.wait(5)

    runner.subscribe(block_first)
    runner.start()
    try:
        runner.submit(emit_events(runner, 5)).result(5)
        assert runner.drain(0.2) is False, "pump 还卡在第一条上，drain 不许说排空了"
        gate.set()
        assert runner.drain(2.0) is True, "队列明明空了却报没排空"
    finally:
        runner.stop()
