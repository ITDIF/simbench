"""pymodbus Modbus TCP 从站：端口归属探针 + 建 server + MBAP 切帧。

与传输无关的那一半（摊平点表、波形 tick、值归属、报文/日志钩子、启停顺序）搬去了
`modbus_base.py`（M5 Task 3）：本文件只剩 TCP 专有的四件事——两级端口探针
（`check_port_is_free`）、`ModbusTcpServer` 的构造、等 listener 起来、按 MBAP 切帧与解析，
外加"声明里的地址再挂几个监听"那一段。

pymodbus 的 import 只允许出现在 backend/（tests/test_architecture.py 守这条线）。
`configure_library_logging` 与 `_DropDatastoreNoise` 从基类模块再导出：它们在 M5 抽基类
之前就住在这里，测试与排查脚本按老路径 import 它们。
"""

from __future__ import annotations

import asyncio
import select
import socket
import time

from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer

from .modbus_base import (  # noqa: F401  （再导出：老路径 import 它的人不少）
    BackendStartError,
    ModbusBackendBase,
    _DropDatastoreNoise,
    configure_library_logging,
)

# 起那一边"毫秒级就好，超过 5 秒就是坏了"：真值见 `_await_ready` 的注释
_START_TIMEOUT = 5.0
_POLL = 0.005
# connect 级端口探针的两个时限（本机实测值，见 check_port_is_free 的说明）：
# _CONNECT_TIMEOUT 是"握手最多允许多久"——真监听者的 SYN-ACK 由内核给，环回上实测
# <1ms，全量 pytest 并发压着跑 40/40 都在 50ms 内完成；_CONNECT_HOLD 是"连上之后还要
# 看它能不能留住"，只有人在服务时才会等满这 50ms。
_CONNECT_TIMEOUT = 0.05
# "这个地址不在本机"的错误码：Windows 是 WSAEADDRNOTAVAIL(10049)，POSIX 是 EADDRNOTAVAIL(99)。
# Windows 上 Python 把 WSA 码同时放进 `winerror` 与 `errno`，两个都收着更稳。
_ADDRESS_NOT_AVAILABLE = frozenset({10049, 99})
_CONNECT_HOLD = 0.05
_LOOPBACK = "127.0.0.1"
# MBAP 的 length 字段说的是"unit id + PDU"的字节数，Modbus TCP 的 PDU 最长 253 字节，
# 所以 length > 254（整帧 > 260 字节）**不可能**是真帧。切帧时见到这种字段就整块停手：
# 猜"下一帧大概从这里开始"会把后面的真报文一条条切歪，宁可这一段一个都不认——真出这种
# 字节块时 `pymodbus` 自己的组帧也一样卡住，那次写压根落不到寄存器上（`_note_master_writes`）。
_MAX_MBAP_LENGTH = 254


class PortInUseError(BackendStartError):
    """端口不归我们：通常是上一个没退干净的模拟器实例还在监听。

    继承 `BackendStartError`（→ `RuntimeError`）是 M5 抽基类时收的口子：调用方按
    "资源不归我们"这一族统一处理（`__main__` 那一行错误、界面的 ▲ 失败），而既有
    `except RuntimeError` 的调用方一个都不用改。
    """


def _refused(channel_name, host: str, port: int, probe_detail: str) -> PortInUseError:
    """两个探针共用一句拒绝。

    必须同时出现的东西：端口号（现场拿去对 netstat）、"previous simulator instance"
    （第一嫌疑人就是没退干净的自己）、以及**双监听会造成什么**——这一句才是让人愿意去
    杀进程而不是"再点一次启动看看"的理由。
    """
    who = f"channel {channel_name}: " if channel_name else ""
    return PortInUseError(
        f"{who}port {host}:{port} may already be in use by another process or a "
        f"previous simulator instance ({probe_detail}); stop the instance that is "
        f"holding it and start again, otherwise two listeners stack up on this port, "
        f"the master is served by the older one, and a point-table change looks like "
        f"it had no effect with nothing in the log to show for it")


def _not_our_address(channel_name, host: str, port: int, exc: OSError) -> PortInUseError:
    """`host` 不在本机任何网卡上——不是"端口被别人占着"，是"这个地址还不存在"。

    这两种情况在 bind 上都只看到一句英文（`WSAEADDRNOTAVAIL` / WinError 10049），
    但该做的事完全相反：地址没配好要去跑 `net up`，端口被占要去停那个进程。照"端口被占"
    报出去，现场会去 netstat 里找一个不存在的占用者。
    """
    who = f"channel {channel_name}: " if channel_name else ""
    return PortInUseError(
        f"{who}cannot listen on {host}:{port}: this address is not configured on any "
        f"local adapter ({exc.strerror}); run `python -m simbench net up --point-table "
        f"<point table>` to apply the point table's network declaration, or fix the host "
        f"in the point table")


