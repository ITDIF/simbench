"""pymodbus Modbus TCP 从站的封装：起停、报文钩子、日志降噪。

pymodbus 的 import 只允许出现在 backend/（tests/test_architecture.py 守这条线）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time

from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer

from ..project.model import Project
from .raw_writer import RawRegisterWriter
from .simdev import build_sim_devices

_NOISE_PREFIX = "Datastore unable to fulfill request"
# 只挡"未定义 unit id 回落 devices[0] 抛 KeyError: 0"这一种已知噪音：三个片段必须**同时**
# 出现才抑制（前缀 + 异常本身 + 抛出它的那一帧）。
_UNIT_FALLBACK_MARKS = (_NOISE_PREFIX, "KeyError: 0", "__get_device")
# 起停都是"毫秒级就好，超过 5 秒就是坏了"：真值见 _await_listener 与 stop 的注释
_START_TIMEOUT = 5.0
_STOP_TIMEOUT = 5.0
_POLL = 0.005


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


class PortInUseError(RuntimeError):
    """端口不归我们：通常是上一个没退干净的模拟器实例还在监听。

    继承 `RuntimeError` 是为了不砸已有调用方的 `except RuntimeError`（`_await_listener`
    的晚到失败、`_require_writer` 的"通道没在跑"都从这里抛）。
    """


def check_port_is_free(host: str, port: int, channel_name: str | None = None) -> None:
    """裸 `socket.bind` 探针：把端口交给 pymodbus **之前**先自己确认一次归属。

    为什么必须自己探：pymodbus 3.15.0 在 Windows 上允许两个 `ModbusTcpServer` 绑同一个
    `(host, port)` 都"启动成功"（它的 listener 带 SO_REUSEADDR 语义，`_await_listener`
    只看自己 `server.transport` 是否为 None，证明不了端口归谁），netstat 上能叠出 3 个
    LISTENING。实测后果：A 用旧点表、B 用新点表，两边都打印 `listening ...`，而主站的
    连接被 A 吃掉——"改了点位重启模拟器"之后平台读到的还是旧值，现场完全没有线索指向
    那个僵尸进程。

    探针只 bind、不 listen，并且**立刻** close（`finally`），所以不会反过来把端口占住。
    bind 与真正 listen 之间仍然存在 TOCTOU 窗口：这里只能说"这端口现在不归我们"，
    因此消息写 "may already be in use"，不说死是谁占的；晚到失败由 `_await_listener`
    兜（两条用例分别钉住这两个阶段）。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        detail = f"WinError {code}" if code is not None else f"errno {exc.errno}"
        who = f"channel {channel_name}: " if channel_name else ""
        raise PortInUseError(
            f"{who}port {host}:{port} may already be in use by another process or a "
            f"previous simulator instance (bind probe failed with [{detail}] "
            f"{exc.strerror}); stop the instance that is holding it and start again"
        ) from exc
    finally:
        probe.close()


def parse_packet(raw: bytes) -> tuple[int | None, int | None, str]:
    """MBAP 头 + 功能码，给人看的摘要。只解析，不参与协议。

    异常响应（功能码 |0x80）单独成一句：报文监视窗里"从站回了异常码 2"必须一眼看出来，
    否则现场只能对着十六进制数功能码。
    """
    if len(raw) < 8:
        return None, None, f"short ({len(raw)} bytes): {raw.hex(' ')}"
    unit, function = raw[6], raw[7]
    body = raw[8:]
    if function & 0x80:
        code = body[0] if body else "?"
        return unit, function & 0x7F, f"unit {unit} fc {function & 0x7F} EXCEPTION {code}"
    return unit, function, f"unit {unit} fc {function} {len(body)}B {body[:8].hex(' ')}"


