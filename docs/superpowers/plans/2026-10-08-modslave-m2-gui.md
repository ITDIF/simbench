# Modbus 从站模拟器 M2（桌面 GUI）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 M1 的内核套一个 PySide6 桌面界面：设备树 + 点位表（双击改值、类型/范围不合法就拒绝）+ 报文监视窗 + 通道独立启停 + CSV/工程读写，并且**状态变化不改变布局几何**这件事有自动化断言。

**Architecture:** 仍守 `gui → engine → backend → project/codec`。M2 新增两个 Qt-free 的引擎件（`engine/runner.py` 持有 asyncio 线程、`engine/session.py` 是唯一可编辑状态与动作入口）与一个 Qt 薄层（`gui/*` 只有 model/view/dialog，全部通过 `Session` 的公开方法与 signal 桥收发）。pymodbus 依旧只在 `backend/`，PySide6 依旧只在 `gui/`——两道都由 `tests/test_architecture.py` 的 AST 守卫钉住。

**Tech Stack:** Python 3.13.9、PySide6 6.9.2（已装进 `.venv`）、pymodbus 3.15.0、pytest 9.1.1。测试在 `QT_QPA_PLATFORM=offscreen` 下跑（已实测可拿到真实几何）。

**依赖:** M1 已完成并在分支 `m1-kernel`（576 测试）。本计划继续在 `m1-kernel` 上做（同一工具的下一个里程碑，不另开分支；M2 结束由用户决定是否合 master）。

---

## M2 前置实测（本轮亲自跑出来的，别当假设）

探针 `%TEMP%\m2-probe\qt_spike.py`，解释器 `.venv/Scripts/python.exe`：

1. **PySide6 6.9.2 在 venv 里可用**，`QT_QPA_PLATFORM=offscreen` 能建 `QApplication`、`QMainWindow`、`QToolBar`、`QTreeWidget`、`QTabWidget`、可勾选 `QPushButton` 组。GUI 测试**不需要**真实显示器，也不需要 pytest-qt。
2. **跨线程 Qt signal 可用，不需要 qasync**：工作线程 `emit` 200 次，主线程收到 **200 次**，`received on wrong thread: 0`，顺序严格单调（`max gap 1`），同窗口内 `QTimer(20ms)` 照常触发 60 次。→ 引擎线程直接往一个"住在主线程的 QObject"上 emit 是安全的，投递由 Qt 的 queued connection 负责。
3. **批量 `dataChanged` 很便宜**：200/1000/5000 行模型上每次覆盖 200 行发 `dataChanged`，最坏 **0.34ms**。→ 10Hz 全表刷新不是瓶颈，不要为它做虚拟滚动或增量 diff 优化（YAGNI）。
4. **offscreen 有真实几何**：`show()` + `processEvents()` 后 `w.size().width()==900`、工具栏高 27；改文案（`''→'已停止'`、`启动→停止`）前后 `label 宽/按钮宽/工具栏高/窗口宽` **完全不变**（在设了 `setMinimumWidth` 的定宽槽位上）。→ "状态变化不许挪布局"可以写成断言，不靠肉眼验收。

## 用户已定的界面口径（不是建议，是前提）

这三条来自他反复当场选过的界面整改，M2 直接按默认取值实现，**不要再列"下拉 vs 标签"让他选第二遍**：

1. **可枚举的少量选项（≤10 项）不用下拉框，用平铺可勾选标签。** 报文窗的过滤就是 4 个标签：`全部 / 请求 / 响应 / 异常`。
2. **默认视图给"全部"**，不给"跟随当前选中"。报文窗打开时默认 `全部`；点位表默认展示当前通道的所有设备，不预选过滤。
3. **状态变化不许改变布局尺寸，也不许让既有可见元素消失。** 通道从 running→error 是**同一格文字变色**，不是"多出一个红色标签把邻居挤掉"，也不是"按钮消失"。滚动条收在面板内部。
4. 面板高度**弹性分配**：报文窗这类会增长的区块不写死 `max-height`/固定高度，避免它把同栏兄弟面板挤没（他在网页端为这件事打过两次回）。

对应到 M2 的可测断言：Task 9 用 offscreen 几何证明"三态下宽/高恒定"。

## 已裁决的界面取舍（写代码前定死，别再猜）

| # | 取舍 | 定的口径 | 理由 |
|---|---|---|---|
| α | 点位表能不能改 area/地址/类型 | **不能**。它们属于点表（CSV/工程），改要走导入或点位属性面板；表里只有"当前值"一列可编辑 | 点位表改地址会与 `unit_duplicated`/`overlap` 校验打架，且现场改点表要留痕 |
| β | 双击改值写不写进寄存器 | **写**，且立刻回读一次刷新该行；失败（类型不符/越界/只读被拒）在表格里就地提示，不弹模态框 | 联调要的是"改了马上能看到主站读到什么" |
| γ | 报文窗与 CLI 打印的关系 | 报文窗**不替代** CLI；两者共用 `PacketEvent` 字段，GUI 只是多一个 `parsed` 摘要视图 | 复用 M1 已钉的 6 字段契约 |
| δ | 主站在 GUI 里写值算不算"手动接管" | M2 不做值归属（`source`）——那是 M4 波形才有的冲突。M2 只在 UI 上显示 CSV 里配的波形名，只读 | 避免造一个 M4 会推翻的中间态 |
| ε | 未保存修改的退出提示 | **做**：窗口标题带 `*`，关闭时问一次 | 现场点了半小时点表被误关是真损失 |
| ζ | 一个通道都没起时 | 表格可读（显示点表与初值），改值按钮禁用并给出原因 | 让"看配置"不依赖"起服务" |

## 文件结构（M2 新增部分）

```
modslave/
├── engine/                       # Qt-free：不 import PySide6
│   ├── runner.py                 # 新增：asyncio 线程（生产版），submit/stop
│   ├── session.py                # 新增：唯一可编辑状态与动作入口（load/start/stop/set_point/snapshot）
│   ├── packets.py                # 新增：环形缓冲 + 过滤（全部/请求/响应/异常）+ 暂停，Qt-free
│   └── events.py? 不动            # M1 的 events 在 backend/events.py，继续用
└── gui/                          # 只有这里能 import PySide6
    ├── __init__.py
    ├── bridge.py                 # EngineBridge(QObject)：把 Session 回调转成 Qt signal
    ├── point_table.py            # PointTableModel(QAbstractTableModel) + 值编辑校验
    ├── packet_view.py            # PacketTableModel + 4 个过滤标签 + 暂停 + 清除
    ├── device_tree.py            # 设备树（通道→设备），状态只改颜色不改几何
    ├── dialogs.py                # 校验错误清单、CSV 导入（端口/整体偏移）、通道属性
    └── main_window.py            # 组装：工具栏 + 左树 + 中表 + 下标签页 + 状态栏
tests/
├── conftest.py                   # 改：qapp fixture（offscreen）+ 复用 EngineRunner
├── test_runner.py                # 新增：线程/submit/stop/异常不吞
├── test_session.py               # 新增：load 校验门槛、启停、set_point 成功与失败路径
├── test_packets.py               # 新增：环形缓冲上限、过滤、暂停
├── test_gui_point_table.py       # 新增：model 列/编辑校验/dataChanged 定点
├── test_gui_packet_view.py       # 新增：过滤默认"全部"、标签而非下拉
├── test_gui_layout_invariant.py  # 新增：三态几何恒定（offscreen 实测断言）
└── test_gui_main_window.py       # 新增：接线（改值→引擎→主站读到）、未保存提示、离线禁用
```

依赖方向新增一条：`gui/` 是**唯一**能 import PySide6 的地方（现有 AST 守卫加一条断言即可，Task 1 做）。

---

## Task 1: GUI 基建与两道分层守卫

**Files:**
- Modify: `requirements.txt`
- Modify: `tests/conftest.py`
- Modify: `tests/test_architecture.py`
- Create: `modslave/gui/__init__.py`、`modslave/gui/bridge.py`
- Test: `tests/test_architecture.py`（守卫用例）、`tests/test_bridge.py`

- [ ] **Step 1: 把 PySide6 写进依赖并确认版本**

Run:
```bash
cd /d/project/PythonProject/modslave-sim
.venv/Scripts/python.exe -m pip show PySide6 | grep -E "^(Name|Version)"
```
Expected: `PySide6 6.9.2`（本轮已装进 venv，别装到全局 Anaconda）。

- [ ] **Step 2: 改 `requirements.txt`**

在文件末尾追加一行（保持 M1 三行不动）：

```
PySide6==6.9.2
```

- [ ] **Step 3: 加守卫用例（先让它红）**

在 `tests/test_architecture.py` 末尾追加。注意 `_offenders` 现在的签名是显式声明跳过哪一层（M1 修过恒过守卫），照它现有写法传参，**不要**回到靠 `parts` 猜层级：

```python
GUI_PACKAGE = "gui"


def test_only_gui_touches_pyside6():
    """PySide6 只许出现在 modslave/gui/。

    这条是 M2 新增的线：引擎/点表/后端一旦摸 Qt，pytest 就必须起事件循环，
    内核测试立刻和界面绑死——那是 M1 特意拆开的东西。
    """
    offenders = _offenders(None, GUI_ONLY, skip_folders=(GUI_PACKAGE,))
    assert offenders == [], f"Qt leaked outside gui/: {offenders}"


def test_gui_actually_exists_and_is_the_only_qt_owner():
    """防空跑：gui/ 里必须真有人 import Qt，否则上一条是恒真断言。"""
    files = _python_files(GUI_PACKAGE)
    assert files, "modslave/gui/ 还不存在——M2 没开始，别留一条空守卫"
    importers = {f.name for f in files if "PySide6" in _imported_modules(f)}
    assert importers, "gui/ 里没有任何 PySide6 import，守卫失去意义"


def test_engine_and_backend_stay_qt_free():
    for folder in ("engine", "backend", "project", "codec"):
        offenders = _offenders(folder, GUI_ONLY)
        assert offenders == [], f"Qt leaked into {folder}/: {offenders}"
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_architecture.py -q`
Expected: FAIL —`test_only_gui_touches_pyside6` 因 `skip_folders`/`_offenders` 签名不符或 `gui/` 不存在而红（`test_gui_actually_exists_and_is_the_only_qt_owner` 会明确报"gui/ 还不存在"）。**如果它直接绿了，说明守卫又是恒过的——停下报告，别继续。**

- [ ] **Step 4: 给 `_offenders` 加显式 skip 参数**

按现有实现的最小改法（签名默认空元组，行为向后兼容）：

```python
def _offenders(folder: str | None, needles: tuple[str, ...],
               skip_folders: tuple[str, ...] = ()) -> list[str]:
    offenders: list[str] = []
    for file in _python_files(folder):
        rel = file.parts[len(ROOT.parts):]
        if rel and rel[0] in skip_folders:
            continue
        for module in _imported_modules(file):
            if module in needles:
                offenders.append(f"{file}: {module}")
    return offenders
```

`test_only_backend_touches_pymodbus` 保持原样（pymodbus 只许 backend），只把它的 `skip_folders` 传成 `(BACKEND,)`；原来的死参 `allow_backend` 删掉，所有调用点同步。

- [ ] **Step 5: 建 `modslave/gui/__init__.py` 与 `bridge.py`**

`gui/__init__.py`：一行 docstring（`"""PySide6 界面层：本目录之外不许 import Qt。"""`）。

`gui/bridge.py` —— 这是 Qt 与非 Qt 的唯一接缝，也是"跨线程 signal"的落点：

```python
"""引擎回调 → Qt signal。工作线程只管 emit，投递交给 Qt。

实测（M2 前置探针）：工作线程 emit 200 次，主线程收到 200 次、顺序不乱、
0 次落在错误线程。所以不需要 qasync，也不需要自己写 QTimer 轮询队列。
"""

from __future__ import annotations

from PySide6 import QtCore

from ..backend.events import ChannelState, LogLine, PacketEvent, PointChanged


class EngineBridge(QtCore.QObject):
    """住在主线程的信号载体。Session 的回调里只准调这几个 emit。"""

    packet = QtCore.Signal(object)      # PacketEvent
    point_changed = QtCore.Signal(object)   # PointChanged
    channel_state = QtCore.Signal(object)   # ChannelState
    logged = QtCore.Signal(object)      # LogLine

    def forward(self, event) -> None:
        """Session/Runner 的唯一入口。线程安全：只做 emit，不碰 widget。"""
        if isinstance(event, PacketEvent):
            self.packet.emit(event)
        elif isinstance(event, PointChanged):
            self.point_changed.emit(event)
        elif isinstance(event, ChannelState):
            self.channel_state.emit(event)
        elif isinstance(event, LogLine):
            self.logged.emit(event)
        else:                           # 未知事件不许静默丢弃
            self.logged.emit(LogLine("warning", f"unknown engine event {type(event).__name__}"))
```

- [ ] **Step 6: `qapp` fixture（offscreen，会话级一次）**

改 `tests/conftest.py`，追加（**不要**改动现有 `open_backend`/`free_port` 的对外签名，M1 的测试在依赖它们）：

```python
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 必须在 import PySide6 之前


@pytest.fixture(scope="session")
def qapp():
    """offscreen 模式的 QApplication。实测 show()+processEvents() 后几何是真的。"""
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    # 不 quit：QApplication 退出后无法再建，同一次会话里还要给后面的用例用
```

```python
def drain(events: int = 50) -> None:
    """把 queued signal 冲到投递完成（测试里等 UI 更新用，别用 sleep 猜时序）。"""
    from PySide6 import QtCore

    loop = QtCore.QCoreApplication.eventLoop()
    for _ in range(events):
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 50)
        if not loop:
            break
```

- [ ] **Step 7: 跑守卫与桥接用例，确认全绿**

`tests/test_bridge.py`：

