# modslave/gui/point_props.py
"""点位属性面板：点表锁死的那些字段，每个点位逐点编辑的唯一入口。

点位表只有"当前值"可编辑（`gui/point_table` 的 α 口径），区/地址/类型/字序这些要改
就只剩导入整张 CSV 一条路——M4 的波形接管需要逐点调地址与波形，这个面板补的就是这一格。

分工与 `channel_panel.apply_edits` 一模一样，不抄第二份判定：

* 面板只做**转换**：地址框里的文字变成 int，变不出来（`4x00021`）就当场拒——那不是一
  个值，是键盘上的字。
* 类型与区搭不搭配、地址越界、波形参数缺这少那，全部住在 `validate_project` 那一份里：
  先写回工程 → 整份验一次 → 与改前的 error 清单做差集，只报"新增的那几条"；有新增就
  整体还原（连输入框一起退回），被拒的改动不进工程、不留 dirty。

生效时机（Task 6 控制器裁决，推翻 brief Step 5 的"apply → `session.load`"）：
`Session.load()` 第一步是 `_stop_all_locked()`——改一个点的地址就把另一路正给网关服务
的从站停掉，是新的现场事故。所以：

* **结构性字段**（区/地址/类型/字序/初值/只读）烘在 `start_channel` 时建好的 `SimDevice`
  里：所属通道**在跑**时一律整体拒绝并点名"先停通道"（不置灰、不半途应用、不排队）；
  通道停着时改完，没有任何通道在跑就走 `session.load`（索引与三视图一次对上），兄弟
  通道还在跑就走 `session.reindex`（只重建读写索引，不打扰任何在跑的通道）。
* **live 安全字段**（名称/波形/波形参数）原地改共享的 `Point` 对象即可：`_tick_once`
  每轮重读同一批对象（Task 3/4 已证），下一拍生效，**绝不**为此重载会话。

`initial`/`datatype` 在跑着时改不动，这条顺带堵住了跨度键的 stale 形状：`Point.span`
由这两个字段算出，接管键按跨度记，跨度在通道生命周期内不会变（裁决 4）。

界面口径（用户定死的几条，`tests/test_gui_point_props.py` 逐条量真实几何）：区/类型/
字序/波形是平铺的 checkable 单选按钮，**整个面板没有一个 QComboBox**；字序按
`word_orders_allowed(datatype)` 收窄——不可选项**置灰不隐藏**；状态变化只动
checked/enabled/文字，一个矩形都不许挪；装不下改文案，不加宽、不截省略号。
"""

from __future__ import annotations

import copy

from PySide6 import QtWidgets

from ..project.csv_io import format_wave_param, parse_wave_param
from ..project.model import AREAS, DATATYPES, WAVEFORMS, WORD_ORDERS, Point, \
    word_orders_allowed
from ..project.validate import error_lines, validate_project
from .point_table import conventional

# 结构性 vs live 安全是裁决的骨架，清单只在这一处定义：面板按它判拒绝与重载，
# 主窗按它措辞"什么时候生效"（live 安全 = 全字段里不属于结构性的那部分，不另列一份，
# 两份清单就是两处会漂移的真相）。抄第二份清单的地方就是漂移将要发生的地方。
STRUCTURAL_FIELDS = ("area", "address", "datatype", "word_order", "initial",
                     "readonly")

_FIELD_LABELS = {"area": "区", "address": "地址", "datatype": "类型",
                 "word_order": "字序", "initial": "初值", "readonly": "只读"}

_NOTICE_WIDTH = 420
_ERROR_VIEW_HEIGHT = 64


