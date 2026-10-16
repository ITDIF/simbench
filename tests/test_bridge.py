"""`EngineBridge`：跨线程投递的唯一落点。

这两条用例钉的是 M2 前置实测第 2 条（工作线程 emit，主线程全收到、顺序不乱、
0 条落错线程）在代码里的回归版本——实测只证明 Qt 干得到，不证明我们写得对。
"""

import threading

from PySide6 import QtCore

from simbench.events import ChannelState, LogLine, PacketEvent, PointChanged
from simbench.gui.bridge import EngineBridge


def test_forward_reaches_main_thread_from_worker(qapp):
    """跨线程投递：worker emit，主线程收到。实测口径的回归版本。"""
    bridge = EngineBridge()
    got: list[object] = []
    threads: list[int] = []
    main_tid = threading.get_ident()
    bridge.packet.connect(lambda e: (got.append(e), threads.append(threading.get_ident())))

    def worker():
        for i in range(20):
            bridge.forward(PacketEvent("c", "rx", bytes([i]), i, 3, "x", 0.0))

    t = threading.Thread(target=worker)
    t.start()
    deadline = QtCore.QTime.currentTime().addMSecs(3000)
    while len(got) < 20 and QtCore.QTime.currentTime() < deadline:
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 20)
    t.join(2)
    qapp.processEvents()

    assert len(got) == 20, f"只收到 {len(got)} 条"
    assert set(threads) == {main_tid}, "有 signal 落在了非主线程"
    assert [p.raw[0] for p in got] == list(range(20)), "投递顺序乱了"


def test_unknown_event_becomes_a_warning_not_silence(qapp):
    bridge = EngineBridge()
    got: list[object] = []
    bridge.logged.connect(got.append)
    bridge.forward(object())
    qapp.processEvents()
    assert got and got[0].level == "warning"


def test_each_event_type_lands_on_its_own_signal(qapp):
    """四类事件各走各的 signal：串台（把 ChannelState 发给 logged）当场算违规者。

    上面两条只覆盖了 packet 与未知事件；`forward()` 有四个分支，不逐个点名的话，
    分支写反（`PointChanged` emit 到 `channel_state`）照样全绿，而界面那边表现为
    "报文窗有数据、设备树不动"这种查起来最费时间的现象。
    """
    cases = [
        (PacketEvent("c", "rx", b"\x00", 1, 3, "p", 0.0), "packet"),
        (PointChanged("c", 1, "holding", 0, 12.5, [0]), "point_changed"),
        (ChannelState("c", "running", ""), "channel_state"),
        (LogLine("info", "hello"), "logged"),
    ]
    for event, signal_name in cases:
        bridge = EngineBridge()
        hits: dict[str, list[object]] = {name: [] for _, name in cases}
        for name, sink in hits.items():
            getattr(bridge, name).connect(sink.append)
        bridge.forward(event)
        qapp.processEvents()
        assert hits[signal_name] == [event], f"{type(event).__name__} 没落到 {signal_name}"
        others = {n: v for n, v in hits.items() if n != signal_name and v}
        assert not others, f"{type(event).__name__} 串到了 {sorted(others)}"
