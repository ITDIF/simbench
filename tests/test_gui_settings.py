"""设置接在界面上的那一半：恢复几何、记住最近打开、菜单里点得开、主题跟着存。

纯逻辑那一半在 `tests/test_settings.py`；这里只验"接线对不对"。
"""

from __future__ import annotations

import pathlib

from PySide6 import QtWidgets

from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui.main_window import MainWindow
from simbench.settings import Settings
from tests.conftest import free_port
from tests.gui_support import gui_deps

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def make_window(qapp, engine_runner, settings=None, path=CSV) -> MainWindow:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = free_port()
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    window = MainWindow(session, store, settings=settings, deps=gui_deps())
    window.use_project(project, path)
    window.show()
    qapp.processEvents()
    return window


def teardown(window, qapp) -> None:
    window.timer.stop()
    window.rows_pump.stop()
    window.session.shutdown()
    window.session.clear_dirty()
    window.deleteLater()
    qapp.processEvents()


def test_the_window_comes_back_at_the_size_it_was_closed(tmp_path, qapp, engine_runner):
    """窗口几何要恢复：否则每次开机都得重新拉一遍（多显示器上尤其烦）。"""
    settings = Settings(path=tmp_path / "settings.json")
    first = make_window(qapp, engine_runner, settings)
    try:
        # 尺寸要落在 offscreen 那块假屏里（800x800）：`restoreGeometry` 会把装不下的
        # 窗口往屏幕内收，越界的尺寸在用例里量不出"恢复对不对"
        first.resize(700, 600)
        qapp.processEvents()
        first.close()                       # 关窗那一刻写盘
    finally:
        teardown(first, qapp)
    assert settings.geometry, "关窗没记住几何"

    second = make_window(qapp, engine_runner, Settings.load(tmp_path / "settings.json"))
    try:
        assert (second.width(), second.height()) == (700, 600), \
            "恢复出来的尺寸不是关窗时那个"
    finally:
        teardown(second, qapp)


def test_a_window_without_settings_is_untouched(qapp, engine_runner):
    """不给设置就什么都不做（用例的几何断言因此不受用户机器上的设置影响）。"""
    window = make_window(qapp, engine_runner, None)
    try:
        assert window.settings is None
        # 没给设置 → 走"屏幕的 2/3 + 居中"那条老路（`_geometry_restored` 一个字节都没设）
        assert getattr(window, "_geometry_restored", False) is False
    finally:
        teardown(window, qapp)


def test_the_opened_file_lands_in_the_recent_menu(qapp, engine_runner, tmp_path):
    """打开过的东西要进"最近打开"；点一下能真打开它。"""
    settings = Settings(path=tmp_path / "settings.json")
    window = make_window(qapp, engine_runner, settings)
    try:
        assert settings.recent and settings.recent[0] == CSV

        window._rebuild_recent_menu()
        texts = [a.text() for a in window.act_recent.actions()]
        assert texts == [CSV], texts

        window.session.load(load_point_table(CSV)[0])     # 换成"别的工程"再点回来
        window.session.clear_dirty()
        window.act_recent.actions()[0].trigger()
        qapp.processEvents()
        assert window._path == pathlib.Path(CSV)
        assert window.session.project.channels[0].name == "tcp-5020"
    finally:
        teardown(window, qapp)


def test_a_recent_entry_that_vanished_is_dropped_on_click(qapp, engine_runner, tmp_path):
    """点到已经不在的文件：一句人话，并且把它从最近列表里摘掉（免得一直挂着死条目）。"""
    gone = tmp_path / "gone.csv"
    settings = Settings(path=tmp_path / "settings.json", recent=[str(gone)])
    window = make_window(qapp, engine_runner, settings, path=None)
    try:
        window._open_recent(str(gone))
        assert "打不开" in window.log_view.toPlainText()
        assert settings.recent == [], "打不开的条目还留在最近列表里"
    finally:
        teardown(window, qapp)


def test_the_recent_menu_says_so_when_there_is_nothing(tmp_path, qapp, engine_runner):
    window = make_window(qapp, engine_runner, Settings(path=tmp_path / "settings.json"),
                         path=None)
    try:
        window._rebuild_recent_menu()
        actions = window.act_recent.actions()
        assert len(actions) == 1 and actions[0].isEnabled() is False
    finally:
        teardown(window, qapp)


def test_the_theme_choice_is_remembered(tmp_path, qapp, engine_runner):
    settings = Settings(path=tmp_path / "settings.json")
    window = make_window(qapp, engine_runner, settings)
    try:
        window._toggle_theme(True)
        window.close()
    finally:
        teardown(window, qapp)
    assert Settings.load(tmp_path / "settings.json").theme in ("light", "dark")


def test_the_dialogs_start_from_the_last_used_folder(tmp_path, qapp, engine_runner):
    """文件对话框从上次那个目录开：现场的点表散在几个目录里，每次重找很烦。"""
    settings = Settings(path=tmp_path / "settings.json")
    window = make_window(qapp, engine_runner, settings)
    seen: list[str] = []

    def fake(parent, title, suggested, filters, options=None):
        seen.append(suggested)
        return ("", "")

    try:
        window._remember_dir(str(tmp_path / "点表.csv"))
        assert settings.last_dir == str(tmp_path)
        monkeypatch_free = QtWidgets.QFileDialog.getSaveFileName
        QtWidgets.QFileDialog.getSaveFileName = staticmethod(fake)
        try:
            window._path = None
            window._export_table()
        finally:
            QtWidgets.QFileDialog.getSaveFileName = monkeypatch_free
        assert seen and seen[0].startswith(str(tmp_path)), seen
    finally:
        teardown(window, qapp)
