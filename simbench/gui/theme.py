# simbench/gui/theme.py
"""外观主题：配色、字体、控件样式与图标集中在这一处，浅色/深色两套只差颜色。

为什么单独成模块：QSS 是一张**全局**表，写进哪个文件，那个文件就顺带成了"谁能改界面
长相"的入口。集中在这里，`main_window` 只调一次 `apply_theme()`，圆角与配色不会散落在
五六个 widget 里各写一半。

两套配色（`LIGHT` / `DARK`）**共用同一张样式模板** `_QSS`，模板里一个颜色字面量都没有，
全部是占位符。这条结构决定了"切主题只换颜色"：几何（padding/border/字号/定宽槽）在
两套之间是同一份字节，不可能出现"深色下一挪就变样"。`tests/test_gui_theme.py` 里有一条
"两套渲染出来的表只差颜色"的断言替人核这件事。

四条不许破的口径（M2 用户口径 3/4/5，`tests/test_gui_layout_invariant.py` 逐条量几何）：

* 主题**只在构造期**落一次（`apply_theme()` 进程内只生效一次，`is_applied()` 可查）。
  运行期状态变化一个 QSS 都不重设——重设样式表会让 Qt 重新 polish，`sizeHint` 有机会
  跟着变，那就是"状态一变布局就跳"。**唯一的例外是用户主动切主题**（`set_mode()`）：
  那是一次显式的外观动作，且两套配色结构相同，所以几何一个像素都不该动——由
  `test_switching_the_theme_does_not_move_anything` 钉住；
* 主题不写会撑开定宽槽的度量。面板里那些定宽槽（通道状态 88px、报文过滤 56px、计数
  96px、状态栏摘要 320px）是**布局合同**，主题只改颜色、边框、圆角与间距；字号统一取
  `_FONT_POINT_SIZE`——改它就得回头核一遍每个槽位装不装得下最长的那句文案，而
  `tests/test_gui_theme.py` 里正有一条"最长的文案装得下"的断言替人核；
* 状态色（`status_colors()`）走 QPalette 与 item foreground，主题里**不写**
  `QLabel { color }` 这种通配规则——样式表的 color 优先级高于 palette，一写就把四态颜色
  全盖成同一种灰，界面就不再"一眼看出哪一路挂了"。深色下这四个色另配一套亮一些的：
  白底上合适的 `#1a7f37` 放到深底上就快看不见了；
* 选择器只写类名（`QToolBar`、`QTableView`…），不写 `#objectName`：主窗里那几个控件的
  objectName 是**创建之后**才设的，而盒模型类属性（padding/spacing）在首次 polish 时
  就按当时的选择器定死了——`#mainToolBar` 那套规则要等下一次重设样式表才生效，首屏与
  切换后于是量出两个工具栏高度（41px vs 49px，实测）。切主题那条用例就是钉这个的；
* 样式表里所有 padding 都按"看得见的最长文案 + 12px 内边距"配（`QPushButton` 的
  `padding: 4px 5px` + `border: 1px`）：`tests/test_gui_packet_view.py` 与
  `tests/test_gui_channel_panel.py` 的"不许截断"断言里那个 12 就是它，两边一起改才不算撒谎。
"""

from __future__ import annotations

import os

from PySide6 import QtCore, QtGui, QtWidgets

# 10pt 是中文界面读起来不费眼的下限；9pt 在 4K 屏上要贴着看。改这个数必看模块 docstring
# 第二条：`_STATUS_WIDTH` 那类定宽槽先按最长文案复核一遍。
_FONT_POINT_SIZE = 10
_FONT_FAMILIES = ("Segoe UI", "Microsoft YaHei UI", "PingFang SC",
                  "Noto Sans CJK SC", "sans-serif")
_MONO_FAMILIES = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono",
                  "Liberation Mono", "monospace")