class PointPropertiesDialog(QtWidgets.QDialog):
    """一个点位一屏：所有字段的现在值、合法选项与拒绝理由都摆在同一张脸上。"""

    def __init__(self, parent, project, key: tuple) -> None:
        super().__init__(parent)
        # 通道在跑要拒绝、dirty 要经 Session 说话、装载/重建索引都在 Session 上：
        # 没有 session 的面板会变成一个"改了但没人知道生效时机"的编辑框，宁可构造即拒。
        self._session = getattr(parent, "session", None)
        if self._session is None:
            raise ValueError("the properties panel needs a parent that exposes .session")
        self._key = key                              # 与 `Row.key` 同源的四元组
        self._project = project
        self._real = _find_point(project, key)       # 工程本体：只有 apply 成功才落到它身上
        self._point: Point | None = copy.deepcopy(self._real)
        self.applied: dict | None = None
        self._error_lines: list[str] = []
        self._tracked: list[QtWidgets.QWidget] = []
        self._containers: dict[int, QtWidgets.QWidget] = {}
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
        self.convention_label = self._fixed(QtWidgets.QLabel(), 88)
        self.initial_edit = self._fixed(QtWidgets.QLineEdit(), 140)
        self.wave_param_edit = self._fixed(QtWidgets.QLineEdit(), 300)
        self.readonly_check = QtWidgets.QCheckBox()
        self.notice = QtWidgets.QLabel(
            "区/地址/类型/字序/初值/只读：须先停止所属通道才能改，改完重启通道后生效"
            "（重启前从站仍按旧配置应答）；名称/波形/波形参数：应用即写入点表，"
            "正在运行的通道下一拍生效。")
        self.notice.setWordWrap(True)
        self.notice.setFixedSize(_NOTICE_WIDTH, 76)
        self.error_view = QtWidgets.QPlainTextEdit()
        self.error_view.setReadOnly(True)
        self.error_view.setFixedSize(_NOTICE_WIDTH, _ERROR_VIEW_HEIGHT)

        self.area_buttons = self._radios(AREAS)
        self.datatype_buttons = self._radios(DATATYPES, columns=6)
        self.word_order_buttons = self._radios(WORD_ORDERS)
        self.waveform_buttons = self._radios(WAVEFORMS)
        for button in self.area_buttons.values():
            button.toggled.connect(lambda checked: checked and self._update_convention())
        for name, button in self.datatype_buttons.items():
            button.toggled.connect(
                lambda checked, n=name: checked and self._sync_word_orders(n))
        self.address_edit.textChanged.connect(self._update_convention)

        rows = [("名称", self.name_edit), ("区", self._box(self.area_buttons)),
                ("地址", self._address_row()), ("类型", self._box(self.datatype_buttons)),
                ("字序", self._box(self.word_order_buttons)),
                ("初值", self.initial_edit), ("波形", self._box(self.waveform_buttons)),
                ("波形参数", self.wave_param_edit), ("只读", self.readonly_check)]
        for index, (label, widget) in enumerate(rows):
            grid.addWidget(QtWidgets.QLabel(label), index, 0)
            grid.addWidget(widget, index, 1)
        grid.addWidget(self.notice, len(rows), 0, 1, 2)
        grid.addWidget(self.error_view, len(rows) + 1, 0, 1, 2)
        grid.addLayout(self._buttons(), len(rows) + 2, 0, 1, 2)

    def _radios(self, names, columns: int = 0) -> dict:
        """一组平铺的 checkable 单选按钮，按参数顺序交还 {文字: 按钮}。

        互斥联动在 click 与程序化 setChecked 两条路上行为不同（隐藏时 click 又走不到），
        所以勾选一律走 `_select` 显式"选中一个、清掉其余"：不依赖那条差异，
        摆上台面前后、隐藏与显示的行为才完全一致。
        """
        parent_box = QtWidgets.QWidget()
        layout = (QtWidgets.QHBoxLayout(parent_box) if not columns
                  else QtWidgets.QGridLayout(parent_box))
        layout.setContentsMargins(0, 0, 0, 0)
        buttons: dict[str, QtWidgets.QRadioButton] = {}
        for index, name in enumerate(names):
            button = QtWidgets.QRadioButton(name, parent_box)
            if columns:
                layout.addWidget(button, index // columns, index % columns)
            else:
                layout.addWidget(button, index)
            buttons[name] = button
        parent_box.setFixedSize(parent_box.sizeHint())   # 组容器定尺：勾选变化不推挤
        self._tracked.extend(buttons.values())
        self._containers[id(buttons)] = parent_box
        return buttons

    def _box(self, buttons: dict) -> QtWidgets.QWidget:
        """某组单选按钮的摆位容器（`_radios` 建好时按 dict 的 id 登记的）。"""
        return self._containers[id(buttons)]

    def _address_row(self) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.address_edit)
        row.addWidget(QtWidgets.QLabel("惯例"))
        row.addWidget(self.convention_label)
        self._tracked.append(self.address_edit)
        self._tracked.append(self.convention_label)
        return box

    def _buttons(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        apply_button = self._fixed(QtWidgets.QPushButton("应用"), 64)
        ok_button = self._fixed(QtWidgets.QPushButton("确定"), 64)
        cancel_button = self._fixed(QtWidgets.QPushButton("取消"), 64)
        apply_button.clicked.connect(lambda _=False: self.apply())
        ok_button.clicked.connect(self._accept_if_applied)
        cancel_button.clicked.connect(self.reject)
        for button in (apply_button, ok_button, cancel_button):
            row.addWidget(button)
            self._tracked.append(button)
        return row

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
            return self._refuse(f"address {text!r} is not an integer")
        target = {
            "name": self.name_edit.text().strip(),
            "area": self._checked(self.area_buttons),
            "address": address,
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
        old = {field: getattr(self._real, field) for field in changes}
        for field, value in changes.items():
            setattr(self._real, field, value)
        new = [line for line in self._errors() if line not in before]
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
            # 顺带把三视图重建到同一份事实上）；兄弟通道在跑 → reindex，绝不为一个已停
            # 通道的小编辑停掉正在服务网关的那一路。
            if any(self._channel_running(name)
                   for name in self._session.channel_names()):
                self._session.reindex()
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
            self.convention_label.setText("—")       # 地址不是数：惯例地址无从算，明说
            return
        self.convention_label.setText(
            conventional(self._checked(self.area_buttons), address))

    def _load_widgets(self, point: Point) -> None:
        # 单选组先摆正，再写地址框：setText 会触发 textChanged → `_update_convention`
        # → `_checked(area)`，组里还没勾任何一项时那条断言就会炸在构造路上。
        self.name_edit.setText(point.name)
        self._select(self.area_buttons, point.area)
        self._select(self.datatype_buttons, point.datatype)
        self._select(self.word_order_buttons, point.word_order)
        self._select(self.waveform_buttons, point.waveform)
        self.address_edit.setText(str(point.address))
        self.initial_edit.setText(point.initial)
        self.wave_param_edit.setText(format_wave_param(point.wave_param))
        self.readonly_check.setChecked(point.readonly)

    def _select(self, buttons: dict, name: str) -> None:
        for option, button in buttons.items():
            button.setChecked(option == name)

    @staticmethod
    def _checked(buttons: dict) -> str:
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


def _find_point(project, key: tuple) -> Point:
    channel_name, unit_id, area, address = key
    for ch in project.channels:
        if ch.name != channel_name:
            continue
        for dev in ch.devices:
            if dev.unit_id != unit_id:
                continue
            for pt in dev.points:
                if pt.area == area and pt.address == address:
                    return pt
    raise KeyError(f"点位 {channel_name}/{unit_id}/{area} {address} 不在当前点表")