```python
import pytest
from PySide6 import QtCore
from modslave.backend.events import PacketEvent, PointChanged, LogLine, ChannelState
from modslave.gui.bridge import EngineBridge


def test_forward_reaches_main_thread_from_worker(qapp):
    """跨线程投递：worker emit，主线程收到。实测口径的回归版本。"""
    import threading

    bridge = EngineBridge()
    got: list[object] = []
    threads: list[int] = []
    main_tid = threading.get_ident()
    bridge.packet.connect(lambda e: (got.append(e), threads.append(threading.get_ident())))

    def worker():
        for i in range(20):
            bridge.forward(PacketEvent("c", "rx", bytes([i]), i, 3, "x", 0.0))

    t = threading.Thread(target=worker)
    t.start()
    deadline = QtCore.QTime.currentTime().addMSecs(3000)
    while len(got) < 20 and QtCore.QTime.currentTime() < deadline:
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 20)
    t.join(2)
    qapp.processEvents()

    assert len(got) == 20, f"只收到 {len(got)} 条"
    assert set(threads) == {main_tid}, "有 signal 落在了非主线程"
    assert [p.raw[0] for p in got] == list(range(20)), "投递顺序乱了"


def test_unknown_event_becomes_a_warning_not_silence(qapp):
    bridge = EngineBridge()
    got: list[object] = []
    bridge.logged.connect(got.append)
    bridge.forward(object())
    qapp.processEvents()
    assert got and got[0].level == "warning"
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_architecture.py tests/test_bridge.py -q`
Expected: 全通过（架构 10~12 条 + 桥接 2 条）。

- [ ] **Step 8: 全量回归 + 提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: M1 的 576 条 + 本次新增全绿，无 skip/xfail。

```bash
git add requirements.txt modslave/gui tests/conftest.py tests/test_architecture.py tests/test_bridge.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "chore: 加 PySide6 依赖、offscreen 测试基建与 Qt 分层守卫"
```

---

## Task 2: 引擎线程 `engine/runner.py`（生产版，替掉测试里的私有副本）

**Files:**
- Create: `modslave/engine/runner.py`
- Modify: `tests/conftest.py`（改用 EngineRunner，删掉重复的 `_LoopThread`）
- Test: `tests/test_runner.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_runner.py
import threading
import time

import pytest
from modslave.engine.runner import EngineRunner


async def noop():
    return 42


def test_submit_returns_result_and_runs_on_engine_thread():
    runner = EngineRunner()
    runner.start()
    try:
        assert runner.submit(noop()).result(5) == 42
        assert runner.thread_ident != threading.get_ident()
        assert runner.thread_name.startswith("modslave-engine")
    finally:
        runner.stop()


def test_start_and_stop_are_idempotent():
    runner = EngineRunner()
    runner.start(); runner.start()
    runner.stop(); runner.stop()
    assert not runner.alive
    with pytest.raises(RuntimeError, match="not running"):
        runner.submit(noop())


def test_stop_joins_the_thread_so_nothing_leaks():
    runner = EngineRunner()
    runner.start()
    tid = runner.thread_ident
    assert tid is not None
    runner.stop()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        alive = [t for t in threading.enumerate() if t.ident == tid]
        if not alive:
            break
        time.sleep(0.02)
    else:
        pytest.fail("引擎线程没被 join 掉")


def test_a_failing_coroutine_raises_out_not_silently():
    async def boom():
        raise ValueError("inner")

    runner = EngineRunner()
    runner.start()
    try:
        with pytest.raises(ValueError, match="inner"):
            runner.submit(boom()).result(5)
    finally:
        runner.stop()


def test_events_reach_the_subscriber_with_ordering():
    """runner 只负责把事件按序交到订阅者手里，不猜 Qt。"""
    runner = EngineRunner()
    seen: list[int] = []
    runner.subscribe(seen.append)
    runner.start()
    try:
        async def emit_all():
            for i in range(30):
                runner.publish(("e", i))
        runner.submit(emit_all()).result(5)
    finally:
        runner.stop()
    assert seen == [("e", i) for i in range(30)]


def test_two_runners_do_not_share_state():
    a, b = EngineRunner(), EngineRunner()
    a.start(); b.start()
    try:
        assert a.thread_ident != b.thread_ident
        assert a.submit(noop()).result(5) == b.submit(noop()).result(5) == 42
    finally:
        a.stop(); b.stop()
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: modslave.engine.runner`

- [ ] **Step 2: 写 `runner.py`**

```python
"""引擎线程：一个专属线程跑 asyncio loop，M1 的 conftest 私有实现的正式版。

为什么不合并到 backend：backend 只管协议，runner 只管"把协程跑在别的线程上、
并把事件按序送出去"。测试与 GUI 都只跟这个类说话。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

_MAX_JOIN_SECONDS = 5.0


class EngineRunner:
    def __init__(self, name: str = "modslave-engine") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sinks: list[Callable[[Any], None]] = []
        self._queue: queue.Queue = queue.Queue()
        self._pump_stop = threading.Event()
        self._pump: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._name)
        self._pump = threading.Thread(target=self._pump_events, daemon=True,
                                      name=f"{self._name}-events")
        self._thread.start()
        self._pump.start()
        while self._loop is None and not ready.is_set():
            ready.wait(0.05)
            if self._thread is not None and not self._thread.is_alive():
                raise RuntimeError("engine thread died before the loop started")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            asyncio.run_coroutine_threadsafe(_noop(), loop)  # 唤醒一次，确保 stop 排队有序
            loop.call_soon_threadsafe(loop.stop)
            thread.join(_MAX_JOIN_SECONDS)
        if self._pump is not None:
            self._pump_stop.set()
            self._pump.join(_MAX_JOIN_SECONDS)
        self._loop, self._thread, self._pump = None, None, None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread else None

    @property
    def thread_name(self) -> str:
        return self._name

    # ---------- 提交协程 ----------

    def submit(self, coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Future:
        """传工厂而不是协程对象：loop 没起来时协程从未被 await 过，不会有悬空 warning。"""
        loop = self._loop
        if loop is None or not self.alive:
            raise RuntimeError("engine runner is not running")
        return asyncio.run_coroutine_threadsafe(coro_factory(), loop)

    # ---------- 事件外发 ----------

    def subscribe(self, sink: Callable[[Any], None]) -> None:
        self._sinks.append(sink)

    def drain(self, seconds: float = 2.0) -> bool:
        """等事件队列排空（测试与 GUI 关闭时用）。True=排空，False=超时。

        只能从非 pump 线程调；pump 线程自己调会死锁，所以这里用队列长度判空，
        再补一次短 sleep 让最后一个 sink 调用返回。
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._queue.empty():
                time.sleep(0.01)
                if self._queue.empty():
                    return True
            time.sleep(0.005)
        return self._queue.empty()

    def publish(self, event: Any) -> None:
        """引擎线程内调用。绝不直接在调用方线程跑订阅者回调——那是 UI 崩溃的来源。

        投递顺序由队列保证；UI 侧再经 EngineBridge 转成 Qt queued signal。
        """
        self._queue.put(event)

    def _pump_events(self) -> None:
        while not self._pump_stop.is_set():
            try:
                event = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            for sink in list(self._sinks):
                try:
                    sink(event)
                except Exception:                        # noqa: BLE001
                    pass    # UI 回调炸了不许带停引擎；错误由 GUI 自己负责显示


async def _noop() -> None:
    return None
```

- [ ] **Step 3: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runner.py -q`
Expected: 7 passed。若 `test_stop_joins_the_thread_so_nothing_leaks` 失败，是 `stop()` 的 join 时序问题——修 `stop()`，**不要**把断言改成 sleep 容忍。

- [ ] **Step 4: 让 conftest 复用 EngineRunner**

`tests/conftest.py` 里删掉私有 `_LoopThread`，`open_backend` 改用 `EngineRunner`（保持 `open_backend(channel, packet_sink=None)` 的对外行为与 `backend.port` 语义完全不变）。改完必须**全量**跑一次：M1 的 27 条真主站用例 + 10 条 CLI 用例是这次重构的回归网。

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿（用例数不减）。

- [ ] **Step 5: 提交**

```bash
git add modslave/engine/runner.py tests/test_runner.py tests/conftest.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 生产用引擎线程 runner，测试夹具改用它"
```

---

## Task 3: 会话层 `engine/session.py`（GUI 唯一能碰的东西）

**Files:**
- Create: `modslave/engine/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_session.py
import time

import pytest
from pymodbus.client import ModbusTcpClient

from modslave.backend.events import PacketEvent
from modslave.engine.session import Session
from modslave.project.model import Project
from modslave.project.validate import Issue
from modslave.csv_io_entry import load_point_table
from tests.conftest import free_port

CSV = "examples/covi-tunnel.csv"          # M1 的点表：unit 11 有 CDAB 浮点与只读点


def loaded(port: int | None = None) -> Project:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    if port:
        project.channels[0].port = port
    return project


@pytest.fixture
def session():
    from modslave.engine.runner import EngineRunner
    runner = EngineRunner()
    s = Session(runner=runner)
    yield s
    s.shutdown()


def test_load_replaces_project_and_reports_issues(session):
    issues = session.load(loaded())
    assert issues == []
    assert session.dirty is False
    assert {r.unit_id for r in session.rows("tcp-5020")} == {11, 12}


def test_start_is_refused_when_validation_fails(session):
    project = loaded()
    project.channels[0].port = 0
    session.load(project)
    with pytest.raises(ValueError) as exc:
        session.start_channel("tcp-5020")
    assert "refusing to start" in str(exc.value)
    assert not session.channel("tcp-5020").running


def test_channel_lifecycle_and_state_events(session):
    seen: list[str] = []
    session.subscribe(lambda e: seen.append(getattr(e, "state", "") or ""))
    session.load(loaded(free_port()))
    session.start_channel("tcp-5020")
    assert session.channel("tcp-5020").running
    session.stop_channel("tcp-5020")
    assert not session.channel("tcp-5020").running
    assert "running" in seen and "stopped" in seen


def test_master_read_seeds_the_row_snapshot(session):
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    row = next(r for r in session.rows("tcp-5020") if r.name == "CO浓度")
    assert row.value_text == "12.5"          # 初值来自 CSV，字序 CDAB
    assert row.registers == [0x0000, 0x4148]


def test_set_point_writes_and_refuses_readonly(session):
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    ok = session.set_point("tcp-5020", 11, "hr", 4, "37")
    assert ok.ok and ok.value_text == "37"
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [37]
    finally:
        client.close()
    refused = session.set_point("tcp-5020", 11, "hr", 0, "9")
    assert refused.ok                          # 引擎侧写只读点是允许的（M1 已定口径）
    blocked = ModbusTcpClient("127.0.0.1", port=port)
    assert blocked.connect()
    try:
        bad = blocked.write_register(0, 123, device_id=11)
        assert bad.isError() and bad.exception_code == 2
    finally:
        blocked.close()


def test_set_point_rejects_bad_input_without_touching_registers(session):
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    for text, needle in [("abc", "int16|int32|uint16|needs"), ("99999999", "range"),
                         ("", "needs")]:
        res = session.set_point("tcp-5020", 11, "hr", 4, text)
        assert not res.ok and needle in res.error
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
    finally:
        client.close()


def test_rows_for_unknown_channel_is_empty(session):
    session.load(loaded())
    assert session.rows("nope") == []
    assert session.channel("nope") is None


def test_set_point_when_channel_stopped_is_a_domain_error(session):
    session.load(loaded(free_port()))
    res = session.set_point("tcp-5020", 11, "hr", 4, "1")
    assert not res.ok and "not running" in res.error


def test_dirty_flag_tracks_edits(session):
    session.load(loaded(free_port()))
    session.start_channel("tcp-5020")
    assert session.dirty is False
    session.mark_dirty()
    assert session.dirty is True


def test_rows_reflect_a_master_write_on_the_next_pull(session):
    """拉模型的正证：主站写进去之后，下一次 rows() 就该是新值（GUI 10Hz 拉）。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        client.write_register(4, 55, device_id=11)
    finally:
        client.close()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if row_read(session, "风机状态") == "55":
            return
        time.sleep(0.02)
    pytest.fail(f"rows() 没能在 2s 内反映主站写入，读到 "
                f"{row_read(session, '风机状态')!r}")


def row_read(session, name: str) -> str:
    return next(r.value_text for r in session.rows("tcp-5020") if r.name == name)


def test_packet_events_carry_the_channel_name(session):
    """通道名在建 backend 时用闭包绑定，所以这里必须是真通道名，不是 "?"。"""
    port = free_port()
    session.load(loaded(port))
    events: list[object] = []
    session.subscribe(events.append)
    session.start_channel("tcp-5020")
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        client.read_holding_registers(0, count=1, device_id=11)
    finally:
        client.close()
    session.wait_idle()
    packets = [e for e in events if isinstance(e, PacketEvent)]
    assert packets, "一个报文事件都没收到"
    assert {p.channel for p in packets} == {"tcp-5020"}
    assert any(p.direction == "rx" and p.unit_id == 11 and p.function_code == 3
               for p in packets), [vars(p) for p in packets]
    assert any(p.direction == "tx" for p in packets)
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_session.py -q`
Expected: FAIL — `ModuleNotFoundError: modslave.engine.session`

- [ ] **Step 2: 写 `session.py`**

```python
"""Session：GUI 唯一可对话的对象。持有工程、通道状态、点表行；所有动作都在此收口。

Qt-free（不 import PySide6），所以 M1/M2 的内核测试不需要事件循环。
所有对 pymodbus 的操作都通过 backend + runner.submit 走引擎线程，Session 自己不碰 loop。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..backend.events import ChannelState, PacketEvent
from ..backend.modbus_tcp import ModbusTcpBackend
from ..codec.point_codec import decode, encode
from ..project.model import Point, Project, area_is_bit
from ..project.validate import Issue, error_lines, validate_project
from .runner import EngineRunner

_ROW_COLS = ("channel", "device", "unit_id", "name", "area", "address", "datatype",
             "word_order", "value_text", "registers", "readonly", "waveform")


@dataclass(frozen=True)
class Row:
    """点位表一行。GUI 按字段名取，顺序变化不影响，但改名字要先改这里。"""

    channel: str
    device: str
    unit_id: int
    name: str
    area: str
    address: int
    datatype: str
    word_order: str
    value_text: str
    registers: tuple[int, ...]
    readonly: bool
    waveform: str

    @property
    def key(self) -> tuple[str, int, str, int]:
        return (self.channel, self.unit_id, self.area, self.address)


