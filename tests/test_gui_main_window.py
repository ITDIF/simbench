"""主窗组装：设备树、点位表、报文面板、通道面板与 Session 之间的**接线**。

这个文件钉的全是"现场一按就出事"的形状：

1. **界面点一下，外面真读得到**：表里改的值经 Session 写进寄存器，第三方主站读到同一个数；
   主站写进来的值在下一次心跳就回到表里（心跳是 10Hz）。用例一律走真 socket、真
   `ModbusTcpClient`，不用 mock。
2. **状态三处同步**：通道起了/停了/失败了，设备树那一格、通道面板那一行、状态栏摘要必须
   同时变，而且来自同一次事件——两份真相迟早给出两个答案。
3. **拒绝要有名字**（β）：改值被拒时屏幕上的话必须带点位名，那一格保持原值，
   原因进状态栏与日志，不弹模态框。
4. **没起也要能看**（ζ）：一个通道都没起时表格照样可读，改值被拒并说明"通道没在跑"。
5. **未保存改动**（ε）：标题带 `*`，关闭时问一次；保存成功才把 `*` 清掉。
6. **槽函数不许炸**：端口被占这类运行期异常落成日志 + 红色状态，不能从 Qt 槽里抛出去
   （那是"界面像卡死"的来源）。等引擎回话的路径超时要把引擎当时的栈一起带出来。

夹具用会话级共享的 `engine_runner`：同进程攒十几个 asyncio loop 会互相饿死（表现为偶发的
`.result()` 超时），共享之后既快又不引入新的时序风险。
"""

import pathlib
import socket

import pytest
from PySide6 import QtCore, QtWidgets
from pymodbus.client import ModbusTcpClient

from modslave.backend.events import ChannelState
from modslave.backend.modbus_tcp import check_port_is_free
from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import PacketStore
from modslave.engine.session import Session
from modslave.gui import main_window as main_window_module
from modslave.gui.dialogs import IssueDialog
from modslave.gui.main_window import MainWindow
from modslave.project.model import Device, Point, TcpChannel
from modslave.project.validate import validate_project
from tests.conftest import free_port

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")
VALUE_COLUMN = 6                       # gui/point_table.COLUMNS 里的"当前值"


def second_channel(port: int) -> TcpChannel:
    """另一路 485：现场最常见的多通道形态，用来钉"独立启停"和"树切换表"。"""
    return TcpChannel(name="tcp-b", host="127.0.0.1", port=port, devices=[
        Device(name="照明回路-2", unit_id=21,
               points=[Point("回路温度", "hr", 0, "uint16", initial="7")])])


def loaded(port: int | None = None):
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"], [str(i) for i in issues]
    if port:
        project.channels[0].port = port
    return project


def row_of(window, name: str) -> int:
    model = window.point_model
    return next(r for r in range(model.rowCount()) if model.index(r, 0).data() == name)


def value_of(window, name: str) -> str:
    return window.point_model.index(row_of(window, name), VALUE_COLUMN).data()


def listening_hog():
    """占住一个端口并 listen（backlog 给足，别用 1：满队列会让探针看不见握手）。"""
    hog = socket.socket()
    hog.bind(("127.0.0.1", 0))
    hog.listen(128)
    return hog, hog.getsockname()[1]


def connect(port: int) -> ModbusTcpClient:
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=2.0)
    assert client.connect(), f"真主站连不上 127.0.0.1:{port}"
    return client


@pytest.fixture(autouse=True)
def issue_lists(monkeypatch):
    """把模态的错误清单换成记录器。

    `IssueDialog.exec()` 在 offscreen 下会开一个**嵌套事件循环**，用例永远回不来。清单
    本身对不对由 `test_the_issue_dialog_lists_every_error` 直接构造真对话框断言（不 exec），
    这里只钉"哪些清单被交给了对话框"以及状态栏那一句。
    """
    shown: list[list[str]] = []

    class Recorder:
        def __init__(self, issues, parent=None) -> None:
            shown.append([str(i) for i in issues])

        def exec(self, *_args) -> int:
            return 0

    monkeypatch.setattr(main_window_module, "IssueDialog", Recorder)
    return shown