# 模式名是给用户看的（环境变量、状态栏那句话），也是 `_PALETTES` 的键。
MODES = ("light", "dark")
DEFAULT_MODE = "light"
# 命令行/快捷方式里可以用 `set SIMBENCH_THEME=dark` 选一个起手模式（与 `SIMBENCH_NET_AUTO`
# 同一个路子：一个环境变量管一件事，不引入一份没人找得到的配置文件）。
ENV_VAR = "SIMBENCH_THEME"

# 两套配色的**键必须完全一样**：模板只认键，键一多一少就是"某一套下少画了一处"。
# 值一律是 `#rrggbb` 字面量，`tests/test_gui_theme.py` 会逐条验它是合法颜色。
LIGHT = {
    "window_bg": "#f2f5f8",        # 窗口底
    "surface": "#ffffff",          # 卡片 / 表格 / 工具栏面
    "header": "#eef3f8",           # 表头
    "band": "#f7fafc",             # 斑马纹
    "line": "#d8e0e9",             # 分隔线 / 边框
    "ink": "#1f2d3d",              # 主文字
    "ink_soft": "#4c5c6d",         # 次文字（表头、状态栏）
    "ink_disabled": "#9aa5b1",     # 置灰的文字
    "accent": "#1f6fb2",           # 强调色（选中、勾选、焦点）
    "accent_hover": "#256ea8",
    "accent_text": "#ffffff",      # 强调色上的文字
    "field_bg": "#ffffff",         # 输入框 / 按钮的面
    "field_border": "#cbd5e0",
    "field_border_hover": "#a8c1d8",
    "disabled_bg": "#f2f4f7",
    "disabled_border": "#e2e7ec",
    "hover_bg": "#eef5fb",         # 输入框/按钮悬停
    "pressed_bg": "#dfe9f3",
    "toolbar_hover_bg": "#e9f1f8",
    "toolbar_hover_border": "#cfe0ee",
    "toolbar_pressed_bg": "#d9e7f3",
    "tab_bg": "#e8eef5",
    "tab_hover_bg": "#f1f6fa",
    "grid": "#edf1f5",             # 表格格线
    "selection_bg": "#d7e8f7",
    "selection_text": "#10293d",
    "menu_selected_bg": "#e6f0fa",
    "text_edit_bg": "#fcfdfe",     # 日志/报文这类大块只读文本
    "scroll": "#c4d0dc",
    "scroll_hover": "#aab9c9",
    "splitter_hover": "#b9cbdc",
    "tooltip_bg": "#1f2d3d",
    "tooltip_text": "#ffffff",
}

DARK = {
    "window_bg": "#161b22",
    "surface": "#1f262e",
    "header": "#262f3a",
    "band": "#1b222a",
    "line": "#333e4a",
    "ink": "#e6edf3",
    "ink_soft": "#9fb0c0",
    "ink_disabled": "#6b7a89",
    "accent": "#3d8fd1",
    "accent_hover": "#4d9fe0",
    "accent_text": "#ffffff",
    "field_bg": "#1a2229",
    "field_border": "#3b4855",
    "field_border_hover": "#55718c",
    "disabled_bg": "#232b33",
    "disabled_border": "#2e3843",
    "hover_bg": "#29343f",
    "pressed_bg": "#33414e",
    "toolbar_hover_bg": "#2a3742",
    "toolbar_hover_border": "#3c4b58",
    "toolbar_pressed_bg": "#33414e",
    "tab_bg": "#232b33",
    "tab_hover_bg": "#2a333d",
    "grid": "#2b3540",
    "selection_bg": "#2f5c85",
    "selection_text": "#ffffff",
    "menu_selected_bg": "#2f5c85",
    "text_edit_bg": "#1a2229",
    "scroll": "#4a5866",
    "scroll_hover": "#61717f",
    "splitter_hover": "#4a5866",
    "tooltip_bg": "#0d1117",
    "tooltip_text": "#e6edf3",
}

_PALETTES = {"light": LIGHT, "dark": DARK}

