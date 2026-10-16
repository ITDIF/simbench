"""主窗与外界打交道的那几样：会弹框的对话框、会探机器的端口探针。

为什么要有这一层（2026-10-16）：这些东西原来是 `main_window` 上的**模块级名字**，用例靠
`monkeypatch.setattr(main_window, "save_project_as", ...)` 换掉它们。两处代价：

1. 混在别的模块里的方法（`window_edit` / `window_io`）必须**运行时**去 `main_window` 上现查
   那个名字（原先那两个 `_mw()` / `_probe()` 辅助函数），否则补丁不生效——一个纯粹为迁就
   测试写法而存在的间接层，代价是"这个方法到底依赖什么"在源码里读不出来；
2. 补丁改的是模块全局：同一进程里两条用例的先后、以及"谁还没还原"都会互相影响。

收成一份显式依赖之后，两种写法都落在**对象**上：构造时注入
（`MainWindow(session, store, deps=WindowDeps(issues_dialog=Recorder))`），或拿到窗口后换某一样
（`monkeypatch.setattr(window.deps, "save_project_as", fake)`）。

默认值就是真件：这份清单只写"主窗与外界的那几个接缝"，不写它自己的内部件（模型、面板那些
由主窗自己造）。`probe` 的默认是 `ports.is_free`——它是唯一一个"默认值也必须能换"的：
不换的话，"新建一个 TCP 工程得到 tcp-5020"这类断言就取决于跑测试的机器上 5020 空不空。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..engine import ports
from . import dialogs
from .point_props import PointPropertiesDialog


@dataclass
class WindowDeps:
    """主窗的"外部件"。字段名与调用处读起来一致（`self.deps.save_project_as(...)`）。"""

    #: "这个端口现在空着吗"。默认走真实探针；用例换成 `lambda _port: True`。
    probe: Callable[[int], bool] = ports.is_free
    #: 问题清单那个模态框（`dialogs.IssueDialog(issues, parent)`）。
    issues_dialog: type = dialogs.IssueDialog
    #: 点位属性面板（`PointPropertiesDialog(parent, project, key)`）。
    point_props_dialog: type = PointPropertiesDialog
    #: 下面四个都是"问一个路径 / 问一个偏移"的文件对话框，返回值见各自的文档串。
    import_table: Callable[..., str | None] = dialogs.import_table
    choose_offset: Callable[..., int] = dialogs.choose_offset
    open_project: Callable[..., object] = dialogs.open_project
    save_project_as: Callable[..., object] = dialogs.save_project_as