@pytest.fixture
def make_window(qapp, engine_runner):
    """工厂夹具：每条用例自己决定工程长什么样，teardown 统一收窗、收通道。

    runner 是会话级共享的，`session.shutdown()` 因此只停通道不收线程（归属口径）；
    端口一律换成 `free_port()`，用例不许去抢点表里写死的 5020。
    """
    built: list[MainWindow] = []

    def _make(project=None, path=CSV):
        store = PacketStore()
        session = Session(runner=engine_runner, packet_store=store)
        window = MainWindow(session, store)
        window.use_project(project if project is not None else loaded(free_port()), path)
        window.resize(1200, 700)
        window.show()
        qapp.processEvents()
        built.append(window)
        return window

    yield _make
    for window in built:
        window.timer.stop()
        window.session.shutdown()
        window.deleteLater()
    qapp.processEvents()


@pytest.fixture
def window(make_window):
    return make_window()


def relaunch(window, project, qapp):
    """换端口后要重新装载一次：通道是按点表建后端的，改完 project 不重装载不生效。"""
    window.use_project(project, window._path)
    qapp.processEvents()
    window._start_all()
    qapp.processEvents()
    return window.session.channel("tcp-5020")


# ---------- 标题与脏标记（口径 ε） ----------

def test_the_title_shows_the_project_and_no_star_while_clean(window):
    assert window.windowTitle() == f"modslave - {pathlib.Path(CSV).name}"
    assert "*" not in window.windowTitle()


def test_a_successful_edit_marks_the_title_dirty(window, qapp):
    relaunch(window, window.session.project, qapp)
    assert window.point_model.setData(
        window.point_model.index(row_of(window, "风机状态"), VALUE_COLUMN), "21") is True
    window._tick()
    assert window.windowTitle().endswith("*")


def test_the_tick_only_rewrites_the_title_when_dirty_changes(window):
    """10Hz 心跳每跳都重画标题的话，标题栏会跟着闪；只有真变化才改（已定口径）。"""
    seen: list[str] = []
    original = window.setWindowTitle

    def counting(title):
        seen.append(title)
        return original(title)

    window.setWindowTitle = counting
    for _ in range(5):
        window._tick()
    assert seen == [], f"脏标记没变却重画了 {len(seen)} 次标题"
    window.session.mark_dirty()
    window._tick()
    assert len(seen) == 1, seen
    window.session.clear_dirty()
    window._tick()
    assert len(seen) == 2, seen


def test_saving_clears_the_star_and_remembers_the_path(window, tmp_path, monkeypatch):
    target = str(tmp_path / "saved.json")
    monkeypatch.setattr(main_window_module, "save_project_as",
                        lambda parent, project, suggested="": target)
    window.session.mark_dirty()
    window._tick()
    assert window.windowTitle().endswith("*")
    assert window._save_project() == target
    assert window.session.dirty is False
    assert "*" not in window.windowTitle()
    assert window._path == pathlib.Path(target)


def test_a_cancelled_save_keeps_the_star(window, monkeypatch):
    """用户在另存为里按了取消：改动还在，`*` 就不许消失（不然他以为已经存过了）。"""
    monkeypatch.setattr(main_window_module, "save_project_as",
                        lambda parent, project, suggested="": None)
    window.session.mark_dirty()
    assert window._save_project() is None
    assert window.session.dirty is True


# ---------- 启停：三处同步 ----------

def test_start_all_flips_the_tree_the_panel_and_the_status_bar(window, qapp):
    assert window.tree.topLevelItem(0).text(1) == "○"
    assert window.channels.rows()[0].status.text() == "○ 停止"
    window._start_all()
    qapp.processEvents()
    assert window.tree.topLevelItem(0).text(1) == "●"
    assert window.channels.rows()[0].status.text() == "● 运行"
    assert "tcp-5020:运行" in window.status_label.text()
    assert "[running] tcp-5020" in window.log_view.toPlainText()


def test_stop_all_flips_everything_back(window, qapp):
    window._start_all()
    qapp.processEvents()
    window._stop_all()
    qapp.processEvents()
    assert window.tree.topLevelItem(0).text(1) == "○"
    assert window.channels.rows()[0].status.text() == "○ 停止"
    assert "tcp-5020:停止" in window.status_label.text()


def test_a_refused_start_becomes_a_red_row_and_a_log_line_not_a_crash(make_window,
                                                                       qapp):
    """端口被别人占着：`_start_all` 不许把异常抛出槽函数，界面上是"失败"+一句原因。

    这也是 connect 级端口探针在 GUI 那条路上的落地检查——占位方在 `127.0.0.1`，
    模拟器的通道按点表绑 `0.0.0.0`，裸 bind 探针在这种组合上是**看不见**冲突的。
    """
    hog, port = listening_hog()
    try:
        window = make_window(loaded(port))
        window._start_all()
        qapp.processEvents()
        assert window.tree.topLevelItem(0).text(1) == "▲"
        assert window.channels.rows()[0].status.text() == "▲ 失败"
        assert not window.session.channel("tcp-5020").running
        assert "may already be in use" in window.log_view.toPlainText()
    finally:
        hog.close()


