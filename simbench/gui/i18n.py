"""界面语言：把 Qt 自带的中文翻译装上，让**标准按钮**也说中文。

为什么需要（2026-10-17 现场反馈）：`OK` / `Cancel` / `Yes` / `No` / `Save` / `Discard` 这些
按钮的文字**不来自我们的代码**，而来自 Qt 翻译目录里的 `qtbase_<语言>.qm`。不装翻译器时
Qt 一律回英文默认值——界面上其余每个字都是我们自己写的中文，只有弹窗上这几个按钮是英文，
看着像没做完。实测（offscreen，本机）：

    装之前：&Yes / &No、OK / Cancel、Save / Discard / Cancel
    装之后：是(&Y) / 否(&N)、确定 / 取消、保存 / 放弃 / 取消

为什么固定 `zh_CN` 而不是跟随 `QLocale.system()`：这个工具的界面**整体是中文**（所有文案都
直接写在源码里）。在英文系统的机器上跟随系统，只会得到"中文界面 + 英文按钮"——同一个毛病
换个方向。真要做多语言是另一件事（每个字符串都得过 `tr()`）。

翻译器必须**留着引用**：Qt 不持有所有权，`QTranslator` 一旦被回收，翻译立刻失效（而且是
悄悄失效——按钮变回英文）。所以把它挂到 app 上，再用模块级变量挡住重复安装。

它盖不到的最后一处是 **Windows 原生文件对话框**（按钮文字跟操作系统语言走，不是 Qt 画的）：
那些调用点在 gui/dialogs.py 里统一加了 DontUseNativeDialog，让文件对话框走 Qt 控件版，
这一份翻译器与主题才够得着。
"""

from __future__ import annotations

import pathlib

import PySide6
from PySide6 import QtCore, QtWidgets

#: 管标准按钮与内建对话框的那一份（`qtbase_zh_CN.qm`）。
_LANGUAGE = "zh_CN"
_SOURCE = "qtbase"

_installed: QtCore.QTranslator | None = None


def _folders() -> list[str]:
    """翻译文件可能在哪：Qt 自己的翻译目录，外加 PySide6 包旁边那一份。

    为什么两个都试：打包成 exe 之后 `QLibraryInfo.TranslationsPath` 会不会被指到包里那份，
    取决于 PyInstaller 那套 runtime hook。实测现成产物里 `_internal/PySide6/translations/
    qtbase_zh_CN.qm` **是在的**，那就再留一条"直接按包的位置找"的路——少一层依赖，
    冻结包里也不会因为钩子换了行为就静默退回英文。
    """
    folders = [QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)]
    folders.append(str(pathlib.Path(PySide6.__file__).resolve().parent / "translations"))
    return [folder for folder in folders if folder]


def install_chinese_ui(app: QtWidgets.QApplication | None = None) -> bool:
    """把中文翻译装到 app 上。

    装上了回 `True`；**找不到翻译文件**（裁剪过的 Qt、被人删了 translations 目录）回
    `False`，不抛：按钮退回英文仍然是个能用的界面，比因为一个文案崩掉强。

    调用点是主窗构造期（与 `theme.apply_theme()` 同一处）——那里是"进程级外观只落一次"的
    既有位置，纯命令行那几条路根本不会造窗口，也就没有标准按钮这回事。
    """
    global _installed
    if _installed is not None:
        return True                      # 幂等：装过就直接回
    if app is None:
        app = QtWidgets.QApplication.instance()
    if app is None:
        return False                     # 没有 QApplication：谈不上"标准按钮"
    translator = QtCore.QTranslator(app)   # 挂 app 上：Qt 不持有所有权，回收即失效
    for folder in _folders():
        if translator.load(f"{_SOURCE}_{_LANGUAGE}", folder):
            app.installTranslator(translator)
            _installed = translator
            return True
    return False
