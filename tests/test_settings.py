"""设置文件（`simbench/settings.py`）：主题、窗口几何、最近打开、上次的目录。

三条口径各有用例钉着：坏文件不许拦启动、写不动不许炸、最近列表只列**还在**的文件。
界面上那一侧的接线（恢复几何、菜单里列出来、点一下能打开）在 `test_gui_settings.py`。
"""

from __future__ import annotations

import json
import pathlib

from simbench import settings as settings_module
from simbench.settings import MAX_RECENT, Settings


def test_the_settings_path_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(settings_module.SETTINGS_DIR_ENV, str(tmp_path / "cfg"))
    assert settings_module.settings_path() == tmp_path / "cfg" / "settings.json"


def test_the_settings_path_defaults_under_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv(settings_module.SETTINGS_DIR_ENV, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert settings_module.settings_path() == tmp_path / "Roaming" / "simbench" / "settings.json"


def test_a_missing_file_is_just_the_defaults(tmp_path):
    loaded = Settings.load(tmp_path / "nope.json")
    assert loaded.theme == "" and loaded.recent == [] and loaded.geometry == ""


def test_a_round_trip_keeps_every_field(tmp_path):
    target = tmp_path / "settings.json"
    saved = Settings(theme="dark", geometry="0a0b", window_state="0c0d",
                     last_dir=r"D:\点表", recent=["a.csv", "b.xlsx"],
                     recent_offsets={"a.csv": -40001})
    assert saved.save(target)
    back = Settings.load(target)
    assert (back.theme, back.geometry, back.window_state, back.last_dir) == \
        ("dark", "0a0b", "0c0d", r"D:\点表")
    assert back.recent == ["a.csv", "b.xlsx"]
    assert back.recent_offsets == {"a.csv": -40001}


def test_a_corrupt_file_is_kept_aside_and_startup_continues(tmp_path):
    """半截 JSON / 被别的东西写坏：当空的用，但把坏的那份留档（否则永远查不出为什么）。"""
    target = tmp_path / "settings.json"
    target.write_text("{not json at all", encoding="utf-8")
    loaded = Settings.load(target)
    assert loaded.theme == "" and loaded.recent == []
    assert not target.exists() and (tmp_path / "settings.json.bad").exists()


def test_a_write_failure_never_raises(tmp_path):
    """写不动就静默（设置丢了是小事，"因为存不了设置而打不开软件"是事故）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert Settings().save(blocker / "sub" / "settings.json") is False


def test_saving_is_atomic(tmp_path):
    target = tmp_path / "settings.json"
    Settings(recent=["a"]).save(target)
    Settings(recent=["b"]).save(target)
    assert json.loads(target.read_text(encoding="utf-8"))["recent"] == ["b"]
    assert list(tmp_path.glob("*.tmp")) == []


def test_recent_is_deduped_newest_first(tmp_path):
    """第二次打开同一个文件要把它**提到最前**，不是留两份。"""
    saved = Settings()
    for name in ["a", "b", "a", "c"]:
        saved.add_recent(tmp_path / name)
    assert [pathlib.Path(p).name for p in saved.recent] == ["c", "a", "b"]


def test_recent_is_capped_and_offsets_are_pruned_with_it(tmp_path):
    """历史只留最近 MAX_RECENT 条；被挤出去的路径，它的偏移也一起删。"""
    saved = Settings()
    for index in range(MAX_RECENT + 3):
        saved.add_recent(tmp_path / f"{index}.csv", offset=index)
    assert len(saved.recent) == MAX_RECENT
    assert len(saved.recent_offsets) == MAX_RECENT
    assert saved.recent[0] == str(tmp_path / f"{MAX_RECENT + 2}.csv"), \
        "最新那个不在最前面"
    assert set(saved.recent_offsets) == set(saved.recent)


def test_the_import_offset_is_remembered_for_quick_reopen(tmp_path):
    """40001 风格的点表：从历史里再打开时要按导入时那个偏移读。"""
    table = tmp_path / "points.csv"
    saved = Settings()
    saved.add_recent(table, offset=-40001)
    assert saved.recent_offset(table) == -40001
    assert saved.recent_offset(tmp_path / "other.csv") == 0


def test_clear_recent_drops_paths_and_offsets(tmp_path):
    saved = Settings(recent=["a.csv"], recent_offsets={"a.csv": 5})
    saved.clear_recent()
    assert saved.recent == [] and saved.recent_offsets == {}


def test_recent_ignores_empty_paths():
    saved = Settings(recent=["a"])
    saved.add_recent(None)
    saved.add_recent("")
    assert saved.recent == ["a"]


def test_only_recent_files_that_still_exist_are_offered(tmp_path):
    """已经删掉的条目不许留在菜单里：点一下弹"文件不存在"比不显示更糟。"""
    alive = tmp_path / "alive.csv"
    alive.write_text("x", encoding="utf-8")
    saved = Settings(recent=[str(alive), str(tmp_path / "gone.csv")])
    assert saved.existing_recent() == [str(alive)]


def test_remember_dir_keeps_the_folder_not_the_file(tmp_path):
    saved = Settings()
    saved.remember_dir(tmp_path / "点表.csv")
    assert saved.last_dir == str(tmp_path)