# 设备树的四态符号与通道面板的状态色：白底上好看的那一组放到深底上快看不见了，
# 所以颜色也按模式给一份（文字/符号仍然成对出现，见 `device_tree` 的模块说明）。
_STATUS_COLORS = {
    "light": {"stopped": "#666666", "starting": "#b26a00",
              "running": "#1a7f37", "error": "#c62828"},
    "dark": {"stopped": "#8b949e", "starting": "#d29922",
             "running": "#3fb950", "error": "#f85149"},
}

# 样式模板：**一个颜色字面量都没有**，全部走占位符。CSS 的花括号成对写（`{{`/`}}`），
# 因为最终要过一遍 `str.format`。
_QSS = """
QMainWindow, QDialog, QMessageBox {{ background: {window_bg}; }}

QWidget#card {{
    background: {surface};
    border: 1px solid {line};
    border-radius: 8px;
}}
/* 卡片里的两句话（通道名、状态）加粗：一行里它们才是"这台设备现在怎样"。
   只写 font-weight——**不许**在这里写 color，四态颜色走 QPalette（见模块 docstring）。 */
QWidget#card QLabel {{ font-weight: 600; }}

/* 工具栏这里**用类选择器**（`QToolBar`），不写 `QToolBar#mainToolBar`：主窗是先
   `addToolBar("main")` 再 `setObjectName("mainToolBar")` 的，而 QToolBar 自己的盒模型
   （padding/spacing）是在**首次 polish** 时按当时的选择器算出来并缓存下来的——那一刻
   objectName 还没设上，`#mainToolBar` 那几条规则要等到下一次 `setStyleSheet()` 才生效。
   实测后果就是：首屏工具栏 41px 高、按钮距左 4px，切一次主题变成 49px、距左 8px，
   "切主题没动布局"这句话当场就假了。类选择器没有这个时序问题（界面上只有这一条工具栏）。 */
QToolBar {{
    background: {surface};
    border: none;
    border-bottom: 1px solid {line};
    padding: 6px 8px;
    spacing: 4px;
}}
QToolBar QToolButton {{
    color: {ink};
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 10px;
}}
QToolBar QToolButton:hover {{
    background: {toolbar_hover_bg};
    border-color: {toolbar_hover_border};
}}
QToolBar QToolButton:pressed {{ background: {toolbar_pressed_bg}; }}
QToolBar QToolButton:checked {{
    background: {accent};
    border-color: {accent};
    color: {accent_text};
}}
QToolBar::separator {{ background: {line}; width: 1px; margin: 6px 6px; }}

QStatusBar {{
    background: {surface};
    border-top: 1px solid {line};
    color: {ink_soft};
}}
QStatusBar::item {{ border: none; }}
/* 状态栏摘要那一格只上颜色，**不上 padding**：`main_window._fits()` 拿 label.width() 与
   文案宽度比，加了内边距就等于让它把"装得下"判宽，而 QLabel 溢出时是硬裁、不出省略号。 */
QStatusBar QLabel {{ color: {ink}; }}

QTabWidget::pane {{
    background: {surface};
    border: 1px solid {line};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    background: {tab_bg};
    color: {ink_soft};
    border: 1px solid {line};
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 6px 18px;
    margin-right: 3px;
}}
QTabBar::tab:selected {{ background: {surface}; color: {accent}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ background: {tab_hover_bg}; }}

QHeaderView::section {{
    background: {header};
    color: {ink_soft};
    border: none;
    border-right: 1px solid {line};
    border-bottom: 1px solid {line};
    padding: 5px 8px;
    font-weight: 600;
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {header}; border: none; }}

QTableView, QTreeView {{
    background: {surface};
    alternate-background-color: {band};
    border: 1px solid {line};
    border-radius: 8px;
    gridline-color: {grid};
    selection-background-color: {selection_bg};
    selection-color: {selection_text};
    outline: none;
}}

QPushButton {{
    background: {field_bg};
    color: {ink};
    border: 1px solid {field_border};
    border-radius: 6px;
    padding: 4px 5px;
}}
QPushButton:hover {{ background: {hover_bg}; border-color: {field_border_hover}; }}
QPushButton:pressed {{ background: {pressed_bg}; }}
QPushButton:checked {{ background: {accent}; border-color: {accent}; color: {accent_text}; }}
QPushButton:checked:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton:disabled {{
    background: {disabled_bg};
    color: {ink_disabled};
    border-color: {disabled_border};
}}

QLineEdit {{
    background: {field_bg};
    color: {ink};
    border: 1px solid {field_border};
    border-radius: 5px;
    padding: 3px 5px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}
QLineEdit:focus {{ border-color: {accent}; }}
QLineEdit:disabled {{
    background: {disabled_bg};
    color: {ink_disabled};
    border-color: {disabled_border};
}}

QPlainTextEdit, QTextEdit {{
    background: {text_edit_bg};
    color: {ink};
    border: 1px solid {line};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {accent};
    selection-color: {accent_text};
}}

QRadioButton, QCheckBox {{ color: {ink}; spacing: 6px; }}

QMenu {{
    background: {surface};
    border: 1px solid {line};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: 5px; color: {ink}; }}
QMenu::item:selected {{ background: {menu_selected_bg}; }}
QMenu::item:disabled {{ color: {ink_disabled}; }}
QMenu::separator {{ background: {line}; height: 1px; margin: 4px 6px; }}

QSplitter::handle {{ background: {line}; }}
QSplitter::handle:hover {{ background: {splitter_hover}; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {scroll_hover}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {scroll}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {tooltip_bg};
    color: {tooltip_text};
    border: none;
    border-radius: 4px;
    padding: 4px 6px;
}}
"""

