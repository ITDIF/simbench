"""主题只动外观、不动布局：这一页把"外观"与"布局合同"的分界钉成断言。

`tests/test_gui_layout_invariant.py` 量的是"状态一变几何不许动"，而主题是**一次性**的外观
改动，最容易在"加个圆角、加点 padding"里把定宽槽撑破——那些槽位（通道状态 88px、报文过滤
56px、计数 96px、状态栏摘要 320px）是操作员"点哪里"的合同，破了以后的表现是文字被截断、
最后一列被挤走。所以这里单独钉这些事：

1. 主题真的落在 QApplication 上（底层样式 + 字体 + 样式表），并且**只落一次**；
2. 主题之后每个定宽槽仍装得下它最长的那句文案（`QPushButton` 那 12px 内边距的口径与
   `test_gui_packet_view` 一致）；
3. 主题不许出现通配的 `QLabel { color }` —— 它的优先级高于 QPalette，一写就把四态颜色
   盖成同一种灰；
4. 设备树的两列仍是定宽槽（240/40），并且"最后一列自动铺满"关着：开着它，同一棵树先后
   量到 40 与 398 两种宽度，右边那张点位表跟着挪（实测就是这么跳的）；
5. 深浅两套配色**共用同一张样式模板**（抠掉色值后逐字节相同），所以切主题只换颜色；
   切完几何、状态色、工具栏图标都要跟着对上，而且浅色不是深色、深色真的暗。
"""

import pathlib
import re

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui import device_tree as device_tree_module
from simbench.gui import main_window as main_window_module
from simbench.gui import theme
from simbench.gui.channel_panel import STATUS_LABELS
from simbench.gui.main_window import MainWindow
from simbench.gui.point_table import COLUMNS, VALUE_COLUMN

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """错误清单是模态的：换成记录器，否则用例回不来（与布局用例同一口径）。"""
    class Recorder:
        def __init__(self, issues, parent=None) -> None:
            pass

        def exec(self, *_args) -> int:
            return 0

    monkeypatch.setattr(main_window_module, "IssueDialog", Recorder)


@pytest.fixture
def window(qapp, engine_runner):
    # 这一页的断言从浅色出发：起手模式可能来自环境变量（`SIMBENCH_THEME=dark`），
    # 不钉住的话同一份用例在两台机器上量的不是同一套配色。
    theme.set_mode("light", qapp)
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    w = MainWindow(session, store)
    w.use_project(project, CSV)
    w.resize(1100, 680)
    w.show()
    qapp.processEvents()
    yield w
    w.timer.stop()
    w.session.shutdown()
    w.deleteLater()
    qapp.processEvents()


# ---------- 1. 主题落在哪 ----------

def test_the_theme_lands_on_the_application_once(qapp):
    """样式表与字体上到 QApplication：后建的弹层（错误清单、属性面板）才跟着一起变。"""
    assert theme.is_applied(), "用例没跑在主题下：`qapp` 夹具负责第一次落地"
    assert qapp.styleSheet() == theme.qss(theme.current_mode())
    assert qapp.font().pointSizeF() == pytest.approx(theme._FONT_POINT_SIZE)
    # 底层样式必须是 Fusion：混着原生样式画，QSS 只盖得住一半控件（见 apply_theme）
    previous = qapp.styleSheet()
    qapp.setStyleSheet("")                    # 摘掉样式表才看得见底下那层是谁
    try:
        assert qapp.style().objectName().lower() == "fusion"
    finally:
        qapp.setStyleSheet(previous)
    theme.apply_theme(qapp)                   # 幂等：再来一次仍然是同一张表
    assert qapp.styleSheet() == theme.qss(theme.current_mode())


# ---------- 2. 定宽槽装得下 ----------

def test_every_fixed_slot_still_fits_its_longest_caption(window, qapp):
    """字体 9pt→10pt、按钮加了内边距之后，每个定宽槽仍须装下最长的那句文案。"""
    row = window.channels.rows()[0]
    for state, text in STATUS_LABELS.items():
        window.channels.set_status(row.channel, state)
        qapp.processEvents()
        assert row.status.text() == text
        assert "…" not in row.status.text() and "..." not in row.status.text()
        advance = row.status.fontMetrics().horizontalAdvance(text)
        assert advance <= row.status.width(), (state, text, advance, row.status.width())
    window.channels.set_status(row.channel, "stopped")

    panel = window.packet_panel
    for button in panel.filter_labels():
        advance = button.fontMetrics().horizontalAdvance(button.text())
        # 12 = 主题给 QPushButton 的左右内边距 + 边框（`padding: 4px 5px` + 1px border）
        assert advance + 12 <= button.width(), (button.text(), advance, button.width())
    for text in ("0 帧", "5000 帧"):
        panel.count_label.setText(text)
        qapp.processEvents()
        advance = panel.count_label.fontMetrics().horizontalAdvance(text)
        assert advance <= panel.count_label.width(), (text, advance)


