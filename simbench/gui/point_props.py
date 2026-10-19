# simbench/gui/point_props.py
"""点位属性面板：点表锁死的那些字段，每个点位逐点编辑的唯一入口。

点位表只有"当前值"可编辑（`gui/point_table` 的 α 口径），区/地址/类型/字序这些要改
就只剩导入整张 CSV 一条路——M4 的波形接管需要逐点调地址与波形，这个面板补的就是这一格。

分工与 `channel_panel.apply_edits` 一模一样，不抄第二份判定：

* 面板只做**转换**：地址框里的文字变成 int，变不出来（`4x00021`）就当场拒——那不是一
  个值，是键盘上的字。
* 类型与区搭不搭配、地址越界、波形参数缺这少那，全部住在 `validate_project` 那一份里：
  先写回工程 → 整份验一次 → 与改前的 error 清单做差集，只报"新增的那几条"；有新增就
  整体还原（连输入框一起退回），被拒的改动不进工程、不留 dirty。差集比较前先把**本次
  编辑点的新名**换回旧名（`_restore_name`）：错误消息里本来就带着点位名，不换就会把
  "同一条错换了个称呼"算成新增，于是带错的行永远改不了名。

生效时机（Task 6 控制器裁决，推翻 brief Step 5 的"apply → `session.load`"）：
`Session.load()` 第一步是 `_stop_all_locked()`——改一个点的地址就把另一路正给网关服务
的从站停掉，是新的现场事故。所以：

* **结构性字段**（区/地址/类型/字序/初值/只读）烘在 `start_channel` 时建好的 `SimDevice`
  里：所属通道**在跑**时一律整体拒绝并点名"先停通道"（不置灰、不半途应用、不排队）；
  通道停着时改完，没有任何通道在跑就走 `session.load`（索引、通道集合与三视图一次
  对上），兄弟通道还在跑就走 `session.reindex(那一行的通道)`（只重建读写索引，不打扰
  任何在跑的通道）。
* **live 安全字段**（名称/波形/波形参数）原地改共享的 `Point` 对象即可：`_tick_once`
  每轮重读同一批对象（Task 3/4 已证），下一拍生效，**绝不**为此重载会话。

点表是**带着 error 也能装载**的（spec §8：操作员开属性… 往往就是为了修那一行）。所以
存储值落不在合法选项里时（区 `holding`、类型 `float96`、字序 `ABDC`、波形 `triang`），
面板把那个值**原样补成一项并选中**——默认到第一个合法值就是在屏幕上编一个看着合法的
错值（表里写 `holding`、面板写 `hr`）。判定与装载侧一个字都不动，面板只是不崩。

`initial`/`datatype` 在跑着时改不动，这条顺带堵住了跨度键的 stale 形状：`Point.span`
由这两个字段算出，接管键按跨度记，跨度在通道生命周期内不会变，接管键因此不会陈旧。

界面口径（用户定死的几条，`tests/test_gui_point_props.py` 逐条量真实几何）：区/类型/
字序/波形是平铺的 checkable 单选按钮，**整个面板没有一个 QComboBox**；字序按
`word_orders_allowed(datatype)` 收窄——不可选项**置灰不隐藏**；状态变化只动
checked/enabled/文字，一个矩形都不许挪；装不下改文案，不加宽、不截省略号。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from PySide6 import QtWidgets

from ..project.csv_io import format_wave_param, parse_wave_param
from ..project.model import (
    AREAS,
    BOOL,
    DATATYPES,
    S7_AREAS,
    S7_TYPES,
    WAVEFORMS,
    WORD_ORDERS,
    Point,
    brief_name,
    channel_transport,
    parse_s7_area,
    word_orders_allowed,
)
from ..project.validate import error_lines, validate_project
from .point_table import NO_CONVENTION, conventional

# 结构性 vs live 安全是裁决的骨架，清单只在这一处定义：面板按它判拒绝与重载，
# 主窗按它措辞"什么时候生效"（live 安全 = 全字段里不属于结构性的那部分，不另列一份，
# 两份清单就是两处会漂移的真相）。抄第二份清单的地方就是漂移将要发生的地方。
STRUCTURAL_FIELDS = ("area", "address", "bit", "datatype", "word_order", "initial",
                     "readonly")

_FIELD_LABELS = {"area": "区", "address": "地址", "bit": "位", "datatype": "类型",
                 "word_order": "字序", "initial": "初值", "readonly": "只读"}

_NOTICE_WIDTH = 420
_ERROR_VIEW_HEIGHT = 64


@dataclass
class _Group:
    """一组平铺单选的摆位档案：容器、换行列数、被选中时要重算什么。

    `_ensure_option` 给脏值补的那一项必须和内置项共用这份档案，否则"带错装载的那一行"
    点开的面板里会有一组按钮行为不同（补进去的项不重算惯例显示、不收窄字序）——
    一个面板两种行为正是这类界面最难查的漂移。
    """

    box: QtWidgets.QWidget
    columns: int
    on_select: Callable[[str], None] | None



def _s7_area_options(current: str) -> tuple[str, ...]:
    """S7 点位的区选项：三个字母区 + **当前那个 `db<n>`**。

    DB 号枚举不完（1..4095），所以只能把点表里现有的那一个补进来选中——M4 定过的老规矩：
    存储值落不在合法选项里时原样补成一项并选中，面板不崩、也不悄悄改值。换一个 DB 号要走
    点表（CSV/工程文件），不在这个面板里。
    """
    if parse_s7_area(current) is not None and current not in S7_AREAS:
        return (*S7_AREAS, current)
    return S7_AREAS


class PointPropertiesDialog(QtWidgets.QDialog):
    """一个点位一屏：所有字段的现在值、合法选项与拒绝理由都摆在同一张脸上。"""

    def __init__(self, parent, project, key: tuple) -> None:
        super().__init__(parent)
        # 通道在跑要拒绝、dirty 要经 Session 说话、装载/重建索引都在 Session 上：
        # 没有 session 的面板会变成一个"改了但没人知道生效时机"的编辑框，宁可构造即拒。
        self._session = getattr(parent, "session", None)
        if self._session is None:
            raise ValueError("the properties panel needs a parent that exposes .session")
        # 与 `Row.key` 同源的五元组：(通道, unit, 区, 地址, 位)。最后那位只有 S7 的 bool
        # 非 None——同字节上的两个 bool 是两个点，身份里不带位就会打开/改错那一个。
        self._key = key
        self._project = project
        self._real = _find_point(project, key)       # 工程本体：只有 apply 成功才落到它身上
        self._point: Point | None = copy.deepcopy(self._real)
        self.applied: dict | None = None
        self._error_lines: list[str] = []
        self._tracked: list[QtWidgets.QWidget] = []
        self._groups: dict[int, _Group] = {}      # 按 {选项: 按钮} 那份 dict 的 identity 登记
        self.setWindowTitle(f"点位属性：{self._real.name}")
        self._build()
        self._load_widgets(self._point)
        self._update_convention()
        self._show_errors()

    # ---------- 结构 ----------

    def _build(self) -> None:
        grid = QtWidgets.QGridLayout(self)
        self.name_edit = self._fixed(QtWidgets.QLineEdit(), 180)
        self.address_edit = self._fixed(QtWidgets.QLineEdit(), 64)
        # S7 的 bool 位（`DBX6.3` 里的那个 3）：它是**地址的一部分**，所以是结构性字段。
        # 只有"通道是 S7 且类型是 bool"时启用，其余情况**置灰不隐藏**（与本面板其它被收窄
        # 的选项同一条口径：藏起来会让下面的行集体上移）。
        self.bit_edit = self._fixed(QtWidgets.QLineEdit(), 48)
        self.bit_edit.setToolTip("S7 的 bool 位：`DBX6.3` 里的 3（0..7）")
        self.bit_hint = QtWidgets.QLabel("S7 的 bool 才有")
        self.convention_label = self._fixed(QtWidgets.QLabel(), 88)
        self.initial_edit = self._fixed(QtWidgets.QLineEdit(), 140)
        self.wave_param_edit = self._fixed(QtWidgets.QLineEdit(), 300)
        self.readonly_check = QtWidgets.QCheckBox()
        self.notice = QtWidgets.QLabel(
            "区/地址/位/类型/字序/初值/只读：须先停止所属通道才能改，改完重启通道后生效"
            "（重启前从站仍按旧配置应答）；名称/波形/波形参数：应用即写入点表，"
            "正在运行的通道下一拍生效。")
        self.notice.setWordWrap(True)
        self.notice.setFixedSize(_NOTICE_WIDTH, 76)
        self.error_view = QtWidgets.QPlainTextEdit()
        self.error_view.setReadOnly(True)
        self.error_view.setFixedSize(_NOTICE_WIDTH, _ERROR_VIEW_HEIGHT)
        # 与两个建工程的对话框同一条：常驻的空框给一句占位说明，几何不动
        self.error_view.setPlaceholderText(
            "被拒的理由会出现在这里（校验原话，不截断）")

        # 区/类型的选项按**点所属通道的传输**给：S7 点位上是 `db<n>`/mk/pe/pa 与 S7 的
        # 类型名。给成 Modbus 那一套的话，操作员选一下就把 `hr`/`uint16` 写进 S7 点位里
        # （validate 会拦，但那是一次无谓的往返，而且"选项里能选的就是错的"本身就在骗人）。
        s7 = channel_transport(self._channel()) == "s7"
        self.area_buttons = self._radios(
            _s7_area_options(self._real.area) if s7 else AREAS,
            on_select=lambda _area: self._update_convention())
        self.datatype_buttons = self._radios(
            S7_TYPES if s7 else DATATYPES, columns=6,
            on_select=self._on_datatype_selected)
        self.word_order_buttons = self._radios(WORD_ORDERS)
        self.waveform_buttons = self._radios(WAVEFORMS)
        self.address_edit.textChanged.connect(self._update_convention)

        rows = [("名称", self.name_edit), ("区", self._box(self.area_buttons)),
                ("地址", self._address_row()), ("位", self._bit_row()),
                ("类型", self._box(self.datatype_buttons)),
                ("字序", self._box(self.word_order_buttons)),
                ("初值", self.initial_edit), ("波形", self._box(self.waveform_buttons)),
                ("波形参数", self.wave_param_edit), ("只读", self.readonly_check)]
        for index, (label, widget) in enumerate(rows):
            grid.addWidget(QtWidgets.QLabel(label), index, 0)
            grid.addWidget(widget, index, 1)
        grid.addWidget(self.notice, len(rows), 0, 1, 2)
        grid.addWidget(self.error_view, len(rows) + 1, 0, 1, 2)
        grid.addWidget(self._buttons(), len(rows) + 2, 0, 1, 2)

    def _radios(self, names, columns: int = 0,
                on_select: Callable[[str], None] | None = None) -> dict:
        """一组平铺的 checkable 单选按钮，按参数顺序交还 {文字: 按钮}。

        互斥联动在 click 与程序化 setChecked 两条路上行为不同（隐藏时 click 又走不到），
        所以勾选一律走 `_select` 显式"选中一个、清掉其余"：不依赖那条差异，
        摆上台面前后、隐藏与显示的行为才完全一致。

        `on_select(选项)` 是这组"被选中时要重算什么"，登记进 `_Group`：
        `_ensure_option` 后补的那一项走同一份档案，两种行为才不是一个面板两套规矩。
        """
        parent_box = QtWidgets.QWidget()
        layout = (QtWidgets.QHBoxLayout(parent_box) if not columns
                  else QtWidgets.QGridLayout(parent_box))
        layout.setContentsMargins(0, 0, 0, 0)
        buttons: dict[str, QtWidgets.QRadioButton] = {}
        for index, name in enumerate(names):
            buttons[name] = self._add_option(parent_box, layout, index, columns,
                                             name, on_select)
        parent_box.setFixedSize(parent_box.sizeHint())   # 组容器定尺：勾选变化不推挤
        self._groups[id(buttons)] = _Group(box=parent_box, columns=columns,
                                           on_select=on_select)
        return buttons

    def _add_option(self, box, layout, index: int, columns: int, name: str,
                    on_select) -> QtWidgets.QRadioButton:
        """往一组的摆位里放一个按钮：内置项和脏值补的项共用这一条路。"""
        button = QtWidgets.QRadioButton(name, box)
        if columns:
            layout.addWidget(button, index // columns, index % columns)
        else:
            layout.addWidget(button, index)
        if on_select is not None:
            button.toggled.connect(
                lambda checked, option=name: checked and on_select(option))
        self._tracked.append(button)
        return button

    def _ensure_option(self, buttons: dict, value: str) -> None:
        """点表里存着、合法选项里却没有的那个值，原样补成一项。

        带着错装载的行（spec §8：有 error 也照样装，操作员开的就是这一行去修它）默认到
        第一个合法值，屏幕上出现的就是点表里根本没有的值——表里写 `holding`、面板写
        `hr`，正是本项目最防的那类"看着合法的错值"。补成一项看得见真相，
        `validate_project` 那一份判定一个字不动，它仍然是错。
        补进来的项**不删**：被拒回滚后本体可能还是那个脏值，留着才显示得出来。
        """
        if value in buttons:
            return
        group = self._groups[id(buttons)]
        # 空值也是点表里的一个值：给个占位文字，别让一个没字的按钮看着像界面坏了。
        button = self._add_option(group.box, group.box.layout(), len(buttons),
                                  group.columns, value or "(空)", group.on_select)
        buttons[value] = button
        group.box.setFixedSize(group.box.sizeHint())   # 多一项要算进容器尺寸

    def _channel(self):
        """这个点位所属的通道对象（区/类型的选项要按它的传输给）。"""
        return self._project.find_channel(self._key[0])

    def _box(self, buttons: dict) -> QtWidgets.QWidget:
        """某组单选按钮的摆位容器（`_radios` 建好时按 dict 的 id 登记的）。"""
        return self._groups[id(buttons)].box

    def _address_row(self) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.address_edit)
        row.addWidget(QtWidgets.QLabel("点表地址"))
        row.addWidget(self.convention_label)
        self._tracked.append(self.address_edit)
        self._tracked.append(self.convention_label)
        return box

    def _bit_row(self) -> QtWidgets.QWidget:
        """位那一行：一个格子 + 一句说明（置灰时两个都灰，行不消失）。"""
        box = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.bit_edit)
        row.addWidget(self.bit_hint)
        row.addStretch(1)
        self._tracked.append(self.bit_edit)
        self._tracked.append(self.bit_hint)
        return box

    def _on_datatype_selected(self, datatype: str) -> None:
        """类型一换，两件事跟着重算：字序的可选范围、位那一格能不能填。"""
        self._sync_word_orders(datatype)
        self._sync_bit_enabled()

    def _sync_bit_enabled(self) -> None:
        enabled = (channel_transport(self._channel()) == "s7"
                   and self._checked(self.datatype_buttons) == BOOL)
        self.bit_edit.setEnabled(enabled)
        self.bit_hint.setEnabled(enabled)

    def _buttons(self) -> QtWidgets.QDialogButtonBox:
        """底部三个按钮：**跟着系统的按钮顺序与对齐**（Windows 上是 确定 · 取消 · 应用，整组靠右）。

        为什么不再手写一排：原来那版是"应用 | 确定 | 取消"三个定宽按钮在整行里**均匀铺开**，
        看着像一排并列的功能按钮，而不是"这个对话框怎么结束"——主按钮（确定）既不在边上、
        也不在习惯的位置上。Windows/Qt 的惯例是 **确定 · 取消 · 应用** 且整组靠右；本软件里
        新建工程、另存为、错误清单那些弹窗用的都是 `QDialogButtonBox`，两处不一致时，同一个
        软件里就会出现两套"哪个是主按钮"的读法（2026-10-19 现场反馈）。

        定宽槽照旧（每个 64px，进 `_tracked`）：几何恒等断言照样盯着它们；按钮文字由 Qt 的
        `zh_CN` 翻译器给（`gui/i18n.py`），这里不再自己写死"确定/取消"。
        """
        box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.Apply, self)
        box.accepted.connect(self._accept_if_applied)
        box.rejected.connect(self.reject)
        box.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(
            lambda _checked=False: self.apply())
        for button in box.buttons():
            self._fixed(button, 64)
            self._tracked.append(button)
        self.buttons = box
        return box

    def _accept_if_applied(self) -> None:
        if self.apply():
            self.accept()

    @staticmethod
    def _fixed(widget, width):
        widget.setFixedWidth(width)
        return widget

    # ---------- 对外 API（brief Produces，测试逐字用这些名字） ----------

    @property
    def point(self) -> Point | None:
        """当前编辑中的副本：apply 成功之前工程本体一个字都不动。"""
        return self._point

    def pick_area(self, area: str) -> None:
        self._select(self.area_buttons, area)

    def pick_datatype(self, datatype: str) -> None:
        self._select(self.datatype_buttons, datatype)

    def pick_word_order(self, order: str) -> None:
        self._select(self.word_order_buttons, order)

    def pick_waveform(self, waveform: str) -> None:
        self._select(self.waveform_buttons, waveform)

    def error_lines(self) -> list[str]:
        """上一次 apply 被拒的理由（新增错误清单或转换/停通道的拒绝），成功时为空。"""
        return list(self._error_lines)

    def snapshot_point(self) -> Point | None:
        """编辑副本的深拷贝：回滚用例拿它和改动前比，别拿到会跟着变的活引用。"""
        return copy.deepcopy(self._point) if self._point is not None else None

    def geometry_snapshot(self) -> tuple:
        """沿用 `channel_panel.geometry_snapshot()` 的写法：只含矩形，不含可见性。

        隐藏/移除一个控件会把后面的矩形集体挪位，所以"藏起来"在这种快照下藏不住；
        置灰与勾选不改度量，快照因此对**所有**合法状态变化恒定。
        """
        return (self.size().width(), self.size().height(),
                tuple(widget.geometry().getRect() for widget in self._tracked))

    def apply(self) -> bool:
        """转换 → 分类 → （必要时）停通道检查 → 写回本体 → 全量验 → 只比新增 → 落地。

        返回 False 时什么都没发生：本体没动、dirty 没碰、applied 没写——界面上给出的
        就是 `error_lines()` 那几句。
        """
        if self._point is None:                      # done() 之后的实例不许再被 apply
            return False
        text = self.address_edit.text().strip()
        try:
            address = int(text)
        except ValueError:
            if channel_transport(self._channel()) == "s7" and "." in text:
                # 点表/CSV 里 S7 的地址写成 `字节.位`，于是有人会照着往这一格里填——
                # 指到"位"那一格去，比一句"不是整数"有用。
                return self._refuse(f"address {text!r} must be the byte only; "
                                    f"the bit has its own 位 field (write it as "
                                    f"byte + 位, like 6 and 3 for DBX6.3)")
            return self._refuse(f"address {text!r} is not an integer")
        bit_text = self.bit_edit.text().strip()
        if self.bit_edit.isEnabled():
            if not bit_text:
                return self._refuse("位不许为空：S7 的 bool 是 `字节.位`（`DBX6.3` 的 3）")
            try:
                bit = int(bit_text)
            except ValueError:
                return self._refuse(f"bit {bit_text!r} is not an integer")
        else:
            # 这一格置灰说明这个点位不该有位（非 S7、或类型不是 bool）：清掉它。
            # 带位的非 bool 点、带位的 Modbus 点都是 validate 拦下的形状（装载时可能带着），
            # 而"清掉"是让这份点表合法化的唯一方向。
            bit = None
        target = {
            "name": self.name_edit.text().strip(),
            "area": self._checked(self.area_buttons),
            "address": address,
            "bit": bit,
            "datatype": self._checked(self.datatype_buttons),
            "word_order": self._checked(self.word_order_buttons),
            "initial": self.initial_edit.text().strip(),
            "waveform": self._checked(self.waveform_buttons),
            "wave_param": parse_wave_param(self.wave_param_edit.text()),
            "readonly": self.readonly_check.isChecked(),
        }
        changes = {field: value for field, value in target.items()
                   if value != getattr(self._point, field)}
        if not changes:
            return True                              # 没改任何东西：不验、不写、不置 dirty
        structural = [field for field in STRUCTURAL_FIELDS if field in changes]
        if structural and self._channel_running(self._key[0]):
            # 拒绝要**点名**且发生在写回之前：半结构性应用（live 那半落了地、结构那半
            # 没落）会让操作员分不清哪一半生效了，比整体不动更难查。
            return self._refuse(
                f"{self._point.name}: channel {self._key[0]} is running; "
                f"stop the channel first, then change "
                f"{'/'.join(_FIELD_LABELS[field] for field in structural)}")
        before = set(self._errors())
        old_name = self._real.name
        old = {field: getattr(self._real, field) for field in changes}
        for field, value in changes.items():
            setattr(self._real, field, value)
        # 差集比较先对上"称呼"：`validate` 的消息里本来就有点位名（自己的定位串、别人
        # 引用它的 `already used by`），不改回来就会把"同一条错换了个名字"算成新增，
        # 带错的行与它的邻居从此互相锁死、谁也别想改名。给操作员看的还是原样那几条。
        new = [line for line in self._errors()
               if _restore_name(line, self._real.name, old_name) not in before]
        if new:
            for field, value in old.items():
                setattr(self._real, field, value)
            # 被拒的值要连输入框一起退回（channel_panel 同口径）：留着它，下一次编辑
            # 就基于一个工程里根本不存在的世界，"拒了还能再改"是演出来的。
            self._load_widgets(self._real)
            self._error_lines = new
            self._show_errors()
            return False
        self._point = copy.deepcopy(self._real)
        self.applied = {**(self.applied or {}), **changes}
        self._error_lines = []
        self._show_errors()
        self.setWindowTitle(f"点位属性：{self._real.name}")
        if structural:
            # 结构性改动必须让 `Session._points`（按 area+address 建键）跟上，否则启动
            # 之后按新地址写值会被挡在"不在点表里"。没有任何通道在跑 → load（裁决原话，
            # 顺带把三视图与通道集合重建到同一份事实上）；兄弟通道在跑 → reindex(这一行的
            # 通道)，绝不为一个已停通道的小编辑停掉正在服务网关的那一路。
            if any(self._channel_running(name)
                   for name in self._session.channel_names()):
                self._session.reindex(self._key[0])
            else:
                self._session.load(self._project)
        self._session.mark_dirty()
        return True

    def done(self, result: int) -> None:
        """关板即放手：不再持有工程里的 Point 引用（spec §12/§13 的 GC 口径对面板同样成立）。

        `applied` 是普通 dict，关掉后主窗照样读——留引用的理由一条都没有，
        多留一条就多一条面板寿命挂在工程对象上的暗线。
        """
        self._point = None
        self._real = None
        super().done(result)

    # ---------- 内部 ----------

    def _sync_word_orders(self, datatype: str) -> None:
        """字序按 `word_orders_allowed` 收窄：**合法选项变少，控件一个都不藏**。

        已勾选但被收窄掉的项保持勾选+置灰，apply 会把它交给 `validate_project` 拒绝
        （`word_order`/`word_order_ignored`）——面板不自作主张改用户的字序，
        悄悄重置成 ABCD 就是替操作员做了一个他没做过的决定。
        """
        allowed = word_orders_allowed(datatype)
        for order, button in self.word_order_buttons.items():
            button.setEnabled(order in allowed)

    def _update_convention(self) -> None:
        """`4x` 惯例显示只由 (当前区, 当前地址文字) 现算——不另存一份，不猜。"""
        text = self.address_edit.text().strip()
        try:
            address = int(text)
        except ValueError:
            self.convention_label.setText(NO_CONVENTION)   # 地址不是数：算不出，明说
            return
        try:
            self.convention_label.setText(
                conventional(self._checked(self.area_buttons), address))
        except ValueError:
            # 带着错装载的行，区名可以是 `holding` 这种合法表里没有的东西：`conventional`
            # 对未知区名直接抛（它不猜前缀）。这一格给"算不出"，不给半截旧值，
            # 也不在面板里抄第二份区名白名单——那份判定住在 validate_project 里。
            self.convention_label.setText(NO_CONVENTION)

    def _load_widgets(self, point: Point) -> None:
        # 单选组先摆正，再写地址框：setText 会触发 textChanged → `_update_convention`
        # → `_checked(area)`，组里还没勾任何一项时那条断言就会炸在构造路上。
        self.name_edit.setText(point.name)
        self._load_choice(self.area_buttons, point.area)
        self._load_choice(self.datatype_buttons, point.datatype)
        self._load_choice(self.word_order_buttons, point.word_order)
        self._load_choice(self.waveform_buttons, point.waveform)
        # 补进来的那一项是在"类型"那一步的收窄**之后**才出现的，`_sync_word_orders` 没
        # 见过它。按当前类型再收窄一次：脏字序与"合法但被收窄"的字序在界面上才是同一个
        # 形状（勾选 + 置灰），而面板依旧不替操作员重置字序。
        self._sync_word_orders(point.datatype)
        self.address_edit.setText(str(point.address))
        self.bit_edit.setText("" if point.bit is None else str(point.bit))
        # `_load_choice` 走的是 setChecked（不触发 clicked），所以联动要在这里显式补一次
        self._sync_bit_enabled()
        self.initial_edit.setText(point.initial)
        self.wave_param_edit.setText(format_wave_param(point.wave_param))
        self.readonly_check.setChecked(point.readonly)

    def _load_choice(self, buttons: dict, value: str) -> None:
        """把这个点位存的值摆进组里：合法选项里没有就先补一项，然后选中它。"""
        self._ensure_option(buttons, value)
        self._select(buttons, value)

    def _select(self, buttons: dict, name: str) -> None:
        for option, button in buttons.items():
            button.setChecked(option == name)

    @staticmethod
    def _checked(buttons: dict) -> str:
        """组里当前选中的那一项。

        不许在这里"退回第一个选项"：调用方要的就是界面上选中的那个值，猜一个就等于
        替操作员做了一个他没做过的决定。`_load_choice` 保证组里永远有一项被选中，
        包括点表里存的脏值——所以走不到下面这句。
        """
        for option, button in buttons.items():
            if button.isChecked():
                return option
        raise AssertionError("no option is checked; the dialog sets one on construction")

    def _channel_running(self, name: str) -> bool:
        runtime = self._session.channel(name)
        return bool(runtime is not None and runtime.running)

    def _errors(self) -> list[str]:
        """当前拦住启动的那几条。判定住在 `validate_project`，面板一份都不抄。"""
        return error_lines(validate_project(self._project))

    def _refuse(self, message: str) -> bool:
        self._error_lines = [message]
        self._show_errors()
        return False

    def _show_errors(self) -> None:
        # β 口径：拒绝理由要在屏幕上看得见，只把返回值给调用方等于没拒绝。
        self.error_view.setPlainText("\n".join(self._error_lines))


def _restore_name(line: str, new_name: str, old_name: str) -> str:
    """把错误行里"本次编辑点的新名"换回它的旧名——只为和改前那份清单对得上。

    点位名会出现在两类消息里：这一行自己的定位串，和别人的 `already used by X`。
    两者都由 `brief_name` 渲染（限长口径在同一处），所以这里也按 `brief_name` 的形式换，
    长名字被截断后照样换得回来。这不是判定、也不改清单：判定与消息全文都只住在
    `validate_project` 一份里，本函数只服务于"哪些是本次改动新增的"那一次差集比较。
    """
    if not new_name or new_name == old_name:
        return line
    return line.replace(brief_name(new_name), brief_name(old_name))


def _find_point(project, key: tuple) -> Point:
    channel_name, unit_id, area, address, bit = key
    for ch in project.channels:
        if ch.name != channel_name:
            continue
        for dev in ch.devices:
            if dev.unit_id != unit_id:
                continue
            for pt in dev.points:
                if pt.area == area and pt.address == address and pt.bit == bit:
                    return pt
    raise KeyError(f"点位 {channel_name}/{unit_id}/{area} {address}"
                   + (f".{bit}" if bit is not None else "") + " 不在当前点表")