CARD_OBJECT_NAME = "card"

_mode: str | None = None          # 当前生效的模式；None = 这个进程还没落过主题


# ---------- 模式 ----------

def resolve_mode() -> str:
    """起手用哪个模式：环境变量说了算，只认 `light`/`dark`，其余一律回默认。

    静默回默认而不是报错：这个变量是给命令行/快捷方式顺手设的，拼错了不该让界面起不来。
    """
    wanted = os.environ.get(ENV_VAR, "").strip().lower()
    return wanted if wanted in _PALETTES else DEFAULT_MODE


def current_mode() -> str | None:
    """当前生效的模式；还没落过主题就是 `None`。"""
    return _mode


def qss(mode: str) -> str:
    """某个模式渲染出来的样式表。两套只是把同一张模板的占位符填成不同的颜色。"""
    return _QSS.format(**_PALETTES[_checked_mode(mode)])


def palette(mode: str) -> QtGui.QPalette:
    """某个模式的 QPalette：QSS 盖不到的原生弹层（文件对话框、提示框）跟着一起变。"""
    colours = _PALETTES[_checked_mode(mode)]
    p = QtGui.QPalette()
    for role, key in ((QtGui.QPalette.Window, "window_bg"),
                      (QtGui.QPalette.Base, "surface"),
                      (QtGui.QPalette.AlternateBase, "band"),
                      (QtGui.QPalette.Text, "ink"),
                      (QtGui.QPalette.WindowText, "ink"),
                      (QtGui.QPalette.ButtonText, "ink"),
                      (QtGui.QPalette.PlaceholderText, "ink_disabled"),
                      (QtGui.QPalette.ToolTipBase, "tooltip_bg"),
                      (QtGui.QPalette.ToolTipText, "tooltip_text"),
                      (QtGui.QPalette.Highlight, "accent"),
                      (QtGui.QPalette.HighlightedText, "accent_text"),
                      (QtGui.QPalette.Link, "accent")):
        p.setColor(role, QtGui.QColor(colours[key]))
    return p


