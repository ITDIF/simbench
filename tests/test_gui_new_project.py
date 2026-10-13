"""新建工程与增删（通道 / 设备 / 点位）：界面这一层的验收。

计划 `docs/superpowers/plans/2026-10-13-new-project.md` 的 §5 那 11 行在这里逐行成立。
两条纪律：

1. **用真对话框**（只把 `exec` 那一步模态嵌套循环换掉，换成"选类型 → 改字 → 点确定"）：
   转换、落地、被拒留在面板上这些形状都是真的在跑；换成替身类的话，"对话框里选的东西
   与落地的东西不是一回事"这类缺陷正好测不出来。
2. **被拒时工程与界面一个字节都不变**：拒绝要么发生在写回之前（通道在跑），要么由
   `project.edit` 整体回滚，两种都在这里对着工程对象逐字段比。
"""

import copy
import pathlib

import pytest
from PySide6 import QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui import main_window as main_window_module
from simbench.gui.channel_form import ChannelForm
from simbench.gui.main_window import MainWindow
from simbench.gui.new_project import AddChannelDialog, AddDeviceDialog, NewProjectDialog
from simbench.project import edit
from simbench.project import json_io
from tests.conftest import free_port

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def loaded(port=None):
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"], [str(i) for i in issues]
    if port:
        project.channels[0].port = port
    return project


def row_of(window, name: str) -> int:
    model = window.point_model
    return next(r for r in range(model.rowCount()) if model.index(r, 0).data() == name)


def rows(window) -> list[str]:
    return [window.point_model.index(r, 0).data()
            for r in range(window.point_model.rowCount())]


def drive(monkeypatch, dialog_class, kind=None, **fields):
    """把真对话框的 `exec` 换成"用户操作一遍"：选类型 → 改几格 → 点确定 → 回结果。"""
    def fake_exec(self, *_args):
        if kind is not None:
            self.form.pick(kind)
        for name, value in fields.items():
            getattr(self.form, name).setText(value)
        self._accept_if_applied()
        return self.result()

    monkeypatch.setattr(dialog_class, "exec", fake_exec)


def answer(monkeypatch, choice):
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: choice))


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """两个模态框换成记录器：`exec()` 在 offscreen 下会开嵌套事件循环，用例回不来。

    错误清单本身对不对由 `test_gui_main_window.py` 直接构造真对话框断言；这里只挡掉
    "点一下会弹框"那条路，顺便记录**属性面板被要求打开了哪一行**（加点位之后要开它）。
    """
    shown: list[tuple] = []

    class Issues:
        def __init__(self, issues, parent=None) -> None:
            pass

        def exec(self, *_args) -> int:
            return 0

    class Props:
        def __init__(self, parent, project, key) -> None:
            shown.append(key)
            self.applied = None

        def exec(self, *_args) -> int:
            return 0

    monkeypatch.setattr(main_window_module, "IssueDialog", Issues)
    monkeypatch.setattr(main_window_module, "PointPropertiesDialog", Props)
    return shown


@pytest.fixture
def make_window(qapp, engine_runner):
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


# ---------- 1/2. 新建工程 ----------

def test_a_blank_new_project_empties_every_view_and_says_where_to_start(
        make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, NewProjectDialog, edit.BLANK)
    window._new_project()
    qapp.processEvents()
    assert window.tree.topLevelItemCount() == 0
    assert rows(window) == []                       # 上一份工程的行不许留着
    assert window.channels.rows() == []
    assert window._path is None
    assert window.windowTitle() == "设备模拟台 - 未命名工程"
    assert "空工程" in window.log_view.toPlainText()
    assert window.session.dirty is False            # 空工程不值得在关窗时拦一下


def test_a_template_new_project_lands_with_one_of_everything(
        make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, NewProjectDialog, "tcp")
    window._new_project()
    qapp.processEvents()
    assert [ch.name for ch in window.session.project.channels] == ["tcp-5020"]
    assert window.tree.topLevelItemCount() == 1
    assert len(rows(window)) == 1
    assert window.windowTitle() == "设备模拟台 - 未命名工程 *"
    assert window.session.dirty is True


