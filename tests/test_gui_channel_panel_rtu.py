"""通道面板的串口行（M5 Task 6）：前两格换含义、第三格是帧格式、TCP 行一个像素都不许动。

口径（与 M5 计划 §3 的口径 3 对应）：

* 串口行复用两个已有定宽槽：第一格是**串口名**（`COM3`）、第二格是**波特率**（`9600`），
  第三格是**帧格式**（`8E1`）；TCP 行第三格留空并置灰（**置灰不隐藏**——隐藏会让后面的
  格子集体左移，那是"状态一变布局就跳"里最难看的一种）；
* 面板只做**转换**（`8E1` → 三个字段、`9600` → int），白名单与"两个通道抢同一个串口"
  全部留给 `validate_project`：先写回工程再验，验出新增的错就整体回滚（连输入框一起退回）；
* 四态只换文字与颜色，几何恒定（`geometry_snapshot()` 逐格量）。
"""

import pytest
from PySide6 import QtWidgets

from simbench.engine.runner import EngineRunner
from simbench.engine.session import Session
from simbench.gui.channel_panel import STATUS_LABELS, ChannelPanel
from simbench.project.model import Device, Point, Project, SerialChannel, TcpChannel


def point(name: str = "温度"):
    return Point(name, "hr", 0, "uint16", initial="7")


@pytest.fixture(scope="module")
def engine():
    """一个模块一条引擎线程 + 一份 Session（与 `test_gui_channel_panel` 同一口径）。"""
    runner = EngineRunner()
    runner.start()
    session = Session(runner=runner)
    yield session
    session.shutdown()
    runner.stop()


@pytest.fixture
def project():
    """一 TCP 一 RTU 的混装工程：既验串口行，也验"加了一列之后 TCP 行没被带动"。"""
    return Project(channels=[
        TcpChannel(name="tcp-5020", host="127.0.0.1", port=5020,
                   devices=[Device("COVI-1", 11, [point()])]),
        SerialChannel(name="com-485-1", port="COM3", baudrate=19200, parity="E",
                      devices=[Device("照明-2", 21, [point("回路温度")])]),
    ])


@pytest.fixture
def built(qapp, project, engine):
    engine.load(project)
    panel = ChannelPanel(engine)
    panel.rebuild(engine.project)
    holder = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(holder).addWidget(panel)
    holder.resize(760, 160)
    holder.show()
    qapp.processEvents()
    yield panel, engine, qapp
    engine.stop_all()
    panel.deleteLater()
    holder.deleteLater()
    qapp.processEvents()


def by_channel(panel):
    return {row.channel: row for row in panel.rows()}


# ---------- 那三格 ----------

def test_a_serial_row_shows_the_serial_parameters_in_its_three_slots(built):
    panel, _session, _qapp = built
    row = by_channel(panel)["com-485-1"]
    assert row.serial is True
    assert row.host_edit.text() == "COM3"
    assert row.port_edit.text() == "19200"
    assert row.frame_edit.text() == "8E1"
    assert row.frame_edit.isEnabled(), "串口行的帧格式格要能改"


def test_a_tcp_row_keeps_its_own_meaning_and_an_empty_disabled_frame_cell(built):
    panel, _session, _qapp = built
    row = by_channel(panel)["tcp-5020"]
    assert row.serial is False
    assert (row.host_edit.text(), row.port_edit.text()) == ("127.0.0.1", "5020")
    assert row.frame_edit.text() == ""
    assert not row.frame_edit.isEnabled(), "TCP 行那一格置灰（不许隐藏：隐藏会挪后面的格子）"
    assert row.frame_edit.isVisible(), "置灰不等于藏起来"


def test_the_three_slots_are_fixed_width_in_both_kinds_of_row(built):
    panel, _session, _qapp = built
    first = panel.geometry_snapshot()
    # 逐格量：两行的三格宽度都一样（定宽槽），且与状态无关
    widths = {(r.host_edit.width(), r.port_edit.width(), r.frame_edit.width())
              for r in panel.rows()}
    assert len(widths) == 1, widths
    for state in STATUS_LABELS:
        panel.set_status("com-485-1", state)
        _qapp.processEvents()
        assert panel.geometry_snapshot() == first, f"{state} 之后几何变了"


