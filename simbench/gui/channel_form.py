# simbench/gui/channel_form.py
"""通道表单：传输 + 名字 + 该传输的那几格。新建工程与添加通道共用这一份。

口径与 `channel_panel` 那一行、`point_props` 那个面板同一套：

* 类型是平铺的 checkable 单选按钮，**整个表单没有一个 QComboBox**（用户口径）；
* 换类型只改标签文字、已填的值与置灰状态，**几何一个矩形都不动**
  （`geometry_snapshot()` 逐格量，和那两个面板同一套写法）；选"空白工程"时整片字段**置灰
  不隐藏**——隐藏会让下面的控件集体上移，那正是"状态一变布局就跳"里最难看的一种；
* 表单只做**转换**：端口是整数、帧格式是 `8N1` 的样子、`rack/slot` 是 `0/1` 的样子，
  变不出来就当场拒（那不是值，是键盘上的字）。范围 / 类型搭配 / 重名 / 端口冲突这些判定
  一律走 `project.edit` 那条"改 → 全量验 → 差集"的管线，这里一句都不抄。

默认值不猜：TCP 的端口从 `edit.next_port` 来、串口名**留空**（`model.SERIAL_DEFAULTS` 那条：
COM1 很可能是别人真设备的串口，不替现场猜硬件）。留空的串口名会在落地那一步被
`validate_project` 自己那条 `serial_port` 拦住并点名——本表单不写第二份"必填"判定。
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..project import edit
from ..project.model import format_frame, parse_frame

_LABEL_WIDTH = 84
_FIELD_WIDTH = 190
_SHORT_WIDTH = 96
_HINT_HEIGHT = 22
# 提示那一格的宽度 = 表单可用宽度（对话框按那块 420 宽的拒绝框定尺寸，减掉标签列与间距）。
# 定 190（与字段同宽）时，最长那句提示要 240px，屏幕上直接被**硬切**半句——Qt 不接省略号，
# 而"串口名要自己填"这句正是要人看见的。截图照出来的（见 2026-10-13 那组界面截图）。
_HINT_WIDTH = 328

# 类型的显示名与"标签订单"：`_LABELS[kind]` 是那三格的标签（空串 = 这一格对这个类型没意义）
_KIND_LABELS = (("blank", "空白工程"), ("tcp", "TCP"), ("rtu", "串口 RTU"), ("s7", "S7"))
_LABELS = {
    "blank": ("", "", ""),
    "tcp": ("监听地址", "端口", ""),
    "rtu": ("串口名", "波特率", "帧格式"),
    "s7": ("监听地址", "端口", "机架/槽位"),
}
_HINTS = {
    "blank": "空工程：落地后在设备树上右键添加通道",
    "tcp": "监听地址 0.0.0.0 = 所有网卡",
    "rtu": "串口名要自己填：本工具不替现场猜 COM 口",
    "s7": "S7 惯例端口 102；机架 0 槽位 1",
}
_FIRST_TIPS = {
    "blank": "",
    "tcp": "监听地址（例：0.0.0.0）",
    "rtu": "串口名（例：COM3）——必须填，且不许含空格",
    "s7": "监听地址（例：0.0.0.0）；不许含 ':' 或 '/'",
}
_SECOND_TIPS = {"blank": "", "tcp": "监听端口（例：5020）", "rtu": "波特率（例：9600）",
                "s7": "S7 惯例端口 102"}
_THIRD_TIPS = {"blank": "", "tcp": "", "rtu": "帧格式：数据位/校验/停止位（例：8E1）",
               "s7": "机架/槽位（例：0/1；客户端连上来时报的那两个数）"}


def form_values(kind: str, channel) -> dict:
    """一个通道对象 -> 表单那四格。后三格的含义随类型变（与通道面板那一行同口径）。"""
    if kind == "rtu":
        return {"name": channel.name, "first": channel.port,
                "second": str(channel.baudrate),
                "third": format_frame(channel.bytesize, channel.parity, channel.stopbits)}
    if kind == "s7":
        return {"name": channel.name, "first": channel.host, "second": str(channel.port),
                "third": f"{channel.rack}/{channel.slot}"}
    return {"name": channel.name, "first": channel.host, "second": str(channel.port),
            "third": ""}


def starting_values(kind: str, project=None) -> dict:
    """起手值：按 `project.edit` 的默认通道算（不另抄一份默认值），没给工程就当空工程。

    "按现有工程算"这件事很要紧：往一份已经有 `tcp-5020` 的工程里再加一条通道，起手值
    必须是 `tcp-5021`——给一个一按就报"端口已被占用"的默认值等于让操作员先吃一次错。
    """
    if kind == edit.BLANK:
        return {"name": "", "first": "", "second": "", "third": ""}
    return form_values(kind, edit.default_channel(project or edit.Project(), kind))


class ChannelForm(QtWidgets.QWidget):
    """`kinds` 决定单选按钮有哪几个；`provider(kind) -> dict` 给每种的起手值（可换）。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None,
                 kinds=("tcp", "rtu", "s7"), start: str = "tcp", provider=None) -> None:
        super().__init__(parent)
        self._kinds = tuple(kinds)
        self._provider = provider or (lambda kind: starting_values(kind))
        self._buttons: dict[str, QtWidgets.QPushButton] = {}
        self._tracked: list[QtWidgets.QWidget] = []

        # 标签的高度要**钉死**：空标签（TCP 的第三格没有标签）的 sizeHint 比有字的矮
        # 2px，不钉的话"换个类型"就会让那一行的矩形变一次——这正是那几条布局用例要抓的。
        label_height = QtWidgets.QLabel("通道名").sizeHint().height()
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        kind_label = self._fixed(QtWidgets.QLabel("类型"), _LABEL_WIDTH, label_height)
        grid.addWidget(kind_label, 0, 0, QtCore.Qt.AlignTop)
        row = QtWidgets.QHBoxLayout()
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        for kind, caption in _KIND_LABELS:
            # 只摆调用方点名要的那几个：原来那句"或者就是 blank"写反了，结果是"添加通道"
            # 对话框里也躺着一个"空白工程"按钮，点下去走到 `default_channel(project, "blank")`
            # 报一句 `unknown transport '"'"'blank'"'"'`——用户看见的是个说不通的错
            if kind not in self._kinds:
                continue
            button = QtWidgets.QPushButton(caption)
            button.setCheckable(True)
            button.setMinimumWidth(78)
            button.clicked.connect(lambda _checked=False, k=kind: self.set_kind(k))
            group.addButton(button)
            row.addWidget(button)
            self._buttons[kind] = button
            self._tracked.append(button)
        row.addStretch(1)
        grid.addLayout(row, 0, 1)

        self.hint = self._fixed(QtWidgets.QLabel(), _HINT_WIDTH, _HINT_HEIGHT)
        self.hint.setWordWrap(False)
        grid.addWidget(self.hint, 1, 1)

        self.name_edit = QtWidgets.QLineEdit()
        self.first_edit = QtWidgets.QLineEdit()
        self.second_edit = QtWidgets.QLineEdit()
        self.third_edit = QtWidgets.QLineEdit()
        self.labels = []
        for offset, (label, edit_widget, width) in enumerate((
                ("通道名", self.name_edit, _FIELD_WIDTH),
                ("", self.first_edit, _FIELD_WIDTH),
                ("", self.second_edit, _SHORT_WIDTH),
                ("", self.third_edit, _SHORT_WIDTH)), start=2):
            text = QtWidgets.QLabel("通道名" if offset == 2 else "")
            text.setFixedWidth(_LABEL_WIDTH)
            text.setFixedHeight(label_height)
            edit_widget.setFixedWidth(width)
            grid.addWidget(text, offset, 0, QtCore.Qt.AlignTop)
            grid.addWidget(edit_widget, offset, 1, QtCore.Qt.AlignLeft)
            self.labels.append(text)
            self._tracked.append(text)
            self._tracked.append(edit_widget)
        self.name_edit.setToolTip("通道名（CSV/工程里的身份列，不许为空）")

        self.set_kind(start if start in self._buttons else self._kinds[0])

    @staticmethod
    def _fixed(widget, width, height=None):
        widget.setFixedWidth(width)
        if height is not None:
            widget.setFixedHeight(height)
        return widget

    # ---------- 对外 ----------

    def kind(self) -> str:
        """当前选中的类型（`blank` 是"空工程"，不是一种传输）。"""
        for kind, button in self._buttons.items():
            if button.isChecked():
                return kind
        return self._kinds[0]

    def pick(self, kind: str) -> None:
        """给用例/调用方用的"选一个"入口（按钮点击走的是同一条）。"""
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        """换类型：改标签、换起手值、置灰字段——**一个矩形都不动**。"""
        for name, button in self._buttons.items():
            button.setChecked(name == kind)
        hint = _HINTS[kind]
        self.hint.setText(hint)
        self.hint.setToolTip(hint)
        for label, text in zip(self.labels, ("通道名",) + _LABELS[kind]):
            label.setText(text)
        self.first_edit.setToolTip(_FIRST_TIPS[kind])
        self.second_edit.setToolTip(_SECOND_TIPS[kind])
        self.third_edit.setToolTip(_THIRD_TIPS[kind])
        values = self._provider(kind)
        self.name_edit.setText(values["name"])
        self.first_edit.setText(values["first"])
        self.second_edit.setText(values["second"])
        self.third_edit.setText(values["third"])
        usable = kind != edit.BLANK
        for widget in (self.name_edit, self.first_edit, self.second_edit, self.third_edit):
            widget.setEnabled(usable)
        # 第三格只对串口/S7 有意义：TCP 行留空并置灰（与通道面板那一行同口径）
        self.third_edit.setEnabled(usable and kind in ("rtu", "s7"))

    def values(self) -> dict:
        """表单里的值 -> `edit.default_channel(project, kind, **values)` 的参数。

        只做转换，转不出来抛 `EditRefused`（话术与 `channel_panel` 逐字一致——同一个字段
        在两处出现两种说法，现场会去查哪一份是真的）。
        """
        kind = self.kind()
        if kind == edit.BLANK:
            return {}
        name = self.name_edit.text().strip()
        if not name:
            raise edit.EditRefused(["通道名不许为空：它是 CSV/工程里的身份列"])
        first = self.first_edit.text().strip()
        second = self.second_edit.text().strip()
        third = self.third_edit.text().strip()
        if kind == "tcp":
            return {"name": name, "host": first, "port": self._int(second, "port")}
        if kind == "s7":
            parts = third.split("/")
            if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
                raise edit.EditRefused([f"rack/slot {third!r} must look like 0/1"])
            return {"name": name, "host": first, "port": self._int(second, "port"),
                    "rack": int(parts[0]), "slot": int(parts[1])}
        frame = parse_frame(third)
        if frame is None:
            raise edit.EditRefused([f"frame {third!r} must look like 8N1 "
                                    f"(data bits / parity / stop bits)"])
        return {"name": name, "port": first, "baudrate": self._int(second, "baudrate"),
                **frame}

    @staticmethod
    def _int(text: str, what: str) -> int:
        try:
            return int(text)
        except ValueError:
            raise edit.EditRefused([f"{what} {text!r} is not a number"]) from None

    def geometry_snapshot(self) -> tuple:
        """每个控件的完整矩形：换类型、换状态都不该有任何一项变化（沿用两个面板的写法）。"""
        return (self.size().width(), self.size().height(),
                tuple(widget.geometry().getRect() for widget in self._tracked))