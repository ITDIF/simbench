"""属性面板改的是点表本体：验不过就整体回滚，工程里不许留下一个"看着改了"的半状态。

口径沿用 `gui/channel_panel.apply_edits`：面板只做**转换**（文字→类型），判定全在
`validate_project` 那一份里；先写回工程再验，验出新增错误就整体还原。

生效时机按 Task 6 控制器裁决，不是 brief Step 5 那句"apply → `session.load`"：
`load()` 会 `_stop_all_locked()` 停掉**所有**通道——改一个点的地址把另一路正在给网关
服务的从站停掉，是新的现场事故。所以结构性字段（区/地址/类型/字序/初值/只读）在**所属
通道在跑**时一律拒绝并点名"先停通道"；`name/waveform/wave_param` 是 live 安全的
（`_tick_once` 每轮重读同一个 `Point` 对象，Task 3/4 已证），在跑时直接改、下一拍生效、
绝不重载。没有任何通道在跑时结构性改动走 `session.load`（索引、通道集合与三视图一次对上）；
兄弟通道还在跑时走 `session.reindex(本行的通道)`（只重建读写索引，不碰任何在跑的通道，
且它自己就挡住"给在跑的通道重建键"这种误用）。

窗口夹具在这里自建（`make_window` 住在 `tests/test_gui_main_window.py`，跨文件借夹具
就是把两个文件的变更绑死，M2 里那种"改一个连累一片"就是这么来的）。
"""

import copy
import pathlib

import pytest
from PySide6 import QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui import main_window as main_window_module
from simbench.gui.point_props import PointPropertiesDialog
from simbench.gui.main_window import MainWindow
from simbench.project.model import DATATYPES, Device, Point, TcpChannel
from simbench.project.validate import error_lines, validate_project
from tests.conftest import free_port

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")
KEY = ("tcp-5020", 11, "hr", 4)          # 状态字：可写、uint16、邻居是 5 上的 uint64
NAME = "风机状态"
VALUE_COLUMN = 6                          # gui/point_table.COLUMNS 里的"当前值"
ADDRESS_COLUMN = 2                        # 同一张表里的"地址"


@pytest.fixture
def window(qapp, engine_runner):
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"], [str(i) for i in issues]
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    win = MainWindow(session, store)
    win.use_project(project, CSV)
    win.resize(1200, 700)
    win.show()
    qapp.processEvents()
    yield win
    win.timer.stop()
    win.session.shutdown()
    win.deleteLater()
    qapp.processEvents()


def real_point(project, key):
    """按 `(channel, unit_id, area, address)` 取工程里**正在被索引**的那个 Point 对象。"""
    channel, unit_id, area, address = key
    for ch in project.channels:
        if ch.name != channel:
            continue
        for dev in ch.devices:
            if dev.unit_id != unit_id:
                continue
            for pt in dev.points:
                if pt.area == area and pt.address == address:
                    return pt
    raise AssertionError(f"point {key} is not in the project")


def row_of(window, name: str) -> int:
    model = window.point_model
    return next(r for r in range(model.rowCount()) if model.index(r, 0).data() == name)


def start_owner(window, qapp):
    """把点表通道换到空闲端口并**真起**："通道在跑"必须是真状态，不是替身。

    夹具装载的是 CSV 里的 5020（生产端口，用例不许去抢），所以先改端口再 `use_project`
    重载一次 —— 这也正是"改配置要重新装载才对上索引"的现场形状。
    """
    project = window.session.project
    project.channels[0].port = free_port()
    window.use_project(project, CSV)
    window._start_all()
    qapp.processEvents()
    assert window.session.channel("tcp-5020").running


def second_running_channel(window, qapp):
    """另加一路**真在跑**的兄弟通道：钉"为一个已停通道的小编辑不许停掉在跑的从站"。"""
    project = window.session.project
    project.channels[0].port = free_port()
    project.channels.append(TcpChannel(
        name="tcp-b", host="127.0.0.1", port=free_port(),
        devices=[Device(name="回路-2", unit_id=21,
                        points=[Point("回路温度", "hr", 0, "uint16", initial="7")])]))
    window.use_project(project, CSV)
    window.session.start_channel("tcp-b")
    qapp.processEvents()
    assert window.session.channel("tcp-b").running