# ---------- 写回与拒绝 ----------

def test_editing_the_frame_writes_all_three_fields_back(built):
    panel, session, qapp = built
    row = by_channel(panel)["com-485-1"]
    row.frame_edit.setText("7O2")
    assert panel.apply_edits("com-485-1") == 19200
    channel = session.project.find_channel("com-485-1")
    assert (channel.bytesize, channel.parity, channel.stopbits) == (7, "O", 2)
    assert session.dirty is True
    qapp.processEvents()


def test_editing_the_port_name_writes_back(built):
    panel, session, _qapp = built
    row = by_channel(panel)["com-485-1"]
    row.host_edit.setText("COM7")
    assert panel.apply_edits("com-485-1") == 19200
    assert session.project.find_channel("com-485-1").port == "COM7"


def test_editing_the_baudrate_writes_back(built):
    panel, session, _qapp = built
    row = by_channel(panel)["com-485-1"]
    row.port_edit.setText("115200")
    assert panel.apply_edits("com-485-1") is not None
    assert session.project.find_channel("com-485-1").baudrate == 115200


def test_a_baudrate_outside_the_whitelist_is_refused_and_rolled_back(built):
    """白名单在 validate 那一份里：面板只做转换，验出新增错误就整体回滚（连输入框一起退回）。"""
    panel, session, _qapp = built
    row = by_channel(panel)["com-485-1"]
    row.port_edit.setText("9500")
    assert panel.apply_edits("com-485-1") is None
    assert row.port_edit.text() == "19200", "被拒的值要连输入框一起退回"
    assert session.project.find_channel("com-485-1").baudrate == 19200
    assert session.dirty is False, "被拒的改动什么都没换，不许留下未保存标记"
    assert "baudrate 9500" in panel.last_error


def test_a_frame_that_is_not_even_written_like_8n1_is_refused_by_the_panel(built):
    """`8X1` 是键盘上的字，不是值：转换阶段就拒（与端口格里的 `COM3` 同一类）。"""
    panel, session, _qapp = built
    row = by_channel(panel)["com-485-1"]
    row.frame_edit.setText("8X1")
    assert panel.apply_edits("com-485-1") is None
    assert session.project.find_channel("com-485-1").parity == "E", "工程一个字段都不许动"
    assert session.dirty is False
    assert "must look like 8N1" in panel.last_error


def test_two_channels_may_not_end_up_on_the_same_serial_port(built):
    panel, session, qapp = built
    second = SerialChannel(name="com-485-2", port="COM4", devices=[])
    session.project.channels.append(second)
    panel.rebuild(session.project)
    qapp.processEvents()
    row = by_channel(panel)["com-485-1"]
    row.host_edit.setText("COM4")
    assert panel.apply_edits("com-485-1") is None
    assert session.project.find_channel("com-485-1").port == "COM3", "整体回滚"
    assert row.host_edit.text() == "COM3", "输入框一起退回"
    assert "serial_duplicated" in panel.last_error


def test_a_running_serial_row_locks_all_three_slots(built):
    """跑着的通道不许改参数：改的不是正在跑的那个（与 TCP 行同一条口径）。"""
    panel, _session, qapp = built
    row = by_channel(panel)["com-485-1"]
    panel.set_status("com-485-1", "running")
    qapp.processEvents()
    for widget in (row.host_edit, row.port_edit, row.frame_edit):
        assert not widget.isEnabled()
    panel.set_status("com-485-1", "stopped")
    qapp.processEvents()
    assert row.frame_edit.isEnabled()


def test_a_tcp_row_still_edits_host_and_port(built):
    """回归：多了一列之后，TCP 行的写回与拒绝一个字都没变。"""
    panel, session, _qapp = built
    row = by_channel(panel)["tcp-5020"]
    row.port_edit.setText("5021")
    assert panel.apply_edits("tcp-5020") == 5021
    assert session.project.find_channel("tcp-5020").port == 5021
    row.port_edit.setText("COM3")
    assert panel.apply_edits("tcp-5020") is None
    assert "is not a number" in panel.last_error
