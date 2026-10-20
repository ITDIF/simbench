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
from PySide6 import QtCore, QtGui, QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.point_source import FakeEngine, session_source
from simbench.engine.runner import EngineRunner
from simbench.engine.session import Row, Session
from simbench.gui.point_table import (
    ADDRESS_COLUMN,
    AREA_COLUMN,
    COLUMNS,
    CONVENTION_COLUMN,
    DEVICE_COLUMN,
    NAME_COLUMN,
    RAW_COLUMN,
    READONLY_COLUMN,
    VALUE_COLUMN,
    PointTableModel,
    PointTableView,
    conventional,
)
from simbench.project.model import Project, TcpChannel
from simbench.project.validate import validate_project
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
    return next(r for r in range(model.rowCount()) if model.index(r, NAME_COLUMN).data() == name)


# ---------- 列与文案 ----------

def test_every_column_is_centred_like_the_header(model):
    """表头与数据**都居中**（2026-10-17 用户口径："数据没有居中"）。

    改之前是"地址/点表地址两列右对齐、其余走 Qt 默认左对齐"，而表头本来就是居中的（实测
    `defaultAlignment` = AlignHCenter|AlignVCenter）——表头与数据各占一边，看着不齐。
    这条把两边钉在同一个值上：哪天有人只改一边，这里就红。
    """
    want = int(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
    roles = [model.data(model.index(0, i), QtCore.Qt.TextAlignmentRole)
             for i in range(len(COLUMNS))]
    assert roles == [want] * len(COLUMNS), roles
    view = PointTableView()
    assert int(view.horizontalHeader().defaultAlignment()) == want, \
        "表头与数据不在同一种对齐上，又会出现「表头居中、数据靠边」"


def test_columns_are_the_agreed_order():
    # 第三列叫"点表地址"而不是"4x"：这一列打的值随区变（4x/3x/1x/0x），
    # S7 上还算不出来（`—`）——"4x"这个词只在 Modbus 的 hr 区成立。
    assert [c.header for c in COLUMNS] == [
        "设备", "名称", "区", "地址", "点表地址", "类型", "字序", "当前值", "原始寄存器",
        "波形", "只读"]


def test_the_columns_are_sized_for_their_widest_content():
    """十一列的宽度按**各自最宽的内容**给（本机 Microsoft YaHei UI 9pt 实测量出来的）。

    两条曾经真被截的形状是这次重排的理由：点位名 12 个中文字要 194px（列只有 150，会被
    Qt 截成 `…`），S7 的高 DB 号 `db4095` 要 51px（"区"只有 46，最后一位没了）。同时
    `4x`/类型/波形/只读四列一共虚占 120px——让给名字与区。

    与"当前值/原始寄存器"那两条同一个写法：只钉**前提与字面量**，不拿 `fontMetrics()`
    现量——offscreen 那套兜底字体把每个字形按 1em 算（17 位数字量出来 221px），照它加宽会
    得到一个部署环境里宽得离谱的表。
    """
    widths = {c.header: c.width for c in COLUMNS}
    assert widths == {"设备": 160, "名称": 250, "区": 56, "地址": 56, "点表地址": 72,
                      "类型": 58, "字序": 60, "当前值": 132, "原始寄存器": 134,
                      "波形": 72, "只读": 40}
    # 各列"最宽内容"的文字量级（前提，可读性比数字本身重要）：
    assert len("db4095") == 6                     # S7 的 DB 号上限 4095，区那一列要装得下
    assert len("72623859790382856") == 17         # 当前值最宽的那个 uint64
    assert len("0000 0000 4148 4E28") == 19       # 原始寄存器最宽的那种（4 格）


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


def test_the_device_column_says_which_device_each_row_belongs_to(model):
    """点表按设备分类：每一行都自带**设备名 + unit**（2026-10-19 用户口径）。

    为什么 unit 也要在：一条 485 总线上同名设备并不罕见，Modbus 认的是 unit id；
    只写设备名时两台"照明"会分不清。
    """
    assert model.index(row_of(model, "CO浓度"), DEVICE_COLUMN).data() == "COVI-1 (unit 11)"
    assert model.index(row_of(model, "回路状态"), DEVICE_COLUMN).data() == \
        "照明回路-1 (unit 12)"


def test_attaching_one_device_shows_only_that_devices_points(model):
    """设备树上点某台设备 = 表只留那一台的点；不给 unit 才是整条通道。"""
    assert model.rowCount() == 9, "默认应当显示这一条通道的全部点位"
    model.attach(model._source, "tcp-5020", unit=12)
    assert [model.index(r, NAME_COLUMN).data() for r in range(model.rowCount())] == \
        ["回路状态"]
    assert model.unit() == 12
    model.attach(model._source, "tcp-5020")
    assert model.rowCount() == 9 and model.unit() is None


def test_rows_show_value_and_raw_registers(model):
    r = row_of(model, "CO浓度")
    assert model.index(r, VALUE_COLUMN).data() == "12.5"
    assert model.index(r, RAW_COLUMN).data() == "0000 4148"      # CDAB 上线形态，一眼可核
    assert model.index(r, CONVENTION_COLUMN).data() == "4x00001"


def test_a_bit_point_renders_as_true_false_not_one_zero(model):
    """位区的当前值用 true/false：主站读的是线圈，界面上写 1/0 会被当成寄存器值。"""
    r = row_of(model, "启动命令")
    assert model.index(r, VALUE_COLUMN).data() == "false"
    assert model.index(r, CONVENTION_COLUMN).data() == "0x00001"


def test_readonly_points_are_marked_in_their_own_column(model):
    r = row_of(model, "CO浓度")
    assert model.index(r, READONLY_COLUMN).data() == "是"
    assert model.index(row_of(model, "风机状态"), READONLY_COLUMN).data() == ""


def test_vertical_header_is_the_one_based_row_number(model):
    assert model.headerData(0, QtCore.Qt.Vertical) == "1"
    assert model.headerData(ADDRESS_COLUMN, QtCore.Qt.Horizontal) == "地址"


# ---------- 带着错装载的行：算不出那一格，但整行必须还在 ----------

DIRTY_CSV = """\
channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
tcp-1,D,1,坏点,holding,0,uint16,ABCD,7,const,,false
tcp-1,D,1,好点,hr,1,uint16,ABCD,8,const,,false
"""


@pytest.fixture
def dirty_table(qapp, tmp_path):
    """一张**带着 error 装载**的真表：`Session.load`/spec §8 允许，操作员要开的就是它。

    走 `load_point_table` 而不是手搓 `Row`：`[area] tcp-1/D/坏点 (line 2)` 这条判定的
    定位串里有 CSV 行号，只有真读出来的工程才带着它——判定与显示得对同一份事实说话。
    """
    path = tmp_path / "dirty.csv"
    path.write_text(DIRTY_CSV, encoding="utf-8")
    project, _notes, issues = load_point_table(str(path))
    table = PointTableModel()
    table.attach(FakeEngine(project))
    return table, project, issues


def test_a_row_with_an_unknown_area_still_renders_every_column(dirty_table):
    """`4x` 是一行里唯一**算出来**的格子，它抛就等于整行抛。

    `4x` 那一格是按区现算的（`conventional` 会抛），而 `data()` 一格一格地问——实测
    修法之前 10 列全报 `ValueError: unknown area 'holding'`，Qt 只能把那一行画成空白：
    "改完再启动"这条路断在表格里，而不是断在属性面板里（面板那一侧上一轮已经给了 `—`）。
    算不出就给 `—`（与 `_update_convention` 同一个标记），一个字都不猜。
    """
    table, _project, _issues = dirty_table
    bad = next(r for r in range(table.rowCount()) if table.row_at(r).area == "holding")
    shown = {spec.header: table.index(bad, col).data()
             for col, spec in enumerate(COLUMNS)}      # 每一格都得读得出来，不许抛
    assert len(shown) == len(COLUMNS)
    assert all(v is not None for v in shown.values()), \
        "兜成 None 也是空白行：Qt 画不出来的现场症状和抛异常一模一样"
    assert shown["点表地址"] == "—", "未知区名算不出惯例地址：给标记，不许留空也不许猜一个前缀"
    assert shown["名称"] == "坏点", "整行的其他格子本来就是好的，别一起吞掉"
    assert shown["区"] == "holding", "脏区名原样显示：显示成 hr 就是把点表里的错洗白了"
    assert shown["当前值"] == "7" and shown["只读"] == ""


def test_the_good_row_of_a_dirty_table_still_renders_as_usual(dirty_table):
    """放宽只针对算不出的那一格：合法行一个字节都不许变。

    写成"整行 except → 一律给 `—`"这一条就先红——那种修法把 10 列的显示换成了 10 列的
    自证，好行也跟着说不清自己了。
    """
    table, _project, _issues = dirty_table
    assert [table.index(1, col).data() for col in
            (NAME_COLUMN, AREA_COLUMN, CONVENTION_COLUMN, VALUE_COLUMN,
             RAW_COLUMN)] == ["好点", "hr", "4x00002", "8", "0008"]


def test_showing_a_dirty_row_does_not_make_it_valid(dirty_table):
    """显示层容忍 ≠ 判定放宽：那一格能读出来，不代表这一行能启动。

    判定只有 `validate_project` 一份这一条纪律，在"表格里也画得出来"之后最容易被人
    顺手删掉（画得出来看着就像没病了），所以这里把两条并排钉在一起。
    """
    table, project, issues = dirty_table
    assert table.rowCount() == 2
    errors = [str(i) for i in validate_project(project) if i.level == "error"]
    assert errors == ["[area] tcp-1/D/坏点 (line 2): unknown area 'holding'"]
    assert [str(i) for i in issues] == errors, "装载时与事后各算一遍，两遍必须一样严"


# ---------- 可编辑性（α） ----------

def test_only_the_value_column_is_editable(model):
    for col, spec in enumerate(COLUMNS):
        editable = bool(model.flags(model.index(0, col)) & QtCore.Qt.ItemIsEditable)
        assert editable == (spec.header == "当前值"), spec.header


def test_editing_another_column_is_refused(model):
    r = row_of(model, "CO浓度")
    assert model.setData(model.index(r, NAME_COLUMN), "改过的名字") is False
    assert model.index(r, NAME_COLUMN).data() == "CO浓度"


def test_every_column_stays_selectable_and_enabled(model):
    """列不许因为不可编辑就失去 ItemIsEnabled|Selectable：那是"整列变灰点不动"的假故障。"""
    for col in range(len(COLUMNS)):
        flags = model.flags(model.index(0, col))
        assert flags & QtCore.Qt.ItemIsEnabled
        assert flags & QtCore.Qt.ItemIsSelectable


# ---------- 写值与错误 ----------

def test_edit_writes_through_the_engine(model, engine):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, VALUE_COLUMN), "37") is True
    assert model.index(r, VALUE_COLUMN).data() == "37"
    assert engine.writes == [("tcp-5020", 11, "hr", 4, "37", None)]


