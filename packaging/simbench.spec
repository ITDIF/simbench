# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包定义：一次分析、两个 onedir EXE + 一个 onefile。

口径见 `docs/superpowers/plans/2026-10-13-packaging.md`。三件容易漏的事写在下面：

- **`snap7` 整包收齐**：`snap7/lib/snap7.dll` 是 `python-snap7==1.3` wheel 自带的原生库，
  而 `snap7.common.find_in_package()` 是按 `pathlib.Path(__file__).parent/'lib'/'snap7.dll'`
  找它的——所以它必须**原样**落在包里的 `snap7/lib/` 下（`collect_all` 同时收数据、子模块
  与动态库，三样都缺一不可）。
- **`pkg_resources` 要显式列**：`snap7/__init__.py` 第 4 行会 `import pkg_resources`
  （setuptools<81 才有的模块），PyInstaller 的静态扫描看不见这种"只 import 名字"的用法，
  不列就等运行到 S7 通道时 ImportError。
- **演示点表不进包**：`examples\covi-tunnel.csv` 是给**人**用的，不是程序运行时要读的数据，
  所以由 `build.ps1` 在构建之后拷到 `dist\simbench\` 旁边（放进 `_internal\` 等于藏起来，
  第一次拿到 exe 的人不会去翻一个下划线开头的目录）。
- **`ROOT` 只用来给 `pathex`**：`simbench` 包本身被 PyInstaller 直接分析，不是"数据文件"。

产物（`dist/simbench/`）：
- `simbench.exe`：控制台（`--check` / `--list` / 无头监听 / `net` 子命令）；
- `simbench-gui.exe`：无控制台（双击开界面）；
- 两者共用同一份 `_internal/`（这就是选 onedir 的理由：不每次解压，缺 DLL 时看得见）。
另外 `dist/simbench-onefile.exe` 是**单文件**形态（拷给同事方便），启动慢是已知代价。

三个 exe 都带**图标**与**版本资源**（资源管理器属性页里能看见产品名/版本）：图标由
`make_icon.py` 在构建时按 `icons.app_icon()` 那张画生成（六档尺寸，各自画、不拉伸）。
"""

import pathlib
import sys

from PyInstaller.utils.hooks import collect_all

HERE = pathlib.Path(SPECPATH)
ROOT = HERE.parent

# 图标与版本资源：图标由 `make_icon.py` **在构建时按同一张画法**生成（见那个文件的说明），
# 版本资源是一份静态的 VSVersionInfo（`version_info.txt`）。两者都只对 Windows 有意义，
# 但这份 spec 本来就只服务 Windows（`net/` 那层是 Windows 专用的）。
sys.path.insert(0, str(HERE))
import make_icon                                    # noqa: E402

ICON = str(make_icon.ensure_icon())
VERSION = str(HERE / "version_info.txt")

# `collect_all` 的返回值就是 Analysis 的三个参数：数据 / 二进制 / 隐藏 import。
snap7_datas, snap7_binaries, snap7_hidden = collect_all("snap7")
datas = list(snap7_datas)
binaries = list(snap7_binaries)
hiddenimports = list(snap7_hidden) + ["pkg_resources"]

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # `pys7` 只是测试用的验收主站，`pytest` 与测试代码同样不该进发布包。
    # 三者都不在产品 import 图里，列出来是"万一有人手滑加了 import"的硬闸。
    excludes=["pys7", "pytest", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="simbench",
    icon=ICON,
    version=VERSION,
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="simbench-gui",
    icon=ICON,
    version=VERSION,
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe_cli, exe_gui, a.binaries, a.datas,
    strip=False,
    upx=False,
    name="simbench",
)

# 单文件形态：同一个 PYZ + 同一份二进制/数据全塞进一个 exe（启动时解压到临时目录）。
# 它**不是**发布形态（每次启动都要解压几百 MB 的 Qt），是"拷一个文件给同事"的方便形态，
# 所以名字里直接写出来，免得现场把它当成主产物。
exe_onefile = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="simbench-onefile",
    icon=ICON,
    version=VERSION,
    debug=False,
    strip=False,
    upx=False,
    console=True,
)