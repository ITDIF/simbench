"""报文面板：四个平铺过滤标签 + 暂停/清除 + 一列表格。

重绘由主窗的 10Hz 心跳统一驱动（`refresh_view()`），store 的通知只打脏标记——每来一帧
就重排一次表格，在总线上扫 247 台设备时会把界面刷成卡顿。所以 `row_count()`/
`visible_rows()` 读的是 store 的**当下**视图（不依赖重绘有没有发生过），而
`repaint_count` 只数真正的重绘：界面状态和重绘次数是两件事，别混在一个数里。

几何恒定（用户口径 3/4）靠这几件事，不靠 `setVisible`：

* 过滤是 4 个 `QPushButton(checkable)` + 互斥 `QButtonGroup`，**没有下拉框**（口径 1），
  默认勾选"全部"（口径 2）；
* 每个标签、暂停/清除、计数标签都是定宽槽位，切状态只改文字，槽位宽度不变；
* 表格吃满剩余高度（`addWidget(table, 1)`），不给它写死 `max-height`（口径 5）；
* 表格的竖直滚动条常驻，过滤器把行数从 31 收到 1 时也不收起、不重排列宽。

`_mark_dirty` 里绝不碰 widget：store 的 `add()` 是引擎 pump 线程调的，回调必须便宜且不
阻塞采集路径（在回调里重排表格 = 把丢报文的风险换成界面卡顿）。
"""

from __future__ import annotations

import bisect

from PySide6 import QtCore, QtWidgets

from ..engine.packets import ALL, FILTERS, PacketStore

COLUMNS = ("时间", "方向", "通道", "unit", "fc", "摘要", "原始字节")

# 定宽槽位：文案在两个状态之间换（暂停↔继续、0 帧↔5000 帧）宽度都不许变。
# 数值是实测出来的（offscreen、默认字体 9pt）：按钮文字 horizontalAdvance=24、
# 样式留的左右边距各 6（36 就够），标签最长文案 "◐ 启动中"/"5000 帧" 是 60px，
# 都留出余量，改文案而不是加宽。
_FILTER_WIDTH = 56
_ACTION_WIDTH = 64
_COUNT_WIDTH = 96
_QWIDGETSIZE_MAX = 16777215


