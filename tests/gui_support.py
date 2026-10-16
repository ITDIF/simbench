"""GUI 用例共用的两样东西：哑掉的模态框、主窗依赖的工厂。

为什么单独一个模块、不放 `tests/conftest.py`：conftest 是**所有**用例都会 import 的，而这里要
import `simbench.gui.deps`（连带 PySide6）。内核用例（engine / project / codec）不该因为跑一次
pytest 就被装上 Qt——那是 M1 特意拆开的东西（`tests/test_architecture.py` 守着"PySide6 只许
出现在 gui/"）。
"""

from __future__ import annotations

from simbench.gui.deps import WindowDeps


class SilentIssuesDialog:
    """把主窗那份模态的问题清单换成哑的。

    `IssueDialog.exec()` 在 offscreen 下会开一个**嵌套事件循环**，用例永远回不来——所以凡是造
    主窗的用例都要把它换掉。清单**内容**对不对由 `test_gui_main_window.py` 直接构造真对话框断言
    （不 exec），这里只负责"点一下不会卡住"。
    """

    def __init__(self, issues, parent=None) -> None:
        pass

    def exec(self, *_args) -> int:
        return 0


def gui_deps(**overrides) -> WindowDeps:
    """用例版的主窗依赖：默认不弹模态框；别的接缝（端口探针、两个文件对话框）按需覆盖。

    这就是 2026-10-16 之前的 `monkeypatch.setattr(main_window, "X", ...)` 的替代写法：原来是改
    模块全局、还得让混在别的模块里的方法运行时去现查那个名字；现在是把假件直接交给主窗。
    """
    base: dict = {"issues_dialog": SilentIssuesDialog}
    base.update(overrides)
    return WindowDeps(**base)
