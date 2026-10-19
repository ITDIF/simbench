"""报文面板 `gui/packet_view.py`：四个平铺过滤标签 + 暂停/清除 + 一列表格。

这一层把用户的界面口径直接钉成断言，三条都是他逐条当 bug 报过的：

1. **≤10 项的可枚举选项用平铺可勾选标签，不用下拉框**——所以 `filter_combo` 必须是
   `None`，而且整个面板里**一个** `QComboBox` 都不许出现（换个地方塞下拉等于没改）。
2. **默认视图给"全部"**，不是"跟随当前通道"。
3. **状态变化不许改变布局尺寸、也不许让既有可见控件消失**：切四种过滤器、暂停/继续、
   计数从 0 帧涨到 40 帧，几何快照必须逐字节相等，`isVisible()` 序列必须逐项相等。
   重绘只能改文字与行数，控件靠 `disabled`/定宽槽位，不靠 `setVisible` 增删。
4. **可伸缩区块不写死高度**：表格拿布局弹性，`maximumHeight` 不许是有限值——
   报文窗涨高把同栏兄弟面板挤没，他在网页端为这件事打过两次回。

刷新口径：store 的通知**只置脏标记**，重绘由主窗的 10Hz 心跳调 `refresh_view()` 统一
驱动。每来一帧就重排一次表格，在总线上扫 247 台设备时会把界面刷成卡顿（M2 前置实测
的批量 dataChanged 是 0.34ms/200 行，那是模型的成本，视图重排的不是）。
"""

import pytest
from PySide6 import QtCore, QtWidgets

from simbench.backend.modbus_tcp import parse_packet
from simbench.engine.packets import (
    ALL,
    EXCEPTIONS,
    FILTERS,
    REQUESTS,
    RESPONSES,
    PacketStore,
)
from simbench.events import PacketEvent
from simbench.gui.packet_view import PacketPanel

COLUMNS = ("时间", "方向", "通道", "unit", "fc", "摘要", "原始字节")


def evt(direction="rx", unit=11, fc=3, mono=0.0, channel="tcp-5020"):
    return PacketEvent(channel, direction, bytes([0, 1, unit, fc]), unit, fc,
                       f"unit {unit} fc {fc}", mono)


@pytest.fixture
def store():
    return PacketStore(capacity=50)


@pytest.fixture
def panel(qapp, store):
    w = PacketPanel(store)
    w.resize(600, 220)                 # 弹性布局下的一个具体尺寸：offscreen 下几何是真的
    w.show()
    qapp.processEvents()
    yield w
    w.close()
    w.deleteLater()
    qapp.processEvents()


def _click(panel, name: str) -> None:
    button = next(b for b in panel.filter_group.buttons() if b.text() == name)
    button.click()


def visibility_snapshot(widget) -> list[tuple[int, bool]]:
    """面板里**每一个**控件的 (身份, 可见性)。控件被增删 → 身份集合变；被藏起来 → 布尔变。"""
    return sorted((id(w), w.isVisible())
                  for w in widget.findChildren(QtWidgets.QWidget))


# ---------- 口径 1/2：平铺标签、默认全部 ----------

def test_filter_uses_flat_toggle_labels_not_a_combobox(panel):
    """≤10 个可枚举选项要平铺标签：下拉的选项不点开看不见，切换还要两次点击。"""
    assert panel.filter_combo is None
    assert not panel.findChildren(QtWidgets.QComboBox), "换了个地方塞下拉等于没改"
    labels = panel.filter_labels()
    assert [t.text() for t in labels] == ["全部", "请求", "响应", "异常"]
    assert all(t.isCheckable() for t in labels)
    group = panel.filter_group
    assert group.exclusive()


def test_filter_labels_are_the_store_filter_names(panel):
    """标签文案就是 `engine.packets.FILTERS`，不是界面自己抄的一份：抄了就会漂移。"""
    assert [t.text() for t in panel.filter_labels()] == list(FILTERS)