def test_the_tree_ignores_a_state_for_a_channel_it_does_not_have(window, qapp):
    """状态事件可能比 rebuild 先到（换工程的瞬间）：没有那一行就什么也不动，不炸。"""
    window.bridge.forward(ChannelState("tcp-9", "running", "127.0.0.1:1"))
    qapp.processEvents()
    assert [window.tree.topLevelItem(i).text(1)
            for i in range(window.tree.topLevelItemCount())] == ["○"]


# ---------- 改值 ↔ 真主站 ----------

def test_a_value_written_in_the_table_reaches_a_real_master(window, qapp):
    runtime = relaunch(window, window.session.project, qapp)
    assert runtime.running
    assert window.point_model.setData(
        window.point_model.index(row_of(window, "风机状态"), VALUE_COLUMN), "33") is True
    port = int(runtime.detail.rpartition(":")[2])
    client = connect(port)
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [33]
    finally:
        client.close()


def test_a_master_write_lands_in_the_table_on_the_next_tick(window, qapp):
    """拉模型的正证：主站写完（ACK 已回、datastore 已更新），下一次心跳就该看见。"""
    runtime = relaunch(window, window.session.project, qapp)
    port = int(runtime.detail.rpartition(":")[2])
    client = connect(port)
    try:
        client.write_register(4, 77, device_id=11)
    finally:
        client.close()
    window._tick()
    assert value_of(window, "风机状态") == "77", "主站写入没能在一个心跳内回到表里"


def test_frames_show_up_in_the_packet_panel(window, qapp):
    runtime = relaunch(window, window.session.project, qapp)
    port = int(runtime.detail.rpartition(":")[2])
    client = connect(port)
    client.read_holding_registers(0, count=1, device_id=11)
    client.close()
    timer = QtCore.QElapsedTimer()
    timer.start()
    while window.packet_panel.row_count() < 2 and timer.elapsed() < 4000:
        QtCore.QThread.msleep(20)
        qapp.processEvents()
    assert window.packet_panel.row_count() >= 2, "报文面板一帧都没收到"
    window._tick()                       # 心跳才重绘：面板的 model 跟着 store 走
    qapp.processEvents()
    assert window.packet_panel.model.rowCount() == window.packet_panel.row_count()
    assert window.packet_panel.repaint_count >= 1
    assert "unit 11" in window.packet_panel.visible_rows()[-1].parsed


def test_a_refused_edit_keeps_the_old_value_and_names_the_point(window, qapp):
    """口径 β：拒绝就地提示（状态栏 + 日志），旧值不动，消息里带点位名。"""
    relaunch(window, window.session.project, qapp)
    before = value_of(window, "风机状态")
    assert window.point_model.setData(
        window.point_model.index(row_of(window, "风机状态"), VALUE_COLUMN),
        "not-a-number") is False
    assert value_of(window, "风机状态") == before
    window._tick()
    assert "风机状态" in window.status.currentMessage()
    assert "uint16 needs an integer" in window.log_view.toPlainText()


def test_an_engine_timeout_is_reported_with_the_engine_stack_not_as_a_hang(
        window, qapp, monkeypatch):
    """GUI 等引擎的每一条路都走 `runner.call(...)`：超时要把引擎卡在哪一起带出来。

    这里把 `call` 换成"必然超时"的那一个，逼出这条路径，再确认屏幕上出现的不是空白或
    "没响应"三个字，而是一句写明 `engine thread did not finish` 的原因；状态栏只给第一行
    （多行栈进日志窗，不给状态栏撑高度）。
    """
    relaunch(window, window.session.project, qapp)
    stack = ("\n  | File \"modslave/engine/runner.py\", line 1, in _run\n")
    monkeypatch.setattr(window.session._runner, "call",
                        lambda factory, seconds, what: (_ for _ in ()).throw(
                            TimeoutError(f"{what}: engine thread did not finish within "
                                         f"{seconds}s; engine stack tail:{stack}")))
    assert window.point_model.setData(
        window.point_model.index(row_of(window, "风机状态"), VALUE_COLUMN), "42") is False
    window._tick()
    assert "did not finish" in window.log_view.toPlainText()
    assert "modslave/engine/runner.py" in window.log_view.toPlainText(), "整段栈要留在日志"
    assert "\n" not in window.status.currentMessage()


