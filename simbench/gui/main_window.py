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

import gc
import pathlib
import time

from PySide6 import QtGui, QtCore, QtWidgets

from ..engine.point_source import session_source
from ..project.csv_io import write_csv_file
from ..project.validate import Issue, error_lines, validate_project
from . import theme
from .bridge import EngineBridge
from .channel_panel import ChannelPanel
from .device_tree import DeviceTree
from .dialogs import (
    IssueDialog, ProjectFileError, choose_offset, format_issues, import_csv,
    open_project, read_point_table, save_project_as,
)
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
_TREE_STRETCH, _TABLE_STRETCH = 1, 4
_FIRST_SPLIT = [600, 400]   # 上栏:下栏 首屏比例（见 _build_layout）
_STATUS_WORD = {"stopped": "停止", "starting": "启动中", "running": "运行",
                "error": "失败"}
# 计数式摘要里状态的固定顺序：不按下标排，同一份现况每次渲染才是同一个字符串
_STATUS_ORDER = ("running", "stopped", "error", "starting")


class MainWindow(QtWidgets.QMainWindow):
    """组装层：只做接线与呈现，判定全部留在 engine/project 那一份里。"""

    def __init__(self, session, store, project=None) -> None:
        super().__init__()
        # 主题只落这一次（构造期）：运行期再动 setStyleSheet 会让 Qt 重新 polish，
        # sizeHint 有机会跟着变——那正是"状态一变布局就跳"的另一种写法（见 theme 模块）。
        theme.apply_theme()
        self.setWindowIcon(theme.app_icon())
        self.session = session
        self.store = store
        self.bridge = EngineBridge()
        self.point_model = PointTableModel()
        self.tree = DeviceTree()
        self.point_table = self._build_table()
        self.packet_panel = PacketPanel(store)
        self.log_view = self._build_log_view()
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
        self._build_status_bar()

        self.tabs.addTab(self.packet_panel, "报文监视")
        self.tabs.addTab(self.log_view, "日志")
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
        self.session.subscribe(self.bridge.forward)

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
        # 等宽字体：异常里带出来的引擎栈与"[refused] name=..."这类行按列对着读才不费眼
        view.setFont(theme.mono_font())
        return view

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
        self.act_import = self._toolbar_action(bar, "in", "导入CSV", self._import_csv)
        self.act_export = self._toolbar_action(bar, "out", "导出CSV", self._export_csv)
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

    def _build_status_bar(self) -> None:
        self.status = self.statusBar()
        self.status_label = QtWidgets.QLabel("未启动")
        # 定宽槽（口径 4）：摘要文字随通道数增长，但状态栏的度量必须恒定。
        self.status_label.setFixedWidth(_STATUS_SLOT_WIDTH)
        self.status_label.setToolTip("各通道当前状态")
        self.status.addPermanentWidget(self.status_label)

    # ---------- 工程 ----------

    def use_project(self, project, path=None, notes=()) -> list:
        """换一份工程并把五个视图一起对上。返回校验清单（有 error 也照样装）。"""
        issues = self.session.load(project, source=path)
        names = self.session.channel_names()
        self.tree.rebuild(project)
        if names:
            self.point_model.attach(session_source(self.session))
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
        self._show_issues(issues)
        if self.tabs.currentWidget() is self.net_panel:
            # 正好停在网络页时换工程：那一页的字是上一份声明的，得跟着换
            self.net_panel.refresh()
        return issues

    def _on_tab_changed(self, index: int) -> None:
        """网络页被选中时重新对一次账（只读）。别的时候一个系统调用都不发。"""
        if self.tabs.widget(index) is self.net_panel:
            self.net_panel.refresh()

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

    def _import_csv(self) -> None:
        path = import_csv(self)
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
        try:
            project, path = open_project(self)
        except ProjectFileError as exc:
            self._show_issues([_parse_issue(exc.path, exc.detail)])
            return
        if project is None:
            return                                 # 用户取消：什么都没发生
        self.use_project(project, path)

    def _save_project(self) -> str | None:
        try:
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

    def _export_csv(self) -> None:
        if self._path is None:
            self._log("[error] 没有工程路径，请先打开或导入一份点表")
            self.status.showMessage("没有工程路径，导出已取消", _NOTICE_MS)
            return
        target = self._path.with_suffix(".csv")
        try:
            write_csv_file(self.session.project, target)
        except OSError as exc:
            self._log(f"[error] 导出 CSV 失败：{exc}")
            self.status.showMessage("导出 CSV 失败，原因见日志页", _NOTICE_MS)
            return
        self._log(f"[info] 已导出 CSV -> {target}")

    def _restore_waveform(self, key: tuple) -> None:
        """右键"恢复波形"：把这一行的点交还给波形，再报还剩几处在人工接管。

        交还本身由 `Session.restore_waveform` 经 `runner.call` 投回 loop 线程，两条拒绝
        都已经是带点位名的 `LogLine`（桥会写进日志页）——这里不再抄第二套拒绝文案。
        计数只在**真还回去过东西**之后说：没恢复成还说"还剩 N 处"，那一行明明仍 `已停`，
        状态栏就成了说谎的那个人。
        """
        channel, unit_id, area, address = key
        if not self.session.restore_waveform(channel, unit_id, area, address):
            return
        remaining = self.session.manual_count(channel)
        self.status.showMessage(
            f"已交还波形，人工接管还剩 {remaining} 处" if remaining
            else "已交还波形，人工接管已全部交还", _NOTICE_MS)

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
        self.log_view.appendPlainText(text)

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

    def closeEvent(self, event) -> None:                # noqa: N802
        if self.session.dirty and not self._confirming:
            answer = QtWidgets.QMessageBox.question(
                self, "有未保存的改动",
                "点表有未保存的改动（标题栏有 *）。确定要退出吗？改动不会写盘。",
                QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Save)
            if answer == QtWidgets.QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QtWidgets.QMessageBox.Save:
                self._confirming = True
                path = self._save_project()
                self._confirming = False
                if path is None:
                    # 另存为里按了取消：改动一片都没落盘，这时候放窗口走就是静默丢数据
                    event.ignore()
                    return
        self.timer.stop()
        self.session.shutdown()
        event.accept()


def _one_line(text: str) -> str:
    """状态栏只给第一行：多行栈留在日志窗里，状态栏撑高就是布局挪动。"""
    return text.splitlines()[0] if text else ""


def _parse_issue(path, exc) -> Issue:
    """装载失败按 parse 类 Issue 报：和 `csv_io_entry` 同一个 code，两处说法一致。"""
    return Issue("error", "parse", pathlib.Path(path).name, str(exc))