def test_default_filter_is_all(panel, store):
    assert panel.filter_group.checkedButton().text() == ALL
    assert store.active_filter == ALL
    assert panel.filter_labels()[0].isChecked()


def test_clicking_a_label_changes_the_visible_rows(panel, store, qapp):
    store.add(evt("rx", 11, 3))
    store.add(evt("tx", 77, 0x83))
    qapp.processEvents()
    assert panel.row_count() == 2
    _click(panel, EXCEPTIONS)
    qapp.processEvents()
    assert panel.row_count() == 1
    assert panel.visible_rows()[0].unit_id == 77
    _click(panel, REQUESTS)
    qapp.processEvents()
    assert [p.unit_id for p in panel.visible_rows()] == [11]


def test_the_exception_label_is_not_empty_for_a_real_backend_frame(panel, store, qapp):
    """**F 表口径**：异常判据必须认 backend `parse_packet` 的真形状。

    `parse_packet` 把 0x80 脱掉放进 function_code、异常信息只写在 `parsed` 文案里，
    只按 `fc & 0x80` 判的话"异常"标签对真从站恒是 0 条——从站明明刚回了异常码 2，
    界面却说没发生异常。这条直接从 `parse_packet` 的真产出灌进来，谁退回按位判就红。
    """
    raw = b"\x00\x03\x00\x00\x00\x03\x4d\x83\x04"       # unit 77, fc 0x83, code 4
    unit, function, summary = parse_packet(raw)
    assert function == 3 and "EXCEPTION" in summary      # M1 已钉的形状
    store.add(PacketEvent("tcp-5020", "tx", raw, unit, function, summary, 0.0))
    qapp.processEvents()
    _click(panel, EXCEPTIONS)
    qapp.processEvents()
    assert panel.row_count() >= 1, "异常标签是空的：判据没认 parse_packet 的脱位形状"
    assert panel.visible_rows()[0].parsed == summary
    _click(panel, RESPONSES)
    assert panel.row_count() == 1, "异常响应同时属于响应桶，过滤响应时不该看不见它"


def test_the_normal_response_is_not_counted_as_an_exception(panel, store, qapp):
    """反方向的假阳性：正常响应里出现 "EXCEPTION" 这个词的可能没有，但桶不能多收。"""
    unit, function, summary = parse_packet(b"\x00\x03\x00\x00\x00\x02\x0b\x03\x01")
    store.add(PacketEvent("tcp-5020", "tx", b"\x0b\x03\x01", unit, function, summary, 0.0))
    _click(panel, EXCEPTIONS)
    assert panel.row_count() == 0
    assert panel.visible_rows() == []


# ---------- 暂停与清除 ----------

def test_pausing_stops_rows_from_growing_but_keeps_the_table_readable(panel, store, qapp):
    store.add(evt(mono=1.0))
    qapp.processEvents()
    panel.pause_button.click()
    store.add(evt(mono=2.0))
    qapp.processEvents()
    assert panel.row_count() == 1
    assert panel.pause_button.text() == "继续"      # 文字换、位置不动
    panel.pause_button.click()
    store.add(evt(mono=3.0))
    qapp.processEvents()
    assert panel.row_count() >= 2


def test_pause_and_clear_buttons_are_there_from_the_start(panel):
    """常驻控件：不许"跑起来才出现一个暂停按钮"，那会把同一行的其它控件推走。"""
    assert panel.pause_button.isVisible()
    assert panel.clear_button.isVisible()
    assert panel.table.isVisible()


def test_clear_empties_view_and_store_keeps_working(panel, store, qapp):
    store.add(evt())
    qapp.processEvents()
    panel.clear_button.click()
    qapp.processEvents()
    assert panel.row_count() == 0 and store.rows() == []
    store.add(evt())
    qapp.processEvents()
    assert panel.row_count() == 1


# ---------- 口径 3：几何与可见性恒定 ----------