def status_colors(mode: str | None = None) -> dict[str, QtGui.QColor]:
    """四态颜色（`stopped/starting/running/error`）。按模式取，深色下另配一套亮一点的。"""
    name = mode if mode in _STATUS_COLORS else (_mode or resolve_mode())
    return {state: QtGui.QColor(value)
            for state, value in _STATUS_COLORS[name].items()}


def _checked_mode(mode: str) -> str:
    if mode not in _PALETTES:
        raise KeyError(f"unknown theme mode {mode!r}; expected one of {MODES}")
    return mode


# ---------- 字体 ----------

def app_font() -> QtGui.QFont:
    """界面正文字体：拉丁走 Segoe UI、中文缺字自动回落到雅黑/苹方。"""
    font = QtGui.QFont()
    font.setFamilies(list(_FONT_FAMILIES))
    font.setPointSizeF(_FONT_POINT_SIZE)
    return font


def mono_font() -> QtGui.QFont:
    """日志与报文这类要对齐着读的地方用等宽字体。"""
    font = QtGui.QFont()
    font.setFamilies(list(_MONO_FAMILIES))
    font.setPointSizeF(_FONT_POINT_SIZE)
    return font


# ---------- 落地与切换 ----------

def apply_theme(app: QtWidgets.QApplication | None = None,
                mode: str | None = None) -> str:
    """把主题落到 QApplication 上，返回生效的模式。**进程内只落一次**。

    走 QApplication 而不是某个窗口，是因为要盖住**无父窗口**的弹层（错误清单、文件对话框
    的非原生部分）；一次设好，后建的 widget 直接继承，不需要每个窗口各设一次
    （两处各设一半，迟早出现"这个框没上色"）。幂等不是"再写一遍同一张表"而是"什么都不做"：
    `setStyleSheet()` 每次都会把进程里**所有**活着的控件重新 polish 一遍。

    用户主动切模式走 `set_mode()`，那条路是允许重新落一遍的。
    """
    target = app if app is not None else QtWidgets.QApplication.instance()
    if target is None:                      # 无头 CLI 不建 QApplication，这里什么都不做
        return _mode or mode or resolve_mode()
    if _mode is not None:
        return _mode
    return _install(target, mode or resolve_mode())


def set_mode(mode: str, app: QtWidgets.QApplication | None = None) -> str:
    """切到某个模式并立刻重画。**只换颜色**：两套配色共用同一张模板，几何不参与。

    这是"运行期不重设 QSS"那条口径的唯一例外，而且是用户按的——主窗负责在切完之后把
    状态色相关的几处（设备树、通道面板）按新模式重涂一遍，并把工具栏图标按新底色重画。
    """
    target = app if app is not None else QtWidgets.QApplication.instance()
    if target is None:
        return _checked_mode(mode)
    return _install(target, _checked_mode(mode))


def toggle(app: QtWidgets.QApplication | None = None) -> str:
    """在浅色/深色之间翻一次，返回切换后的模式。"""
    return set_mode("dark" if (_mode or resolve_mode()) == "light" else "light", app)


def _install(app: QtWidgets.QApplication, mode: str) -> str:
    global _mode
    # Fusion：原生样式与 QSS 混画时，QSS 只能盖住一半控件（另一半仍是系统灰），
    # 换掉之后"说了算的"只剩这张表。
    app.setStyle("Fusion")
    app.setPalette(palette(mode))
    app.setFont(app_font())
    app.setStyleSheet(qss(mode))
    _mode = mode
    return _mode


def is_applied() -> bool:
    return _mode is not None


