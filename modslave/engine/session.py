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
from dataclasses import dataclass

from ..backend.events import ChannelState, LogLine, PacketEvent
from ..backend.modbus_tcp import ModbusTcpBackend
from ..codec.point_codec import decode, encode
from ..project.model import Point, Project, area_is_bit
from ..project.validate import Issue, error_lines, validate_project
from .packets import PacketStore
from .runner import EngineRunner

# 等引擎回话的三条上限。**口径**：GUI 能走到的每一条等待都用 `runner.call(...)`，
# 不用裸 `submit(...).result(...)`——`call` 在超时会把引擎线程当时的栈帧一起写进异常
# 消息，而裸 TimeoutError 在现场只剩"界面没响应"这一条线索（本项目最防的"看不出是谁卡的"）。
#   `_START_TIMEOUT` 10s：backend 内部 `_await_listener` 自己 5s 就放弃，10s 严格在它之上，
#              所以这里等到的必然是"已经报过错了"而不是"还在等"。
#   `_STOP_TIMEOUT`  10s：实测 `stop()` 里 serve_forever 在 0.2ms 内收尾；10s 的含义是
#              "根本没关掉"，宁可带上栈帧报错也不许把旧监听静默留着占端口。
#   `_WRITE_TIMEOUT`  5s：写一个点是内存操作（改 SimRuntime 的寄存器列表），5s 不回
#              说明引擎线程被别的东西占住了，再继续等下去只会让操作员多点几次同一格。
_START_TIMEOUT = 10.0
_STOP_TIMEOUT = 10.0
_WRITE_TIMEOUT = 5.0


