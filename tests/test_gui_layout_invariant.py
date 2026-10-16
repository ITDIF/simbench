"""布局恒等断言：stopped / running / error 三态下所有几何必须一模一样。

用户在网页端为"状态一变布局就跳"打过两次回，所以这里不靠肉眼看，全靠 offscreen 实测几何
（M2 前置探针已证明该平台下 `show()` + `processEvents()` 之后量到的是真宽度）。

覆盖的三态是**真三态**：running 由真端口起来的真通道给出，error 由"端口被别人占着"给出
（占位方在 `127.0.0.1`、模拟器按点表绑 `0.0.0.0`，正是裸 bind 探针看不见的那一种）。
"""

import pathlib
import socket
from collections import Counter

import pytest
from PySide6 import QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui.main_window import MainWindow
from simbench.project.model import Device, Point, TcpChannel
from tests.conftest import free_port
from tests.gui_support import gui_deps

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def loaded(port: int) -> object:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = port
    return project


def listening_hog():
    hog = socket.socket()
    hog.bind(("127.0.0.1", 0))
    hog.listen(128)
    return hog, hog.getsockname()[1]


@pytest.fixture
def window(qapp, engine_runner):
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    w = MainWindow(session, store, deps=gui_deps())
    w.use_project(loaded(free_port()), CSV)
    w.resize(1100, 680)
    w.show()
    qapp.processEvents()
    yield w
    w.timer.stop()
    w.session.shutdown()
    w.deleteLater()
    qapp.processEvents()


def snapshot(window) -> dict:
    """所有会影响操作员"点哪里"的几何量。

    刻意不含**内容**（行数、帧数、状态文字）：那些本来就该随状态变，混进来等于往恒等
    断言里塞一个注定不等的量，下一次改布局就没人看得懂这条失败。
    """
    table, tree = window.point_table, window.tree
    return {
        "window": (window.width(), window.height()),
        "splitter": window.splitter.sizes(),
        "tree": (tree.geometry().getRect(), tree.columnWidth(0), tree.columnWidth(1),
                 tree.rowHeight(tree.indexFromItem(tree.topLevelItem(0)))),
        "table": (table.geometry().getRect(), table.rowHeight(0),
                  [table.columnWidth(i) for i in range(window.point_model.columnCount())],
                  table.verticalScrollBar().isVisible(),
                  table.horizontalScrollBar().isVisible()),
        "channel_panel": window.channels.geometry_snapshot(),
        "tabs": window.tabs.geometry().getRect(),
        "packet_panel": window.packet_panel.geometry_snapshot(),
        "packet_table_rows": window.packet_panel.table.geometry().getRect(),
        "log_view": window.log_view.geometry().getRect(),
        "status": window.status.geometry().getRect(),
        "toolbar": [(window.toolbar.geometry().getRect(),
                     [window.toolbar.actionGeometry(a).getRect()
                      for a in window.toolbar.actions()])],
    }


def test_three_states_keep_the_layout_identical(window, qapp):
    base = snapshot(window)
    assert window.tree.topLevelItem(0).text(1) == "○"

    window._start_all()
    qapp.processEvents()
    window._tick()
    assert window.session.channel("tcp-5020").running
    assert window.tree.topLevelItem(0).text(1) == "●"
    running = snapshot(window)
    assert running == base, _diff(base, running, "启动后布局挪了")

    window._stop_all()
    qapp.processEvents()
    window._tick()
    assert snapshot(window) == base, "停止后布局没回到原样"

    hog, port = listening_hog()
    try:
        window.session.project.channels[0].port = port
        window.use_project(window.session.project, window._path)
        qapp.processEvents()
        window._start_all()
        qapp.processEvents()
        window._tick()
        assert not window.session.channel("tcp-5020").running
        assert window.tree.topLevelItem(0).text(1) == "▲", "没进 error 态，这条测试没测到"
        errored = snapshot(window)
        assert errored == base, _diff(base, errored, "启动失败后布局挪了")
    finally:
        hog.close()