def test_geometry_is_stable_across_filter_and_pause(panel, store, qapp):
    """状态变化不许挪布局：宽/高/按钮位置在四种过滤器与暂停两态下都恒定。

    行数**是**会变的（切"异常"就该只剩一条），变的只有内容。这条钉的是"内容变、几何
    不变"，所以每个状态都顺手把行数断出来——免得将来有人靠藏掉表格来"稳定几何"。
    """
    for i in range(30):
        store.add(evt("tx" if i % 3 else "rx", mono=float(i)))
    store.add(PacketEvent("tcp-5020", "tx", b"\x4d\x83\x02", 77, 3,
                          "unit 77 fc 3 EXCEPTION 2", 99.0))
    panel.refresh_view()
    qapp.processEvents()
    baseline = panel.geometry_snapshot()
    assert panel.row_count() == 31, "先确认这个快照是在有内容时取的"
    for name, expected in [(ALL, 31), (REQUESTS, 10), (RESPONSES, 21), (EXCEPTIONS, 1)]:
        _click(panel, name)
        panel.refresh_view()
        qapp.processEvents()
        assert panel.row_count() == expected, name
        assert panel.geometry_snapshot() == baseline, f"切到 {name} 后布局变了"
    panel.pause_button.click()
    panel.refresh_view()
    qapp.processEvents()
    assert panel.geometry_snapshot() == baseline, "暂停后布局变了"
    panel.pause_button.click()
    panel.refresh_view()
    qapp.processEvents()
    assert panel.geometry_snapshot() == baseline


def test_the_widgets_do_not_disappear_across_states(panel, store, qapp):
    """控件不消失：状态切换前后每个控件的 isVisible() 逐项相等（口径 3 的后半句）。

    先把缓冲灌满再切，四种过滤器的行数是真的在变（31/10/21/1）——空表上取快照等于
    什么都没测。快照取**整棵子控件树**，连表格自己的竖直滚动条也算：切到"异常"只剩一帧时
    它会自动收掉，屏幕上就是一个本来在看的元素没了，同时视口变宽、列宽重新排一次，
    两样都是口径 3 禁止的。所以面板把竖直滚动条设成常驻（见 `packet_view`）。
    """
    for i in range(30):
        store.add(evt("tx" if i % 3 else "rx", mono=float(i)))
    store.add(PacketEvent("tcp-5020", "tx", b"\x4d\x83\x02", 77, 3,
                          "unit 77 fc 3 EXCEPTION 2", 99.0))
    panel.refresh_view()
    qapp.processEvents()
    before = visibility_snapshot(panel)
    baseline = panel.geometry_snapshot()
    assert panel.row_count() == 31, "先确认快照是在有内容时取的"
    for paused in (False, True):
        if paused != panel.pause_button.isChecked():
            panel.pause_button.click()
        for name in FILTERS:
            _click(panel, name)
            panel.refresh_view()
            qapp.processEvents()
            tag = f"{name}{'/暂停' if paused else ''}"
            assert visibility_snapshot(panel) == before, f"切到 {tag} 时控件增删或消失了"
            assert panel.geometry_snapshot() == baseline, f"切到 {tag} 后布局变了"


def test_count_label_text_changes_without_moving_anything(panel, store, qapp):
    """"0 帧"→"30 帧" 只改文字：定宽槽位保证它不推挤右边的控件。"""
    baseline = panel.geometry_snapshot()
    for i in range(30):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    qapp.processEvents()
    assert panel.count_label.text() == "30 帧"
    assert panel.geometry_snapshot() == baseline


def test_filter_labels_never_clip_their_text(panel):
    """槽位装不下要改文案，不许加宽、不许截断省略号（口径 4）。"""
    for button in panel.filter_labels():
        advance = button.fontMetrics().horizontalAdvance(button.text())
        assert advance + 12 <= button.width(), (button.text(), advance, button.width())
        assert "…" not in button.text() and "..." not in button.text()


# ---------- 口径 5：不写死高度 ----------

