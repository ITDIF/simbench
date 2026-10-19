"""S7 的界面（Task 6）：通道面板那一行、点表"原始寄存器"列、报文窗的空字节列、属性面板的选项。

三条口径：

* 通道面板第三格对 S7 是 **rack/slot**（`0/1`），TCP 行留空并置灰——三格仍是定宽槽，
  四态几何恒定（口径 3/4）；
* 点表的"原始寄存器"列在 S7 行上打的是**字节**（两位十六进制）：`Row.registers` 在 S7
  这一路装字节，打成 0041 会让人以为是 16 位寄存器；
* 报文窗的"原始字节"列在 S7 事件行上**留空**（原生库不给原始报文，口径 5）——列宽与列序
  一律不动，摘要里写清"读/写 区 长度B @偏移"。
"""

import pytest
from PySide6 import QtWidgets

from simbench.engine.packets import PacketStore
from simbench.engine.runner import EngineRunner
from simbench.engine.session import Row, Session
from simbench.events import PacketEvent
from simbench.gui.channel_panel import STATUS_LABELS, ChannelPanel
from simbench.gui.packet_view import PacketModel
from simbench.gui.point_props import PointPropertiesDialog
from simbench.gui.point_table import COLUMNS, RAW_COLUMN, _display
from simbench.project.model import Device, Point, Project, S7Channel, TcpChannel


def point(name="温度", area="db1", address=0, datatype="real", initial="12.5", bit=None):
    return Point(name, area, address, datatype, initial=initial, bit=bit)


@pytest.fixture(scope="module")
def engine():
    runner = EngineRunner()
    runner.start()
    session = Session(runner=runner)
    yield session
    session.shutdown()
    runner.stop()


@pytest.fixture
def project():
    """一 TCP 一 S7 的混装工程：既验 S7 行，也验"TCP 行没被带动"。"""
    return Project(channels=[
        TcpChannel("tcp-5020", host="127.0.0.1", port=5020,
                   devices=[Device("COVI-1", 11, [point("温度", "hr", 0, "uint16", "1")])]),
        S7Channel("s7-1", host="192.168.0.10", port=102, rack=0, slot=1,
                  devices=[Device("D1", 21, [
                      point("出口温度", "db1", 0, "real"),
                      point("运行", "db1", 6, "bool", "true", bit=3)])]),
    ])


@pytest.fixture
def built(qapp, project, engine):
    engine.load(project)
    panel = ChannelPanel(engine)
    panel.rebuild(engine.project)
    holder = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(holder).addWidget(panel)
    holder.resize(820, 160)
    holder.show()
    qapp.processEvents()
    yield panel, engine, qapp
    engine.stop_all()
    panel.deleteLater()
    holder.deleteLater()
    qapp.processEvents()


def by_channel(panel):
    return {row.channel: row for row in panel.rows()}


# ---------- 通道面板那一行 ----------

def test_a_s7_row_shows_host_port_and_rack_slot(built):
    panel, _session, _qapp = built
    row = by_channel(panel)["s7-1"]
    assert row.s7 is True and row.serial is False
    assert row.host_edit.text() == "192.168.0.10"
    assert row.port_edit.text() == "102"
    assert row.frame_edit.text() == "0/1"
    assert row.frame_edit.isEnabled(), "S7 行的第三格要能改"


def test_a_tcp_row_keeps_an_empty_disabled_third_slot(built):
    panel, _session, _qapp = built
    row = by_channel(panel)["tcp-5020"]
    assert row.s7 is False and row.frame_edit.text() == ""
    assert not row.frame_edit.isEnabled() and row.frame_edit.isVisible()


def test_the_unused_third_slot_says_what_it_is_for(built):
    """TCP 行那格空着，但鼠标停上去要有话说——光看一个灰空框，操作员只会怀疑坏了。"""
    panel, _engine, _qapp = built
    tcp = by_channel(panel)["tcp-5020"]
    assert tcp.frame_edit.text() == ""
    assert "串口" in tcp.frame_edit.toolTip() and "S7" in tcp.frame_edit.toolTip()
    assert "TCP" in tcp.frame_edit.toolTip()


