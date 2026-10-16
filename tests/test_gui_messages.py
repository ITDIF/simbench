"""界面上那条"中文说明"（`simbench/gui/messages.py`，O6 的落地口径）。

三条判据：

1. **真失败能配上**：拿真从站/真端口造出那几种最常见的失败，断言日志页里出现了中文说明
   ——覆盖率是这一层的全部价值，写死一两条"包含即可"是自证；
2. **原文一个字不动**：英文原话必须还在（命令行、README、冻结冒烟、贴给上游都用它）；
3. **不认识就原样显示**：没有匹配到已知形状时**不许编一句中文**（那会是一句可能不成立的
   话），只显示英文——这条比"覆盖率越高越好"更重要。
"""

from __future__ import annotations

import pathlib
import socket

from PySide6 import QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui import messages
from simbench.gui.main_window import MainWindow
from tests.conftest import free_port
from tests.gui_support import gui_deps

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def make_window(qapp, engine_runner) -> MainWindow:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = free_port()
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    window = MainWindow(session, store, deps=gui_deps())
    window.use_project(project, CSV)
    window.show()
    qapp.processEvents()
    return window


def teardown(window, qapp) -> None:
    window.timer.stop()
    window.rows_pump.stop()
    window.session.shutdown()
    window.session.clear_dirty()
    window.deleteLater()
    qapp.processEvents()


def logs_of(window) -> str:
    window.session.wait_idle(2.0)
    qapp = QtWidgets.QApplication.instance()
    for _ in range(4):                      # 事件是经 Qt 队列投过来的：跑几轮再读
        qapp.processEvents()
    return window.log_view.toPlainText()


# ---------- 纯函数那一层 ----------

def test_an_unknown_message_is_left_exactly_as_it_is():
    """认不出来就原样显示：不许为了让"看起来都有中文"而编一句可能不成立的话。"""
    raw = "channel tcp-1: something nobody has ever seen"
    assert messages.explain(raw) == ""
    assert messages.with_explanation(raw) == raw


def test_an_empty_message_stays_empty():
    assert messages.with_explanation("") == ""


def test_the_original_english_is_never_dropped():
    raw = "channel tcp-1: port 5020 may already be in use"
    rendered = messages.with_explanation(raw)
    assert raw in rendered and has_chinese(rendered)


def test_every_explanation_is_chinese_and_says_something():
    for fragment, hint in messages.EXPLANATIONS:
        assert fragment and has_chinese(hint), fragment
        assert len(hint) > 10, fragment         # 一行"出错了"不算说明


# ---------- 真失败在界面上看到什么 ----------

def test_a_busy_port_shows_a_chinese_explanation(qapp, engine_runner):
    """端口被占（现场最常见的那一条）：英文原话 + 一行中文。"""
    window = make_window(qapp, engine_runner)
    hog = socket.socket()
    try:
        hog.bind(("127.0.0.1", 0))
        hog.listen(8)
        window.session.project.channels[0].port = hog.getsockname()[1]
        window.use_project(window.session.project, window._path)
        qapp.processEvents()
        window._start_all()
        text = logs_of(window)
        assert "may already be in use" in text, "英文原话丢了（命令行/冒烟都靠它）"
        assert "已经有别人在用了" in text, text
    finally:
        hog.close()
        teardown(window, qapp)


def test_a_point_table_with_errors_explains_itself_in_chinese(qapp, engine_runner):
    """点表有错拒绝启动：那句"refusing to start"后面要有中文。"""
    window = make_window(qapp, engine_runner)
    try:
        window.session.project.channels[0].port = 0        # 端口 0 是 validate 拦下的错
        window.use_project(window.session.project, window._path)
        qapp.processEvents()
        window._start_all()
        text = logs_of(window)
        assert "refusing to start" in text
        assert "拒绝启动" in text, text
    finally:
        teardown(window, qapp)


def test_a_write_on_a_stopped_channel_explains_itself(qapp, engine_runner):
    """没在跑的时候改值/交还波形：中文说明。"""
    window = make_window(qapp, engine_runner)
    try:
        window.session.restore_waveform("tcp-5020", 11, "hr", 4, None)
        text = logs_of(window)
        assert "is not running; the waveform" in text
        assert "没有在跑" in text, text
    finally:
        teardown(window, qapp)