def load_dirty(window, key, **fields):
    """把某一行的字段改成"合法选项之外的值"，然后**带着错**重新装载。

    `Session.load` 与 spec §8 都允许带着 error 装载（操作员开的就是这一行去修它），
    所以脏值不是测试里的假想形状。这里绕开 `use_project` 直接 `session.load`：
    后者会把清单交给模态的 `IssueDialog`，offscreen 下那条嵌套事件循环会把用例吞掉。
    """
    project = window.session.project
    point = real_point(project, key)
    for field, value in fields.items():
        setattr(point, field, value)
    window.session.load(project)
    return project


def panel_from_the_slot(monkeypatch):
    """把主窗引用的对话框名字换成"真面板 + 非模态 exec"，供**直接调槽**的用例用。

    `exec()` 的模态嵌套循环会把用例吞掉（`IssueDialog` 同形），而构造路上真崩了的话，
    异常会原样从槽函数里逃到用例里——这正是"槽函数不许炸"要量的那一格。
    """
    opened: list = []
    real_cls = main_window_module.PointPropertiesDialog

    def factory(parent, project, key):
        dialog = real_cls(parent, project, key)
        opened.append(dialog)
        dialog.exec = lambda *_args: 0
        return dialog

    monkeypatch.setattr(main_window_module, "PointPropertiesDialog", factory)
    return opened


# ---------- brief Step 1-3 的三条钉死用例（逐字） ----------

def test_applying_an_illegal_address_change_is_rolled_back_wholesale(qapp, window):
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    before = dialog.snapshot_point()
    dialog.address_edit.setText("0")            # CO浓度 是 float32，在 0 占两格 → 撞上
    dialog.apply()
    assert dialog.snapshot_point() == before, "被拒的改动留在工程里了"
    assert dialog.error_lines(), "回滚了却没给任何理由"
    assert not window.session.dirty, "被拒的改动不该留下未保存标记"
    # 回滚必须落到**工程里的本体**，不只是面板的副本：留一个半状态在本体里，
    # 下一次装载读到的就是它（副本对上了也没用）。
    assert real_point(window.session.project, KEY) == before


def test_the_panel_says_the_change_needs_a_channel_restart(qapp, window):
    """SimDevice 是 start_channel 时建的：改区/地址/类型/字序不重启就不生效。

    这条标注是给现场看的——最坏的形状是"改了地址、从站还在旧地址应答、界面不说"。
    """
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.address_edit.setText("20")
    dialog.apply()
    assert "重启通道" in dialog.notice.text(), dialog.notice.text()


def test_the_panel_geometry_is_identical_across_every_state(qapp, window):
    """切区/切类型会让字序、波形参数那些控件的可用性变，但一个矩形都不许挪。"""
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.resize(460, 520)
    dialog.show()
    qapp.processEvents()
    baseline = dialog.geometry_snapshot()
    for area in ("co", "di", "hr", "ir"):
        dialog.pick_area(area)
        qapp.processEvents()
        assert dialog.geometry_snapshot() == baseline, area


# ---------- 裁决：生效时机（结构性 vs live 安全） ----------

