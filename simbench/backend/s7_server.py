"""S7（ISO-on-TCP / S7comm）从站：用 snap7 的原生服务端，按点表挂区域缓冲。

四条实测口径（脚本与输出：`docs/superpowers/notes/2026-10-13-s7-probes.md`）：

1. **一个服务端可以挂多个 DB**：`register_area(srvAreaDB, <DB号>, buffer)`，`index` 是区域
   编号（对 DB 区就是 DB 号）；MK/PE/PA 各自独立；没注册的 DB/区回 `Item not available`
   （正是"未定义地址"该有的形状）。缓冲必须是 **ctypes 缓冲**（`bytearray` 会在
   `ctypes.sizeof` 上炸）。
2. **主站的写落进我们那块缓冲，我们改缓冲主站也读得到** ⇒ 波形 tick 的落点与值归属的记账
   方式与 Modbus 那套同构（改共享内存 + `SourceStore`），不需要另造一套。
3. **拿不到原始报文**：回调只给事件（`EvtCode` 实测 `0x20000`=读、`0x40000`=写，
   `EvtParam1`=区代码、`EvtParam2`=DB 号、`EvtParam3`=起始字节、`EvtParam4`=长度字节）。
   所以报文窗这一路只有**主站请求**那一行（方向统一 `rx`、摘要写"读/写 区 长度B @偏移"），
   没有 tx、也没有原始字节——这是口径 5 定下的取舍。
4. **依赖是可选的**：`python-snap7`（1.3 的 wheel 自带 `snap7.dll`）+ `setuptools<81`
   （1.3 会 import pkg_resources）。本模块**延迟判断可用性**：没装的人用 Modbus 两条路
   不该被拦，只有起 S7 通道时才报一句人话（`BackendStartError`）。

跨线程纪律：区域缓冲是"tick 在 loop 线程写、DLL 在自己的线程读"，所以写之前拿 **DLL 那把
区域锁**（`lock_area`/`unlock_area`）——一次多字节写（real/string）不加锁就可能让主站读到
半个值（spec §13 那条跨线程老账的同类）。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import time
import warnings
from types import ModuleType
from typing import Any

from ..codec.s7_codec import clamp_to_type, encode, read_bit, span_bytes, write_bit
from ..project.model import Point, parse_s7_area, s7_summary
from .base import BackendBase, BackendStartError

# S7 也是 TCP 端口：归属判定与 TCP 同一套两级探针（bind + connect），不另写一份。
from .modbus_tcp import check_port_is_free

# 三个可选模块的**类型**先声明：`except` 那一支要把它们置成 None，不声明的话 mypy 按第一
# 次赋值（Module）推，后面 `= None` 就成了赋值不符。
_snap7_common: ModuleType | None
_snap7_server: ModuleType | None
_snap7_types: ModuleType | None

try:                       # 延迟可用性：装不上依赖不该拦住"只用 Modbus 的人"
    with warnings.catch_warnings():
        # python-snap7 1.3 的 `__init__` 会 `import pkg_resources`，而 setuptools>=81 已把它
        # 标成将移除（每次 import 都刷一条 UserWarning）。我们自己知道这条的来源与约束
        # （见 README/requirements），这里压掉它——不认识它的操作员会被这条吓一跳。
        warnings.simplefilter("ignore", UserWarning)
        from snap7 import common as _snap7_common
        from snap7 import server as _snap7_server
        from snap7 import types as _snap7_types
except Exception:                                   # noqa: BLE001
    _snap7_common = None
    _snap7_server = None
    _snap7_types = None

DEPENDENCY_HINT = ('install S7 support with `pip install "python-snap7==1.3" '
                   '"setuptools<81"` (the 1.3 wheel carries snap7.dll)')

# 事件里的区代码（实测值 = **客户端侧**的 S7Area* 常量）：132=DB、131=MK、129=PE、130=PA。
# 注意与 `register_area` 要的**服务端**代码（`srvArea*`，实测 DB=5/MK=2/PE=0）不是一套，
# 两张表混用会 KeyError（实测踩过）。
_EVENT_AREA_CODES = {132: "db", 131: "mk", 129: "pe", 130: "pa"}
# 事件码：实测 0x20000 = 一次读、0x40000 = 一次写（其余是服务端自身的事件，如连接/启停）
_EVENT_READ, _EVENT_WRITE = 0x20000, 0x40000
_CPU_RUN = 8                       # snap7 的 S7CpuStatusRun
_START_TIMEOUT = 5.0


class S7Backend(BackendBase):
    """S7（ISO-on-TCP / S7comm）从站。"""

    TRANSPORT = "s7"
    """一个 S7 通道 = 一个 snap7 `Server` + 若干块按点表算出来的区域缓冲。"""

    def __init__(self, channel, packet_sink=None, on_connection=None,
                 tick_ms: int = 0, log_sink=None) -> None:
        super().__init__(channel, packet_sink=packet_sink, on_connection=on_connection,
                         tick_ms=tick_ms, log_sink=log_sink)
        self.host = channel.host
        self.port = channel.port
        self._server = None
        # `[(区名, DB 号, 服务端区代码, 区域编号, ctypes 缓冲, 该区点表)]`：区名/DB 号用来把
        # 点位对上缓冲，服务端代码/编号给 snap7 的注册与区域锁用。
        self._areas: list[tuple[str, int, Any, int, Any, list]] = []
        # 波形 tick 上一跳给每个点写进去的字节，键与 `SourceStore` 同形状（区/地址/位）。
        # `_write_point` 拿它判"缓冲还是不是我写的"，所以只在 loop 线程读写——与归属表同一条
        # 纪律，也就同样被 S7 的事件线程绕过；绕过的后果由内容比对本身兜住（判出接管只会
        # 少写一次，不会写错值）。
        self._tick_written: dict[tuple[str, int, int | None], bytes] = {}

    # ---------- 基类的钩子 ----------

    async def _probe(self) -> None:
        """S7 也是 TCP 端口：交给 TCP 那套两级探针（bind + connect）。"""
        await asyncio.to_thread(check_port_is_free, self.host, self.port,
                                self.channel_name)

    def _flatten_points(self) -> list[tuple[int, Point]]:
        """定位键固定 0：S7 按区/DB/字节寻址，主站的事件里也没有 unit 这一维
        （用 unit_id 当键的话，事件驱动的归属标记就会一个点都命中不了）。"""
        return [(0, pt) for dev in self.channel.devices for pt in dev.points]

    def _devices(self):
        """S7 没有 pymodbus 那一套数据面：区域缓冲就是数据面（`_build_areas` 建）。"""
        return

    def _make_writer(self, server):
        """S7 没有"寄存器写入器"这一层：读写在区域缓冲上直接做。"""
        return

    def _create_server(self, devices) -> Any:
        """建 snap7 服务端 + 按点表挂区域缓冲（`devices` 恒为 None：S7 不看 Modbus 那套）。"""
        # 两个都判：下面 `load_library()` 走的是 `_snap7_common`，只判 `_snap7_server`
        # 会在"只装了一半"时甩出一个不认识的 AttributeError。
        if _snap7_common is None or _snap7_server is None:
            raise BackendStartError(f"channel {self.channel_name}: S7 support is not "
                                    f"installed; {DEPENDENCY_HINT}")
        try:
            # 先把**原生库**单独加载一遍，再建 Server。为什么多这一步：`Server.__init__`
            # 里 `load_library()` 失败时它已经把 `self.pointer` 挂上了、却没有 `self.library`，
            # 而 `Server.__del__` 是 `self.destroy()` → `if self.library:` —— 于是解释器退出
            # 时还会多打一行 `Exception ignored ... has no attribute 'library'`（库里的事，
            # 它自己的 bug）。先探一下就不建那个半成品对象了；`Snap7Library` 是单例，
            # 探过之后 `Server()` 不会再加载第二次。
            #
            # 这一条为什么必须收成一句人话：模块装上了、**原生库**却没加载起来（`snap7.dll`
            # 被删、被打包漏掉、换机器时没跟着走）——snap7 是在**用的时候**才 `load_library()`，
            # 所以它炸在 import 之后，`_snap7_server is None` 那道闸拦不到它。冻结包里最容易
            # 出现的正是这一种（源码装得上、产物漏一个文件）。
            _snap7_common.load_library()
            server = _snap7_server.Server()
        except (AttributeError, RuntimeError) as exc:
            raise BackendStartError(
                f"channel {self.channel_name}: snap7.dll cannot be loaded ({exc}); "
                f"{DEPENDENCY_HINT}") from exc
        self._areas = _build_areas(self.channel)
        for _name, _db, area_code, index, buffer, _points in self._areas:
            server.register_area(area_code, index, buffer)
        server.set_events_callback(self._on_event)
        server.set_cpu_status(_CPU_RUN)
        return server

    def _start_server(self, server):
        """snap7 的 `start_to()` 是**同步**绑端口：没有长活任务可等，回 None。"""
        try:
            server.start_to(self.host, self.port)
        except Exception as exc:
            raise BackendStartError(
                f"channel {self.channel_name}: cannot listen on {self.host}:{self.port} "
                f"for S7 ({exc}); the port may already be in use") from exc
        return

    async def _await_ready(self, server, task) -> None:
        """`start_to` 是同步的：走到这里端口已经绑上了，确认一句 status 就够。"""
        status = server.get_status()
        if not status or not status[0]:
            raise BackendStartError(
                f"channel {self.channel_name}: the S7 server on {self.host}:{self.port} "
                f"did not start (status {status!r})")

    async def _shutdown_server(self, server) -> None:
        """`stop()` 关监听、`destroy()` 放掉 C 侧对象（socket 与客户端连接一起收）。"""
        server.stop()
        server.destroy()

    def describe(self) -> str:
        """主站该连哪：`host:port rack r slot s`（与命令行那一行同一句）。"""
        return s7_summary(self.channel)

    @property
    def endpoints(self) -> list[tuple[str, int]]:
        """S7 只有一个 `host:port`（没有"额外监听"那回事）。"""
        return [(self.host, self.port)]

    def _require_ready(self) -> None:
        if self._server is None:
            raise RuntimeError(f"channel {self.channel_name} is not started")

    def _clamp(self, value, pt):
        """S7 那套区间（word/real/...），与 Modbus 的 uint16/float32 那套分开。"""
        return clamp_to_type(value, pt.datatype)

    def _write_point(self, key, pt, value) -> bool:
        """引擎侧写值（波形 tick）= 改那块区域缓冲（主站下一次读就看得见，实测过）。

        写之前先看缓冲**还是不是上一跳自己写的那串字节**：不是就说明主站已经改过，于是这一跳
        一个字都不写、当场把该点标成接管并出声。

        为什么要多这一次比对：主站的写与它的**事件**之间实测有中位 0.65ms 的间隔（snap7 在它
        自己的线程上回调，见 `_on_event`），而波形节拍只看归属表、不看事件。所以"主站已经落了
        盘、归属却还归曲线"这个中间态每一拍都可能出现——1000 次主站写里约 10 次会被这一跳顶掉
        （2026-10-16 实测，`docs/superpowers/notes/s7_ownership_race_probe.py`）。事件靠不住，
        缓冲不骗人：内容比对用的是写入那一刻就能同步看到的证据。

        没有记录（这个点第一次被 tick 写，或上一次刚判出接管）就照常写：那时没有"本该是什么"
        可比，而启动与恢复之后本来就该由曲线接手。

        读回与写在**同一次**区域锁里做：实测主站的服务线程写区域时也取这把锁（持锁 3s 期间
        pys7 的 write 落不进去、最后超时），所以一次锁 = 检查与写之间没人插得进来；取两次锁的
        话，那条缝里落进来的主站值仍会被这一次写覆盖掉。`own()`、日志、编码一律留在锁外——
        持锁期间主站是**阻塞**的，把慢东西放进锁里就等于把它的超时变大。
        """
        try:
            raw = encode(value, pt.datatype, span_bytes(pt.datatype, pt.initial))
        except ValueError:
            return False                     # 编不出来（坏点表）：tick 的逐点兜会记一条日志
        entry = self._area_of(pt)
        if entry is None or self._server is None:
            return False                     # 地址不在任何已注册的块上，与 write_point_bytes 同口径
        identity = (pt.area, pt.address, pt.bit)
        mine = self._tick_written.get(identity)
        wrote = False
        with self._area_lock(entry):
            present = self._read_naked(entry, pt)
            stale = mine is not None and present is not None and present != mine
            if not stale:
                wrote = self._write_naked(entry, pt, raw)
        if stale:
            # 与事件那条路用同一个键与同一个跨度（bool 占 1、位跟着走），否则两条路会把
            # 同一个点记成两段，`manual_count` 与"恢复波形"就会各说各话。
            span = 1 if pt.datatype == "bool" else span_bytes(pt.datatype, pt.initial)
            self._sources.own(self.channel_name, key, pt.area, pt.address,
                              count=span, who="master", bit=pt.bit)
            del self._tick_written[identity]
            self._emit("warning", "takeover", (key, pt.area, pt.address),
                       f"{pt.name} ({pt.area} {pt.address}, bit {pt.bit}) was already "
                       f"rewritten by the master before its event arrived; the waveform "
                       f"handed this point back", time.monotonic())
            return False
        if not wrote:
            return False
        self._tick_written[identity] = raw
        return True

    def _read_naked(self, entry, pt) -> bytes | None:
        """不带锁读这个点占的那几位。**只在已经持有时调用**。

        拆出来是为了让 `_write_point` 能把"读回比对"与"写"放进**同一次**区域锁：`lock_area`
        实测**不可重入**（同一线程连着取第二次会直接阻塞），所以已经持锁时绝不能再调
        `read_point_bytes` / `write_point_bytes` 那两个各自加锁的公开方法——那是自死锁，
        症状恰好就是本仓库 2026-10-11 那次"引擎不回话 + 连片超时"的形状。
        """
        _name, _db, _code, _index, buffer, _points = entry
        if pt.datatype == "bool":
            if pt.bit is None:
                return None
            index = pt.address * 8 + pt.bit
            if index >= len(buffer) * 8:
                return None
            return b"\x01" if read_bit(buffer, index) else b"\x00"
        span = span_bytes(pt.datatype, pt.initial)
        if pt.address + span > len(buffer):
            return None
        return bytes(buffer[pt.address:pt.address + span])

    def _write_naked(self, entry, pt, raw: bytes) -> bool:
        """不带锁把字节写进区域缓冲（`bool` 只动那一位）。**只在已经持有时调用**。"""
        _name, _db, _code, _index, buffer, _points = entry
        if pt.datatype == "bool":
            if pt.bit is None:
                return False
            write_bit(buffer, pt.address * 8 + pt.bit, bool(raw[0]) if raw else False)
            return True
        if pt.address + len(raw) > len(buffer):
            return False                     # 地址不在块内（点表与缓冲不一致）
        ctypes.memmove(ctypes.byref(buffer, pt.address), raw, len(raw))
        return True

    def restore_waveform(self, unit_id: int, area: str, address: int,
                         count: int = 1, bit: int | None = None) -> bool:
        """交还波形：归属清掉的同时，"我上次写进去的是哪串字节"也必须一起忘掉。

        不忘掉就永远恢复不了：接管期间的每一跳 tick 都在 `is_waveform` 那一步被挡住，
        `_write_point` 压根没跑，那份记录还停在接管**之前**的波形值上；恢复之后第一跳一看
        缓冲里是主站的值、记录里是自己的旧值，就判定"又被主站写了"，当场把刚恢复的点重新标
        成接管——操作员按了"恢复波形"，曲线却永远不接手。钉住这个形状的是
        `test_the_tick_writes_into_the_area_and_respects_ownership` 的最后一步。

        跨度与 `SourceStore.restore` 逐字节同口径（`bit` 只作用在基础地址那一个上），两条路
        对"这一段有多大"必须给同一个答案。
        """
        handed_back = super().restore_waveform(unit_id, area, address, count=count, bit=bit)
        if handed_back:
            for offset in range(max(1, int(count))):
                self._tick_written.pop((area, address + offset,
                                        bit if offset == 0 else None), None)
        return handed_back

    def read_point_bytes(self, pt) -> bytes | None:
        """读一个点占的那几个字节（`bool` 回一个字节的 `00`/`01`）。

        没起、或这个点的地址不在任何一块已注册的缓冲里，一律回 `None`——调用方（`Session`）
        据此把那一格标 `stale`，不许拿初值冒充"读到了"。
        """
        entry = self._area_of(pt)
        if entry is None or self._server is None:
            return None
        with self._area_lock(entry):
            return self._read_naked(entry, pt)

    def write_point_bytes(self, pt, raw: bytes) -> bool:
        """把一个点占的那几个字节写进区域缓冲：`bool` 只动那一位，其余整段覆盖。

        `Session.set_point`（界面改值）走这一条。波形 tick 不走这里——它要把"读回比对"与
        "写"放进**同一次**区域锁，走 `_write_point`；两条路对"写进哪里"仍是同一个答案
        （都落在 `_write_naked` 上），各写一份 memmove 迟早漂。
        """
        entry = self._area_of(pt)
        if entry is None or self._server is None:
            return False
        with self._area_lock(entry):
            return self._write_naked(entry, pt, raw)

    # ---------- 主站事件 ----------

    def _on_event(self, event) -> None:
        """snap7 的事件回调（**在 DLL 自己的线程上被调**）：报文事件 + 写事件的归属记账。

        只认读/写那两种事件码：其余（连接、启停、内部通知）不是"主站动了哪一段"，混进来只会
        把报文窗刷成噪声。`EvtRetCode != 0` 的事件是**被拒**的请求——与 M4 那条"被协议拒的写
        不切 manual"同一个理由：一次没成功的写不该把曲线停掉。写事件还要顺手把这一段标成
        manual（`_own_intersecting`），这是 spec §7 的归属唯一入口。
        """
        code = int(getattr(event, "EvtCode", 0) or 0)
        if code not in (_EVENT_READ, _EVENT_WRITE):
            return
        if int(getattr(event, "EvtRetCode", 0) or 0) != 0:
            return                                    # 被拒的请求：一次没写成就别停曲线
        area = _EVENT_AREA_CODES.get(int(getattr(event, "EvtParam1", 0) or 0))
        if area is None:
            return                                    # 不认识的区（CT/TM 之类）：不理
        db = int(getattr(event, "EvtParam2", 0) or 0)
        start = int(getattr(event, "EvtParam3", 0) or 0)
        size = int(getattr(event, "EvtParam4", 0) or 0)
        area_name = area if area != "db" else f"db{db}"
        self._report_master(code == _EVENT_WRITE, area_name, start, size)
        if code == _EVENT_WRITE:
            self._own_intersecting(
                0, area_name, start, size,
                span_of=lambda pt: (1 if pt.datatype == "bool"
                                    else span_bytes(pt.datatype, pt.initial)))

    def _report_master(self, is_write: bool, area_name: str, start: int,
                       size: int) -> None:
        """往报文 sink 发一行主站请求（口径 5：S7 这一路只有 rx 事件行、没有原始字节）。"""
        if self._packet_sink is None:
            return
        verb = "写" if is_write else "读"
        summary = f"{verb} {area_name} {size}B @{start}"
        # 六个位置与 Modbus 那条 sink 合同一致（方向、原始字节、unit、fc、摘要、时刻）；
        # S7 拿不到原始字节（原生库不交出来），所以 raw 是空的、unit/fc 用 None——
        # "没有就是没有"，不许拿摘要反填一个看起来像真的 hex 出去。
        self._packet_sink("rx", b"", None, None, summary,
                          time.monotonic() - self.started_at)

    # ---------- 内部 ----------

    def _area_of(self, pt):
        """这个点位落在哪一块缓冲上（按**区名 + DB 号**对，不拿代码反查——服务端代码与
        事件里的客户端代码是两套，混用会 KeyError）。"""
        area = parse_s7_area(pt.area)
        if area is None:
            return None
        for entry in self._areas:
            name, db = entry[0], entry[1]
            if name == area[0] and (name != "db" or db == area[1]):
                return entry
        return None

    @contextlib.contextmanager
    def _area_lock(self, entry):
        """写区域缓冲前拿 DLL 那把区域锁（跨线程读写同一块内存的纪律）。"""
        _name, _db, code, index, _buffer, _points = entry
        locked = False
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.lock_area(code, index)
                locked = True
        try:
            yield
        finally:
            if locked:
                with contextlib.suppress(Exception):
                    self._server.unlock_area(code, index)


def _build_areas(channel) -> list[tuple[str, int, Any, int, Any, list]]:
    """按点表算每个区要多大、把初值填进去、建成 ctypes 缓冲。

    区按 `(区名, DB 号)` 分组（S7 的地址是"哪个区的第几个字节"）；缓冲区大小 = 该区最靠后的
    那个点占到的末尾（`offset + span_bytes`），**不留余量**：主站读到我们没声明过的字节就该
    得到"未定义地址"，而不是一个看着合法的 0。

    返回 `[(区名, DB 号, 服务端区代码, 区域编号, 缓冲, 点表)]`——区名与 DB 号是我们自己的
    寻址口径，服务端区代码/编号只给 snap7 的注册与区域锁用。
    """
    groups: dict[tuple[str, int | None], list] = {}
    for dev in channel.devices:
        for pt in dev.points:
            area = parse_s7_area(pt.area)
            if area is not None:
                groups.setdefault(area, []).append(pt)
    areas = []
    for area, points in groups.items():
        end = max(pt.address + _point_span(pt) for pt in points)
        buffer = (ctypes.c_ubyte * max(1, end))()
        for pt in points:
            _seed_point(buffer, pt)
        code = getattr(_snap7_types, _AREA_ATTR[area[0]])
        areas.append((area[0], area[1] or 0, code, area[1] or 0, buffer, points))
    return areas


_AREA_ATTR = {"db": "srvAreaDB", "mk": "srvAreaMK", "pe": "srvAreaPE", "pa": "srvAreaPA"}


def _point_span(pt) -> int:
    if pt.datatype == "bool":
        return 1
    return span_bytes(pt.datatype, pt.initial)


def _seed_point(buffer, pt) -> None:
    """把点表初值写进缓冲（主站一开始就该读到点表里的数，而不是一片 0）。"""
    if pt.datatype == "bool":
        if pt.bit is None:
            return
        try:
            write_bit(buffer, pt.address * 8 + pt.bit, pt.initial)
        except ValueError:
            return                                    # 初值不是 bool 词：validate 会报，这里不炸
        return
    try:
        raw = encode(pt.initial, pt.datatype, _point_span(pt))
    except ValueError:
        return                                        # 初值编不出来（坏点表）：交给 validate
    if pt.address + len(raw) > len(buffer):
        return
    ctypes.memmove(ctypes.byref(buffer, pt.address), raw, len(raw))
