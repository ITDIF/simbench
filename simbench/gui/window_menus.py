"""主窗的菜单与工具栏（从 main_window 拆出）。"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import icons, theme
from .dialogs import (
    ProjectFileError,
    read_point_table,
)
from .window_consts import (
    _NOTICE_MS,
)


class MenuMixin:
    def _build_toolbar(self) -> None:
        bar = self.addToolBar("main")
        bar.setObjectName("mainToolBar")
        bar.setMovable(False)                     # 可拖走的工具栏 = 布局会自己挪
        # 图标与文字并排：只留图标的话，现场得靠悬停猜哪个是"全部停止"，
        # 而这一排按钮里"停止"点错的代价是掉一路正在服务的从站。
        bar.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        bar.setIconSize(icons.icon_size())
        self.toolbar = bar
        self.act_start = self._toolbar_action(bar, "play", "全部启动", self._start_all)
        self.act_stop = self._toolbar_action(bar, "stop", "全部停止", self._stop_all)
        bar.addSeparator()
        self.act_new = self._toolbar_action(bar, "new", "新建工程", self._new_project)
        self.act_import = self._toolbar_action(bar, "in", "导入点表", self._import_table)
        self.act_export = self._toolbar_action(bar, "out", "导出点表", self._export_table)
        self.act_open = self._toolbar_action(bar, "folder", "打开工程", self._open_project)
        self.act_save = self._toolbar_action(bar, "save", "保存工程", self._save_project)
        bar.addSeparator()
        self.act_check = self._toolbar_action(bar, "check", "检查点表", self._check)
        bar.addSeparator()
        self.act_theme = self._toolbar_action(bar, "dark", "深色主题", self._toggle_theme)
        self.act_theme.setCheckable(True)
        # 起手模式可能来自环境变量（`SIMBENCH_THEME=dark`），勾选状态必须与真现况一致
        self.act_theme.setChecked(theme.current_mode() == "dark")
        self.act_theme.setShortcut("Ctrl+Shift+D")
        self.act_theme.setToolTip("在浅色/深色之间切换（Ctrl+Shift+D）")

    def _toolbar_action(self, bar: QtWidgets.QToolBar, icon: str, text: str, slot):
        """工具栏动作的唯一构造口：文字是常量、图标按名字现画，动作顺序与文案都不变。

        图标名登记在 `self._icon_names` 上：切主题时底色变了，位图图标要按新模式重画一遍
        （样式表刷不掉已经画好的 QPixmap）。
        """
        action = bar.addAction(icons.icon(icon), text, slot)
        action.setToolTip(text)
        self._icon_names.append((action, icon))
        return action

    def _build_menu(self) -> None:
        """"文件"菜单：工具栏那排按钮的家谱，也是"另存为"与快捷键的家。

        为什么要它：工具栏已经 9 个动作（每个都要配图标与定宽槽的断言），而"保存"与
        "另存为"是**两件事**——塞进菜单既给得出快捷键，也不用再加两个按钮。
        菜单项与工具栏按钮是**同一批 QAction 对象**（行为只有一份，不会两处漂移）。
        """
        menu = self.menuBar().addMenu("文件")
        self.act_recent = QtWidgets.QMenu("最近打开", self)
        self.act_recent.setToolTip("最近打开过的工程/点表（只列还在的文件）")
        # 每次弹出前现建：菜单开着的时候文件可能被删了/换了（`existing_recent` 会滤掉）
        self.act_recent.aboutToShow.connect(self._rebuild_recent_menu)
        self.act_save_as = QtGui.QAction("另存为…", self)
        self.act_save_as.setToolTip("总是问路径（保存是有路径就直接写回）")
        self.act_save_as.triggered.connect(
            lambda _checked=False: self._save_project(ask=True))
        for action, shortcut in ((self.act_new, "Ctrl+N"), (self.act_open, "Ctrl+O"),
                                 (self.act_import, "Ctrl+I"), (self.act_save, "Ctrl+S"),
                                 (self.act_save_as, "Ctrl+Shift+S"),
                                 (self.act_export, "Ctrl+E")):
            action.setShortcut(shortcut)
            menu.addAction(action)
        # "最近打开"插在"打开工程"后面（上面那一段加完之后插，否则它会被插到最前面）
        menu.insertAction(self.act_import, self.act_recent.menuAction())

    def _rebuild_recent_menu(self) -> None:
        """现建"最近打开"里那几条（只列**还在**的文件，一个都不在时置灰一条说明）。"""
        self.act_recent.clear()
        if self.settings is None:
            action = self.act_recent.addAction("（这次没有记住任何文件）")
            action.setEnabled(False)
            return
        paths = self.settings.existing_recent()
        if not paths:
            action = self.act_recent.addAction("（还没有打开过文件）")
            action.setEnabled(False)
            return
        for text in paths:
            action = self.act_recent.addAction(text)
            action.triggered.connect(
                lambda _checked=False, target=text: self._open_recent(target))

    def _open_recent(self, path: str) -> None:
        """从"最近打开"里打开一份（与"打开工程"同一条路：先问未保存改动）。"""
        if not self._confirm_discard():
            return
        try:
            project, notes = read_point_table(path)
        except ProjectFileError as exc:
            self._log(f"[refused] 打不开 {path}：{exc}")
            self.status.showMessage("这个文件打不开，原因见日志页", _NOTICE_MS)
            # 打不开的多半已经不在/坏了：从最近列表里摘掉，免得菜单里一直挂着一个死条目
            self.settings.recent = [p for p in self.settings.recent if p != path]
            self._remember_settings()
            return
        self.use_project(project, path, notes=notes)

    def _toggle_theme(self, _checked: bool = False) -> None:
        """浅色/深色切换。只换颜色——几何一个像素都不动（见 `test_gui_theme.py`）。

        切完必须把"按配色画出来的东西"重涂一遍：状态色走 QPalette（设备树、通道面板）、
        工具栏图标是已经画好的位图，这两类都不是换一张样式表就能刷掉的。
        """
        mode = theme.toggle()
        if self.settings is not None:
            self.settings.theme = mode                   # 关窗时随几何一起落盘
        self.act_theme.setChecked(mode == "dark")
        for action, name in self._icon_names:
            action.setIcon(icons.icon(name))
        for channel, state in self._states.items():
            self.tree.set_status(channel, state)
            self.channels.set_status(channel, state)
        self.status.showMessage(
            "已切到深色主题" if mode == "dark" else "已切到浅色主题", _NOTICE_MS)
