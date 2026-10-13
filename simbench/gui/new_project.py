# simbench/gui/new_project.py
"""新建工程 / 添加通道 / 添加设备三个对话框。

三个对话框只有同一件事：把界面上的字**转换**成 `project.edit` 的参数，然后让那条管线
（改 → 全量验 → 差集 → 不行就回滚）落地。范围、重名、端口冲突、串口名必填这些判定
一句都不在这里——被拒时摆到屏幕上的就是 `validate_project` 的原话。

节奏与 `gui/point_props` 一致：点"确定"→ 转换 → 落地 → **被拒就把理由留在面板上、面板
不关**（关掉面板等于把"为什么不行"一起关掉，操作员得重开一遍才能再试）；成功才 `accept()`。
`error_view` 因此**常驻**（空的时候也占着那块地方），与点属性面板同一口径：几何不随状态变。

落地（`edit.add_channel` / `add_device`）就是**就地改真工程**：`edit` 那些改动全部自带
"验不过就回滚"，所以"确定"= 提交、"取消"= 什么都没发生。主窗在这一步之后只负责刷新视图。
"""

from __future__ import annotations

from functools import partial

from PySide6 import QtCore, QtWidgets

from ..engine import ports
from ..project import edit
from ..project.model import Device
from .channel_form import ChannelForm, starting_values

_NOTICE_WIDTH = 420
_ERROR_VIEW_HEIGHT = 64
_FIELD_WIDTH = 190
_SHORT_WIDTH = 72
# 标签列的宽度按**最长的那一句**给：`unit id` 在 10pt 下要 91px，84 会把它硬切成
# `unit i…`（截图 + `test_every_fixed_slot_in_the_forms_fits_its_longest_caption` 抓到的）。
_LABEL_WIDTH = 104


class _EditDialog(QtWidgets.QDialog):
    """三个对话框的共同骨架：一块表单 + 一块定宽的拒绝理由 + 确定/取消。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        column = QtWidgets.QVBoxLayout(self)
        self.form_area = QtWidgets.QWidget(self)
        column.addWidget(self.form_area)
        # 定宽 + 只读 + 可滚动：理由有多长都不会把对话框撑开，也不会被截掉（`point_props`
        # 那一块同样的尺寸与理由）。
        self.error_view = QtWidgets.QPlainTextEdit(self)
        self.error_view.setReadOnly(True)
        self.error_view.setFixedSize(_NOTICE_WIDTH, _ERROR_VIEW_HEIGHT)
        column.addWidget(self.error_view)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self._accept_if_applied)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)

    def apply(self) -> list[str]:
        """转换 + 落地。成功回空清单；被拒**抛** `project.edit.EditRefused`（子类不接，
        由 `_accept_if_applied` 统一接住并摆到屏幕上）。不要在这里 `accept()`。"""
        raise NotImplementedError

    def error_lines(self) -> list[str]:
        return self.error_view.toPlainText().splitlines()

    def _accept_if_applied(self) -> None:
        try:
            reasons = self.apply()
        except edit.EditRefused as exc:
            reasons = exc.reasons
        except KeyError as exc:
            # 建对话框那一刻还在、点确定时已经不在了（换工程的一瞬间）：槽函数不许炸
            reasons = [f"目标已经不在这份工程里了：{exc}"]
        self.error_view.setPlainText("\n".join(reasons))
        if not reasons:
            self.accept()


class NewProjectDialog(_EditDialog):
    """新建工程：起步形态（空白 / TCP / 串口 / S7）+ 骨架参数。成功后 `project` 就绪。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None, probe=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建工程")
        self.project = None
        # 端口探针：默认走 `engine.ports.is_free`（绑一次看看），用例可以注入替身——
        # 对话框不该为了算一个默认值去真占端口。
        self._probe = probe or ports.is_free
        column = QtWidgets.QVBoxLayout(self.form_area)
        self.form = ChannelForm(self.form_area, kinds=("blank", "tcp", "rtu", "s7"),
                                start=edit.BLANK,
                                provider=lambda kind: starting_values(
                                    kind, probe=self._probe))
        self.form.kind_changed = self._note_bumped_port
        column.addWidget(self.form)
        self._note_bumped_port(edit.BLANK)

    def _note_bumped_port(self, kind: str) -> None:
        """默认端口被系统占着而顺延了的时候，在提示框里说一句。

        为什么说：`tcp-5020` 这个名字自带端口，顺延之后名字也会跟着变成 `tcp-5021`——
        不解释一句，操作员会以为工具把端口搞错了。
        """
        if kind not in ("tcp", "s7"):
            return
        start = edit.DEFAULT_TCP_PORT if kind == "tcp" else edit.DEFAULT_S7_PORT
        plain = edit.next_port(edit.Project(), start)          # 只避开工程内冲突的那个值
        chosen = self.form.second_edit.text().strip()
        if chosen and chosen != str(plain):
            self.error_view.setPlainText(
                f"{start} 现在被别的程序占着（或已被本工程用掉），默认端口顺延到 {chosen}；"
                f"要改就改左边那一格")

    def apply(self) -> list[str]:
        # 新建工程对着**一份空工程**分配名字与端口：它要替换掉当前这份，避不避开当前
        # 那份的端口都无所谓，而"从零算"才能与表单里的起手值对得上。
        built = edit.new_project(self.form.kind(), probe=self._probe,
                                 **self.form.values())
        self.project = built
        return []


