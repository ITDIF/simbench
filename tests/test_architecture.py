"""分层守卫：pymodbus/pyserial 只许出现在 backend/，下层不碰 GUI。

这条线是"引擎/点表层可测、可换后端"的唯一保障，靠人肉 review 一定会漏，所以钉成自动化
用例。扫描用 AST 而不是行前缀匹配：`import pymodbus.simulator as sim`、
`from pymodbus.simulator.simcore import SimCore`、函数体里的延迟 import 都得抓到
（`SimCore`/`SimUtils` 不在 `pymodbus.simulator.__all__` 里，backend 走的正是完整模块路径）。

豁免口径按**顶层包名**判，不按目录层级猜（`len(file.parts)` 那种写法实测恒过：扫 backend
时它给每一个文件都判成"这是 backend，跳过"，于是违规者永远是空表）。所以豁免必须显式
传名字，且每条守卫都配一条"扫描器确实检查过这些文件"的证据。
"""

import ast
import pathlib

import simbench

# 只许出现在 backend/：pymodbus 与 pyserial 是 Modbus 两条路的原生依赖，snap7 是 S7 的。
# snap7 进这张表是 S7 里程碑 Task 8 的收尾项——它本来只在 backend/s7_server.py 里 import，
# 但不写进守卫就等于"靠人记得住"，而下一个想在 gui 里直接摸 DLL 的人不会被拦。
LIBRARY_ONLY = ("pymodbus", "serial", "snap7")
GUI_ONLY = ("PySide6", "PyQt5")              # 只许出现在 gui/（M2 才落地）
BACKEND = "backend"
GUI_PACKAGE = "gui"
SYSTEM_NET = "net"                        # 唯一允许碰系统网络（netsh / PowerShell）的包

# 由包自己的位置推出根目录：pytest 的 cwd 变了也不影响判定（不依赖 "simbench" 这个相对路径）
ROOT = pathlib.Path(simbench.__file__).resolve().parent


def _top_module(name: str) -> str:
    return (name or "").split(".")[0]


