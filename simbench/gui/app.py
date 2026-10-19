"""GUI 入口：把 CLI 已有的装载路径接到窗口上。

工程装载复用 `csv_io_entry.load_point_table`（和 CLI 同一个口），错误清单由
`MainWindow.use_project` 走一次 `IssueDialog`——有 error 也照样把表装进界面，因为
"带着错点表起来"由 `start_channel` 那道闸门拦，而"改完再启动"需要看得见那张表。

这里刻意**不自动启动任何通道**：命令行给的是点表而不是授权，界面开起来之后
`●` 一个都不该亮。

网络那一层（`net/startup.py`）反过来是"该自动就自动"：装载点表之后按旁边那份声明把
网卡对一遍，结论一行进状态栏、几行进 stdout。它只报话，不改退出码，也不会拦着开窗。

**证据落点**在开窗之前装好（`simbench/diagnostics.py`，2026-10-14 深度审查）：界面版 exe
是无控制台构建，`sys.stdout/stderr` 是 `None`，任何未捕获异常本来**没有落点**——现场只能
看到"窗口一闪就没了"。装完后崩溃日志、Qt 警告、日志页的每一行都进
`%LOCALAPPDATA%\\simbench\\logs\\simbench-<日期>.log`（可用 `SIMBENCH_LOG_DIR` 改）。
装不上不拦开窗（日志是证据，不是功能），只是窗口里会说一句为什么。

GC 归 GUI 线程（这里关掉自动回收，破环交给 `MainWindow._maybe_collect`）：见 spec §13
那条跨线程 `~QWidget` 死锁。无头 CLI 不碰 Qt，不受影响。
"""

from __future__ import annotations

import gc
import sys

from PySide6 import QtWidgets

from .. import diagnostics
from .. import settings as settings_module
from ..__about__ import __version__
from ..csv_io_entry import load_point_table
from ..engine.packets import PacketStore
from ..engine.session import Session
from ..net import startup as net_startup
from ..project.validate import error_lines
from . import theme
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
        # 无控制台的 exe（`pythonw` / `simbench-gui.exe`）里 stdout 是 None，`print` 谁也看不见；
        # 而这几行常常是"该去跑哪条命令"的唯一线索（例如通道 host 写成具体 IP、又没有声明时），
        # 所以同时落到日志页——那里是窗口版现场唯一能翻的地方。
        window._log(f"[net] {line}")
    if lines:
        window.status.showMessage(lines[0])


def build_app(argv: list[str] | None = None):
    """建 QApplication + Session + 窗口；给了点表路径就装载它。返回 `(app, window, session)`。

    runner 交给 Session 自己建（而不是 `Session(runner=EngineRunner())`）：归属口径是
    "谁建的 runner 谁收"，显式传进去的话 `shutdown()` 不会停它，关窗之后留两条僵尸线程。
    """
    _take_gc_into_gui_thread()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or [])
    # 上次那份设置：主题、窗口几何、最近打开、上次用过的目录（`simbench/settings.py`）
    saved = settings_module.Settings.load()
    if saved.theme in ("light", "dark"):
        # 起手就按上次的主题落一次（默认是浅色；用户切过之后不该每次重来）
        theme.apply_theme(app, mode=saved.theme)
    store = PacketStore()
    session = Session(packet_store=store)
    # 先装证据落点，再建窗：窗口构造期炸的那一类也要能落盘
    log_file = diagnostics.install_crash_handlers(version=__version__)
    window = MainWindow(session, store, log_file=log_file, settings=saved)
    if log_file is None:
        window._log("[warning] 这次的日志没有落盘（见启动时的提示）：崩溃与日志只在窗口里")
    else:
        window._log(f"[info] 本次会话的日志写在 {log_file.path}")
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        project, notes, issues = load_point_table(args[0])
        window.use_project(project, args[0], notes)
        if error_lines(issues):
            window.status.showMessage(f"{len(error_lines(issues))} 个错误，启动已被阻止")
        _prepare_network(window, args[0])
    window.show()
    return app, window, session
