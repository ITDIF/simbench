"""后端共用的那一半：与传输无关的启停骨架、波形 tick、值归属、报文与日志钩子。

**名字**（2026-10-16 改）：本文件原先叫 `modbus_base.py`、类叫 `ModbusBackendBase`。它承载的是
"与传输无关的那一半"，而 S7 也继承它（`s7_server.py` 的 `S7Backend`）——S7comm 不是 Modbus，
名字里带 Modbus 会让读者以为这一层只管 Modbus 那几条路，它的文档串还得反过来写一句
"与传输无关"来纠正自己。现在按它**实际的身份**叫：`backend/base.py` 的 `BackendBase`。

为什么单独成文件（M5 Task 3）：TCP 与 RTU 的后端除了"怎么建 server / 怎么确认起来了 /
怎么把字节块切成帧"这三件事之外**完全一样**——点位摊平、波形算值与钳位、值归属
（`SourceStore`）、运行期日志去重、raw_writer 读写、启停顺序。散文式的重复在这里的代价
不是行数，是漂移：波形那一套有几十条用例钉着，第二份一旦落后一条，串口那一路就会
"看起来也能跑，但曲线/接管的行为和 TCP 不一样"。

因此：**本文件不 import pymodbus**（建 server 是子类的事），也不认识任何具体的
framer/地址/串口参数。子类只填五个钩子（`_probe` / `_create_server` / `_await_ready` /
`_split_frames` / `_parse_frame`），TCP 子类另外覆盖 `_open_extra` / `_close_extra` /
`endpoints` / `describe`。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from ..codec.point_codec import encode
from ..codec.waveform import clamp_to_datatype, waveform_value
from ..project.model import Point, Project, area_is_bit
from .events import PacketEvent
from .ownership import LogDedupe, SourceStore, write_targets
from .raw_writer import RawRegisterWriter
from .simdev import build_sim_devices

_NOISE_PREFIX = "Datastore unable to fulfill request"
# 只挡"未定义 unit id 回落 devices[0] 抛 KeyError: 0"这一种已知噪音：三个片段必须**同时**
# 出现才抑制（前缀 + 异常本身 + 抛出它的那一帧）。
_UNIT_FALLBACK_MARKS = (_NOISE_PREFIX, "KeyError: 0", "__get_device")
# 停通道等它收尾的上限（"超过 5 秒就是坏了"，真值见 `stop` 的注释）。起那一边的上限
# 在各自子类里（TCP 的 `_await_ready`、RTU 的同一支），因为"起来了"的判据本来就不一样。
_STOP_TIMEOUT = 5.0


class BackendStartError(RuntimeError):
    """这一路起不来，而且原因在"资源不归我们"（端口被占、串口打不开……）。

    两个传输的拒绝都从这里派生，调用方（`__main__` 的那一行错误、界面的 ▲ 失败）
    因此可以只认这一个基类，不必逐个传输去列举。
    """


class _DropDatastoreNoise(logging.Filter):
    """压掉"未定义 unit id 扫总线"那一条栈，但**只**压那一条。

    协议行为是对的（主站收到异常码 4，服务不崩，"已实测事实"4），一次 1..247 扫描会刷
    492 行栈，所以要压。但判据不能按消息前缀一刀切：`requesthandler.py` 打这句的地方是
    一个裸 `except Exception`，datastore 路径上**任何**异常都汇到同一句、都回异常码 4。
    按前缀判就会把模拟器自己的崩溃一起吞掉——真出内部 bug（M4 波形算出坏寄存器、点表映射
    越界）时主站只看到 4，日志一条不剩，而"异常码 4"与"unit 不在线"在报文上完全同形，
    现场根本分辨不出来（spec §13 的第一风险）。这条边界由
    `test_internal_datastore_failure_is_not_swallowed` 钉住。

    为什么按文本判、不按 record 结构判：pymodbus 的 `Log.error(fmt, *args)` 在自己的
    `Log._logger.log(...)` 里已经把 `{}` 填成完整字符串再发记录，实测 `record.args == ()`
    ——拿不到异常对象、也拿不到栈对象，只剩文本可读。被抑制那一条的文本实测长这样：
    `Datastore unable to fulfill request: 0; Traceback ... return self.devices[device_id]
    if device_id in self.devices else self.devices[0] ... KeyError: 0`，
    所以三个片段合起来指向的就是 `SimCore.__get_device` 那一帧的 `KeyError: 0`。

    **版本耦合**：这三段文本是 pymodbus 3.15.0 的实测形状。升级 pymodbus 时这条判据必须
    重新验一遍——文案或帧名一变，抑制就会失效（表现是日志重新刷屏，属于可见的退化，
    不会悄悄把真异常吞掉；反方向失效不可能，因为判据是"三句都对上才压"）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        text = str(record.getMessage())
        return not all(mark in text for mark in _UNIT_FALLBACK_MARKS)