@dataclass(frozen=True)
class Row:
    """点位表一行。字段顺序是**位置参数契约**：Task 5 的 `point_source.FakeEngine`
    按这个顺序构造，改名或换序都要先看 `tests/test_session.py` 那条顺序断言。

    `registers` 定死 `tuple[int, ...]`（A–I 表 G）：`_render` 与替身给的是同一种容器，
    上层才能用 `!=` 判"这行变了没有"。回读失败时它是**空的**，不是 `[0]`。

    `stale` 只在**通道在跑且回读失败**时为真。"没启动"不是异常（那时显示 CSV 初值
    是正确行为），"启动了却没读到"才是——两者回落的是同一份初值文字，界面上分不出来，
    而把"没读到"演成"设备报了这个值"正是本项目最防的那类假象，所以这里必须带出来。

    `source` 是 spec §7 那唯一一条值归属：`waveform` = 曲线还在驱动这个点，`manual` =
    被界面或主站接管过、波形一个字都不再写。它**只能放在最后一位且带默认值**：
    `FakeEngine`/本文件的 `rows()` 都按位置构造，插进 `waveform` 前面会把 `readonly`
    与 `waveform` 串成一列看着合法的错值——那种错测试全绿、现场读错。
    """

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
    stale: bool = False
    source: str = "waveform"

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
        self._channels: dict[str, ChannelRuntime] = {}
        self._points: dict[tuple[str, int], dict[tuple[str, int], Point]] = {}
        self._rows_meta: dict[str, list[tuple[int, str, Point]]] = {}
        self._dirty = False
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

    def load(self, project: Project) -> list[Issue]:
        """换一份工程。有 error 也照样装（让人能改完再启动），但把清单原样返回。"""
        with self._lock:
            stuck = self._stop_all_locked()
            self._project = project
            self._channels = {ch.name: ChannelRuntime(ch.name) for ch in project.channels}
            self._rebuild_index_locked()
            self._dirty = False
            # 旧通道没关掉就得在这里留一句话：它的句柄随 `_channels` 一起换掉了，再没有
            # 第二次"停止"能摸到它，端口会一直占到进程结束。只发日志不发 ChannelState——
            # 通道对象马上是新的了，给一个还没起的通道标红是本末倒置。
            for runtime in stuck:
                self.publish_log("error",
                                 f"channel {runtime.name} did not close "
                                 f"while loading the point table; its port is still held: "
                                 f"{runtime.last_error}")
        return validate_project(project)

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
    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        self._dirty = True

    def clear_dirty(self) -> None:
        """把脏标记放回"没改过"。只有一个调用理由：**改动已经落盘了**。

        主窗的"另存为"取消时不许调它——`*` 消失就等于告诉用户"存好了"，而他的改动
        还在内存里没写过盘。换工程时 `load()` 自己会把标记归零，那是"这些改动已经不
        存在了"，与这里不是一件事。
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
        backend = ModbusTcpBackend(channel, packet_sink=self._packet_sink(name),
                                   tick_ms=self._project.tick_ms,
                                   log_sink=self.publish_log)
        # `tick_ms` 只有两个来源：这里的界面路径，和无头 CLI（`__main__._serve` 把同一个
        # 工程值交下去）；backend 自己默认 0，M1/M2 的既有用例形状一个字都没变。
        try:
            self._runner.call(lambda: backend.start(), _START_TIMEOUT,
                              f"start channel {name}")
        except Exception as exc:                    # noqa: BLE001
            # 运行期失败（真端口被占 10048、保留端口 10013、监听起不来）由 backend 抛出，
            # 原样再抛给调用方，同时落 last_error + 发 error 状态：界面上那一格变红，
            # 而不是"日志说 listening 但外面连不上"。通道保持 stopped。
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
        self._runner.unsubscribe(self._on_engine_event)
        with self._lock:
            self._sinks.clear()
        if self._owns_runner:
            self._runner.stop()

    # ---------- 点表读写 ----------

    def rows(self, channel: str) -> list[Row]:
        """按点表顺序出一批行。通道在跑就回读寄存器，没跑就用 CSV 初值。

        `stale` 的判据是"通道在跑 **且** 这一行回读失败"：没跑的时候显示初值是
        正确行为（ζ：一个通道都没起时表格照样可读），把它一起标灰就等于界面在
        演出一个没发生的故障。

        `source` 同一条口径：没起通道时一律 `waveform`，起之后现问 backend 那一份归属
        状态——`source_is_waveform` 是无 await 的纯内存集合判定，与 `_read_back` 的直读
        同一个口径；写侧（接管/恢复/tick）全部只在 loop 线程发生。
        """
        with self._lock:
            runtime = self._channels.get(channel)
            meta = list(self._rows_meta.get(channel, ()))
        running = bool(runtime and runtime.running)
        backend = runtime.backend if running else None
        out: list[Row] = []
        for unit_id, device, pt in meta:
            regs, failed = self._read_back(runtime, pt, unit_id)
            value_text, registers = _render(pt, regs)
            out.append(Row(channel, device, unit_id, pt.name, pt.area, pt.address,
                           pt.datatype, pt.word_order, value_text, registers,
                           pt.readonly, pt.waveform, stale=failed and running,
                           source=_source_of(backend, unit_id, pt)))
        return out

    def manual_count(self, channel: str) -> int:
        """当前被人工接管的**点位**数（状态栏那句"有 N 处被接管"用）。

        按点位数而不是按地址数：`SourceStore.cleared()` 数的是地址，一个 uint64 点被接管
        在那里是 4，而操作员改的是"一个点"——单位对不上，那个数就没法对着看。这里手里
        就有点表，所以逐个点问归属，不要求 backend 交出整表快照（Task 2 复审的口径）。
        通道没起时是 0：没起就谈不上接管。
        """
        with self._lock:
            runtime = self._channels.get(channel)
            meta = list(self._rows_meta.get(channel, ()))
        if runtime is None or not runtime.running or runtime.backend is None:
            return 0
        backend = runtime.backend
        return sum(1 for unit_id, _device, pt in meta
                   if not backend.source_is_waveform(unit_id, pt.area, pt.address))

    def restore_waveform(self, channel: str, unit_id: int, area: str,
                         address: int) -> bool:
        """把一个人工接管的点交还给波形，返回"真还回去过东西没有"。

        签名里只有**基础地址**，跨度在这里自己算：`Session` 手里有摊平的点表，取出该点的
        `pt.span` 再投给 backend。少传跨度就会让一个 4 格的 uint64 恢复之后仍留 3 格是
        `manual`——按地址数的 `SourceStore.cleared()` 因此永远回不了零，而按点数的那个
        （`manual_count` 只问基础格）根本看不见这三格，界面上一切正常、地址却再也归不了波形。

        两条拒绝（不在点表 / 通道没在跑）都是日志一句话 + `False`，不许从 Qt 槽位里逃出
        `AttributeError`（M1 的 `_require_writer` 钉过同形问题）。恢复了什么要点名，
        静默把值交还波形等于操作员看不见发生了什么。
        """
        runtime, _ = self._require(channel)
        pt = self._points.get((channel, unit_id), {}).get((area, address))
        if pt is None:
            self.publish_log("warning",
                             f"{area} address {address} (unit {unit_id}) on channel "
                             f"{channel} is not in the point table; nothing was "
                             f"handed back to the waveform")
            return False
        if not runtime.running or runtime.backend is None:
            self.publish_log("warning",
                             f"point {pt.name} ({area} {address}, unit {unit_id}) on "
                             f"channel {channel} is not running; the waveform was not "
                             f"resumed")
            return False
        try:
            restored = self._runner.call(
                lambda: _arestore(runtime.backend, unit_id, area, address, pt.span),
                _WRITE_TIMEOUT,
                f"restore waveform {area} {address} (unit {unit_id})")
        except Exception as exc:                    # noqa: BLE001
            self.publish_log("error",
                             f"restore waveform for point {pt.name} ({area} {address}, "
                             f"unit {unit_id}) failed: {exc}")
            return False
        if restored:
            self.publish_log("info",
                             f"point {pt.name} ({area} {address}, unit {unit_id}) on "
                             f"channel {channel} handed back to the waveform; "
                             f"{pt.span} register(s) released")
        else:
            # 没在 manual 上就别谎报"恢复了"：那句日志会是操作员唯一能看见的证据
            self.publish_log("info",
                             f"point {pt.name} ({area} {address}, unit {unit_id}) on "
                             f"channel {channel} was already driven by the waveform")
        return bool(restored)

    def write(self, channel: str, unit_id: int, area: str, address: int,
              text: str) -> tuple[bool, str]:
        """点位表模型（`gui/point_table`）用的写入口：只把 `WriteResult` 收成二元组。

        判定与错误文案全部留在 `set_point`/codec 那一份里——GUI 抄一套自己的话术，
        同一件错在现场就有两种说法。要 `value_text`/`registers` 的调用方直接用
        `set_point`。
        """
        res = self.set_point(channel, unit_id, area, address, text)
        return (res.ok, res.error)

    def set_point(self, channel: str, unit_id: int, area: str, address: int,
                  text: str) -> WriteResult:
        """改一个点的值。类型不符/越界/超出点位跨度一律拒绝且不落寄存器。

        引擎写只读点是允许的（M1 定的口径：readonly 是"挡主站"的墙），
        所以从界面改只读量不会被拒——界面上要另外标注它是只读量。

        位区与寄存器区分两条路走：M1 已钉 `write_registers(11, "co", ...)` 直接抛
        `ValueError`（两边的"地址 0"含义不同，混用会把风机写成反向），所以 `area_is_bit`
        的判定必须在**这里**做对，不许把区名原样丢给后端去猜。
        """
        runtime, _ = self._require(channel)
        if not runtime.running or runtime.backend is None:
            return WriteResult(False, f"channel {channel} is not running")
        pt = self._points.get((channel, unit_id), {}).get((area, address))
        if pt is None:
            # 不在点表（含未知 area、未知 unit、越界地址）一律在这里挡住：
            # 放进 backend 就会变成 `unknown area 'xx'` 那种像模拟器 bug 的话。
            return WriteResult(False, f"{area} address {address} is not in the point table")
        try:
            registers = encode(text, pt.datatype, pt.word_order)
        except ValueError as exc:
            return WriteResult(False, str(exc))
        if len(registers) > pt.span:
            # `Point.span` 是**初值**撑出来的跨度（`model.register_span`：string 按
            # ceil(len/2)），所以把 string 点改成更长的文本时，编出来的寄存器比这个点占的
            # 格子多——照写就踩到邻居的格子上，而 `_awrite` 按 `len(registers)` 标接管，
            # `restore_waveform` 只还得回 `pt.span` 那一段：邻居从此永久 manual。
            # 跨度真正的入口是点表的初值，所以那句得指到那里去，不是指这个编辑框。
            return WriteResult(
                False, f"point {pt.name} at {area} {address} is {pt.span} register(s), "
                       f"this value needs {len(registers)}; widen it via the point's "
                       f"初值 and restart the channel")
        try:
            # 走 `call` 而不是裸 `submit(...).result()`：超时时 `call` 会把引擎线程当时的
            # 栈帧带进消息。界面上"点了没反应"和"引擎卡在写 4x5 这一帧"是两种完全不同的
            # 故障，后者能凭一张截图定位。5 秒的口径见 `_WRITE_TIMEOUT`。
            #
            # 接管状态在**同一个 lambda 里、写成功之后**记（spec §7）：`SourceStore` 是
            # loop 线程的可变状态，主线程往它插一脚就是跨线程共享。写成 False（地址不在块内、
            # 引擎拒绝）时一个状态都不动——"一次没写成的编辑把曲线停了"和主站写只读点被拒
            # 是同一类误判。
            if area_is_bit(area):
                ok = self._runner.call(
                    lambda: _awrite_bits(runtime.backend, unit_id, area, address,
                                         bool(registers[0])),
                    _WRITE_TIMEOUT, f"write {area} {address} (unit {unit_id}) "
                                    f"on channel {channel}")
            else:
                ok = self._runner.call(
                    lambda: _awrite(runtime.backend, unit_id, area, address, registers),
                    _WRITE_TIMEOUT, f"write {area} {address} (unit {unit_id}) "
                                    f"on channel {channel}")
        except Exception as exc:                    # noqa: BLE001
            return WriteResult(False, f"engine write failed: {exc}")
        if not ok:
            return WriteResult(False, f"{area} address {address} is outside the point table")
        regs, failed = self._read_back(runtime, pt, unit_id)
        if failed or regs is None:
            # 写成功但读不回来：这不是"值没变"，是回读通道出了毛病。宁可说"没读到"，
            # 也不许回一个初值冒充当前值（那一格会被当成"设备报了这个值"）。
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
                pass    # 一个 widget 回调炸了不许带停别的订阅者，也不许带停通道

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
            runtime.running = False
            runtime.detail = ""
            return True
        try:
            self._runner.call(lambda: runtime.backend.stop(), _STOP_TIMEOUT,
                              f"stop channel {runtime.name}")
        except Exception as exc:                # noqa: BLE001
            runtime.last_error = str(exc)
            return False
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
        self._points = {}
        self._rows_meta = {ch.name: [] for ch in self._project.channels}
        for ch in self._project.channels:
            for dev in ch.devices:
                bucket = self._points.setdefault((ch.name, dev.unit_id), {})
                for p in dev.points:
                    bucket[(p.area, p.address)] = p
                    self._rows_meta[ch.name].append((dev.unit_id, dev.name, p))

    def _stop_all_locked(self) -> list[ChannelRuntime]:
        """逐个关，返回**没关掉**的那些（调用方决定怎么报，这里不碰状态机）。"""
        return [runtime for runtime in self._channels.values()
                if not self._stop_one(runtime)]

    def _read_back(self, runtime: ChannelRuntime | None, pt: Point,
                   unit_id: int) -> tuple[list[int] | None, bool]:
        """回读一个点位当前的寄存器，返回 `(regs, failed)`。

        `failed` 与"regs 是 None"不是一回事，必须分开说：

        * 通道没跑（`runtime`/`backend` 为空）→ `(None, False)`。那是"还没起"，
          不是异常，界面上显示 CSV 初值就是正确行为（不标灰）。
        * 后端抛异常、或回读 `None`/空序列 → `(None, True)`。那是"起了却没读到"，
          调用方（`rows()`）据此置 `stale`——否则这一格和"设备真报了这个值"同形。

        空序列也算失败：`_render` 对 `[]` 同样回落初值，只判 None 会把"读到一串空"
        当成成功，`stale` 就漏了这一条。
        """
        if runtime is None or runtime.backend is None:
            return None, False
        try:
            if area_is_bit(pt.area):
                bits = runtime.backend.read_bits(unit_id, pt.area, pt.address, 1)
                if bits is None or not bits:
                    return None, True
                return [1 if bits[0] else 0], False
            regs = runtime.backend.read_registers(unit_id, pt.area, pt.address, pt.span)
        except Exception:                                # noqa: BLE001
            return None, True
        if not regs:
            return None, True
        return list(regs), False


def _source_of(backend, unit_id: int, pt: Point) -> str:
    """这一行的值当前归谁驱动：`waveform` / `manual`。

    `backend` 为空（通道没起）一律回 `waveform`：那时那一格显示的是点表初值，标"已停"
    是界面在演一出没发生的接管（ζ，与 `stale` 同一条判据）。
    """
    if backend is None:
        return "waveform"
    return "waveform" if backend.source_is_waveform(unit_id, pt.area, pt.address) \
        else "manual"


def _render(pt: Point, regs: list[int] | None) -> tuple[str, tuple[int, ...]]:
    """寄存器 -> 界面文案。回读失败（regs 为 None/空）时回落 CSV 初值。

    回落本身有两种含义——①通道没起，本来就该显示初值；②通道在跑但读不到，显示初值
    就是演给界面一个"设备报了这个值"。这一层分不出来（它拿不到通道状态），所以
    `rows()` 用 `_read_back` 的 `failed` 补 `Row.stale` 把两者分开；这里不许改成
    显示 0 或空串（那等于用一个假的 0 替掉"没读到"）。

    寄存器一律回 `tuple`（A–I 表 G）：`Row.registers` 与 `WriteResult.registers`
    共用这一份产出，两种容器并存会让上层的"这行变了没有"判等时好时坏。
    """
    if not regs:
        return pt.initial, ()
    try:
        value = decode(list(regs), pt.datatype, pt.word_order)
    except ValueError:
        return pt.initial, tuple(regs)
    if isinstance(value, bool):
        return ("true" if value else "false"), tuple(regs)
    if isinstance(value, float):
        return (f"{value:g}", tuple(regs))
    return (str(value), tuple(regs))


async def _awrite(backend, unit_id, area, address, registers):
    """寄存器写 + 人工接管，一次投递做完（两步都只在 loop 线程发生）。

    接管按这次真写下去的跨度记：`set_point` 已经挡掉 `len(registers) > pt.span` 的编辑，
    所以 `restore_waveform` 交还的 `pt.span` 恒是它的超集，恢复不会留尾巴。
    """
    ok = backend.write_registers(unit_id, area, address, registers)
    if ok:
        backend.mark_manual(unit_id, area, address, count=len(registers), who="gui")
    return ok


async def _awrite_bits(backend, unit_id, area, address, value):
    """位区写：一个线圈/离散输入就是一个点，跨度天然是 1。"""
    ok = backend.write_bits(unit_id, area, address, value)
    if ok:
        backend.mark_manual(unit_id, area, address, count=1, who="gui")
    return ok


async def _arestore(backend, unit_id, area, address, count):
    """把整点的跨度交还波形。`runner.call` 只收协程，所以和上面两个同一个形状；
    状态改动仍然全部发生在 loop 线程，主线程只是等一句"还真回去没有"。
    """
    return backend.restore_waveform(unit_id, area, address, count=count)
