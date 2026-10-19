"""网络页：把点表旁边那份声明的现状摊开，并给一个"应用"按钮。

为什么要有这一页：网络是唯一会改操作系统的那一层，而它的两种结果在现场长得一模一样
——"地址都配好了"和"压根没声明过"。以前只有命令行能看现状（`net show` / `net check`），
而现场常常只开着界面。

三条口径：

- **文字进只读文本框**，不进会自动换行的标签：标签一换行就改高度，布局恒等那几条断言
  立刻红（用户口径 3/4）；
- **刷新只读**：走 `net/startup.status()`，一条系统调用都不写，随便点；
- **应用要提权**，走 `net up` 那条路（缺权限时弹一次 UAC）。它和启动动作一样放到后台，
  界面用模态进度框挡住输入但仍能重绘；收回地址（`net clean`）不放在这里：那是要人逐条
  确认的动作，留在命令行。
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..net import startup as net_startup
from . import theme
from .background import run_waiting


class NetPanel(QtWidgets.QWidget):
    """现状 + 差异 + 一键应用。判定全在 `net/` 那一层，这里只负责显示与转达。"""

    # 一句话给状态栏：应用/拒绝的理由必须落到操作员看得见的地方，不能只在页里
    notice = QtCore.Signal(str)

    def __init__(self, session) -> None:
        super().__init__()
        self.session = session
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.view.setFont(theme.mono_font())     # 网卡名/地址按列对，等宽才看得齐
        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.apply_button = QtWidgets.QPushButton("应用（提权）")
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.apply_button)
        buttons.addStretch(1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addLayout(buttons)
        self.refresh_button.clicked.connect(self.refresh)
        self.apply_button.clicked.connect(self.apply)
        self._operation_busy = False
        # 起手**不读系统**：读一次网卡现状是几次 PowerShell（秒级），而多数人根本不会打开
        # 这一页。先在页里说明白，切进来或点刷新时再去读（`MainWindow._on_tab_changed`）。
        self.view.setPlainText("还没有读过系统：点「刷新」，或切到本页时自动读一次。")

    def _say(self, lines) -> None:
        self.view.setPlainText("\n".join(lines))

    def _read_status(self) -> list[str]:
        return net_startup.status(self.session.source, self.session.project)

    def refresh(self) -> None:
        """只读对账，写进文本框。总闸关着也照样看（口径见 `net/startup.status`）。"""
        if self._operation_busy:
            return
        self._operation_busy = True
        try:
            self._say(run_waiting(self.window(), "正在读取网络…", self._read_status))
        finally:
            self._operation_busy = False

    def apply(self) -> None:
        """"应用"= 跑一次 `net up`：建专用网卡（没有就建）+ 把缺的地址配上。"""
        source = self.session.source
        if not source:
            self.notice.emit("没有工程路径：先打开或保存一份点表，网络才知道按哪份声明配")
            return
        if self._operation_busy:
            return
        self._operation_busy = True
        try:
            self.notice.emit("正在准备网络（若弹出 UAC，请确认）…")
            code, lines = run_waiting(
                self.window(), "正在准备网络…",
                lambda: net_startup.apply_for_start(source, self.session.project))
            # 应用之后按新现状刷新。不能调 self.refresh()：那会被 _operation_busy 挡掉。
            self._say(run_waiting(self.window(), "正在读取网络…", self._read_status))
            if lines:
                self.notice.emit(lines[-1])
            elif code:
                self.notice.emit("网络没配好，原因见网络页")
            else:
                self.notice.emit("网络已一致，无需改动")
        finally:
            self._operation_busy = False
