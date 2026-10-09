"""Session：GUI 唯一可对话的对象。持有工程、通道状态、点表行；所有动作都在此收口。

Qt-free（不 import PySide6），所以 M1/M2 的内核测试不需要事件循环。
所有对 pymodbus 的操作都通过 backend + runner.submit 走引擎线程，Session 自己不碰 loop。

刷新是**拉模型**：GUI 以 10Hz 调 `rows(channel)`，`rows()` 在通道运行时回读寄存器、
未运行时用 CSV 初值。这里刻意不做任何"主站写入"回调、也不让引擎轮询——M1 的 backend
只有 `packet_sink` / `on_connection` 两个钩子，多一个钩子就多一条跨线程改 UI 的路，
而 10Hz 全表刷新实测最坏 0.34ms，不值得为它换推送。
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
from .runner import EngineRunner


@dataclass(frozen=True)
class Row:
    """点位表一行。字段顺序是**位置参数契约**：Task 5 的 `point_source.FakeEngine`
    按这个顺序构造，改名或换序都要先看 `tests/test_session.py` 那条顺序断言。

    `registers` 现在就是一段整数序列（`_render` 给的是 list，Task 5 的替身给 tuple），
    消费方按序列处理，不要按具体容器类型判等。
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
    def __init__(self, runner: EngineRunner | None = None) -> None:
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
        regs = self._read_back(runtime, pt, unit_id)
        if regs is None:
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
    """寄存器 -> 界面文案。回读失败（regs 为 None/空）时回落 CSV 初值。

    **这个回落有两种含义，现在分不出来**：①通道没起，本来就该显示初值；
    ②通道在跑但读不到，显示初值就是演给界面一个"设备报了这个值"。
    Task 5 给 `Row` 加 `stale: bool` 把它们分开（界面据此灰掉那一格），并在那一步
    同时补内核测试——所以这里不提前加半个 stale，也不许把 ② 改成显示 0 或空串。
    """
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
