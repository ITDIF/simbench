"""主站模式的界面：扫 unit / 逐点核对 / 持续监视 / 导出报告（2026-10-14 E1）。

这一页与别的页有一个根本区别：**它只读别人的设备，自己一个端口都不监听**。所以它跟
"全部启动"无关，也不参与点表编辑——它是拿去现场"对点表"的那把尺子。

2026-10-20 起多了**持续监视**：按间隔反复核对同一路设备，异常点位标色、新增/恢复写日志，
标签页上挂一个 `●N` 角标。现场要的不只是"我点过一次"，而是"这台设备现在有没有病"。

四条口径：

1. **长活必须在后台线程**：扫 247 个 unit、每个等 1 秒超时就是几分钟；跑在界面线程上
   表现是"整个窗口卡死"，而操作员的第一反应是再点一次。这里用一条普通线程 + Qt 信号
   把结果投回来（与 `EngineBridge` 同一套：工作线程只 emit，不碰 widget）。
2. **一次只干一件事**：扫描/核对/监视互斥；跑着的时候按钮**置灰不隐藏**（界面口径 4/5）。
3. **结果表只列结论**：列就是报告里那几列；对得上的也在表里（现场要的是"我全看了一遍"）。
   异常行按状态标色：对不上=琥珀、没应答/读出错=红；颜色走 `theme.status_colors()`。
4. **告警不刷屏**：每一轮只报"新增的异常"和"恢复的异常"，同一项一直坏不会每 5 秒
   重复一行；告警同时写日志页和状态栏，标签页角标显示当前异常总数。
"""

from __future__ import annotations

import threading

from PySide6 import QtCore, QtGui, QtWidgets

from ..master import report
from ..master.modbus import MasterLink
from ..master.mqtt import MqttMasterLink
from ..master.scan import scan_units, verify_points
from ..project.model import channel_transport as _transport_of
from ..project.model import is_mqtt, is_s7
from . import theme
from .dialogs import FILE_DIALOG_OPTION

# 列的宽度按"最宽内容 + 余量"量出来（真字体 9pt 下量过一遍；与点位表那一套同形）
COLUMNS = (("名称", 150), ("区", 56), ("地址", 56), ("期望", 110), ("读到", 110),
           ("结论", 72), ("说明", 220))
BUTTON_WIDTHS = {"扫描 unit": 96, "逐点核对": 96, "开始监视": 96, "停止监视": 96,
                 "导出报告": 88}
_STATUS_WIDTH = 320
_MONITOR_SECONDS = 5
# 设备明确离线时 `MqttMasterLink.read` 的 detail；告警按通道合并，不按点刷屏。
_OFFLINE_MARK = "设备在线状态为离线"


def master_channel(project, name: str | None = None):
    """挑一路当主站目标：点名优先，否则第一条可读通道（Modbus 或 MQTT）。"""
    for channel in project.channels:
        if name and channel.name != name:
            continue
        if not is_s7(_transport_of(channel)):
            return channel
    return None


