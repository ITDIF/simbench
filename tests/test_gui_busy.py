"""等待动画：`gui/busy.py` 的等待光标 + 状态栏文案。

为什么钉（2026-10-19 现场："整个项目如果需要等待的能不能加上等待动画"）：启动/停止、网络准备、
大表读写都会阻塞界面，没有反馈时操作员会以为"点了没反应"。等待光标是**不占布局**的动画
（spec §7 的布局恒等），这里钉住它真的出现、并且**一定还原**（异常路径也一样）。
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

from simbench.gui.busy import busy


def _cursor_shape():
    cursor = QtWidgets.QApplication.overrideCursor()
    return None if cursor is None else cursor.shape()


def test_busy_shows_a_wait_cursor_and_restores_it(qapp):
    assert _cursor_shape() is None, "上一条用例把光标留下来了"
    with busy("正在启动…"):
        assert _cursor_shape() == QtCore.Qt.CursorShape.WaitCursor
    assert _cursor_shape() is None, "等待光标没有还原"


def test_busy_restores_the_cursor_even_when_the_body_raises(qapp):
    with pytest.raises(RuntimeError), busy("正在启动…"):
        raise RuntimeError("boom")
    assert _cursor_shape() is None, "异常路径把等待光标留下了"


class _WindowWithStatus(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.status = QtWidgets.QStatusBar(self)


def test_busy_puts_the_text_in_the_status_bar_and_puts_the_old_one_back(qapp):
    window = _WindowWithStatus()
    window.status.showMessage("上一句")
    with busy("正在启动…", window=window):
        assert window.status.currentMessage() == "正在启动…"
    assert window.status.currentMessage() == "上一句", "退出时没还回原来那句"


def test_a_result_message_inside_the_block_is_not_overwritten(qapp):
    """块里把状态栏改成结果（"已启动 1 条通道"）时，退出不许拿旧文案盖掉它。"""
    window = _WindowWithStatus()
    window.status.showMessage("上一句")
    with busy("正在启动…", window=window):
        window.status.showMessage("已启动 1 条通道")
    assert window.status.currentMessage() == "已启动 1 条通道"