def test_the_three_slots_are_fixed_width_and_geometry_is_stable(built):
    panel, _session, qapp = built
    baseline = panel.geometry_snapshot()
    widths = {(r.host_edit.width(), r.port_edit.width(), r.frame_edit.width())
              for r in panel.rows()}
    assert len(widths) == 1, widths
    for state in STATUS_LABELS:
        panel.set_status("s7-1", state)
        qapp.processEvents()
        assert panel.geometry_snapshot() == baseline, f"{state} 之后几何变了"


def test_editing_the_s7_row_writes_back_and_rolls_back(built):
    panel, session, _qapp = built
    row = by_channel(panel)["s7-1"]
    row.frame_edit.setText("2/3")
    row.port_edit.setText("1102")
    assert panel.apply_edits("s7-1") == 1102
    target = session.project.find_channel("s7-1")
    assert (target.rack, target.slot, target.port) == (2, 3, 1102)
    row.frame_edit.setText("2/3/4")
    assert panel.apply_edits("s7-1") is None
    assert "must look like 0/1" in panel.last_error
    row.port_edit.setText("70000")
    assert panel.apply_edits("s7-1") is None
    assert session.project.find_channel("s7-1").port == 1102, "被拒的端口要回滚"


# ---------- 点表的"原始寄存器"列 ----------

def test_the_raw_column_prints_bytes_on_an_s7_row_and_registers_on_a_modbus_row():
    s7_row = Row("s7-1", "D1", 21, "出口温度", "db1", 0, "real", "ABCD",
                 "12.5", (0x41, 0x48, 0x00, 0x00), False, "const", False, "waveform")
    assert _display(s7_row, "原始寄存器") == "41 48 00 00"
    modbus_row = Row("tcp-5020", "COVI-1", 11, "温度", "hr", 0, "uint16", "ABCD",
                     "12", (0x000C,), False, "const", False, "waveform")
    assert _display(modbus_row, "原始寄存器") == "000C"
    assert COLUMNS[RAW_COLUMN].header == "原始寄存器"      # 列头与列序不动


# ---------- 报文窗 ----------

def test_the_packet_model_leaves_the_raw_column_empty_for_an_s7_event():
    store = PacketStore()
    model = PacketModel(store)
    store.paused = False
    store.add(PacketEvent("s7-1", "rx", b"", None, None, "写 db1 2B @4", 0.5))
    model.sync()
    assert model.data(model.index(0, 5)) == "写 db1 2B @4"      # 摘要说清读/写与区间
    assert model.data(model.index(0, 6)) == ""                  # 原始字节列留空
    assert model.data(model.index(0, 3)) == "-"                 # S7 没有 unit


# ---------- 属性面板的选项 ----------

class _Host(QtWidgets.QWidget):
    """属性面板要求父窗口暴露 `.session`（M4 定的契约：没有 session 就构造即拒）。"""

    def __init__(self, session):
        super().__init__()
        self.session = session


def test_the_properties_dialog_offers_s7_areas_and_types(qapp, engine):
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 11, [point("出口温度", "db3", 0, "real")])])])
    engine.load(project)
    host = _Host(engine)                 # 引用要留着：父窗口被回收时对话框会跟着没
    dialog = PointPropertiesDialog(host, project, ("s7-1", 11, "db3", 0, None))
    try:
        # `_radios` 回的是"值 -> 按钮"的字典（M4 的形状），键就是选项名
        areas = list(dialog.area_buttons)
        types = list(dialog.datatype_buttons)
        assert "mk" in areas and "db3" in areas, areas     # 当前那个 DB 号补进来选中
        assert "real" in types and "word" in types, types
        assert "uint16" not in types and "hr" not in areas, (types, areas)
    finally:
        dialog.deleteLater()
        host.deleteLater()
        qapp.processEvents()


def test_the_properties_dialog_keeps_modbus_options_for_modbus_points(qapp, engine):
    project = Project(channels=[TcpChannel("tcp-1", devices=[
        Device("D1", 11, [point("温度", "hr", 0, "uint16", "1")])])])
    engine.load(project)
    host = _Host(engine)
    dialog = PointPropertiesDialog(host, project, ("tcp-1", 11, "hr", 0, None))
    try:
        assert list(dialog.area_buttons) == ["co", "di", "hr", "ir"]
        assert "uint16" in list(dialog.datatype_buttons)
    finally:
        dialog.deleteLater()
        host.deleteLater()
        qapp.processEvents()