def test_a_structural_apply_with_nothing_running_lands_in_project_and_write_index(qapp,
                                                                                   window):
    """没有任何通道在跑：结构性改动要写进本体、重建索引、置 dirty，一句"重启通道后生效"。

    索引这条**必须真写到**才算数：`Session._points` 按 `(area, address)` 建键，不重建的
    话通道启动后按新地址写值会被挡在"不在点表里"——改了地址却写不进去，正是本文件
    docstring 说的那种"看着改了"的半状态。
    """
    project = window.session.project
    project.channels[0].port = free_port()
    window.use_project(project, CSV)
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.address_edit.setText("20")
    assert dialog.apply() is True
    assert dialog.applied == {"address": 20}
    assert real_point(project, ("tcp-5020", 11, "hr", 20)).name == NAME
    assert window.session.dirty is True
    window.session.start_channel("tcp-5020")
    window.point_model.refresh()
    row = row_of(window, NAME)
    assert window.point_model.index(row, ADDRESS_COLUMN).data() == "20"
    assert window.point_model.setData(
        window.point_model.index(row, VALUE_COLUMN), "42") is True, \
        "地址改动没进 Session 的读写索引"


def test_structural_fields_refuse_an_apply_while_the_owning_channel_runs(qapp, window):
    """六个结构性字段逐个钉：通道在跑就整体拒绝，拒绝理由点名点位并要求先停通道。

    少钉一个字段都是一个洞：漏掉哪个，哪个就能在跑着的通道上改出"界面改了、从站还
    在旧配置应答"的半状态。拒绝发生在写回**之前**——所以本体一点没动、`dirty` 没碰、
    没有半途应用也没有悄悄排队。
    """
    start_owner(window, qapp)
    project = window.session.project
    untouched = copy.deepcopy(real_point(project, KEY))
    attempts = [
        ("区", lambda d: d.pick_area("co")),
        ("地址", lambda d: d.address_edit.setText("20")),
        ("类型", lambda d: d.pick_datatype("int16")),
        ("字序", lambda d: d.pick_word_order("CDAB")),
        ("初值", lambda d: d.initial_edit.setText("12")),
        ("只读", lambda d: d.readonly_check.setChecked(True)),
    ]
    for label, mutate in attempts:
        dialog = PointPropertiesDialog(window, project, KEY)
        mutate(dialog)
        assert dialog.apply() is False, f"{label}：通道在跑却应用了结构性改动"
        joined = "\n".join(dialog.error_lines())
        assert NAME in joined, f"{label}: 拒绝没点名点位 {joined}"
        assert "stop the channel" in joined, f"{label}: 拒绝没说先停通道 {joined}"
        assert dialog.applied is None, f"{label}: 被拒的改动留下了 applied"
        assert real_point(project, KEY) == untouched, f"{label}: 本体被动过"
    assert not window.session.dirty, "被拒的改动不该留下未保存标记"
    assert window.session.channel("tcp-5020").running, "拒绝不许顺手停通道"


def test_live_safe_fields_apply_while_the_channel_runs_without_reloading(qapp, window):
    """名称/波形/波形参数在跑着时直接应用：不许走 `load`（那会把在跑的通道停掉）。

    `_tick_once` 每轮重读同一个 `Point` 对象（Task 3/4 已证），所以不重载也能下一拍
    生效；这里钉的是面板侧的三件事：本体真的改了、通道还在跑、`dirty` 置上了。
    """
    start_owner(window, qapp)
    project = window.session.project
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.name_edit.setText("风机状态B")
    assert dialog.apply() is True
    assert dialog.applied == {"name": "风机状态B"}
    assert window.session.channel("tcp-5020").running, \
        "live 安全改动走了整会话重载：把正在服务网关的通道停了"
    assert real_point(project, KEY).name == "风机状态B"

    dialog2 = PointPropertiesDialog(window, project, ("tcp-5020", 11, "hr", 4))
    dialog2.pick_waveform("ramp")
    dialog2.wave_param_edit.setText("from=0;to=10;speed=2")
    assert dialog2.apply() is True
    assert dialog2.applied == {"waveform": "ramp", "wave_param":
                               {"from": "0", "to": "10", "speed": "2"}}
    assert window.session.channel("tcp-5020").running
    assert real_point(project, ("tcp-5020", 11, "hr", 4)).waveform == "ramp"
    assert window.session.dirty is True