class PacketModel(QtCore.QAbstractTableModel):
    """只读的一列表格模型。**按顺序号做差集**：只通知真变了的那一段。

    原来两种形状都贵（2026-10-14 实测）：行数变了就 `beginResetModel`（选中行与键盘位置
    每 100ms 被清一次），行数不变而环形缓冲整体前移就逐行 `dataChanged`（缓冲满时 5000 行/
    跳，实测 65.6ms 一跳，占掉主线程三分之二的预算）。两条都源于旧写法"只能按位置比内容"。

    现在按 `PacketEvent.seq` 算差集——顺序号由 `PacketStore` 严格递增地发，而缓冲只在一头
    淘汰、另一头追加，所以"我这边少了哪一段、多了哪一段"用 `bisect` 就能算出来，分别发
    `rowsRemoved` / `rowsInserted`。选中行跟着**序号**走：内容前移不会再把选中项带跑
    （旧写法下那件事表现成"选中第 3000 行看的是另一帧"）。
    """

    def __init__(self, store: PacketStore) -> None:
        super().__init__()
        self._store = store
        self._rows: list = []
        self._seqs: list[int] = []
        self._view_filter = store.active_filter
        # 差集对不上的次数（有人绕过 store 直接改缓冲之类）：退回整表 reset，并留个计数，
        # 免得"悄悄变成每次都全表重画"没人发现。
        self.mismatched_syncs = 0

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role != QtCore.Qt.DisplayRole:
            return None
        if orientation == QtCore.Qt.Horizontal:
            return COLUMNS[section]
        return str(section + 1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        if role == QtCore.Qt.TextAlignmentRole:
            # 与点位表同一条口径：表头与数据都居中（2026-10-19 用户现场口径）
            return int(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        if role != QtCore.Qt.DisplayRole:
            return None
        event = self._rows[index.row()]
        return (f"{event.mono:.3f}", event.direction.upper(), event.channel,
                "-" if event.unit_id is None else str(event.unit_id),
                "-" if event.function_code is None else f"0x{event.function_code:02X}",
                event.parsed, event.raw.hex(" "))[index.column()]

    def flags(self, index):
        """整表只读：这一层没有任何可编辑的字段（报文是已经发生过的事）。"""
        base = super().flags(index) & ~QtCore.Qt.ItemIsEditable
        if not index.isValid():
            return QtCore.Qt.NoItemFlags
        return base | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable

    def sync(self) -> int:
        """把 store 的当前视图搬进模型。返回这一跳动了多少行。

        差集算不出来的两种情况直接整表 reset：**过滤器变了**（视图不再是同一份缓冲的子集）
        与**缓冲被清过**（我这边整批都是旧的）。其余情况都只发增量通知。
        """
        fresh = self._store.rows()
        if self._view_filter != self._store.active_filter:
            self._view_filter = self._store.active_filter
            return self._reset(fresh)
        if not fresh or not self._rows or self._seqs[-1] < fresh[0].seq:
            # 空视图 / 还没有过内容 / 缓冲被清过：整批换掉
            return self._reset(fresh)
        fresh_seqs = [event.seq for event in fresh]
        dropped = bisect.bisect_left(self._seqs, fresh_seqs[0])
        added = len(fresh_seqs) - bisect.bisect_right(fresh_seqs, self._seqs[-1])
        if dropped + (len(fresh_seqs) - added) != len(self._rows):
            # 对不上就不许硬套（那会把整表错位一行）：退回 reset，并记一笔
            self.mismatched_syncs += 1
            return self._reset(fresh)
        if dropped:
            self.beginRemoveRows(QtCore.QModelIndex(), 0, dropped - 1)
            del self._rows[:dropped]
            del self._seqs[:dropped]
            self.endRemoveRows()
        if added:
            first = len(self._rows)
            self.beginInsertRows(QtCore.QModelIndex(), first, first + added - 1)
            self._rows.extend(fresh[len(fresh) - added:])
            self._seqs.extend(fresh_seqs[len(fresh_seqs) - added:])
            self.endInsertRows()
        return dropped + added

    def _reset(self, fresh) -> int:
        """整表换掉（条目集合本身变了，按位置的差集没有意义）。"""
        self.beginResetModel()
        self._rows = list(fresh)
        self._seqs = [event.seq for event in fresh]
        self.endResetModel()
        return len(self._rows)


class PacketPanel(QtWidgets.QWidget):
    """报文面板。几何恒定：所有状态都只改文字/颜色，不增删控件。"""

    def __init__(self, store: PacketStore, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self.repaint_count = 0
        self._dirty = False
        self._follow_tail = True
        self._subscribed = False

        self.model = PacketModel(store)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)   # 一屏几十帧，隔行底色是"哪一帧到哪一帧"的分界
        # 竖直滚动条常驻：默认的 AsNeeded 在切到"异常"只剩一帧时会自己收掉——屏幕上一个
        # 本来在看的元素消失，同时视口变宽、列宽重排一次（口径 3：状态变化不许挪布局，
        # 也不许既有可见元素消失）。由 `test_the_widgets_do_not_disappear_across_states`
        # 连 Qt 自己的子控件一起钉住。
        self.table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(
            QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        self.table.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self.filter_combo = None                      # 明确不用下拉框（口径 1）
        bar = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        self._buttons: list[QtWidgets.QPushButton] = []
        group = QtWidgets.QButtonGroup(bar)
        group.setExclusive(True)
        self.filter_group = group
        for name in FILTERS:
            button = QtWidgets.QPushButton(name)
            button.setCheckable(True)
            button.setChecked(name == ALL)            # 默认"全部"（口径 2）
            button.setFixedWidth(_FILTER_WIDTH)       # 定宽槽：换状态不改宽度
            group.addButton(button)
            self._buttons.append(button)
            row.addWidget(button)
        group.buttonClicked.connect(self._on_filter_clicked)

        row.addStretch(1)
        self.pause_button = QtWidgets.QPushButton("暂停")
        self.pause_button.setCheckable(True)
        self.pause_button.setFixedWidth(_ACTION_WIDTH)
        self.pause_button.toggled.connect(self._on_pause_toggled)
        self.clear_button = QtWidgets.QPushButton("清除")
        self.clear_button.setFixedWidth(_ACTION_WIDTH)
        self.clear_button.clicked.connect(self._on_clear)
        row.addWidget(self.pause_button)
        row.addWidget(self.clear_button)

        self.count_label = QtWidgets.QLabel("0 帧")
        self.count_label.setFixedWidth(_COUNT_WIDTH)
        self.count_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        row.addWidget(self.count_label)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(bar)
        layout.addWidget(self.table, 1)               # 弹性：不给表格写死高度

        store.subscribe(self._mark_dirty)
        self._subscribed = True

    # ---------- 对主窗暴露的接口 ----------

    def filter_labels(self) -> list[QtWidgets.QPushButton]:
        return list(self._buttons)

    def row_count(self) -> int:
        """当前过滤器下的帧数：读 store 的当下视图，不看重绘有没有发生过。"""
        return len(self._store.rows())

    def visible_rows(self) -> list:
        return self._store.rows()

    def set_filter(self, name: str) -> None:
        """程序化切过滤器（主窗快捷键/菜单用），标签勾选状态跟着走。"""
        self._store.set_filter(name)
        self._check_label(name)
        self._mark_dirty()

    def refresh_view(self) -> int:
        """主窗 10Hz 心跳里调。脏了才同步模型并重算计数。"""
        if not self._dirty:
            return 0
        self._dirty = False
        rows = self.model.sync()
        self.repaint_count += 1
        self.count_label.setText(f"{self.model.rowCount()} 帧")
        if self._follow_tail:
            # 不读 verticalScrollBar().maximum()：模型刚 reset 时那个范围还是上一轮的
            # （实测 40 行时它仍是 0），setValue(0) 会把界面甩回最上面。scrollToBottom
            # 走视图自己的布局，重算完几何再落位。
            self.table.scrollToBottom()
        return rows

    def geometry_snapshot(self) -> dict:
        """给布局恒等断言用：面板、四个标签、两个动作按钮、计数标签、表格的完整几何。

        这里**只有几何**：行数会随过滤器正当变化（切"异常"就该少），把它混进来等于
        往恒等断言里塞一个注定不等的量，下一次改布局就没人看得懂这条失败。
        """
        return {
            "panel": (self.size().width(), self.size().height()),
            "filters": tuple(b.geometry().getRect() for b in self._buttons),
            "pause": self.pause_button.geometry().getRect(),
            "clear": self.clear_button.geometry().getRect(),
            "count": self.count_label.geometry().getRect(),
            "table": self.table.geometry().getRect(),
        }

    def closeEvent(self, event) -> None:
        if self._subscribed:
            self._store.unsubscribe(self._mark_dirty)
            self._subscribed = False
        super().closeEvent(event)

    # ---------- 内部 ----------

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _check_label(self, name: str) -> None:
        for button in self._buttons:
            if button.text() == name:
                button.setChecked(True)               # 互斥组里 setChecked 会松开上一个
                return

    def _on_filter_clicked(self, button) -> None:
        self._store.set_filter(button.text())

    def _on_pause_toggled(self, checked: bool) -> None:
        self._store.paused = checked
        self.pause_button.setText("继续" if checked else "暂停")

    def _on_clear(self) -> None:
        self._store.clear()

    def _on_scroll(self, value: int) -> None:
        bar = self.table.verticalScrollBar()
        self._follow_tail = value >= bar.maximum() - 1
