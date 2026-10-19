"""把会阻塞的启动/网络动作放到工作线程，主线程只跑事件循环与界面更新。

为什么不直接把所有动作写成异步回调：现有调用方（工具栏、面板、用例）都按
"_start_all() 返回时已经完成"使用。这里用嵌套 QEventLoop 保持这个语义，同时让窗口
在 UAC、PowerShell、建网卡、等地址生效这些秒级动作期间仍能重绘和响应。

三条边界：

* 工作线程**不许碰 widget**：`fn` 只做纯动作，结果回到主线程后由调用方更新界面；
* 快动作不闪进度框：250ms 内完成就不显示对话框，只留等待光标；
* 动作期间用 Qt 的应用级模态对话框挡住主窗输入，避免连点叠出第二次启动。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6 import QtCore, QtWidgets

from .busy import busy

# 进度框的延迟：大多数启动在几十毫秒内完成，立即弹框反而像故障。
_SHOW_AFTER_MS = 250

# 操作期间挡住所有用户输入。模态框出现前有 250ms 的窗口，光靠各面板的
# `_operation_busy` 挡不住"打开工程""改点位"这类别的按钮；事件过滤器在应用层一次挡住。
_INPUT_EVENTS = frozenset({
    QtCore.QEvent.Type.KeyPress,
    QtCore.QEvent.Type.KeyRelease,
    QtCore.QEvent.Type.Shortcut,
    QtCore.QEvent.Type.ShortcutOverride,
    QtCore.QEvent.Type.MouseButtonPress,
    QtCore.QEvent.Type.MouseButtonRelease,
    QtCore.QEvent.Type.MouseButtonDblClick,
    QtCore.QEvent.Type.Wheel,
    QtCore.QEvent.Type.ContextMenu,
    QtCore.QEvent.Type.TouchBegin,
    QtCore.QEvent.Type.TouchUpdate,
    QtCore.QEvent.Type.TouchEnd,
})


class _InputBlocker(QtCore.QObject):
    def eventFilter(self, obj, event) -> bool:
        return event.type() in _INPUT_EVENTS


class _Worker(QtCore.QObject):
    """在工作线程里跑一个可调用对象，把结果/异常 queued 回主线程。"""

    finished = QtCore.Signal(object, object)

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    @QtCore.Slot()
    def run(self) -> None:
        try:
            value = self._fn()
        except BaseException as exc:            # noqa: BLE001 —— 原样交回主线程
            self.finished.emit(None, exc)
            return
        self.finished.emit(value, None)


def _same_thread(window: QtWidgets.QWidget) -> bool:
    """当前是不是窗口所在的主线程。"""
    return QtCore.QThread.currentThread() == window.thread()


def run_waiting(window: QtWidgets.QWidget | None, text: str,
                fn: Callable[[], Any]) -> Any:
    """运行 `fn`；窗口可见且在主线程时用工作线程 + 嵌套事件循环，否则同步执行。

    返回 `fn` 的返回值；`fn` 抛出的异常原样重抛。调用方仍然按同步方式写代码。
    """
    app = QtWidgets.QApplication.instance()
    if app is None or window is None or not window.isVisible() or not _same_thread(window):
        return fn()

    done = {"value": None, "error": None, "finished": False}
    loop = QtCore.QEventLoop()
    worker = _Worker(fn)
    dialog = QtWidgets.QProgressDialog(text, "", 0, 0, window)
    dialog.setWindowTitle("设备模拟台")
    dialog.setCancelButton(None)
    dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumDuration(0)
    dialog.setWindowFlag(QtCore.Qt.WindowType.WindowCloseButtonHint, False)

    show_timer = QtCore.QTimer(window)
    show_timer.setSingleShot(True)
    show_timer.timeout.connect(dialog.show)
    show_timer.start(_SHOW_AFTER_MS)

    def finished(value, error) -> None:
        done.update(value=value, error=error, finished=True)
        show_timer.stop()
        dialog.close()
        # 先让工作线程期间积压的桥接事件（ChannelState / LogLine）落到界面上，
        # 再退出嵌套循环；否则调用方 `_start_all()` 一返回就看到旧状态。
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        loop.quit()

    worker.finished.connect(finished, QtCore.Qt.ConnectionType.QueuedConnection)
    app.aboutToQuit.connect(loop.quit)
    blocker = _InputBlocker()
    app.installEventFilter(blocker)

    try:
        with busy(text, window):
            thread = threading.Thread(target=worker.run, name="simbench-waiting",
                                      daemon=True)
            thread.start()
            loop.exec()
    finally:
        app.removeEventFilter(blocker)
    try:
        app.aboutToQuit.disconnect(loop.quit)
    except (RuntimeError, TypeError):
        pass
    dialog.deleteLater()
    # 收尾再排一次：嵌套循环退出前可能还有一条状态事件在路上。
    app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    if done["error"] is not None:
        raise done["error"]
    if not done["finished"]:
        return None                            # 应用正在退出：不再假装动作完成了
    return done["value"]