def _bind_probe(host: str, port: int, channel_name) -> None:
    """裸 `socket.bind` 探针：同一个 host 上已经有人占着这个端口就立刻炸。

    只 bind、不 listen，并且**立刻** close（`finally`），所以不会反过来把端口占住。
    bind 与真正 listen 之间仍然存在 TOCTOU 窗口：这里只能说"这端口现在不归我们"。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        if code in _ADDRESS_NOT_AVAILABLE or exc.errno in _ADDRESS_NOT_AVAILABLE:
            raise _not_our_address(channel_name, host, port, exc) from exc
        detail = f"WinError {code}" if code is not None else f"errno {exc.errno}"
        raise _refused(channel_name, host, port,
                       f"bind probe failed with [{detail}] {exc.strerror}") from exc
    finally:
        probe.close()


def _is_being_served(target: str, port: int) -> bool:
    """对 `target:port` 真连一次，并看这条连接**留不留得住**。

    为什么不能只看 `connect_ex()==0`（本机实测）：上一个实例被硬杀之后，第一次 connect
    仍然会成功（内核把 SYN 交给那个正在消失的 listener），随后立刻 RST——12 次里 9 次
    是这个形状。照"连上就拒"写，每次"杀掉旧实例再起新的"都会被误拒，那比原来的双监听
    更让人怀疑模拟器。所以连上之后还要在 `_CONNECT_HOLD` 里看一眼可读性：有人在服务 =
    连接安静地留着；残影 = 立刻可读（RST 或 EOF）。

    任何非 0 的 connect 结果（拒绝、超时、网络不可达）都算"没人在服务"。这一步是 bind
    之外的加练，判不准时不许把用户的正常启动挡在门外。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(_CONNECT_TIMEOUT)
    try:
        try:
            if probe.connect_ex((target, port)) != 0:
                return False
        except OSError:
            return False
        try:
            readable, _, _ = select.select([probe], [], [], _CONNECT_HOLD)
        except OSError:
            return True                 # 连上了而 select 都报错：按有人在服务处理
        if not readable:
            return True
        try:
            return bool(probe.recv(1))  # 对方主动说话也是"在服务"；空/异常=残影
        except OSError:
            return False
    finally:
        probe.close()


def _connect_probe(host: str, port: int, channel_name) -> None:
    """connect 级探针：补 bind 探针看不见的那两个窄洞。

    本机实测（探针 socket 不带 SO_REUSEADDR，Windows）：

    | 别人在监听    | 我们 bind    | 结果         |
    |--------------|-------------|-------------|
    | 127.0.0.1:P  | 0.0.0.0:P   | **OK** ← 洞 |
    | 0.0.0.0:P    | 127.0.0.1:P | **OK** ← 洞 |
    | 0.0.0.0:P    | 0.0.0.0:P   | FAIL 10048  |
    | 127.0.0.1:P  | 127.0.0.1:P | FAIL 10048  |

    也就是说 bind 只看得见"绑定地址完全一样"的冲突，而**通配与具体地址两个方向都能叠**。
    叠上的后果不是报错而是静默读旧值：主站连的是先起来那一个，刚改完点表重启的这一个
    一句日志都不说。所以这里对 `127.0.0.1:port` 真连一次——通配监听者和环回监听者都在
    环回上应答，两个窄洞一起堵掉；`host` 是具体的非环回地址时再多探它自己（那种配置的
    冲突只有打它本身才看得见）。

    **还剩一处看不见**：对方只绑在另一块网卡的具体地址上（它 `192.168.1.5:P`、我们
    `0.0.0.0:P`），它不该答环回，探它又必须枚举本机地址且会误拒别的服务，这里刻意不做。
    现场最常见的形态——同一台笔记本上两个模拟器实例，默认都绑通配或都绑 127.0.0.1——
    已经全部覆盖。
    """
    targets = [_LOOPBACK]
    if host and host not in (_LOOPBACK, "localhost", "::1", "0.0.0.0", "::"):
        targets.append(host)
    for target in targets:
        if _is_being_served(target, port):
            raise _refused(channel_name, host, port,
                           f"connect probe to {target}:{port} was accepted and the "
                           f"connection stayed open, so something is serving this "
                           f"port right now — most likely a previous simulator "
                           f"instance that did not exit cleanly")