def test_the_value_column_is_sized_for_the_widest_value_it_shows(window):
    """字号 9pt→10pt 之后，"当前值"那一列必须跟着加宽：110px 装不下 17 位的 uint64。

    为什么不拿 `fontMetrics()` 现量：offscreen 那套兜底字体把每个字形都按 1em 算（数字和
    汉字一样宽，17 位数字量出来 221px），照它加宽会得到一个部署环境里宽得离谱的列。所以
    这里改成钉**前提 + 结论**两件事：示例点表里最长的那个值确实是 17 位，而列宽是 132
    ——17 位数字在部署环境的 Segoe UI 10pt 下实测 119px，加单元格左右各 4px 的文字 margin
    是 127，132 留出余量。谁把列宽改回去、或者示例点表换了个更长的值，这条就红。

    截断的后果不是"不好看"：`QTableView` 画不下会自己接省略号，`7262385979038…` 少掉的
    那几位正是操作员拿去和点表核对的东西。
    """
    values = [str(window.point_model.index(r, VALUE_COLUMN).data() or "")
              for r in range(window.point_model.rowCount())]
    assert values, "点表是空的，这条断言没测到东西"
    assert max(len(value) for value in values) == 17, values
    assert COLUMNS[VALUE_COLUMN].width == 132
    assert window.point_table.columnWidth(VALUE_COLUMN) == 132


def test_the_theme_never_overrides_a_state_color():
    """四态颜色走 QPalette（`theme.status_colors()`），而样式表的 color 优先级更高。

    所以主题里不许出现通配的 `QLabel { ... }`：那样四态文字会被刷成同一个颜色，
    "哪一路挂了"就只能靠读字。带前缀的选择器（`QStatusBar QLabel`）不算通配。
    """
    naked = [line.strip() for line in theme.qss("light").splitlines()
             if line.strip().startswith("QLabel")]
    assert naked == [], naked
    assert set(theme.status_colors()) == set(STATUS_LABELS)


def test_the_channel_status_keeps_its_palette_colour(window, qapp):
    """颜色确实还是从 palette 走，而且取的是当前模式那一组。"""
    row = window.channels.rows()[0]
    for state in ("running", "error", "stopped"):
        window.channels.set_status(row.channel, state)
        qapp.processEvents()
        colour = row.status.palette().color(QtGui.QPalette.WindowText)
        assert colour.name() == theme.status_colors()[state].name(), state


# ---------- 3. 设备树的两列 ----------

def test_the_device_tree_keeps_two_fixed_columns(window, qapp):
    """两列都是定宽槽，"最后一列自动铺满"必须关着——否则列宽跟着瞬时视口走。"""
    tree = window.tree
    name = tree.topLevelItem(0).text(0)
    assert not tree.header().stretchLastSection()
    for state in ("starting", "running", "error", "stopped"):
        tree.set_status(name, state)
        qapp.processEvents()
        assert tree.columnWidth(0) == device_tree_module._NAME_WIDTH, state
        assert tree.columnWidth(1) == device_tree_module._MARK_WIDTH, state


# ---------- 4. 深浅两套 ----------

def _luma(value: str) -> float:
    """色值的明度（人眼权重）：只用来比"哪个更亮"，不假装是 WCAG 对比度。"""
    colour = QtGui.QColor(value)
    return 0.299 * colour.red() + 0.587 * colour.green() + 0.114 * colour.blue()


def test_the_two_modes_differ_only_in_colour():
    """两套配色共用同一张模板：把色值抠掉之后，两个模式的样式表必须**逐字节相同**。

    这条是"切主题不许动布局"的结构保证：只要模板里有哪一个 padding/字号是按模式写的，
    这里立刻不等——而靠人眼比对两张两百行的样式表，是做不完的活。
    """
    assert set(theme.LIGHT) == set(theme.DARK), "两套配色的键不一样，模板会缺项"
    assert theme.LIGHT != theme.DARK, "深色那套是浅色的拷贝"

    colour = re.compile(r"#[0-9a-fA-F]{6}")
    stripped = {mode: colour.sub("#colour", theme.qss(mode)) for mode in theme.MODES}
    assert stripped["light"] == stripped["dark"]
    for mode in theme.MODES:
        # 占位符全都填上了（`str.format` 漏一个会抛，漏成一整个词条却不会）
        assert not re.search(r"\{[a-z_]+\}", theme.qss(mode)), mode
        for key, value in theme._PALETTES[mode].items():
            assert QtGui.QColor(value).isValid(), (mode, key, value)
    # 图标也是按模式取色的：两张色表的键要一样，且覆盖每一个画得出来的图标
    assert set(theme._ICON_COLORS["light"]) == set(theme._ICON_COLORS["dark"])
    for name in theme._DRAW:
        assert name in theme._ICON_COLORS["light"], name