def test_a_template_new_project_can_be_named_and_ported_while_creating(
        make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, NewProjectDialog, "tcp",
          name_edit="网关-1", second_edit="15020")
    window._new_project()
    qapp.processEvents()
    channel = window.session.project.channels[0]
    assert (channel.name, channel.port) == ("网关-1", 15020)


def test_the_serial_template_needs_a_port_name_from_the_operator(
        make_window, monkeypatch, qapp):
    """串口名留空是故意的（不猜真实 COM 口），所以直接确定会被挡住、面板不关。"""
    window = make_window()
    drive(monkeypatch, NewProjectDialog, "rtu")
    window._new_project()
    qapp.processEvents()
    assert window.session.project.channels[0].name == "tcp-5020"   # 还是原来那份
    assert window.windowTitle().startswith("设备模拟台 - covi-tunnel")


def test_the_serial_template_lands_once_the_port_name_is_filled(
        make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, NewProjectDialog, "rtu", first_edit="COM3")
    window._new_project()
    qapp.processEvents()
    channel = window.session.project.channels[0]
    assert (channel.name, channel.port) == ("rtu-1", "COM3")


# ---------- 3/4. 加通道、加设备 ----------

def test_adding_a_channel_from_the_tree_updates_tree_panel_and_table(
        make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, AddChannelDialog, "s7")
    window.tree.add_channel_requested.emit()
    qapp.processEvents()
    assert [ch.name for ch in window.session.project.channels] == ["tcp-5020", "s7-1"]
    assert window.tree.topLevelItemCount() == 2
    assert len(window.channels.rows()) == 2
    assert window.point_model.channel() == "s7-1", "加完没有站到新通道上"
    assert window.session.dirty is True


def test_the_channel_dialog_prefills_around_what_is_already_there(make_window, qapp):
    """第二路 TCP 的起手值是 5021，不是 5020——给一个一按就报重复的默认值是让人白吃一次错。"""
    window = make_window(loaded(5020))
    dialog = AddChannelDialog(window, window.session.project)
    dialog.form.pick("tcp")
    assert dialog.form.second_edit.text() == "5021"


def test_a_channel_added_while_another_is_running_is_refused(
        make_window, qapp):
    """加通道要走 `Session.load`，而它第一步停掉**所有**在跑的通道：先拒绝，并点名。"""
    window = make_window(loaded(free_port()))
    window._start_all()
    qapp.processEvents()
    before = copy.deepcopy(window.session.project)
    called: list[int] = []
    window.tree.add_channel_requested.connect(lambda: called.append(1))
    window._add_channel()
    qapp.processEvents()
    assert called == [] and window.session.project == before
    assert "正在运行" in window.log_view.toPlainText()


def test_adding_a_device_from_the_tree_extends_the_channel(make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, AddDeviceDialog)
    window.tree.add_device_requested.emit("tcp-5020")
    qapp.processEvents()
    channel = window.session.project.channels[0]
    assert [dev.unit_id for dev in channel.devices] == [11, 12, 1]
    assert window.tree.item_for("tcp-5020").childCount() == 3
    assert window.session.dirty is True


def test_the_device_dialog_prefills_a_free_unit_and_name(make_window, qapp):
    window = make_window()
    dialog = AddDeviceDialog(window, window.session.project, "tcp-5020")
    assert (dialog.name_edit.text(), dialog.unit_edit.text()) == ("设备1", "1")
    dialog.unit_edit.setText("not a number")
    dialog._accept_if_applied()
    assert dialog.error_lines() == ["unit_id 'not a number' is not a number"]
    assert dialog.result() == 0                                   # 被拒：面板不关
    assert len(window.session.project.channels[0].devices) == 2   # 也没落地


