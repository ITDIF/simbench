"""吞异常要留痕（`simbench/swallow.py`）。

这一组钉住的是一种很容易被当成"无所谓"的缺陷：某个回调开始抛异常，而**哪里都看不见**。
投递路径（引擎 → 界面）与通知路径（Session → 订阅者）上各有一处必须吞的地方——吞是为了
"一个坏订阅者不许把采集带停"，但"吞了要记账"是另一半。
"""

from __future__ import annotations

from simbench import swallow
from simbench.backend.events import LogLine
from simbench.engine.runner import EngineRunner
from simbench.engine.session import Session


def test_the_book_keeps_the_first_place_and_the_last_error():
    book = swallow.Swallower("somewhere")
    assert book.count == 0 and book.summary() == ""
    assert book.note(ValueError("first"), "sink A") is True
    assert book.note(ValueError("second"), "sink B") is False
    assert book.count == 2
    assert book.first_place == "sink A" and "first" in book.first_error
    assert "second" in book.last_error, "最后一次的错误没跟上"
    summary = book.summary()
    assert "somewhere" in summary and "first" in summary and "2 次" in summary


def test_note_says_it_out_loud_only_once():
    book = swallow.Swallower("where")
    said: list[str] = []
    swallow.note(book, ValueError("boom"), "sink", sink=said.append)
    swallow.note(book, ValueError("boom again"), "sink", sink=said.append)
    assert len(said) == 1, "同一个吞点说了不止一次（10Hz 的路径上会刷屏）"
    assert "boom" in said[0]


def test_the_engine_keeps_delivering_after_a_sink_blows_up():
    """投递路径的正证：一个订阅者抛异常，**别的订阅者照样收到**，引擎不停，账上有一笔。"""
    runner = EngineRunner(name="swallow-test")
    runner.start()
    good: list[object] = []

    def bad(_event):
        raise RuntimeError("this sink is broken")

    try:
        runner.subscribe(bad)
        runner.subscribe(good.append)
        runner.publish(LogLine("info", "hello"))
        assert runner.drain(2.0), "事件队列没排空"
        assert [line.text for line in good] == ["hello"], "坏订阅者把好订阅者带停了"
        assert runner.swallowed.count == 1
        assert "this sink is broken" in runner.swallowed.first_error
        assert "sink" in runner.swallowed.first_place
    finally:
        runner.stop()


def test_a_session_sink_blowing_up_is_recorded_too():
    runner = EngineRunner(name="swallow-session-test")
    session = Session(runner=runner)
    good: list[object] = []
    try:
        session.subscribe(lambda _event: (_ for _ in ()).throw(RuntimeError("bad sink")))
        session.subscribe(good.append)
        session.publish_log("info", "hello")
        assert runner.drain(2.0)
        assert [line.text for line in good] == ["hello"]
        assert session.swallowed.count == 1
        assert "bad sink" in session.swallowed.first_error
    finally:
        session.shutdown()
        runner.stop()