def test_the_properties_panel_edits_the_bit_that_was_asked_for(qapp, engine):
    """同字节上的两个 bool 是两个点：面板按身份（带位）取本体，改的是**那一行**。

    不带位取本体的话，这里改的是 `_find_point` 先找到的那一个——现场表现是"改甲，乙变了"。
    """
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 11, [point("起", "db1", 6, "bool", "false", bit=2),
                          point("停", "db1", 6, "bool", "false", bit=3)])])])
    engine.load(project)
    host = _Host(engine)
    dialog = PointPropertiesDialog(host, project, ("s7-1", 11, "db1", 6, 3))
    try:
        assert dialog.point.name == "停"
        dialog.name_edit.setText("停止位")
        assert dialog.apply() is True
    finally:
        dialog.deleteLater()
    names = [pt.name for pt in project.channels[0].devices[0].points]
    assert names == ["起", "停止位"], names


def test_the_table_shows_the_bit_so_two_bools_in_one_byte_look_different(qapp):
    """同字节两个 bool：点位表的"地址"列分别显示 `6.2` 与 `6.3`（点表/CSV 的写法）。

    不带位显示的话两行一模一样——写对了也看不出右键的是哪一个（数字不撒谎，但少一位
    就等于没写）。
    """
    from simbench.engine.point_source import session_source
    from simbench.engine.session import Session
    from simbench.gui.point_table import ADDRESS_COLUMN, PointTableModel
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 11, [point("起", "db1", 6, "bool", "false", bit=2),
                          point("停", "db1", 6, "bool", "false", bit=3),
                          point("字", "db1", 6, "word", "0")])])])
    session = Session()
    session.load(project)
    model = PointTableModel()
    model.attach(session_source(session), "s7-1")
    addresses = [model.index(r, ADDRESS_COLUMN).data() for r in range(model.rowCount())]
    assert addresses == ["6.2", "6.3", "6"], addresses
    session.shutdown()


def test_the_panel_can_turn_an_s7_point_into_a_bool_and_write_its_bit(qapp, engine):
    """界面里把 S7 的点位改成 bool 并填位：这是"从界面建 S7 bool 点位"唯一的路。

    改前走不通——面板没有"位"这一格，改成 bool 会被 `validate` 挡回
    "bool points need a bit"，于是 S7 的位只能靠手写 CSV 才能建出来。
    """
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 11, [point("温度", "db1", 0, "word", "0")])])])
    engine.load(project)
    host = _Host(engine)
    dialog = PointPropertiesDialog(host, project, ("s7-1", 11, "db1", 0, None))
    try:
        assert dialog.bit_edit.isEnabled() is False, "不是 bool 时这一格该是灰的"
        dialog.pick_datatype("bool")
        qapp.processEvents()
        assert dialog.bit_edit.isEnabled() is True
        dialog.address_edit.setText("6")
        dialog.bit_edit.setText("3")
        assert dialog.apply() is True, dialog.error_lines()
    finally:
        dialog.deleteLater()
        host.deleteLater()
        qapp.processEvents()
    pt = project.channels[0].devices[0].points[0]
    assert (pt.area, pt.address, pt.datatype, pt.bit) == ("db1", 6, "bool", 3)


def test_the_panel_refuses_a_bool_without_a_bit_and_points_at_the_right_field(qapp, engine):
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 11, [point("温度", "db1", 0, "word", "0")])])])
    engine.load(project)
    host = _Host(engine)
    dialog = PointPropertiesDialog(host, project, ("s7-1", 11, "db1", 0, None))
    try:
        dialog.pick_datatype("bool")
        dialog.address_edit.setText("6")
        assert dialog.apply() is False
        assert "位不许为空" in dialog.error_lines()[0]
        # 地址格里照点表写法填 `6.3` 时，那句话要指到"位"那一格，而不是干说"不是整数"
        dialog.address_edit.setText("6.3")
        assert dialog.apply() is False
        assert "位 field" in dialog.error_lines()[0]
    finally:
        dialog.deleteLater()
        host.deleteLater()
        qapp.processEvents()
    assert project.channels[0].devices[0].points[0].datatype == "word", "被拒之后本体不许变"