def test_the_device_dialog_refuses_an_empty_name(make_window, qapp):
    window = make_window()
    dialog = AddDeviceDialog(window, window.session.project, "tcp-5020")
    dialog.name_edit.setText("   ")
    dialog._accept_if_applied()
    assert "不许为空" in dialog.error_lines()[0]
    assert len(window.session.project.channels[0].devices) == 2


# ---------- 5. 加点位 ----------

def test_adding_a_point_lands_with_a_free_address_and_opens_the_properties(
        make_window, qapp, no_modal_dialogs):
    window = make_window()
    window.tree.add_point_requested.emit("tcp-5020", 11)
    qapp.processEvents()
    device = edit.find_device(window.session.project, "tcp-5020", 11)
    added = device.points[-1]
    # 示例点表在 hr 上占到 11：`铭牌` 是 string "FAN-01"，占 3 个寄存器（9..11），
    # 所以新点必须落在 12——只看"最后一个点的首地址"就会算出重叠。
    assert (added.name, added.area, added.address, added.datatype) == \
        ("点位1", "hr", 12, "uint16")
    assert "点位1" in rows(window)
    assert no_modal_dialogs == [("tcp-5020", 11, "hr", 12)], "加完没打开属性面板"
    assert window.session.dirty is True


def test_adding_a_point_from_the_table_uses_that_rows_device(make_window, qapp):
    """表上右键哪一行，就加到那一行的设备里（示例点表两台设备，unit 11 与 12）。"""
    window = make_window()
    window.point_table.add_point_requested.emit(("tcp-5020", 12, "hr", 0))
    qapp.processEvents()
    assert edit.find_device(window.session.project, "tcp-5020", 12).points[-1].name == "点位1"
    assert len(edit.find_device(window.session.project, "tcp-5020", 11).points) == 8


def test_adding_a_point_while_its_channel_runs_is_refused(make_window, qapp):
    window = make_window(loaded(free_port()))
    window._start_all()
    qapp.processEvents()
    before = copy.deepcopy(window.session.project)
    window.tree.add_point_requested.emit("tcp-5020", 11)
    qapp.processEvents()
    assert window.session.project == before
    assert "正在运行" in window.log_view.toPlainText()
    window._stop_all()
    qapp.processEvents()
    window.tree.add_point_requested.emit("tcp-5020", 11)
    qapp.processEvents()
    assert window.session.project != before


# ---------- 6. 删（点位 / 设备 / 通道） ----------

def test_removing_a_point_asks_first_and_does_nothing_when_declined(
        make_window, monkeypatch, qapp):
    window = make_window()
    before = copy.deepcopy(window.session.project)
    answer(monkeypatch, QtWidgets.QMessageBox.No)
    window.point_table.remove_point_requested.emit(("tcp-5020", 11, "hr", 4))
    qapp.processEvents()
    assert window.session.project == before
    assert "风机状态" in rows(window)
    assert "已删除" not in window.log_view.toPlainText()


def test_removing_a_point_takes_exactly_that_row(make_window, monkeypatch, qapp):
    window = make_window()
    answer(monkeypatch, QtWidgets.QMessageBox.Yes)
    window.point_table.remove_point_requested.emit(("tcp-5020", 11, "hr", 4))
    qapp.processEvents()
    assert "风机状态" not in rows(window)
    assert "CO浓度" in rows(window)
    assert "已删除点位 风机状态" in window.log_view.toPlainText()
    assert window.session.dirty is True


def test_removing_a_device_takes_its_points_with_it(make_window, monkeypatch, qapp):
    window = make_window()
    answer(monkeypatch, QtWidgets.QMessageBox.Yes)
    window.tree.remove_device_requested.emit("tcp-5020", 12)
    qapp.processEvents()
    channel = window.session.project.channels[0]
    assert [dev.unit_id for dev in channel.devices] == [11]
    assert "回路状态" not in rows(window)