def test_a_structural_edit_with_a_sibling_running_keeps_the_sibling_and_reindexes(
        qapp, window):
    """兄弟通道在跑、本通道停着：结构性改动要**不**重载会话（兄弟不许被停），但索引
    必须重建 —— 之后启动本通道，按新地址写值要能走通。
    """
    second_running_channel(window, qapp)
    project = window.session.project
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.address_edit.setText("20")
    assert dialog.apply() is True
    assert window.session.channel("tcp-b").running, \
        "为一个已停通道的编辑把在跑的兄弟停了：这正是裁决要防的现场事故"
    window.session.start_channel("tcp-5020")
    window.point_model.refresh()
    row = row_of(window, NAME)
    assert window.point_model.index(row, ADDRESS_COLUMN).data() == "20"
    assert window.point_model.setData(
        window.point_model.index(row, VALUE_COLUMN), "42") is True, \
        "跳过 load 之后没补 reindex：读写索引还停在旧地址"


# ---------- 校验复用：只报"新增的那几条" ----------

def test_only_the_newly_introduced_errors_are_reported(qapp, window):
    """既有错误不许算进本次改动的账；回滚也不许顺手"修好"它们。

    判据仍然只有 `validate_project` 一份：改前后各验一次、做差集，与
    `channel_panel.apply_edits` 同一个写法。
    """
    project = window.session.project
    project.tick_ms = 5                       # 一条与本次编辑无关的既有 error
    window.session.load(project)
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.address_edit.setText("0")          # 撞 CO浓度 → 新增 overlap
    assert dialog.apply() is False
    joined = "\n".join(dialog.error_lines())
    assert "overlap" in joined, joined
    assert "tick_ms" not in joined, "把既有错误也算成这次改动的账"
    assert project.tick_ms == 5, "回滚不该顺手改动别的字段"
    assert not window.session.dirty


def test_an_unparseable_address_is_refused_as_a_conversion_without_touching_project(
        qapp, window):
    """地址框里的 `4x00021` 不是一个值，是键盘上的字：转换层当场拒，话要说得出口。

    拒绝理由既要进 `error_lines()` 也要显示在面板里（β：屏幕上看不到的拒绝等于没拒绝），
    并且工程本体一字未动、不留 dirty。
    """
    project = window.session.project
    before = copy.deepcopy(real_point(project, KEY))
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.address_edit.setText("4x00021")
    assert dialog.apply() is False
    assert any("is not an integer" in line for line in dialog.error_lines())
    assert "is not an integer" in dialog.error_view.toPlainText()
    assert real_point(project, KEY) == before
    assert not window.session.dirty


def test_a_datatype_switch_the_initial_text_cannot_survive_is_refused_by_name(
        qapp, window):
    """类型改成 bool 而初值 `11` 不是 bool 写法：带点位名拒绝，不静默清成 false。

    静默清值 = 界面替操作员做了一个他没做过的决定，而 `11` 到底想填什么没人知道
    （裁决 A：宁可拒绝也不猜）。
    """
    project = window.session.project
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.pick_datatype("bool")
    assert dialog.apply() is False
    joined = "\n".join(dialog.error_lines())
    assert NAME in joined and "bool needs one of" in joined, joined
    point = real_point(project, KEY)
    assert point.datatype == "uint16" and point.initial == "11", "本体被动过"
    assert not window.session.dirty


# ---------- 呈现：平铺可点选、置灰不藏、4x 跟着算 ----------

def test_the_choice_fields_are_flat_checkable_buttons_and_no_dropdown(qapp, window):
    """区/类型/字序/波形全是平铺的 checkable 单选按钮，整个面板一个 QComboBox 都没有。

    下拉把"一共有哪些合法值"藏进一次点击里，而这张表的操作员需要**扫一眼**就知道
    字序被收窄成哪两个——与报文过滤同一条界面口径。
    """
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    assert dialog.findChild(QtWidgets.QComboBox) is None, "平铺字段被换成了下拉"
    for buttons, expected in ((dialog.area_buttons, ("co", "di", "hr", "ir")),
                              (dialog.datatype_buttons, DATATYPES),
                              (dialog.word_order_buttons,
                               ("ABCD", "CDAB", "BADC", "DCBA")),
                              (dialog.waveform_buttons,
                               ("const", "ramp", "sine", "random", "step"))):
        assert tuple(buttons) == expected
        for name, button in buttons.items():
            assert isinstance(button, QtWidgets.QRadioButton), name
            assert button.isCheckable() and button.text() == name


