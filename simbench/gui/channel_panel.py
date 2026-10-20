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

from PySide6 import QtCore, QtGui, QtWidgets

from ..events import ChannelState
from ..project.model import channel_transport, format_frame, is_serial, parse_frame
from . import theme
from .background import run_waiting
from .channel_state import STATUS_LABELS

# 四态的文案共用一个定宽槽（88px）：主题把正文字号提到 10pt 之后 offscreen 实测最长的
# 一条 "◐ 启动中" 文字宽 65px（9pt 时是 60px），88 是"装得下 + 留一点余量"；
# 短的三条（52px）也落进同一个宽度里。
# 装不下时要改文案，**不许加宽、也不许截断成省略号**——由
# `test_the_status_label_is_the_same_slot_in_every_state` 钉住。
#
# 四态的符号与中文在 `channel_state.py` 一处定义（面板、设备树、状态栏摘要共用一份）；
# 颜色不在这里定义：深浅两套配色各有一组（白底上合适的绿放到深底上快看不见），
# 由 `theme.status_colors()` 按当前模式给。

_HOST_WIDTH = 120
_PORT_WIDTH = 64
# 帧格式那一格（只对串口行有意义：`8E1`）：与其它槽一样是定宽槽，TCP 行留空并置灰。
_FRAME_WIDTH = 56
# MQTT 的 topic 前缀（默认 simbench）：只对 MQTT 行启用，其它行留空并置灰。
_PREFIX_WIDTH = 110
_BUTTON_WIDTH = 56
_STATUS_WIDTH = 88


@dataclass
class ChannelRow:
    channel: str
    serial: bool                  # 串口通道：那一行的三格换含义（串口名/波特率/帧格式）
    s7: bool                      # S7 通道：第三格是 rack/slot（前两格仍是 host/port）
    mqtt: bool                    # MQTT 通道：前两格是 broker host/port，第三格是 QoS
    name_label: QtWidgets.QLabel
    host_edit: QtWidgets.QLineEdit
    port_edit: QtWidgets.QLineEdit
    frame_edit: QtWidgets.QLineEdit
    prefix_edit: QtWidgets.QLineEdit
    start_button: QtWidgets.QPushButton
    stop_button: QtWidgets.QPushButton
    status: QtWidgets.QLabel


# 卡片最多露出几行：通道再多也只占这么高，其余行滚动。4 路时还没感觉，
# 十几路时整块面板会把下面的树和点位表挤出屏幕（截图反馈）。
_VISIBLE_ROWS = 3