def test_removing_the_only_channel_leaves_an_empty_project(make_window, monkeypatch, qapp):
    window = make_window()
    answer(monkeypatch, QtWidgets.QMessageBox.Yes)
    window.tree.remove_channel_requested.emit("tcp-5020")
    qapp.processEvents()
    assert window.session.project.channels == []
    assert window.tree.topLevelItemCount() == 0
    assert rows(window) == []                       # 表要跟着空，不许留着那一屏
    assert window.session.dirty is True


def test_a_running_channel_is_named_when_a_removal_is_refused(make_window, monkeypatch, qapp):
    window = make_window(loaded(free_port()))
    window._start_all()
    qapp.processEvents()
    before = copy.deepcopy(window.session.project)
    answer(monkeypatch, QtWidgets.QMessageBox.Yes)   # 就算确认了也不许删
    window.tree.remove_channel_requested.emit("tcp-5020")
    qapp.processEvents()
    assert window.session.project == before
    assert "tcp-5020" in window.log_view.toPlainText()


# ---------- 7. 未保存改动：四个动作一起问 ----------

def test_new_project_asks_about_unsaved_changes_first(make_window, monkeypatch, qapp):
    window = make_window()
    window.session.mark_dirty()
    answer(monkeypatch, QtWidgets.QMessageBox.Cancel)
    opened: list[int] = []
    monkeypatch.setattr(NewProjectDialog, "exec",
                        lambda self, *a: opened.append(1) or 0)
    window._new_project()
    assert opened == [], "取消之后连新建对话框都不该打开"
    assert [ch.name for ch in window.session.project.channels] == ["tcp-5020"]
    assert window.session.dirty is True


def test_open_and_import_ask_about_unsaved_changes_first(make_window, monkeypatch):
    window = make_window()
    window.session.mark_dirty()
    answer(monkeypatch, QtWidgets.QMessageBox.Cancel)
    called: list[int] = []
    monkeypatch.setattr(main_window_module, "open_project",
                        lambda parent=None: called.append(1))
    monkeypatch.setattr(main_window_module, "import_csv",
                        lambda parent=None: called.append(2))
    window._open_project()
    window._import_csv()
    assert called == []
    assert window.session.dirty is True


def test_saving_from_the_ask_writes_the_project(make_window, monkeypatch, tmp_path, qapp):
    """选"保存"就真存：另存为成功之后动作才继续（这里继续到新建对话框）。"""
    window = make_window()
    window.session.mark_dirty()
    answer(monkeypatch, QtWidgets.QMessageBox.Save)
    target = str(tmp_path / "saved.json")
    def fake_save(parent, project, suggested=""):
        json_io.save(project, target)          # 真写一份：主窗读的是返回值，盘上也得有东西
        return target

    monkeypatch.setattr(main_window_module, "save_project_as", fake_save)
    drive(monkeypatch, NewProjectDialog, edit.BLANK)
    window._new_project()
    qapp.processEvents()
    assert pathlib.Path(target).exists()
    assert window.session.project.channels == []
    assert window.windowTitle() == "设备模拟台 - 未命名工程"


# ---------- 8/9. 导出（含往返） ----------

def test_a_new_project_exports_a_csv_that_comes_back_the_same(
        make_window, monkeypatch, tmp_path, qapp):
    window = make_window()
    drive(monkeypatch, NewProjectDialog, "tcp")
    window._new_project()
    qapp.processEvents()
    target = tmp_path / "fresh.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "CSV (*.csv)")))
    window._export_csv()
    project, _notes, issues = load_point_table(str(target))
    assert not [i for i in issues if i.level == "error"], [str(i) for i in issues]
    assert [(ch.name, ch.port) for ch in project.channels] == [("tcp-5020", 5020)]
    points = project.channels[0].devices[0].points
    assert [(pt.name, pt.area, pt.address, pt.datatype) for pt in points] == \
        [("点位1", "hr", 0, "uint16")]


def test_exporting_says_when_the_port_is_not_written_in_the_channel_name(
        make_window, monkeypatch, tmp_path, qapp):
    """CSV 没有端口列：端口靠通道名尾数带过去。名字与端口对不上时导出这边也要说一句。"""
    window = make_window()
    project = edit.new_project("tcp", name="网关", port=15020)
    window.use_project(project, None)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(tmp_path / "x.csv"), "CSV (*.csv)")))
    window._export_csv()
    assert "不在名字里" in window.log_view.toPlainText()


