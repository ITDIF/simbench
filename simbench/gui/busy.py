"""等待动画的唯一出口：等待光标 + 状态栏一句，**不新增控件**。

为什么不是转圈控件（2026-10-19 现场：整个项目凡是要等的都想要个动画）：这个界面的口径是
「状态变化只改文字与颜色、不动几何」（spec §7 的布局恒等），往状态栏塞一个动画控件会让那一栏
在忙/闲之间改宽度——正是那批几何用例防的形状。等待光标（`WaitCursor`）本身就是转圈动画，
它不占布局；再配一句状态栏文字，操作员就知道「在忙、在忙什么」。

用法：

    with busy("正在启动通道…"):
        self.session.start_all()

两条要点（都对应"看起来还是卡住"的那次实测）：

* 进块前 `processEvents(ExcludeUserInputEvents)` 一次——不刷这一次，光标和文字要等阻塞结束
  才画出来，等于没有动画；排除输入事件，是为了不让用户在阻塞期间再点出第二层操作。
* 出块时**只在文案还是自己那句**时才恢复上一条：块里通常会把状态栏改成结果
  （「已启动 1 条通道」），无条件恢复会把结果盖掉。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from PySide6 import QtCore, QtWidgets


@contextlib.contextmanager
def busy(text: str, window: QtWidgets.QWidget | None = None) -> Iterator[None]:
    """在块内显示等待光标与一句状态栏文案；退出时一定还原（异常路径也一样）。

    `window` 不传就用当前活动窗口（生产里的主窗）；纯内核用例没有 QApplication 时什么都不做。
    """
    app = QtWidgets.QApplication.instance()
    if app is None:
        yield
        return
    target = window if window is not None else app.activeWindow()
    status = getattr(target, "status", None) if target is not None else None
    previous = status.currentMessage() if status is not None else ""
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
    try:
        if status is not None:
            status.showMessage(text)
        # 先刷一次界面，否则光标/文案要等阻塞结束才出现（那就等于没有动画）。
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        yield
    finally:
        # processEvents 本身也可能抛（某个槽函数出错），所以 setOverrideCursor 之后
        # 的所有动作都必须在 try 里，保证光标一定还原。
        QtWidgets.QApplication.restoreOverrideCursor()
        if status is not None and status.currentMessage() == text:
            status.showMessage(previous)