def test_bad_edit_is_refused_and_keeps_the_old_text(model):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, VALUE_COLUMN), "abc") is False
    assert model.index(r, VALUE_COLUMN).data() == "11"
    assert "uint16 needs an integer" in model.last_error
    assert model.last_error.startswith("风机状态:")


def test_error_message_names_the_point(model):
    r = row_of(model, "风机状态")
    model.setData(model.index(r, VALUE_COLUMN), "999999")
    assert "风机状态" in model.last_error          # 现场要一眼看出是哪一行
    assert "uint16 out of range" in model.last_error


def test_a_successful_edit_clears_the_previous_error(model, engine):
    r = row_of(model, "风机状态")
    model.setData(model.index(r, VALUE_COLUMN), "abc")
    assert model.last_error
    assert model.setData(model.index(r, VALUE_COLUMN), "37") is True
    assert model.last_error == ""


def test_noop_edit_does_not_emit_dataChanged(model):
    r = row_of(model, "风机状态")
    fired: list[int] = []
    model.dataChanged.connect(lambda tl, br, _roles=None: fired.append(tl.row()))
    assert model.setData(model.index(r, VALUE_COLUMN), "11") is False
    assert fired == []


def test_a_edit_with_surrounding_spaces_is_stripped_before_writing(model, engine):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, VALUE_COLUMN), "  37  ") is True
    assert engine.writes == [("tcp-5020", 11, "hr", 4, "37", None)]


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
    assert model.index(r, VALUE_COLUMN).data() == "12"
    assert model.index(r, RAW_COLUMN).data() == "000C"


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
    # 72 而不是 96（2026-10-14 全量重排）：`random` 要 53px、`已停` 更短——原来的 96
    # 虚占 43px，而点位名那一列正被截。宽度变了但**列序**没变，这条断言的意图（不许为了
    # 两个字的文案动布局）由 `test_the_columns_are_sized_for_their_widest_content` 一起守。
    assert COLUMNS[wave].width == 72, "为两个字的文案加宽 = 动了既有布局"
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
    assert m.data(m.index(0, VALUE_COLUMN), stale_role) is True, "接管把 stale 角色顶掉了"
    assert m.index(0, VALUE_COLUMN).data() == "7", "初值回落的文字不许被两种状态改掉"
    # 单态行各自只说自己那一半，互不越界
    assert m.index(1, wave).data() == "已停"
    assert m.data(m.index(1, VALUE_COLUMN), stale_role) is False, "接管自己不算回读失败"
    assert m.index(2, wave).data() == "sine", "stale 不许去动波形列的文字"
    assert m.data(m.index(2, VALUE_COLUMN), stale_role) is True
    assert m.index(3, wave).data() == "sine"
    assert m.data(m.index(3, VALUE_COLUMN), stale_role) is False


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
    assert m.data(m.index(0, VALUE_COLUMN), stale) is False
    assert m.data(m.index(1, VALUE_COLUMN), stale) is True
    assert m.index(1, VALUE_COLUMN).data() == "7"


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
    assert m.index(row_of(m, "CO浓度"), RAW_COLUMN).data() == "0000 4148"
    assert m.index(row_of(m, "CO浓度"), VALUE_COLUMN).data() == "12.5"


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


