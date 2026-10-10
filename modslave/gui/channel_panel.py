"""每个通道一行：host/port 编辑 + 独立启动/停止 + 定宽状态标签。

状态只改文字与颜色：控件不增删、标签定宽且**文案装得下**，所以几何在任何状态下都一样
（用户口径 3/4）。颜色走 `QPalette` 而不是 `setStyleSheet`——样式表会让 Qt 重新 polish
这个控件、sizeHint 有机会跟着变，而一格颜色不该牵动任何度量。

判定分工（`apply_edits` 与 `validate_project` 不许各判一套）：

* 面板只做**转换**：输入框里的文字变成端口号，变不出来（`COM3`、空串）就当场拒——
  那不是一个值，是键盘上的字。
* 端口范围、host 非空、跨通道端口重复这些**判定**只在 `project/validate.py` 那一份里，
  由 `Session.issues()` 现算。流程是"先写回工程 → 立刻按整份工程验一次 → 验出新增的错
  就整体回滚（连输入框一起退回）"，所以界面上看到的拒绝理由和启动前自检是同一套话术，
  不会出现"面板放过了、启动时又被拦下"。

写回成功时顺手 `Session.mark_dirty()`：改地址就是改工程，口径 ε 的"未保存改动"必须
听见这件事，否则改完端口关窗不提示，下次开的是 CSV 里的旧地址。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtGui, QtWidgets

from ..backend.events import ChannelState

# 四态的文案共用一个定宽槽（72px）：offscreen 实测最长的一条 "◐ 启动中" 文字宽 60px、
# QLabel 的 sizeHint 也是 60，装得下；短的三条（48px）也落进同一个宽度里。
# 装不下时要改文案，**不许加宽、也不许截断成省略号**——由
# `test_the_status_label_is_the_same_slot_in_every_state` 钉住。
STATUS_LABELS = {"stopped": "○ 停止", "starting": "◐ 启动中",
                 "running": "● 运行", "error": "▲ 失败"}
STATUS_COLORS = {"stopped": QtGui.QColor("#666666"), "starting": QtGui.QColor("#b26a00"),
                 "running": QtGui.QColor("#1a7f37"), "error": QtGui.QColor("#c62828")}

_HOST_WIDTH = 120
_PORT_WIDTH = 64
_BUTTON_WIDTH = 56
_STATUS_WIDTH = 72


@dataclass
class ChannelRow:
    channel: str
    name_label: QtWidgets.QLabel
    host_edit: QtWidgets.QLineEdit
    port_edit: QtWidgets.QLineEdit
    start_button: QtWidgets.QPushButton
    stop_button: QtWidgets.QPushButton
    status: QtWidgets.QLabel


class ChannelPanel(QtWidgets.QWidget):
    def __init__(self, session, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._rows: dict[str, ChannelRow] = {}
        self._grid = QtWidgets.QGridLayout(self)
        self._grid.setContentsMargins(4, 2, 4, 2)
        self.last_error = ""

    # ---------- 结构 ----------

    def rebuild(self, project) -> None:
        """按工程重画一行一个通道。先清干净上一批控件，不留悬空行。"""
        for index in reversed(range(self._grid.count())):
            item = self._grid.takeAt(index)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        for index, ch in enumerate(project.channels):
            self._rows[ch.name] = self._make_row(index, ch)
            self._set_enabled(ch.name, running=False)

    def rows(self) -> list[ChannelRow]:
        return list(self._rows.values())

    # ---------- 状态 ----------

    def set_status(self, channel: str, state: str) -> None:
        row = self._rows.get(channel)
        if row is None:
            return                     # 状态事件可能比 rebuild 先到：没有那一行就什么也不动
        if state not in STATUS_LABELS:
            # 未知状态不许当成"没发生"留一格旧文字冒充现况，那是编程错误，炸出来
            raise ValueError(f"unknown channel state {state!r}")
        row.status.setText(STATUS_LABELS[state])       # 定宽标签：文字变了也不推挤
        palette = row.status.palette()
        palette.setColor(QtGui.QPalette.WindowText, STATUS_COLORS[state])
        row.status.setPalette(palette)
        self._set_enabled(channel, running=state == "running")

    def on_event(self, event) -> None:
        """主窗/桥把 Session 的 `ChannelState` 送进来（面板自己不订阅，避免跨线程摸 widget）。"""
        if isinstance(event, ChannelState):
            self.set_status(event.channel, event.state)

    # ---------- 地址编辑 ----------

    def apply_edits(self, channel: str) -> int | None:
        """host/port 写回工程。返回写进去的端口；转换不了、通道在跑、验出新增错误都回 None。"""
        row = self._rows.get(channel)
        if row is None:
            raise KeyError(channel)     # 面板上有一行而工程里没有：编程错误，不许静默
        runtime = self._session.channel(channel)
        if runtime is not None and runtime.running:
            return self._refuse(
                f"{channel} is running; stop it before changing the address")
        host = row.host_edit.text().strip()
        text = row.port_edit.text().strip()
        try:
            port = int(text)
        except ValueError:
            return self._refuse(f"{channel}: port {text!r} is not a number")
        target = next((ch for ch in self._session.project.channels
                       if ch.name == channel), None)
        if target is None:
            raise KeyError(channel)
        before = self._errors()
        old_host, old_port = target.host, target.port
        target.host, target.port = host, port
        new = [e for e in self._errors() if e not in before]
        if new:
            # 整体回滚：判定不归面板，但"验不过就当没改"必须在这里做，
            # 否则工程里会留下一个起不来的地址，而输入框显示的是另一个。
            target.host, target.port = old_host, old_port
            row.host_edit.setText(old_host)
            row.port_edit.setText(str(old_port))
            return self._refuse(f"{channel}: {new[0]}")
        self.last_error = ""
        # 改地址就是改工程：不置 dirty 的话主窗标题不带 `*`、关窗也不问（口径 ε），
        # 现场改完端口直接关窗，下次开的是 CSV 里的旧端口。被拒的改动在上面就返回了，
        # 到不了这里——什么都没换，自然不该留下"有未保存改动"。
        self._session.mark_dirty()
        return port

    def geometry_snapshot(self) -> tuple:
        """每个控件的完整矩形：状态换了文字/颜色也不该有任何一项变化。"""
        rects = tuple((row.name_label.geometry().getRect(),
                       row.host_edit.geometry().getRect(),
                       row.port_edit.geometry().getRect(),
                       row.start_button.geometry().getRect(),
                       row.stop_button.geometry().getRect(),
                       row.status.geometry().getRect()) for row in self._rows.values())
        return (self.size().width(), self.size().height(), rects)

    # ---------- 内部 ----------

    def _make_row(self, index, ch) -> ChannelRow:
        name = QtWidgets.QLabel(ch.name)
        name.setMinimumWidth(72)
        host = QtWidgets.QLineEdit(ch.host)
        host.setFixedWidth(_HOST_WIDTH)
        port = QtWidgets.QLineEdit(str(ch.port))
        port.setFixedWidth(_PORT_WIDTH)
        # 端口框刻意**不装** QIntValidator：范围判定只有 validate 那一份，输入框里
        # 悄悄吞掉键击是"没反馈的拒绝"，用户只会觉得点不动。改完就验、验不过就退回去
        # 并说一句人话，比默默拦键好读。
        start = QtWidgets.QPushButton("启动")
        start.setFixedWidth(_BUTTON_WIDTH)
        stop = QtWidgets.QPushButton("停止")
        stop.setFixedWidth(_BUTTON_WIDTH)
        status = QtWidgets.QLabel(STATUS_LABELS["stopped"])
        status.setFixedWidth(_STATUS_WIDTH)
        start.clicked.connect(lambda _=False, n=ch.name: self._start(n))
        stop.clicked.connect(lambda _=False, n=ch.name: self._stop(n))
        port.editingFinished.connect(lambda n=ch.name: self.apply_edits(n))
        host.editingFinished.connect(lambda n=ch.name: self.apply_edits(n))
        for column, widget in enumerate((name, host, port, start, stop, status)):
            self._grid.addWidget(widget, index, column)
        return ChannelRow(ch.name, name, host, port, start, stop, status)

    def _start(self, channel: str) -> None:
        try:
            self._session.start_channel(channel)
        except KeyError:
            raise                       # 通道名都不存在：不是用户输入错误，炸出来给人看
        except Exception as exc:        # noqa: BLE001
            # 点表有错（ValueError）和端口被占（PortInUseError）都到这里：界面那一格变红、
            # 留下原因、并发一条日志。槽函数里抛出去等于 Qt 事件循环里炸，界面看着像卡死。
            self.last_error = f"{channel}: {exc}"
            self._session.publish_log("error", self.last_error)
            self.set_status(channel, "error")
            return
        self.last_error = ""
        self.set_status(channel, "running")

    def _stop(self, channel: str) -> None:
        try:
            self._session.stop_channel(channel)
        except Exception as exc:        # noqa: BLE001
            self.last_error = f"{channel}: {exc}"
            self._session.publish_log("error", self.last_error)
            self.set_status(channel, "error")
            return
        self.last_error = ""
        self.set_status(channel, "stopped")

    def _set_enabled(self, channel: str, running: bool) -> None:
        row = self._rows[channel]
        row.start_button.setEnabled(not running)
        row.stop_button.setEnabled(running)
        row.host_edit.setEnabled(not running)        # 跑着改地址 = 改的不是正在跑的那个
        row.port_edit.setEnabled(not running)

    def _errors(self) -> list[str]:
        """当前拦住启动的那几条。判定住在 `validate_project`，面板一份都不抄。"""
        return [str(issue) for issue in self._session.issues()
                if issue.level == "error"]

    def _refuse(self, message: str) -> None:
        self.last_error = message
        return None