def test_word_order_narrows_by_datatype_disabled_not_hidden(qapp, window):
    """字序按 `word_orders_allowed` 收窄：不可选的置灰，四个全都可见、矩形不动。

    期望集在这里**逐字面**写死（不重算那个函数），否则收窄口径漂了测试还跟着绿。
    """
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.resize(460, 520)
    dialog.show()
    qapp.processEvents()
    baseline = dialog.geometry_snapshot()
    expected = {"float32": {"ABCD", "CDAB", "BADC", "DCBA"},
                "int64": {"ABCD", "DCBA"}, "uint64": {"ABCD", "DCBA"},
                "float64": {"ABCD", "DCBA"}, "string": {"ABCD"},
                "uint16": {"ABCD"}, "bool": {"ABCD"}}
    for datatype, allowed in expected.items():
        dialog.pick_datatype(datatype)
        qapp.processEvents()
        enabled = {order for order, b in dialog.word_order_buttons.items()
                   if b.isEnabled()}
        assert enabled == allowed, datatype
        assert all(b.isVisible() for b in dialog.word_order_buttons.values()), \
            f"{datatype}: 不可选的字序被藏起来了（隐藏即改布局）"
        assert dialog.geometry_snapshot() == baseline, datatype


def test_the_panel_geometry_is_identical_across_every_datatype(qapp, window):
    """11 种类型挨个选一遍：可用范围在变，矩形一个都不许变。"""
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.resize(460, 520)
    dialog.show()
    qapp.processEvents()
    baseline = dialog.geometry_snapshot()
    for datatype in DATATYPES:
        dialog.pick_datatype(datatype)
        qapp.processEvents()
        assert dialog.geometry_snapshot() == baseline, datatype


def test_the_conventional_display_follows_the_area_and_the_new_address(qapp, window):
    """改了区，`4x` 那列要跟着换区重算（`conventional(area, address)`），不另存一份。"""
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.address_edit.setText("20")
    qapp.processEvents()
    assert dialog.convention_label.text() == "4x00021"
    dialog.pick_area("co")
    qapp.processEvents()
    assert dialog.convention_label.text() == "0x00021"


def test_the_notice_names_both_the_restart_and_the_next_tick_honestly(qapp, window):
    """裁决后的通知必须同时说清两件事：结构性改动等重启，live 安全改动下一拍生效。

    只留"重启通道后生效"那句，改个点位名也会被误导去重启通道——而重启会把正在
    服务网关的那一路断掉，这是有代价的动作，不该为它给假理由。
    """
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    text = dialog.notice.text()
    assert "重启通道" in text, text
    assert "下一拍" in text, text
    assert "…" not in text and "..." not in text, "装不下要改文案，不许截省略号"


def test_the_panel_releases_the_point_when_it_closes(qapp, window):
    """关掉的面板不许还握着工程里的 Point：GC 与跨线程口径（spec §12/§13）同样成立。

    关板后的 `apply()` 只能回 False：引用已经放手，再"应用"就是一次没人见过的写入。
    """
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    assert dialog.point is not None
    dialog.done(0)
    assert dialog.point is None
    assert dialog.snapshot_point() is None
    assert dialog.apply() is False


# ---------- 带着错装载的行：面板不许崩，也不许把脏值洗成合法值 ----------