# ---------- 右键菜单（视图） ----------

def _menu_view(qapp, rows: list[Row]) -> PointTableView:
    """一张真摆上去的表：`visualRect` 要真实几何才量得到行，offscreen 下 show() 就够。"""
    view = PointTableView()
    model = PointTableModel(view)
    model.attach(TwoRowSource(rows))
    view.setModel(model)
    view.resize(600, 300)
    view.show()
    qapp.processEvents()
    return view


def test_the_row_menu_carries_the_point_name_and_greys_out_the_wrong_rows(qapp):
    """菜单每一项的判据来自**那一行**的归属：名字进文案，`source` 进 enabled。

    越界行号回空清单而不是抛：右键落在表头或空白处是操作员每天做的事。
    `属性…` 对每一行都开着——能不能改是 `apply()` 里带理由的拒绝（通道在跑、验不过
    回滚），菜单先把它置灰就成了"没打开就拒"，操作员看不见那一句。
    """
    view = _menu_view(qapp, [_row("曲线在跑", "1", waveform="sine"),
                             _row("人工改过", "2", source="manual")])
    assert view.row_key(0) == ("tcp-5020", 11, "hr", 0, None)
    assert view.row_key(9) is None
    # 四项：交还波形、属性、加点位、删这一个点位（后两项是"从零搭工程"那一步加的）
    assert [a.text() for a in view.row_menu_actions(0)] == \
        ["恢复波形：曲线在跑", "属性…", "添加点位…", "删除点位：曲线在跑…"]
    assert [a.text() for a in view.row_menu_actions(1)] == \
        ["恢复波形：人工改过", "属性…", "添加点位…", "删除点位：人工改过…"]
    # 置灰的只有"恢复"那一条（没有归属就谈不上交还）；另外三项对每一行都开着——
    # 能不能加/删是 `project.edit` 里带理由的拒绝，菜单先置灰就成了"没试就拒"。
    assert [a.isEnabled() for a in view.row_menu_actions(0)] == [False, True, True, True]
    assert [a.isEnabled() for a in view.row_menu_actions(1)] == [True, True, True, True]
    assert view.row_menu_actions(9) == []