class VerifyPanel(QtWidgets.QWidget):
    """核对页。`notice` 让主窗把它的话写进日志页。"""

    notice = QtCore.Signal(str)
    finished = QtCore.Signal()
    # 结果表**只许在主线程里填**（widget 的规矩）：工作线程算完 emit，队列把它投回主线程
    rows_ready = QtCore.Signal(object)
    # 当前异常总数（0 = 全部正常）：主窗拿它给"核对"标签挂角标
    alerts_changed = QtCore.Signal(int)
    _monitor_stopped = QtCore.Signal(int)

    def __init__(self, session, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._rows: list = []
        self._busy = False
        self._monitoring = False
        self._stop_event: threading.Event | None = None
        self._monitor_gen = 0
        self._interval_seconds = _MONITOR_SECONDS
        # 上一轮的异常：点位身份 -> 状态。只报"新增"与"恢复"，同一项不重复刷屏。
        self._last_bad: dict[tuple, str] = {}
        self._thread: threading.Thread | None = None

        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.setFixedWidth(170)
        self.target_combo.setToolTip("要核对/监视的 Modbus 通道：用它自己的地址/端口连过去")
        self.scan_button = self._button("扫描 unit", self.scan)
        self.scan_button.setToolTip("从 1 扫到 247：读一次 hr 0，应答的就是活着的 unit")
        self.verify_button = self._button("逐点核对", self.verify)
        self.verify_button.setToolTip("按点表逐点读回来，与点表初值比——只读，不写任何寄存器")
        self.monitor_button = self._button("开始监视", self.toggle_monitor)
        self.monitor_button.setToolTip("按间隔反复核对；异常写日志并在标签页上挂角标")
        # Ctrl+Enter 只在核对页有焦点时生效：挂成全局键会把日志页/输入框里的回车抢走。
        self.monitor_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence("Ctrl+Return"), self,
            activated=lambda: self.toggle_monitor())
        self.monitor_shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.interval = QtWidgets.QSpinBox()
        self.interval.setRange(1, 3600)
        self.interval.setValue(_MONITOR_SECONDS)
        self.interval.setSuffix(" 秒")
        self.interval.setFixedWidth(78)
        self.interval.setToolTip("持续监视的间隔（1~3600 秒）")
        self.export_button = self._button("导出报告", self.export)
        self.export_button.setEnabled(False)
        self.export_button.setToolTip("把这次核对的结论导成 CSV / Excel")

        self.status_label = QtWidgets.QLabel("还没开始")
        self.status_label.setFixedWidth(_STATUS_WIDTH)

        self.model = QtGui.QStandardItemModel(0, len(COLUMNS), self)
        self.model.setHorizontalHeaderLabels([name for name, _ in COLUMNS])
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        self.table.horizontalHeader().setDefaultAlignment(
            QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        for index, (_name, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        self.table.setToolTip("这一页只读别人的设备；异常行会标色，报告在「导出报告」里")

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.target_combo)
        for widget in (self.scan_button, self.verify_button, self.monitor_button,
                       self.interval, self.export_button):
            bar.addWidget(widget)
        bar.addStretch(1)

        self.rows_ready.connect(self._show_rows)
        self._monitor_stopped.connect(self._on_monitor_stopped)
        self.target_combo.currentIndexChanged.connect(self._target_changed)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(bar)
        # 状态单独一行：和按钮挤在一行时，核对页的最小宽度会超过 1000px，
        # 小窗口就恢复不到原来的尺寸（700px 的用例直接量得到）。
        layout.addWidget(self.status_label)
        layout.addWidget(self.table, 1)
        self.refresh_targets()

    # ---------- 主窗用的接口 ----------

    def row_count(self) -> int:
        return self.model.rowCount()

    def status_text(self) -> str:
        return self.status_label.text()

    def is_monitoring(self) -> bool:
        return self._monitoring

    def alert_count(self) -> int:
        return len(self._last_bad)

    def wait_monitor(self, timeout: float = 2.0) -> None:
        """等后台监视线程退出（用例/关窗收尾用；界面按钮不调它，避免卡住）。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def geometry_snapshot(self) -> dict:
        """给布局恒等断言用：这一页的几何（状态文字变了也不许动）。"""
        return {
            "target": self.target_combo.geometry().getRect(),
            "buttons": tuple(button.geometry().getRect()
                             for button in (self.scan_button, self.verify_button,
                                            self.monitor_button, self.export_button)),
            "interval": self.interval.geometry().getRect(),
            "status": self.status_label.geometry().getRect(),
            "table": self.table.geometry().getRect(),
        }

    def set_busy(self, busy: bool) -> None:
        """跑着的时候按钮置灰（**不隐藏**）：几何一个像素都不动。"""
        self._busy = busy
        self._sync_buttons()

    def rebind(self, session) -> None:
        """切标签：停监视、换会话、清结果与通道列表。监视不能跨工程继续——
        它的结论会写进另一份工程的界面，比"停下来"更糟。"""
        self.stop_monitor()
        self.session = session
        self.reset()

    def refresh_targets(self, project=None) -> None:
        """把下拉框刷成当前工程里的 Modbus 通道；S7 主站还没做，不列。"""
        if project is None:
            project = getattr(self.session, "project", None)
        names = [ch.name for ch in getattr(project, "channels", [])
                 if not is_s7(_transport_of(ch))]
        keep = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        try:
            self.target_combo.clear()
            if names:
                self.target_combo.addItems(names)
                if keep in names:
                    self.target_combo.setCurrentIndex(names.index(keep))
            else:
                self.target_combo.addItem("（没有 Modbus 通道）")
        finally:
            self.target_combo.blockSignals(False)

    def reset(self) -> None:
        """换工程/改结构之后调用：停监视、清结果、刷新通道列表。"""
        self.stop_monitor()
        self._rows = []
        self._last_bad = {}
        self.model.removeRows(0, self.model.rowCount())
        self.status_label.setText("还没开始")
        self.refresh_targets()
        self._sync_buttons()
        self.alerts_changed.emit(0)

    # ---------- 动作 ----------

    def scan(self) -> None:
        """扫 unit：扫完把应答的那几个写进状态那一格（结果表不动——那是核对的活）。"""
        channel = self._target()
        if channel is None:
            return

        def work() -> None:
            try:
                with self._link(channel) as link:
                    found = scan_units(
                        link, progress=lambda done, total: self._progress(
                            f"扫描中 {done}/{total}"))
                    self._say(f"应答的 unit（{len(found)} 个）："
                              + ("、".join(str(unit) for unit in found) if found else "无"))
                    self.notice.emit(f"[master] {channel.name} 扫到 {len(found)} 个 unit 应答")
            except Exception as exc:                       # noqa: BLE001
                self._fail(exc)

        self._run(work)

    def verify(self) -> None:
        """逐点核对：结果进表；异常会标色并写日志（只报新增/恢复）。"""
        channel = self._target()
        if channel is None:
            return
        project = self.session.project

        def work() -> None:
            try:
                with self._link(channel) as link:
                    rows = verify_points(
                        link, project,
                        progress=lambda row, done, total: self._progress(
                            f"核对中 {done}/{total}"))
                    self._rows = rows
                    self.rows_ready.emit(rows)         # 填表这一步回主线程做
                    self.notice.emit("[master] " + report.to_text(rows).splitlines()[0])
            except Exception as exc:                       # noqa: BLE001
                self._fail(exc)

        self._run(work)

    def toggle_monitor(self) -> None:
        if self._monitoring:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self) -> None:
        """开始持续监视：后台线程反复核对，直到点「停止监视」或换工程。"""
        if self._busy or self._monitoring:
            return
        channel = self._target()
        if channel is None:
            return
        self._interval_seconds = max(1, int(self.interval.value()))
        self._monitor_gen += 1
        gen = self._monitor_gen
        self._stop_event = threading.Event()
        stop_event = self._stop_event
        self._monitoring = True
        self._last_bad = {}
        self._sync_buttons()
        self._say(f"正在连接 {channel.name}…")
        self.notice.emit(f"[master] 开始持续监视 {channel.name}"
                         f"（每 {self._interval_seconds} 秒）")
        self._thread = threading.Thread(
            target=self._monitor_loop, args=(channel, stop_event, gen),
            name="simbench-monitor", daemon=True)
        self._thread.start()

    def stop_monitor(self) -> None:
        """请求停止：不 join（当前那一轮读完就退），界面立刻恢复可操作。"""
        if not self._monitoring:
            return
        self._monitor_gen += 1                 # 让旧线程的收尾信号作废
        if self._stop_event is not None:
            self._stop_event.set()
        self._monitoring = False
        self._sync_buttons()
        self._say("监视已停止")
        self.notice.emit("[master] 已停止持续监视")

    def export(self) -> None:
        """把这次核对的结论导出成一份报告（CSV / Excel，后缀说了算）。"""
        if not self._rows:
            return
        suggested = "核对报告.xlsx"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出核对报告", suggested, "Excel (*.xlsx);;CSV (*.csv);;所有文件 (*)",
            options=FILE_DIALOG_OPTION)
        if not path:
            return
        try:
            target = report.write(path, self._rows)
        except (OSError, ValueError) as exc:
            self._say(f"导出失败：{exc}")
            self.notice.emit(f"[error] 导出核对报告失败：{exc}")
            return
        self._say(f"报告已写到 {target}")
        self.notice.emit(f"[info] 核对报告已导出 -> {target}")

    # ---------- 内部 ----------

    def _button(self, text: str, slot) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(text)
        button.setFixedWidth(BUTTON_WIDTHS[text])
        button.clicked.connect(lambda _checked=False: slot())
        return button

    def _target(self):
        name = self.target_combo.currentData()
        channel = master_channel(self.session.project, name)
        if channel is None:
            self._say("这份点表里没有可用的 Modbus 通道（S7 的主站模式还没做）")
            self.notice.emit("[refused] 主站模式需要一条 Modbus 通道")
        return channel

    def _link(self, channel):
        """连过去。`host`/`port` 用通道自己的（界面上没有第二个可以改的地方）。"""
        if is_mqtt(_transport_of(channel)):
            return _OpenedLink(MqttMasterLink.for_channel(channel, timeout=1.0))
        return _OpenedLink(MasterLink.for_channel(channel, timeout=1.0))

    def _run(self, work) -> None:
        if self._busy:
            return                       # 一次只干一件事（按钮已置灰，这里是第二道闸）
        self.set_busy(True)
        self._say("正在连接…")

        def runner() -> None:
            try:
                work()
            finally:
                self.set_busy(False)
                self.finished.emit()

        self._thread = threading.Thread(target=runner, name="simbench-verify", daemon=True)
        self._thread.start()

    def _monitor_loop(self, channel, stop_event: threading.Event, gen: int) -> None:
        try:
            with self._link(channel) as link:
                while not stop_event.is_set():
                    rows = verify_points(
                        link, self.session.project,
                        progress=lambda row, done, total: self._progress(
                            f"监视中 {done}/{total}"))
                    if stop_event.is_set():
                        break
                    self._rows = rows
                    self.rows_ready.emit(rows)
                    stop_event.wait(max(1, self._interval_seconds))
        except Exception as exc:                       # noqa: BLE001
            self._fail(exc)
        finally:
            self._monitor_stopped.emit(gen)

    def _progress(self, text: str) -> None:
        # 从工作线程调：只改**文字**（不是几何），Qt 会把信号/直接调用排到主线程
        QtCore.QMetaObject.invokeMethod(self.status_label, "setText",
                                        QtCore.Qt.QueuedConnection,
                                        QtCore.Q_ARG(str, text))

    def _say(self, text: str) -> None:
        QtCore.QMetaObject.invokeMethod(self.status_label, "setText",
                                        QtCore.Qt.QueuedConnection,
                                        QtCore.Q_ARG(str, text))

    def _fail(self, exc: Exception) -> None:
        self._say(f"失败：{exc}")
        self.notice.emit(f"[error] 核对失败：{type(exc).__name__}: {exc}")

    def _sync_buttons(self) -> None:
        running = self._busy or self._monitoring
        self.scan_button.setEnabled(not running)
        self.verify_button.setEnabled(not running)
        self.monitor_button.setEnabled(not self._busy)
        self.monitor_button.setText("停止监视" if self._monitoring else "开始监视")
        self.export_button.setEnabled(not running and bool(self._rows))
        self.target_combo.setEnabled(not running)
        self.interval.setEnabled(not running)

    @QtCore.Slot(int)
    def _on_monitor_stopped(self, gen: int) -> None:
        if gen != self._monitor_gen:
            return                     # 旧线程收尾：新监视已经开始了，别动它的状态
        self._monitoring = False
        self._sync_buttons()

    @QtCore.Slot(int)
    def _target_changed(self, _index: int) -> None:
        """换目标通道：旧的结论/告警都不再适用，清掉。"""
        if self._monitoring:
            self.stop_monitor()
        self._rows = []
        self._last_bad = {}
        self.model.removeRows(0, self.model.rowCount())
        self.status_label.setText("还没开始")
        self._sync_buttons()
        self.alerts_changed.emit(0)

    @QtCore.Slot(object)
    def _show_rows(self, rows) -> None:
        rows = list(rows)
        self._rows = rows
        self.model.removeRows(0, self.model.rowCount())
        colors = theme.status_colors()
        for row in rows:
            offline = _OFFLINE_MARK in row.detail
            label = ("设备离线" if offline
                     else report.STATUS_LABELS.get(row.status, row.status))
            items = [QtGui.QStandardItem(text) for text in (
                row.name, row.area, str(row.address), row.expected, row.actual,
                label, row.detail)]
            colour = (colors.get("starting") if row.status == "mismatch"
                      else colors.get("error") if row.status in ("no_unit", "error")
                      else None)
            for item in items:
                item.setEditable(False)
                item.setTextAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
                if colour is not None:
                    item.setForeground(QtGui.QBrush(colour))
            self.model.appendRow(items)

        bad_rows = {self._key(row): row for row in rows if row.status != "ok"}
        bad_status = {key: row.status for key, row in bad_rows.items()}
        new = [row for key, row in bad_rows.items()
               if self._last_bad.get(key) != row.status]
        recovered = [key for key in self._last_bad if key not in bad_status]
        self._last_bad = bad_status
        self._announce(new, recovered)

        if self._monitoring:
            self._say("监视中：正常" if not bad_status
                      else f"监视中：异常 {len(bad_status)}")
        else:
            self._say(report.to_text(rows, limit=3).splitlines()[0])
        self._sync_buttons()
        self.alerts_changed.emit(len(bad_status))

    @staticmethod
    def _key(row) -> tuple:
        return (row.channel, row.unit_id, row.area, row.address, row.name)

    def _announce(self, new, recovered) -> None:
        """只报新增/恢复；设备离线按通道合并成一条，不按点刷屏。"""
        offline = [row for row in new if _OFFLINE_MARK in row.detail]
        rest = [row for row in new if _OFFLINE_MARK not in row.detail]
        by_channel: dict[str, list] = {}
        for row in offline:
            by_channel.setdefault(row.channel, []).append(row)
        for channel, rows in sorted(by_channel.items()):
            self.notice.emit(
                f"[alert] {channel}: MQTT 设备离线"
                f"（{len(rows)} 个点暂时无法核对）")
        for row in rest[:5]:
            detail = f"；{row.detail}" if row.detail else ""
            self.notice.emit(
                f"[alert] {row.channel}/{row.device}/{row.name}："
                f"{report.STATUS_LABELS.get(row.status, row.status)}"
                f"（期望 {row.expected}，读到 {row.actual}）{detail}")
        if len(rest) > 5:
            self.notice.emit(f"[alert] 另有 {len(rest) - 5} 项新增异常")
        if recovered:
            self.notice.emit(f"[info] 已恢复 {len(recovered)} 项异常")


class _OpenedLink:
    """`with _OpenedLink(link): …`：进来自动 open，出来自动 close（连不上抛错给调用方）。"""

    def __init__(self, link) -> None:
        self.link = link

    def __enter__(self):
        self.link.open()
        return self.link

    def __exit__(self, *exc_info) -> None:
        self.link.close()