def test_a_row_with_an_unknown_area_opens_and_stays_an_error(qapp, window):
    """`Session.load` 允许带着 error 装载（spec §8：操作员开的就是这一行去修它）。

    存储值落不在合法选项里时，面板**把那个值原样补成一项并选中**，而不是默认到第一个
    合法值——表里写 `holding`、面板显示 `hr`，正是本项目最防的那类"看着合法的错值"。
    validator 那一份判定一字不动：这一行仍然是错，面板只是不再崩在 `no option is checked`。
    """
    project = load_dirty(window, KEY, area="holding")
    dialog = PointPropertiesDialog(window, project, ("tcp-5020", 11, "holding", 4))
    assert [n for n, b in dialog.area_buttons.items() if b.isChecked()] == ["holding"]
    assert dialog.convention_label.text() == "—", "未知区名算不出惯例地址，也不许猜一个前缀"
    joined = "\n".join(error_lines(validate_project(project)))
    assert "unknown area 'holding'" in joined, "开一下面板就把点表里的错悄悄洗白了"
    dialog.pick_area("hr")
    assert dialog.apply() is True, "把脏区名改对了却被拒"
    assert error_lines(validate_project(project)) == []
    assert real_point(project, KEY).area == "hr"


@pytest.mark.parametrize("field, bogus, picker, legal, code, enabled", [
    ("datatype", "float96", "pick_datatype", "uint16", "unknown datatype", True),
    ("word_order", "ABDC", "pick_word_order", "ABCD", "unknown word_order", False),
    ("waveform", "triang", "pick_waveform", "const", "unknown waveform", True),
])
def test_an_unknown_choice_is_shown_as_stored_and_stays_an_error(qapp, window, field,
                                                                 bogus, picker, legal,
                                                                 code, enabled):
    """类型/字序/波形三个平铺组同一条口径：脏值原样列出并选中，错还是那条错。

    字序这一组还多一层：脏值被 `word_orders_allowed` 判为不可选时是**勾选+置灰**
    （`_sync_word_orders` 的既有形状，补进来的那一项也得走它），面板不自作主张把它
    重置成 ABCD。
    """
    project = load_dirty(window, KEY, **{field: bogus})
    dialog = PointPropertiesDialog(window, project, KEY)
    buttons = getattr(dialog, f"{field}_buttons")
    assert [n for n, b in buttons.items() if b.isChecked()] == [bogus]
    assert buttons[bogus].isEnabled() is enabled
    assert code in "\n".join(error_lines(validate_project(project))), "判定被面板放宽了"
    getattr(dialog, picker)(legal)
    assert dialog.apply() is True
    assert error_lines(validate_project(project)) == []


def test_opening_the_panel_for_a_dirty_row_does_not_escape_the_slot(qapp, window,
                                                                    monkeypatch):
    """槽函数不许炸（与 `tests/test_gui_main_window.py` 同一走法）：直接调槽。

    评审实测的形状是 `point_props._checked` 那句 AssertionError 从 Qt 槽里逃出去——
    表现成"点了属性… 没反应"。开板这条路（构造）和 `KeyError` 那条兜底都得走通，
    状态栏也不能把一次成功的打开说成"该行点位已不在当前点表中"。
    """
    load_dirty(window, KEY, area="holding")
    opened = panel_from_the_slot(monkeypatch)
    window._show_point_props(("tcp-5020", 11, "holding", 4))
    assert len(opened) == 1, "面板没建起来：构造路上的异常还是从槽里逃出去了"
    assert opened[0].area_buttons["holding"].isChecked()
    assert "不在当前点表" not in window.status.currentMessage()


# ---------- 差集比较要先对上"称呼"：错误消息里本来就带着点位名 ----------

