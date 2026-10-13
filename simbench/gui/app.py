"""GUI 入口：把 CLI 已有的装载路径接到窗口上。

工程装载复用 `csv_io_entry.load_point_table`（和 CLI 同一个口），错误清单由
`MainWindow.use_project` 走一次 `IssueDialog`——有 error 也照样把表装进界面，因为
"带着错点表起来"由 `start_channel` 那道闸门拦，而"改完再启动"需要看得见那张表。

这里刻意**不自动启动任何通道**：命令行给的是点表而不是授权，界面开起来之后
`●` 一个都不该亮。

网络那一层（`net/startup.py`）反过来是"该自动就自动"：装载点表之后按旁边那份声明把
网卡对一遍，结论一行进状态栏、几行进 stdout。它只报话，不改退出码，也不会拦着开窗。

GC 归 GUI 线程（这里关掉自动回收，破环交给 `MainWindow._maybe_collect`）：见 spec §13
那条跨线程 `~QWidget` 死锁。无头 CLI 不碰 Qt，不受影响。
"""

from __future__ import annotations

import gc
import sys

from PySide6 import QtWidgets

from ..csv_io_entry import load_point_table
from ..engine.packets import PacketStore
from ..engine.session import Session
from ..net import startup as net_startup
from ..project.validate import error_lines
from .main_window import MainWindow


def _take_gc_into_gui_thread() -> None:
    """关掉自动回收，把破环这件事交给主线程的心跳；导入期建好的对象先冻结出去。"""
    gc.collect()
    gc.freeze()
    gc.disable()


def _prepare_network(window, point_table: str) -> None:
    """起界面前按声明把网络对一遍：一行进状态栏，全部进 stdout。

    走状态栏是因为 GUI 常常没有控制台（pythonw 起的话 stdout 是 None），而"弹了 UAC
    是在干什么"必须有地方看见。失败也只落在这一行上，不弹框、不拦开窗。
    """
    # 把工程一起交下去：自动配网要按**通道类型**过滤（纯串口工程不配设备 IP，见
    # `net/startup._tcp_endpoints`），不传的话那条判据只能看见声明文件。
    lines = net_startup.prepare(point_table, project=window.session.project)
    for line in lines:
        print(f"[net] {line}", flush=True)
    if lines:
        window.status.showMessage(lines[0])


def build_app(argv: list[str] | None = None):
    """建 QApplication + Session + 窗口；给了点表路径就装载它。返回 `(app, window, session)`。

    runner 交给 Session 自己建（而不是 `Session(runner=EngineRunner())`）：归属口径是
    "谁建的 runner 谁收"，显式传进去的话 `shutdown()` 不会停它，关窗之后留两条僵尸线程。
    """
    _take_gc_into_gui_thread()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or [])
    store = PacketStore()
    session = Session(packet_store=store)
    window = MainWindow(session, store)
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        project, notes, issues = load_point_table(args[0])
        window.use_project(project, args[0], notes)
        if error_lines(issues):
            window.status.showMessage(f"{len(error_lines(issues))} 个错误，启动已被阻止")
        _prepare_network(window, args[0])
    window.show()
    return app, window, session
