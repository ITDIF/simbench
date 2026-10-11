"""点位表模型 `gui/point_table.py`：列定义、只有"当前值"可编辑、定点刷新。

这一层值钱的地方是**替身与真 Session 必须说同一种话**：GUI 用例不每次起 socket，靠
`engine/point_source.FakeEngine` 顶上去；替身一旦在 `value_text`/`registers`/错误文案上
和真 Session 分叉，界面用例钉住的就是替身的行为而不是现场的行为——所以这里除了模型自身
的断言，还专门有把两边摆在一起比的一条。

钉住的几件事：

1. **列就是那 10 列**，`原始寄存器` 一眼能核字序（CDAB 的 12.5 是 `0000 4148`），
   `4x` 是现场惯例写法（`conventional()`），裸 0-based 地址另外单独一列。
2. **只有"当前值"可编辑**（α：area/地址/类型属于点表，改要走导入或点位属性面板）。
   Qt 默认 flags 自带 `ItemIsEditable`，所以这里必须**显式清掉**再按列加回去——
   留着默认值，"地址"一格就能被双击改掉，而它和 `overlap`/`unit_duplicated` 校验打架。
3. **刷新只给真变了的行发 `dataChanged`**，条目数没变绝不 `beginResetModel()`：
   reset 会把选中行和滚动位置一起丢掉，那属于"状态变化动了既有可见状态"。
4. **`stale` 走角色，不走文字**：灰显是那一格的*呈现*，改文字就等于把"没读到"
   演成"设备报了这个值"。
"""

import dataclasses

import pytest
from PySide6 import QtCore

from modslave.csv_io_entry import load_point_table
from modslave.engine.point_source import FakeEngine, session_source
from modslave.engine.runner import EngineRunner
from modslave.engine.session import Row, Session
from modslave.gui.point_table import COLUMNS, PointTableModel, conventional
from modslave.project.model import Project, TcpChannel
from tests.conftest import free_port

CSV = "examples/covi-tunnel.csv"


def loaded(port: int | None = None) -> Project:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    if port:
        project.channels[0].port = port
    return project


@pytest.fixture
def engine():
    return FakeEngine(loaded())


@pytest.fixture
def model(qapp, engine):
    m = PointTableModel()
    m.attach(engine)
    return m


@pytest.fixture
def live_session():
    """真 Session + 真起通道：给"替身与真相一致"那两条用例用。

    runner 是这里自己建的，所以 `shutdown()` 之后还要 `runner.stop()`：换成外部传进来
    的 runner 就只能停通道——替别人收线程会把同一条 runner 上别的用例直接打断。
    """
    runner = EngineRunner()
    s = Session(runner=runner)
    yield s
    s.shutdown()                       # 只停自己的通道：runner 不归它
    runner.stop()


def row_of(model, name):
    return next(r for r in range(model.rowCount()) if model.index(r, 0).data() == name)


# ---------- 列与文案 ----------

def test_columns_are_the_agreed_order():
    assert [c.header for c in COLUMNS] == [
        "名称", "区", "地址", "4x", "类型", "字序", "当前值", "原始寄存器", "波形", "只读"]


def test_conventional_addresses_match_the_field_convention():
    """厂商点表上的 40001 那一套：惯例列只由 area+address 推出来，不另存一份。"""
    assert conventional("hr", 0) == "4x00001"
    assert conventional("ir", 4) == "3x00005"
    assert conventional("co", 0) == "0x00001"
    assert conventional("di", 3) == "1x00004"


def test_conventional_rejects_an_area_that_is_not_in_the_model():
    """未知区名不许悄悄落进某一列：那会在表格里显示成一个看着像惯例地址的假数。"""
    with pytest.raises(ValueError, match="unknown area"):
        conventional("xx", 0)


def test_rows_show_value_and_raw_registers(model):
    r = row_of(model, "CO浓度")
    assert model.index(r, 6).data() == "12.5"
    assert model.index(r, 7).data() == "0000 4148"      # CDAB 上线形态，一眼可核
    assert model.index(r, 3).data() == "4x00001"


def test_a_bit_point_renders_as_true_false_not_one_zero(model):
    """位区的当前值用 true/false：主站读的是线圈，界面上写 1/0 会被当成寄存器值。"""
    r = row_of(model, "启动命令")
    assert model.index(r, 6).data() == "false"
    assert model.index(r, 3).data() == "0x00001"


def test_readonly_points_are_marked_in_their_own_column(model):
    r = row_of(model, "CO浓度")
    assert model.index(r, 9).data() == "是"
    assert model.index(row_of(model, "风机状态"), 9).data() == ""