@dataclass
class WriteResult:
    ok: bool
    error: str = ""
    value_text: str = ""
    registers: tuple[int, ...] = ()


@dataclass
class ChannelRuntime:
    name: str
    running: bool = False
    detail: str = ""
    connections: int = 0
    backend: ModbusTcpBackend | None = None
    last_error: str = ""


class Session:
    def __init__(self, runner: EngineRunner | None = None,
                 on_event: Callable[[object], None] | None = None) -> None:
        self._runner = runner or EngineRunner()
        self._owns_runner = runner is None
        if not self._runner.alive:
            self._runner.start()
        self._sinks: list[Callable[[object], None]] = []
        self._own_sink = on_event
        self._lock = threading.RLock()
        self._project = Project(channels=[])
        self._channels: dict[str, ChannelRuntime] = {}
        self._points: dict[tuple[str, int], dict[str, Point]] = {}
        self._dirty = False
        self._runner.subscribe(self._on_engine_event)

    # ---------- 装载 ----------

    def load(self, project: Project) -> list[Issue]:
        """换一份工程。有 error 也照样装（让人能改完再启动），但把清单原样返回。"""
        with self._lock:
            self._stop_all_locked()
            self._project = project
            self._channels = {ch.name: ChannelRuntime(ch.name) for ch in project.channels}
            self._points = {}
            self._rows_meta = {ch.name: [] for ch in project.channels}
            for ch in project.channels:
                for dev in ch.devices:
                    bucket = self._points.setdefault((ch.name, dev.unit_id), {})
                    for p in dev.points:
                        bucket[(p.area, p.address)] = p
                        self._rows_meta[ch.name].append((dev.unit_id, dev.name, p))
            self._dirty = False
        return validate_project(project)

    @property
    def project(self) -> Project:
        return self._project

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        self._dirty = True

    # ---------- 启停 ----------

    def channel(self, name: str) -> ChannelRuntime | None:
        with self._lock:
            return self._channels.get(name)

    def channel_names(self) -> list[str]:
        with self._lock:
            return list(self._channels)

    def start_channel(self, name: str) -> None:
        runtime, channel = self._require(name)
        if runtime.running:
            return
        issues = validate_project(self._project)
        errors = error_lines(issues)
        if errors:
            raise ValueError(f"point table has {len(errors)} error(s); refusing to start: "
                             + " | ".join(errors[:3]))
        backend = ModbusTcpBackend(channel, packet_sink=self._packet_sink(name))
        try:
            self._runner.submit(lambda: backend.start()).result(10)
        except Exception as exc:                    # noqa: BLE001
            runtime.last_error = str(exc)
            self._publish(ChannelState(name, "error", str(exc)))
            raise
        runtime.backend = backend
        runtime.running = True
        runtime.detail = f"{channel.host}:{backend.port}"
        runtime.last_error = ""
        self._publish(ChannelState(name, "running", runtime.detail))

    def stop_channel(self, name: str) -> None:
        runtime, _ = self._require(name)
        self._stop_one(runtime)
        self._publish(ChannelState(name, "stopped", ""))

    def start_all(self) -> list[str]:
        failures: list[str] = []
        for name in self.channel_names():
            try:
                self.start_channel(name)
            except ValueError as exc:
                failures.append(str(exc))
        return failures

    def stop_all(self) -> None:
        with self._lock:
            self._stop_all_locked()

    def shutdown(self) -> None:
        self.stop_all()
        if self._owns_runner:
            self._runner.stop()

    # ---------- 点表读写 ----------

    def rows(self, channel: str) -> list[Row]:
        """按点表顺序出一批行。通道在跑就回读寄存器，没跑就用 CSV 初值。"""
        with self._lock:
            runtime = self._channels.get(channel)
            meta = list(self._rows_meta.get(channel, ()))
        out: list[Row] = []
        for unit_id, device, pt in meta:
            regs = self._read_back(runtime, pt, unit_id)
            value_text, registers = _render(pt, regs)
            out.append(Row(channel, device, unit_id, pt.name, pt.area, pt.address,
                           pt.datatype, pt.word_order, value_text, registers,
                           pt.readonly, pt.waveform))
        return out

    def set_point(self, channel: str, unit_id: int, area: str, address: int,
                  text: str) -> WriteResult:
        """改一个点的值。类型不符/越界一律拒绝且不落寄存器。

        引擎写只读点是允许的（M1 定的口径：readonly 是"挡主站"的墙），
        所以从界面改只读量不会被拒——界面上要另外标注它是只读量。
        """
        runtime, _ = self._require(channel)
        if not runtime.running or runtime.backend is None:
            return WriteResult(False, f"channel {channel} is not running")
        pt = self._points.get((channel, unit_id), {}).get((area, address))
        if pt is None:
            return WriteResult(False, f"{area} address {address} is not in the point table")
        try:
            registers = encode(text, pt.datatype, pt.word_order)
        except ValueError as exc:
            return WriteResult(False, str(exc))
        try:
            if area_is_bit(area):
                ok = self._runner.submit(
                    lambda: _awrite_bits(runtime.backend, unit_id, area, address,
                                         bool(registers[0]))).result(5)
            else:
                ok = self._runner.submit(
                    lambda: _awrite(runtime.backend, unit_id, area, address,
                                    registers)).result(5)
        except Exception as exc:                    # noqa: BLE001
            return WriteResult(False, f"engine write failed: {exc}")
        if not ok:
            return WriteResult(False, f"{area} address {address} is outside the point table")
        regs = self._read_back(runtime, pt, unit_id)
        if regs is None:
            return WriteResult(False, f"read-back failed for {area} {address}")
        value_text, registers_out = _render(pt, regs)
        with self._lock:
            self._dirty = True
        return WriteResult(True, "", value_text, registers_out)

    # ---------- 事件 ----------

    def subscribe(self, sink: Callable[[object], None]) -> None:
        with self._lock:
            self._sinks.append(sink)

    def wait_idle(self, seconds: float = 2.0) -> None:
        """等事件队列排空（测试与关窗时用）。GUI 里别调，它会堵事件循环。"""
        if not self._runner.drain(seconds):
            raise AssertionError("engine event queue did not drain")

    def _packet_sink(self, channel: str):
        """把通道名闭包进去：M1 的 backend 回调签名是定死的六个位置参数，不带通道名。

        不在回调里按 unit id 反查通道——同一 unit 理论上可以出现在两个通道，
        反查会给出错答案；绑一次就永远是对的那个。
        """
        def sink(direction: str, raw: bytes, unit, function, summary: str,
                 mono: float) -> None:
            self._runner.publish(PacketEvent(channel, direction, raw, unit, function,
                                             summary, mono))
        return sink

    def _publish(self, event: object) -> None:
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink(event)
            except Exception:                        # noqa: BLE001
                pass

    def _on_engine_event(self, event: object) -> None:
        self._publish(event)

    # ---------- 内部 ----------

    def _require(self, name: str) -> tuple[ChannelRuntime, object]:
        with self._lock:
            runtime = self._channels.get(name)
            if runtime is None:
                raise KeyError(name)
            channel = next(c for c in self._project.channels if c.name == name)
            return runtime, channel

    def _stop_one(self, runtime: ChannelRuntime) -> None:
        if runtime.backend is not None:
            try:
                self._runner.submit(lambda: runtime.backend.stop()).result(10)
            except Exception as exc:                # noqa: BLE001
                runtime.last_error = str(exc)
        runtime.backend = None
        runtime.running = False
        runtime.detail = ""

    def _stop_all_locked(self) -> None:
        for runtime in self._channels.values():
            self._stop_one(runtime)

    def _read_back(self, runtime: ChannelRuntime | None, pt: Point,
                   unit_id: int) -> list[int] | None:
        """回读一个点位当前的寄存器。通道没跑/地址不在块内/后端异常一律回 None。

        返回 None 时 `_render` 回落到 CSV 初值——那是"当前值未知"的显式表达，
        比拿 0 冒充一个真值好（0 在界面上和"设备报了 0"分不出来）。
        """
        if runtime is None or runtime.backend is None:
            return None
        try:
            if area_is_bit(pt.area):
                bits = runtime.backend.read_bits(unit_id, pt.area, pt.address, 1)
                return None if bits is None else [1 if bits[0] else 0]
            return runtime.backend.read_registers(unit_id, pt.area, pt.address, pt.span)
        except Exception:                                # noqa: BLE001
            return None


def _render(pt: Point, regs: list[int] | None) -> tuple[str, list[int]]:
    if not regs:
        return pt.initial, []
    try:
        value = decode(list(regs), pt.datatype, pt.word_order)
    except ValueError:
        return pt.initial, list(regs)
    if isinstance(value, bool):
        return ("true" if value else "false"), list(regs)
    if isinstance(value, float):
        return (f"{value:g}", list(regs))
    return (str(value), list(regs))


async def _awrite(backend, unit_id, area, address, registers):
    return backend.write_registers(unit_id, area, address, registers)


async def _awrite_bits(backend, unit_id, area, address, value):
    return backend.write_bits(unit_id, area, address, value)
```

**一处要留意的现有取舍**：`_render()` 在回读失败（`regs is None`）时回落到 CSV 初值。这正好覆盖
"通道没启动时显示初值"，但也意味着**启动了且读不到**会看起来像初值——Task 5 的 `Row` 会加一个
`stale: bool` 字段把这两种情况分开，界面据此灰掉该格。M2 内部按"先有值可读再谈刷新"的顺序做，
Task 3 这里不提前加半个 stale。

Run: `.venv/Scripts/python.exe -m pytest tests/test_session.py -q`
Expected: 11 passed

- [ ] **Step 3: 全量回归 + 提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿（M1 的 576 + Task 1/2/3 新增，用例数不减、无 skip、无 xfail）。

---

---

## Task 4: 报文缓冲 `engine/packets.py`（Qt-free）

**Files:**
- Create: `modslave/engine/packets.py`
- Modify: `modslave/engine/session.py`（`__init__` 接一个可选 `packet_store`）
- Test: `tests/test_packets.py`

缓冲单独一层是因为 CLI（M1）只往 stdout 打，GUI 要能回看与过滤；两边共用同一个 ring buffer
和同一套过滤判据，"暂停/清除"才和"过滤"行为一致。**默认过滤器是 `全部`**（默认给全貌）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_packets.py
import time

import pytest
from pymodbus.client import ModbusTcpClient

from modslave.backend.events import PacketEvent
from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import (
    ALL, EXCEPTIONS, FILTERS, REQUESTS, RESPONSES, PacketStore, filters_for,
)
from modslave.engine.runner import EngineRunner
from modslave.engine.session import Session
from tests.conftest import free_port


def evt(direction="rx", unit=11, fc=3, mono=0.0, channel="tcp-5020"):
    return PacketEvent(channel, direction, b"\x00\x01", unit, fc, f"unit {unit} fc {fc}", mono)


def test_default_filter_is_everything():
    store = PacketStore()
    assert store.active_filter == ALL
    store.add(evt())
    store.add(evt("tx", 77, 0x83))
    assert [p.direction for p in store.rows()] == ["rx", "tx"]


def test_buckets_are_disjoint_and_exhaustive():
    """除"全部"之外每个事件只落一个桶，且异常帧同时属于响应。"""
    assert filters_for(evt()) == {ALL, REQUESTS}
    assert filters_for(evt("tx", 1, 3)) == {ALL, RESPONSES}
    assert filters_for(evt("tx", 77, 0x83)) == {ALL, RESPONSES, EXCEPTIONS}


def test_filter_names_are_exactly_the_four_labels():
    assert FILTERS == ("全部", "请求", "响应", "异常")


def test_set_filter_rejects_unknown_name():
    store = PacketStore()
    with pytest.raises(ValueError, match="unknown packet filter"):
        store.set_filter("最近一小时")
    assert store.active_filter == ALL          # 拒绝时不改原状态


def test_filter_narrows_rows():
    store = PacketStore()
    store.add(evt())
    store.add(evt("tx", 77, 0x83))
    store.set_filter(EXCEPTIONS)
    assert [p.direction for p in store.rows()] == ["tx"]
    store.set_filter(REQUESTS)
    assert [p.direction for p in store.rows()] == ["rx"]


def test_ring_buffer_drops_oldest_and_counts_it():
    store = PacketStore(capacity=3)
    for i in range(5):
        store.add(evt(mono=float(i)))
    assert [p.mono for p in store.rows()] == [2.0, 3.0, 4.0]
    assert store.dropped == 2


def test_pausing_stops_ingest_but_keeps_rows_readable():
    store = PacketStore()
    store.add(evt(mono=1.0))
    store.paused = True
    store.add(evt(mono=2.0))
    assert [p.mono for p in store.rows()] == [1.0]
    store.paused = False
    store.add(evt(mono=3.0))
    assert [p.mono for p in store.rows()] == [1.0, 3.0]


def test_clear_keeps_capacity():
    store = PacketStore(capacity=4)
    store.add(evt())
    store.clear()
    assert store.rows() == [] and store.dropped == 0
    store.add(evt(mono=9.0))
    assert [p.mono for p in store.rows()] == [9.0]


def test_bad_capacity_is_refused():
    with pytest.raises(ValueError, match="capacity"):
        PacketStore(capacity=0)


def test_listener_notified_once_per_accepted_packet():
    store = PacketStore()
    hits: list[int] = []
    store.subscribe(lambda: hits.append(1))
    store.add(evt())
    store.paused = True
    store.add(evt())                       # 暂停时不收，也就不通知
    assert len(hits) == 1


def test_a_broken_listener_does_not_stop_the_store():
    store = PacketStore()
    store.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("widget gone")))
    assert store.add(evt()) is True
    assert len(store.rows()) == 1


def test_session_feeds_the_store_with_the_real_channel_name():
    """端到端一句：起服务、主站读一次，缓冲里就该有 rx/tx 两条，且通道名是真的。"""
    project, _notes, issues = load_point_table("examples/covi-tunnel.csv")
    assert not [i for i in issues if i.level == "error"]
    port = free_port()
    project.channels[0].port = port
    store = PacketStore()
    session = Session(runner=EngineRunner(), packet_store=store)
    try:
        session.load(project)
        session.start_channel("tcp-5020")
        client = ModbusTcpClient("127.0.0.1", port=port)
        assert client.connect()
        try:
            client.read_holding_registers(0, count=1, device_id=11)
        finally:
            client.close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(store.rows()) < 2:
            time.sleep(0.02)
        directions = [p.direction for p in store.rows()]
        assert "rx" in directions and "tx" in directions
        assert {p.channel for p in store.rows()} == {"tcp-5020"}
    finally:
        session.shutdown()
```