def configure_library_logging(level: int = logging.ERROR) -> None:
    """把 pymodbus 的报文级日志压到 ERROR，并丢掉未定义 unit id 那一条栈。

    logger 名字是实测出来的：`Log._logger = logging.getLogger(__name__)`，而 `__name__`
    是 `pymodbus.logging`（不是 `pymodbus`）；`pymodbus_internal` 是 pymodbus 自己另外用的
    一个。三个都设级别 + 挂 filter：`logging` 只在**发出记录的那个 logger** 上跑 filter
    （父 logger 的 filter 不参与传播），所以 `pymodbus.logging` 这一条才是真正起作用的。

    filter 必须防重复挂载：`start()` 会被多次调用（GUI 停再起、每个通道各起一次），
    `addFilter` 是追加语义，重复挂就在 filter 列表里堆一串同类的东西。
    """
    for name in ("pymodbus", "pymodbus.logging", "pymodbus_internal"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not any(isinstance(f, _DropDatastoreNoise) for f in logger.filters):
            logger.addFilter(_DropDatastoreNoise())



class BackendBase:
    """传输无关的那一半：摊平点表、波形 tick、值归属、报文/日志钩子、启停的骨架。

    为什么要抽这一层（M5 Task 3）：波形算值、值归属、钳位去重、raw_writer 这几套有几十条
    用例钉着，串口那一路上再抄一份就是"两份判定迟早漂移"的老路。子类只填五个钩子——
    `_probe()`（起之前的归属探针）、`_create_server(devices)`、`_await_ready(server, task)`、
    `_split_frames(raw)`（把一次到达的字节块切成整帧）、`_parse_frame(frame)`（帧 -> 摘要）
    ——加上 TCP 那份"多挂几个监听"的 `_open_extra()`/`_close_extra()`。
    """

    #: PDU 在 raw 里的起点：`write_targets` 靠它找地址与数量。Modbus TCP 是 MBAP 之后的
    #: 第 7 字节，RTU 是单元号之后的第 1 字节；子类覆盖。
    _pdu_offset = 7

    def __init__(self, channel, packet_sink=None, on_connection=None,
                 tick_ms: int = 0, log_sink=None) -> None:
        self.channel = channel
        self.channel_name = channel.name
        self.started_at = 0.0
        # 默认 0 = **不起 tick 任务**：M1/M2 的既有全部用例形状因此一个字没变，只有
        # `Session.start_channel` 与无头 CLI（`__main__._serve`）会把工程的 `tick_ms` 交下来。
        self.tick_ms = tick_ms
        self._packet_sink = packet_sink
        self._on_connection = on_connection
        # `(level, text)` 回调，和 `packet_sink` 一样在 **loop 线程**上被调：报错了要能
        # 落到界面/CLI，但曲线不能因此停（`_emit` 那一道就是为这件事设的）。
        self._log_sink = log_sink
        # 同点同类 60s 一条（spec §8）：本 backend 一份表，所以 key 里不用再带通道名。
        self._dedupe = LogDedupe()
        # 真类型是 pymodbus 的 server（TCP）或 SerialServer（RTU）——这一层**刻意**
        # 不 import pymodbus（tests/test_architecture.py 守着那条线），所以注解写成
        # `Any`：写一个不存在的类名只会让静态检查报「未定义的名字」。
        self._server: Any | None = None
        self._writer: RawRegisterWriter | None = None
        self._task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._tick_started = 0.0
        # 值归属只在 loop 线程写（`Session` 把 GUI 的命令用 runner.call 投回来）；
        # 读（`source_is_waveform`）给主线程的 10Hz 刷新用，与 `_read_back` 同口径。
        self._sources = SourceStore()
        # 摊平的点位表（`[(定位键, Point)]`）：Modbus 的定位键是 **unit_id**，S7 一路填 0
        # （它不上线 unit id，寻址全靠点自己的区/DB/地址）。每次 tick / 每条写报文都只遍历
        # 这一份，不再爬 project 对象树——与 `Session.load()` 一次建索引同一个理由。
        self._wave_points: list[tuple[int, Point]] = []

    async def start(self) -> None:
        """建 SimDevice -> 起监听 -> 起 tick。重复调用是安全的（已经在跑就直接返回）。"""
        if self._server is not None:
            return
        # "这个资源归谁"先自己确认：pymodbus 证明不了这件事，让第二个实例"启动成功"
        # 就等于把主站的连接留给上一个僵尸进程。探针是**阻塞调用**，子类必须在
        # `_probe()` 里用 `asyncio.to_thread` 包住（实测：在 loop 上直接跑时，探针那 300ms
        # 里同 loop 的其它协程推进 0 次，GUI 的每次改值、每条报文都会被一次启动冻住）。
        await self._probe()
        configure_library_logging()
        # 摊平要在监听起来**之前**：`_await_ready` 里有 await，其间到的第一条写报文
        # 就会对着空表判"这一点不归我管"，接管漏判漏得没有声音。
        self._wave_points = self._flatten_points()
        server = self._create_server(self._devices())
        task = self._start_server(server)
        try:
            await self._await_ready(server, task)
            self._extra_servers = await self._open_extra(server)
        except BaseException:
            # 起不来就不留半个后端：先把已经支起来的监听全收干净，再把真实异常原样抛给
            # 调用方。否则 self._server 还留着，stop() 会对一个从没监听过的 server 再
            # shutdown 一次，额外那几个监听则连收的人都没有（端口占到进程结束）。
            await self._close_extra()
            with contextlib.suppress(Exception):
                await self._shutdown_server(server)
            if task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, _STOP_TIMEOUT)
            raise
        self._server = server
        self._writer = self._make_writer(server)
        self._task = task
        self.started_at = time.monotonic()
        # 曲线的 0 相位与监听同时起（spec §5：`t` 是引擎启动后的单调秒，不是系统时间）。
        self._tick_started = self.started_at
        # 去重表跟着相位一起归零：重起通道通常就是操作员照着那句日志改了点什么，
        # 上一轮报过的"这个点在飘"不许把这一轮的同一句话压成一分钟静音。
        self._dedupe.reset()
        if self.tick_ms > 0:
            self._tick_task = asyncio.create_task(
                self._tick_loop(self.tick_ms / 1000.0))

    # ---------- 子类要填的五个钩子 ----------

    async def _probe(self) -> None:
        """起之前先自己确认"这个资源归我们"；拿不到就抛 `BackendStartError` 的子类。"""
        raise NotImplementedError

    def _make_writer(self, server):
        """读写入口背后的那层（Modbus：`RawRegisterWriter(server.context)`；S7 回 None）。

        S7 的读写在区域缓冲上直接做（`_write_point` / 事件里的 `_own_intersecting`），
        没有"寄存器写入器"这一层——`server.context` 那个属性在 snap7 的服务端上也不存在。
        """
        return RawRegisterWriter(server.context)

    def _flatten_points(self) -> list[tuple[int, Point]]:
        """把点表摊平成 `[(定位键, Point)]`：Modbus 用 **unit_id**（写寄存器要用它），
        S7 固定用 0——它按区/DB/字节寻址，主站的事件里也没有 unit 这一维。

        为什么是钩子：这张表的**键**决定归属记账对不对（`own` 与 `is_waveform` 必须用同一个
        键）。S7 后端一开始自己在 `__init__` 里建了 `[(0, pt)]`，却被 `start()` 这行覆盖成
        `unit_id`，于是"主站写过的点没被标成 manual"——曲线下一拍就把主站的值顶回去
        （实测踩过，属于 spec §7 那类事故）。
        """
        return [(dev.unit_id, pt) for dev in self.channel.devices for pt in dev.points]

    def _devices(self):
        """这一路的数据面（Modbus：`build_sim_devices` 摊出来的 `SimDevice` 列表；S7 回 None）。

        为什么是钩子：`build_sim_devices` 是 **Modbus** 的 SimDevice 构造器，喂进 S7 的点位
        （区名 `db1`、类型 `real`）会当场 `ValueError: unknown area`——两种传输的数据面本来
        就不同（一边是寄存器块，一边是区域缓冲），在这里分开比在构造器里加 if 干净。
        """
        return build_sim_devices(Project(channels=[self.channel]))

    def _create_server(self, devices):
        """建这一路的 server（同步；`devices` 是 `_devices()` 给的，S7 拿到的恒为 None）。"""
        raise NotImplementedError

    def _start_server(self, server):
        """让 server 开始服务，返回它的长活任务（没有长活任务的实现回 None）。

        默认是 **pymodbus 那一套**（TCP 与 RTU 两个后端共用）：`create_task(serve_forever())`。
        S7 覆盖成 snap7 的同步 `start_to()`（没有任务可等，回 None）。
        """
        return asyncio.create_task(server.serve_forever())

    async def _shutdown_server(self, server) -> None:
        """收 server。默认（pymodbus）：`await server.shutdown()`；S7：`stop()` + `destroy()`。"""
        await server.shutdown()

    async def _await_ready(self, server, task) -> None:
        """等"真的起来了"：起不来就把真实异常抛出去，不许"start 成功但外面连不上"。"""
        raise NotImplementedError

    def _split_frames(self, raw: bytes):
        """一次到达的 rx 字节块 -> 逐帧产出（切不出来的部分到此为止，不猜）。"""
        raise NotImplementedError

    def _parse_frame(self, frame: bytes) -> tuple[int | None, int | None, str]:
        """一整帧 -> `(unit_id, function_code, 给人看的摘要)`。"""
        raise NotImplementedError

    @property
    def endpoints(self) -> list[tuple[str, int]]:
        """在听的 `(host, port)`：串口没有这一回事，回空列表（TCP 子类覆盖）。"""
        return []

    async def _open_extra(self, server) -> list:
        """额外监听（只有 TCP 那一路有）；基类什么都不挂。"""
        return []

    async def _close_extra(self) -> None:
        """关掉额外监听；基类没有。"""
        return

    def describe(self) -> str:
        """状态栏/日志里"主站该连哪"那一句（TCP 是所有监听地址，串口是串口参数）。"""
        where = [f"{host}:{port}" for host, port in self.endpoints]
        if len(where) > 3:
            return "、".join(where[:3]) + f" 等 {len(where)} 个"
        return "、".join(where)

    async def stop(self) -> None:
        """关监听并等 serve_forever 收尾。幂等：没起过、重复停都安全返回。

        实测：`shutdown()` 之后 serve_forever 在 0.2ms 内自己返回，**不需要** `task.cancel()`。
        计划里那句 cancel 会被 serve_forever 自己的 `with suppress(CancelledError)` 吞掉，
        task 最终仍是"正常结束"（`cancelled()` 为 False），状态反而更难读；所以这里只等它
        结束，等不到才算错（TimeoutError 会带着端口名抛出去，比留下一个活 task 好查）。
        """
        # tick 任务排在**第一件事**：本方法后面会把 `_writer` 置 None，而 `_tick_once` 要
        # 用它。顺序一反就变成"停通道时崩一次"（任务里未捕获的异常没人接），而且崩完
        # 通道还在跑——主站读到的是已经不该再存在的模拟量，比报错更难查。
        if self._tick_task is not None:
            self._tick_task.cancel()
            self._tick_task = None
        # 额外监听先关：先断新连接，再收主 server（它的 shutdown 会把各地址上的连接收干净）
        await self._close_extra()
        server, task = self._server, self._task
        self._server = None
        self._writer = None
        self._task = None
        if server is not None:
            await self._shutdown_server(server)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, _STOP_TIMEOUT)

    def _trace_packet(self, is_outgoing: bool, payload: bytes) -> bytes:
        """pymodbus 的报文钩子：**必须把字节原样返回**（实测改 payload 就是改线上数据）。

        交给 sink 的顺序是 `(direction, raw, unit_id, function, summary, mono)`，与
        `events.PacketEvent` 去掉 channel 后的字段顺序一致，Task 11 与 M3 的报文窗都按
        这个位置消费。sink 里抛的异常原样冒出去：静默丢一包会让报文窗"看起来没流量"，
        比炸出来更难查。

        rx 那一路还要顺手认一次"主站在写哪个点"（`_note_master_writes`）：归属状态必须
        和报文同线程、同一次到达落地，晚一步就是"平台下发了、波形又顶回去"。
        """
        raw = bytes(payload)
        if not is_outgoing:
            self._note_master_writes(raw)
        if self._packet_sink is not None:
            unit, function, summary = self._parse_frame(raw)
            self._packet_sink("tx" if is_outgoing else "rx", raw, unit, function,
                              summary, time.monotonic() - self.started_at)
        return payload

    def _note_master_writes(self, raw: bytes) -> None:
        """一次到达的 rx 字节块 -> 切成帧 -> 逐帧把落在点表上的点标成 manual。

        为什么要自己切帧：pymodbus 在**组帧之前**就 trace，一个块里可能挤了两条写请求
        （实测 24 字节一次到达），而 `write_targets` 按契约只解**第一条**，剩下那条就白写
        不接管——而且漏得没有声音（spec §7 正是防"下发没生效"这类误判的）。

        为什么不给自己留"切不完的余量"（trace 的实测形状决定，别照直觉改回去）：
        `trace_packet` 拿到的是 transport 那一侧**累计**的缓冲，半个帧会在下一次连同新到的
        字节**整段重放**（先到 10 字节、再到 2 字节 → 第二次给的是完整的 12 字节）。所以
        每次都从第 0 字节切才对；自己再留一份余量拼回去，等于把已经见过的字节数两遍，
        之后每一条真帧都会对错偏移。

        `length` 不在 2..254 之内就不是 Modbus TCP 帧（PDU 最长 253 + unit id 1）：整块到此
        为止，不猜下一帧从哪儿开始——撒谎的长度字段同样会把 pymodbus 自己的组帧卡住，
        那次写压根落不到寄存器上，这里不替它标接管。
        """
        for frame in self._split_frames(raw):
            self._own_write_frame(frame)

    def _own_write_frame(self, frame: bytes) -> None:
        """一整帧 rx 报文 -> 命中的点整段切成 manual（点表里没有的格子一律不理）。"""
        unit, function, summary = self._parse_frame(frame)
        # `pdu_offset` 必须按传输给对：`write_targets` 从它开始找地址与数量，差几个字节
        # 就是把"写哪个点"认到别的偏移上（TCP 是 7、RTU 是 1）。
        event = PacketEvent(self.channel_name, "rx", frame, unit, function, summary,
                            time.monotonic() - self.started_at,
                            pdu_offset=self._pdu_offset)
        for unit_id, area, address, count in write_targets(event) or ():
            # 标记这一步与 S7 共用（`_own_intersecting`）：三条口径（只读点跳过、点表外不理、
            # 重叠判定）都写在那一处，这里只把 Modbus 的"跨度"告诉它。
            self._own_intersecting(unit_id, area, address, count,
                                   span_of=lambda pt: pt.span)

    def _trace_connect(self, is_new: bool) -> None:
        """连接建立/断开回调：实测一次"连上→读→关"正好是 ("open", "close") 两次。"""
        if self._on_connection is not None:
            self._on_connection("open" if is_new else "close")

    async def _tick_loop(self, interval: float) -> None:
        """引擎 loop 上的波形节拍。

        按点兜的三种异常在 `_tick_once` 里；这一道是**故意瞎捕**的最后一层，与本仓库
        普遍"只捕具体类型"的口径相反，理由要写明白：走到这里的都是没预料到的
        （`_require_writer` 的 RuntimeError、writer 内部的 IndexError、点表与 SimDevice
        不一致……），而这里"捕漏了"的代价不是少一条日志，是**任务死亡**——曲线静默停、
        通道照旧应答、界面照旧 10Hz 刷新，正是 spec §8"运行期错误要有一条日志"要防的那类
        假象。兜住之后这一轮丢掉几个点的写入、下一轮照旧，并且 60s 内留一句带通道名的话。

        不写 `except BaseException`：`CancelledError` 必须原样逃出去，`stop()` 靠把任务
        取消来收尾（Task 3 的顺序用例钉着 `task.cancelled()`）。
        """
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            try:
                self._tick_once(now)
            except Exception as exc:                    # noqa: BLE001
                self._report_tick_failure(exc, now)

    def _tick_once(self, now: float) -> int:
        """把"还在波形手上"的点算一遍值写进寄存器，返回真写掉的点数。

        `t = now - self._tick_started`：重新起通道曲线就从 0 相位开始，改系统时间
        影响不到它（spec §5）。`now` 由调用方给，测试才注得进固定时间戳——它同时是
        去重表的时间戳，所以"60s 只留一条"也能被 `_tick_once(now=61.0)` 这样验，
        不必有人真睡一分钟。
        """
        self._require_ready()
        written = 0
        t = now - self._tick_started
        for key, pt in self._wave_points:
            if not self._sources.is_waveform(self.channel_name, key, pt.area,
                                             pt.address):
                continue                                  # spec §7：被接管就一个字都不写
            if pt.waveform in ("", "const") and not pt.wave_param:
                continue                                  # W4：空 const 不参与
            stage = "compute"
            try:
                value = waveform_value(pt.waveform, pt.wave_param, t)
                # 先把值钳进这个点位类型的区间再编码——写一个被 `& 0xFFFF` 截过的错值比不报
                # 日志严重（主站读到的是"合法但完全错误"的遥测量）。报告必须排在这个 try
                # **之内**：那句日志自己出故障时，代价不许越过"这一个点的这一轮"（spec §8
                # 只要求运行期有一条日志，没要求拿整轮写入去换），
                # `_emit` 里那道 sink 兜底是同一件事的另一半。
                # "钳到哪个类型的哪个区间"是**传输相关**的（Modbus 是 uint16/float32 那套、
                # S7 是 word/real 那套），所以走钩子。
                value, clamped = self._clamp(value, pt)
                if clamped:
                    self._report_clamp(key, pt, value, now)
                # 从这里起再炸就不是"算值"的问题了：分类只影响日志那一句怎么说，
                # 而两种错要改的东西不一样（曲线参数 vs 点表的类型）。
                stage = "encode"
                wrote = self._write_point(key, pt, value)
            except (KeyError, ZeroDivisionError, ValueError) as exc:
                # 逐点兜：未知波形名是 KeyError、`period=0`/`speed=0` 是 ZeroDivisionError、
                # 参数与编码是 ValueError（Task 1 实测三种都会到这里来）。只跳过这一个点——
                # 异常冒进 asyncio 任务就是"波形停了而通道还在跑"，而整轮中断还会让同通道
                # 其余点位一起冻住。这三种**不许往窄里收**，也不许像 Task 3 那样只 `continue`：
                # 一个坏点从此不刷新而日志一句都没有，操作员只剩"挨个点位猜哪个坏了"这条路。
                self._report_failure(stage, key, pt, exc, now)
                continue
            if not wrote:
                continue          # 地址不在块内（点表与 SimDevice 不一致）：静默跳下一个点
            written += 1
        return written

    # ---------- 传输相关的三个钩子（Modbus 的默认实现就是原来那段代码） ----------

    def _require_ready(self) -> None:
        """写之前先确认这一路是起着的。Modbus：有 writer；S7 子类覆盖（它没有 writer）。"""
        self._require_writer()

    def _clamp(self, value, pt):
        """把算出来的值钳进这个点位类型的区间，返回 `(值, 是否钳过)`。

        Modbus 默认走 `clamp_to_datatype`（uint16/float32 那套区间）；S7 子类覆盖成 S7 类型
        那一套（word/real/...）——两套区间不一样，硬塞进一个函数就是靠 if 缝两种语义。
        """
        return clamp_to_datatype(value, pt.datatype)

    def _write_point(self, key, pt, value) -> bool:
        """把这个点的值写进从站内存，返回是否真写进去。

        Modbus 默认：编码成 reg16 序列 + `raw_writer` 写寄存器/位。S7 子类覆盖成"编码成
        字节 + 写区域缓冲"。`key` 是摊平表里的定位键（Modbus 是 unit_id、S7 是 0）。
        """
        registers = encode(value, pt.datatype, pt.word_order)
        if area_is_bit(pt.area):
            # 两边的"地址 0"含义不同（位区误用 `write_registers` 现在直接抛 ValueError；
            # 实测它曾把 0x0002 点成**线圈 1**），所以两个入口不许混用。
            return self._require_writer().write_bits(key, pt.area, pt.address,
                                                     bool(registers[0]))
        return self._require_writer().write_registers(key, pt.area, pt.address,
                                                      list(registers))

    def _own_intersecting(self, key, area, address, count, span_of) -> None:
        """把 `[address, address+count)` 碰到的点整段标成 manual —— 两条传输共用的那一步。

        Modbus 传 `span_of=lambda pt: pt.span`（寄存器格数）、S7 传字节数；`count` 是主站这次
        动的范围（Modbus 是报文里的数量、S7 是事件里的长度）。三条老口径都在这里收口：
        只读点跳过（主站写它会被协议拒，但 rx 照样解成"写"，不过滤就变成"一次没成功的写把
        曲线停了"）；点表里没有的格子一律不理；区间**重叠**判定而不是"基础格落在区间内"
        （主站从 4 格 uint64 的中间那一格写起时，按基础地址判一个点都命中不了）。
        """
        for point_key, pt in self._wave_points:
            if point_key != key or pt.area != area or pt.readonly:
                continue
            span = span_of(pt)
            if pt.address < address + count and address < pt.address + span:
                # `pt.bit` 一起交下去：S7 的同字节 bool 是两个点（判据见 `SourceStore`）。
                # 事件只给**字节**范围，所以同字节里另一个 bool 也会被标上——这是既有的
                # S7 口径"宁可多标一个"（一次恢复就回来），不是这里算错。
                self._sources.own(self.channel_name, key, area, pt.address,
                                  count=span, who="master", bit=pt.bit)

    def _emit(self, level: str, kind: str, key: tuple, text: str,
              now: float) -> None:
        """运行期报错的唯一出口：没接 sink 就一个字都不发，接了也同点同类 60s 一条。

        没接 sink 是**常态**而不是错误：M1/M2 的绝大多数用例只起 backend、不看运行期日志，
        这里既不查去重表也不发回调，它们的形状因此一个字没变
        （`test_a_backend_nobody_listens_to_keeps_no_dedupe_book` 钉这一支）。

        `key` 是这次故障的身份：一个点给 `(unit_id, area, address)`，整轮的失败没有点可指
        就给 `()`（本 backend 一份表，通道已经隐含在里面了）。
        """
        if self._log_sink is None:
            return
        line = self._dedupe.report(kind, key, text, now)
        if line is None:
            return
        try:
            self._log_sink(level, line)
        except Exception:                               # noqa: BLE001
            # 吞掉的是一条日志，不是一条曲线。与 `packet_sink` 的"异常原样冒出去"是**反**
            # 的口径，因为两者丢的东西不一样：报文 sink 炸了报文窗露一段空档（看得见），
            # 日志 sink 炸了如果放它出去，带走的是正在跑的模拟量——症状不许升级成病。
            # `Session.publish_log` 那一侧同样吞订阅者异常（`_publish`），两道一起才是
            # "界面坏了曲线照跑"。这里刻意不留 traceback：能报出这条故障的只有同一个 sink。
            pass

    def _report_clamp(self, unit_id: int, pt: Point, value, now: float) -> None:
        """值飘出 datatype 之外：这一轮**照样写**，只是写在边界上，所以要告诉操作员
        他的曲线配大了（spec §8：越界要钳到边界并只记一条日志、同点同类 60s 不重复刷屏；
        那句日志里带**点位名**是 M4 计划的报错口径，不是 §8 的原文）。
        """
        self._emit("warning", "clamp", (unit_id, pt.area, pt.address),
                   f"{pt.name}: waveform value outside {pt.datatype}, "
                   f"clamped to {value}", now)

    def _report_failure(self, stage: str, unit_id: int, pt: Point, exc: Exception,
                        now: float) -> None:
        """这个点这一轮一个字都没写出去：通道照旧应答，值冻在上一次（或点表初值）上。

        `stage` 进身份也进文案，因为两类要改的东西不是一回事：`compute` 是曲线自己算不出
        来（波形名写错、`period=0`、参数不是数），`encode` 是值算出来了但这个点位编不成
        寄存器（最常见是给 string 点位挂了数值曲线）。共用一个身份时，先报的那一类会把
        后报的那一类压住 60s，操作员看见的就是"日志说了句不相干的话"。
        异常类名一起带上：只说 failed 的话，他还得自己复现一遍才知道为什么。
        """
        self._emit("error", stage, (unit_id, pt.area, pt.address),
                   f"{pt.name}: waveform {stage} failed: "
                   f"{type(exc).__name__}: {exc}", now)

    def _report_tick_failure(self, exc: Exception, now: float) -> None:
        """整轮 tick 被一个没预料到的异常打断了：这一轮一个点都没写完，下一轮照旧。

        这一类没有"点"可指（能逃到 `_tick_loop` 的已经不是某个点的参数问题），身份就是
        本通道——`()` 足够，因为去重表是一个 backend 一份。消息带通道名与异常类名：
        操作员要看的是"曲线为什么不动了"，答案得说清是这一层崩了、崩在哪一句。
        """
        self._emit("error", "tick", (),
                   f"channel {self.channel_name}: waveform tick stopped by "
                   f"{type(exc).__name__}: {exc}", now)

    def mark_manual(self, unit_id: int, area: str, address: int,
                    count: int = 1, who: str = "gui", bit: int | None = None) -> None:
        """把一段地址标成"人工接管"。**只能在 loop 线程调**（`Session` 经 runner.call）。

        `bit` 只对 S7 的 bool 点有意义（同一字节上可以有多个点位，见 `SourceStore`）。
        """
        self._sources.own(self.channel_name, unit_id, area, address, count=count,
                          who=who, bit=bit)

    def restore_waveform(self, unit_id: int, area: str, address: int,
                         count: int = 1, bit: int | None = None) -> bool:
        """把 `[address, address+count)` 交还波形；这一段里原本有 manual 才返回 True。

        跨度由调用方给（`Session` 手里才有点位的 `span`），这一层不认点只认地址；
        `bit` 同理只为 S7 的 bool 服务。
        """
        return self._sources.restore(self.channel_name, unit_id, area, address,
                                     count=count, bit=bit)

    def source_is_waveform(self, unit_id: int, area: str, address: int,
                           bit: int | None = None) -> bool:
        """这一格当前还归波形驱动吗（`Session.rows()` 的 `source` 列读它）。

        只给"调用方要的那个点"的答案，不外泄整表快照（Task 2 复审的口径）。
        """
        return self._sources.is_waveform(self.channel_name, unit_id, area, address,
                                         bit=bit)

    def read_point_bytes(self, pt):
        """**按字节**读一个点（S7 那条路专用）：回 `bytes | None`。

        Modbus 的两条传输没有"这一个点的字节"这个概念（寄存器是 16 位格），所以这里默认
        抛：真走到这一支说明 `make_backend` 把传输认错了——那是编程错误，不该被当成
        "读不到"静默降级成灰显。S7 后端覆盖它（`s7_server.S7Backend.read_point_bytes`）。
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no byte-wise read; only the S7 backend does")

    def _require_writer(self) -> RawRegisterWriter:
        """没起（或已经停了）就调读写接口：一句带通道名的话，不要 `AttributeError`。

        `stop()` 把 `_writer` 置 None 是故意的——停掉的通道不许再被摸；但 None 上取属性
        出来的 `'NoneType' object has no attribute 'read_registers'` 看着像模拟器的 bug，
        真原因只是"这个通道当前没在跑"（GUI 关通道与引擎 tick 抢顺序时就会撞上）。
        """
        if self._writer is None:
            raise RuntimeError(f"channel {self.channel_name} is not started")
        return self._writer

    def write_registers(self, unit_id: int, area: str, address: int,
                        values: list[int]) -> bool:
        return self._require_writer().write_registers(unit_id, area, address, values)

    def read_registers(self, unit_id: int, area: str, address: int,
                       count: int) -> list[int] | None:
        return self._require_writer().read_registers(unit_id, area, address, count)

    def write_bits(self, unit_id: int, area: str, bit_address: int, value: bool) -> bool:
        return self._require_writer().write_bits(unit_id, area, bit_address, value)

    def read_bits(self, unit_id: int, area: str, bit_address: int,
                  count: int) -> list[bool] | None:
        return self._require_writer().read_bits(unit_id, area, bit_address, count)
