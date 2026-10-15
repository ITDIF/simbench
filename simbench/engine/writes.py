"""写侧：点值写入（寄存器 / 字节 / 位）、人工接管与"交还波形"。

所有会改运行态的动作都经 
unner.call 投回引擎线程执行（见 session.py 的线程模型）。
Session 通过 WriteMixin 继承这些方法，对外的名字与行为不变。
"""

from __future__ import annotations

from ..codec import s7_codec
from ..codec.point_codec import encode
from ..project.model import area_is_bit, is_s7, s7_span_bytes
from .records import _WRITE_TIMEOUT, WriteResult
from .render import _render, _span_of
from .session_host import SessionHost


async def _awrite(backend, unit_id, area, address, registers):
    """寄存器写 + 人工接管，一次投递做完（两步都只在 loop 线程发生）。

    接管按这次真写下去的跨度记：`set_point` 已经挡掉 `len(registers) > pt.span` 的编辑，
    所以 `restore_waveform` 交还的 `pt.span` 恒是它的超集，恢复不会留尾巴。
    """
    ok = backend.write_registers(unit_id, area, address, registers)
    if ok:
        backend.mark_manual(unit_id, area, address, count=len(registers), who="gui")
    return ok


async def _awrite_s7(backend, pt, raw):
    """S7 的写 + 接管，一次投递做完（两步都只在 loop 线程发生）。

    归属键与跨度和事件那条路**必须一致**（0 / 字节数），否则界面改过的点在主站的
    写事件里算另一段，`manual_count` 与"恢复波形"就会各说各话。
    """
    ok = backend.write_point_bytes(pt, raw)
    if ok:
        backend.mark_manual(0, pt.area, pt.address,
                            count=s7_span_bytes(pt.datatype, pt.initial),
                            who="gui", bit=pt.bit)
    return ok


async def _awrite_bits(backend, unit_id, area, address, value):
    """位区写：一个线圈/离散输入就是一个点，跨度天然是 1。"""
    ok = backend.write_bits(unit_id, area, address, value)
    if ok:
        backend.mark_manual(unit_id, area, address, count=1, who="gui")
    return ok


async def _arestore(backend, unit_id, area, address, count, bit=None):
    """把整点的跨度交还波形。`runner.call` 只收协程，所以和上面两个同一个形状；
    状态改动仍然全部发生在 loop 线程，主线程只是等一句"还真回去没有"。
    """
    return backend.restore_waveform(unit_id, area, address, count=count, bit=bit)