def test_a_point_that_already_has_an_error_can_still_be_renamed(qapp, window):
    """带着既有错误的行改名：那条错只是换了称呼，不是本次改动**新增**的错误。

    `validate._points` 的定位串里就有点位名，所以不归一化就会把"同一条错的新名字版本"
    当成新增，于是最需要改名的那一行永远改不了名。判定仍然只在 `validate_project`
    一份里：错还是那条错，只是行首的名字跟着新名走，一条都不许被顺手修掉。
    """
    project = window.session.project
    real_point(project, KEY).initial = "not-a-number"   # 既有的 [initial]，消息里带着 NAME
    window.session.load(project)
    before = [line for line in error_lines(validate_project(project))
              if "needs an integer" in line]
    assert len(before) == 1 and NAME in before[0], before
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.name_edit.setText("风机状态B")
    assert dialog.apply() is True, "改名被自己那条旧错误挡住了"
    assert dialog.error_lines() == []
    assert real_point(project, KEY).name == "风机状态B"
    after = [line for line in error_lines(validate_project(project))
             if "needs an integer" in line]
    assert len(after) == 1, "改名把既有错误改掉或改重了"
    assert "风机状态B" in after[0], f"错误清单还在用旧名字：{after[0]}"


def test_renaming_a_point_does_not_rewrite_its_neighbour_into_a_new_error(qapp, window):
    """别人的 `overlap` 消息里引用的是**我的名字**，那也不是我这次改动新增的错误。

    评审实测的形状：X 占了某个寄存器、Y 报 `already used by X`；改 X 的名字会重写 Y
    那条消息，于是 X 改不了名，改完之后的 Y 又因为"自己那条消息变了"改不了名——
    两个点被同一条引用互相锁死。归一化只作用于差集比较，清单本身一个字没改。
    """
    project = window.session.project
    real_point(project, ("tcp-5020", 11, "hr", 9)).address = 7   # 铭牌 落进 累计电量 的格子里
    window.session.load(project)
    before = "\n".join(error_lines(validate_project(project)))
    assert "already used by 累计电量" in before, before
    dialog = PointPropertiesDialog(window, project, ("tcp-5020", 11, "hr", 5))
    dialog.name_edit.setText("累计电量B")
    assert dialog.apply() is True, "改名被邻居那条引用了自己旧名的消息挡住了"
    joined = "\n".join(error_lines(validate_project(project)))
    assert "already used by 累计电量B" in joined, joined
    dialog2 = PointPropertiesDialog(window, project, ("tcp-5020", 11, "hr", 7))
    dialog2.name_edit.setText("铭牌B")
    assert dialog2.apply() is True, "邻居改完名之后，自己的名字也改不动了"
    assert real_point(project, ("tcp-5020", 11, "hr", 7)).name == "铭牌B"


def test_a_rename_that_also_creates_a_real_collision_is_still_refused(qapp, window):
    """归一化不许把**真新增**的错误一起抹掉：改名同时撞进 CO浓度 就得被拒并整体回滚。"""
    project = window.session.project
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.name_edit.setText("风机状态B")
    dialog.address_edit.setText("0")                     # CO浓度 是 float32，占 0、1 两格
    assert dialog.apply() is False
    joined = "\n".join(dialog.error_lines())
    assert "overlap" in joined, joined
    assert real_point(project, KEY).name == NAME, "被拒的改名留在了本体里"
    assert real_point(project, KEY).address == 4


def test_blanking_the_name_is_refused_and_brought_back_into_the_field(qapp, window):
    """把名称框清空是面板自己造得出来的形状：`target["name"] = text().strip()` 原样写回本体。

    判据不住在这里——`validate_project._points` 一份，面板靠既有的"新增错误"差集免费拿到
    这条（这正是它没走进 `_refuse` 那条只服务转换失败的路）。被拒之后本体一字未动、输入框
    退回真名、不留 dirty，与本文件其余拒绝用例同一个口径。
    """
    project = window.session.project
    before = copy.deepcopy(real_point(project, KEY))
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.name_edit.setText("")
    assert dialog.apply() is False, "空点位名被应用了"
    joined = "\n".join(dialog.error_lines())
    assert "must not be empty" in joined and "device COVI-1" in joined, joined
    assert real_point(project, KEY) == before, "被拒的空名字留在了本体里"
    assert dialog.name_edit.text() == NAME, "被拒的值没连输入框一起退回"
    assert not window.session.dirty