class ModbusTcpBackend:
    """一个 TCP 通道 = 一个 ModbusTcpServer = 一个 loop 线程里的监听。"""

    def __init__(self, channel, packet_sink=None, on_connection=None) -> None:
        self.channel = channel
        self.channel_name = channel.name
        # port=0（"随便给一个"）不支持：Task 5 的 validate 已把它判成 error，
        # 这里不去摸 transport 里的实际端口（pymodbus 版本一变就碎）
        self.port = channel.port
        self.started_at = 0.0
        self._packet_sink = packet_sink
        self._on_connection = on_connection
        self._server: ModbusTcpServer | None = None
        self._writer: RawRegisterWriter | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """建 SimDevice -> 起监听。重复调用是安全的（已经在跑就直接返回）。"""
        if self._server is not None:
            return
        # 端口归属先自己确认：pymodbus 证明不了这件事（见 check_port_is_free 的实测说明），
        # 让第二个实例"启动成功"就等于把主站的连接留给上一个僵尸进程。
        check_port_is_free(self.channel.host, self.port, self.channel_name)
        configure_library_logging()
        devices = build_sim_devices(Project(channels=[self.channel]))
        server = ModbusTcpServer(
            devices,
            framer=FramerType.SOCKET,
            address=(self.channel.host, self.channel.port),
            ignore_missing_devices=False,
            trace_packet=self._trace_packet,
            trace_connect=self._trace_connect,
        )
        task = asyncio.create_task(server.serve_forever())
        try:
            await self._await_listener(server, task)
        except BaseException:
            # 起不来就不留半个后端：先把 server 收干净，再把真实异常原样抛给调用方。
            # 否则 self._server 还留着，stop() 会对一个从没监听过的 server 再 shutdown 一次。
            with contextlib.suppress(Exception):
                await server.shutdown()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, _STOP_TIMEOUT)
            raise
        self._server = server
        self._writer = RawRegisterWriter(server.context)
        self._task = task
        self.started_at = time.monotonic()

    async def _await_listener(self, server, task) -> None:
        """等端口真在听了再返回；起不来就把真实异常抛出去，不许"start 成功但连不上"。

        `serve_forever()` 只有两种提前结束：bind 失败（实测端口被占时是
        `RuntimeError: Could not start listen, please check address.`，Windows 上撞
        保留端口还会附带 10013）和被 shutdown。所以 task 一结束就说明从站没起来。

        计划里这里只有 `await asyncio.sleep(0)`，实测不够：sleep(0) 之后 `task.done()`
        仍是 False，bind 失败要再跑两三个 loop 迭代才落地——那样 start() 会"成功返回"，
        调用方以为通道已经在监听，实际外面根本连不上（静默失败）。
        判定只看"有没有 listener"这一个布尔，不去读 transport 里的端口号——
        读端口才是计划拒绝 port=0 的理由（版本一变就碎）。
        """
        deadline = time.monotonic() + _START_TIMEOUT
        while True:
            if task.done():
                task.result()                       # 真实启动异常，原样抛
            if getattr(server, "transport", None) is not None:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"channel {self.channel_name}: listener on "
                    f"{self.channel.host}:{self.channel.port} did not come up "
                    f"within {_START_TIMEOUT}s")
            await asyncio.sleep(_POLL)

    async def stop(self) -> None:
        """关监听并等 serve_forever 收尾。幂等：没起过、重复停都安全返回。

        实测：`shutdown()` 之后 serve_forever 在 0.2ms 内自己返回，**不需要** `task.cancel()`。
        计划里那句 cancel 会被 serve_forever 自己的 `with suppress(CancelledError)` 吞掉，
        task 最终仍是"正常结束"（`cancelled()` 为 False），状态反而更难读；所以这里只等它
        结束，等不到才算错（TimeoutError 会带着端口名抛出去，比留下一个活 task 好查）。
        """
        server, task = self._server, self._task
        self._server = None
        self._writer = None
        self._task = None
        if server is not None:
            await server.shutdown()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, _STOP_TIMEOUT)

    def _trace_packet(self, is_outgoing: bool, payload: bytes) -> bytes:
        """pymodbus 的报文钩子：**必须把字节原样返回**（实测改 payload 就是改线上数据）。

        交给 sink 的顺序是 `(direction, raw, unit_id, function, summary, mono)`，与
        `events.PacketEvent` 去掉 channel 后的字段顺序一致，Task 11 与 M3 的报文窗都按
        这个位置消费。sink 里抛的异常原样冒出去：静默丢一包会让报文窗"看起来没流量"，
        比炸出来更难查。
        """
        if self._packet_sink is not None:
            raw = bytes(payload)
            unit, function, summary = parse_packet(raw)
            self._packet_sink("tx" if is_outgoing else "rx", raw, unit, function,
                              summary, time.monotonic() - self.started_at)
        return payload

    def _trace_connect(self, is_new: bool) -> None:
        """连接建立/断开回调：实测一次"连上→读→关"正好是 ("open", "close") 两次。"""
        if self._on_connection is not None:
            self._on_connection("open" if is_new else "close")

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