class WriteMixin(SessionHost):
    def restore_waveform(self, channel: str, unit_id: int, area: str,
                         address: int, bit: int | None = None) -> bool:
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
        pt = self._points.get((channel, unit_id), {}).get((area, address, bit))
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
        # 跨度与归属键都按传输取：S7 是 (0, 字节数)，Modbus 是 (unit_id, 寄存器格数)。
        transport = self._transport(channel)
        key = 0 if is_s7(transport) else unit_id
        span = _span_of(pt, transport)
        try:
            restored = self._runner.call(
                lambda: _arestore(runtime.backend, key, area, address, span, bit),
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
                             f"{span} {'byte(s)' if transport == 's7' else 'register(s)'} released")
        else:
            # 没在 manual 上就别谎报"恢复了"：那句日志会是操作员唯一能看见的证据
            self.publish_log("info",
                             f"point {pt.name} ({area} {address}, unit {unit_id}) on "
                             f"channel {channel} was already driven by the waveform")
        return bool(restored)


    def write(self, channel: str, unit_id: int, area: str, address: int,
              text: str, bit: int | None = None) -> tuple[bool, str]:
        """点位表模型（`gui/point_table`）用的写入口：只把 `WriteResult` 收成二元组。

        判定与错误文案全部留在 `set_point`/codec 那一份里——GUI 抄一套自己的话术，
        同一件错在现场就有两种说法。要 `value_text`/`registers` 的调用方直接用
        `set_point`。
        """
        res = self.set_point(channel, unit_id, area, address, text, bit)
        return (res.ok, res.error)


    def set_point(self, channel: str, unit_id: int, area: str, address: int,
                  text: str, bit: int | None = None) -> WriteResult:
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
        pt = self._points.get((channel, unit_id), {}).get((area, address, bit))
        if pt is None:
            # 不在点表（含未知 area、未知 unit、越界地址、对不上的位）一律在这里挡住：
            # 放进 backend 就会变成 `unknown area 'xx'` 那种像模拟器 bug 的话。
            return WriteResult(False, f"{area} address {address} is not in the point table")
        transport = self._transport(channel)
        if is_s7(transport):
            return self._set_s7_point(runtime, pt, text, channel, unit_id, area, address)
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
        if len(registers) < pt.span:
            # 写短了要把它**自己那一段里剩下的格子补零**（M4 计划判给"人定"的那条老账，
            # 审查后这么定）：string 点按初值撑出跨度（"FAN-01" = 3 格），改成 "AB" 只编出
            # 1 格；不补的话主站按跨度读回来是 "AB" + 上一版留下的尾巴（"ABMP07"），
            # 那是界面在报一个没人写过的值。
            # 只补到 `pt.span` 为止——补到别处就是 A2 那个"一次编辑接管三个点"的事故。
            # 主站写（FC16）不走这条路，也不该走：主站写的是**寄存器**（写到哪就只动哪），
            # 而界面编辑的是**点位**（跨度语义归点表初值管），两种语义不能混。
            padded = tuple(registers) + (0,) * (pt.span - len(registers))
            registers = list(padded)
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
        regs, failed = self._read_back(runtime, pt, unit_id, transport)
        if failed or regs is None:
            # 写成功但读不回来：这不是"值没变"，是回读通道出了毛病。宁可说"没读到"，
            # 也不许回一个初值冒充当前值（那一格会被当成"设备报了这个值"）。
            return WriteResult(False, f"read-back failed for {area} {address}")
        value_text, registers_out = _render(pt, regs, transport)
        with self._lock:
            self._dirty = True
        return WriteResult(True, "", value_text, registers_out)


    def _set_s7_point(self, runtime, pt, text: str, channel: str, unit_id: int,
                      area: str, address: int) -> WriteResult:
        """S7 的改值：按 S7 类型编成字节 → 写进区域缓冲 → 回读一次。

        与 Modbus 那条**分开**而不是在一条里塞 if：跨度单位（字节 vs 寄存器格）、编码器
        （`s7_codec` vs `point_codec`）、写入口（`write_point_bytes` vs `write_registers`）
        三处都不同，缝在一起就得靠 if 把两种语义粘着——一处的单位错就是"15 位还算得出来、
        16 位少写一格"这类看不见的错。
        """
        span = _span_of(pt, "s7")
        try:
            raw = s7_codec.encode(text, pt.datatype, span)
        except ValueError as exc:
            message = str(exc)
            if "at most" in message:
                # 与 Modbus 那条同一句"该改哪里"：跨度归点表初值管，不在这个编辑框里
                message = (f"{message}; widen it via the point's 初值 "
                           f"and restart the channel")
            return WriteResult(False, message)
        try:
            ok = self._runner.call(
                lambda: _awrite_s7(runtime.backend, pt, raw),
                _WRITE_TIMEOUT, f"write {area} {address} (unit {unit_id}) "
                                f"on channel {channel}")
        except Exception as exc:                    # noqa: BLE001
            return WriteResult(False, f"engine write failed: {exc}")
        if not ok:
            return WriteResult(False, f"{area} address {address} is outside the point table")
        regs, failed = self._read_back(runtime, pt, unit_id, "s7")
        if failed or regs is None:
            return WriteResult(False, f"read-back failed for {area} {address}")
        value_text, registers_out = _render(pt, regs, "s7")
        with self._lock:
            self._dirty = True
        return WriteResult(True, "", value_text, registers_out)