# ---------- 两条臂的分工：load 对上一切，reindex 只重建索引且自带守卫 ----------

def test_a_structural_apply_with_nothing_running_reconciles_the_channel_set(
        qapp, window):
    """全停时那一条臂必须真是 `load`：它连**通道集合**一起对上，`reindex` 只重建索引。

    评审实测的两条后果：点表里新增的通道在会话里没有 runtime → `start_channel` 直接
    `KeyError`；点表里删掉的通道还留着 runtime 在服务 → 没人再能停它。这两件事只有
    load 那条臂做得到，所以"永远走 reindex"这种变异在这里变红。
    """
    project = window.session.project
    project.channels[0].port = free_port()
    added = TcpChannel(
        name="tcp-new", host="127.0.0.1", port=free_port(),
        devices=[Device(name="回路-9", unit_id=91,
                        points=[Point("回路温度", "hr", 0, "uint16", initial="7")])])
    dropped = TcpChannel(
        name="tcp-gone", host="127.0.0.1", port=free_port(),
        devices=[Device(name="回路-8", unit_id=81,
                        points=[Point("回路湿度", "hr", 0, "uint16", initial="3")])])
    project.channels.extend([added, dropped])
    window.use_project(project, CSV)
    project.channels.remove(dropped)          # 装载之后又删掉一路：runtime 成了孤儿
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.address_edit.setText("20")
    assert dialog.apply() is True
    assert window.session.channel("tcp-gone") is None, "删掉的通道还留着 runtime 在服务"
    window.session.start_channel("tcp-new")
    assert window.session.channel("tcp-new").running, "新增的通道没被对上：索引之外的活儿 reindex 不管"


def test_session_reindex_refuses_to_rekey_a_channel_that_is_running(qapp, window):
    """`reindex(通道)` 自己就得挡住"给在跑的那一路重新建键"，不能只指望调用方记得先拒。

    坏形状（评审实测）：在跑时重建索引，`_points` 按新地址建键，而界面上的行和跑着的
    `SimDevice` 还认旧地址 → 新旧两个地址的写值全被挡在"不在点表里"，两头都对不上。
    面板本来就在写回**之前**拒绝这种改动（上面几条钉着，拒绝理由要带点位名）；这里
    绕开面板直接调，钉的是缝另一侧的守卫——下一个调用方忘了拒绝也拿不到一份错索引。
    """
    start_owner(window, qapp)
    session = window.session
    real_point(session.project, KEY).address = 20        # 绕过面板直接改本体
    with pytest.raises(ValueError) as excinfo:
        session.reindex("tcp-5020")
    assert "is running" in str(excinfo.value)
    # 索引没被重建才对：旧地址仍然**在点表里**（写进去会因回读越界而被拒，那是另一句
    # 话），新地址仍然"不在点表里"。守卫要是没挡住，这两句会同时反过来。
    _ok, error = session.write("tcp-5020", 11, "hr", 4, "42")
    assert "not in the point table" not in error, \
        "守卫没挡住重建索引：旧地址已经不在读写索引里了"
    ok, error = session.write("tcp-5020", 11, "hr", 20, "42")
    assert ok is False and "not in the point table" in error, error


# ---------- 被拒的改动连输入框一起退回（channel_panel 同口径） ----------

def test_a_refused_apply_snaps_the_fields_back_to_what_is_really_in_effect(
        qapp, window):
    """被拒的改动要把输入框也退回本体值：留着"0"这个没生效的地址，下一次编辑就基于一个
    工程里根本不存在的世界，"拒了还能再改"是演出来的。"""
    project = window.session.project
    dialog = PointPropertiesDialog(window, project, KEY)
    dialog.name_edit.setText("风机状态B")
    dialog.address_edit.setText("0")                     # 撞 CO浓度 → 整笔被拒
    assert dialog.apply() is False
    point = real_point(project, KEY)
    assert dialog.address_edit.text() == str(point.address)
    assert dialog.name_edit.text() == point.name

