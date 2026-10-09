"""分层守卫：pymodbus/pyserial 只许出现在 backend/，下层不碰 GUI。

这条线是"引擎/点表层可测、可换后端"的唯一保障，靠人肉 review 一定会漏，所以钉成自动化
用例。扫描用 AST 而不是行前缀匹配：`import pymodbus.simulator as sim`、
`from pymodbus.simulator.simcore import SimCore`、函数体里的延迟 import 都得抓到
（`SimCore`/`SimUtils` 不在 `pymodbus.simulator.__all__` 里，backend 走的正是完整模块路径）。
"""

import ast
import pathlib

import modslave

LIBRARY_ONLY = ("pymodbus", "serial")        # 只许出现在 backend/
GUI_ONLY = ("PySide6", "PyQt5")              # 只许出现在 gui/（M2 才落地）
BACKEND = "backend"

# 由包自己的位置推出根目录：pytest 的 cwd 变了也不影响判定（不依赖 "modslave" 这个相对路径）
ROOT = pathlib.Path(modslave.__file__).resolve().parent


def _top_module(name: str) -> str:
    return (name or "").split(".")[0]


def _imported_modules(path: pathlib.Path) -> list[str]:
    """一个源文件里所有 import 的顶层模块名（含函数体内的延迟 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(_top_module(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # 相对 import（`from . import x` / `from ..model import y`）import 的是本项目
            # 自己的模块，level > 0 直接跳过，否则 `from ..codec import ...` 会被当成外部包
            if not node.level:
                found.append(_top_module(node.module))
    return found


def _python_files(folder: str | None) -> list[pathlib.Path]:
    base = ROOT / folder if folder else ROOT
    return sorted(base.rglob("*.py"))


def _offenders(folder: str | None, needles: tuple[str, ...],
               allow_backend: bool = False) -> list[str]:
    offenders: list[str] = []
    for file in _python_files(folder):
        if not allow_backend and len(file.parts) > len(ROOT.parts) + 1:
            if file.parts[len(ROOT.parts)] == BACKEND:
                continue
        for module in _imported_modules(file):
            if module in needles:
                offenders.append(f"{file}: {module}")
    return offenders


def test_only_backend_touches_pymodbus():
    """`import pymodbus` 与 `from pymodbus.simulator.simcore import SimCore` 都要认。"""
    offenders = _offenders(None, LIBRARY_ONLY)
    assert offenders == [], f"pymodbus/serial leaked outside backend/: {offenders}"


def test_backend_actually_ownes_the_library_imports():
    """守卫不许是空跑：backend/ 里必须真的有人在 import pymodbus。

    只判"违规者为 0"的话，把 backend 的 pymodbus import 全删掉这条照样绿——而那样本文件
    就再也没守任何东西。反过来钉一句"该有的人必须有"，删依赖或改目录都会在这里变红。
    """
    importers = {file.name for file in _python_files(BACKEND)
                 if "pymodbus" in _imported_modules(file)}
    assert {"simdev.py", "raw_writer.py", "modbus_tcp.py"} <= importers


def test_project_and_codec_do_not_touch_gui():
    offenders = _offenders("project", GUI_ONLY) + _offenders("codec", GUI_ONLY)
    assert offenders == [], f"GUI leaked into project/ or codec/: {offenders}"


def test_backend_does_not_touch_gui():
    offenders = _offenders(BACKEND, GUI_ONLY)
    assert offenders == [], f"GUI leaked into backend/: {offenders}"


# ---- 扫描器自身的判据：不许"看起来能抓其实抓不到" ----

def test_scanner_recognises_every_pymodbus_spelling(tmp_path):
    """把守卫打在假样本上：四种写法都必须认出来，相对 import 不许误报。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import pymodbus\n"
        "import pymodbus.simulator as sim\n"
        "from pymodbus import DataType\n"
        "from pymodbus.simulator.simcore import SimCore\n"
        "from pymodbus.simulator import (\n    SimData,\n)\n"
        "from .model import Point\n"
        "from ..codec import point_codec\n",
        encoding="utf-8")
    assert _imported_modules(sample) == [
        "pymodbus", "pymodbus", "pymodbus", "pymodbus", "pymodbus"]


def test_scanner_sees_a_library_import_inside_a_function(tmp_path):
    """延迟 import（`def start(): from pymodbus... import X`）同样是泄漏，必须抓到。"""
    sample = tmp_path / "lazy.py"
    sample.write_text("def start():\n"
                      "    from pymodbus.server import ModbusTcpServer\n"
                      "    return ModbusTcpServer\n", encoding="utf-8")
    assert _imported_modules(sample) == ["pymodbus"]


def test_scanner_does_not_flag_a_local_import(tmp_path):
    """`from . import events` / `from ..project.model import Point` 不算外部依赖。

    相对 import 的 `node.module` 只有 "raw_writer"/"model" 这种片段，按名字判库名会把
    自家模块当成泄漏（`from ..serial_bridge import x` 一类），所以 level > 0 一律跳过。
    """
    sample = tmp_path / "local.py"
    sample.write_text("from . import events\n"
                      "from ..project.model import Point\n", encoding="utf-8")
    assert _imported_modules(sample) == []