def test_vertical_header_is_the_one_based_row_number(model):
    assert model.headerData(0, QtCore.Qt.Vertical) == "1"
    assert model.headerData(2, QtCore.Qt.Horizontal) == "地址"


# ---------- 可编辑性（α） ----------

def test_only_the_value_column_is_editable(model):
    for col, spec in enumerate(COLUMNS):
        editable = bool(model.flags(model.index(0, col)) & QtCore.Qt.ItemIsEditable)
        assert editable == (spec.header == "当前值"), spec.header


def test_editing_another_column_is_refused(model):
    r = row_of(model, "CO浓度")
    assert model.setData(model.index(r, 0), "改过的名字") is False
    assert model.index(r, 0).data() == "CO浓度"


def test_every_column_stays_selectable_and_enabled(model):
    """列不许因为不可编辑就失去 ItemIsEnabled|Selectable：那是"整列变灰点不动"的假故障。"""
    for col in range(len(COLUMNS)):
        flags = model.flags(model.index(0, col))
        assert flags & QtCore.Qt.ItemIsEnabled
        assert flags & QtCore.Qt.ItemIsSelectable


# ---------- 写值与错误 ----------

def test_edit_writes_through_the_engine(model, engine):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, 6), "37") is True
    assert model.index(r, 6).data() == "37"
    assert engine.writes == [("tcp-5020", 11, "hr", 4, "37")]


def test_bad_edit_is_refused_and_keeps_the_old_text(model):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, 6), "abc") is False
    assert model.index(r, 6).data() == "11"
    assert "uint16 needs an integer" in model.last_error
    assert model.last_error.startswith("风机状态:")


def test_error_message_names_the_point(model):
    r = row_of(model, "风机状态")
    model.setData(model.index(r, 6), "999999")
    assert "风机状态" in model.last_error          # 现场要一眼看出是哪一行
    assert "uint16 out of range" in model.last_error


def test_a_successful_edit_clears_the_previous_error(model, engine):
    r = row_of(model, "风机状态")
    model.setData(model.index(r, 6), "abc")
    assert model.last_error
    assert model.setData(model.index(r, 6), "37") is True
    assert model.last_error == ""


def test_noop_edit_does_not_emit_dataChanged(model):
    r = row_of(model, "风机状态")
    fired: list[int] = []
    model.dataChanged.connect(lambda tl, br, _roles=None: fired.append(tl.row()))
    assert model.setData(model.index(r, 6), "11") is False
    assert fired == []


def test_a_edit_with_surrounding_spaces_is_stripped_before_writing(model, engine):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, 6), "  37  ") is True
    assert engine.writes == [("tcp-5020", 11, "hr", 4, "37")]


# ---------- 刷新 ----------

def test_refresh_touches_only_changed_rows(model, engine):
    """10Hz 刷新不许整表 reset：那会丢选中行与滚动位置。

    `dataChanged(topLeft, bottomRight)` 的两个索引**同一行**：行号就是那一条，列跨度
    必须是从第 0 列到最后一列，否则"原始寄存器""波形"这些兄弟列会留在旧值上。
    """
    fired: list[tuple[int, int, int, int]] = []
    model.dataChanged.connect(lambda tl, br, _roles=None:
                              fired.append((tl.row(), br.row(), tl.column(), br.column())))
    engine.values[("tcp-5020", 11, "hr", 4)] = "12"
    changed = model.refresh()
    assert changed == 1
    r = row_of(model, "风机状态")
    assert fired == [(r, r, 0, len(COLUMNS) - 1)]
    assert model.index(r, 6).data() == "12"
    assert model.index(r, 7).data() == "000C"


def test_a_refresh_with_unchanged_rows_emits_nothing(model, engine):
    fired: list[int] = []
    model.dataChanged.connect(lambda tl, _br, _roles=None: fired.append(tl.row()))
    engine.values[("tcp-5020", 11, "hr", 4)] = "11"      # 写了个和初值一样的值
    assert model.refresh() == 0
    assert fired == []


def test_refresh_resets_only_when_the_row_count_changes(qapp):
    """行数没变绝不 reset（选中行与滚动位置会一起丢），行数真变了才必须 reset 一次。"""
    m = PointTableModel()
    source = TwoRowSource([_row("甲", "1")])
    m.attach(source)
    resets: list[int] = []
    fired: list[int] = []
    m.modelReset.connect(lambda: resets.append(1))
    m.dataChanged.connect(lambda tl, _br, _roles=None: fired.append(tl.row()))
    source.set_rows([_row("甲", "1"), _row("乙", "2")])
    assert m.refresh() == 2
    assert resets == [1] and fired == []
    assert m.rowCount() == 2
    source.set_rows([_row("甲", "9"), _row("乙", "2")])
    assert m.refresh() == 1
    assert resets == [1], "行数没变又 reset 了一次"
    assert fired == [0]


