"""核对页（主站模式的界面，E1）：扫 unit / 逐点核对 / 导出报告。

用**我们自己的从站**当真设备（真端口、真 datastore），界面那一侧走的是真线程 + 真信号。
长活必须在后台跑：扫 247 个 unit 每个等 1 秒超时就是几分钟，跑在界面线程上就是"窗口卡死"
（而操作员的第一反应是再点一次）。
"""

from __future__ import annotations

import pathlib
import socket
import time

import pytest
from PySide6 import QtWidgets

from simbench.backend import make_backend
from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui.main_window import MainWindow
from simbench.gui.verify_panel import BUTTON_WIDTHS, master_channel
from tests.gui_support import gui_deps

HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,"
          "initial,waveform,wave_param,readonly")
_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_table(tmp_path: pathlib.Path, port: int) -> pathlib.Path:
    target = tmp_path / "verify.csv"
    target.write_text(
        f"{HEADER}\n"
        f"tcp-1@tcp:127.0.0.1:{port},D1,11,温度,hr,0,uint16,ABCD,25,const,,false\n"
        f"tcp-1@tcp:127.0.0.1:{port},D2,12,别家,hr,0,uint16,ABCD,7,const,,false\n",
        encoding="utf-8")
    return target


@pytest.fixture
def rig(qapp, engine_runner, tmp_path):
    """起一路真从站 + 一个装着这份点表的窗口（核对页面对的就是它）。"""
    port = free_port()
    table = write_table(tmp_path, port)
    project, _notes, issues = load_point_table(str(table))
    assert not [i for i in issues if i.level == "error"]
    backend = make_backend(project.channels[0])
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "verify panel: start")
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    window = MainWindow(session, store, deps=gui_deps())
    window.use_project(project, str(table))
    window.show()
    qapp.processEvents()
    yield window, table, backend
    window.timer.stop()
    window.rows_pump.stop()
    window.session.shutdown()
    window.session.clear_dirty()
    window.deleteLater()
    qapp.processEvents()
    engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "verify panel: stop")


def wait_until(qapp, predicate, timeout: float = 20.0) -> bool:
    """等后台那件事做完（界面用的信号是排队投过来的，所以要转事件循环）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_verify_page_is_a_tab_with_three_fixed_width_buttons(rig, qapp):
    window, _table, _backend = rig
    panel = window.verify_panel
    assert window.tabs.indexOf(panel) >= 0, "核对页没加到标签里"
    assert (panel.scan_button.width(), panel.verify_button.width(),
            panel.export_button.width()) == (BUTTON_WIDTHS["扫描 unit"],
                                             BUTTON_WIDTHS["逐点核对"],
                                             BUTTON_WIDTHS["导出报告"])
    assert panel.export_button.isEnabled() is False, "还没有结论就允许导出"


def test_the_layout_does_not_move_when_the_status_text_changes(rig, qapp):
    """状态那一格是**定宽槽**：文字从"还没开始"变成一串 unit 号，几何一个像素都不动。"""
    window, _table, _backend = rig
    panel = window.verify_panel
    before = panel.geometry_snapshot()
    panel._say("应答的 unit（247 个）：1、2、3、…、247")
    qapp.processEvents()
    assert panel.status_text() != "还没开始"
    assert panel.geometry_snapshot() == before


def test_verifying_reads_the_real_slave_and_fills_the_table(rig, qapp):
    window, _table, _backend = rig
    panel = window.verify_panel
    panel.verify()
    # 表与状态都是**排队**投回来的：等到两者都落地再断言
    assert wait_until(qapp, lambda: panel.row_count() == 2
                      and "对得上 2" in panel.status_text()), panel.status_text()
    assert panel.model.item(0, 5).text() == "对得上", panel.model.item(0, 6).text()
    assert panel.model.item(1, 5).text() == "对得上"
    assert panel.export_button.isEnabled() is True
    assert "扫到" not in window.log_view.toPlainText()      # 核对不写"扫到"那一行


def test_scanning_reports_the_units_that_answer(rig, qapp):
    window, _table, _backend = rig
    panel = window.verify_panel
    panel.scan()
    assert wait_until(qapp, lambda: "应答的 unit" in panel.status_text(), timeout=60.0), \
        panel.status_text()
    assert "11" in panel.status_text() and "12" in panel.status_text()


def test_a_report_can_be_exported_from_the_page(rig, qapp, monkeypatch, tmp_path):
    window, _table, _backend = rig
    panel = window.verify_panel
    target = tmp_path / "报告.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "CSV (*.csv)")))
    panel.verify()
    assert wait_until(qapp, lambda: panel.row_count() == 2)
    panel.export()
    assert wait_until(qapp, lambda: target.exists()), panel.status_text()
    assert "温度" in target.read_text(encoding="utf-8")
    assert "核对报告已导出" in window.log_view.toPlainText()


def test_the_buttons_are_greyed_while_a_job_runs(rig, qapp, monkeypatch):
    """跑着的时候置灰**不隐藏**（界面口径 4/5）：几何不变，按钮还在原地。"""
    window, _table, _backend = rig
    panel = window.verify_panel
    window.tabs.setCurrentWidget(panel)          # `isVisible()` 只对"正在看的那一页"为真
    qapp.processEvents()
    before = panel.geometry_snapshot()
    panel.set_busy(True)
    assert not panel.scan_button.isEnabled() and not panel.verify_button.isEnabled()
    assert panel.scan_button.isVisible() and panel.verify_button.isVisible()
    assert panel.geometry_snapshot() == before
    panel.set_busy(False)
    assert panel.scan_button.isEnabled()


def test_the_page_refuses_when_there_is_no_modbus_channel(rig, qapp):
    """一份只有 S7 的点表：给一句人话，别让表空着（空表看着像"扫过了没发现"）。"""
    from simbench.project.model import Project, S7Channel
    window, _table, _backend = rig
    assert master_channel(Project(channels=[S7Channel("s7-1")])) is None
    window.verify_panel.session.load(Project(channels=[S7Channel("s7-1")]))
    window.verify_panel.scan()
    qapp.processEvents()
    assert "没有可用的 Modbus 通道" in window.verify_panel.status_text()
    assert "主站模式需要一条 Modbus 通道" in window.log_view.toPlainText()