def make_card(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """把一个裸 QWidget 变成白面圆角卡片（返回值就是它自己，便于链式摆放）。"""
    widget.setObjectName(CARD_OBJECT_NAME)
    widget.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    return widget


# ---------- 图标 ----------
#
# 图标用 QPainter 现画而不是带一套 png：仓库里没有图片资源目录，几百字节的矢量形状
# 不引入打包路径问题，也就不会出现"装完之后按钮是空白的"。颜色按模式给：深底上要用
# 亮一档的那几个，否则"保存"这种深灰图标直接消失在底色里。

_ICON_SIZE = 18
_ICON_2X = 2                # 图标按 2 倍图建再交给 Qt 缩放：HiDPI 上不糊

_ICON_COLORS = {
    "light": {"play": "#1a7f37", "stop": "#c62828", "in": "#1f6fb2", "out": "#1f6fb2",
              "folder": "#b26a00", "save": "#42546a", "check": "#0f766e",
              "wave": "#1f6fb2", "dark": "#42546a", "new": "#7c3aed"},
    "dark": {"play": "#3fb950", "stop": "#f85149", "in": "#58a6ff", "out": "#58a6ff",
             "folder": "#d29922", "save": "#adbac7", "check": "#39c5bb",
             "wave": "#58a6ff", "dark": "#adbac7", "new": "#a78bfa"},
}


def _pixmap() -> QtGui.QPixmap:
    pix = QtGui.QPixmap(_ICON_SIZE * _ICON_2X, _ICON_SIZE * _ICON_2X)
    pix.setDevicePixelRatio(_ICON_2X)
    pix.fill(QtCore.Qt.transparent)
    return pix


def _painter(pix: QtGui.QPixmap, color: str) -> QtGui.QPainter:
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtGui.QColor(color))
    return painter


def _draw_play(painter: QtGui.QPainter) -> None:
    painter.drawPolygon([QtCore.QPointF(5.0, 3.0), QtCore.QPointF(15.0, 9.0),
                         QtCore.QPointF(5.0, 15.0)])


def _draw_stop(painter: QtGui.QPainter) -> None:
    painter.drawRoundedRect(QtCore.QRectF(4.5, 4.5, 9.0, 9.0), 1.5, 1.5)


def _draw_arrow(painter: QtGui.QPainter, down: bool) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    if down:
        painter.drawLine(QtCore.QPointF(9.0, 2.5), QtCore.QPointF(9.0, 11.0))
        painter.drawPolyline([QtCore.QPointF(5.2, 7.6), QtCore.QPointF(9.0, 11.4),
                              QtCore.QPointF(12.8, 7.6)])
        painter.drawPolyline([QtCore.QPointF(3.0, 12.5), QtCore.QPointF(3.0, 15.2),
                              QtCore.QPointF(15.0, 15.2), QtCore.QPointF(15.0, 12.5)])
    else:
        painter.drawLine(QtCore.QPointF(9.0, 15.5), QtCore.QPointF(9.0, 7.0))
        painter.drawPolyline([QtCore.QPointF(5.2, 10.4), QtCore.QPointF(9.0, 6.6),
                              QtCore.QPointF(12.8, 10.4)])
        painter.drawPolyline([QtCore.QPointF(3.0, 5.5), QtCore.QPointF(3.0, 2.8),
                              QtCore.QPointF(15.0, 2.8), QtCore.QPointF(15.0, 5.5)])


def _draw_folder(painter: QtGui.QPainter) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    path = QtGui.QPainterPath()
    path.moveTo(2.5, 13.5)
    path.lineTo(2.5, 5.0)
    path.lineTo(7.0, 5.0)
    path.lineTo(8.6, 6.8)
    path.lineTo(15.5, 6.8)
    path.lineTo(15.5, 13.5)
    path.closeSubpath()
    painter.drawPath(path)


def _draw_save(painter: QtGui.QPainter) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawRoundedRect(QtCore.QRectF(3.0, 3.0, 12.0, 12.0), 2.0, 2.0)
    painter.drawRect(QtCore.QRectF(6.0, 3.0, 6.0, 4.5))
    painter.drawRect(QtCore.QRectF(5.5, 10.0, 7.0, 5.0))


def _draw_check(painter: QtGui.QPainter) -> None:
    pen = painter.pen()
    pen.setWidthF(2.4)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawPolyline([QtCore.QPointF(3.5, 9.5), QtCore.QPointF(7.5, 13.0),
                          QtCore.QPointF(14.5, 5.0)])


