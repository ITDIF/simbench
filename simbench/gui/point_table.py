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

from PySide6 import QtCore, QtGui, QtWidgets

from ..engine.point_source import PointSource
from ..project.model import S7_TYPES
from ..engine.session import Row

VALUE_HEADER = "当前值"
RAW_HEADER = "原始寄存器"


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
    # 132 而不是 110：示例点表里那个 17 位的 uint64（累计电量）在 110px 里会被 Qt 截成
    # `7262385979038…`——这一列是操作员拿去和点表核对的那个数，少一位就没法核。
    # 132 是**量出来的**（本机 Microsoft YaHei UI 9pt）：`72623859790382856` 的 advance 是
    # 120，加单元格左右各 4px 的 margin 是 128，132 留 4px 余量——再瘦就会截。
    # 见 `tests/test_gui_theme.py` 那条（它钉的是"最宽内容是 17 位 + 列宽 132"这个前提，
    # 不拿 `fontMetrics()` 现量：offscreen 的兜底字体把每个字形按 1em 算，量出来 221px）。
    Column(VALUE_HEADER, 132),
    # 原始寄存器：最宽的内容是**4 个寄存器**的 uint64（`0000 0000 4148 4E28`，19 个字符）
    # 或 int64/float64，advance 123 + 8 = 131；原来的 150 是没人量过的，虚 19px。
    # S7 那一列打的是字节（`41 48 00 00`，67px），比这更窄。
    Column(RAW_HEADER, 134),
    Column("波形", 96),
    Column("只读", 46),
)

VALUE_COLUMN = next(i for i, c in enumerate(COLUMNS) if c.header == VALUE_HEADER)
ADDRESS_COLUMN = next(i for i, c in enumerate(COLUMNS) if c.header == "地址")
RAW_COLUMN = next(i for i, c in enumerate(COLUMNS) if c.header == RAW_HEADER)
STALE_ROLE = QtCore.Qt.UserRole + 1

_CONVENTION = {"co": "0x", "di": "1x", "ir": "3x", "hr": "4x"}
# 惯例地址算不出时那一格给什么。只有一个字面量：点位表那一格与属性面板那个标签是同一个
# 意思（地址不是数、或区名根本不在表里），各留一份就会漂成两种标记（面板本来就 import
# 本模块，从这里取是 2 行，不必为它再加第三个文件）。
NO_CONVENTION = "—"


