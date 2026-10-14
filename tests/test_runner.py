"""`EngineRunner`：把 asyncio loop 关在一个专属线程里，并把事件按序送出去。

这套契约是 GUI 与测试共同依赖的地基，所以钉死四件事：
submit 收**协程工厂**（不是协程对象）、stop 幂等且真 join 线程、
回调炸了不许带停引擎、两个 runner 之间不共享任何状态。
"""

import threading
import time

import pytest

from simbench.engine.runner import EngineRunner


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
        assert runner.thread_name.startswith("simbench-engine")
    finally:
        runner.stop()


def test_start_and_stop_are_idempotent():
    runner = EngineRunner()
    runner.start()
    runner.start()          # 幂等：第二次什么都不做
    runner.stop()
    runner.stop()           # 幂等：第二次什么都不做
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
    a.start()
    b.start()
    try:
        assert a.thread_ident != b.thread_ident
        assert a.submit(noop).result(5) == b.submit(noop).result(5) == 42
    finally:
        a.stop()
        b.stop()


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


def test_stop_raises_when_a_thread_refuses_to_die(monkeypatch):
    """join 超时后静默返回会留下看不见的僵尸线程：端口与 loop 都还在它手里。

    现场形状是"下一次启动报端口被占用，而占用者查不到"。这里把两条线程的 join 都改成
    空转，`stop()` 必须抛出线程名而不是悄悄成功；两条都要试完再抛，只抛第一条会把另一条
    永久留在原地。收尾把它们真收掉——留着会污染同一会话里后面用例的线程断言。
    """
    runner = EngineRunner()
    runner.start()
    loop_thread, pump_thread = runner._thread, runner._pump
    monkeypatch.setattr(loop_thread, "join", lambda *_a, **_k: None)
    monkeypatch.setattr(pump_thread, "join", lambda *_a, **_k: None)
    try:
        with pytest.raises(RuntimeError, match="did not stop") as caught:
            runner.stop()
        assert "simbench-engine" in str(caught.value)
        assert "simbench-engine-events" in str(caught.value)
    finally:
        monkeypatch.undo()
        pump_thread.join(5)
        loop_thread.join(5)
        assert not pump_thread.is_alive(), "pump 没收干净"
        assert not loop_thread.is_alive(), "loop 没收干净"


def test_call_reports_where_the_engine_thread_is_stuck():
    """超时消息必须带引擎线程当时的栈。

    裸 `TimeoutError` 在现场等于"引擎没响应但不知道卡在哪"，只能靠复现碰运气。
    """
    runner = EngineRunner()
    gate = threading.Event()

    async def stuck():
        gate.wait(5)
        return "never"

    runner.start()
    try:
        with pytest.raises(TimeoutError) as caught:
            runner.call(lambda: stuck(), 0.3, "start channel tcp-1")
        message = str(caught.value)
        assert "start channel tcp-1" in message
        assert "did not finish within 0.3s" in message
        assert "alive=True" in message
        assert "test_call_reports_where_the_engine_thread_is_stuck" in message or \
            "stuck" in message, message
    finally:
        gate.set()
        runner.stop()
