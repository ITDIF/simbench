"""后台等待：工作线程 + 嵌套事件循环，保持调用方同步语义但窗口不假死。"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtCore, QtWidgets

from simbench.gui.background import run_waiting


def test_run_waiting_returns_the_value_and_keeps_timers_running(qapp):
    window = QtWidgets.QWidget()
    window.show()
    qapp.processEvents()
    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    try:
        result = run_waiting(window, "正在等待…", lambda: (time.sleep(0.12), 42)[1])
    finally:
        timer.stop()
        window.close()
    assert result == 42
    assert len(ticks) >= 3, "工作线程等待期间事件循环没有跑"
    assert QtWidgets.QApplication.overrideCursor() is None


def test_run_waiting_re_raises_the_worker_exception(qapp):
    window = QtWidgets.QWidget()
    window.show()
    qapp.processEvents()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            run_waiting(window, "正在等待…", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    finally:
        window.close()
    assert QtWidgets.QApplication.overrideCursor() is None