def conventional(area: str, address: int) -> str:
    """裸 0-based 地址 -> 现场惯例写法（厂商点表常用 40001 那一套）。

    只由 area+address 现算，不在 `Row` 里另存一份：存了就会有"改了 CSV 忘了改它"这种
    漂移，而这一列恰好是现场拿去和点表对号的那一列。未知区名直接抛，不猜一个前缀——
    两个调用方（本模块 `_display`、属性面板 `_update_convention`）各自把这一抛换成
    `NO_CONVENTION`，靠的是"抛"和"给标记"分得开：标记只在显示层出现，判定只在
    `validate_project`。
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

    def row_at(self, row: int) -> Row | None:
        """按行号取正在展示的 `Row`：视图的右键菜单要名字/归属/身份，但不自己抄一份行。

        越界回 `None` 而不是抛——右键落在表头下方空白处是操作员的日常动作，菜单可以不弹，
        槽函数不许炸。
        """
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def detach(self) -> None:
        """没有通道可展示：把表清空，**不许**留着上一份工程的行。

        空工程不是"表空着"，是"没得看"（新建空白工程、把最后一条通道删掉都会走到这里）。
        留着上一份的行，操作员会以为那份点表还在——本项目最防的就是"界面演出现场没发生的
        事"。清空走 `beginResetModel`：条目变了本来就该 reset（刷新口径只管"条目没变"）。
        """
        self._source = None
        self._channel = ""
        self._rows = []
        self.beginResetModel()
        self.endResetModel()

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
                                       row.address, text, row.bit)
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


class PointTableView(QtWidgets.QTableView):
    """点位表的视图：右键一行 = 四条命令——交还波形、属性、**加点位**、**删这一个点位**。

    每一条行**都**给这四项菜单，恢复项按归属置灰而**不删**：删项会让弹框高度随行变，
    操作员两次右键看见两种菜单（本项目把"状态变化动布局"记成缺陷）。恢复的文案带上点位名，
    因为一次只可能恢复右键的那一行，操作员不需要在弹层里再认一次地址。

    "加点位"落在**这一行所属的设备**上（表是按通道看的，行里带 unit），"删点位"删的就是
    右键的这一行；两个都以 `Row.key` 交出去，跟另外两项同一份身份（五元组，最后那位是
    S7 的"位"——同字节上的两个 bool 是两个点）。
    """

    restore_requested = QtCore.Signal(tuple)      # payload = 那一行的 `Row.key`
    props_requested = QtCore.Signal(tuple)        # 同一份身份：属性面板按 key 找点位本体
    add_point_requested = QtCore.Signal(tuple)
    remove_point_requested = QtCore.Signal(tuple)

    def row_key(self, row: int) -> tuple | None:
        """行号 -> 点位身份 `(channel, unit_id, area, address)`；越界回 `None`。"""
        found = self._row_at(row)
        return found.key if found is not None else None

    def row_menu_actions(self, row: int) -> list[QtGui.QAction]:
        """那一行右键菜单的动作清单：`contextMenuEvent` 与用例走**同一个**构造口。"""
        menu = self._menu_for(row)
        return [] if menu is None else menu.actions()

    def contextMenuEvent(self, event) -> None:                # noqa: N802
        # `event.pos()` 是控件坐标而 `indexAt` 吃的是视口坐标，中间必须过 `mapFrom`；
        # 两侧都按"没偏移"写的话，带边框的真实视图会整行错位。
        menu = self._menu_for(
            self.indexAt(self.viewport().mapFrom(self, event.pos())).row())
        if menu is None:
            event.ignore()               # 空白处/表头：没有哪一行可以被恢复
            return
        self._popup(menu, event.globalPos())

    def _popup(self, menu, pos) -> None:
        """弹层唯一的入口。`exec` 是会吞掉用例的模态嵌套循环（`IssueDialog.exec` 同形），
        所以弹出这一步单独成方法：界面用例替掉它，弹出的**内容**仍由 `_menu_for` 真建。"""
        menu.exec(pos)
        menu.deleteLater()

    def _row_at(self, row: int) -> Row | None:
        model = self.model()
        return model.row_at(row) if isinstance(model, PointTableModel) else None

    def _menu_for(self, row: int) -> QtWidgets.QMenu | None:
        found = self._row_at(row)
        if found is None:
            return None
        menu = QtWidgets.QMenu(self)
        action = menu.addAction(f"恢复波形：{found.name}")
        # 置灰的判据只有一条归属事实：manual 才谈得上"交还"。不查通道在不在跑——
        # 没起的通道给不出 manual 行（`_source_of`），真点了而通道恰好停了，
        # 拒绝由 `Session.restore_waveform` 点名，界面不重复造第二套判定。
        action.setEnabled(found.source == "manual")
        key = found.key
        action.triggered.connect(lambda _checked=False: self.restore_requested.emit(key))
        props = menu.addAction("属性…")
        # `属性…` 对每一行都开着：能不能改是 `point_props.apply()` 里带理由的拒绝
        # （通道在跑、验不过回滚都会点名），菜单先置灰就成了"没打开就拒"，
        # 操作员看不见那一句为什么。
        props.triggered.connect(lambda _checked=False: self.props_requested.emit(key))
        add = menu.addAction("添加点位…")
        # 指哪加哪：加的永远是右键那一行所属的设备（空的设备在表上没有行，那条路走设备树）
        add.triggered.connect(lambda _checked=False: self.add_point_requested.emit(key))
        drop = menu.addAction(f"删除点位：{found.name}…")
        drop.triggered.connect(lambda _checked=False: self.remove_point_requested.emit(key))
        return menu


def _display(row: Row, header: str) -> str:
    # `4x` 是一行里唯一算出来的格子，也是唯一可能算不出来的：带着错装载的行（区名写成
    # `holding`）在这里会抛，而这个 dict 一次构造**整行**，`data()` 又被问哪一列都走它
    # ——一抛就是 10 个格子全抛，Qt 把那行画成空白，操作员连"改完再启动"要开的这一行
    # 都找不到。所以显示层给"算不出"的标记，与点位属性面板 `_update_convention` 同一个
    # 口径；`conventional` 自己继续抛（它同时是面板的判定入口，那里靠异常区分"算不出"）。
    # 猜一个前缀或把 `holding` 当 `hr` 才是这里真正的风险：现场会照着假地址对点表。
    try:
        convention = conventional(row.area, row.address)
    except ValueError:
        convention = NO_CONVENTION
    return {
        # S7 的 bool 把位一起显示（`6.3`）——点表里的写法就是它，同字节上的两个 bool
        # 不带位在表上长得一模一样（写对了也看不出来右键的是哪一个）。Modbus 的点
        # `bit` 恒为 None，这一格与以前逐字相同。
        "名称": row.name, "区": row.area,
        "地址": f"{row.address}.{row.bit}" if row.bit is not None else str(row.address),
        "4x": convention, "类型": row.datatype,
        "字序": row.word_order, VALUE_HEADER: row.value_text,
        # S7 那一列装的其实是**字节**（`Row.registers` 在 S7 通道上是字节序列）：
        # 按两位十六进制打，别把 0x41 打成 0041——那看着像 16 位寄存器，会把现场带偏。
        # 判据用 datatype：S7 的类型名（real/word/...）与 Modbus 的（float32/uint16/...）
        # 不相交，且 validate 强制"类型与传输匹配"，所以这个判据是可靠的。
        "原始寄存器": " ".join(f"{b:02X}" if row.datatype in S7_TYPES else f"{r:04X}"
                             for b, r in [(v, v) for v in row.registers]),
        # spec §7：被人工接管的点，波形列显示"已停"而不是波形名——"曲线还在跑"和
        # "这格的值是人/主站写进去的"在排查下发问题时是两个完全不同的答案。
        # 文案占原来那一列，列宽与列序一个都不动（界面口径：装不下改文案，不加宽不截断）。
        "波形": "已停" if row.source == "manual" else row.waveform,
        "只读": "是" if row.readonly else "",
    }[header]
