"""主站模式的界面：扫 unit / 逐点核对 / 导出报告（2026-10-14 E1）。

这一页与别的页有一个根本区别：**它只读别人的设备，自己一个端口都不监听**。所以它跟
"全部启动"无关，也不参与点表编辑——它是拿去现场"对点表"的那把尺子。

三条口径：

1. **长活必须在后台线程**：扫 247 个 unit、每个等 1 秒超时就是几分钟；跑在界面线程上
   表现是"整个窗口卡死"，而操作员的第一反应是再点一次。这里用一条普通线程 + Qt 信号
   把结果投回来（与 `EngineBridge` 同一套：工作线程只 emit，不碰 widget）。
2. **一次只干一件事**：跑着的时候三个按钮**置灰不隐藏**（界面口径 4/5），状态那一格
   说清"正在扫到第几个 unit"。
3. **结果表只列结论**：列就是报告里那几列；对得上的也在表里（现场要的是"我全看了一遍"），
   但"导出"给的那份是完整报告。
"""

from __future__ import annotations

import threading

from PySide6 import QtCore, QtGui, QtWidgets

from ..master import report
from ..master.modbus import MasterLink
from ..master.scan import scan_units, verify_points
from ..project.model import channel_transport as _transport_of
from ..project.model import is_s7

# 列的宽度按"最宽内容 + 余量"量出来（真字体 9pt 下量过一遍；与点位表那一套同形）
COLUMNS = (("名称", 150), ("区", 56), ("地址", 56), ("期望", 110), ("读到", 110),
           ("结论", 72), ("说明", 220))
BUTTON_WIDTHS = {"扫描 unit": 96, "逐点核对": 96, "导出报告": 88}
_STATUS_WIDTH = 320


def master_channel(project, name: str | None = None):
    """挑一路当主站目标：点名优先，否则第一条 Modbus 通道（与命令行同一个判定）。"""
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

    def __init__(self, session, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._rows: list = []
        self._busy = False
        self._thread: threading.Thread | None = None

        self.scan_button = self._button("扫描 unit", self.scan)
        self.scan_button.setToolTip("从 1 扫到 247：读一次 hr 0，应答的就是活着的 unit")
        self.verify_button = self._button("逐点核对", self.verify)
        self.verify_button.setToolTip("按点表逐点读回来，与点表初值比——只读，不写任何寄存器")
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
        for index, (_name, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        self.table.setToolTip("这一页只读别人的设备；导出的报告在「导出报告」里")

        bar = QtWidgets.QHBoxLayout()
        for widget in (self.scan_button, self.verify_button, self.export_button):
            bar.addWidget(widget)
        bar.addWidget(self.status_label)
        bar.addStretch(1)

        self.rows_ready.connect(self._show_rows)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(bar)
        layout.addWidget(self.table, 1)

    # ---------- 主窗用的接口 ----------

    def row_count(self) -> int:
        return self.model.rowCount()

    def status_text(self) -> str:
        return self.status_label.text()

    def geometry_snapshot(self) -> dict:
        """给布局恒等断言用：这一页的几何（状态文字变了也不许动）。"""
        return {
            "buttons": tuple(button.geometry().getRect()
                             for button in (self.scan_button, self.verify_button,
                                            self.export_button)),
            "status": self.status_label.geometry().getRect(),
            "table": self.table.geometry().getRect(),
        }

    def set_busy(self, busy: bool) -> None:
        """跑着的时候三个按钮置灰（**不隐藏**）：几何一个像素都不动。"""
        self._busy = busy
        for button in (self.scan_button, self.verify_button):
            button.setEnabled(not busy)
        self.export_button.setEnabled(not busy and bool(self._rows))

    # ---------- 两个动作 ----------

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
        """逐点核对：结果进表，同时对得上的也说一句（现场要知道"我全看了一遍"）。"""
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
                    self._say(report.to_text(rows, limit=3).splitlines()[0])
                    self.notice.emit("[master] " + report.to_text(rows).splitlines()[0])
            except Exception as exc:                       # noqa: BLE001
                self._fail(exc)

        self._run(work)

    def export(self) -> None:
        """把这次核对的结论导出成一份报告（CSV / Excel，后缀说了算）。"""
        if not self._rows:
            return
        suggested = "核对报告.xlsx"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出核对报告", suggested, "Excel (*.xlsx);;CSV (*.csv);;所有文件 (*)")
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
        channel = master_channel(self.session.project)
        if channel is None:
            self._say("这份点表里没有可用的 Modbus 通道（S7 的主站模式还没做）")
            self.notice.emit("[refused] 主站模式需要一条 Modbus 通道")
        return channel

    def _link(self, channel):
        """连过去。`host`/`port` 用通道自己的（界面上没有第二个可以改的地方）。"""
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

    @QtCore.Slot(object)
    def _show_rows(self, rows) -> None:
        self.model.removeRows(0, self.model.rowCount())
        for row in rows:
            items = [QtGui.QStandardItem(text) for text in (
                row.name, row.area, str(row.address), row.expected, row.actual,
                report.STATUS_LABELS.get(row.status, row.status), row.detail)]
            for item in items:
                item.setEditable(False)
            self.model.appendRow(items)


class _OpenedLink:
    """`with _OpenedLink(link): …`：进来自动 open，出来自动 close（连不上抛错给调用方）。"""

    def __init__(self, link) -> None:
        self.link = link

    def __enter__(self):
        self.link.open()
        return self.link

    def __exit__(self, *exc_info) -> None:
        self.link.close()
