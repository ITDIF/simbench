"""Session：GUI 唯一可对话的对象。持有工程、通道状态、点表行；所有动作都在此收口。

Qt-free（不 import PySide6），所以 M1/M2 的内核测试不需要事件循环。
所有对 pymodbus 的操作都通过 backend + runner.submit 走引擎线程，Session 自己不碰 loop。

刷新是**拉模型**：GUI 以 10Hz 调 `rows(channel)`，`rows()` 在通道运行时回读寄存器、
未运行时用 CSV 初值。这里刻意不做任何"主站写入"回调、也不让引擎轮询——M1 的 backend
只有 `packet_sink` / `on_connection` 两个钩子，多一个钩子就多一条跨线程改 UI 的路，
而 10Hz 全表刷新实测最坏 0.34ms，不值得为它换推送。

M4 的值归属沿同一个口径：`Row.source` 是 `rows()` 现问 backend 的
（`source_is_waveform`，无 await 的纯内存读，和 `_read_back` 同类），而**写侧**的接管与
恢复全部经 `runner.call(...)` 投回 loop 线程——运行态只有一条线程碰（spec §13）。
"主站写了什么"由 backend 在 rx 钩子里自己记账，Session 不接回调，界面上才有唯一一条
拉取路径。

回读失败时那一格**仍然**显示 CSV 初值，但会带上 `Row.stale`：不带着它，"通道在跑却没
读到"和"设备报了这个值"在界面上就是同一个样子（本项目最防的那类演出）；而"还没启动"
不是异常，不标灰。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .. import swallow
from ..backend import BackendStartError, make_backend
from ..events import ChannelState, LogLine, PacketEvent
from ..project.model import Point, Project, channel_transport
from ..project.validate import Issue, error_lines, validate_project
from .packets import PacketStore
from .reads import ReadMixin
from .records import (
    _START_TIMEOUT,
    _STOP_TIMEOUT,
    ChannelRuntime,
    Row,
    WriteResult,
)
from .render import _detail
from .runner import EngineRunner
from .writes import WriteMixin

__all__ = ["ChannelRuntime", "Row", "Session", "WriteResult"]

class Session(ReadMixin, WriteMixin):
    def __init__(self, runner: EngineRunner | None = None,
                 packet_store: PacketStore | None = None) -> None:
        self._runner = runner or EngineRunner()
        # 谁建的 runner 谁负责收：外面传进来的可能同时给几个 Session 用，
        # shutdown() 替别人停线程会让另一个 Session 的下一发 submit 直接抛。
        self._owns_runner = runner is None
        if not self._runner.alive:
            self._runner.start()
        self._sinks: list[Callable[[object], None]] = []
        self._lock = threading.RLock()
        self._project = Project(channels=[])
        self._source: str | None = None
        self._channels: dict[str, ChannelRuntime] = {}
        # 内层键是**(区, 地址, 位)**：S7 同字节上的两个 bool 是两个点，不带位会互相覆盖
        self._points: dict[tuple[str, int], dict[tuple[str, int, int | None], Point]] = {}
        self._rows_meta: dict[str, list[tuple[int, str, Point]]] = {}
        self._dirty = False
        # `_render` 的记忆化（见 `_render_cached`）：键里带渲染相关的全部输入，值里带
        # "这份文案是按哪一串寄存器算出来的"。`_rebuild_index_locked` 会清空它。
        self._render_cache: dict[tuple, tuple] = {}
        # "整批回读超时"那条错只报一次（每 100ms 一条能把日志窗刷干净，而"表全是灰的"本身
        # 就是看得见的信号）。见 `_snapshot_for`。
        self._read_failed = False
        self._closed = False
        # 通知路径上吞掉的异常（见 `swallow.py`）
        self.swallowed = swallow.Swallower("session events")
        # 结构版本号：`_rebuild_index_locked` 每跑一次就 +1（装载 / 改结构 / 增删通道）。
        # 给"按节拍取数"的那一层（`rows_pump`）一个**便宜且可靠**的失效判据——光比工程
        # 对象身份认不出"同一份对象被原地改了结构"（界面增删点位走的正是那条路）。
        self._revision = 0
        self._runner.subscribe(self._on_engine_event)
        if packet_store is not None:
            # 必须包这一层 isinstance：Session 的事件流里除 PacketEvent 还有 ChannelState，
            # 直接 subscribe(packet_store.add) 会把"通道起了/停了"灌进报文缓冲，
            # 表现成报文窗里冒出没有功能码的假帧。分流放在这里而不是 PacketStore.add 里，
            # 是为了让 add 继续只是"收一条报文"这一个动作。
            def to_store(event: object, store=packet_store) -> None:
                if isinstance(event, PacketEvent):
                    store.add(event)
            self.subscribe(to_store)

    # ---------- 装载 ----------

    def load(self, project: Project, source=None) -> list[Issue]:
        """换一份工程。有 error 也照样装（让人能改完再启动），但把清单原样返回。

        `source` 是这份工程装进来的那个文件路径。它只有两个用处：点表旁边的网络声明
        （`x.csv` -> `x.net.json`）按它去认，以及界面标题/网络页显示"这是哪份"。给了就
        记住、没给就清掉——上一份工程的路径留在身上，会让新工程的通道去听旧声明里的地址。
        """
        with self._lock:
            stuck = self._stop_all_locked()
            # **同一份对象**再装载一次 = 原地刷新（界面增删之后走的正是这条路）：那些改动
            # 还在这份工程里，脏标记必须留着——清掉它，标题上的 `*` 就此消失、关窗也不再问，
            # 一次"加完通道顺手关门"就把改动丢了。换一份**对象**才是换工程（旧改动已经
            # 不存在了），照旧归零。
            same_project = project is self._project
            self._project = project
            self._source = str(source) if source else None
            self._channels = {ch.name: ChannelRuntime(ch.name) for ch in project.channels}
            self._rebuild_index_locked()
            self._dirty = self._dirty and same_project
            self._read_failed = False
            # 旧通道没关掉就得在这里留一句话：它的句柄随 `_channels` 一起换掉了，再没有
            # 第二次"停止"能摸到它，端口会一直占到进程结束。只发日志不发 ChannelState——
            # 通道对象马上是新的了，给一个还没起的通道标红是本末倒置。
            for runtime in stuck:
                self.publish_log("error",
                                 f"channel {runtime.name} did not close "
                                 f"while loading the point table; its port is still held: "
                                 f"{runtime.last_error}")
        return validate_project(project)

    def add_channel(self, channel) -> None:
        """加**一路**通道的运行时：只给这一路建 runtime 并重建索引，**不碰别的通道**。

        为什么要有它：`load()` 换的是整份工程，第一步 `_stop_all_locked()` 会把正在给网关
        服务的那一路一起停掉——界面里"有通道在跑时不许加通道"那条拒绝就是这么来的。
        这里只动 `self._channels` 里的一个键。

        顺序口径：**先把工程改对**（`project.edit.add_channel`，带校验与回滚），再让它跟上
        ——认不出来的名字直接拒（否则会出现一个"有 runtime、点表里没有"的通道）。
        """
        with self._lock:
            if not any(ch.name == channel.name for ch in self._project.channels):
                raise ValueError(
                    f"channel {channel.name} is not in the project; add it there first "
                    f"(simbench.project.edit.add_channel)")
            self._channels[channel.name] = ChannelRuntime(channel.name)
            self._rebuild_index_locked()

    def remove_channel(self, name: str) -> None:
        """删**一路**通道的运行时：先停它（停不掉就拒绝），再摘掉并重建索引。

        别的通道一律不碰。停不掉就拒绝的理由与 `_stop_one` 一样：句柄一摘，那个端口就再也
        没有第二次"停止"能摸到它了。
        """
        with self._lock:
            runtime = self._channels.get(name)
            if runtime is None:
                raise KeyError(name)
            if any(ch.name == name for ch in self._project.channels):
                raise ValueError(f"channel {name} is still in the project; remove it there "
                                 f"first (simbench.project.edit.remove_channel)")
            if runtime.running and not self._stop_one(runtime):
                raise BackendStartError(
                    f"channel {name} did not stop, so it was not removed: "
                    f"{runtime.last_error}")
            self._channels.pop(name, None)
            self._rows_meta.pop(name, None)
            self._rebuild_index_locked()

    def reindex(self, channel: str) -> None:
        """按当前工程重建读写索引，**不停任何通道**（点位属性面板的结构性改动用它）。

        `load()` 同样重建索引，但它第一步就是 `_stop_all_locked()`：改一个已停通道的
        点地址，会把另一路正给网关服务的从站一起停掉——那是新的现场事故。这里只摊平
        引用：`_points`/`_rows_meta` 存的本来就是同一批 `Point` 对象，在跑的 SimDevice
        拿的也是它们，重建不打扰任何通道。

        边界，三条，谁都不许误用这个缝：

        * **`channel` 在跑就直接拒绝**（`ValueError`）。摊平是全工程摊的，`channel`
          说的就是"这次改动落在哪一路名下"：在跑的那一路被重新建键，等于把 `SimDevice`
          在 `start_channel` 时烘死的地址表和 `_points` 拆成两份真相——界面上的行还按
          旧地址显示，按旧地址写被挡在"不在点表里"，按新地址写又被在跑的从站拒收，
          两头都对不上。调用方（`gui/point_props`）本来就在写回**之前**拒绝了这种改动
          （那份拒绝的理由要带点位名，所以留在面板里），这里的守卫是给下一个调用方的：
          忘了拒绝也拿不到一份错索引。会话里根本没有这一路的 runtime 时没有"在跑的
          从站"可拆台，照建。
        * **不对账通道集合**。加了/删了通道要走 `load()`：`_channels` 在这里一个字不动，
          新建的通道没人给它 runtime（`start_channel` 找不到人），删掉的通道则留着一个
          没人再能停的后端。
        * **兄弟通道在跑是可以的**，这正是它存在的理由。前提是调用方没动任何在跑通道的
          点位身份（区/地址/类型/初值）——面板靠上面那条拒绝保证这一点。
        """
        with self._lock:
            runtime = self._channels.get(channel)
            if runtime is not None and runtime.running:
                raise ValueError(
                    f"channel {channel} is running; reindexing would re-key the point "
                    f"table under the live slave -- stop it first or use load()")
            self._rebuild_index_locked()

    @property
    def project(self) -> Project:
        return self._project

    @property
    def source(self) -> str | None:
        """这份工程是从哪个文件装的；None = 还没落过盘（或调用方没给）。"""
        return self._source

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def runner(self) -> EngineRunner:
        """引擎线程那一层（只读用途：投递路径的记账、诊断、用例断言）。"""
        return self._runner

    @property
    def revision(self) -> int:
        """结构版本号：装载、改点位结构、增删通道都会 +1（只增不减）。"""
        with self._lock:
            return self._revision

    @property
    def closed(self) -> bool:
        """这个 Session 已经收工（`shutdown()` 过了）。

        给后台泵那类"自己按节拍取数"的东西看的：对着一个已经停掉的 runner 继续取值，
        每跳都会留一条错误——那是"日志把工具拖死"的另一种写法。
        """
        return self._closed

    def mark_dirty(self) -> None:
        self._dirty = True

    def clear_dirty(self) -> None:
        """把脏标记放回"没改过"。只有一个调用理由：**改动已经落盘了**。

        主窗的"另存为"取消时不许调它——`*` 消失就等于告诉用户"存好了"，而他的改动
        还在内存里没写过盘。换工程（`load()` 拿到**另一份对象**）时它自己会把标记归零，
        那是"这些改动已经不存在了"，与这里不是一件事；同一份对象重装载不清（原地刷新）。
        """
        with self._lock:
            self._dirty = False

    def issues(self) -> list[Issue]:
        """按**当前**工程现算一次校验，返回完整清单（不只拦住启动的那几条）。

        存在的理由只有一个：GUI 改完 host/port 要立刻问同一份判定，而不是在界面层
        再抄一套端口范围/主机名规则。两份判定迟早漂移，漂了的表现是"面板放行的配置
        启动时被拦下"或反之，现场只会当成模拟器 bug。
        """
        return validate_project(self._project)

    def publish_log(self, level: str, text: str) -> None:
        """往事件流发一条 `LogLine`：界面上的拒绝理由要让状态栏/日志窗也听见。

        只发一句话、不改任何状态，所以给 widget 用是安全的；`_publish` 会吞掉订阅者
        自己的异常，不会把通道带停。
        """
        self._publish(LogLine(level, text))

    # ---------- 启停 ----------

    def channel(self, name: str) -> ChannelRuntime | None:
        with self._lock:
            return self._channels.get(name)

    def channel_names(self) -> list[str]:
        with self._lock:
            return list(self._channels)

    def start_channel(self, name: str) -> None:
        runtime, channel = self._require(name)
        with self._lock:
            if runtime.running:
                return
        # 启动门槛：一次报全，不过就不起。挤牙膏式"改一个错报一个错"在现场是纯损耗，
        # 而"带着错点表起来"更糟——主站读到的是一个半成品设备，没人会怀疑模拟器。
        issues = validate_project(self._project)
        errors = error_lines(issues)
        if errors:
            raise ValueError(f"point table has {len(errors)} error(s); refusing to start: "
                             + " | ".join(errors[:3]))
        # 运行期的报错出口：backend 在 **loop 线程**上直接调 `publish_log(level, text)`，
        # 与 `packet_sink` 同一个口径（见 `_packet_sink`），这里不投递也不排队——
        # `publish_log` 只做"往事件流发一条 LogLine"，而 `_publish` 已经吞掉订阅者的异常，
        # 所以界面那一侧坏了也不会把曲线带停（backend 的 `_emit` 还有同一道兜底）。
        # 这条线一断，§8 要的"运行期故障留一条日志"就只剩测试看得见（带点位名是 M4 计划的口径）。
        # `tick_ms` 只有两个来源：这里的界面路径，和无头 CLI（`__main__._serve` 把同一个
        # 工程值交下去）；backend 自己默认 0，M1/M2 的既有用例形状一个字都没变。
        try:
            # 选后端只走 `make_backend`（backend 包的装配口）：串口那一路落地后这里一个字
            # 不用改。**选型也算在这一段里**：认不出的传输类型（例如现在的 rtu）与起不来
            # 的后端一样，是"这一路没起来"，得走同一条路——落 last_error、发 error 状态、
            # 再抛给调用方。放在 try 外面的话，界面那一格会停在"○ 停止"，而工具栏与日志
            # 已经在报失败，正是本项目最防的那类演出。
            backend = make_backend(channel, packet_sink=self._packet_sink(name),
                                   tick_ms=self._project.tick_ms,
                                   log_sink=self.publish_log,
                                   extra_endpoints=_listen_endpoints(channel, self._source))
            self._runner.call(lambda: backend.start(), _START_TIMEOUT,
                              f"start channel {name}")
        except Exception as exc:
            # 运行期失败（真端口被占 10048、保留端口 10013、监听起不来）由 backend 抛出，
            # 原样再抛给调用方，同时落 last_error + 发 error 状态：界面上那一格变红，
            # 而不是"日志说 listening 但外面连不上"。通道保持 stopped。
            runtime.last_error = str(exc)
            self._publish(ChannelState(name, "error", str(exc)))
            raise
        with self._lock:
            runtime.backend = backend
            runtime.running = True
            runtime.detail = _detail(backend)
            runtime.last_error = ""
            detail = runtime.detail
        self._publish(ChannelState(name, "running", detail))

    def stop_channel(self, name: str) -> None:
        runtime, _ = self._require(name)
        if self._stop_one(runtime):
            self._publish(ChannelState(name, "stopped", ""))
            return
        # 没关掉就不许发 stopped：界面说停了、端口还在应答，下一路 485 会撞上"端口被占"
        # 而没人知道是谁占的。状态给 error 并把异常抛给调用方，与 `start_channel` 同姿态。
        self._publish(ChannelState(name, "error", runtime.last_error))
        raise RuntimeError(f"stop channel {name} failed: {runtime.last_error}")

    def start_all(self) -> list[str]:
        """工具栏"全部启动"：把每个通道的拒绝原因收集起来，不是撞上第一个就抛。

        收的是**任何**异常而不只是 `ValueError`：端口被占抛的是 `PortInUseError`
        （RuntimeError 系），只挡 ValueError 的话第一个坏通道会把后面的全部连累——
        现场接了两路 485 只起得来一路，而界面上没有任何一条说"另一个通道我没试过"。
        一次报全才是这里的行为契约。`KeyError`（通道名都不存在）在 `_require` 里，
        那是编程错误，照样外抛。
        """
        failures: list[str] = []
        for name in self.channel_names():
            try:
                self.start_channel(name)
            except Exception as exc:                # noqa: BLE001
                # 不带异常类名前缀：两类拒绝消息自己就说清了是哪个通道（"channel tcp-1:
                # port ..." / "point table has N error(s)..."），再套一层就成重复句。
                # 消息为空时退回类名，绝不给界面留一条空白的"报过了"。
                failures.append(str(exc) or f"{type(exc).__name__} (no message)")
        return failures

    def stop_all(self) -> list[str]:
        """工具栏"全部停止"：逐个关后端并**逐个**发状态，关不掉的把原因收集起来返回。

        和 `stop_channel` 同一份形状：内部 `_stop_all_locked` 只收后端不发事件（`load()`
        换工程时要用它，给旧通道发"停了"界面上根本没人听）。少了这几个事件，工具栏
        "全部停止"之后的设备树就还挂着 `●`——通道已经停了而界面说它还在跑，正是本项目
        最防的那类演出；反过来"还在跑而界面说停了"同样要防，所以没关掉的通道发 `error`
        而不是跟着发 `stopped`。和 `start_all` 一样一个失败不连累后面的。
        """
        with self._lock:
            runtimes = list(self._channels.values())
            stuck = {id(runtime) for runtime in self._stop_all_locked()}
        for runtime in runtimes:
            failed = id(runtime) in stuck
            self._publish(ChannelState(runtime.name,
                                       "error" if failed else "stopped",
                                       runtime.last_error if failed else ""))
        return [f"stop channel {runtime.name}: {runtime.last_error}"
                for runtime in runtimes if id(runtime) in stuck]

    def shutdown(self) -> None:
        """收尾：停所有通道 → 把自己从 runner 上摘掉 → 断开界面订阅。

        退订这一步**不能省**：runner 通常是共享的（测试里会话级一个，GUI 里几个 Session
        也可能共用同一个）。没退订的话，一个已经关掉的 Session 会永远留在事件流里，
        每条报文都再穿过它一遍、再往它那些已经 `deleteLater` 掉的 widget 上投一次——
        实测加上界面用例之后全量套件从 20 秒涨到 8 分钟，就是这个 O(历史窗口数) 的扇出。
        """
        self.stop_all()                       # 先把 stopped 状态发完，界面还听得见
        self._closed = True
        self._runner.unsubscribe(self._on_engine_event)
        with self._lock:
            self._sinks.clear()
        if self._owns_runner:
            self._runner.stop()

    # ---------- 点表读写 ----------










    def _transport(self, channel: str) -> str:
        """这一路是什么传输（读/写/归属三处都要按它分派，判定只在这里一次）。

        返回**传输串**而不是"是不是 S7"的布尔：布尔只有两态，第四种协议加不进来
        （见 `docs/superpowers/plans/2026-10-14-import-export-formats.md` §1）。
        认不出的通道（换工程那一瞬间）按 TCP 走：那是既有行为里最保守的一档。
        """
        try:
            return channel_transport(self._project.find_channel(channel))
        except KeyError:
            return "tcp"

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
            except Exception as exc:                 # noqa: BLE001
                # 一个 widget 回调炸了不许带停别的订阅者，也不许带停通道——但**不许没有
                # 痕迹**（见 `swallow.py`）：留一本账，界面/用例都能问"吞过什么"。
                swallow.note(self.swallowed, exc, f"sink {sink!r}")

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

    def _stop_one(self, runtime: ChannelRuntime) -> bool:
        """关掉一个通道的后端，返回"端口真的释放了没有"。

        关不掉时**引用留住、running 不动**：`call` 超时说明引擎线程没回话，监听大概率还
        在应答。放开引用就再没人能关它——界面上写着 ○ 停止、外面主站照样读到值，操作员
        只剩重启进程一条路；留着句柄，他再点一次"停止"还能真的关掉。
        """
        if runtime.backend is None:
            with self._lock:
                runtime.running = False
                runtime.detail = ""
            return True
        try:
            self._runner.call(lambda: runtime.backend.stop(), _STOP_TIMEOUT,
                              f"stop channel {runtime.name}")
        except Exception as exc:                # noqa: BLE001
            with self._lock:
                runtime.last_error = str(exc)
            return False
        with self._lock:
            runtime.backend = None
            runtime.running = False
            runtime.detail = ""
            runtime.last_error = ""
        return True

    def _rebuild_index_locked(self) -> None:
        """索引一次建好：渲染路径（10Hz）只查表，不遍历工程对象树。

        `load()` 与 `reindex()` 共用这一份摊平逻辑——两份键的构造迟早漂移，漂了的
        表现是"按新地址写值被挡在不在点表里"。调用方持锁。
        """
        self._render_cache = {}          # 换工程/改结构：旧文案一律不许留
        self._revision += 1              # 结构变了：按节拍取数的那一层据此重新取
        self._points = {}
        self._rows_meta = {ch.name: [] for ch in self._project.channels}
        for ch in self._project.channels:
            for dev in ch.devices:
                bucket = self._points.setdefault((ch.name, dev.unit_id), {})
                for p in dev.points:
                    # 键里带 `bit`：同字节上的两个 S7 bool 是两个点，不带位会互相覆盖
                    bucket[(p.area, p.address, p.bit)] = p
                    self._rows_meta[ch.name].append((dev.unit_id, dev.name, p))

    def _stop_all_locked(self) -> list[ChannelRuntime]:
        """逐个关，返回**没关掉**的那些（调用方决定怎么报，这里不碰状态机）。"""
        return [runtime for runtime in self._channels.values()
                if not self._stop_one(runtime)]


def _listen_endpoints(channel, source) -> list[tuple[str, int]]:
    """声明里这一路要额外听的地址（口径见 `net/endpoints.py`）。

    没有声明、声明读不出来、或名字对不上都回空列表：这里只是"多听几个地址"，不是判定，
    所以不给它任何拦住启动的机会。
    """
    from ..net.endpoints import for_channel

    return for_channel(channel, source)