def check_port_is_free(host: str, port: int,
                       channel_name: str | None = None) -> None:
    """把端口交给 pymodbus **之前**先自己确认归属：bind 一次、connect 一次。

    为什么必须自己探：pymodbus 3.15.0 在 Windows 上允许两个 `ModbusTcpServer` 绑同一个
    `(host, port)` 都"启动成功"（它的 listener 带 SO_REUSEADDR 语义，`_await_ready`
    只看自己 `server.transport` 是否为 None，证明不了端口归谁），netstat 上能叠出 3 个
    LISTENING。实测后果：A 用旧点表、B 用新点表，两边都打印 `listening ...`，而主站的
    连接被 A 吃掉——"改了点位重启模拟器"之后平台读到的还是旧值，现场完全没有线索指向
    那个僵尸进程。

    两级探针各管一半：bind 管"同一个绑定地址已经被占"（快、确定），connect 管"通配与
    具体地址互叠"那一类 bind 根本看不见的情形（见 `_connect_probe` 的实测表）。两条都
    只说"可能已被占用"——探针之后仍有 TOCTOU 窗口，真占用者是谁证明不了；晚到失败由
    `_await_ready` 兜（用例分别钉住这两个阶段）。
    """
    _bind_probe(host, port, channel_name)
    _connect_probe(host, port, channel_name)


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


