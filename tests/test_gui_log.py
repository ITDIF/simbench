"""日志页那一排按钮 + 会话日志落盘（界面这一侧的接线）。

对应 `simbench/diagnostics.py` 的三条落点里"日志页的每一行"那一条：窗口里那份给人看（能另存、
能清空），文件那份给事后查（连崩溃都在）。界面版 exe 没有控制台，这两条是现场**唯一**能带
回来的东西，所以单独一组用例钉住。
"""

from __future__ import annotations

import faulthandler
import pathlib
import sys
import threading

import pytest
from PySide6 import QtWidgets

from simbench import diagnostics
from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui import app as app_module
from simbench.gui.main_window import MainWindow
from tests.conftest import free_port
from tests.gui_support import gui_deps

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")

BUTTON_WIDTHS = {"另存日志": 76, "清空": 64}


@pytest.fixture(autouse=True)
def restore_global_hooks():
    """崩溃钩子是进程级的：用完原样还回去。"""
    excepthook, thread_hook = sys.excepthook, threading.excepthook
    was_enabled = faulthandler.is_enabled()
    yield
    sys.excepthook, threading.excepthook = excepthook, thread_hook
    diagnostics._installed.clear()
    if not was_enabled:
        faulthandler.disable()


def make_window(qapp, engine_runner, log_file=None) -> MainWindow:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = free_port()
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    window = MainWindow(session, store, log_file=log_file, deps=gui_deps())
    window.use_project(project, CSV)
    window.resize(1100, 680)
    window.show()
    qapp.processEvents()
    return window


def teardown_window(window: MainWindow, qapp) -> None:
    window.timer.stop()
    window.session.shutdown()
    window.session.clear_dirty()
    window.deleteLater()
    qapp.processEvents()


def test_the_log_tab_buttons_are_fixed_width_slots(qapp, engine_runner):
    """定宽槽（界面口径 4）：两个按钮的宽度是字面量，不随文字/状态变。"""
    window = make_window(qapp, engine_runner)
    try:
        widths = {"另存日志": window.act_save_log.width(), "清空": window.act_clear_log.width()}
        assert widths == BUTTON_WIDTHS
        assert window.act_save_log.minimumWidth() == window.act_save_log.maximumWidth()
    finally:
        teardown_window(window, qapp)


def test_every_log_line_also_lands_in_the_session_log_file(qapp, engine_runner, tmp_path):
    """`_log` 写两处：窗里给人看、文件里给事后查。"""
    log = diagnostics.LogFile(tmp_path / "nested" / "s.log").open()
    window = make_window(qapp, engine_runner, log_file=log)
    try:
        window._log("[refused] 添加点位：这一路正在运行")
        window._log("[info] 已新建工程")
        body = (tmp_path / "nested" / "s.log").read_text(encoding="utf-8")
        assert "[refused] 添加点位：这一路正在运行" in body
        assert "已新建工程" in window.log_view.toPlainText()
        assert window.act_save_log.toolTip().count(str(log.path)) == 1
    finally:
        teardown_window(window, qapp)


def test_the_log_pane_can_be_saved_to_a_file(qapp, engine_runner, tmp_path, monkeypatch):
    window = make_window(qapp, engine_runner)
    target = tmp_path / "exported.txt"
    asked: list[str] = []

    def fake(parent, title, suggested, filters):
        asked.append(suggested)
        return (str(target), "文本文件 (*.txt)")

    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(fake))
    try:
        window._log("[running] tcp-5020 0.0.0.0:5020")
        window._save_log()
        assert asked and asked[0].startswith("simbench-log-")
        assert "[running] tcp-5020" in target.read_text(encoding="utf-8")
        assert "已导出日志" in window.log_view.toPlainText()
        assert list(tmp_path.glob("*.bak")) == [], "导出给别人的文件不该旁边躺一份 .bak"
    finally:
        teardown_window(window, qapp)


def test_a_cancelled_log_export_writes_nothing(qapp, engine_runner, tmp_path, monkeypatch):
    window = make_window(qapp, engine_runner)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    try:
        window._save_log()
        assert list(tmp_path.iterdir()) == []
        assert "已导出日志" not in window.log_view.toPlainText()
    finally:
        teardown_window(window, qapp)


def test_build_app_installs_the_crash_handlers(qapp, engine_runner, tmp_path, monkeypatch):
    """真实入口（`build_app`）一开窗就该有落点，并且告诉操作员日志在哪。"""
    monkeypatch.setenv(diagnostics.LOG_DIR_ENV, str(tmp_path / "logs"))
    _app, window, _session = app_module.build_app([])
    try:
        assert sys.excepthook is not sys.__excepthook__, "崩溃钩子没装上"
        body = (tmp_path / "logs" / f"simbench-{__import__('datetime').date.today()}.log")
        text = body.read_text(encoding="utf-8")
        assert "本次会话的日志写在" in text
        assert "本次会话的日志写在" in window.log_view.toPlainText()
        assert window.log_file is not None
    finally:
        teardown_window(window, qapp)

def test_a_swallowed_callback_failure_says_so_once_in_the_log(qapp, engine_runner):
    """一个正在抛的订阅者回调要在日志页留一句（`simbench/swallow.py`）。

    旧写法下它**一点痕迹都没有**：表现是"界面有时候不跟着变了"而日志干干净净。
    只报一次——10Hz 的心跳里每次报一遍会把日志窗刷满。
    """
    window = make_window(qapp, engine_runner)
    try:
        window.session.swallowed.note(ValueError("boom in a sink"), "sink <bad>")
        window._tick()
        text = window.log_view.toPlainText()
        assert "有回调失败" in text and "boom in a sink" in text
        window._tick()
        assert window.log_view.toPlainText().count("有回调失败") == 1, "同一笔故障报了不止一次"
    finally:
        teardown_window(window, qapp)
