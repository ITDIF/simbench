# simbench/gui/main_window.py
"""主窗：工具栏 + 上栏（通道面板 / 左树右表） + 下标签页（报文监视、日志、网络） + 状态栏。

刷新是**拉的**：10Hz 心跳调 `point_model.refresh()` 与 `packet_panel.refresh_view()`。
点位表的**取数**交给后台泵（`engine/rows_pump.py`）：心跳那一跳只把最近一份快照搬进模型，
不再等引擎回话；需要"此刻的真值"的地方（改完属性、用例）调 `refresh_rows_now()`。
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

from PySide6 import QtCore, QtGui, QtWidgets

from ..engine.point_source import pumped_source
from ..engine.rows_pump import RowsPump
from ..project.validate import error_lines, validate_project
from . import icons, messages, theme
from .bridge import EngineBridge
from .channel_panel import ChannelPanel
from .channel_state import STATUS_ORDER, STATUS_WORD
from .deps import WindowDeps
from .device_tree import DeviceTree
from .dialogs import format_issues
from .net_panel import NetPanel
from .packet_view import PacketPanel
from .point_table import COLUMNS, PointTableModel, PointTableView
from .verify_panel import VerifyPanel
from .window_common import _one_line, _parse_issue
from .window_consts import (
    _FIRST_SPLIT,
    _GC_EVERY_S,
    _LOG_LINES,
    _NOTICE_MS,
    _START_SIZE,
    _STATUS_SLOT_WIDTH,
    _TABLE_STRETCH,
    _TICK_MS,
    _TREE_STRETCH,
)
from .window_edit import EditMixin
from .window_io import IoMixin
from .window_menus import MenuMixin


class MainWindow(MenuMixin, EditMixin, IoMixin, QtWidgets.QMainWindow):
    """组装层：只做接线与呈现，判定全部留在 engine/project 那一份里。"""

    def __init__(self, session, store, project=None, log_file=None, settings=None,
                 deps: WindowDeps | None = None) -> None:
        """`log_file` 是这次的**证据落点**（`simbench/diagnostics.py` 那份会话日志）。

        默认 `None` = 不落盘：用例构造窗口时不该往用户的 `%LOCALAPPDATA%` 里写文件；
        真实入口（`gui/app.py::build_app`）会装好崩溃钩子并把同一份 `LogFile` 递进来。
        界面上"另存日志"那个按钮两条路都能用（它导出的是**窗里**的文本）。

        `settings`（`simbench/settings.py`）给了就**恢复并记住**窗口几何、主题、最近打开的
        工程；不给就什么都不做（用例的几何断言因此不会被用户机器上的设置影响）。

        `deps`（`gui/deps.py`）是往外界伸手的那几样——会弹框的对话框与端口探针。默认真件；
        用例注入假件，不必再 monkeypatch 本模块的全局名字。
        """
        super().__init__()
        # 主题只落这一次（构造期）：运行期再动 setStyleSheet 会让 Qt 重新 polish，
        # sizeHint 有机会跟着变——那正是"状态一变布局就跳"的另一种写法（见 theme 模块）。
        theme.apply_theme()
        self.setWindowIcon(icons.app_icon())
        self.session = session
        self.store = store
        self.log_file = log_file
        self.settings = settings
        # 与外界的那几个接缝（会弹框的对话框、端口探针）；不给就是真件，用例注入假件。
        self.deps = deps or WindowDeps()
        # 点位表的取数挪到后台（见 `engine/rows_pump.py`）：主线程那一跳只做模型 diff，
        # 不再等引擎回话——4000 点的表上一次 16ms，卡在心跳里就是 37% 的主线程预算。
        self.rows_pump = RowsPump(session)
        self.rows_pump.start()
        self.bridge = EngineBridge()
        self.point_model = PointTableModel()
        self.tree = DeviceTree()
        self.point_table = self._build_table()
        self.packet_panel = PacketPanel(store)
        self.log_view = self._build_log_view()
        self.log_tab = self._build_log_tab(self.log_view)
        # 核对页（主站模式）：只读别人的设备，自己一个端口都不监听（E1）
        self.verify_panel = VerifyPanel(session)
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
        self._restore_settings()
        self._build_status_bar()

        self.tabs.addTab(self.packet_panel, "报文监视")
        self.tabs.addTab(self.log_tab, "日志")
        self.tabs.addTab(self.verify_panel, "核对")
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
        self.verify_panel.notice.connect(self._log)
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



    # ---------- 设置（`simbench/settings.py`）----------

    def _restore_settings(self) -> None:
        """按上次那份设置恢复窗口几何/分隔条。**没给设置就什么都不做**（用例就是这么跑的）。"""
        if self.settings is None:
            return
        geometry = self._bytes_from_hex(self.settings.geometry)
        if geometry is not None:
            self.restoreGeometry(geometry)
            # 恢复过几何之后不要再按屏幕一半重算——用户明确调过的尺寸优先
            self._geometry_restored = True
        state = self._bytes_from_hex(self.settings.window_state)
        if state is not None:
            self.restoreState(state)

    @staticmethod
    def _bytes_from_hex(text: str):
        """`saveGeometry()` 的十六进制 -> 字节；空/坏值回 `None`（当没存过）。"""
        if not text:
            return None
        try:
            return QtCore.QByteArray.fromHex(text.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return None




    def _remember_settings(self) -> None:
        """把这次的窗口几何/主题/最近打开记下来（关窗时一次）。失败就算了（见 settings 模块）。"""
        if self.settings is None:
            return
        self.settings.geometry = bytes(self.saveGeometry().toHex()).decode("ascii")
        self.settings.window_state = bytes(self.saveState().toHex()).decode("ascii")
        self.settings.save()



    def _fit_to_screen(self) -> None:
        """起手尺寸 = **当前屏幕可用区域的 2/3**（`_START_SIZE`），并居中。

        为什么不按布局的 `sizeHint` 起手：那个尺寸是"控件刚好装得下"（本机实测 1058×317），
        又矮又宽，看着像一条带子；整屏铺满在小屏/多显示器上又太霸道。2/3 是现场最省事的
        一档——报文窗与日志页都还得往下翻，而左边/右边仍然留得下一个主站工具或浏览器。

        两个细节：屏幕用 `self.screen()`（还没 show 时是主屏）的**可用区域**（去掉任务栏），
        按整屏算的话窗口会被任务栏盖掉一条边；装不下的窄屏交给 Qt——它按布局的最小尺寸
        （`minimumSizeHint`）兜底，不会把控件压没，也不会让"2/3"变成 0。
        """
        if getattr(self, "_geometry_restored", False):
            # 上次的窗口尺寸已经恢复了：用户明确调过的大小优先于"屏幕的 2/3"
            return
        screen = self.screen()
        if screen is None:                          # 极端情况下没有屏幕信息：不猜尺寸
            return
        area = screen.availableGeometry()
        hint = self.minimumSizeHint()
        numerator, denominator = _START_SIZE
        self.resize(max(area.width() * numerator // denominator, hint.width()),
                    max(area.height() * numerator // denominator, hint.height()))
        frame = self.frameGeometry()
        frame.moveCenter(area.center())
        self.move(frame.topLeft())


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
            self.point_model.attach(pumped_source(self.rows_pump))
        else:
            # 一条通道都没有：表必须是**空的**，不许留着上一份的行（那是在演一份不存在的点表）
            self.point_model.detach()
        # 空点表不能悄悄给一张"看着合法的空白表"：CLI 那句在这里说同一遍
        self.channels.rebuild(project)
        self.store.clear()
        self._path = pathlib.Path(path) if path else None
        if path and self.settings is not None:
            # 记进"最近打开"（去重 + 置顶 + 只留最近 5 个）
            self.settings.add_recent(path)
        self._states = dict.fromkeys(names, "stopped")
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










    # ---------- 结构性改动共用的几步 ----------







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
        self.deps.issues_dialog(issues, self).exec()
        return False







    # ---------- 主题 ----------


    # ---------- 心跳与事件 ----------

    def _tick(self) -> None:
        self._report_swallowed()
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

    def _report_swallowed(self) -> None:
        """把"通知路径上吞掉的异常"报一次（见 `simbench/swallow.py`）。

        为什么要报：一个正在抛的订阅者回调（比如某次重绘炸了）在旧写法下**一点痕迹都没有**
        ——表现是"界面有时候不跟着变了"，而日志里干干净净。这里只在第一次报，10Hz 的路径上
        不许刷屏；之后再吞仍然记账（`count` 会涨，状态栏那一句里带着次数）。
        """
        for book in (self.session.swallowed, self.session.runner.swallowed):
            if book.count and not getattr(book, "reported", False):
                book.reported = True                     # 挂在账本上：换工程也不重复报
                self._log(f"[warning] {book.summary()}")

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
        """引擎/后端的一行 → 日志页。**原文保留，下面补一句中文**（见 `gui/messages.py`）。

        为什么不直接把引擎的文案改成中文：命令行、README 与冻结冒烟都按英文原话断言它
        （那是对外合同）；也不改成"界面上只显示中文"——原始英文是现场取证要贴给上游的东西。
        """
        self._log(f"[{line.level}] {line.text}")

    def _on_tree_selection(self, current, _previous) -> None:
        channel = self.tree.channel_of(current)
        if not channel or channel == self.point_model.channel():
            return
        self.point_model.attach(pumped_source(self.rows_pump), channel)
        self._shown_error = ""

    def _log(self, text: str) -> None:
        """写一行到日志页，**同时**落进会话日志文件（装了才有）。

        两处一起写是刻意的：窗里那份给人看（还能"清空"、能另存），文件那份给"事后查"
        （崩溃钩子写的也是它）。只写窗里的话，关窗之后什么都没了。
        """
        # 引擎/后端那些英文报错：原文保留 + 补一行中文（见 `gui/messages.py`）。
        # 只在这一处补：`_log` 是所有日志的必经之路，别的地方再补一次就会补两行。
        text = messages.with_explanation(text)
        self.log_view.appendPlainText(text)
        if self.log_file is not None:
            self.log_file.append(text)

    def refresh_rows_now(self) -> int:
        """同步取一次快照再刷表，回改了行数。

        为什么要有它：界面平时读的是泵的缓存（最多晚一个节拍，100ms），但"我刚改完一个值"
        或"用例要断言引擎此刻的真值"时不能等下一个节拍。**只有明确知道要等的人**才调它——
        心跳那条路不许调（那正是把界面拖回"跟引擎同节拍"的老样子）。
        """
        self.rows_pump.fetch_now()
        return self.point_model.refresh()


    def _summary(self) -> str:
        if not self._states:
            return "未启动"
        full = "；".join(f"{name}:{STATUS_WORD[state]}"
                          for name, state in self._states.items())
        if self._fits(full):
            return full
        # 定宽槽装不下时改文案（口径 4）：换成计数式摘要，**不加宽、不截断成"…"**
        counts: dict[str, int] = {}
        for state in self._states.values():
            counts[state] = counts.get(state, 0) + 1
        return " ".join(f"{STATUS_WORD[state]}{counts[state]}"
                        for state in STATUS_ORDER if counts.get(state))

    def _fits(self, text: str) -> bool:
        return QtGui.QFontMetrics(self.status_label.font()).horizontalAdvance(
            text) <= self.status_label.width()

    # ---------- 关闭（口径 ε） ----------


    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self.timer.stop()
        self.rows_pump.stop()
        self.session.shutdown()
        self._remember_settings()
        if self.log_file is not None:
            # 收工前把缓冲刷下去：崩溃钩子写的是同一个文件，句柄留着也无妨，但关窗之后
            # 不该再有人往它里面写（这个进程的日志到此为止）。
            self.log_file.append(f"==== simbench closed "
                                 f"{dt.datetime.now().isoformat(timespec='seconds')} ====")
            self.log_file.close()
        event.accept()

__all__ = [
    "_FIRST_SPLIT",
    "_GC_EVERY_S",
    "_LOG_LINES",
    "_NOTICE_MS",
    "_START_SIZE",
    "_STATUS_SLOT_WIDTH",
    "_TABLE_STRETCH",
    "_TICK_MS",
    "_TREE_STRETCH",
    "MainWindow",
    "_one_line",
    "_parse_issue",
]