- [ ] **Step 2: 实现 `packets.py`**

```python
# modslave/engine/packets.py
"""报文环形缓冲 + 过滤。Qt-free：CLI 与 GUI 共用，暂停/清除/过滤只有一份语义。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ..backend.events import PacketEvent

ALL = "全部"
REQUESTS = "请求"
RESPONSES = "响应"
EXCEPTIONS = "异常"
FILTERS: tuple[str, ...] = (ALL, REQUESTS, RESPONSES, EXCEPTIONS)   # 顺序即标签顺序


def filters_for(event: PacketEvent) -> set[str]:
    """一个事件属于哪些桶。

    方向以 pymodbus 的 `trace_packet(is_outgoing)` 为唯一依据（实测 RX 在解帧前、TX 在
    发送前）。异常响应也是"发出去的一帧"，所以它同时属于 响应 与 异常——不是单独一桶，
    否则过滤"响应"时会看不见异常，而现场最想看的恰恰就是它。
    """
    buckets = {ALL, RESPONSES if event.direction == "tx" else REQUESTS}
    if (event.function_code or 0) & 0x80:
        buckets.add(EXCEPTIONS)
    return buckets


class PacketStore:
    """容量固定、按到达顺序追加；`rows()` 只返回当前过滤器下可见的行。"""

    def __init__(self, capacity: int = 5000) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._items: deque[PacketEvent] = deque(maxlen=capacity)
        self.capacity = capacity
        self.dropped = 0
        self.paused = False
        self.active_filter = ALL
        self._listeners: list[Callable[[], None]] = []

    def add(self, event: PacketEvent) -> bool:
        """暂停时不落缓冲。返回是否收下——GUI 用它决定要不要重绘。"""
        if self.paused:
            return False
        overflow = len(self._items) == self._items.maxlen
        self._items.append(event)
        if overflow:
            self.dropped += 1
        self._notify()
        return True

    def set_filter(self, name: str) -> None:
        if name not in FILTERS:
            raise ValueError(f"unknown packet filter {name!r}, use one of {FILTERS}")
        self.active_filter = name
        self._notify()

    def rows(self) -> list[PacketEvent]:
        if self.active_filter == ALL:
            return list(self._items)
        return [e for e in self._items if self.active_filter in filters_for(e)]

    def clear(self) -> None:
        self._items.clear()
        self.dropped = 0
        self._notify()

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:                        # noqa: BLE001
                pass    # 界面回调炸了不许带停采集；窗口销毁前会自己退订
```

- [ ] **Step 3: Session 认 `packet_store`**

`session.py` 的 `__init__` 签名改成 `(self, runner=None, packet_store=None)`，末尾挂上：

```python
        if packet_store is not None:
            def to_store(event: object, store=packet_store) -> None:
                if isinstance(event, PacketEvent):
                    store.add(event)
            self.subscribe(to_store)
```

为什么包一层：Session 的事件流里除 `PacketEvent` 还有 `ChannelState`，直接
`subscribe(packet_store.add)` 会把状态事件也塞进报文缓冲。分流放在这里而不是
`PacketStore.add` 里，是为了让 `add` 继续是"收一条报文"这一个动作。

`PacketEvent` 已在 Task 3 导入；`field` 这个未用导入顺手删掉。

Run: `.venv/Scripts/python.exe -m pytest tests/test_packets.py tests/test_session.py -q`
Expected: 全通过，Task 3 的 12 条不退化。

- [ ] **Step 4: 提交**

```bash
git add modslave/engine/packets.py modslave/engine/session.py tests/test_packets.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 报文环形缓冲与四个过滤桶"
```

---

## Task 5: 点位表模型 `gui/point_table.py`

**Files:**
- Create: `modslave/engine/point_source.py`、`modslave/gui/point_table.py`
- Modify: `modslave/engine/session.py`（`Row` 加 `stale`；补 `write()` 协议方法）
- Test: `tests/test_gui_point_table.py`、`tests/test_session.py`（追加 stale 用例）

`stale` 为什么必须补在这一步：Task 3 的 `_render()` 在回读失败时回落 CSV 初值——"没启动所以
显示初值"是对的，但"启动了却没读到"也显示初值，就和"设备真报了这个值"分不出来。一格灰显是
代价最小的区分。

- [ ] **Step 1: 先给 stale 写内核测试（红）**

追加到 `tests/test_session.py`：

```python
def test_rows_are_marked_stale_when_the_running_backend_cannot_answer(session, monkeypatch):
    """通道在跑但后端读不到：值仍显示初值，但必须标 stale。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    runtime = session.channel("tcp-5020")

    def blow_up(*_args, **_kwargs):
        raise OSError("backend gone")

    monkeypatch.setattr(runtime.backend, "read_registers", blow_up)
    rows = session.rows("tcp-5020")
    stale = {r.name for r in rows if r.stale}
    assert "CO浓度" in stale, "寄存器区回读失败没体现在 stale 上"
    assert not any(r.stale for r in rows if r.area == "co"), "位区走另一条回读路径"


def test_rows_are_not_stale_before_the_channel_starts(session):
    session.load(loaded())
    assert not any(r.stale for r in session.rows("tcp-5020")), "没启动不是异常，不该灰显"
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_session.py -q`
Expected: FAIL — `Row` 没有 `stale` 字段 / stale 恒 False。

- [ ] **Step 2: 加字段并实现**

`Row` 末尾加 `stale: bool = False`（有默认值，现有构造点不破）。`_read_back` 改成返回
`(regs, failed)` 二元组：异常或回读 `None` → `failed=True`；通道未运行 → `failed=False`
（那不是异常，是"还没起"）。`rows()`：

```python
        for unit_id, device, pt in meta:
            regs, failed = self._read_back(runtime, pt, unit_id)
            value_text, registers = _render(pt, regs)
            out.append(Row(channel, device, unit_id, pt.name, pt.area, pt.address,
                           pt.datatype, pt.word_order, value_text, registers,
                           pt.readonly, pt.waveform,
                           stale=failed and bool(runtime and runtime.running)))
        return out
```

`set_point` 里那处 `_read_back` 调用同步改成解二元组，`WriteResult` 的字段与消息都不动。
再给 `Session` 加一个 GUI 表模型统一用的方法名：

```python
    def write(self, channel: str, unit_id: int, area: str, address: int,
              text: str) -> tuple[bool, str]:
        res = self.set_point(channel, unit_id, area, address, text)
        return (res.ok, res.error)
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_session.py -q`
Expected: 14 passed。

- [ ] **Step 3: 写点位表测试（红）**

```python
# tests/test_gui_point_table.py
import pytest
from PySide6 import QtCore

from modslave.csv_io_entry import load_point_table
from modslave.engine.point_source import FakeEngine
from modslave.gui.point_table import COLUMNS, PointTableModel, conventional


@pytest.fixture
def engine():
    project, _notes, issues = load_point_table("examples/covi-tunnel.csv")
    assert not [i for i in issues if i.level == "error"]
    return FakeEngine(project)


@pytest.fixture
def model(qapp, engine):
    m = PointTableModel()
    m.attach(engine)
    return m


def row_of(model, name):
    return next(r for r in range(model.rowCount()) if model.index(r, 0).data() == name)


def test_columns_are_the_agreed_order():
    assert [c.header for c in COLUMNS] == [
        "名称", "区", "地址", "4x", "类型", "字序", "当前值", "原始寄存器", "波形", "只读"]


def test_conventional_addresses_match_the_field_convention():
    assert conventional("hr", 0) == "4x00001"
    assert conventional("ir", 4) == "3x00005"
    assert conventional("co", 0) == "0x00001"
    assert conventional("di", 3) == "1x00004"


def test_rows_show_value_and_raw_registers(model):
    r = row_of(model, "CO浓度")
    assert model.index(r, 6).data() == "12.5"
    assert model.index(r, 7).data() == "0000 4148"      # CDAB 上线形态，一眼可核
    assert model.index(r, 3).data() == "4x00001"


def test_only_the_value_column_is_editable(model):
    for col, spec in enumerate(COLUMNS):
        editable = bool(model.flags(model.index(0, col)) & QtCore.Qt.ItemIsEditable)
        assert editable == (spec.header == "当前值"), spec.header


def test_edit_writes_through_the_engine(model, engine):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, 6), "37") is True
    assert model.index(r, 6).data() == "37"
    assert engine.writes == [("tcp-5020", 11, "hr", 4, "37")]


def test_bad_edit_is_refused_and_keeps_the_old_text(model):
    r = row_of(model, "风机状态")
    assert model.setData(model.index(r, 6), "abc") is False
    assert model.index(r, 6).data() == "11"
    assert "int16" in model.last_error


def test_error_message_names_the_point(model):
    r = row_of(model, "风机状态")
    model.setData(model.index(r, 6), "999999")
    assert "风机状态" in model.last_error          # 现场要一眼看出是哪一行


def test_noop_edit_does_not_emit_dataChanged(model):
    r = row_of(model, "风机状态")
    fired: list[int] = []
    model.dataChanged.connect(lambda tl, br, _roles=None: fired.append(tl.row()))
    assert model.setData(model.index(r, 6), "11") is False
    assert fired == []


def test_refresh_touches_only_changed_rows(model, engine):
    """10Hz 刷新不许整表 reset：那会丢选中行与滚动位置。"""
    fired: list[tuple[int, int]] = []
    model.dataChanged.connect(lambda tl, br, _roles=None: fired.append((tl.row(), br.row())))
    engine.values[("tcp-5020", 11, "hr", 4)] = "12"
    changed = model.refresh()
    assert changed == 1
    assert fired == [(row_of(model, "风机状态"), len(COLUMNS) - 1)]


def test_stale_rows_report_their_role(model, engine):
    r = row_of(model, "CO浓度")
    assert model.index(r, 6).data() == "12.5"
    assert model.data(model.index(r, 6), model.stale_role()) is False


def test_empty_project_is_refused_not_blank(model, qapp):
    from modslave.project.model import Project
    m = PointTableModel()
    with pytest.raises(ValueError, match="no channel"):
        m.attach(FakeEngine(Project(channels=[])))
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_point_table.py -q`
Expected: FAIL — `point_source` / `point_table` 不存在。

- [ ] **Step 4: 实现 `point_source.py` 与 `point_table.py`**

`modslave/engine/point_source.py`（Qt-free；表模型只认 `rows()` / `write()`，真实现是
Session、测试用 FakeEngine，这样 GUI 用例不必每次起 socket，而 Session 那条路仍由
`test_session.py` 用真主站覆盖）：

```python
# modslave/engine/point_source.py
"""点位表的数据源协议 + 测试替身。"""

from __future__ import annotations

from typing import Protocol

from ..codec.point_codec import encode
from .session import Row, Session


class PointSource(Protocol):
    def rows(self, channel: str) -> list[Row]: ...

    def write(self, channel: str, unit_id: int, area: str, address: int,
              text: str) -> tuple[bool, str]: ...


def session_source(session: Session) -> PointSource:
    """真 Session 已满足协议；这个函数只是把类型写清楚。"""
    return session


class FakeEngine:
    """内存点位源：写进字典、读回字典；编码校验走真 codec，所以错误路径也是真的。"""

    def __init__(self, project) -> None:
        self.project = project
        self.writes: list[tuple[str, int, str, int, str]] = []
        self.values: dict[tuple[str, int, str, int], str] = {}
        self._rows: list[Row] = []
        for ch in project.channels:
            for dev in ch.devices:
                for pt in dev.points:
                    self._rows.append(Row(ch.name, dev.name, dev.unit_id, pt.name,
                                          pt.area, pt.address, pt.datatype,
                                          pt.word_order, str(pt.initial),
                                          _registers(pt), pt.readonly, pt.waveform))

    def rows(self, channel: str) -> list[Row]:
        out = []
        for row in self._rows:
            if row.channel != channel:
                continue
            text = self.values.get((channel, row.unit_id, row.area, row.address))
            out.append(_with_value(row, text) if text is not None else row)
        return out

    def write(self, channel, unit_id, area, address, text):
        row = next((r for r in self._rows if r.channel == channel
                    and r.unit_id == unit_id and r.area == area
                    and r.address == address), None)
        if row is None:
            return (False, f"{area} address {address} is not in the point table")
        try:
            encode(text, row.datatype, row.word_order)
        except ValueError as exc:
            return (False, f"{row.name}: {exc}")        # 消息带点位名，见测试
        self.writes.append((channel, unit_id, area, address, text))
        self.values[(channel, unit_id, area, address)] = text
        return (True, "")


def _registers(row_or_pt, value=None) -> tuple[int, ...]:
    datatype = getattr(row_or_pt, "datatype", "")
    word_order = getattr(row_or_pt, "word_order", "ABCD")
    text = value if value is not None else str(getattr(row_or_pt, "initial", "0"))
    try:
        return tuple(encode(text, datatype, word_order))
    except ValueError:
        return ()


def _with_value(row: Row, text: str) -> Row:
    return Row(row.channel, row.device, row.unit_id, row.name, row.area, row.address,
               row.datatype, row.word_order, text, _registers(row, text),
               row.readonly, row.waveform, row.stale)
```

`Row` 的第 9 个位置参数是 `registers`；`_registers` 在编码失败时回 `()`——FakeEngine 只用于
测试与预览，真值判定交给 Session。

`modslave/gui/point_table.py`：