# ---------- ζ：一个通道都没起时 ----------

def test_the_table_is_readable_before_any_channel_starts(window):
    assert window.point_model.rowCount() == 9
    assert value_of(window, "CO浓度") == "12.5"
    assert window.session.channel_names() == ["tcp-5020"]


def test_editing_while_every_channel_is_stopped_is_refused_with_a_reason(window):
    """ζ：没起通道时改值被拒，而给出的原因是"这个通道没在跑"，不是 traceback。"""
    assert window.point_model.setData(
        window.point_model.index(row_of(window, "风机状态"), VALUE_COLUMN), "5") is False
    assert value_of(window, "风机状态") == "11"
    window._tick()
    assert "not running" in window.status.currentMessage()
    assert "风机状态" in window.log_view.toPlainText()


# ---------- 设备树 → 点位表 ----------

def test_picking_a_channel_in_the_tree_switches_the_table(make_window, qapp):
    """树不只是状态灯：点了第二个通道，表就要给那个通道的点，不许还演第一个通道。"""
    project = loaded(free_port())
    project.channels.append(second_channel(free_port()))
    window = make_window(project)
    assert window.point_model.channel() == "tcp-5020"
    window.tree.setCurrentItem(window.tree.topLevelItem(1))
    qapp.processEvents()
    assert window.point_model.channel() == "tcp-b"
    assert window.point_model.rowCount() == 1
    assert window.point_model.index(0, 0).data() == "回路温度"


# ---------- 多通道独立启停 ----------

def test_two_channels_start_and_stop_independently(make_window, qapp):
    """现场常常只接了一路：停掉一个通道不许动另一个的端口和状态。"""
    project = loaded()
    first, second = free_port(), free_port()
    project.channels[0].port = first
    project.channels.append(second_channel(second))
    window = make_window(project)
    window._start_all()
    qapp.processEvents()
    assert window.session.channel("tcp-5020").running
    assert window.session.channel("tcp-b").running
    rows = {row.channel: row for row in window.channels.rows()}
    rows["tcp-b"].stop_button.click()
    qapp.processEvents()
    assert not window.session.channel("tcp-b").running
    assert window.session.channel("tcp-5020").running
    assert rows["tcp-b"].status.text() == "○ 停止"
    assert rows["tcp-5020"].status.text() == "● 运行"
    client = connect(first)                        # 另一个停了不碍这个通道服务
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
    finally:
        client.close()
    assert "tcp-5020:运行" in window.status_label.text()
    assert "tcp-b:停止" in window.status_label.text()


# ---------- 工程读写 ----------

def test_open_project_remembers_the_path(window, monkeypatch, tmp_path):
    target = tmp_path / "again.csv"
    target.write_text(pathlib.Path(CSV).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(target), "工程 (*.csv)")))
    window._open_project()
    assert window._path == target
    assert window.windowTitle() == f"modslave - {target.name}"
    assert window.point_model.rowCount() == 9


def test_a_dirty_point_table_is_an_issue_list_not_a_traceback(window, monkeypatch,
                                                             tmp_path, issue_lists):
    """读不懂的表要走 Issue 清单（和 CLI 同一个姿态），不许在槽函数里抛 traceback。"""
    bad = tmp_path / "bad.csv"
    bad.write_text("channel,device,unit_id,name,area,address,datatype,word_order,"
                   "initial,waveform,wave_param,readonly\n"
                   "tcp-1,D,11,x,hr,notanumber,uint16,ABCD,0,const,,false\n",
                   encoding="utf-8")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(bad), "工程 (*.csv)")))
    window._open_project()
    log = window.log_view.toPlainText()
    assert "parse" in log, log
    assert "Traceback" not in log
    assert issue_lists and "parse" in issue_lists[-1][0]
    assert "line 2" in issue_lists[-1][0], "行号要在清单里，不然厂商改不动那张表"


def test_import_csv_logs_the_guessed_port_note(window, monkeypatch, tmp_path):
    """导入时端口是按通道名猜的，这条提醒必须进日志（GUI 不能比 CLI 少说话）。"""
    src = tmp_path / "vendor.csv"
    src.write_text("channel,device,unit_id,name,area,address,datatype,word_order,"
                   "initial,waveform,wave_param,readonly\n"
                   "tcp-1502,D,11,x,hr,0,uint16,ABCD,1,const,,false\n",
                   encoding="utf-8")
    monkeypatch.setattr(main_window_module, "import_csv", lambda parent=None: str(src))
    monkeypatch.setattr(main_window_module, "choose_offset", lambda parent=None: 0)
    window._import_csv()
    assert "port guessed as 1502" in window.log_view.toPlainText()
    assert window.session.channel("tcp-1502") is not None
    assert window.point_model.rowCount() == 1