class ModbusTcpBackend(ModbusBackendBase):
    """一个 socket 通道 = 一个 pymodbus server = 一个 loop 线程里的监听。

    与传输无关的行为（波形、归属、报文/日志、启停顺序）全在 `ModbusBackendBase`；
    本类只填那五个钩子，再加"声明里的地址再挂几个监听"。

    **三种 socket 传输共用这一层**（2026-10-14 E7）：`tcp`（MBAP）、`udp`（MBAP over UDP）、
    `rtutcp`（RTU 帧跑在 TCP 上，现场的以太网转串口网关就是这种）。子类只覆盖三个类属性
    与"切帧/解帧"那两个钩子——服务器类、framer、PDU 偏移各写三份的话，迟早漂成三种行为。
    """

    _pdu_offset = 7                    # MBAP 头 7 字节，PDU 从第 7 字节开始
    SERVER_CLASS = ModbusTcpServer     # 子类换 `ModbusUdpServer` 之类
    FRAMER = FramerType.SOCKET         # socket framer = MBAP；RTU-over-TCP 换 `FramerType.RTU`
    TRANSPORT = "tcp"                  # 这个后端对应哪种传输（`describe()` 与测试都用它）

    def __init__(self, channel, packet_sink=None, on_connection=None,
                 tick_ms: int = 0, log_sink=None, extra_endpoints=()) -> None:
        super().__init__(channel, packet_sink=packet_sink, on_connection=on_connection,
                         tick_ms=tick_ms, log_sink=log_sink)
        # 声明里这一路要**额外**听的地址（`net/endpoints.py` 按点表旁的 `.net.json` 算好）。
        # 为什么要有这个：主站连的是设备那一侧的地址（10.136.107.240:502），而通道自己的
        # `host:port` 是点表写给本机自测用的（0.0.0.0:5020）；只听后者，现场那侧连不上。
        self.extra_endpoints: list[tuple[str, int]] = [(str(host), int(port))
                                                       for host, port in extra_endpoints]
        self._extra_servers: list[asyncio.Server] = []
        # port=0（"随便给一个"）不支持：Task 5 的 validate 已把它判成 error，
        # 这里不去摸 transport 里的实际端口（pymodbus 版本一变就碎）
        self.port = channel.port

    async def _probe(self) -> None:
        """把端口交给 pymodbus **之前**先自己确认归属（见 `check_port_is_free`）。

        走 `asyncio.to_thread`：探针是阻塞 socket 调用，落在 loop 上会把同 loop 的其它
        协程冻住（实测那 300ms 里推进 0 次，见
        `test_the_port_probe_must_not_block_the_engine_loop`）。
        """
        await asyncio.to_thread(check_port_is_free, self.channel.host, self.port,
                                self.channel_name)

    def _create_server(self, devices):
        return self.SERVER_CLASS(
            devices,
            framer=self.FRAMER,
            address=(self.channel.host, self.channel.port),
            ignore_missing_devices=False,
            trace_packet=self._trace_packet,
            trace_connect=self._trace_connect,
        )

    def _parse_frame(self, frame: bytes):
        """MBAP 帧 -> `(unit_id, function_code, 摘要)`（`parse_packet` 是它的实现）。"""
        return parse_packet(frame)

    def _split_frames(self, raw: bytes):
        """一次到达的 rx 字节块 -> 逐帧产出（MBAP 切帧）。

        为什么不给自己留"切不完的余量"（trace 的实测形状决定，别照直觉改回去）：
        `trace_packet` 拿到的是 transport 那一侧**累计**的缓冲，半个帧会在下一次连同新到的
        字节**整段重放**（先到 10 字节、再到 2 字节 → 第二次给的是完整的 12 字节）。所以
        每次都从第 0 字节切才对；自己再留一份余量拼回去，等于把已经见过的字节数两遍，
        之后每一条真帧都会对错偏移。

        `length` 不在 2..254 之内就不是 Modbus TCP 帧（PDU 最长 253 + unit id 1）：整块到此
        为止，不猜下一帧从哪儿开始——撒谎的长度字段同样会把 pymodbus 自己的组帧卡住，
        那次写压根落不到寄存器上，这里不替它标接管。
        """
        offset = 0
        while len(raw) - offset >= 8:              # 8 = MBAP(7) + 功能码(1)，再短读不出长度
            length = int.from_bytes(raw[offset + 4:offset + 6], "big")
            if not 2 <= length <= _MAX_MBAP_LENGTH:
                return                             # 不是帧头：这一段一个都不认
            if 6 + length > len(raw) - offset:
                return                             # 帧还没到齐：重放的那一次会带上余量
            yield raw[offset:offset + 6 + length]
            offset += 6 + length

    async def _open_extra(self, server) -> list[asyncio.Server]:
        """给声明里的每个地址再挂一个监听，**共用**主 server 的连接工厂与 datastore。

        pymodbus 3.15.0 的一个 server 只按一个 `(host, port)` 建监听（`ModbusProtocol
        .listen()` 里是 `loop.create_server(factory, host, port)`，`host` 只取
        `source_address[0]` 这**一个**值），所以第二个地址只能自己挂。工厂用的是
        pymodbus 自己那个 `server.handle_new_connection`：它每条连接造一个
        `ServerRequestHandler(server, ...)` 并登记进 `server.active_connections`，而
        handler 读写的是 `server.context` —— 于是所有地址读写的是**同一份寄存器**，
        而不是每个地址一个副本（副本的后果：主站从 A 地址写、界面按主 server 读回旧值，
        两边都"没报错"）。副作用是 `server.shutdown()` 也会把这几条监听上的连接一起收掉
        （它遍历的正是 `active_connections`），我们只需要自己关掉监听套接字。

        端口归属先探一次（与主监听同一套两级探针）：声明里那个地址很可能正是上一个没退
        干净的实例占着的，早一步报"有人在服务"比晚一步报 bind 失败有用得多。
        """
        opened: list[asyncio.Server] = []
        loop = asyncio.get_running_loop()
        for host, port in self.extra_endpoints:
            await asyncio.to_thread(check_port_is_free, host, port, self.channel_name)
            try:
                opened.append(await loop.create_server(
                    server.handle_new_connection, host, port,
                    reuse_address=True, start_serving=True))
            except OSError as exc:
                raise self._cannot_listen(host, port, exc) from exc
        return opened

    def _cannot_listen(self, host: str, port: int, exc: OSError) -> PortInUseError:
        """额外监听起不来时的原话：地址不在本机 / 端口被别人占着，都要说清去哪看。"""
        code = getattr(exc, "winerror", None)
        detail = f"WinError {code}" if code is not None else f"errno {exc.errno}"
        return PortInUseError(
            f"channel {self.channel_name}: cannot listen on {host}:{port} "
            f"[{detail}] {exc.strerror}; either this address is not configured on any "
            f"local adapter (run `python -m simbench net up --point-table <point table>` "
            f"to apply the network declaration) or another process is already bound "
            f"to this address and port")

    @property
    def endpoints(self) -> list[tuple[str, int]]:
        """这个后端在听的地址：通道自己的那个在前，声明里额外挂的在后。"""
        return [(self.channel.host, self.port), *self.extra_endpoints]

    async def _close_extra(self) -> None:
        """关掉额外监听。`asyncio.Server.close()` 是同步关掉监听套接字，不必等。"""
        servers, self._extra_servers = self._extra_servers, []
        for extra in servers:
            extra.close()


    async def _await_ready(self, server, task) -> None:
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

