"""读侧：点位表取数（
ows）、整批快照、手动接管计数与单点回读。

都是"看"的操作：跑着的通道在引擎线程上取一次整批快照，没跑就只用 CSV 初值。
Session 通过 ReadMixin 继承这些方法，对外的名字与行为不变。
"""

from __future__ import annotations

from ..project.model import Point, area_is_bit, is_s7
from .records import _READ_TIMEOUT, ChannelRuntime, Row
from .render import _render
from .session_host import SessionHost


class ReadMixin(SessionHost):
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
        transport = self._transport(channel)
        # 跑着的时候，**整批**回读在引擎线程上一次做完（见 `_snapshot`）。没跑就没有快照：
        # 那时显示的是点表初值，一次跨线程调用都不发。
        snapshot = self._snapshot_for(runtime, meta, transport, channel) if running else {}
        out: list[Row] = []
        for unit_id, device, pt in meta:
            entry = snapshot.get((unit_id, pt.area, pt.address, pt.bit))
            regs, failed, waveform = entry if entry is not None else (None, True, True)
            out.append(self._row_cached(channel, device, unit_id, pt, regs, failed,
                                        waveform, running, transport))
        return out


    def _row_cached(self, channel: str, device: str, unit_id: int, pt: Point, regs,
                    failed: bool, waveform: bool, running: bool, transport: str) -> Row:
        """造一行，**输入没变就把上一跳那一行原样还回去**。

        为什么连 `Row` 一起缓存（实测）：光缓存"解码后的文案"只省掉三分之一的成本——
        4000 行的表上，剩下那一大截是"每跳新建 4000 个 frozen dataclass"。这些字段全是
        `(点位元信息, 寄存器, 归属, 在不在跑)` 的纯函数，所以这四样没变时复用上一跳那个对象
        是安全的；模型那边靠 `==` 比行（frozen dataclass 逐字段比），同一批对象回来等于
        一次都不重画。

        代价是"复用同一个对象"这件事必须成立：`Row` 是 frozen 的（没人能就地改它），
        `_rebuild_index_locked` 换工程/改结构时清空整张缓存（不会把上一份工程的行带给新的）。

        **键必须带上一行的全部输入**——不只是身份与寄存器。第一版只带了
        (身份, 类型, 字序, 初值)，于是"在属性面板里改个名字"这类 **live 改动**（不改结构、
        不重建索引）会读到上一跳那个旧名字的 `Row`：表里一行都没变，看着像没生效
        （由 `test_a_live_only_apply_from_the_menu_says_next_tick_not_restart` 逮住）。
        名字 / 只读 / 波形都是 `Point` 上可以原地改的字段，所以它们都进键。
        """
        key = (channel, device, unit_id, transport, pt.area, pt.address, pt.bit, pt.name,
               pt.datatype, pt.word_order, pt.initial, pt.readonly, pt.waveform)
        cached = self._render_cache.get(key)
        if cached is not None:
            was_regs, was_failed, was_waveform, was_running, row = cached
            if (was_regs == regs and was_failed == failed and was_waveform == waveform
                    and was_running == running):
                return row
        value_text, registers = _render(pt, regs, transport)
        row = Row(channel, device, unit_id, pt.name, pt.area, pt.address,
                  pt.datatype, pt.word_order, value_text, registers,
                  pt.readonly, pt.waveform, stale=failed and running,
                  source="waveform" if waveform else "manual", bit=pt.bit)
        self._render_cache[key] = (regs, failed, waveform, running, row)
        return row


    def _snapshot_for(self, runtime, meta, transport: str, channel: str) -> dict:
        """整批快照（寄存器 + 归属）在引擎线程上取一次；超时/失败就交回空表。

        空表的意思是"这一跳整批没读到"：`rows()` 把每一行标成 `stale`（界面灰显），与
        "设备真报了这个值"分得开。**不把异常抛进 GUI 心跳**——10Hz 的界面炸一次就没了。

        只报一次错（`self._read_failed`）：引擎不回话时每 100ms 一条日志会把日志窗刷干净，
        而"表全是灰的"本身就是看得见的信号。
        """
        try:
            snapshot = self._runner.call(
                lambda: self._snapshot(runtime, meta, transport), _READ_TIMEOUT,
                f"read rows of channel {channel}")
        except Exception as exc:                    # noqa: BLE001
            if not self._read_failed:
                self._read_failed = True
                self.publish_log(
                    "error", f"channel {channel}: the engine did not answer the row read "
                             f"within {_READ_TIMEOUT:g}s ({exc}); the table shows stale "
                             f"values until it answers again")
            return {}
        self._read_failed = False
        return snapshot


    async def _snapshot(self, runtime, meta, transport: str) -> dict:
        """**在引擎线程上**取整批快照：`{(unit, 区, 地址, 位): (寄存器|None, 失败?, 归波形?)}`。

        为什么要有它（而不是让 `rows()` 一个一个直读）：运行态——寄存器缓冲与 `SourceStore`
        ——按设计只由 loop 线程读写（`backend/ownership.py` 开头那段、spec §13 的跨线程那一课）。
        直读单个 int 不会读出半个值，但**多寄存器的值**（float32/uint64 跨两格）在两格之间被
        tick 改写时，界面上会出现一帧"谁都没写过"的数——正是本项目最防的那类假象。
        收成一次之后，这一跳要么全看到、要么整批标 `stale`。

        写成 `async` 是接口要求（`EngineRunner.call` 收的是**协程工厂**，不是同步函数）：
        里面一个 `await` 都没有，因为要的就是"在 loop 线程上同步做完"。
        """
        backend = runtime.backend
        if backend is None:
            return {}
        key = 0 if is_s7(transport) else None
        out: dict = {}
        for unit_id, _device, pt in meta:
            regs, failed = self._read_back(runtime, pt, unit_id, transport)
            waveform = backend.source_is_waveform(
                key if key is not None else unit_id, pt.area, pt.address, bit=pt.bit)
            out[(unit_id, pt.area, pt.address, pt.bit)] = (regs, failed, waveform)
        return out


    def manual_count(self, channel: str) -> int | None:
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
        # 与 `rows()` 同一条路：从引擎线程取一次整批快照，再在本地数。取不到（引擎没回话）
        # 回 `None`——"不知道"与"0 处接管"是两件事，界面宁可不说那个数，也不能报一个假的 0。
        snapshot = self._snapshot_for(runtime, meta, self._transport(channel), channel)
        if not snapshot:
            return None
        return sum(1 for entry in snapshot.values() if not entry[2])


    def _read_back(self, runtime: ChannelRuntime | None, pt: Point,
                   unit_id: int,
                   transport: str = "tcp") -> tuple[list[int] | None, bool]:
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
            if is_s7(transport):
                # S7 回的是**字节**（bool 也回一个字节的 0/1）：`_render` 那一侧按 S7 类型解。
                raw = runtime.backend.read_point_bytes(pt)
                if raw is None or not raw:
                    return None, True
                return list(raw), False
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