class TwoRowSource:
    """最小 PointSource：条目可换、写永远失败，只用来验角色与"定点刷新 / reset"的分界。"""

    def __init__(self, rows: list[Row]) -> None:
        self._rows = list(rows)
        self.project = Project(channels=[TcpChannel(name="tcp-5020", devices=[])])

    def set_rows(self, rows: list[Row]) -> None:
        self._rows = list(rows)

    def rows(self, channel: str) -> list[Row]:
        return list(self._rows)

    def write(self, channel, unit_id, area, address, text):
        return (False, "source refuses everything")


def _row(name: str, text: str, stale: bool = False, waveform: str = "const",
         source: str = "waveform") -> Row:
    return Row("tcp-5020", "COVI-1", 11, name, "hr", 0, "uint16", "ABCD",
               text, (int(text),), False, waveform, stale, source)


def test_the_takeover_shows_up_in_the_wave_column_without_moving_the_table(qapp):
    """spec §7：被人工接管的点，"波形"那一列显示 `已停`；没人动过的显示波形名。

    列序与列宽一起钉：界面口径是"槽位装不下就改文案，不加宽、不截成省略号"，
    所以 `已停` 必须落在原来那一列里，`只读` 不能因为多了个状态就往后挪一位。
    """
    m = PointTableModel()
    m.attach(TwoRowSource([_row("曲线在跑", "1", waveform="sine"),
                           _row("人工改过", "2", source="manual")]))
    wave = [c.header for c in COLUMNS].index("波形")
    assert COLUMNS[wave].width == 96, "为两个字的文案加宽 = 动了既有布局"
    assert m.index(0, wave).data() == "sine"
    assert m.index(1, wave).data() == "已停"
    assert m.index(1, wave + 1).data() == "", "波形列右边已经不是只读列了"


def test_the_stale_and_wave_columns_do_not_share_a_value(qapp):
    """`stale`（没读到）走角色、`已停`（被接管）走文字：钉的是**同一行两态叠加**。

    现场的形状就是叠加的：通道在跑、点被界面/主站接管（`source="manual"`）、某次回读
    又失败（`stale=True`，那一格显示的是回落的初值）。此时波形列必须仍说得出 `已停`
    （不许被 stale 顶成空白或波形名），当前值必须仍灰得起来（不许被 `已停` 顶掉角色）。
    早先版本这条跑在 `model`/FakeEngine 夹具上，而替身从不产生这两种状态（见
    `point_source.FakeEngine` 的口径）——没有任何一行是 stale 或 manual，"stale 时把波形
    列清空"和"干脆忽略 stale"两种变异全绿，它实际只是在重复别处钉过的事实。这里换成
    真带这两种状态的数据源（与 `test_stale_rows_report_their_role` 同一条 attach 路），
    四种组合逐格对照。
    """
    m = PointTableModel()
    m.attach(TwoRowSource([_row("接管又没读到", "7", waveform="sine", stale=True,
                                source="manual"),
                           _row("只被接管", "8", waveform="sine", source="manual"),
                           _row("只没读到", "9", waveform="sine", stale=True),
                           _row("谁都没动", "1", waveform="sine")]))
    wave = [c.header for c in COLUMNS].index("波形")
    stale_role = m.stale_role()
    # 叠加行：两个事实必须同时说得出，谁也不许顶掉谁
    assert m.index(0, wave).data() == "已停", "stale 把接管文案顶掉了"
    assert m.data(m.index(0, 6), stale_role) is True, "接管把 stale 角色顶掉了"
    assert m.index(0, 6).data() == "7", "初值回落的文字不许被两种状态改掉"
    # 单态行各自只说自己那一半，互不越界
    assert m.index(1, wave).data() == "已停"
    assert m.data(m.index(1, 6), stale_role) is False, "接管自己不算回读失败"
    assert m.index(2, wave).data() == "sine", "stale 不许去动波形列的文字"
    assert m.data(m.index(2, 6), stale_role) is True
    assert m.index(3, wave).data() == "sine"
    assert m.data(m.index(3, 6), stale_role) is False