def test_the_table_is_the_elastic_block_not_a_hardcoded_height(panel):
    """表格拿满剩余高度：写死 max-height 会把同栏兄弟面板挤没。"""
    layout = panel.layout()
    assert layout.stretch(layout.indexOf(panel.table)) > 0
    assert panel.maximumHeight() >= 16777215, "面板自己也不许被限高"
    assert panel.table.maximumHeight() >= 16777215
    assert panel.table.minimumHeight() < panel.table.maximumHeight()


# ---------- 重绘与滚动 ----------


# ---------- 差集通知（O4：只通知真变了的那一段）----------

def test_new_packets_arrive_as_an_insert_not_a_reset(panel, store, qapp):
    """纯追加只发 `rowsInserted`：`beginResetModel` 每 100ms 清一次选中行与键盘位置，
    操作员在跑流量时根本选不中一条报文（这是用户会当成"卡住"的那类毛病）。"""
    resets: list[int] = []
    inserts: list[tuple] = []
    panel.model.modelReset.connect(lambda: resets.append(1))
    panel.model.rowsInserted.connect(lambda _p, first, last: inserts.append((first, last)))

    store.add(evt(mono=0.0))
    panel.refresh_view()
    assert resets == [1], "第一屏没有内容时本来就该 reset"
    resets.clear()

    for i in range(1, 6):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    assert resets == [], "只是追加了几帧，整表被 reset 了"
    assert inserts == [(1, 5)], inserts


def test_a_full_buffer_reports_the_dropped_head_and_the_new_tail(panel, store, qapp):
    """缓冲写满之后：只报"头上掉了 1 条、尾巴来了 1 条"，**不许**整表重画。

    旧写法在这里逐行发 `dataChanged`（5000 行/跳、实测 65.6ms 一跳），而且内容整体前移，
    选中行看着"自己变了"。
    """
    removed: list[tuple] = []
    inserts: list[tuple] = []
    resets: list[int] = []
    panel.model.rowsRemoved.connect(lambda _p, first, last: removed.append((first, last)))
    panel.model.rowsInserted.connect(lambda _p, first, last: inserts.append((first, last)))
    panel.model.modelReset.connect(lambda: resets.append(1))

    for i in range(store.capacity):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    resets.clear()

    store.add(evt(mono=99.0))
    panel.refresh_view()
    assert resets == [], "缓冲写满之后每跳都在整表重画"
    assert removed == [(0, 0)] and inserts == [(store.capacity - 1, store.capacity - 1)]
    assert panel.model.rowCount() == store.capacity


def test_the_selection_follows_the_event_not_the_row_number(panel, store, qapp):
    """选中行跟着**那条报文**走：内容前移之后选中的还是同一帧（不是"第 N 行"）。"""
    for i in range(store.capacity):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    panel.table.selectRow(2)
    qapp.processEvents()
    chosen = panel.model._rows[2]

    store.add(evt(mono=99.0))
    panel.refresh_view()
    qapp.processEvents()

    selected = panel.table.selectionModel().selectedRows()
    assert selected, "选中行被清掉了（每跳 reset 的老毛病）"
    assert panel.model._rows[selected[0].row()] is chosen, "选中的变成了另一帧"


def test_switching_the_filter_still_resets_the_model(panel, store, qapp):
    """过滤器一换，视图不再是同一份缓冲的子集——那种情况必须整表换（差集算不出来）。"""
    for i in range(5):
        store.add(evt(direction="rx" if i % 2 else "tx", mono=float(i)))
    panel.refresh_view()
    resets: list[int] = []
    panel.model.modelReset.connect(lambda: resets.append(1))
    panel.set_filter(REQUESTS)
    qapp.processEvents()
    panel.refresh_view()
    assert resets == [1], "换过滤器没有整表换：行会串"


def test_clearing_the_store_empties_the_model(panel, store, qapp):
    for i in range(5):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    assert panel.model.rowCount() == 5
    panel._on_clear()
    panel.refresh_view()
    assert panel.model.rowCount() == 0