```python
# modslave/gui/point_table.py
"""点位表模型。列定义集中一处，视图只管摆。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore


@dataclass(frozen=True)
class Column:
    header: str
    width: int
    numeric: bool = False


COLUMNS: tuple[Column, ...] = (
    Column("名称", 150),
    Column("区", 46),
    Column("地址", 56, numeric=True),
    Column("4x", 82, numeric=True),
    Column("类型", 74),
    Column("字序", 60),
    Column("当前值", 110),
    Column("原始寄存器", 150),
    Column("波形", 96),
    Column("只读", 46),
)

VALUE_COLUMN = next(i for i, c in enumerate(COLUMNS) if c.header == "当前值")
STALE_ROLE = QtCore.Qt.UserRole + 1

_CONVENTION = {"co": "0x", "di": "1x", "ir": "3x", "hr": "4x"}


def conventional(area: str, address: int) -> str:
    """裸 0-based 地址 → 现场惯例写法（厂商点表常用 40001 那一套）。"""
    prefix = _CONVENTION.get(area)
    if prefix is None:
        raise ValueError(f"unknown area {area!r}")
    return f"{prefix}{address + 1:05d}"


class PointTableModel(QtCore.QAbstractTableModel):
    """展示整表，只有"当前值"可编辑。刷新只动真变了的行。

    整表 `beginResetModel()` 会丢选中行和滚动位置（用户口径：状态变化不许动既有可见
    状态），所以日常刷新只按行发 `dataChanged`；只有点表条目数真变了才 reset 一次。
    """

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list = []
        self._source = None
        self._channel = ""
        self.last_error = ""

    def stale_role(self) -> int:
        return STALE_ROLE

    def attach(self, source, channel: str = "") -> None:
        self._source = source
        project = getattr(source, "project", None)
        names = [ch.name for ch in getattr(project, "channels", [])]
        if not names:
            raise ValueError("the project has no channel")
        self._channel = channel or names[0]
        self._rows = list(source.rows(self._channel))
        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role != QtCore.Qt.DisplayRole:
            return None
        return COLUMNS[section].header if orientation == QtCore.Qt.Horizontal else str(section + 1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        spec = COLUMNS[index.column()]
        if role == QtCore.Qt.DisplayRole:
            return _display(row, spec.header)
        if role == QtCore.Qt.TextAlignmentRole and spec.numeric:
            return int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        if role == STALE_ROLE:
            return row.stale
        return None

    def flags(self, index):
        base = super().flags(index) | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
        if index.isValid() and index.column() == VALUE_COLUMN:
            return base | QtCore.Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=QtCore.Qt.EditRole) -> bool:
        if not index.isValid() or role != QtCore.Qt.EditRole or index.column() != VALUE_COLUMN:
            return False
        row = self._rows[index.row()]
        text = str(value).strip()
        if text == row.value_text:
            return False                      # 没变就别惊动视图
        ok, error = self._source.write(row.channel, row.unit_id, row.area,
                                       row.address, text)
        self.last_error = "" if ok else error
        if not ok:
            return False
        self.refresh()
        return True

    def refresh(self) -> int:
        """重读数据源，只给真变了的行发 dataChanged。返回改动行数。"""
        fresh = self._source.rows(self._channel)
        if len(fresh) != len(self._rows):
            self.beginResetModel()
            self._rows = list(fresh)
            self.endResetModel()
            return len(fresh)
        changed = 0
        for i, new in enumerate(fresh):
            if not _differs(self._rows[i], new):
                continue
            self._rows[i] = new
            self.dataChanged.emit(self.index(i, 0),
                                  self.index(i, self.columnCount() - 1))
            changed += 1
        return changed


def _display(row, header: str) -> str:
    return {
        "名称": row.name, "区": row.area, "地址": str(row.address),
        "4x": conventional(row.area, row.address), "类型": row.datatype,
        "字序": row.word_order, "当前值": row.value_text,
        "原始寄存器": " ".join(f"{r:04X}" for r in row.registers),
        "波形": row.waveform, "只读": "是" if row.readonly else "",
    }[header]


def _differs(a, b) -> bool:
    return (a.value_text != b.value_text or a.registers != b.registers
            or a.stale != b.stale or a.waveform != b.waveform)
```

- [ ] **Step 5: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_point_table.py -q`
Expected: 11 passed。`test_only_the_value_column_is_editable` 红的话，是 `flags()` 里
`ItemIsEditable` 的位置判断错——用 `QtCore.Qt.ItemIsEditable`，实现侧不许留魔数。

- [ ] **Step 6: 全量回归 + 提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿，用例数 ≥ 576 + Task 1–5 新增，无 skip、无 xfail。

```bash
git add modslave/engine/point_source.py modslave/gui/point_table.py modslave/engine/session.py \
        tests/test_gui_point_table.py tests/test_session.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 点位表模型，定点刷新与可编辑值列"
```

---

---

## Task 6: 报文视图 `gui/packet_view.py`

**Files:**
- Create: `modslave/gui/packet_view.py`
- Test: `tests/test_gui_packet_view.py`

用户界面口径落地成断言：**四个过滤条件用平铺可勾选标签，不用下拉框**；**默认选中"全部"**；
切过滤器/暂停时按钮文字变化但**几何尺寸不变**（不靠 `setVisible` 增删控件）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gui_packet_view.py
import pytest
from PySide6 import QtCore, QtWidgets

from modslave.backend.events import PacketEvent
from modslave.engine.packets import ALL, EXCEPTIONS, REQUESTS, PacketStore
from modslave.gui.packet_view import PacketPanel


def evt(direction="rx", unit=11, fc=3, mono=0.0):
    return PacketEvent("tcp-5020", direction, bytes([0, 1, unit, fc]), unit, fc,
                       f"unit {unit} fc {fc}", mono)


@pytest.fixture
def store():
    return PacketStore(capacity=50)


@pytest.fixture
def panel(qapp, store):
    w = PacketPanel(store)
    w.show()
    qapp.processEvents()
    yield w
    w.deleteLater()


def test_filter_uses_flat_toggle_labels_not_a_combobox(panel):
    """≤10 个可枚举选项要平铺标签：下拉的选项不点开看不见，切换还要两次点击。"""
    assert panel.filter_combo is None
    labels = panel.filter_labels()
    assert [t.text() for t in labels] == ["全部", "请求", "响应", "异常"]
    assert all(t.isCheckable() for t in labels)
    group = panel.filter_group
    assert group.isExclusive()


def test_default_filter_is_all(panel, store):
    assert panel.filter_group.checkedButton().text() == ALL
    assert store.active_filter == ALL


def test_clicking_a_label_changes_the_visible_rows(panel, store, qapp):
    store.add(evt("rx", 11, 3))
    store.add(evt("tx", 77, 0x83))
    qapp.processEvents()
    assert panel.row_count() == 2
    _click(panel, EXCEPTIONS)
    qapp.processEvents()
    assert panel.row_count() == 1
    assert panel.visible_rows()[0].unit_id == 77
    _click(panel, REQUESTS)
    qapp.processEvents()
    assert [p.unit_id for p in panel.visible_rows()] == [11]


def _click(panel, name: str) -> None:
    button = next(b for b in panel.filter_group.buttons() if b.text() == name)
    button.click()


def test_pausing_stops_rows_from_growing_but_keeps_the_table_readable(panel, store, qapp):
    store.add(evt(mono=1.0))
    qapp.processEvents()
    panel.pause_button.click()
    store.add(evt(mono=2.0))
    qapp.processEvents()
    assert panel.row_count() == 1
    assert panel.pause_button.text() == "继续"      # 文字换、位置不动
    panel.pause_button.click()
    store.add(evt(mono=3.0))
    qapp.processEvents()
    assert panel.row_count() >= 2


def test_geometry_is_stable_across_filter_and_pause(panel, store, qapp):
    """状态变化不许挪布局：宽/高/按钮位置在四种过滤器与暂停两态下都恒定。"""
    store.add(evt())
    qapp.processEvents()
    baseline = panel.geometry_snapshot()
    for name in (REQUESTS, EXCEPTIONS, ALL):
        _click(panel, name)
        qapp.processEvents()
        assert panel.geometry_snapshot() == baseline, f"切到 {name} 后布局变了"
    panel.pause_button.click()
    qapp.processEvents()
    assert panel.geometry_snapshot() == baseline, "暂停后布局变了"
    panel.pause_button.click()
    qapp.processEvents()
    assert panel.geometry_snapshot() == baseline


def test_clear_empties_view_and_store_keeps_working(panel, store, qapp):
    store.add(evt())
    qapp.processEvents()
    panel.clear_button.click()
    qapp.processEvents()
    assert panel.row_count() == 0 and store.rows() == []
    store.add(evt())
    qapp.processEvents()
    assert panel.row_count() == 1


def test_new_packet_arrival_does_not_steal_scroll_when_user_is_reading_bottom_up(
        panel, store, qapp):
    """跟随新行是默认，但用户往上翻过就不硬拽回去（滚动条位置也属于"既有可见状态"）。"""
    for i in range(40):
        store.add(evt(mono=float(i)))
    qapp.processEvents()
    view = panel.table
    bar = view.verticalScrollBar()
    bar.setValue(0)                                  # 人工翻到顶
    user_pos = bar.value()
    store.add(evt(mono=99.0))
    qapp.processEvents()
    assert bar.value() == user_pos, "用户在看旧帧时被拽到底部"


def test_view_refresh_is_throttled_to_one_repaint_per_batch(panel, store, qapp):
    """来一条刷一次太贵；panel 只在 store 通知时打脏标记，由主窗的 10Hz 心跳统一刷。"""
    before = panel.repaint_count
    for i in range(10):
        store.add(evt(mono=float(i)))
    qapp.processEvents()
    assert panel.repaint_count == before, "panel 在 store 通知里直接重绘了"
    panel.refresh_view()
    assert panel.repaint_count == before + 1
```

- [ ] **Step 2: 实现 `packet_view.py`**

```python
# modslave/gui/packet_view.py
"""报文面板：四个平铺过滤标签 + 暂停/清除 + 一列表格。

重绘由主窗的 10Hz 心跳统一驱动（refresh_view），store 通知只打脏标记——
每来一帧就重排一次表格在总线上扫 247 台时会把界面刷成卡顿。
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..engine.packets import ALL, FILTERS, PacketStore

_COLUMNS = ("时间", "方向", "通道", "unit", "fc", "摘要", "原始字节")


class PacketModel(QtCore.QAbstractTableModel):
    def __init__(self, store: PacketStore) -> None:
        super().__init__()
        self._store = store
        self._rows: list = []

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if role != QtCore.Qt.DisplayRole:
            return None
        return _COLUMNS[section] if orientation == QtCore.Qt.Horizontal else str(section + 1)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or role != QtCore.Qt.DisplayRole:
            return None
        event = self._rows[index.row()]
        return (f"{event.mono:.3f}", event.direction.upper(), event.channel,
                "-" if event.unit_id is None else str(event.unit_id),
                "-" if event.function_code is None else f"0x{event.function_code:02X}",
                event.parsed, event.raw.hex(" "))[index.column()]

    def sync(self) -> None:
        fresh = self._store.rows()
        if len(fresh) != len(self._rows):
            self.beginResetModel()
            self._rows = list(fresh)
            self.endResetModel()
            return
        for i, new in enumerate(fresh):
            if new is not self._rows[i]:
                self._rows[i] = new
                self.dataChanged.emit(self.index(i, 0),
                                      self.index(i, self.columnCount() - 1))


class PacketPanel(QtWidgets.QWidget):
    """报文面板。几何恒定：所有状态都只改文字/颜色，不增删控件。"""

    def __init__(self, store: PacketStore, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self.repaint_count = 0
        self._dirty = False
        self._follow_tail = True

        self.model = PacketModel(store)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self.filter_combo = None                      # 明确不用下拉框（见测试）
        labels = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(labels)
        row.setContentsMargins(0, 0, 0, 0)
        self._buttons: list[QtWidgets.QPushButton] = []
        group = QtWidgets.QButtonGroup(labels)
        group.setExclusive(True)
        self.filter_group = group
        for name in FILTERS:
            button = QtWidgets.QPushButton(name)
            button.setCheckable(True)
            button.setChecked(name == ALL)
            button.setFixedWidth(56)                  # 定宽槽：换文字不改宽度
            group.addButton(button)
            self._buttons.append(button)
            row.addWidget(button)
        group.buttonClicked.connect(self._on_filter_clicked)

        row.addStretch(1)
        self.pause_button = QtWidgets.QPushButton("暂停")
        self.pause_button.setCheckable(True)
        self.pause_button.setFixedWidth(64)
        self.pause_button.toggled.connect(self._on_pause_toggled)
        self.clear_button = QtWidgets.QPushButton("清除")
        self.clear_button.setFixedWidth(64)
        self.clear_button.clicked.connect(self._on_clear)
        row.addWidget(self.pause_button)
        row.addWidget(self.clear_button)

        self.count_label = QtWidgets.QLabel("0 帧")
        self.count_label.setFixedWidth(96)
        row.addWidget(self.count_label)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(labels)
        layout.addWidget(self.table, 1)              # 弹性：不给表格写死高度

        store.subscribe(self._mark_dirty)

    # ---------- 对主窗暴露的接口 ----------

    def filter_labels(self) -> list[QtWidgets.QPushButton]:
        return list(self._buttons)

    def row_count(self) -> int:
        return self.model.rowCount()

    def visible_rows(self) -> list:
        return self._store.rows()

    def refresh_view(self) -> int:
        """主窗 10Hz 心跳里调。脏了才同步模型并重算计数。"""
        if not self._dirty:
            return 0
        self._dirty = False
        self.model.sync()
        self.repaint_count += 1
        self.count_label.setText(f"{self.model.rowCount()} 帧")
        if self._follow_tail:
            bar = self.table.verticalScrollBar()
            bar.setValue(bar.maximum())
        return 1

    def geometry_snapshot(self) -> tuple:
        """给布局恒等断言用：面板与三个按钮的几何。"""
        return (self.size().width(), self.size().height(),
                self.pause_button.geometry().getRect(),
                self.clear_button.geometry().getRect(),
                self.table.geometry().getRect(),
                tuple(b.geometry().x() for b in self._buttons))

    def closeEvent(self, event) -> None:              # noqa: N802
        self._store.unsubscribe(self._mark_dirty)
        super().closeEvent(event)

    # ---------- 内部 ----------

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _on_filter_clicked(self, button) -> None:
        self._store.set_filter(button.text())
        self._dirty = True

    def _on_pause_toggled(self, checked: bool) -> None:
        self._store.paused = checked
        self.pause_button.setText("继续" if checked else "暂停")

    def _on_clear(self) -> None:
        self._store.clear()

    def _on_scroll(self, value: int) -> None:
        bar = self.table.verticalScrollBar()
        self._follow_tail = value >= bar.maximum() - 1
```

