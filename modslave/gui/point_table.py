"""点位表模型。列定义集中一处，视图只管摆。

只展示与编辑，不做任何判定：类型/范围/是否在册全部留在 `codec` 与 `Session` 那一份里
（α：area/地址/类型属于点表，改它们要走导入或点位属性面板，所以这里只有"当前值"可编辑）。

刷新口径（用户定的界面口径 3 的延伸）：日常 10Hz 刷新**只**给真变了的行发 `dataChanged`，
条目数没变绝不 `beginResetModel()`——reset 会把选中行、滚动位置和展开状态一起丢掉，
那属于"状态变化动了既有可见状态"。灰显（`stale`）走自定义角色，不改显示文字：
把"没读到"改成显示 0 或空串，界面就等于演了一出"设备报了这个值"。
接管（`source == "manual"`）反过来走文字：`已停` 就占在"波形"那一列原来的位置，
不加宽、不截断、不挪列（spec §7 的界面口径）。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore

from ..engine.point_source import PointSource
from ..engine.session import Row

VALUE_HEADER = "当前值"


@dataclass(frozen=True)
class Column:
    header: str
    width: int
    numeric: bool = False


COLUMNS: tuple[Column, ...] = (
    Column("名称", 150),
    Column("区", 46),
    Column("地址", 56, numeric=True),
    Column("4x", 82, numeric=True),
    Column("类型", 74),
    Column("字序", 60),
    Column(VALUE_HEADER, 110),
    Column("原始寄存器", 150),
    Column("波形", 96),
    Column("只读", 46),
)

VALUE_COLUMN = next(i for i, c in enumerate(COLUMNS) if c.header == VALUE_HEADER)
STALE_ROLE = QtCore.Qt.UserRole + 1

_CONVENTION = {"co": "0x", "di": "1x", "ir": "3x", "hr": "4x"}


def conventional(area: str, address: int) -> str:
    """裸 0-based 地址 -> 现场惯例写法（厂商点表常用 40001 那一套）。

    只由 area+address 现算，不在 `Row` 里另存一份：存了就会有"改了 CSV 忘了改它"这种
    漂移，而这一列恰好是现场拿去和点表对号的那一列。未知区名直接抛，不猜一个前缀。
    """
    prefix = _CONVENTION.get(area)
    if prefix is None:
        raise ValueError(f"unknown area {area!r}")
    return f"{prefix}{address + 1:05d}"


class PointTableModel(QtCore.QAbstractTableModel):
    """展示整表，只有"当前值"可编辑。刷新只动真变了的行。"""

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[Row] = []
        self._source: PointSource | None = None
        self._channel = ""
        self.last_error = ""

    def stale_role(self) -> int:
        """视图用这个角色决定要不要灰显那一格（`Row.stale` 的对外出口）。"""
        return STALE_ROLE

    def channel(self) -> str:
        return self._channel

    def attach(self, source: PointSource, channel: str = "") -> None:
        """换一个数据源。默认展示这个通道**全部**点位（口径 2：不给"跟随当前上下文"）。"""
        self._source = source
        project = getattr(source, "project", None)
        names = [ch.name for ch in getattr(project, "channels", [])]
        if not names:
            # 空工程不是"表格空着"，是没装载：让调用方听见，别让界面演一张合法的空白点表
            raise ValueError("the project has no channel")
        self._channel = channel or names[0]
        self._rows = list(source.rows(self._channel))
        self.beginResetModel()
        self.endResetModel()

    # ---------- Qt 的 model 接口 ----------

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role != QtCore.Qt.DisplayRole:
            return None
        if orientation == QtCore.Qt.Horizontal:
            return COLUMNS[section].header
        return str(section + 1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        spec = COLUMNS[index.column()]
        if role in (QtCore.Qt.DisplayRole, QtCore.Qt.EditRole):
            # EditRole 也要给：只给 DisplayRole 的话，双击进来的编辑器是**空的**，
            # 用户会在空框里敲一个新值——那是界面自己造的误操作。
            return _display(row, spec.header)
        if role == QtCore.Qt.TextAlignmentRole and spec.numeric:
            return int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        if role == STALE_ROLE:
            return bool(row.stale)
        return None

    def flags(self, index):
        """默认 flags 自带 `ItemIsEditable`，必须显式清掉再按列加回去。

        留着它，"地址"和"类型"就能被双击改掉——那是点表（CSV/工程）的内容，
        改了会和 `overlap`/`unit_duplicated` 校验打架（α）。列仍然保持 enabled+selectable，
        否则整列变灰连选中都做不到，看起来像模拟器坏了。
        """
        if not index.isValid():
            return QtCore.Qt.NoItemFlags
        base = (super().flags(index) & ~QtCore.Qt.ItemIsEditable) \
            | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
        if index.column() == VALUE_COLUMN:
            base |= QtCore.Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=QtCore.Qt.EditRole) -> bool:
        if not index.isValid() or role != QtCore.Qt.EditRole \
                or index.column() != VALUE_COLUMN or self._source is None:
            return False
        row = self._rows[index.row()]
        text = str(value).strip()
        if text == row.value_text:
            return False                      # 没变就别惊动视图
        ok, error = self._source.write(row.channel, row.unit_id, row.area,
                                       row.address, text)
        # 点位名在这里加，只加一次：数据源（真 Session 与替身）给的文案是同一份句式，
        # 界面再套一层"是哪一行"，现场靠这个名字认屏幕上哪一格被拒了。
        self.last_error = "" if ok else f"{row.name}: {error}"
        if not ok:
            return False
        self.refresh()
        return True

    # ---------- 主窗心跳 ----------

    def refresh(self) -> int:
        """重读数据源，只给真变了的行发 dataChanged。返回改动行数。"""
        if self._source is None:
            return 0
        fresh = list(self._source.rows(self._channel))
        if len(fresh) != len(self._rows):
            # 只有条目数真变了才 reset：行数没变而内容变了，靠 dataChanged 就够
            self.beginResetModel()
            self._rows = fresh
            self.endResetModel()
            return len(fresh)
        changed = 0
        for i, new in enumerate(fresh):
            if new == self._rows[i]:          # Row 是 frozen dataclass：逐字段比，
                continue                      # 不手工挑字段就不怕以后加列时漏掉一个
            self._rows[i] = new
            self.dataChanged.emit(self.index(i, 0),
                                  self.index(i, self.columnCount() - 1))
            changed += 1
        return changed


def _display(row: Row, header: str) -> str:
    return {
        "名称": row.name, "区": row.area, "地址": str(row.address),
        "4x": conventional(row.area, row.address), "类型": row.datatype,
        "字序": row.word_order, VALUE_HEADER: row.value_text,
        "原始寄存器": " ".join(f"{r:04X}" for r in row.registers),
        # spec §7：被人工接管的点，波形列显示"已停"而不是波形名——"曲线还在跑"和
        # "这格的值是人/主站写进去的"在排查下发问题时是两个完全不同的答案。
        # 文案占原来那一列，列宽与列序一个都不动（界面口径：装不下改文案，不加宽不截断）。
        "波形": "已停" if row.source == "manual" else row.waveform,
        "只读": "是" if row.readonly else "",
    }[header]