class ChannelPanel(QtWidgets.QWidget):
    def __init__(self, session, parent: QtWidgets.QWidget | None = None,
                 *, before_start=None) -> None:
        super().__init__(parent)
        self._session = session
        # 启动前的钩子（主窗注入）：把这一路要监听的"具体 IP"配到专用网卡上。
        # 面板自己不认识 net 那一层——它只负责"点启动时先问一句"（2026-10-19）。
        self._before_start = before_start
        self._rows: dict[str, ChannelRow] = {}
        # 白面卡片：底色与边框由主题给，这里只把 WA_StyledBackground 打开——裸 QWidget
        # 不开这个属性，QSS 的 background 一个字都不会画（见 theme.make_card）。
        theme.make_card(self)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)      # 留出卡片边框
        # 行放在滚动区里：高度由 `_fit_scroll()` 卡在 `_VISIBLE_ROWS` 行，
        # 通道多时出竖向滚动条，而不是把下面的界面往下推。
        self._scroll = QtWidgets.QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        # 滚动区默认用 Base 色铺底，会盖掉卡片的白面；让父级的卡片底色透出来。
        self._scroll.viewport().setAutoFillBackground(False)
        self._host = QtWidgets.QWidget()
        self._host.setAutoFillBackground(False)
        self._grid = QtWidgets.QGridLayout(self._host)
        self._grid.setContentsMargins(4, 2, 4, 2)
        # 尾部留一个空的**拉伸列**（第 7 列，前面只用到第 6 列）。不给它的话，多出来的
        # 宽度会全部落进第一格"通道名"里，整行被摊成"名字贴最左、参数与按钮贴最右"两撮，
        # 中间一大片空白（截图照出来的）。有了它，行内容左对齐、余量在行尾。
        self._grid.setColumnStretch(8, 1)
        self._scroll.setWidget(self._host)
        outer.addWidget(self._scroll)
        self.last_error = ""
        self._operation_busy = False

    # ---------- 结构 ----------

    def rebuild(self, project, only: str | list[str] | None = None) -> None:
        """按工程重画一行一个通道。`only` 给名字（或名字列表）时只画那几路。

        主窗的紧凑模式每次都只画当前选中的通道；`only=[]` 用来清空（空工程）。
        """
        if isinstance(only, str):
            wanted: set[str] | None = {only}
        elif only is None:
            wanted = None
        else:
            wanted = set(only)
        for index in reversed(range(self._grid.count())):
            item = self._grid.takeAt(index)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        channels = [ch for ch in project.channels
                    if wanted is None or ch.name in wanted]
        for index, ch in enumerate(channels):
            self._rows[ch.name] = self._make_row(index, ch)
            self._set_enabled(ch.name, running=False)
        self._fit_scroll()
        self._scroll.verticalScrollBar().setValue(0)

    def rows(self) -> list[ChannelRow]:
        return list(self._rows.values())

    def shown_channel(self) -> str | None:
        """当前显示的是哪一路（主窗只会画选中的那一路）；没有回 None。"""
        return next(iter(self._rows), None)

    def _fit_scroll(self) -> None:
        """把滚动区高度卡在 `_VISIBLE_ROWS` 行；行数少时自然收矮。"""
        if not self._rows:
            self._scroll.setFixedHeight(0)
            self.setFixedHeight(2)               # 只剩边框
            return
        first = next(iter(self._rows.values()))
        # 行高取这一行**所有控件**的最大 sizeHint：只看标签/按钮会漏掉输入框
        # （QLineEdit 通常更高），算出来的高度会装不下整行。
        row_h = max(widget.sizeHint().height() for widget in (
            first.name_label, first.host_edit, first.port_edit, first.frame_edit,
            first.start_button, first.stop_button, first.status))
        spacing = self._grid.verticalSpacing()
        if spacing < 0:
            spacing = self.style().pixelMetric(
                QtWidgets.QStyle.PM_LayoutVerticalSpacing)
        margins = self._grid.contentsMargins()
        # 不用 `grid.sizeHint()`：刚 rebuild 完它可能还是旧值（实测重建后算成 4px，
        # 面板会塌成一条缝）；按行数直接算，结果与布局时序无关。
        visible = min(len(self._rows), _VISIBLE_ROWS)
        height = (visible * row_h + max(0, visible - 1) * spacing
                  + margins.top() + margins.bottom())
        self._scroll.setFixedHeight(height)
        self.setFixedHeight(height + 2)

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
        palette.setColor(QtGui.QPalette.WindowText, theme.status_colors()[state])
        row.status.setPalette(palette)
        # `error` 有两种，端口占没占是唯一分界：起不来（没占，只能再点启动）和关不掉
        # （还占着，必须让他再点一次停止，否则那一格成了"停不掉的运行中"）。
        runtime = self._session.channel(channel)
        holding = state == "running" or (state == "error" and runtime is not None
                                        and runtime.running)
        self._set_enabled(channel, running=holding)

    def on_event(self, event) -> None:
        """主窗/桥把 Session 的 `ChannelState` 送进来（面板自己不订阅，避免跨线程摸 widget）。"""
        if isinstance(event, ChannelState):
            self.set_status(event.channel, event.state)

    # ---------- 地址编辑 ----------

    def apply_edits(self, channel: str) -> int | None:
        """把这一行的编辑写回工程。返回写进去的端口（串口通道是波特率，那一行没有端口）；

        转换不了（`COM3` 塞进端口格、帧格式写成 `8X1`）、通道在跑、验出新增错误都回 None。
        """
        row = self._rows.get(channel)
        if row is None:
            raise KeyError(channel)     # 面板上有一行而工程里没有：编程错误，不许静默
        runtime = self._session.channel(channel)
        if runtime is not None and runtime.running:
            return self._refuse(
                f"{channel} is running; stop it before changing the address")
        target = next((ch for ch in self._session.project.channels
                       if ch.name == channel), None)
        if target is None:
            raise KeyError(channel)
        if row.serial:
            return self._apply_serial_edits(row, target, channel)
        if row.s7:
            return self._apply_s7_edits(row, target, channel)
        if row.mqtt:
            return self._apply_mqtt_edits(row, target, channel)
        return self._apply_tcp_edits(row, target, channel)

    def _apply_tcp_edits(self, row: ChannelRow, target, channel: str) -> int | None:
        """TCP 行：地址格 + 端口格。"""
        host = row.host_edit.text().strip()
        text = row.port_edit.text().strip()
        try:
            port = int(text)
        except ValueError:
            return self._refuse(f"{channel}: port {text!r} is not a number")

        def restore(old) -> None:
            row.host_edit.setText(old["host"])
            row.port_edit.setText(str(old["port"]))

        return self._commit(channel, target, {"host": host, "port": port},
                            restore, result=port)

    def _apply_serial_edits(self, row: ChannelRow, target, channel: str) -> int | None:
        """串口行：串口名格 + 波特率格 + 帧格式格（`8E1`）。

        帧格式**在这里转**（`8E1` -> 数据位/校验/停止位三个字段），转不出来当场拒：
        与"端口格里的 `COM3` 不是数"同一类，那是键盘上的字，不是工程里的值；
        而"9N1"这种形状对、值不合法的一路走到 validate 去报，两处不各判一套。
        """
        port = row.host_edit.text().strip()
        text = row.port_edit.text().strip()
        frame_text = row.frame_edit.text().strip()
        try:
            baudrate = int(text)
        except ValueError:
            return self._refuse(f"{channel}: baudrate {text!r} is not a number")
        frame = parse_frame(frame_text)
        if frame is None:
            return self._refuse(f"{channel}: frame {frame_text!r} must look like 8N1 "
                                f"(data bits / parity / stop bits)")

        def restore(old) -> None:
            row.host_edit.setText(old["port"])
            row.port_edit.setText(str(old["baudrate"]))
            row.frame_edit.setText(format_frame(old["bytesize"], old["parity"],
                                                old["stopbits"]))

        changes = {"port": port, "baudrate": baudrate, **frame}
        return self._commit(channel, target, changes, restore, result=baudrate)

    def _apply_mqtt_edits(self, row: ChannelRow, target, channel: str) -> int | None:
        """MQTT 行：broker 地址格 + 端口格 + QoS 格（0/1/2）。"""
        host = row.host_edit.text().strip()
        text = row.port_edit.text().strip()
        text_qos = row.frame_edit.text().strip()
        prefix = row.prefix_edit.text().strip()
        try:
            port = int(text)
            qos = int(text_qos)
        except ValueError:
            return self._refuse(f"{channel}: port/QoS {text!r}/{text_qos!r} is not a number")

        def restore(old) -> None:
            row.host_edit.setText(old["host"])
            row.port_edit.setText(str(old["port"]))
            row.frame_edit.setText(str(old["qos"]))
            row.prefix_edit.setText(old["topic_prefix"])

        return self._commit(channel, target,
                            {"host": host, "port": port, "qos": qos,
                             "topic_prefix": prefix},
                            restore, result=port)

    def _apply_s7_edits(self, row: ChannelRow, target, channel: str) -> int | None:
        """S7 行：地址格 + 端口格 + `rack/slot` 格（`0/1` 这种写法）。

        只做**转换**（端口是整数、`rack/slot` 拆成两个数是整数），范围与"同 host:port
        的两个通道"这类判定全在 validate 那一份里——与串口/TCP 两条路同一条分工。
        """
        host = row.host_edit.text().strip()
        text_port = row.port_edit.text().strip()
        text_rack = row.frame_edit.text().strip()
        try:
            port = int(text_port)
        except ValueError:
            return self._refuse(f"{channel}: port {text_port!r} is not a number")
        parts = text_rack.split("/")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            return self._refuse(f"{channel}: rack/slot {text_rack!r} must look like 0/1")
        rack, slot = int(parts[0]), int(parts[1])

        def restore(old) -> None:
            row.host_edit.setText(old["host"])
            row.port_edit.setText(str(old["port"]))
            row.frame_edit.setText(f"{old['rack']}/{old['slot']}")

        changes = {"host": host, "port": port, "rack": rack, "slot": slot}
        return self._commit(channel, target, changes, restore, result=port)

    def _commit(self, channel: str, target, changes: dict, restore, result):
        """写回 + 整份验一次 + 验出新增错误就整体回滚（连输入框一起退回）。

        判定不归面板，但"验不过就当没改"必须在这里做：否则工程里会留下一个起不来的
        地址/串口参数，而输入框显示的是另一个。两条编辑路（TCP / 串口）共用这一段节奏。
        """
        before = self._errors()
        old = {name: getattr(target, name) for name in changes}
        for name, value in changes.items():
            setattr(target, name, value)
        new = [e for e in self._errors() if e not in before]
        if new:
            for name, value in old.items():
                setattr(target, name, value)
            restore(old)
            return self._refuse(f"{channel}: {new[0]}")
        self.last_error = ""
        # 改地址/串口参数就是改工程：不置 dirty 的话主窗标题不带 `*`、关窗也不问（口径 ε），
        # 现场改完端口直接关窗，下次开的是 CSV 里的旧端口。被拒的改动在上面就返回了，
        # 到不了这里——什么都没换，自然不该留下"有未保存改动"。
        self._session.mark_dirty()
        return result

    def geometry_snapshot(self) -> tuple:
        """每个控件的完整矩形：状态换了文字/颜色也不该有任何一项变化。"""
        rects = tuple((row.name_label.geometry().getRect(),
                       row.host_edit.geometry().getRect(),
                       row.port_edit.geometry().getRect(),
                       row.frame_edit.geometry().getRect(),
                       row.prefix_edit.geometry().getRect(),
                       row.start_button.geometry().getRect(),
                       row.stop_button.geometry().getRect(),
                       row.status.geometry().getRect()) for row in self._rows.values())
        return (self.size().width(), self.size().height(), rects)

    # ---------- 内部 ----------

    def _make_row(self, index, ch) -> ChannelRow:
        # 串口行与 TCP 行共用前两个定宽槽，含义不同（串口名/波特率）；第三格"帧格式"
        # 只对串口行有意义，TCP 行留空并置灰——**置灰不隐藏**，隐藏会让后面的格子集体
        # 左移，那正是"状态一变布局就跳"里最难看的一种。
        transport = channel_transport(ch)
        serial = is_serial(transport)
        s7 = transport == "s7"
        mqtt = transport == "mqtt"
        name = QtWidgets.QLabel(ch.name)
        name.setMinimumWidth(72)
        host = QtWidgets.QLineEdit(ch.port if serial else ch.host)
        host.setFixedWidth(_HOST_WIDTH)
        host.setToolTip("串口名（例：COM3）" if serial else
                        "broker 地址（例：127.0.0.1）" if mqtt else
                        "监听地址（例：0.0.0.0）")
        port = QtWidgets.QLineEdit(str(ch.baudrate) if serial else str(ch.port))
        port.setFixedWidth(_PORT_WIDTH)
        port.setToolTip("波特率（例：9600）" if serial else
                        "broker 端口（例：1883）" if mqtt else
                        "监听端口（例：5020；S7 惯例 102）")
        # 第三格：串口是帧格式、**S7 是 rack/slot**（`0/1`）、TCP 留空并置灰。
        # 三个传输共用同一列，是为了不动 TCP 行已经钉住的几何（口径 4/5）。
        if serial:
            third = format_frame(ch.bytesize, ch.parity, ch.stopbits)
            third_tip = "帧格式：数据位/校验/停止位（例：8E1）"
        elif s7:
            third = f"{ch.rack}/{ch.slot}"
            third_tip = "机架/槽位（例：0/1；客户端连上来时报的那两个数）"
        elif mqtt:
            third = str(ch.qos)
            third_tip = "QoS：0（最多一次）/ 1（至少一次）/ 2（恰好一次）"
        else:
            third = ""
            # 空着的那一格也留一句话：光看一个灰空框，操作员只会怀疑"这里是不是坏了"。
            third_tip = ("这一格只对串口（帧格式）与 S7（机架/槽位）有意义；"
                         "TCP 通道用不上它")
        frame = QtWidgets.QLineEdit(third)
        frame.setFixedWidth(_FRAME_WIDTH)
        frame.setToolTip(third_tip)
        frame.setEnabled(serial or s7 or mqtt)
        prefix = QtWidgets.QLineEdit(ch.topic_prefix if mqtt else "")
        prefix.setFixedWidth(_PREFIX_WIDTH)
        prefix.setToolTip("MQTT topic 前缀（例：simbench；只用这一列对 MQTT 有意义）")
        prefix.setEnabled(mqtt)
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
        frame.editingFinished.connect(lambda n=ch.name: self.apply_edits(n))
        for column, widget in enumerate((name, host, port, frame, prefix, start, stop,
                                         status)):
            self._grid.addWidget(widget, index, column)
        return ChannelRow(ch.name, serial, s7, mqtt, name, host, port, frame, prefix,
                          start, stop, status)

    def _start(self, channel: str) -> None:
        if self._operation_busy:
            return
        self._operation_busy = True
        try:
            def work():
                lines = self._before_start() if self._before_start is not None else []
                try:
                    self._start_now(channel)
                except Exception as exc:            # noqa: BLE001 —— 原样交回主线程
                    return lines or [], exc
                return lines or [], None

            lines, error = run_waiting(self.window(), f"正在启动 {channel}…", work)
            for line in lines:
                self._session.publish_log("info", f"[net] {line}")
            if isinstance(error, KeyError):
                raise error                         # 通道名不在会话里：调用方的 bug
            if error is not None:
                self._fail(channel, error)
                return
            self.last_error = ""
            self.set_status(channel, "running")
        except KeyError:
            raise
        except Exception as exc:                    # noqa: BLE001
            self._fail(channel, exc)
        finally:
            self._operation_busy = False

    def _start_now(self, channel: str) -> None:
        self._session.start_channel(channel)

    def _stop(self, channel: str) -> None:
        if self._operation_busy:
            return
        self._operation_busy = True
        try:
            def work():
                try:
                    self._stop_now(channel)
                except Exception as exc:            # noqa: BLE001
                    return exc
                return None

            error = run_waiting(self.window(), f"正在停止 {channel}…", work)
            if error is not None:
                self._fail(channel, error)
                return
            self.last_error = ""
            self.set_status(channel, "stopped")
        except Exception as exc:                    # noqa: BLE001
            self._fail(channel, exc)
        finally:
            self._operation_busy = False

    def _stop_now(self, channel: str) -> None:
        self._session.stop_channel(channel)

    def _fail(self, channel: str, exc: Exception) -> None:
        # 类型写错（ValueError）、端口被占（PortInUseError）都落在这里：面板上变红字、
        # 留下原因、再记一条日志。槽函数里抛出去等于在 Qt 事件循环里炸。
        self.last_error = f"{channel}: {exc}"
        self._session.publish_log("error", self.last_error)
        self.set_status(channel, "error")

    def _set_enabled(self, channel: str, running: bool) -> None:
        row = self._rows[channel]
        row.start_button.setEnabled(not running)
        row.stop_button.setEnabled(running)
        row.host_edit.setEnabled(not running)        # 跑着改地址 = 改的不是正在跑的那个
        row.port_edit.setEnabled(not running)
        # 第三格：TCP 行恒置灰（这一格对它没有意义），串口/S7 行跟着"在跑"置灰
        row.frame_edit.setEnabled((row.serial or row.s7 or row.mqtt) and not running)
        row.prefix_edit.setEnabled(row.mqtt and not running)

    def _errors(self) -> list[str]:
        """当前拦住启动的那几条。判定住在 `validate_project`，面板一份都不抄。"""
        return [str(issue) for issue in self._session.issues()
                if issue.level == "error"]

    def _refuse(self, message: str) -> None:
        self.last_error = message
        return