def _imported_modules(path: pathlib.Path) -> list[str]:
    """一个源文件里所有 import 的顶层模块名（含函数体内的延迟 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(_top_module(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            # 相对 import（`from . import x` / `from ..model import y`）import 的是本项目
            # 自己的模块，level > 0 直接跳过，否则 `from ..codec import ...` 会被当成外部包
            found.append(_top_module(node.module))
    return found


def _python_files(folder: str | None, root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    base = root / folder if folder else root
    return sorted(base.rglob("*.py"))


def _top_package(path: pathlib.Path, root: pathlib.Path) -> str | None:
    """文件相对包根的第一层目录名；根下的裸文件（`__main__.py`）没有这一层，回 None。

    豁免只认这个名字。以前按 `len(parts)` 判层级，等于"路径里第 N 段叫 backend 就跳过"，
    而扫 backend 时每个文件的第 N 段都正好叫 backend —— 全部被跳过，断言变成 `[] == []`。
    """
    parts = path.relative_to(root).parts
    return parts[0] if len(parts) > 1 else None


def _scanned_files(folder: str | None, root: pathlib.Path = ROOT,
                   skip_packages: tuple[str, ...] = ()) -> list[pathlib.Path]:
    """这条守卫**真的**会去读哪些文件：豁免按顶层包名判，其余一个不漏。

    单独暴露出来，是为了让每条守卫能拿出"扫过 > 0 个文件"的证据（防空跑）。
    """
    return [file for file in _python_files(folder, root)
            if _top_package(file, root) not in skip_packages]


def _offenders(folder: str | None, needles: tuple[str, ...],
               root: pathlib.Path = ROOT,
               skip_packages: tuple[str, ...] = ()) -> list[str]:
    """folder 下 import 了 needles 里任何东西的文件；skip_packages 是显式豁免的顶层包。

    默认不豁免：只有"库只许出现在 backend"那条需要把 backend 整个放行；GUI 的判定
    对 backend 同样成立（spec §4），传错参数就会让守卫变成空跑。
    """
    offenders: list[str] = []
    for file in _scanned_files(folder, root, skip_packages):
        for module in _imported_modules(file):
            if module in needles:
                offenders.append(f"{file}: {module}")
    return offenders


def _fake_root(tmp_path, files: dict[str, str]) -> pathlib.Path:
    """在仓库外造一棵形状相同的假包树，用来给守卫打植入样本（不往仓库里塞脏文件）。"""
    root = tmp_path / "pkg"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ---- 真仓库上的判定 ----

def test_only_backend_touches_pymodbus():
    """`import pymodbus` 与 `from pymodbus.simulator.simcore import SimCore` 都要认。

    豁免是显式传给 `skip_packages` 的，只放行 backend 这一个顶层包：project/codec/gui 和
    根下的裸文件照样在扫，所以"改松"会立刻在下面的植入样本用例里变红。
    """
    offenders = _offenders(None, LIBRARY_ONLY, skip_packages=(BACKEND,))
    assert offenders == [], f"pymodbus/serial leaked outside backend/: {offenders}"


def test_backend_actually_ownes_the_library_imports():
    """守卫不许是空跑：backend/ 里必须真的有人在 import pymodbus。

    只判"违规者为 0"的话，把 backend 的 pymodbus import 全删掉这条照样绿——而那样本文件
    就再也没守任何东西。反过来钉一句"该有的人必须有"，删依赖或改目录都会在这里变红。
    """
    importers = {file.name for file in _python_files(BACKEND)
                 if "pymodbus" in _imported_modules(file)}
    # `modbus_rtu.py` 也 import pymodbus（RTU 从站），一并列进"该有的人"里：
    # 少了它，这一条只证明"TCP 那半边还在"，串口那半边的依赖被删掉也不会红。
    assert {"simdev.py", "raw_writer.py", "modbus_tcp.py", "modbus_rtu.py"} <= importers


def test_the_s7_library_also_only_lives_in_backend():
    """同一把尺子量 snap7：`snap7` 只许出现在 `backend/`（S7 后端是唯一 import 它的地方），
    且 `backend/` 里**真有人** import 它（守卫不许是空跑——删掉依赖或改目录都要在这里红）。"""
    offenders = _offenders(None, ("snap7",), skip_packages=(BACKEND,))
    assert offenders == [], offenders
    scanned = _scanned_files(BACKEND)
    assert scanned, "backend/ 一个文件都没扫到，这条守卫是空跑"
    assert "s7_server.py" in {file.name for file in scanned
                              if "snap7" in _imported_modules(file)}


def test_project_and_codec_do_not_touch_gui():
    scanned = _scanned_files("project") + _scanned_files("codec")
    assert scanned, "project/ 与 codec/ 一个文件都没扫到，这条守卫是空跑"
    offenders = _offenders("project", GUI_ONLY) + _offenders("codec", GUI_ONLY)
    assert offenders == [], f"GUI leaked into project/ or codec/: {offenders}"


def test_backend_does_not_touch_gui():
    """spec §4：backend 不 import Qt。M2 一上来就可能让 backend 直接摸 PySide6。"""
    scanned = _scanned_files(BACKEND)
    assert scanned, "backend/ 一个文件都没扫到，这条守卫是空跑"
    offenders = _offenders(BACKEND, GUI_ONLY)
    assert offenders == [], f"GUI leaked into backend/: {offenders}"


def test_only_gui_touches_pyside6():
    """PySide6 只许出现在 simbench/gui/。

    这条是 M2 新增的线：引擎/点表/后端一旦摸 Qt，pytest 就必须起事件循环，
    内核测试立刻和界面绑死——那是 M1 特意拆开的东西。
    """
    scanned = _scanned_files(None, skip_packages=(GUI_PACKAGE,))
    assert scanned, "除了 gui/ 一个文件都没扫到，这条 Qt 守卫是空跑"
    offenders = _offenders(None, GUI_ONLY, skip_packages=(GUI_PACKAGE,))
    assert offenders == [], f"Qt leaked outside gui/: {offenders}"


def test_gui_actually_exists_and_is_the_only_qt_owner():
    """防空跑：gui/ 里必须真有人 import Qt，否则上一条是恒真断言。

    和 `test_backend_actually_ownes_the_library_imports` 同一个理由：只判"违规者为 0"，
    那么把 gui/ 的 Qt import 全删掉（或目录名拼错导致没人被豁免）这条照样绿。
    """
    files = _python_files(GUI_PACKAGE)
    assert files, "simbench/gui/ 还不存在——M2 没开始，别留一条空守卫"
    importers = {f.name for f in files if "PySide6" in _imported_modules(f)}
    assert importers, "gui/ 里没有任何 PySide6 import，守卫失去意义"


def test_project_codec_engine_backend_do_not_touch_gui():
    """逐层点名：engine/backend/project/codec 四个目录都不许出现 Qt。

    整仓扫描那条已经覆盖这里，但按目录再钉一遍是为了失败信息能直接指到人：
    `engine/` 是 M2 新加的、最容易"顺手 import 一下 QtCore 拿 QTimer"的一层。
    """
    for folder in ("engine", BACKEND, "project", "codec"):
        scanned = _scanned_files(folder)
        assert scanned, f"{folder}/ 一个文件都没扫到，这条守卫是空跑"
        offenders = _offenders(folder, GUI_ONLY)
        assert offenders == [], f"Qt leaked into {folder}/: {offenders}"


# ---- 扫描器自身的判据：不许"看起来能抓其实抓不到" ----

def test_guard_flags_a_planted_gui_import_in_backend(tmp_path):
    """把守卫打在植入样本上：backend 里 import PySide6 必须报，不许"扫了但跳过"。

    这条是 `test_backend_does_not_touch_gui` 的防空跑证据——上一条只能证明"现在没人
    违规"，证明不了"有人违规时报得出"。真实变异（在仓库外的整仓副本里给 backend 加一个
    import PySide6 的文件）也跑过：修之前全量全绿，修之后架构用例变红。
    """
    root = _fake_root(tmp_path, {
        "backend/modbus_tcp.py": "import PySide6\n",
        "backend/simdev.py": "from pymodbus.simulator import SimData\n",
    })
    assert _scanned_files(BACKEND, root), "backend 下本该扫 2 个文件"
    assert _offenders(BACKEND, GUI_ONLY, root) == [
        f"{root / 'backend' / 'modbus_tcp.py'}: PySide6"]


def test_guard_flags_a_planted_gui_import_in_project_and_codec(tmp_path):
    """同一条判据的 project/codec 版本：植入样本必须报，扫描清单不许是空的。"""
    root = _fake_root(tmp_path, {
        "project/model.py": "from PyQt5.QtCore import Qt\n",
        "codec/point_codec.py": "import PySide6\n",
    })
    assert _scanned_files("project", root) and _scanned_files("codec", root)
    offenders = _offenders("project", GUI_ONLY, root) + \
        _offenders("codec", GUI_ONLY, root)
    assert len(offenders) == 2, offenders


def test_guard_flags_a_planted_gui_import_outside_gui(tmp_path):
    """`test_only_gui_touches_pyside6` 的植入样本：gui/ 之外 import Qt 必须报。

    真实仓库现在没人违规，所以那条守卫只能证明"此刻是干净的"；这条证明"脏了会红"，
    顺带钉住豁免口径只认 `gui` 这一个顶层包名（写成 `GUI_PACKAGE="GUI"` 之类的拼错，
    这里会立刻变成"gui/ 里的 Qt 也被报出来"）。
    """
    root = _fake_root(tmp_path, {
        "gui/bridge.py": "from PySide6 import QtCore\n",
        "engine/session.py": "import PySide6\n",
        "project/model.py": "from PyQt5.QtCore import Qt\n",
    })
    scanned = _scanned_files(None, root, skip_packages=(GUI_PACKAGE,))
    assert [f.name for f in scanned] == ["session.py", "model.py"], scanned
    offenders = _offenders(None, GUI_ONLY, root, skip_packages=(GUI_PACKAGE,))
    leaked = {pathlib.Path(o.rsplit(": ", 1)[0]).relative_to(root).as_posix()
              for o in offenders}
    assert leaked == {"engine/session.py", "project/model.py"}, offenders


def test_library_import_outside_backend_is_still_a_leak(tmp_path):
    """pymodbus 的豁免只到 backend 为止：同样的 import 挪到 project/ 就是违规。

    钉住"不能改松"——如果 skip 口径写成"跳过所有子目录"或"按层级猜"，这条会一起变绿。
    """
    root = _fake_root(tmp_path, {
        "backend/simdev.py": "import pymodbus\n",
        "project/model.py": "import pymodbus\n",
        "gui/main.py": "from pymodbus.server import ModbusTcpServer\n",
        "__main__.py": "import serial\n",
    })
    offenders = _offenders(None, LIBRARY_ONLY, root, skip_packages=(BACKEND,))
    leaked = {pathlib.Path(o.rsplit(": ", 1)[0]).relative_to(root).as_posix()
              for o in offenders}
    assert leaked == {"project/model.py", "gui/main.py", "__main__.py"}, offenders


def test_skip_is_by_package_name_not_by_depth(tmp_path):
    """豁免认名字不认深度：backend 的子目录一起豁免，别的顶层包一律不豁免。"""
    root = _fake_root(tmp_path, {
        "backend/sub/deep.py": "from PySide6 import QtCore\n",
        "backend/top.py": "from PySide6 import QtCore\n",
        "project/deep/deeper.py": "from PySide6 import QtCore\n",
    })
    assert [f.name for f in _scanned_files(BACKEND, root, skip_packages=(BACKEND,))] == []
    assert _offenders(BACKEND, GUI_ONLY, root, skip_packages=(BACKEND,)) == []
    assert len(_offenders(BACKEND, GUI_ONLY, root)) == 2
    assert len(_offenders(None, GUI_ONLY, root, skip_packages=(BACKEND,))) == 1


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


# ---- 系统网络：只有 net/ 能碰 ----

def test_only_net_touches_system_network():
    """`subprocess` 只许出现在 `net/`。

    加/删网卡地址要跑 netsh 或 PowerShell，那是**唯一**一个会改变现场机器网络状态的动作。
    把它圈在一个包里，才能保证"校验流程不可能顺手改系统"这句话是结构上成立的，
    而不是靠每个调用点自觉。判据用 import 而不是搜字符串：`netsh` 写进文档、
    写进帮助文本都不算违规，真的去调用才算。
    """
    offenders = _offenders(None, ("subprocess",), skip_packages=(SYSTEM_NET,))
    assert offenders == [], offenders
    assert _scanned_files(None, skip_packages=(SYSTEM_NET,))       # 扫描清单不许是空的


def test_planted_subprocess_outside_net_is_flagged(tmp_path):
    """植入样本：同样的 import 放在 gui/ 或 engine/ 必须报出来（守卫不是恒真）。"""
    root = _fake_root(tmp_path, {
        "net/apply.py": "import subprocess\n",
        "engine/session.py": "import subprocess\n",
        "gui/main_window.py": "from subprocess import run\n",
    })
    scanned = _scanned_files(None, root, skip_packages=(SYSTEM_NET,))
    assert [f.name for f in scanned] == ["session.py", "main_window.py"], scanned
    offenders = _offenders(None, ("subprocess",), root, skip_packages=(SYSTEM_NET,))
    leaked = {pathlib.Path(o.rsplit(": ", 1)[0]).relative_to(root).as_posix()
              for o in offenders}
    assert leaked == {"engine/session.py", "gui/main_window.py"}, offenders