def test_view_refresh_is_throttled_to_one_repaint_per_batch(panel, store, qapp):
    """来一条刷一次太贵；panel 只在 store 通知时打脏标记，由主窗的 10Hz 心跳统一刷。"""
    before = panel.repaint_count
    for i in range(10):
        store.add(evt(mono=float(i)))
    qapp.processEvents()
    assert panel.repaint_count == before, "panel 在 store 通知里直接重绘了"
    panel.refresh_view()
    assert panel.repaint_count == before + 1
    panel.refresh_view()
    assert panel.repaint_count == before + 1, "没脏就不该再重绘一次"


def test_new_packet_arrival_does_not_steal_scroll_when_user_is_reading_bottom_up(
        panel, store, qapp):
    """跟随新行是默认，但用户往上翻过就不硬拽回去（滚动条位置也属于"既有可见状态"）。"""
    for i in range(40):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    qapp.processEvents()
    bar = panel.table.verticalScrollBar()
    assert bar.maximum() > 0, "这个尺寸下滚动条没范围，断言会假通过"
    bar.setValue(0)                                  # 人工翻到顶
    qapp.processEvents()
    user_pos = bar.value()
    store.add(evt(mono=99.0))
    panel.refresh_view()
    qapp.processEvents()
    assert bar.value() == user_pos, "用户在看旧帧时被拽到底部"


def test_scroll_follows_the_tail_while_the_user_is_at_the_bottom(panel, store, qapp):
    """默认跟着新帧走：报文窗的意义就是"最后那一帧在屏幕上"。"""
    for i in range(40):
        store.add(evt(mono=float(i)))
    panel.refresh_view()
    qapp.processEvents()
    bar = panel.table.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum(), "在底部时被甩开了"
    store.add(evt(mono=99.0))
    panel.refresh_view()
    qapp.processEvents()
    assert bar.value() == bar.maximum(), "用户一直在底部却没跟着新帧"


def test_the_table_shows_the_agreed_columns(panel, store, qapp):
    store.add(evt("tx", 77, 0x83, mono=1.25))
    panel.refresh_view()
    qapp.processEvents()
    model = panel.table.model()
    assert [model.headerData(c, QtCore.Qt.Horizontal) for c in range(model.columnCount())] \
        == list(COLUMNS)
    assert model.index(0, 0).data() == "1.250"
    assert model.index(0, 1).data() == "TX"
    assert model.index(0, 2).data() == "tcp-5020"
    assert model.index(0, 3).data() == "77"
    assert model.index(0, 4).data() == "0x83"
    assert model.index(0, 5).data() == "unit 77 fc 131"


def test_a_closed_panel_stops_being_notified(qapp, store):
    """关窗必须退订：悬空回调往已销毁的 widget 上刷，症状是"报文窗自己崩了"。"""
    w = PacketPanel(store)
    w.show()
    qapp.processEvents()
    before = w.repaint_count
    w.close()
    store.add(evt(mono=5.0))
    assert w.refresh_view() == 0
    assert w.repaint_count == before


def test_the_panel_survives_a_store_that_notifies_after_it_is_hidden(qapp, store):
    """隐藏（不是关闭）时来报文不该崩，也不该在看不见的情况下把行数丢掉。"""
    w = PacketPanel(store)
    w.show()
    qapp.processEvents()
    store.add(evt(mono=1.0))
    w.refresh_view()
    w.hide()
    store.add(evt(mono=2.0))
    w.refresh_view()
    w.show()
    qapp.processEvents()
    assert w.row_count() == 2


def test_the_packet_table_is_centred_like_the_point_table(panel, store):
    """报文表的数据与表头都居中（2026-10-19 用户现场口径）。"""
    store.add(evt())
    panel.refresh_view()
    want = int(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
    model = panel.table.model()
    assert model.rowCount() == 1
    assert [model.index(0, column).data(QtCore.Qt.TextAlignmentRole)
            for column in range(model.columnCount())] == [want] * model.columnCount()
    assert int(panel.table.horizontalHeader().defaultAlignment()) == want