def test_export_csv_writes_the_point_table_next_to_the_project(window, tmp_path):
    window._path = tmp_path / "proj.json"
    window._export_csv()
    exported = (tmp_path / "proj.csv").read_text(encoding="utf-8")
    assert "CO浓度" in exported
    assert "已导出 CSV" in window.log_view.toPlainText()


def test_export_without_a_path_says_so_instead_of_crashing(window):
    window._path = None
    window._export_csv()
    assert "没有工程路径" in window.log_view.toPlainText()


# ---------- 检查点表 ----------

def test_check_reports_everything_the_validator_found(window, issue_lists):
    project = loaded()
    project.channels[0].port = 0
    project.tick_ms = 5
    window.use_project(project, window._path)
    issue_lists.clear()
    window._check()
    codes = [line.split("]")[0].lstrip("[") for line in issue_lists[-1]]
    assert "port" in codes and "tick_ms" in codes, issue_lists[-1]
    assert "2 个错误" in window.status.currentMessage()


def test_the_issue_dialog_lists_every_error_with_warnings_separate(qapp):
    """清单必须一条不少，且 warning 不混在 error 里（真错被淹没就等于没报）。"""
    project = loaded()
    project.channels[0].port = 0
    project.tick_ms = 5
    # 单寄存器类型填非 ABCD 字序是 warning（§8），正好用来验"两段分开展示"
    point = next(p for p in project.channels[0].devices[0].points if p.datatype == "uint16")
    point.word_order = "CDAB"
    issues = validate_project(project)
    errors = [str(i) for i in issues if i.level == "error"]
    warnings = [str(i) for i in issues if i.level == "warning"]
    assert errors and warnings, [str(i) for i in issues]

    dialog = IssueDialog(issues)
    body = dialog.findChild(QtWidgets.QPlainTextEdit).toPlainText()
    for line in errors:
        assert line in body, line
    for line in warnings:
        assert f"(warning) {line}" in body, line
    assert dialog.windowTitle() == f"点表检查：{len(errors)} 错误 / {len(warnings)} 警告"
    errors_first = body.index(errors[0]) < body.index(f"(warning) {warnings[0]}")
    assert errors_first, "warning 排在前面会把真错挤下屏"


# ---------- 关闭（口径 ε） ----------

def test_closing_with_unsaved_edits_asks_once_and_cancel_keeps_the_window(window,
                                                                          monkeypatch):
    asked: list[int] = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(1)
                                     or QtWidgets.QMessageBox.Cancel))
    window.session.mark_dirty()
    window.close()
    assert asked == [1], "有未保存改动必须问一次"
    assert window.isVisible()


def test_closing_a_clean_window_does_not_ask(window, monkeypatch):
    asked: list[int] = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(1)
                                     or QtWidgets.QMessageBox.Cancel))
    window.close()
    assert asked == []
    assert not window.isVisible()


def test_discarding_unsaved_edits_closes_and_shuts_the_session_down(window, monkeypatch):
    port = window.session.project.channels[0].port
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Discard))
    window._start_all()
    QtWidgets.QApplication.processEvents()
    window.session.mark_dirty()
    window.close()
    assert not window.isVisible()
    assert not window.session.channel("tcp-5020").running
    assert not window.timer.isActive(), "关窗还留着 10Hz 心跳就是在往死对象上刷表"
    check_port_is_free("127.0.0.1", port)         # 通道真收干净了


def test_a_cancelled_save_at_close_keeps_the_window_open(window, monkeypatch):
    """选"保存"又在文件对话框里按取消：不能把没存下来的改动带出窗口。"""
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.Save))
    monkeypatch.setattr(main_window_module, "save_project_as",
                        lambda parent, project, suggested="": None)
    window.session.mark_dirty()
    window.close()
    assert window.isVisible()
    assert window.session.dirty is True


def test_the_window_shuts_its_session_down_only_once(window, monkeypatch):
    """关闭走一遍 `session.shutdown()`：重复关窗不许把已经停掉的通道再停一次。"""
    calls: list[int] = []
    monkeypatch.setattr(window.session, "shutdown", lambda: calls.append(1))
    window.close()
    window.close()
    assert calls == [1], calls
