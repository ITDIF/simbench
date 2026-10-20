"""启动时默认接着上次的工程：没给路径就打开"最近打开"里第一条还在的文件。"""

from __future__ import annotations

import json
import pathlib

from simbench.gui import app as app_module

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")
OTHER = str(ROOT / "examples" / "modbus-poll-3units.csv")


def _write_settings(tmp_path, recent):
    (tmp_path / "settings.json").write_text(
        json.dumps({"recent": recent, "recent_offsets": {}}, ensure_ascii=False),
        encoding="utf-8")


def _close(window, session):
    window.timer.stop()
    window.deleteLater()
    session.shutdown()


def test_no_args_reopens_the_last_project(qapp, engine_runner, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMBENCH_SETTINGS_DIR", str(tmp_path))
    _write_settings(tmp_path, [CSV])
    _app, window, session = app_module.build_app([])
    try:
        assert window._path == pathlib.Path(CSV)
        assert session.project.channels, "上次的工程没有被装进来"
    finally:
        _close(window, session)


def test_dead_entries_are_skipped_and_the_next_one_opens(qapp, engine_runner, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMBENCH_SETTINGS_DIR", str(tmp_path))
    gone = str(tmp_path / "已经删掉的.csv")
    _write_settings(tmp_path, [gone, OTHER])
    _app, window, session = app_module.build_app([])
    try:
        assert window._path == pathlib.Path(OTHER)
        assert session.project.channels
    finally:
        _close(window, session)


def test_command_line_path_wins_over_history(qapp, engine_runner, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMBENCH_SETTINGS_DIR", str(tmp_path))
    _write_settings(tmp_path, [OTHER])
    _app, window, session = app_module.build_app([CSV])
    try:
        assert window._path == pathlib.Path(CSV)
    finally:
        _close(window, session)


def test_no_history_starts_empty(qapp, engine_runner, tmp_path, monkeypatch):
    monkeypatch.setenv("SIMBENCH_SETTINGS_DIR", str(tmp_path))
    _app, window, session = app_module.build_app([])
    try:
        assert window._path is None
        assert not session.project.channels
    finally:
        _close(window, session)
