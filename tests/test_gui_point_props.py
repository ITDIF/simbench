"""属性面板改的是点表本体：验不过就整体回滚，工程里不许留下一个"看着改了"的半状态。

口径沿用 `gui/channel_panel.apply_edits`：面板只做**转换**（文字→类型），判定全在
`validate_project` 那一份里；先写回工程再验，验出新增错误就整体还原。

生效时机按 Task 6 控制器裁决，不是 brief Step 5 那句"apply → `session.load`"：
`load()` 会 `_stop_all_locked()` 停掉**所有**通道——改一个点的地址把另一路正在给网关
服务的从站停掉，是新的现场事故。所以结构性字段（区/地址/类型/字序/初值/只读）在**所属
通道在跑**时一律拒绝并点名"先停通道"；`name/waveform/wave_param` 是 live 安全的
（`_tick_once` 每轮重读同一个 `Point` 对象，Task 3/4 已证），在跑时直接改、下一拍生效、
绝不重载。没有任何通道在跑时结构性改动走 `session.load`（索引与三视图一次对上）；
兄弟通道还在跑时走 `session.reindex`（只重建读写索引，不碰任何在跑的通道）。

窗口夹具在这里自建（`make_window` 住在 `tests/test_gui_main_window.py`，跨文件借夹具
就是把两个文件的变更绑死，M2 里那种"改一个连累一片"就是这么来的）。
"""

import copy
import pathlib

import pytest
from PySide6 import QtWidgets

from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import PacketStore
from modslave.engine.session import Session
from modslave.gui.point_props import PointPropertiesDialog
from modslave.gui.main_window import MainWindow
from modslave.project.model import DATATYPES, Device, Point, TcpChannel
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