`PacketStore` 需要一个 `unsubscribe`（面板关窗时退订，否则测试与真实关窗都会留下悬空回调）：
在 `packets.py` 的 `subscribe` 旁补

```python
    def unsubscribe(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)
```

并在 `test_packets.py` 补一条：

```python
def test_unsubscribe_stops_notifications():
    store = PacketStore()
    hits: list[int] = []
    store.subscribe(hits.append)
    store.unsubscribe(hits.append)
    store.add(evt())
    assert hits == []
```

`PacketPanel._on_filter_clicked` 的信号是 `buttonClicked`，参数是被点的按钮；
`_mark_dirty` 只做置位，所以 `store.add` 的循环不会被重绘卡住。

- [ ] **Step 3: 跑报文面板测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_packet_view.py tests/test_packets.py -q`
Expected: 全通过（报文视图 8 条 + 缓冲 13 条）。`test_geometry_is_stable_across_filter_and_pause`
失败时改**布局**（定宽槽、去掉 setVisible 增删），不要把断言放松。

- [ ] **Step 4: 通道面板的失败测试**

`modslave/gui/channel_panel.py` + `tests/test_gui_channel_panel.py`。**为什么必须有这一层**：
spec §11 把"通道启停"列进 M2、§7 画了"通道属性"，只有工具栏"全部启动/全部停止"时，
现场接了一路 485、想单起一个 TCP 通道做不到。

```python
# tests/test_gui_channel_panel.py
import pytest
from PySide6 import QtWidgets

from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import PacketStore
from modslave.engine.runner import EngineRunner
from modslave.engine.session import Session
from modslave.gui.channel_panel import ChannelPanel
from modslave.project.model import TcpChannel
from tests.conftest import free_port


@pytest.fixture
def project():
    loaded, _n, issues = load_point_table("examples/covi-tunnel.csv")
    assert not [i for i in issues if i.level == "error"]
    loaded.channels[0].port = free_port()
    loaded.channels.append(TcpChannel(name="tcp-2", host="127.0.0.1",
                                      port=free_port(), devices=[]))
    return loaded


@pytest.fixture
def built(qapp, project):
    store = PacketStore()
    session = Session(runner=EngineRunner(), packet_store=store)
    panel = ChannelPanel(session)
    panel.rebuild(project)
    holder = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(holder).addWidget(panel)
    holder.resize(680, 160)
    holder.show()
    qapp.processEvents()
    yield panel, session, qapp
    session.shutdown()
    holder.deleteLater()


def by_channel(panel):
    return {row.channel: row for row in panel.rows()}


def test_one_row_per_channel_with_both_buttons(built):
    panel, _session, _qapp = built
    rows = panel.rows()
    assert [r.channel for r in rows] == ["tcp-5020", "tcp-2"]
    for row in rows:
        assert row.start_button.text() == "启动"
        assert row.stop_button.text() == "停止"
        assert row.start_button.isVisible() and row.stop_button.isVisible()


