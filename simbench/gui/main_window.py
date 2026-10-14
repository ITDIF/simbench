# simbench/gui/main_window.py
"""主窗：工具栏 + 上栏（通道面板 / 左树右表） + 下标签页（报文监视、日志、网络） + 状态栏。

刷新是**拉的**：10Hz 心跳调 `point_model.refresh()` 与 `packet_panel.refresh_view()`。
引擎侧不推 UI（M2 定的口径），实测 200 行一次 `dataChanged` 只要 0.34ms，所以不做得
增量 diff，也不做虚拟滚动。

布局恒等（用户口径 3/4/5）靠这几件事，`tests/test_gui_layout_invariant.py` 逐条量真实
几何，不靠肉眼：

* 上下两栏的 `QSplitter` 两段都 `setCollapsible(False)`——报文刷满一屏不许把点表压没；
* 点位表的竖直滚动条常驻：它一收一排，视口就变宽、最后一列跟着重排一次；
* 树的列宽、通道面板的每一格、报文面板的每个标签都是定宽槽；
* 状态变化只改文字与颜色：不 addWidget、不 setVisible、不换控件；
* 状态栏摘要是定宽槽，字数装不下时**改文案**（换成计数式摘要），不加宽也不截断；
* 网络页（`net_panel.py`）只在被选中时才读系统：那几次 PowerShell 是秒级，"打开点表"
  不许跟着等，页面文字也不许在状态变化时改高度。

界面不许演出现场没发生的事：改值被拒那一格保持原值并把原因落到日志与状态栏；
等引擎回话超时带出引擎当时的栈（状态栏只给第一行，多行进日志窗，不给状态栏撑高度）。
"""

from __future__ import annotations

import datetime as dt
import gc
import pathlib
import time

from PySide6 import QtGui, QtCore, QtWidgets

from .. import diagnostics
from ..engine import ports
from ..engine.point_source import session_source
from ..project import edit, formats
from ..project.csv_io import port_from_name
from ..project.fsio import write_text_atomic
from ..project.model import channel_transport
from ..project.validate import Issue, error_lines, validate_project
from . import theme
from .bridge import EngineBridge
from .channel_panel import ChannelPanel
from .device_tree import DeviceTree
from .dialogs import (
    IssueDialog, ProjectFileError, choose_offset, export_table_path, format_issues,
    import_table, open_project, read_point_table, save_project_as, save_project_to,
)
from .new_project import AddChannelDialog, AddDeviceDialog, NewProjectDialog
from .net_panel import NetPanel
from .packet_view import PacketPanel
from .point_props import STRUCTURAL_FIELDS, PointPropertiesDialog
from .point_table import COLUMNS, PointTableModel, PointTableView

_TICK_MS = 100
_NOTICE_MS = 8000                       # 拒绝理由要在状态栏留够一读的时间，别一闪就没
# 界面上每点一次导入/换工程都会留下一批已经没人引用的 widget；引用环得破，但**只能在
# 这条线程上破**（口径见 spec §13：引擎线程上触发的一次自动回收如果碰到界面刚丢弃的
# QWidget 包装器，shiboken 会在非 GUI 线程上 `~QWidget → QWindow::close()`，而它等的主
# 线程正卡在 `runner.call` 上等引擎回话——互等成环，整个软件冻死）。
_GC_EVERY_S = 5.0
_STATUS_SLOT_WIDTH = 320                # 状态摘要的定宽槽（实测最长两条通道 ≈ 200px）
_LOG_LINES = 5000                       # 日志窗只留最近多少行（与报文窗的 5000 帧同一量级）
# "这个端口现在空着吗"的探针（新建工程 / 添加通道算默认端口时用）。放成模块级常量是为了
# **用例能换掉它**：不换的话，"新建一个 TCP 工程得到 tcp-5020"这类断言就取决于跑测试的
# 机器上 5020 空不空——那种红最难查。
PORT_PROBE = ports.is_free
_TREE_STRETCH, _TABLE_STRETCH = 1, 4
_FIRST_SPLIT = [600, 400]   # 上栏:下栏 首屏比例（见 _build_layout）
_STATUS_WORD = {"stopped": "停止", "starting": "启动中", "running": "运行",
                "error": "失败"}
# 计数式摘要里状态的固定顺序：不按下标排，同一份现况每次渲染才是同一个字符串
_STATUS_ORDER = ("running", "stopped", "error", "starting")