def _draw_wave(painter: QtGui.QPainter) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    path = QtGui.QPainterPath()
    path.moveTo(2.5, 12.0)
    path.lineTo(5.5, 12.0)
    path.lineTo(7.0, 5.5)
    path.lineTo(9.0, 14.5)
    path.lineTo(11.0, 3.5)
    path.lineTo(13.0, 12.0)
    path.lineTo(15.5, 12.0)
    painter.drawPath(path)


def _draw_moon(painter: QtGui.QPainter) -> None:
    """"深色主题"那个按钮：一轮月牙（自己画的，两个圆弧咬出来）。"""
    path = QtGui.QPainterPath()
    path.addEllipse(QtCore.QRectF(3.0, 3.0, 12.0, 12.0))
    bite = QtGui.QPainterPath()
    bite.addEllipse(QtCore.QRectF(7.5, 1.5, 12.0, 12.0))
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawPath(path.subtracted(bite))


def _draw_new(painter: QtGui.QPainter) -> None:
    """"新建工程"那个按钮：一张纸 + 右上角折角。

    不用一个 `+`：`+` 在这个界面上更像"加一行"，而这是**换一份空工程**（把当前这份放下）。
    折角是"一份新文件"最省笔墨的形状，也和 `folder`/`save` 那几个图标同一套线宽。
    """
    painter.setBrush(QtCore.Qt.NoBrush)
    path = QtGui.QPainterPath()
    path.moveTo(3.5, 2.5)
    path.lineTo(10.0, 2.5)
    path.lineTo(14.5, 7.0)
    path.lineTo(14.5, 15.5)
    path.lineTo(3.5, 15.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawPolyline([QtCore.QPointF(10.0, 2.5), QtCore.QPointF(10.0, 7.0),
                          QtCore.QPointF(14.5, 7.0)])


_DRAW = {"play": _draw_play, "stop": _draw_stop,
         "in": lambda p: _draw_arrow(p, down=True),
         "out": lambda p: _draw_arrow(p, down=False),
         "folder": _draw_folder, "save": _draw_save, "check": _draw_check,
         "wave": _draw_wave, "dark": _draw_moon, "new": _draw_new}


def icon(name: str, mode: str | None = None) -> QtGui.QIcon:
    """按名字取一个工具栏图标。名字不在表里就抛——空白按钮是现场最难查的一种"没反应"。"""
    draw = _DRAW.get(name)
    if draw is None:
        raise KeyError(f"unknown icon {name!r}")
    which = mode if mode in _ICON_COLORS else (_mode or resolve_mode())
    pix = _pixmap()
    painter = _painter(pix, _ICON_COLORS[which][name])
    try:
        draw(painter)
    finally:
        painter.end()
    return QtGui.QIcon(pix)


def icon_size() -> QtCore.QSize:
    return QtCore.QSize(_ICON_SIZE, _ICON_SIZE)


def app_icon(size: int = 128) -> QtGui.QIcon:
    """窗口/任务栏图标：深蓝底圆角方块 + 一条波形（和工具栏那个 `wave` 同一个形状）。

    两种模式共用一张：任务栏底很少是界面底色，跟着主题变反而更不好认。

    `size` 是**画**出来的边长（不是把 128 那张拉伸）：`packaging/make_icon.py` 用它生成
    exe 图标那几档尺寸，放大位图在 256 上会糊。
    """
    size = int(size)
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor("#123a5c"))
    painter.drawRoundedRect(QtCore.QRectF(6, 6, size - 12, size - 12), 26, 26)
    painter.scale(size / _ICON_SIZE, size / _ICON_SIZE)
    pen = QtGui.QPen(QtGui.QColor("#7fd1ff"))
    pen.setWidthF(1.6)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    _draw_wave(painter)
    painter.end()
    return QtGui.QIcon(pix)