class AddChannelDialog(_EditDialog):
    """往**当前工程**里加一条通道。成功后 `channel` 就绪（已经落进工程了）。"""

    def __init__(self, parent, project, probe=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加通道")
        self.project = project
        self.channel = None
        self._probe = probe or ports.is_free
        column = QtWidgets.QVBoxLayout(self.form_area)
        self.form = ChannelForm(self.form_area, kinds=edit.TRANSPORTS, start="tcp",
                                provider=partial(starting_values, project=project,
                                                 probe=self._probe))
        column.addWidget(self.form)

    def apply(self) -> list[str]:
        channel = edit.default_channel(self.project, self.form.kind(),
                                       probe=self._probe, **self.form.values())
        edit.add_channel(self.project, channel)
        self.channel = channel
        return []


class AddDeviceDialog(_EditDialog):
    """往某条通道里加一个设备（名字与 unit 的默认值都避开已有的）。"""

    def __init__(self, parent, project, channel_name: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"添加设备 → {channel_name}")
        self.project = project
        self.channel_name = channel_name
        self.device = None
        channel = project.find_channel(channel_name)
        grid = QtWidgets.QGridLayout(self.form_area)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        self.name_edit = QtWidgets.QLineEdit(edit.next_device_name(channel))
        self.name_edit.setFixedWidth(_FIELD_WIDTH)
        self.name_edit.setToolTip("设备名（CSV/工程里的身份列，不许为空）")
        self.unit_edit = QtWidgets.QLineEdit(str(edit.next_unit_id(channel)))
        self.unit_edit.setFixedWidth(_SHORT_WIDTH)
        self.unit_edit.setToolTip("unit id：同一通道内唯一，1..247")
        for row, (caption, widget) in enumerate((("设备名", self.name_edit),
                                                 ("unit id", self.unit_edit))):
            label = QtWidgets.QLabel(caption)
            label.setFixedWidth(_LABEL_WIDTH)
            grid.addWidget(label, row, 0, QtCore.Qt.AlignTop)
            grid.addWidget(widget, row, 1, QtCore.Qt.AlignLeft)

    def apply(self) -> list[str]:
        name = self.name_edit.text().strip()
        if not name:
            raise edit.EditRefused(["设备名不许为空：它是 CSV/工程里的身份列"])
        text = self.unit_edit.text().strip()
        try:
            unit_id = int(text)
        except ValueError:
            raise edit.EditRefused([f"unit_id {text!r} is not a number"]) from None
        device = Device(name=name, unit_id=unit_id)
        edit.add_device(self.project, self.channel_name, device)
        self.device = device
        return []