def test_a_right_click_builds_the_menu_for_the_row_under_the_cursor(qapp, monkeypatch):
    """命中测试逐行：所有行弹同一份菜单（或永远按第 0 行建）等于点恢复甲、还回去乙。

    弹出这一步（`_popup`）是模态嵌套循环，offscreen 里进不去也出不来；用例替掉它，
    只留"弹的是哪一行的菜单、在哪个全局点弹"这两个事实，菜单内容仍由 `_menu_for` 真建。
    """
    view = _menu_view(qapp, [_row("曲线在跑", "1", waveform="sine"),
                             _row("人工改过", "2", source="manual")])
    popped: list[tuple[list[str], QtCore.QPoint]] = []
    monkeypatch.setattr(PointTableView, "_popup",
                        lambda self, menu, pos: popped.append(
                            ([a.text() for a in menu.actions()], pos)))

    def click(point: QtCore.QPoint):
        # 真实路径：事件发给 viewport，pos() 就是视口坐标（`visualRect` 也是视口坐标）。
        event = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Mouse, point,
                                        view.viewport().mapToGlobal(point))
        QtWidgets.QApplication.sendEvent(view.viewport(), event)
        return event

    model = view.model()
    row_point = view.visualRect(model.index(1, NAME_COLUMN)).center()
    click(row_point)
    assert [texts for texts, _ in popped] == \
        [["恢复波形：人工改过", "属性…", "添加点位…", "删除点位：人工改过…"]]
    assert popped[0][1] == view.viewport().mapToGlobal(row_point), \
        "传给 _popup 的不是 globalPos，菜单不会开在光标下"
    popped.clear()
    blank = click(QtCore.QPoint(10, view.viewport().height() - 4))
    assert popped == [] and not blank.isAccepted(), "空白处右键弹了菜单或没把事件交还出去"