# ---------- 10. 空工程不该炸 ----------

def test_starting_an_empty_project_does_nothing_instead_of_crashing(
        make_window, monkeypatch, qapp):
    window = make_window()
    drive(monkeypatch, NewProjectDialog, edit.BLANK)
    window._new_project()
    window._start_all()
    window._stop_all()
    window._check()
    qapp.processEvents()
    assert window.status_label.text() == "未启动"
    assert window.point_model.rowCount() == 0


# ---------- 11. 界面合同：没有 QComboBox、几何恒定 ----------

def test_the_channel_form_has_no_combo_box_and_keeps_its_geometry(qapp):
    form = ChannelForm(kinds=(edit.BLANK,) + edit.TRANSPORTS, start=edit.BLANK)
    form.show()
    qapp.processEvents()
    baseline = form.geometry_snapshot()
    for kind in ("tcp", "rtu", "s7", edit.BLANK, "tcp"):
        form.pick(kind)
        qapp.processEvents()
        assert form.geometry_snapshot() == baseline, f"{kind} 之后几何变了"
    assert form.findChildren(QtWidgets.QComboBox) == []
    form.deleteLater()


def test_the_dialogs_keep_their_geometry_when_a_refusal_shows_up(qapp):
    """被拒时理由进那块**定宽**的只读框：对话框本身不许被理由撑大。"""
    dialog = NewProjectDialog()
    dialog.show()
    qapp.processEvents()
    baseline = dialog.size()
    dialog.form.pick("rtu")
    dialog._accept_if_applied()
    qapp.processEvents()
    assert dialog.error_lines(), "这条用例要的是'被拒并显示理由'"
    assert dialog.size() == baseline
    assert dialog.result() == 0, "被拒之后面板不该关"
    dialog.deleteLater()


# ---------- 12. 从零搭出来的工程，真主站读得到（这个里程碑的正面验收） ----------

def test_a_project_built_from_scratch_serves_a_real_master(
        make_window, monkeypatch, qapp):
    """空白 → 加通道 → 加设备 → 加点位 → 启动 → 真主站读写。

    这条把计划 §5 那 12 行串成一件真事：全程只点界面（`exec` 那一步换成"用户操作一遍"），
    没有任何"先手写一份 CSV"的绕路。主站是真的 `ModbusTcpClient`，端口是测试挑的空闲口
    （新建工程默认那个 5020 在用例里不能用：可能被别人占着）。
    """
    from pymodbus.client import ModbusTcpClient

    window = make_window()
    port = free_port()
    drive(monkeypatch, NewProjectDialog, edit.BLANK)
    window._new_project()
    qapp.processEvents()
    assert window.session.project.channels == []

    drive(monkeypatch, AddChannelDialog, "tcp",
          name_edit=f"tcp-{port}", second_edit=str(port))
    window.tree.add_channel_requested.emit()
    qapp.processEvents()
    channel_name = f"tcp-{port}"
    assert window.session.project.channels[0].port == port

    drive(monkeypatch, AddDeviceDialog)
    window.tree.add_device_requested.emit(channel_name)
    qapp.processEvents()
    assert window.session.project.channels[0].devices[0].unit_id == 1

    window.tree.add_point_requested.emit(channel_name, 1)
    qapp.processEvents()
    assert rows(window) == ["点位1"]

    window._start_all()
    qapp.processEvents()
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=2.0)
    try:
        assert client.connect(), f"真主站连不上 127.0.0.1:{port}"
        assert client.read_holding_registers(0, count=1, device_id=1).registers == [0]
        client.write_register(0, 4242, device_id=1)
        assert client.read_holding_registers(0, count=1, device_id=1).registers == [4242]
    finally:
        client.close()
        window._stop_all()
        qapp.processEvents()
