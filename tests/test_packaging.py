"""打包素材：图标容器、版本资源、spec 的接线（不需要真打包就能验的那部分）。

真打包的验收在 `packaging/smoke.py`（拿真主站打产物）与 `packaging/SMOKE.md` 的记录里；
这一份只守"素材本身对不对"——那几样东西出问题时，现象是"exe 图标是默认的白纸"或"属性页里
什么都没有"，而它们离代码很远，没人会回头查。
"""

from __future__ import annotations

import importlib.util
import pathlib
import struct

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def _load_make_icon():
    """按**路径**加载 `packaging/make_icon.py`。

    不能用 `import packaging.make_icon`：`packaging` 这个名字已经被 PyPI 上那个打包库占了
    （pytest 自己就依赖它），那样导入的是别人的模块。
    """
    spec = importlib.util.spec_from_file_location("simbench_make_icon",
                                                 PACKAGING / "make_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ico_bytes_produces_the_container_windows_expects():
    """ICO 容器：6 字节头 + 每条 16 字节的目录项 + 各张图；256 那一档的边长写 0（约定）。"""
    fake = {16: b"\x89PNG-one", 256: b"\x89PNG-two-longer"}
    data = _load_make_icon().ico_bytes(fake)
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)
    first = struct.unpack("<BBBBHHII", data[6:22])
    assert first[0] == 16 and first[1] == 16 and first[6] == len(fake[16])
    second = struct.unpack("<BBBBHHII", data[22:38])
    assert second[0] == 0 and second[1] == 0, "256 的边长在目录项里必须是 0"
    payload_start = 6 + 16 * 2
    assert data[first[7]:first[7] + first[6]] == fake[16]
    assert data[second[7]:second[7] + second[6]] == fake[256]
    assert first[7] == payload_start


def test_the_generated_icon_is_a_real_icon_qt_can_load(qapp):
    """生成的 `.ico` 要能被 QIcon 读出来（读不出来的话 exe 图标会是白纸，而且没有任何报错）。"""
    from PySide6 import QtGui
    path = PACKAGING / "simbench.ico"
    if not path.exists():
        _load_make_icon().ensure_icon(path)          # 构建产物不在（干净仓库）就生成一份
    for size in (16, 32, 48, 256):
        pixmap = QtGui.QIcon(str(path)).pixmap(size, size)
        assert not pixmap.isNull() and pixmap.width() == size, size


def test_the_version_resource_is_what_pyinstaller_reads():
    """版本资源是 PyInstaller 的 VSVersionInfo DSL：名字、版本、语言代码页都写上。"""
    text = (PACKAGING / "version_info.txt").read_text(encoding="utf-8")
    for needle in ("VSVersionInfo(", "FixedFileInfo(", "StringFileInfo(",
                   "VarFileInfo(", "'080404b0'", "ProductName", "simbench",
                   "filevers=(1, 0, 0, 0)"):
        assert needle in text, needle


def test_the_spec_wires_the_icon_and_the_version_into_every_exe():
    """spec 里三个 EXE 都要挂图标与版本资源：漏掉一个就会出现"一个 exe 有图标、另一个没有"。"""
    text = (PACKAGING / "simbench.spec").read_text(encoding="utf-8")
    assert "ensure_icon()" in text, "spec 没有在构建时保证图标存在"
    assert text.count("icon=ICON") == 3, text.count("icon=ICON")
    assert text.count("version=VERSION") == 3
    assert "packaging/*.ico" in (ROOT / ".gitignore").read_text(encoding="utf-8"), \
        "图标是产物，不该进仓库"