def test_the_summary_line_does_not_move_with_a_second_channel(window, qapp):
    """通道数从 1 变 2 是合法的内容增长，但它不许把报文面板的格子顶掉。"""
    window._start_all()
    qapp.processEvents()
    before = window.packet_panel.geometry_snapshot()
    project = window.session.project
    project.channels.append(TcpChannel(
        name="tcp-c", host="127.0.0.1", port=free_port(),
        devices=[Device(name="D2", unit_id=99,
                        points=[Point("温度", "hr", 0, "uint16", initial="1")])]))
    window.use_project(project, window._path)
    qapp.processEvents()
    window._start_all()
    qapp.processEvents()
    window._tick()
    after = window.packet_panel.geometry_snapshot()
    assert after == before, _diff(before, after, "多一个通道把报文面板挪了/压了")
    assert window.packet_panel.table.geometry().height() > 100


def test_no_control_that_is_visible_becomes_invisible(window, qapp):
    """既有可见元素不许消失（用户口径 3）：三态各扫一遍所有子控件的可见性。"""
    def visible():
        """可见控件的多重集合：(类名, 矩形)。

        按矩形而不是按对象身份比，是因为换工程会把通道面板的控件**重建**一遍——在操作员
        眼里那一格还在原地、还是那个大小，就不算"消失了"。反过来，真被隐藏或被挪走的
        控件会带着自己的矩形从集合里掉出去，这条就红。
        """
        return Counter((type(widget).__name__, widget.geometry().getRect())
                       for widget in window.findChildren(QtWidgets.QWidget)
                       if widget.isVisible())

    before = visible()
    window._start_all()
    qapp.processEvents()
    window._tick()
    after_running = visible()
    window._stop_all()
    qapp.processEvents()
    window._tick()
    after_stopped = visible()
    hog, port = listening_hog()
    try:
        window.session.project.channels[0].port = port
        window.use_project(window.session.project, window._path)
        qapp.processEvents()
        window._start_all()
        qapp.processEvents()
        window._tick()
        after_error = visible()
    finally:
        hog.close()
    assert not before - after_running, _missing(before - after_running)
    assert not before - after_stopped, _missing(before - after_stopped)
    assert not before - after_error, _missing(before - after_error)


def test_the_packet_panel_does_not_get_a_written_height(window, qapp):
    """会增长的区块不许写死高度/最大高度（用户口径 4）：它把同栏兄弟面板挤没。"""
    limit = 16777215                      # QtWidgets.QWIDGETSIZE_MAX
    assert window.packet_panel.maximumHeight() == limit
    assert window.packet_panel.table.maximumHeight() == limit
    assert window.tabs.maximumHeight() == limit
    # 报文吃满一整屏之后，兄弟面板还在原来的位置
    store = window.store
    before = snapshot(window)
    _flood(store, 5000)
    qapp.processEvents()
    window._tick()
    assert window.packet_panel.row_count() == store.capacity
    assert snapshot(window) == before, _diff(before, snapshot(window), "报文满了布局挪了")
    assert window.tree.isVisible() and window.point_table.isVisible()


def _flood(store, count: int) -> None:
    from simbench.events import PacketEvent

    store.paused = False
    for index in range(count):
        store.add(PacketEvent("tcp-5020", "rx" if index % 2 else "tx",
                              b"\x00\x01\x00\x00\x00\x06\x0b\x03\x00\x00\x00\x01",
                              11, 3, f"unit 11 fc 3 {index}", float(index)))


def _diff(before: dict, after: dict, label: str) -> str:
    """失败信息直接指出哪一项动了：只说"字典不等"没人改得动。"""
    moved = {key: (before[key], after[key]) for key in before
             if before[key] != after.get(key)}
    return f"{label}: {moved}"


def _missing(gone) -> str:
    return f"这些控件在状态变化后看不见了（类名, 矩形, 少了几个）: {sorted(gone.items())}"