class MainWindow(QtWidgets.QMainWindow):
    """组装层：只做接线与呈现，判定全部留在 engine/project 那一份里。"""

    def __init__(self, session, store, project=None, log_file=None) -> None:
        """`log_file` 是这次的**证据落点**（`simbench/diagnostics.py` 那份会话日志）。

        默认 `None` = 不落盘：用例构造窗口时不该往用户的 `%LOCALAPPDATA%` 里写文件；
        真实入口（`gui/app.py::build_app`）会装好崩溃钩子并把同一份 `LogFile` 递进来。
        界面上"另存日志"那个按钮两条路都能用（它导出的是**窗里**的文本）。
        """
        super().__init__()
        # 主题只落这一次（构造期）：运行期再动 setStyleSheet 会让 Qt 重新 polish，
        # sizeHint 有机会跟着变——那正是"状态一变布局就跳"的另一种写法（见 theme 模块）。
        theme.apply_theme()
        self.setWindowIcon(theme.app_icon())
        self.session = session
        self.store = store
        self.log_file = log_file
        self.bridge = EngineBridge()
        self.point_model = PointTableModel()
        self.tree = DeviceTree()
        self.point_table = self._build_table()
        self.packet_panel = PacketPanel(store)
        self.log_view = self._build_log_view()
        self.log_tab = self._build_log_tab(self.log_view)
        self.channels = ChannelPanel(session)
        self.net_panel = NetPanel(session)
        self.tabs: QtWidgets.QTabWidget
        self.splitter: QtWidgets.QSplitter
        self.status: QtWidgets.QStatusBar
        # 工具栏图标名（动作 -> 图标名）：切主题时底色变了要按新模式重画，登记在这里
        self._icon_names: list[tuple[QtGui.QAction, str]] = []

        self._path: pathlib.Path | None = None
        self._states: dict[str, str] = {}
        self._dirty_shown = False
        self._shown_error = ""
        self._confirming = False
        self._next_collect = time.monotonic() + _GC_EVERY_S

        self.setCentralWidget(self._build_layout())
        self._build_toolbar()
        self._build_menu()
        self._build_status_bar()

        self.tabs.addTab(self.packet_panel, "报文监视")
        self.tabs.addTab(self.log_tab, "日志")
        self.tabs.addTab(self.net_panel, "网络")
        # 切到网络页才去读系统：读一次网卡现状是几次 PowerShell（秒级），不能让
        # "打开点表"这一步跟着等——点表一开就调，现场会以为软件卡住了。
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tree.currentItemChanged.connect(self._on_tree_selection)

        if project is not None:
            self.use_project(project)

        self.net_panel.notice.connect(
            lambda text: self.status.showMessage(text, _NOTICE_MS))
        self.bridge.channel_state.connect(self._on_state)
        self.bridge.logged.connect(self._on_log)
        self.bridge.point_changed.connect(lambda _event: self.point_model.refresh())
        self.point_table.restore_requested.connect(self._restore_waveform)
        self.point_table.props_requested.connect(self._show_point_props)
        self.point_table.add_point_requested.connect(self._add_point)
        self.point_table.remove_point_requested.connect(self._remove_point)
        self.tree.add_channel_requested.connect(self._add_channel)
        self.tree.add_device_requested.connect(self._add_device)
        self.tree.add_point_requested.connect(self._add_point_to_device)
        self.tree.remove_channel_requested.connect(self._remove_channel)
        self.tree.remove_device_requested.connect(self._remove_device)
        self.session.subscribe(self.bridge.forward)

        self._fit_to_screen()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(_TICK_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.status_label.setText(self._summary())

    # ---------- 组装 ----------

    def _build_table(self) -> PointTableView:
        table = PointTableView()
        table.setModel(self.point_model)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setWordWrap(False)
        table.setAlternatingRowColors(True)      # 十列宽的表格，隔行底色才不容易串行
        # 竖直滚动条常驻：AsNeeded 在"这个通道的点少到一屏装得下"时会收起整条，视口跟着
        # 变宽、stretchLastSection 重排一次列宽——操作员正在看的那一列就挪了（口径 3）。
        table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        for index, spec in enumerate(COLUMNS):
            table.setColumnWidth(index, spec.width)
        return table

    def _build_log_view(self) -> QtWidgets.QPlainTextEdit:
        view = QtWidgets.QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        # 只留最近 5000 行（与报文窗那个环形缓冲同一个量级）：编辑、状态变化、运行期报错
        # 都往这里写，跑一晚上的会话是几万行——那是"跑得越久越占内存"的典型形状，
        # 而且它一条也没用了（几十小时前的报错没人回头看）。
        view.setMaximumBlockCount(_LOG_LINES)
        view.setToolTip(f"只保留最近 {_LOG_LINES} 行")
        # 等宽字体：异常里带出来的引擎栈与"[refused] name=..."这类行按列对着读才不费眼
        view.setFont(theme.mono_font())
        return view

    def _build_log_tab(self, view: QtWidgets.QPlainTextEdit) -> QtWidgets.QWidget:
        """日志页：只读框 + "另存日志" + "清空"。

        "清空"为什么要有：报文页有"清除"，日志页没有——而只读框里**没有任何办法自己清掉**
        （光标选不中内容、`delete` 没用）。5 万行上限只保证它不会无限涨，不保证"现在这一屏
        我不想要了"能擦掉。

        "另存日志"为什么要有（2026-10-14 深度审查）：日志只在窗里，关窗即失；而现场报问题
        时最需要的就是"把刚才那段发我"。它导出的是**窗里**的文本（含 `_log` 收到的一切），
        与 `%LOCALAPPDATA%\\simbench\\logs` 里那份会话日志互为备份（后者连崩溃都在）。
        两个按钮都是定宽槽：切状态只改文字，宽度不动（界面口径 4）。
        """
        host = QtWidgets.QWidget()
        rows = QtWidgets.QVBoxLayout(host)
        rows.setContentsMargins(4, 4, 4, 4)
        bar = QtWidgets.QHBoxLayout()
        bar.addStretch(1)
        self.act_save_log = QtWidgets.QPushButton("另存日志")
        self.act_save_log.setFixedWidth(76)          # "另存日志" 四个中文字（实测 60px）+ 余量
        tip = "把日志窗里的内容另存成一份文本文件"
        if self.log_file is not None:
            tip += f"；本次会话的日志也在 {self.log_file.path}"
        self.act_save_log.setToolTip(tip)
        self.act_save_log.clicked.connect(self._save_log)
        bar.addWidget(self.act_save_log)
        self.act_clear_log = QtWidgets.QPushButton("清空")
        self.act_clear_log.setFixedWidth(64)
        self.act_clear_log.setToolTip("清掉日志窗里的内容（只影响这一屏；会话日志文件不受影响）")
        self.act_clear_log.clicked.connect(view.clear)
        bar.addWidget(self.act_clear_log)
        rows.addLayout(bar)
        rows.addWidget(view, 1)
        return host

    def _build_layout(self) -> QtWidgets.QWidget:
        host = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(host)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setObjectName("mainSplitter")

        top = QtWidgets.QWidget()
        rows = QtWidgets.QVBoxLayout(top)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.addWidget(self.channels)             # 第一行：通道属性 + 独立启停
        cols = QtWidgets.QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.addWidget(self.tree, _TREE_STRETCH)
        cols.addWidget(self.point_table, _TABLE_STRETCH)
        rows.addLayout(cols, 1)

        bottom = QtWidgets.QWidget()
        bottom_rows = QtWidgets.QVBoxLayout(bottom)
        bottom_rows.setContentsMargins(0, 0, 0, 0)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("bottomTabs")
        bottom_rows.addWidget(self.tabs)

        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        # 两段都不许压没：上面被压没是"点表看不见了"，下面被压没是"报文刷不出来"，
        # 两种都是操作员只能重启模拟器的那种症状。
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        # 首屏就按 6:4 分：只写 stretchFactor 的话，第一次布局是按两个 pane 的 sizeHint
        # 抢空间的，报文那一栏会被压到只剩几十像素（实测表格高 72），操作员看不到任何一帧。
        # 比例钉死之后，窗口 resize 时按同一个比例缩放，三态之间也就不会挪。
        splitter.setSizes(_FIRST_SPLIT)
        self.splitter = splitter
        grid.addWidget(splitter)
        return host

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("main")
        bar.setObjectName("mainToolBar")
        bar.setMovable(False)                     # 可拖走的工具栏 = 布局会自己挪
        # 图标与文字并排：只留图标的话，现场得靠悬停猜哪个是"全部停止"，
        # 而这一排按钮里"停止"点错的代价是掉一路正在服务的从站。
        bar.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        bar.setIconSize(theme.icon_size())
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
        action = bar.addAction(theme.icon(icon), text, slot)
        action.setToolTip(text)
        self._icon_names.append((action, icon))
        return action

    def _fit_to_screen(self) -> None:
        """起手尺寸 = **当前屏幕可用区域的一半**，并居中。

        为什么不按布局的 `sizeHint` 起手：那个尺寸是"控件刚好装得下"（本机实测 1058×317），
        又矮又宽，看着像一条带子；整屏铺满在小屏/多显示器上又太霸道。取一半是现场最省事的
        一档——左右还能摆一个主站工具或浏览器。

        两个细节：屏幕用 `self.screen()`（还没 show 时是主屏）的**可用区域**（去掉任务栏），
        按整屏算的话窗口会被任务栏盖掉一条边；装不下的窄屏交给 Qt——它按布局的最小尺寸
        （`minimumSizeHint`）兜底，不会把控件压没，也不会让"一半"变成 0。
        """
        screen = self.screen()
        if screen is None:                          # 极端情况下没有屏幕信息：不猜尺寸
            return
        area = screen.availableGeometry()
        hint = self.minimumSizeHint()
        self.resize(max(area.width() // 2, hint.width()),
                    max(area.height() // 2, hint.height()))
        frame = self.frameGeometry()
        frame.moveCenter(area.center())
        self.move(frame.topLeft())

    def _build_menu(self) -> None:
        """"文件"菜单：工具栏那排按钮的家谱，也是"另存为"与快捷键的家。

        为什么要它：工具栏已经 9 个动作（每个都要配图标与定宽槽的断言），而"保存"与
        "另存为"是**两件事**——塞进菜单既给得出快捷键，也不用再加两个按钮。
        菜单项与工具栏按钮是**同一批 QAction 对象**（行为只有一份，不会两处漂移）。
        """
        menu = self.menuBar().addMenu("文件")
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

    def _build_status_bar(self) -> None:
        self.status = self.statusBar()
        self.status_label = QtWidgets.QLabel("未启动")
        # 定宽槽（口径 4）：摘要文字随通道数增长，但状态栏的度量必须恒定。
        self.status_label.setFixedWidth(_STATUS_SLOT_WIDTH)
        self.status_label.setToolTip("各通道当前状态")
        self.status.addPermanentWidget(self.status_label)

    # ---------- 工程 ----------

    def use_project(self, project, path=None, notes=(), announce=True) -> list:
        """换一份工程并把五个视图一起对上。返回校验清单（有 error 也照样装）。

        `announce=False` 给**改动**那条路用（新建/增删之后刷新视图）：那一步本来就不该
        用模态清单打断连续动作，但清单仍要逐条进日志——带着旧错的工程改完还带着那些错，
        不写出来就等于假装干净了。
        """
        # 换工程（新建 / 打开 / 导入）会把在跑的通道一起停掉——这是**故意的**（用户要换的
        # 是整份工程，跟"为一个编辑停掉正在服务的另一路"不是一回事）。但不能静默：树上的
        # ● 变 ○ 太容易被忽略，日志里必须有一行说清楚是谁被停了。
        stopped = [name for name in self.session.channel_names()
                   if (runtime := self.session.channel(name)) is not None
                   and runtime.running]
        issues = self.session.load(project, source=path)
        for name in stopped:
            self._log(f"[stopped] {name}：换工程时停掉了这一路正在运行的服务")
        names = self.session.channel_names()
        self.tree.rebuild(project)
        if names:
            self.point_model.attach(session_source(self.session))
        else:
            # 一条通道都没有：表必须是**空的**，不许留着上一份的行（那是在演一份不存在的点表）
            self.point_model.detach()
        # 空点表不能悄悄给一张"看着合法的空白表"：CLI 那句在这里说同一遍
        self.channels.rebuild(project)
        self.store.clear()
        self._path = pathlib.Path(path) if path else None
        self._states = {name: "stopped" for name in names}
        self._dirty_shown = self.session.dirty
        self._shown_error = ""
        for note in notes:
            self._log(f"[note] {note}")
        self._refresh_title()
        self.status_label.setText(self._summary())
        if announce:
            self._show_issues(issues)
        else:
            for line in format_issues(issues):
                self._log(f"[issue] {line}")
        if self.tabs.currentWidget() is self.net_panel:
            # 正好停在网络页时换工程：那一页的字是上一份声明的，得跟着换
            self.net_panel.refresh()
        return issues

    # ---------- 工程结构：新建 / 增删（口径见计划 §3） ----------

    def _new_project(self) -> None:
        """工具栏"新建工程"：空白起步，或 TCP / 串口 / S7 骨架起步。"""
        if not self._confirm_discard():
            return
        dialog = NewProjectDialog(self, probe=PORT_PROBE)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.use_project(dialog.project, path=None, announce=False)
        if dialog.project.channels:
            self._log(f"[edit] 已新建工程（{len(dialog.project.channels)} 条通道）")
            # 有内容才置脏：一份空工程在关窗时拦一下问"要不要保存"是在为空气操心
            self._mark_dirty()
        else:
            self._log("[edit] 已新建空工程：在设备树上右键『添加通道』")
            self.status.showMessage("空工程：设备树上右键『添加通道』", _NOTICE_MS)

    def _add_channel(self) -> None:
        """加一路通道。**在跑的别的通道不受影响**（`Session.add_channel` 只动一个键）。

        对话框里那一步已经 `edit.add_channel`（带校验与回滚）；这里只让会话跟上，
        再把视图重画——不走 `load()`，所以不需要"先全部停止"。
        """
        dialog = AddChannelDialog(self, self.session.project, probe=PORT_PROBE)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        if not self._edit_call("添加通道", lambda: self.session.add_channel(dialog.channel)):
            return
        self._after_edit(f"已添加通道 {dialog.channel.name}", keep=dialog.channel.name)

    def _add_device(self, channel: str) -> None:
        if self._guard_running([channel]):
            return
        try:
            dialog = AddDeviceDialog(self, self.session.project, channel)
        except KeyError:
            self._log(f"[refused] 添加设备：{channel} 已经不在这份工程里了")
            return
        except edit.EditRefused as exc:
            # 构造期就会算默认值（名字与 unit），算不出来时它抛的是这个（unit 用满 247 个）
            self._show_refused("添加设备", exc.reasons)
            return
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self._after_edit(f"已添加设备 {dialog.device.name}"
                         f"（unit {dialog.device.unit_id}）", reindex=channel)

    def _add_point(self, key: tuple) -> None:
        """点位表右键"添加点位…"：落在**右键那一行所属的设备**上。"""
        channel, unit_id, _area, _address, _bit = key
        self._create_point(channel, unit_id)

    def _add_point_to_device(self, channel: str, unit_id: int) -> None:
        """设备树右键"添加点位…"：空设备在表上没有行，这条是它唯一的加点入口。"""
        self._create_point(channel, unit_id)

    def _create_point(self, channel: str, unit_id: int) -> None:
        """加一个点位：按默认值落地，**然后立刻打开属性面板**让它改名/改类型。

        为什么不先弹一个"新建点位"表单：那 10 个字段的表单 `point_props` 已经有一份
        （带类型联动、定宽槽、一组几何断言）。抄第二份必然漂移，漂了的表现是"新建时
        能选的组合，打开属性面板就报错"。
        """
        if self._guard_running([channel]):
            return
        try:
            transport = channel_transport(self.session.project.find_channel(channel))
            device = edit.find_device(self.session.project, channel, unit_id)
            point = edit.default_point(device, edit.default_area(transport), transport)
            edit.add_point(self.session.project, channel, unit_id, point)
        except edit.EditRefused as exc:
            self._show_refused("添加点位", exc.reasons)
            return
        except KeyError:
            self._log(f"[refused] 添加点位：{channel} / unit {unit_id} "
                      f"已经不在这份工程里了")
            return
        self._after_edit(f"已添加点位 {point.name}（{channel} unit {unit_id}）",
                         reindex=channel)
        self._show_point_props((channel, unit_id, point.area, point.address, point.bit))

    def _remove_channel(self, channel: str) -> None:
        """删一路通道：**只停它自己**（别的通道照旧服务），先问一次再动手。"""
        try:
            target = self.session.project.find_channel(channel)
        except KeyError:
            self._log(f"[refused] 删除通道：{channel} 已经不在这份工程里了")
            return
        points = sum(len(device.points) for device in target.devices)
        running = self.session.channel(channel) is not None \
            and self.session.channel(channel).running
        note = "\n\n这一路正在运行，删除会先把它停掉（其它通道不受影响）。" if running else ""
        if not self._confirm(f"删除通道 {channel}？",
                             f"连同 {len(target.devices)} 个设备、{points} 个点位一起删除，"
                             f"删掉就回不来了。{note}"):
            return
        if not self._edit_call("删除通道",
                               lambda: edit.remove_channel(self.session.project, channel)):
            return
        if not self._edit_call("删除通道",
                              lambda: self.session.remove_channel(channel)):
            return
        self._after_edit(f"已删除通道 {channel}")

    def _remove_device(self, channel: str, unit_id: int) -> None:
        if self._guard_running([channel]):
            return
        try:
            device = edit.find_device(self.session.project, channel, unit_id)
        except KeyError:
            self._log(f"[refused] 删除设备：{channel} / unit {unit_id} "
                      f"已经不在这份工程里了")
            return
        if not self._confirm(f"删除设备 {device.name}？",
                             f"连同它的 {len(device.points)} 个点位一起删除，"
                             f"删掉就回不来了。"):
            return
        if self._edit_call("删除设备",
                           lambda: edit.remove_device(self.session.project, channel,
                                                      unit_id)):
            self._after_edit(f"已删除设备 {device.name}（{channel} unit {unit_id}）",
                             reindex=channel)

    def _remove_point(self, key: tuple) -> None:
        channel, unit_id, area, address, bit = key
        if self._guard_running([channel]):
            return
        try:
            device = edit.find_device(self.session.project, channel, unit_id)
        except KeyError:
            self._log(f"[refused] 删除点位：{channel} / unit {unit_id} "
                      f"已经不在这份工程里了")
            return
        try:
            point = edit.find_point(device, area, address, bit)
        except edit.EditRefused as exc:
            self._show_refused("删除点位", exc.reasons)
            return
        except KeyError:
            self._log(f"[refused] 删除点位：{channel} / {area} {address} "
                      f"已经不在这份工程里了")
            return
        if not self._confirm(f"删除点位 {point.name}？",
                             f"{channel} / unit {unit_id} / {area} {address}，"
                             f"删掉就回不来了。"):
            return
        if self._edit_call(f"删除点位 {point.name}",
                           lambda: edit.remove_point(self.session.project, channel,
                                                     unit_id, area, address, bit)):
            self._after_edit(f"已删除点位 {point.name}（{channel} unit {unit_id}）",
                             reindex=channel)

    # ---------- 结构性改动共用的几步 ----------

    def _guard_running(self, channel_names) -> bool:
        """要动的通道里只要有一条在跑，就整体拒绝并点名（口径 6）。

        为什么不"先停再改"：加/删通道要走 `Session.load()`，而它第一步就是
        `_stop_all_locked()`——为一个已停通道的编辑把正在给网关服务的那一路停掉，是新的
        现场事故（M4 对"改点位地址"已经因为同一个理由拒绝过一次）。加/删设备点位走的
        `reindex()` 也在文档里写死了：不许在那一路上跑着的时候调用。
        """
        running = [name for name in channel_names
                   if (runtime := self.session.channel(name)) is not None
                   and runtime.running]
        if not running:
            return False
        names = "、".join(running)
        self._log(f"[refused] {names} 正在运行；改点表要先停止（只影响这一路）")
        self.status.showMessage(f"{names} 正在运行，先停止再改点表", _NOTICE_MS)
        return True

    def _confirm(self, title: str, text: str) -> bool:
        """删除这种不可撤销的动作先问一次，默认按钮是"否"。"""
        answer = QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def _edit_call(self, what: str, action) -> bool:
        """跑一次结构性改动：被拒就点名进日志与状态栏，成功回 True（调用方再去刷视图）。

        两种都是"这件事做不成"而不是 bug：`EditRefused` 带校验原话（或分配器的理由），
        `KeyError` 是"目标已经不在了"（换工程那一瞬间）。两种都不许炸在 Qt 槽里。
        """
        try:
            action()
        except edit.EditRefused as exc:
            self._show_refused(what, exc.reasons)
            return False
        except (KeyError, ValueError) as exc:
            # KeyError：目标已经不在这份工程里（换工程那一瞬间）。
            # ValueError：会话拒绝了这次对账（`reindex` 不许动在跑的那一路、
            # `add_channel`/`remove_channel` 要求工程先改对）——那也是一句人话。
            self._log(f"[refused] {what}：{exc}")
            self.status.showMessage(f"{what}被拒：{_one_line(str(exc))}", _NOTICE_MS)
            return False
        return True

    def _show_refused(self, what: str, reasons) -> None:
        """拒绝理由一条都不许只留在返回值里：日志各一条，状态栏给第一句。"""
        for line in reasons:
            self._log(f"[refused] {what}: {line}")
        if reasons:
            self.status.showMessage(f"{what}被拒：{_one_line(str(reasons[0]))}", _NOTICE_MS)

    def _after_edit(self, message: str, keep: str | None = None,
                    reindex: str | None = None) -> None:
        """结构性改动落地之后：**让会话跟上**（只动该动的那一份），再把五个视图重画。

        `reindex` 给"改动落在哪一路"（加/删设备与点位）：`Session.reindex` 只重建读写索引，
        跑着的**兄弟**通道照旧服务。加/删通道由调用方自己做（`add_channel` /
        `remove_channel`，它们只动一个键），所以那种情况不传 `reindex`。

        为什么不再走 `use_project`（它内部是 `Session.load`）：那条路第一步停掉**所有**在跑
        的通道——加一个点位就把正在给网关服务的那一路停掉，是新的现场事故（这正是"在跑的
        时候不许增删"那条限制的来源）。现在只动该动的那一份。

        选中的通道也要保住：往第二条通道里加点位，表不该自己跳回第一条。
        """
        if reindex is not None:
            self.session.reindex(reindex)
        self._refresh_views(keep=keep)
        self._mark_dirty()
        self._log(f"[edit] {message}")

    def _refresh_views(self, keep: str | None = None) -> None:
        """按**当前**工程重画五个视图：不停通道、不清报文、不碰 dirty。

        与 `use_project` 的分工：那个是"换了一份工程"（停所有通道、清报文、路径/脏标记跟着
        走），这个是"同一份工程被改了"——在跑的那几路必须连状态一起保住（树上还是 ●），
        报文窗里已经收到的帧也不该被清掉。
        """
        project = self.session.project
        names = self.session.channel_names()
        states = {name: self._states.get(name, "stopped") for name in names}
        current = keep or self.point_model.channel()
        self.tree.rebuild(project)
        if names:
            self.point_model.attach(session_source(self.session), current)
        else:
            self.point_model.detach()
        self.channels.rebuild(project)
        self._states = states
        for name, state in states.items():
            # rebuild 之后控件是新的：状态要重新贴一遍，否则在跑的那一路会显示成"停止"
            self.tree.set_status(name, state)
            self.channels.set_status(name, state)
        self._shown_error = ""
        self._refresh_title()
        self.status_label.setText(self._summary())
        for line in format_issues(self.session.issues()):
            self._log(f"[issue] {line}")
        target = current if current in names else (names[0] if names else "")
        if target:
            self._select_channel(target)

    def _select_channel(self, channel: str) -> None:
        """把树的选中项挪到某个通道（表跟着树走）。名字不在工程里就什么都不做。"""
        item = self.tree.item_for(channel)
        if item is not None:
            self.tree.setCurrentItem(item)

    def _on_tab_changed(self, index: int) -> None:
        """网络页被选中时重新对一次账（只读）。别的时候一个系统调用都不发。"""
        if self.tabs.widget(index) is self.net_panel:
            self.net_panel.refresh()

    def _mark_dirty(self) -> None:
        """置脏并**立刻**把标题上的 `*` 打上。

        只调 `session.mark_dirty()` 的话，`*` 要等下一次心跳（最多 100ms）才出现——
        "刚加完一个点位，界面上什么反应都没有"是真的会让人再点一次。
        """
        self.session.mark_dirty()
        self._dirty_shown = True
        self._refresh_title()

    def _refresh_title(self) -> None:
        name = self._path.name if self._path else "未命名工程"
        star = " *" if self.session.dirty else ""
        self.setWindowTitle(f"设备模拟台 - {name}{star}")

    # ---------- 动作 ----------

    def _start_all(self) -> None:
        try:
            failures = self.session.start_all()
        except Exception as exc:                    # noqa: BLE001
            # 槽函数里抛出去等于在 Qt 事件循环里炸，表现成"点了没反应"。这里不吞：
            # 异常类名和消息整段进日志，状态栏给一句能读的。
            self._log(f"[error] 全部启动异常：{type(exc).__name__}: {exc}")
            self.status.showMessage("全部启动异常，原因见日志页", _NOTICE_MS)
            return
        for message in failures:
            # 只在树上变个红符号是不够的：操作员要看见"为什么"，一次报全和 CLI 同口径
            self._log(f"[error] {message}")
            self.status.showMessage(_one_line(message), _NOTICE_MS)

    def _stop_all(self) -> None:
        try:
            failures = self.session.stop_all()
        except Exception as exc:                    # noqa: BLE001
            self._log(f"[error] 全部停止异常：{type(exc).__name__}: {exc}")
            self.status.showMessage("全部停止异常，原因见日志页", _NOTICE_MS)
            return
        for message in failures:
            # 关不掉的通道要说得出名字和原因：只把那一格标红，操作员看不出端口还占着
            self._log(f"[error] {message}")
            self.status.showMessage(_one_line(message), _NOTICE_MS)

    def _check(self) -> None:
        # 已定口径：这里不许写 `self.session.project and validate(...)` 那种无意义表达式
        self._show_issues(validate_project(self.session.project))

    def _show_issues(self, issues) -> bool:
        errors = error_lines(issues)
        if not issues:
            self.status.showMessage("点表检查通过", _NOTICE_MS)
            return True
        for line in format_issues(issues):
            self._log(f"[issue] {line}")        # 对话框关掉后日志里还得留一份
        if errors:
            self.status.showMessage(f"{len(errors)} 个错误，启动已被阻止")
        else:
            self.status.showMessage(f"{len(issues)} 个警告")
        IssueDialog(issues, self).exec()
        return False

    def _import_table(self) -> None:
        if not self._confirm_discard():
            return
        path = import_table(self)
        if not path:
            return
        offset = choose_offset(self)
        try:
            project, notes = read_point_table(path, offset)
        except ProjectFileError as exc:
            self._show_issues([_parse_issue(exc.path, exc.detail)])
            return
        self.use_project(project, path, notes=notes)

    def _open_project(self) -> None:
        if not self._confirm_discard():
            return
        try:
            project, path = open_project(self)
        except ProjectFileError as exc:
            self._show_issues([_parse_issue(exc.path, exc.detail)])
            return
        if project is None:
            return                                 # 用户取消：什么都没发生
        self.use_project(project, path)

    def _save_project(self, ask: bool = False) -> str | None:
        """保存。`ask=True` 是"另存为"（总要问路径）；否则**有路径就直接写回**。

        原来无条件走 `save_project_as`：已经存过、路径都知道了，再点一次"保存"还是弹
        文件名对话框——一天存十次就问十次，人开始点错（`xx (2).json` 就是这么来的）。
        没有路径时才问（新建的工程第一次保存）。
        """
        try:
            if self._path is not None and not ask:
                path = str(save_project_to(self.session.project, self._path))
                self.status.showMessage(f"已保存 {self._path.name}", _NOTICE_MS)
            else:
                path = save_project_as(self, self.session.project,
                                       str(self._path) if self._path else "")
        except ProjectFileError as exc:
            self._log(f"[error] 保存失败：{exc}")
            self.status.showMessage("保存失败，原因见日志页", _NOTICE_MS)
            return None
        if path:
            self._path = pathlib.Path(path)
            self.session.clear_dirty()             # 落盘成功了才配清 `*`
        self._refresh_title()
        return path

    def _export_table(self) -> None:
        """导出一份**点表**给现场：写 CSV 还是写 Excel 由落盘路径的后缀说了算。

        **每次都要问一次落哪**（默认名 = 工程旁边那份 `.csv`，回车即就地落一份 CSV）。
        为什么不学"保存"那样有路径就直接写：导出对话框给的是**两种**格式，不问就等于
        "这个按钮只能出 CSV"——要 Excel 只能绕道"另存为"，而那一动作会把 Ctrl+S 的落点
        也换成点表文件（工程里的 `tick_ms` 那份设定就丢了）。导出是明确要一份**给别人的
        文件**，问一次路径才配得上"选格式"这件事；也顺带修掉"选了 Excel 却写 CSV 字节"
        （Excel 打开会报"文件格式不对"）。
        """
        suggested = str(self._path.with_suffix(".csv")) if self._path is not None else ""
        chosen = export_table_path(self, suggested)
        if not chosen:
            return
        target = pathlib.Path(chosen)
        try:
            formats.write_project(self.session.project, target)
        except (OSError, ValueError) as exc:
            # ValueError 是"这份工程点表表示不了"（一个点位都没有）：那也是一句人话，
            # 不是 traceback——从 Qt 槽里逃出去的表现是"点了没反应"。
            self._log(f"[error] 导出点表失败：{exc}")
            self.status.showMessage("导出点表失败，原因见日志页", _NOTICE_MS)
            return
        self._log(f"[info] 已导出点表 -> {target}")
        for channel in self.session.project.channels:
            # 点表没有端口列：TCP 的端口是靠**通道名尾数**带过去的（`tcp-5020`）。
            # 名字与端口对不上时，这份点表导回来会读到另一个端口——导入那边"猜了要说"，
            # 导出这边也得说，否则就是一条静默变味的路（CSV 与 Excel 都是这条规则）。
            if channel_transport(channel) == "tcp" \
                    and port_from_name(channel.name) != channel.port:
                self._log(f"[note] {channel.name}: 端口 {channel.port} 不在名字里；"
                          f"这份点表按名字读回来会是 {port_from_name(channel.name)}"
                          f"（把名字改成 {channel.name}-{channel.port} 之类再导出）")

    def _restore_waveform(self, key: tuple) -> None:
        """右键"恢复波形"：把这一行的点交还给波形，再报还剩几处在人工接管。

        交还本身由 `Session.restore_waveform` 经 `runner.call` 投回 loop 线程，两条拒绝
        都已经是带点位名的 `LogLine`（桥会写进日志页）——这里不再抄第二套拒绝文案。
        计数只在**真还回去过东西**之后说：没恢复成还说"还剩 N 处"，那一行明明仍 `已停`，
        状态栏就成了说谎的那个人。
        """
        channel, unit_id, area, address, bit = key
        if not self.session.restore_waveform(channel, unit_id, area, address, bit):
            return
        remaining = self.session.manual_count(channel)
        if remaining is None:
            # 引擎这一跳没回话（`manual_count` 明说"不知道"）：不许拿 0 顶替，
            # 那会变成"已全部交还"——一句很可能不成立的话。
            message = "已交还波形（接管计数这一跳没读到，下一跳再看）"
        elif remaining:
            message = f"已交还波形，人工接管还剩 {remaining} 处"
        else:
            message = "已交还波形，人工接管已全部交还"
        self.status.showMessage(message, _NOTICE_MS)

    def _show_point_props(self, key: tuple) -> None:
        """右键"属性…"：逐点编辑点表锁死的字段，成功后把"什么时候生效"说清楚。

        装载口径按 Task 6 裁决，不是"apply → `session.load`"：`load()` 会停掉**所有**
        通道，为一个已停通道的小编辑把正在服务网关的那一路停了就是新的现场事故。停通道
        检查、`load`/`reindex` 的分叉都住在 `point_props.apply()` 里（那里有拒绝理由），
        主窗只做事后呈现：表立刻刷一次 + 状态栏一句生效时机。措辞分三档：纯结构性改动
        只提重启；纯 live 改动只提下一拍（骗操作员去重启在跑的通道比不说更糟）；一次
        应用里两种都有就两句都说——只说一种就是另一半在撒谎。
        """
        try:
            dialog = PointPropertiesDialog(self, self.session.project, key)
        except KeyError as exc:
            # 菜单建好与打开面板之间行没了（换工程的瞬间）：槽函数不许炸，给一句人话。
            self._log(f"[refused] 打开点位属性失败：{exc}")
            self.status.showMessage("该行点位已不在当前点表中，属性面板未打开", _NOTICE_MS)
            return
        dialog.exec()
        changes = dialog.applied
        if changes is None:
            return                                   # 取消或整体被拒：什么都没发生
        self.point_model.refresh()
        count = len(changes)
        structural = set(changes) & set(STRUCTURAL_FIELDS)
        if structural and len(structural) == count:
            message = f"已改 {count} 处，重启通道后生效"
        elif structural:
            message = (f"已改 {count} 处：区/地址等重启通道后生效，"
                       f"名称/波形改动下一拍生效")
        else:
            message = f"已改 {count} 处，下一拍生效"
        self.status.showMessage(message, _NOTICE_MS)

    # ---------- 主题 ----------

    def _toggle_theme(self, _checked: bool = False) -> None:
        """浅色/深色切换。只换颜色——几何一个像素都不动（见 `test_gui_theme.py`）。

        切完必须把"按配色画出来的东西"重涂一遍：状态色走 QPalette（设备树、通道面板）、
        工具栏图标是已经画好的位图，这两类都不是换一张样式表就能刷掉的。
        """
        mode = theme.toggle()
        self.act_theme.setChecked(mode == "dark")
        for action, name in self._icon_names:
            action.setIcon(theme.icon(name))
        for channel, state in self._states.items():
            self.tree.set_status(channel, state)
            self.channels.set_status(channel, state)
        self.status.showMessage(
            "已切到深色主题" if mode == "dark" else "已切到浅色主题", _NOTICE_MS)

    # ---------- 心跳与事件 ----------

    def _tick(self) -> None:
        self.point_model.refresh()
        self.packet_panel.refresh_view()
        self._surface_edit_error()
        dirty = self.session.dirty
        if dirty != self._dirty_shown:            # 标题只在真变化时改，避免每跳重画标题
            self._dirty_shown = dirty
            self._refresh_title()
        self._maybe_collect()

    def _maybe_collect(self) -> None:
        """到点就在 GUI 线程上破一次引用环（`app.py` 把自动回收关了，这里就是唯一的回收口）。

        放在心跳里而不是另起一个计时器：心跳已经是 10Hz 且跑在主线程，再建一个 QTimer
        只会多一条能独立失效的路径。
        """
        now = time.monotonic()
        if now < self._next_collect:
            return
        self._next_collect = now + _GC_EVERY_S
        gc.collect()

    def _surface_edit_error(self) -> None:
        """把表格模型的拒绝理由搬到屏幕上：只有那一格变色等于没告诉操作员为什么。"""
        error = self.point_model.last_error
        if error == self._shown_error:
            return
        self._shown_error = error
        if not error:
            return
        self._log(f"[refused] {error}")
        self.status.showMessage(_one_line(error), _NOTICE_MS)

    def _on_state(self, state) -> None:
        self.tree.set_status(state.channel, state.state)
        self.channels.set_status(state.channel, state.state)
        if state.channel in self._states:
            # 未知通道（换工程那一瞬的事件）不许进摘要：那等于在列表里凭空多一台设备
            self._states[state.channel] = state.state
        self._log(f"[{state.state}] {state.channel} {state.detail}".rstrip())
        self.status_label.setText(self._summary())

    def _on_log(self, line) -> None:
        self._log(f"[{line.level}] {line.text}")

    def _on_tree_selection(self, current, _previous) -> None:
        channel = self.tree.channel_of(current)
        if not channel or channel == self.point_model.channel():
            return
        self.point_model.attach(session_source(self.session), channel)
        self._shown_error = ""

    def _log(self, text: str) -> None:
        """写一行到日志页，**同时**落进会话日志文件（装了才有）。

        两处一起写是刻意的：窗里那份给人看（还能"清空"、能另存），文件那份给"事后查"
        （崩溃钩子写的也是它）。只写窗里的话，关窗之后什么都没了。
        """
        self.log_view.appendPlainText(text)
        if self.log_file is not None:
            self.log_file.append(text)

    def _save_log(self) -> None:
        """把日志窗里的文本另存成一份文件（回执给现场"把这段发我"用）。"""
        suggested = f"simbench-log-{dt.date.today().isoformat()}.txt"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "另存日志", suggested, "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            # 不备份：这是**导出**一份给别人的文件，旁边再躺一个 .bak 只是噪音
            write_text_atomic(path, self.log_view.toPlainText(), backup=False)
        except OSError as exc:
            self._log(f"[error] 另存日志失败：{exc}")
            self.status.showMessage("另存日志失败，原因见日志页", _NOTICE_MS)
            return
        self._log(f"[info] 已导出日志 -> {path}")
        self.status.showMessage("日志已导出", _NOTICE_MS)

    def _summary(self) -> str:
        if not self._states:
            return "未启动"
        full = "；".join(f"{name}:{_STATUS_WORD[state]}"
                          for name, state in self._states.items())
        if self._fits(full):
            return full
        # 定宽槽装不下时改文案（口径 4）：换成计数式摘要，**不加宽、不截断成"…"**
        counts: dict[str, int] = {}
        for state in self._states.values():
            counts[state] = counts.get(state, 0) + 1
        return " ".join(f"{_STATUS_WORD[state]}{counts[state]}"
                        for state in _STATUS_ORDER if counts.get(state))

    def _fits(self, text: str) -> bool:
        return QtGui.QFontMetrics(self.status_label.font()).horizontalAdvance(
            text) <= self.status_label.width()

    # ---------- 关闭（口径 ε） ----------

    def _confirm_discard(self) -> bool:
        """有未保存的改动时问一次（保存 / 丢弃 / 取消）。回 False = **整个动作放弃**。

        四个"会把当前工程换掉/丢掉"的动作共用它：关窗、新建、打开、导入。原来只有关窗问，
        另外三个是实测确认过的缺陷——`use_project` → `Session.load` 会 `_dirty = False`，
        标题上的 `*` 就这么消失、关窗也不再问，改动静默丢。`_confirming` 是重入闸：
        `_save_project` 自己会弹另存为，不许在那条路上再进一次这个框。
        """
        if not self.session.dirty or self._confirming:
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "有未保存的改动",
            "点表有未保存的改动（标题栏有 *）。要先保存吗？",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save)
        if answer == QtWidgets.QMessageBox.Cancel:
            return False
        if answer == QtWidgets.QMessageBox.Discard:
            return True
        self._confirming = True
        try:
            path = self._save_project()
        finally:
            self._confirming = False
        # 另存为里按了取消：改动一片都没落盘，这时候放动作继续就是静默丢数据
        return path is not None

    def closeEvent(self, event) -> None:                # noqa: N802
        if not self._confirm_discard():
            event.ignore()
            return
        self.timer.stop()
        self.session.shutdown()
        if self.log_file is not None:
            # 收工前把缓冲刷下去：崩溃钩子写的是同一个文件，句柄留着也无妨，但关窗之后
            # 不该再有人往它里面写（这个进程的日志到此为止）。
            self.log_file.append(f"==== simbench closed "
                                 f"{dt.datetime.now().isoformat(timespec='seconds')} ====")
            self.log_file.close()
        event.accept()


def _one_line(text: str) -> str:
    """状态栏只给第一行：多行栈留在日志窗里，状态栏撑高就是布局挪动。"""
    return text.splitlines()[0] if text else ""


def _parse_issue(path, exc) -> Issue:
    """装载失败按 parse 类 Issue 报：和 `csv_io_entry` 同一个 code，两处说法一致。"""
    return Issue("error", "parse", pathlib.Path(path).name, str(exc))
