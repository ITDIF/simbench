"""Session：GUI 唯一可对话的对象。持有工程、通道状态、点表行；所有动作都在此收口。

Qt-free（不 import PySide6），所以 M1/M2 的内核测试不需要事件循环。
所有对 pymodbus 的操作都通过 backend + runner.submit 走引擎线程，Session 自己不碰 loop。

刷新是**拉模型**：GUI 以 10Hz 调 `rows(channel)`，`rows()` 在通道运行时回读寄存器、
未运行时用 CSV 初值。这里刻意不做任何"主站写入"回调、也不让引擎轮询——M1 的 backend
只有 `packet_sink` / `on_connection` 两个钩子，多一个钩子就多一条跨线程改 UI 的路，
而 10Hz 全表刷新实测最坏 0.34ms，不值得为它换推送。

回读失败时那一格**仍然**显示 CSV 初值，但会带上 `Row.stale`：不带着它，"通道在跑却没
读到"和"设备报了这个值"在界面上就是同一个样子（本项目最防的那类演出）；而"还没启动"
不是异常，不标灰。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..backend.events import ChannelState, PacketEvent
from ..backend.modbus_tcp import ModbusTcpBackend
from ..codec.point_codec import decode, encode
from ..project.model import Point, Project, area_is_bit
from ..project.validate import Issue, error_lines, validate_project
from .packets import PacketStore
from .runner import EngineRunner


@dataclass(frozen=True)
class Row:
    """点位表一行。字段顺序是**位置参数契约**：Task 5 的 `point_source.FakeEngine`
    按这个顺序构造，改名或换序都要先看 `tests/test_session.py` 那条顺序断言。

    `registers` 定死 `tuple[int, ...]`（A–I 表 G）：`_render` 与替身给的是同一种容器，
    上层才能用 `!=` 判"这行变了没有"。回读失败时它是**空的**，不是 `[0]`。

    `stale` 只在**通道在跑且回读失败**时为真。"没启动"不是异常（那时显示 CSV 初值
    是正确行为），"启动了却没读到"才是——两者回落的是同一份初值文字，界面上分不出来，
    而把"没读到"演成"设备报了这个值"正是本项目最防的那类假象，所以这里必须带出来。
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
            self._stop_all_locked()
            self._project = project
            self._channels = {ch.name: ChannelRuntime(ch.name) for ch in project.channels}
            # 索引一次建好：渲染路径（10Hz）只查表，不遍历工程对象树。
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
        # 启动门槛：一次报全，不过就不起。挤牙膏式"改一个错报一个错"在现场是纯损耗，
        # 而"带着错点表起来"更糟——主站读到的是一个半成品设备，没人会怀疑模拟器。
        issues = validate_project(self._project)
        errors = error_lines(issues)
        if errors:
            raise ValueError(f"point table has {len(errors)} error(s); refusing to start: "
                             + " | ".join(errors[:3]))
        backend = ModbusTcpBackend(channel, packet_sink=self._packet_sink(name))
        try:
            self._runner.submit(lambda: backend.start()).result(10)
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
        self._stop_one(runtime)
        self._publish(ChannelState(name, "stopped", ""))

    def start_all(self) -> list[str]:
        """工具栏"全部启动"：把每个通道的拒绝原因收集起来，不是撞上第一个就抛。"""
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
        """按点表顺序出一批行。通道在跑就回读寄存器，没跑就用 CSV 初值。

        `stale` 的判据是"通道在跑 **且** 这一行回读失败"：没跑的时候显示初值是
        正确行为（ζ：一个通道都没起时表格照样可读），把它一起标灰就等于界面在
        演出一个没发生的故障。
        """
        with self._lock:
            runtime = self._channels.get(channel)
            meta = list(self._rows_meta.get(channel, ()))
        running = bool(runtime and runtime.running)
        out: list[Row] = []
        for unit_id, device, pt in meta:
            regs, failed = self._read_back(runtime, pt, unit_id)
            value_text, registers = _render(pt, regs)
            out.append(Row(channel, device, unit_id, pt.name, pt.area, pt.address,
                           pt.datatype, pt.word_order, value_text, registers,
                           pt.readonly, pt.waveform, stale=failed and running))
        return out

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
        """改一个点的值。类型不符/越界一律拒绝且不落寄存器。

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

    def _stop_one(self, runtime: ChannelRuntime) -> None:
        if runtime.backend is not None:
            try:
                self._runner.submit(lambda: runtime.backend.stop()).result(10)
            except Exception as exc:                # noqa: BLE001
                runtime.last_error = str(exc)
            # 无论 stop 有没有报错，引用都要放开：留着半个 server 让下一发读/写
            # 打到一个已经不在监听的后端上，症状是"值读不到"而不是"通道已停"。
            runtime.backend = None
        runtime.running = False
        runtime.detail = ""

    def _stop_all_locked(self) -> None:
        for runtime in self._channels.values():
            self._stop_one(runtime)

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
    return backend.write_registers(unit_id, area, address, registers)


async def _awrite_bits(backend, unit_id, area, address, value):
    return backend.write_bits(unit_id, area, address, value)