def test_triggering_the_row_action_hands_back_that_rows_point(qapp):
    """动作带回去的必须是**建这一项时那一行**的身份：两行的 key 在这里要分得开。"""
    rows = [Row("tcp-5020", "COVI-1", 11, "风机状态", "hr", 4, "uint16", "ABCD",
                "5", (5,), False, "const", False, "manual"),
            Row("tcp-5020", "COVI-1", 11, "CO浓度", "hr", 0, "float32", "CDAB",
                "12.5", (0, 1), True, "const")]
    view = _menu_view(qapp, rows)
    got: list[tuple] = []
    view.restore_requested.connect(got.append)
    view.row_menu_actions(0)[0].trigger()
    assert got == [view.row_key(0)] == [("tcp-5020", 11, "hr", 4, None)]


def test_the_properties_item_carries_the_identity_of_its_own_row(qapp):
    """`属性…` 和恢复同一条纪律：payload 是**建这一项时那一行**的 key，逐行分得开。

    两行的 unit/区/地址都给不同的值：如果菜单把行号抄成了固定值或取错了行，
    这里收到的第二个 key 会对不上——属性面板改错点比恢复错点更糟（直接动点表本体）。
    """
    rows = [Row("tcp-5020", "COVI-1", 11, "甲", "hr", 0, "uint16", "ABCD",
                "1", (1,), False, "const"),
            Row("tcp-5020", "COVI-2", 12, "乙", "co", 3, "bool", "ABCD",
                "true", (1,), False, "const", False, "manual")]
    view = _menu_view(qapp, rows)
    got: list[tuple] = []
    view.props_requested.connect(got.append)
    view.row_menu_actions(0)[1].trigger()
    view.row_menu_actions(1)[1].trigger()
    assert got == [("tcp-5020", 11, "hr", 0, None), ("tcp-5020", 12, "co", 3, None)]


def test_a_greyed_item_stays_inert_when_triggered(qapp):
    """"置灰不删"的完整语义：disabled 项被直接 `trigger()` 也不发 `restore_requested`。

    真菜单里 disabled 项由 Qt 自己拦点击，可操作员碰不到 ≠ 代码碰不到；若哪天弹出路径
    换了（主题、键盘重映射），这一条就是"没被接管的点被还回波形"的最后防线。
    （PySide6 6.9 实测：disabled 的 `trigger()` 不发 `triggered`，先验后钉。）
    """
    view = _menu_view(qapp, [_row("曲线在跑", "1", waveform="sine"),
                             _row("人工改过", "2", source="manual")])
    got: list[tuple] = []
    view.restore_requested.connect(got.append)
    greyed = view.row_menu_actions(0)[0]
    assert greyed.isEnabled() is False
    greyed.trigger()
    assert got == []


def test_the_add_point_item_tells_the_caller_which_device_is_under_the_cursor(qapp):
    """加点位落在**右键那一行所属的设备**上：两行的 unit 不同，交出去的 key 就必须不同。"""
    rows = [Row("tcp-5020", "COVI-1", 11, "甲", "hr", 0, "uint16", "ABCD",
                "1", (1,), False, "const"),
            Row("tcp-5020", "COVI-2", 12, "乙", "co", 3, "bool", "ABCD",
                "true", (1,), False, "const")]
    view = _menu_view(qapp, rows)
    got: list[tuple] = []
    view.add_point_requested.connect(got.append)
    view.row_menu_actions(1)[2].trigger()
    assert got == [("tcp-5020", 12, "co", 3, None)]


def test_the_remove_point_item_names_the_row_it_would_delete(qapp):
    """删点位删的就是右键那一行：文案带名字，payload 带那一行的身份。"""
    rows = [Row("tcp-5020", "COVI-1", 11, "甲", "hr", 0, "uint16", "ABCD",
                "1", (1,), False, "const"),
            Row("tcp-5020", "COVI-1", 11, "乙", "hr", 4, "uint16", "ABCD",
                "2", (2,), False, "const")]
    view = _menu_view(qapp, rows)
    got: list[tuple] = []
    view.remove_point_requested.connect(got.append)
    assert view.row_menu_actions(1)[3].text() == "删除点位：乙…"
    view.row_menu_actions(0)[3].trigger()
    assert got == [("tcp-5020", 11, "hr", 0, None)]