def test_start_is_per_channel(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    assert session.channel("tcp-2").running
    assert not session.channel("tcp-5020").running, "一个通道启动不该带动另一个"


def test_stop_is_per_channel(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    rows["tcp-2"].stop_button.click()
    qapp.processEvents()
    assert not session.channel("tcp-2").running


def test_running_channel_locks_its_address_fields(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    assert not rows["tcp-2"].port_edit.isEnabled(), "跑着还能改端口，改的就不是运行的那个了"
    assert rows["tcp-2"].stop_button.isEnabled()
    rows["tcp-2"].stop_button.click()
    qapp.processEvents()
    assert rows["tcp-2"].port_edit.isEnabled()


def test_geometry_is_stable_in_every_state(built):
    """状态文字换内容/换颜色不许推动行宽（用户口径：状态变化不挪布局）。"""
    panel, _session, qapp = built
    rows = by_channel(panel)
    before = panel.geometry_snapshot()
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    panel.set_status("tcp-2", "running")
    qapp.processEvents()
    assert panel.geometry_snapshot() == before, "启动后通道面板几何变了"
    panel.set_status("tcp-2", "error")
    qapp.processEvents()
    assert panel.geometry_snapshot() == before, "失败后通道面板几何变了"


def test_bad_port_is_refused_and_reported(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].port_edit.setText("COM3")
    assert panel.apply_edits("tcp-2") is None
    assert "port" in panel.last_error
    assert session.project.channels[1].port != "COM3"


def test_valid_port_reaches_the_project(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    port = free_port()
    rows["tcp-2"].port_edit.setText(str(port))
    assert panel.apply_edits("tcp-2") == port
    assert session.project.channels[1].port == port


def test_empty_host_is_refused(built):
    panel, _session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].host_edit.setText("   ")
    assert panel.apply_edits("tcp-2") is None
    assert "host" in panel.last_error


def test_editing_a_running_channel_is_refused(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    rows["tcp-2"].port_edit.setText(str(free_port()))
    assert panel.apply_edits("tcp-2") is None
    assert "running" in panel.last_error
```

- [ ] **Step 5: 实现 `channel_panel.py`**

```python
# modslave/gui/channel_panel.py
"""每个通道一行：host/port 编辑 + 独立启动/停止 + 状态标记。

状态只改文字与颜色：控件不增删、标签定宽，所以几何在任何状态下都一样。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtGui, QtWidgets

from ..project.validate import validate_project

_MARK = {"stopped": "○ 未启动", "running": "● 运行中", "error": "▲ 失败",
         "starting": "◐ 启动中"}
_COLOR = {"stopped": "#666666", "running": "#1a7f37", "error": "#c62828",
          "starting": "#b26a00"}
_PORT_MIN, _PORT_MAX = 1, 65535


@dataclass
class ChannelRow:
    channel: str
    host_edit: QtWidgets.QLineEdit
    port_edit: QtWidgets.QLineEdit
    start_button: QtWidgets.QPushButton
    stop_button: QtWidgets.QPushButton
    status: QtWidgets.QLabel


class ChannelPanel(QtWidgets.QWidget):
    def __init__(self, session, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._rows: dict[str, ChannelRow] = {}
        self._grid = QtWidgets.QGridLayout(self)
        self._grid.setContentsMargins(4, 2, 4, 2)
        self.last_error = ""

    def rebuild(self, project) -> None:
        for old in self._rows.values():
            for widget in (old.host_edit, old.port_edit, old.start_button,
                           old.stop_button, old.status):
                widget.setParent(None)
        self._rows.clear()
        for index, ch in enumerate(project.channels):
            widgets = self._make_row(index, ch)
            self._rows[ch.name] = ChannelRow(ch.name, *widgets[1:])
            self._set_enabled(ch.name, running=False)

    def rows(self) -> list[ChannelRow]:
        return list(self._rows.values())

    def set_status(self, channel: str, state: str) -> None:
        row = self._rows.get(channel)
        if row is None:
            return                     # 状态事件可能比 rebuild 先到
        if state not in _MARK:
            raise ValueError(f"unknown channel state {state!r}")
        row.status.setText(_MARK[state])              # 定宽标签：文字变了也不推挤
        row.status.setStyleSheet(f"color: {_COLOR[state]};")
        self._set_enabled(channel, running=state == "running")

    def apply_edits(self, channel: str) -> int | None:
        """host/port 写回工程。非法值、或通道在跑时返回 None 并留下 last_error。"""
        row = self._rows[channel]
        runtime = self._session.channel(channel)
        if runtime is not None and runtime.running:
            self.last_error = f"{channel} is running; stop it before changing the address"
            return None
        text = row.port_edit.text().strip()
        if not text.isdigit() or not _PORT_MIN <= int(text) <= _PORT_MAX:
            self.last_error = f"{channel}: port {text!r} is not valid"
            return None
        if not row.host_edit.text().strip():
            self.last_error = f"{channel}: host must be a non-empty string"
            return None
        for ch in self._session.project.channels:
            if ch.name == channel:
                ch.host, ch.port = row.host_edit.text(), int(text)
        self.last_error = ""
        self._revalidate()
        return int(text)

    def geometry_snapshot(self) -> tuple:
        return tuple((r.status.geometry().width(), r.start_button.geometry().x(),
                      r.port_edit.geometry().x()) for r in self._rows.values())

    def _make_row(self, index, ch):
        self._grid.addWidget(QtWidgets.QLabel(ch.name), index, 0)
        host = QtWidgets.QLineEdit(ch.host)
        host.setFixedWidth(120)
        port = QtWidgets.QLineEdit(str(ch.port))
        port.setFixedWidth(64)
        port.setValidator(QtGui.QIntValidator(_PORT_MIN, _PORT_MAX, port))
        start = QtWidgets.QPushButton("启动")
        start.setFixedWidth(56)
        stop = QtWidgets.QPushButton("停止")
        stop.setFixedWidth(56)
        status = QtWidgets.QLabel(_MARK["stopped"])
        status.setFixedWidth(96)
        start.clicked.connect(lambda _=False, n=ch.name: self._start(n))
        stop.clicked.connect(lambda _=False, n=ch.name: self._stop(n))
        port.editingFinished.connect(lambda n=ch.name: self.apply_edits(n))
        host.editingFinished.connect(lambda n=ch.name: self.apply_edits(n))
        for column, widget in enumerate((host, port, start, stop, status), start=1):
            self._grid.addWidget(widget, index, column)
        return (QtWidgets.QLabel(ch.name), host, port, start, stop, status)

    def _start(self, channel: str) -> None:
        try:
            self._session.start_channel(channel)
        except ValueError as exc:
            self.last_error = str(exc)
            self._session.publish_log("error", str(exc))

    def _stop(self, channel: str) -> None:
        self._session.stop_channel(channel)

    def _set_enabled(self, channel: str, running: bool) -> None:
        row = self._rows[channel]
        row.start_button.setEnabled(not running)
        row.stop_button.setEnabled(running)
        row.host_edit.setEnabled(not running)        # 跑着改地址 = 改的不是正在跑的那个
        row.port_edit.setEnabled(not running)

    def _revalidate(self) -> None:
        errors = [i for i in validate_project(self._session.project)
                  if i.level == "error"]
        if errors:
            self.last_error = f"{len(errors)} error(s): {errors[0]}"
```

`Session` 补两个方法（`LogLine` 从 `..backend.events` 一起导入）：

```python
    def publish_log(self, level: str, text: str) -> None:
        self._publish(LogLine(level, text))
```

- [ ] **Step 6: 跑通道面板测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_channel_panel.py -q`
Expected: 10 passed。`test_geometry_is_stable_in_every_state` 红的话，是给状态标签留了
自适应宽度——**改回定宽**，不要放松断言。

- [ ] **Step 7: 提交**

```bash
git add modslave/gui/packet_view.py modslave/gui/channel_panel.py modslave/engine/packets.py \
        modslave/engine/session.py tests/test_gui_packet_view.py tests/test_packets.py \
        tests/test_gui_channel_panel.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 报文面板与通道面板，平铺标签、独立启停、几何恒定"
```

---

## Task 7: 主窗组装 `gui/main_window.py` + 设备树 + 对话框

**Files:**
- Create: `modslave/gui/device_tree.py`、`modslave/gui/dialogs.py`、`modslave/gui/main_window.py`
- Test: `tests/test_gui_main_window.py`、`tests/test_gui_layout_invariant.py`

- [ ] **Step 1: 设备树与对话框**

`modslave/gui/device_tree.py`：

```python
# modslave/gui/device_tree.py
"""通道→设备两级树。状态只改前景色，不改文字长度（几何恒定）。"""

from __future__ import annotations

from PySide6 import QtGui, QtCore, QtWidgets

_STATUS_MARK = {"stopped": "○", "running": "●", "error": "▲", "starting": "◐"}
_STATUS_COLOR = {"stopped": QtGui.QColor("#666666"),
                 "running": QtGui.QColor("#1a7f37"),
                 "error": QtGui.QColor("#c62828"),
                 "starting": QtGui.QColor("#b26a00")}


class DeviceTree(QtWidgets.QTreeWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["通道 / 设备", "状态"])
        self.setColumnCount(2)
        self.setColumnWidth(0, 240)                  # 定宽，避免状态变化推挤
        self.setColumnWidth(1, 72)
        self._items: dict[str, QtWidgets.QTreeWidgetItem] = {}

    def rebuild(self, project) -> None:
        self._items.clear()
        self.clear()
        for ch in project.channels:
            top = QtWidgets.QTreeWidgetItem([ch.name, _STATUS_MARK["stopped"]])
            top.setData(0, QtCore.Qt.UserRole, ("channel", ch.name))
            top.setExpanded(True)
            self.addTopLevelItem(top)
            self._items[ch.name] = top
            for dev in ch.devices:
                child = QtWidgets.QTreeWidgetItem([f"  {dev.name} (unit {dev.unit_id})", ""])
                child.setData(0, QtCore.Qt.UserRole, ("device", ch.name, dev.unit_id))
                top.addChild(child)

    def set_status(self, channel: str, state: str) -> None:
        """未知通道直接返回：状态事件可能比 rebuild 先到（启动瞬间）。"""
        item = self._items.get(channel)
        if item is None:
            return
        if state not in _STATUS_MARK:
            raise ValueError(f"unknown channel state {state!r}")
        item.setText(1, _STATUS_MARK[state])         # 单元格宽度固定
        brush = QtGui.QBrush(_STATUS_COLOR[state])
        item.setForeground(0, brush)
        item.setForeground(1, brush)
        if state == "error":
            item.setToolTip(0, "启动失败，见日志页")
```

`modslave/gui/dialogs.py`：

```python
# modslave/gui/dialogs.py
"""校验错误清单 + CSV 导入对话框。错误必须一次看全，不许弹窗逐个点。"""

from __future__ import annotations

from PySide6 import QtWidgets

from ..project.csv_io import HEADER, read_csv_file, write_csv_file
from ..project.json_io import load as load_project, save as save_project
from ..project.validate import validate_project


def format_issues(issues) -> list[str]:
    return [str(i) for i in issues]


class IssueDialog(QtWidgets.QDialog):
    """一条错误都不少地列出来；warnings 与 errors 分段，避免真错被淹没。"""

    def __init__(self, issues, parent=None) -> None:
        super().__init__(parent)
        errors = [i for i in issues if i.level == "error"]
        warnings = [i for i in issues if i.level == "warning"]
        self.setWindowTitle(f"点表检查：{len(errors)} 错误 / {len(warnings)} 警告")
        self.resize(760, 420)
        box = QtWidgets.QVBoxLayout(self)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        text.setPlainText("\n".join(format_issues(errors) +
                                    [f"(warning) {i}" for i in warnings]))
        box.addWidget(text, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        box.addWidget(buttons)


def import_csv(parent=None):
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent, "导入 CSV 点表", "", "CSV (*.csv);;所有文件 (*)")
    return path or None


def choose_offset(parent=None) -> int:
    """40001 风格点表的整体偏移。默认 0：本工具的地址一律 0-based 裸地址。"""
    value, ok = QtWidgets.QInputDialog.getInt(
        parent, "地址偏移", "点表地址是 4x/3x 惯例写法时填偏移量\n"
                            "（例如 40001 风格填 -40001，让它变成 0-based）",
        0, -65535, 65535, 1)
    return value if ok else 0


def open_project(parent=None):
    """返回 (project | None, path | None)。路径要一路带回去，理由见计划正文。"""
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent, "打开工程", "", "工程 (*.json *.csv)")
    if not path:
        return None, None
    if path.lower().endswith(".json"):
        return load_project(path), path
    project, _notes = read_csv_file(path, with_notes=True)
    return project, path


def save_project_as(parent, project, suggested: str = ""):
    """返回落盘路径或 None（用户取消）。路径回传给主窗记住。"""
    path, _ = QtWidgets.QFileDialog.getSaveFileName(parent, "保存工程", suggested,
                                                    "JSON (*.json);;CSV (*.csv)")
    if not path:
        return None
    if path.lower().endswith(".json"):
        save_project(project, path)
    else:
        write_csv_file(project, path)
    return path
```

- [ ] **Step 2: 主窗**

`modslave/gui/main_window.py`：

```python
# modslave/gui/main_window.py
"""主窗：工具栏 + 左设备树 + 中点位表 + 下标签页（报文/日志） + 状态栏。

刷新是拉的：10Hz 心跳调 point_model.refresh() 与 packet_panel.refresh_view()，
实测 200 行一次 dataChanged 只要 0.34ms，所以不需要增量 diff。
"""

from __future__ import annotations

import pathlib

from PySide6 import QtCore, QtGui, QtWidgets

from ..engine.packets import PacketStore
from ..engine.point_source import session_source
from ..engine.session import Session
from ..project.csv_io import write_csv_file
from ..project.json_io import save as save_project
from ..project.validate import error_lines
from .bridge import EngineBridge
from .channel_panel import ChannelPanel
from .device_tree import DeviceTree
from .dialogs import (
    IssueDialog, choose_offset, import_csv, open_project, save_project_as,
)
from .packet_view import PacketPanel
from .point_table import PointTableModel

_TICK_MS = 100


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, session: Session, store: PacketStore,
                 project=None) -> None:
        super().__init__()
        self.session = session
        self.store = store
        self.bridge = EngineBridge()
        self.point_model = PointTableModel()
        self.tree = DeviceTree()
        self.channels: ChannelPanel | None = None
        self.packet_panel = PacketPanel(store)
        self.log_view = QtWidgets.QPlainTextEdit()
        self._path: pathlib.Path | None = None
        self._confirming = False
        self._dirty_shown = False

        self.setCentralWidget(self._build_layout())
        self._build_toolbar()
        self.status = self.statusBar()
        self.status.showMessage("未启动")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)

        self.tabs = self.findChild(QtWidgets.QTabWidget, "bottomTabs")
        self.tabs.addTab(self.packet_panel, "报文监视")
        self.tabs.addTab(self.log_view, "日志")

        if project is not None:
            self.use_project(project)

        self.bridge.channel_state.connect(self._on_state)
        self.bridge.logged.connect(self._on_log)
        self.bridge.point_changed.connect(lambda _e: self.point_model.refresh())
        self.session.subscribe(self.bridge.forward)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(_TICK_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    # ---------- 组装 ----------

    def _build_layout(self) -> QtWidgets.QWidget:
        host = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(host)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        top = QtWidgets.QWidget()
        rows = QtWidgets.QVBoxLayout(top)
        rows.setContentsMargins(0, 0, 0, 0)
        self.channels = ChannelPanel(self.session)     # 第一行：通道属性 + 独立启停
        rows.addWidget(self.channels)

        cols = QtWidgets.QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.point_model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setWordWrap(False)
        cols.addWidget(self.tree, 1)
        cols.addWidget(self.table, 4)
        rows.addLayout(cols, 1)

        bottom = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("bottomTabs")
        bl.addWidget(self.tabs)

        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, False)             # 上栏不许被压没（兄弟面板挤掉是 bug）
        splitter.setCollapsible(1, False)
        grid.addWidget(splitter)
        return host

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("main")
        bar.setMovable(False)
        self.act_start = bar.addAction("全部启动", self._start_all)
        self.act_stop = bar.addAction("全部停止", self._stop_all)
        bar.addSeparator()
        bar.addAction("导入CSV", self._import_csv)
        bar.addAction("导出CSV", self._export_csv)
        bar.addAction("打开工程", self._open_project)
        bar.addAction("保存工程", self._save_project)
        bar.addSeparator()
        bar.addAction("检查点表", self._check)

    # ---------- 工程 ----------

    def use_project(self, project, path=None) -> list:
        issues = self.session.load(project)
        self.tree.rebuild(project)
        self.point_model.attach(session_source(self.session))
        self.channels.rebuild(project)
        self.store.clear()
        self._path = pathlib.Path(path) if path else None
        self._dirty_shown = self.session.dirty
        self._refresh_title()
        self._show_issues(issues)
        return issues

    def _refresh_title(self) -> None:
        name = self._path.name if self._path else "未命名工程"
        self.setWindowTitle(f"modslave - {name}{' *' if self.session.dirty else ''}")

    # ---------- 动作 ----------

    def _start_all(self) -> None:
        for message in self.session.start_all():
            self._log(f"[error] {message}")

    def _stop_all(self) -> None:
        self.session.stop_all()

    def _check(self) -> None:
        self._show_issues(self.session.project and _validate(self.session.project))

    def _show_issues(self, issues) -> bool:
        errors = error_lines(issues)
        if not issues:
            self.status.showMessage("点表检查通过", 5000)
            return True
        if errors:
            self.status.showMessage(f"{len(errors)} 个错误，启动已被阻止")
        else:
            self.status.showMessage(f"{len(issues)} 个警告")
        IssueDialog(issues, self).exec()
        return False

    def _import_csv(self) -> None:
        path = import_csv(self)
        if not path:
            return
        offset = choose_offset(self)
        try:
            from ..project.csv_io import read_csv_file
            project, _notes = read_csv_file(path, with_notes=True,
                                            ports=_ports_from(path),
                                            address_offset=offset)
        except ValueError as exc:
            self._show_issues([_parse_issue(path, exc)])
            return
        self.use_project(project, path)

    def _export_csv(self) -> None:
        if self._path is None:
            self._log("[error] 没有工程路径，请先打开或导入一份点表")
            return
        write_csv_file(self.session.project, self._path.with_suffix(".csv"))
        self._log(f"[info] 已导出 CSV -> {self._path.with_suffix('.csv')}")

    def _open_project(self) -> None:
        project, path = _open_with_path(self)
        if project is None:
            return
        self.use_project(project, path)

    def _save_project(self) -> str | None:
        path = save_project_as(self, self.session.project,
                               str(self._path) if self._path else "")
        if path:
            self._path = pathlib.Path(path)
            self.session.clear_dirty()
        self._refresh_title()
        return path

    # ---------- 心跳与事件 ----------

    def _tick(self) -> None:
        self.point_model.refresh()
        self.packet_panel.refresh_view()
        dirty = self.session.dirty
        if dirty != self._dirty_shown:            # 标题只在真变化时改，避免每跳重画标题
            self._dirty_shown = dirty
            self._refresh_title()

    def _on_state(self, state) -> None:
        self.tree.set_status(state.channel, state.state)
        self.channels.set_status(state.channel, state.state)
        self._log(f"[{state.state}] {state.channel} {state.detail}".rstrip())
        self.status.showMessage(
            "；".join(f"{n}:{r.state}" for n, r in _runtimes(self.session)) or "未启动")

    def _on_log(self, line) -> None:
        self._log(f"[{line.level}] {line.text}")

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def closeEvent(self, event) -> None:                # noqa: N802
        if self.session.dirty and not self._confirming:
            answer = QtWidgets.QMessageBox.question(
                self, "有未保存的改动",
                "点表有未保存的改动（标题栏有 *）。确定要退出吗？改动不会写盘。",
                QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Save)
            if answer == QtWidgets.QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QtWidgets.QMessageBox.Save:
                self._confirming = True
                self._save_project()
                self._confirming = False
        self.timer.stop()
        self.session.shutdown()
        event.accept()


def _validate(project):
    return validate_project(project)


def _runtimes(session):
    return [(name, session.channel(name)) for name in session.channel_names()]


def _parse_issue(path, exc):
    from ..project.validate import Issue
    return Issue("error", "parse", pathlib.Path(path).name, str(exc))


def _ports_from(path):
    """导入时不猜端口：端口属于工程，属于点表列以外的东西。"""
    return {}


def _open_with_path(parent):
    return open_project(parent)          # dialogs.open_project 返回 (project, path)
```

上面两处必须按下列口径落地，**不许自选**：

- `Session` 需要 `clear_dirty()`（保存成功后清 `*`）：在 `session.py` 加
  `def clear_dirty(self) -> None:\n        with self._lock:\n            self._dirty = False`，
  并在 `tests/test_session.py` 补一条 `test_clear_dirty_resets_the_flag`。
- `dialogs.open_project()` 返回 `(project | None, path | None)` 二元组——主窗要把路径记下来
  才能判断"另存为"和脏标记该不该清。只返回 project 的话路径就得挂到某个对象的野生属性上传，
  下一个改对话框的人一定漏。`dialogs.save_project_as()` 同样返回 `(path | None)`。
- `_check` 里 `self.session.project and _validate(...)` 是无意义写法，直接
  `self._show_issues(validate_project(self.session.project))`。

- [ ] **Step 3: 主窗测试（含布局恒等）**

`tests/test_gui_main_window.py`：

```python
# tests/test_gui_main_window.py
import pytest
from PySide6 import QtWidgets
from pymodbus.client import ModbusTcpClient

from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import PacketStore
from modslave.engine.runner import EngineRunner
from modslave.engine.session import Session
from modslave.gui.main_window import MainWindow
from tests.conftest import free_port


@pytest.fixture
def window(qapp):
    project, _notes, issues = load_point_table("examples/covi-tunnel.csv")
    assert not [i for i in issues if i.level == "error"]
    store = PacketStore()
    w = MainWindow(Session(runner=EngineRunner(), packet_store=store), store, project)
    w.resize(1200, 700)
    w.show()
    qapp.processEvents()
    yield w
    w.timer.stop()
    w.session.shutdown()
    w.deleteLater()


def _row(window, name):
    model = window.point_model
    return next(r for r in range(model.rowCount()) if model.index(r, 0).data() == name)


def test_title_marks_dirty_after_a_write(window, qapp):
    assert "*" not in window.windowTitle()
    window.table.model().setData(window.point_model.index(_row(window, "风机状态"), 6), "21")
    qapp.processEvents()
    assert window.windowTitle().endswith("*")


def test_start_then_stop_flips_the_tree_without_moving_it(window, qapp):
    project = window.session.project
    project.channels[0].port = free_port()
    before = window.tree.columnWidth(0), window.tree.size().width()
    window._start_all()
    qapp.processEvents()
    assert window.session.channel("tcp-5020").running
    window._stop_all()
    qapp.processEvents()
    assert not window.session.channel("tcp-5020").running
    assert (window.tree.columnWidth(0), window.tree.size().width()) == before


def test_value_written_in_gui_reaches_a_real_master(window, qapp):
    window.session.project.channels[0].port = free_port()
    window.use_project(window.session.project)
    window._start_all()
    qapp.processEvents()
    runtime = window.session.channel("tcp-5020")
    assert runtime.running
    assert window.point_model.setData(
        window.point_model.index(_row(window, "风机状态"), 6), "33") is True
    client = ModbusTcpClient("127.0.0.1", port=runtime_backend_port(runtime))
    assert client.connect()
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [33]
    finally:
        client.close()


def runtime_backend_port(runtime) -> int:
    host, _, port = runtime.detail.rpartition(":")
    return int(port)


def test_master_write_shows_up_in_the_table_within_two_ticks(window, qapp):
    window.session.project.channels[0].port = free_port()
    window.use_project(window.session.project)
    window._start_all()
    qapp.processEvents()
    runtime = window.session.channel("tcp-5020")
    client = ModbusTcpClient("127.0.0.1", port=runtime_backend_port(runtime))
    assert client.connect()
    try:
        client.write_register(4, 77, device_id=11)
    finally:
        client.close()
    for _ in range(40):                      # 最多 4s，正常应两跳内到位
        qapp.processEvents()
        window._tick()
        if window.point_model.index(_row(window, "风机状态"), 6).data() == "77":
            return
    pytest.fail("主站写入没能在 10Hz 心跳内反映到表里")


def test_packets_appear_in_the_panel(window, qapp):
    window.session.project.channels[0].port = free_port()
    window.use_project(window.session.project)
    window._start_all()
    qapp.processEvents()
    client = ModbusTcpClient("127.0.0.1",
                             port=runtime_backend_port(window.session.channel("tcp-5020")))
    assert client.connect()
    client.read_holding_registers(0, count=1, device_id=11)
    client.close()
    for _ in range(40):
        qapp.processEvents()
        window._tick()
        if window.packet_panel.row_count() >= 2:
            return
        qt_sleep(0.05)
    pytest.fail("报文面板没收到帧")


def qt_sleep(seconds: float) -> None:
    from PySide6 import QtCore
    QtCore.QThread.msleep(int(seconds * 1000))


def test_bad_edit_keeps_old_value_and_logs_the_reason(window, qapp):
    before = window.point_model.index(_row(window, "风机状态"), 6).data()
    assert window.point_model.setData(
        window.point_model.index(_row(window, "风机状态"), 6), "not-a-number") is False
    assert window.point_model.index(_row(window, "风机状态"), 6).data() == before
    assert "int16" in window.point_model.last_error


def test_channels_are_independent_when_two_exist(window, qapp):
    from modslave.project.model import TcpChannel
    project = window.session.project
    second = TcpChannel(name="tcp-2", host="127.0.0.1", port=free_port(), devices=[])
    project.channels.append(second)
    issues = window.use_project(project)
    assert not [i for i in issues if i.level == "error"]
    window._start_all()
    qapp.processEvents()
    assert window.session.channel("tcp-5020").running
    window.session.stop_channel("tcp-5020")
    assert window.session.channel("tcp-5020").running is False
    assert window.session.channel("tcp-2").running is False   # 第二个端口空着也起不来？见 Step 4
```

`tests/test_gui_layout_invariant.py`（用户的界面硬要求，单独成文以便失败时一眼看出是谁的锅）：

```python
# tests/test_gui_layout_invariant.py
import pytest
from PySide6 import QtWidgets

from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import PacketStore
from modslave.engine.runner import EngineRunner
from modslave.engine.session import Session
from modslave.gui.main_window import MainWindow
from tests.conftest import free_port


@pytest.fixture
def window(qapp):
    project, _n, issues = load_point_table("examples/covi-tunnel.csv")
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = free_port()
    store = PacketStore()
    w = MainWindow(Session(runner=EngineRunner(), packet_store=store), store, project)
    w.resize(1100, 680)
    w.show()
    qapp.processEvents()
    yield w
    w.timer.stop()
    w.session.shutdown()
    w.deleteLater()


def snapshot(window):
    return {
        "window": window.size().getRect(),
        "tree_col0": window.tree.columnWidth(0),
        "tree_width": window.tree.size().width(),
        "table_col_widths": [window.table.columnWidth(i)
                             for i in range(window.point_model.columnCount())],
        "tabs_geometry": window.tabs.geometry().getRect(),
        "packet_panel": window.packet_panel.geometry_snapshot(),
        "status_height": window.status.size().height(),
        "toolbar_height": window.toolBar().geometry().height(),
    }


def test_three_states_keep_the_layout_identical(window, qapp):
    """stopped → running → error 三态下所有几何必须一模一样。

    现场操作员靠元素位置和邻居是否还在判断系统是否响应，界面一跳他就分不清
    是系统没响应还是自己没点到。
    """
    base = snapshot(window)
    window._start_all()
    qapp.processEvents()
    window._tick()
    assert snapshot(window) == base, "启动后布局挪了"
    window.session.project.channels[0].port = 1        # 下一个端口必然占不到
    window.use_project(window.session.project)
    qapp.processEvents()
    window._start_all()
    qapp.processEvents()
    assert snapshot(window) == base, "启动失败后布局挪了"


def test_no_control_is_hidden_on_state_change(window, qapp):
    """既有可见元素不许消失：按钮、标签、表格列在四态下都还在。"""
    def visible():
        return ([b.isVisible() for b in window.packet_panel.filter_labels()],
                window.packet_panel.pause_button.isVisible(),
                window.packet_panel.clear_button.isVisible(),
                window.table.isVisible(), window.tree.isVisible())
    before = visible()
    window._start_all()
    qapp.processEvents()
    window._stop_all()
    qapp.processEvents()
    assert visible() == before
```

- [ ] **Step 4: 跑测试并按失败原因修**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_main_window.py tests/test_gui_layout_invariant.py -q`
Expected: 逐条修到全绿。**已预判的两处**，按下面处理，不要自己发明：

- `test_channels_are_independent_when_two_exist` 里 `modslave.project.model.Channel` **不存在**，
  M1 里的类叫 `TcpChannel`。改测试用 `TcpChannel`。
- `test_three_states_keep_the_layout_identical` 造"启动失败"用的是端口 1。若 `use_project()`
  之后 `_start_all()` 不报错而是抛异常，说明 `start_all()` 把 `ValueError` 收集成清单而不是
  外抛——这是 M1 定的行为（返回 failures 列表），保持，别改成抛。
- `status_height` 若因消息文字长度变化而抖动，给状态栏消息标签设
  `setMinimumWidth`（定宽槽），**不要**把断言改成"允许 2px 误差"。

- [ ] **Step 5: 全量回归 + 提交**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全绿，无 skip、无 xfail。

```bash
git add modslave/gui tests/test_gui_main_window.py tests/test_gui_layout_invariant.py \
        modslave/engine/session.py tests/test_session.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 主窗组装、设备树与对话框，附布局恒等断言"
```

---

## Task 8: GUI 入口、冒烟与人工验收

**Files:**
- Create: `modslave/gui/app.py`（`main()`）
- Modify: `modslave/__main__.py`（加 `--gui`）
- Test: `tests/test_cli.py` 追加

- [ ] **Step 1: `gui/app.py`**

```python
# modslave/gui/app.py
"""GUI 入口。工程装载复用 CLI 的 load_point_table，错误先弹清单再决定是否启动。"""

from __future__ import annotations

import sys

from PySide6 import QtWidgets

from ..csv_io_entry import load_point_table
from ..engine.packets import PacketStore
from ..engine.runner import EngineRunner
from ..engine.session import Session
from ..project.validate import error_lines
from .main_window import MainWindow


def build_app(argv: list[str] | None = None):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or [])
    store = PacketStore()
    session = Session(runner=EngineRunner(), packet_store=store)
    window = MainWindow(session, store)
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        project, notes, issues = load_point_table(args[0])
        window.use_project(project, args[0])
        for note in notes:
            window._log(f"[note] {note}")
        if error_lines(issues):
            window.status.showMessage(f"{len(error_lines(issues))} 个错误，启动已被阻止")
    window.show()
    return app, window, session
```

- [ ] **Step 2: `__main__.py` 加 `--gui`（不改 CLI 行为）**

在 `main()` 里 `args = parser.parse_args(argv)` 之后插入：

```python
    if args.gui:
        from .gui.app import build_app
        app, window, session = build_app([args.point_table] if args.point_table else [])
        try:
            return app.exec()
        except KeyboardInterrupt:
            session.shutdown()
            return 0
```

并在 argparse 里加：

```python
    parser.add_argument("--gui", action="store_true",
                        help="打开桌面界面而不是命令行监听")
```

`point_table` 参数改成 `nargs="?"`，同时保留一条校验：**不带 `--gui` 且没给点表路径**时
`parser.error("point table is required")`。`tests/test_cli.py` 必须仍全绿。

- [ ] **Step 3: 测试**

追加到 `tests/test_cli.py`：

```python
def test_gui_flag_opens_a_window_and_still_validates(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "channel,device,unit_id,name,area,address,datatype,word_order,initial,"
        "waveform,wave_param,readonly\n"
        "tcp-1,D,300,x,hr,0,uint16,ABCD,0,const,,false\n", encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys; from modslave.gui.app import build_app;"
         "app, w, s = build_app(sys.argv[1:]);"
         "assert not w.session.channel('tcp-1').running; print('GUI_OK')", str(bad)],
        capture_output=True, text=True, encoding="utf-8", env=env)
    assert done.returncode == 0, done.stderr
    assert "GUI_OK" in done.stdout


def test_gui_flag_rejects_a_point_table_with_errors(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "channel,device,unit_id,name,area,address,datatype,word_order,initial,"
        "waveform,wave_param,readonly\n"
        "tcp-1,D,11,x,hr,0,uint16,ABCD,0,sine,base=0,,false\n", encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run(
        [sys.executable, "-m", "modslave", str(bad), "--gui"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    assert "error" in (done.stdout + done.stderr).lower() or done.returncode != 0


def test_cli_headless_path_is_unchanged(tmp_path):
    """加 --gui 之后，原来的无界面路径必须一字不变（现场 CI 还要用）。"""
    done = subprocess.run([sys.executable, "-m", "modslave", EXAMPLE_CSV, "--check"],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    assert "valid" in done.stdout
```

（`EXAMPLE_CSV` 与 `os` 按 `tests/test_cli.py` 现有顶部定义补齐；若 `os` 没导入，加上。）

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q`
Expected: 全绿（M1 的 10 条 + 3 条）。

- [ ] **Step 4: 提交**

```bash
git add modslave/gui/app.py modslave/__main__.py tests/test_cli.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: GUI 入口与 --gui 开关"
```

---

## Task 9: 冒烟与人工验收清单（需要你亲手做一次）

- [ ] **Step 1: offscreen 冒烟（脚本级，可重复）**

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -c "
from modslave.gui.app import build_app
app, w, s = build_app(['examples/covi-tunnel.csv'])
print('rows', w.point_model.rowCount(), 'cols', w.point_model.columnCount())
w._start_all(); app.processEvents()
print('running', [n for n in s.channel_names() if s.channel(n).running])
print('tree', [w.tree.topLevelItem(i).text(0) for i in range(w.tree.topLevelItemCount())])
s.shutdown()
"
```
Expected: 打印行数列数、`running ['tcp-5020']`、树里有 `tcp-5020`。

- [ ] **Step 2: 真界面冒烟（你要做的部分，一）**

```bash
.venv/Scripts/python.exe -m modslave examples/covi-tunnel.csv --gui
```

逐项确认（这是 M2 的验收面，不是走过场）：

1. 左树两行通道/设备、中文点名显示不乱码（乱码 → `brief`/字体问题，不是渲染问题）；
2. 选中 `CO浓度` 那行的"当前值"改成 `44.5` → 立刻回读显示 `44.5`，
   "原始寄存器"变成 `0000 4230`（CDAB）；
3. 把 `风机状态` 改成 `abc` → 值不变，状态栏/日志里能看到带点位名的原因；
4. 点"全部启动" → 树状态列从 `○` 变 `●`，**其余一切位置不动**；
5. 下面"报文监视"里能看到 `RX/TX` 行，切"响应/异常/全部"标签，行数变化但面板不跳；
6. 点"全部停止" → `○`，标题栏有 `*`（因为改过值），关闭时问一次保存/丢弃/取消。

- [ ] **Step 3: 真界面 + 真主站（你要做的部分，二）**

```bash
.venv/Scripts/python.exe -m modslave examples/covi-tunnel.csv --gui
```
另开一个进程当主站：

```bash
.venv/Scripts/python.exe -c "
from pymodbus.client import ModbusTcpClient as C
c = C('127.0.0.1', port=5020); assert c.connect()
print('hr0..1', c.read_holding_registers(0, count=2, device_id=11).registers)
print('写 hr4', c.write_register(4, 88, device_id=11).registers)
print('越界', c.read_holding_registers(300, count=1, device_id=11).exception_code)
print('未知 unit', c.read_holding_registers(0, count=1, device_id=77).exception_code)
c.close()
```
Expected: 界面上"风机状态"两跳内变成 `88`；报文面板同时出现对应 RX/TX 行；
越界与未知 unit 各回异常码 2 / 4。

- [ ] **Step 4: 文档回写**

改 `docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md`：

- §7 界面：把"四个平铺过滤标签、默认全部、几何恒定"写成已实现口径（原来只画了布局图）；
- §12 的 M2 部分补"已实测"三条：跨线程 signal 200/200 且落主线程、
  `dataChanged` 批量刷新 200 行 0.34ms、offscreen `show()` 后几何真实可用于断言；
- §11：M2 标已完成（写 commit 与用例数）。

改 `docs/superpowers/plans/2026-10-08-modslave-m2-gui.md`：把被评审改过的
Step 3/4 代码块前加"裁决前快照，以仓库为准"提示（沿用 M1 计划的措辞）。

- [ ] **Step 5: 提交**

```bash
git add docs
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "docs: 回写 M2 实测结论与界面口径"
```

---

## M2 完成检查表

- [ ] `.venv/Scripts/python.exe -m pytest -q` 全绿，无 skip / xfail（用例数只增不减，基线 576）
- [ ] `tests/test_architecture.py` 通过：pymodbus/serial 只在 `backend/`，**PySide6 只在 `gui/`**，
      且每条守卫都有"防空跑"断言；变异检查（往 `gui/` 外塞 `import PySide6`）确实变红
- [ ] `tests/test_gui_layout_invariant.py` 通过：stopped/running/error 三态几何完全一致，
      且没有任何控件在状态变化时消失
- [ ] 报文过滤是 4 个平铺标签（无下拉），默认"全部"
- [ ] GUI 改值经真主站读回一致；非法输入被拒且消息带点位名
- [ ] 主站写入在 2 个心跳（≤200ms）内出现在表里
- [ ] 未保存改动有 `*` 且关闭时提示；`--check`/无头 CLI 行为未退化
- [ ] Task 9 Step 2、Step 3 的人工清单由用户当面跑过（自动化替不了）
- [ ] `git status --short` 为空

## M2 不做（留给后续计划）

| 内容 | 归属 | 为什么不在这里 |
|---|---|---|
| 波形引擎（`const/ramp/sine/random/step` 真跑起来）与 `source` 值归属 | M4 | 需要 tick 计算与"手动/主站写入接管波形"的规则，纯逻辑可单测，先单独出计划 |
| RTU 串口通道 | M5 | 需要 USB-485 人工冒烟 |
| 点位属性面板（改地址/类型/字序） | M4 一并做 | α 口径：点表列编辑要联动重校验，与波形参数编辑一起做更省 |
| 报文导出/录制 | v2 之外 | 明确不做，接口留 `PacketStore` |