def test_the_dark_palette_is_actually_dark_and_the_status_colours_go_with_it():
    """深色不是"把字刷白"：底色要真暗、文字要真亮；白底上那四个状态色放到深底上要换一批
    更亮的，否则"运行/失败"在深色下比在浅色下更难认。
    """
    assert _luma(theme.LIGHT["window_bg"]) > _luma(theme.LIGHT["ink"])
    assert _luma(theme.DARK["window_bg"]) < _luma(theme.DARK["ink"])
    assert _luma(theme.DARK["window_bg"]) < _luma(theme.LIGHT["window_bg"])
    assert _luma(theme.DARK["surface"]) < _luma(theme.LIGHT["surface"])

    light = theme._STATUS_COLORS["light"]
    dark = theme._STATUS_COLORS["dark"]
    assert set(light) == set(dark) == set(STATUS_LABELS)
    for state in light:
        assert _luma(dark[state]) > _luma(light[state]), state


def geometry_of(window) -> dict:
    """会影响操作员"点哪里"的几何量（与 `test_gui_layout_invariant.snapshot` 同一口径）。"""
    table, tree = window.point_table, window.tree
    return {
        "window": (window.width(), window.height()),
        "splitter": window.splitter.sizes(),
        "tree": (tree.geometry().getRect(), tree.columnWidth(0), tree.columnWidth(1),
                 tree.rowHeight(tree.indexFromItem(tree.topLevelItem(0)))),
        "table": (table.geometry().getRect(), table.rowHeight(0),
                  [table.columnWidth(i) for i in range(window.point_model.columnCount())]),
        "channels": window.channels.geometry_snapshot(),
        "packet": window.packet_panel.geometry_snapshot(),
        "tabs": window.tabs.geometry().getRect(),
        "status": window.status.geometry().getRect(),
        "toolbar": (window.toolbar.geometry().getRect(),
                    [window.toolbar.actionGeometry(a).getRect()
                     for a in window.toolbar.actions()]),
    }


def test_switching_the_theme_does_not_move_anything(window, qapp):
    """切深浅只换颜色：几何一个都不许动——包括定宽槽、表格列宽与工具栏按钮。"""
    before = geometry_of(window)
    try:
        assert theme.toggle(qapp) == "dark"
        qapp.processEvents()
        assert qapp.styleSheet() == theme.qss("dark")
        assert geometry_of(window) == before, "切到深色把几何动了"
    finally:
        theme.set_mode("light", qapp)
        qapp.processEvents()
    assert theme.current_mode() == "light"
    assert geometry_of(window) == before, "切回浅色没把几何带回原样"


def test_the_toolbar_switch_flips_the_mode_and_repaints_what_the_qss_cannot(window, qapp):
    """工具栏那个勾选动作：模式、勾选状态、状态色、图标底色要一起对上。

    样式表刷不掉已经画好的 QPixmap，也刷不掉走 QPalette 的状态色——所以切完这两处必须
    由主窗自己重涂，这条就是钉它。
    """
    action = window.act_theme
    row = window.channels.rows()[0]
    assert action.isCheckable() and not action.isChecked()
    try:
        action.trigger()
        qapp.processEvents()
        assert theme.current_mode() == "dark"
        assert action.isChecked(), "切到深色了，工具栏那个勾却没跟上"
        assert qapp.styleSheet() == theme.qss("dark")
        assert not action.icon().isNull()
        assert row.status.palette().color(QtGui.QPalette.WindowText).name() \
            == theme.status_colors("dark")["stopped"].name(), "状态色没跟着换"
        assert window.tree.topLevelItem(0).foreground(1).color().name() \
            == theme.status_colors("dark")["stopped"].name()
    finally:
        action.trigger()
        qapp.processEvents()
    assert theme.current_mode() == "light"
    assert not action.isChecked()
    assert row.status.palette().color(QtGui.QPalette.WindowText).name() \
        == theme.status_colors("light")["stopped"].name()


# ---------- 5. 工具栏 ----------

def test_the_toolbar_keeps_a_word_beside_every_icon(window):
    """图标不许把文字挤掉：只留图标时，现场要靠悬停猜哪个是"全部停止"。"""
    assert window.toolbar.toolButtonStyle() == QtCore.Qt.ToolButtonTextBesideIcon
    buttons = [a for a in window.toolbar.actions() if not a.isSeparator()]
    assert buttons, "工具栏一个动作都没有——这条断言没测到东西"
    for action in buttons:
        assert action.text(), action
        assert not action.icon().isNull(), action.text()