def test_the_fake_keeps_every_field_it_does_not_edit(engine):
    """替身换"当前值"时是**重建**一个 Row，所以 `Row` 每加一列都得跟着带过去。

    `source` 是这次新加的、第一屏两边又都是 `waveform`——正是最容易在重建时悄悄丢掉的那
    一列：丢了界面用例照样全绿，而现场（真 Session）改完值之后那一格会自己从"已停"跳回
    波形名，等于界面在撤销一次没人点过的"恢复波形"。
    """
    engine._rows = [dataclasses.replace(row, source="manual") for row in engine._rows]
    assert {r.source for r in engine.rows("tcp-5020")} == {"manual"}
    engine.values[("tcp-5020", 11, "hr", 4)] = "12"        # 走 rows() 的重建路径
    edited = next(r for r in engine.rows("tcp-5020") if r.address == 4)
    assert (edited.value_text, edited.registers) == ("12", (12,))
    assert edited.source == "manual", "重建 Row 时把归属丢了"
    assert edited.readonly is False and edited.waveform == "const", "串列：位置参数顺序错了"


def test_stale_rows_report_their_role(qapp):
    """stale 通过角色送出去，`当前值` 的显示文字仍是回落到初值那一份。"""
    m = PointTableModel()
    m.attach(TwoRowSource([_row("可改的量", "1"), _row("没读到的量", "7", stale=True)]))
    stale = m.stale_role()
    assert m.data(m.index(0, 6), stale) is False
    assert m.data(m.index(1, 6), stale) is True
    assert m.index(1, 6).data() == "7"


def test_a_stale_change_alone_counts_as_a_changed_row(qapp):
    """回读从失败恢复：文字没变（都回落初值），但 stale 变了，那一行必须重画。"""
    m = PointTableModel()
    source = TwoRowSource([_row("没读到的量", "7", stale=True)])
    m.attach(source)
    fired: list[int] = []
    m.dataChanged.connect(lambda tl, _br, _roles=None: fired.append(tl.row()))
    source.set_rows([_row("没读到的量", "7", stale=False)])
    assert m.refresh() == 1
    assert fired == [0]


# ---------- 数据源 ----------

def test_empty_project_is_refused_not_blank(model, qapp):
    m = PointTableModel()
    with pytest.raises(ValueError, match="no channel"):
        m.attach(FakeEngine(Project(channels=[])))


def test_refresh_before_attach_is_harmless(qapp):
    m = PointTableModel()
    assert m.rowCount() == 0
    assert m.refresh() == 0


def test_attach_uses_the_first_channel_by_default(model):
    """默认视图给"全部"：attach 不选通道时就是点表里的第一个通道，不做上下文跟随。"""
    assert model.channel() == "tcp-5020"
    assert model.rowCount() == 9


def test_a_real_session_satisfies_the_same_protocol(qapp, live_session):
    """`session_source()` 不是装饰：真 Session 必须真的能被模型 attach。"""
    live_session.load(loaded(free_port()))
    live_session.start_channel("tcp-5020")
    m = PointTableModel()
    m.attach(session_source(live_session))
    assert m.rowCount() == 9
    assert m.index(row_of(m, "CO浓度"), 7).data() == "0000 4148"
    assert m.index(row_of(m, "CO浓度"), 6).data() == "12.5"


def test_the_fake_engine_refuses_with_the_same_words_as_the_session(engine, qapp,
                                                                      live_session):
    """替身与真 Session 的**错误文案**必须一字不差。

    界面只钉得住一种说法：替身这里多一个前缀、少一个类型名，用例全绿而现场（真
    Session）给出的话完全变样，运维就会当成模拟器的 bug 去调。
    """
    live_session.load(loaded(free_port()))
    live_session.start_channel("tcp-5020")
    for address, text in [(4, "abc"), (4, "999999"), (30, "1"), (0, "")]:
        fake = engine.write("tcp-5020", 11, "hr", address, text)
        real = live_session.write("tcp-5020", 11, "hr", address, text)
        assert fake == real, (address, text, fake, real)


def test_the_fake_engine_and_a_real_session_render_the_same_first_screen(qapp,
                                                                         live_session):
    """同一份 CSV，替身与真 Session 各渲染一次首屏：两套真相就是从这里长出来的。"""
    fake_rows = FakeEngine(loaded()).rows("tcp-5020")
    live_session.load(loaded(free_port()))
    live_session.start_channel("tcp-5020")
    real_rows = live_session.rows("tcp-5020")
    assert [(r.name, r.area, r.address) for r in real_rows] == \
        [(r.name, r.area, r.address) for r in fake_rows]
    assert [(r.name, r.value_text, r.registers) for r in real_rows] == \
        [(r.name, r.value_text, r.registers) for r in fake_rows], \
        "value_text/registers 分叉：界面用例钉的是替身而不是现场"
    assert [r.stale for r in real_rows] == [r.stale for r in fake_rows]
    # 归属也同一个口径：谁都没写过值时，整列都是波形在跑（`已停` 一个都不许有）
    assert [r.source for r in real_rows] == [r.source for r in fake_rows] == \
        ["waveform"] * len(real_rows)
