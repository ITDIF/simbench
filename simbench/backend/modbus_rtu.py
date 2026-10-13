"""pymodbus Modbus RTU 从站：串口归属探针 + 建 server + 按 RTU 帧切帧。

与传输无关的那一半在 `modbus_base.py`（M5 Task 3）：波形 tick、值归属、报文/日志钩子、
启停顺序在两条传输上是同一份代码。本文件只留串口专有的四件事——串口归属探针
（`check_serial_is_free`）、`ModbusSerialServer` 的构造、等它起来、按 RTU 帧形状切帧与解析。

两条实测口径（脚本与输出见 `docs/superpowers/notes/2026-10-12-m5-rtu-probes.md`）：

1. **串口打不开时 pymodbus 只说一句没有信息量的话**：`listen()` 把真实原因降级成一条
   warning 日志后 `return False`，`serve_forever()` 只抛
   `RuntimeError("Could not start listen, please check address.")`；而我们自己的
   `configure_library_logging(ERROR)` 连那条 warning 也压掉。所以必须先自己开一次串口，
   把 pyserial 的原话（"不存在" vs "被别的程序占着"）拿到手——与 TCP 那条"端口归属先
   自己确认"同一个理由、同一个位置。
2. **`port="socket://host:port"` 会让串口服务端自建 TCP 监听**（pymodbus 的显式分支，
   `transport/transport.py:151-160`），而主站那一侧仍是 pyserial 的串口对象。集成用例
   就走这条路：两端跑真 RTU framer（CRC、单元号、静默切帧都在），不需要任何硬件。

pymodbus / pyserial 的 import 只允许出现在 backend/（tests/test_architecture.py 守这条线）。
"""

from __future__ import annotations

import asyncio
import time

import serial
from pymodbus import FramerType
from pymodbus.server import ModbusSerialServer

from ..project.model import serial_summary
from .modbus_base import BackendStartError, ModbusBackendBase
# `socket://` 那一路的"归属"就是那个 TCP 端口，判定与 TCP 一模一样（见
# `check_serial_is_free`），所以直接用它那一份两级探针——两份探针一定会漂。
from .modbus_tcp import check_port_is_free

# 探针打开串口后立刻关掉：只借它问一句"这个口现在能不能开"
_PROBE_TIMEOUT = 0.05
_START_TIMEOUT = 5.0
_POLL = 0.005

# RTU 帧 = 单元号(1) + PDU + CRC(2)。这张表只管**请求**帧的长度——接管判定只关心主站
# 写下来的帧，而请求的长度是定死的（响应那边的长度随数据变，我们不看 tx 方向）。
# 1..6 都是 8 字节（fc+地址2+数量/值2+CRC2），17（报告从站 ID）请求只有 4 字节。
_FIXED_REQUEST_LENGTH = {1: 8, 2: 8, 3: 8, 4: 8, 5: 8, 6: 8, 17: 4}
# 15/16（写多个）：单元号+fc+addr(2)+qty(2)+字节数(1)+数据+CRC(2)
_VARIABLE_REQUEST = (15, 16)
_HEADER_AND_CRC = 9                      # 1 + 1 + 2 + 2 + 1 + 2
_SOCKET_URL = "socket://"                # pyserial 的"串口挂在 TCP 上"方案


def socket_endpoint(port: str) -> tuple[str, int] | None:
    """`socket://127.0.0.1:5000` -> `("127.0.0.1", 5000)`；不是这个形状就回 None。

    单独成函数是因为它有两处用途：探针要按 TCP 端口判归属，而误用测试/排查时也要看得出
    这一路其实跑在 TCP 上。解析不出端口号就回 None（交给 pyserial 自己报错），
    不在这里猜一个默认端口。
    """
    if not isinstance(port, str) or not port.startswith(_SOCKET_URL):
        return None
    host, _, raw_port = port[len(_SOCKET_URL):].rpartition(":")
    if not host or not raw_port.isdigit():
        return None
    return host, int(raw_port)


class SerialOpenError(BackendStartError):
    """串口不归我们：不存在、被别的程序占着、或者驱动没装好。

    与 `PortInUseError` 并列，都从 `BackendStartError` 派生：调用方按"资源不归我们"
    这一族统一处理，不必逐个传输去列举。
    """


def check_serial_is_free(port: str, channel_name: str | None = None) -> None:
    """把串口交给 pymodbus **之前**先自己开一次，拿 pyserial 的原话。

    为什么必须自己探（见文件头第 1 条实测）：不探的话现场只看到 pymodbus 那句
    `Could not start listen, please check address.`，而"这个口不存在"和"被串口助手占着"
    要做的事完全不同（插好 USB-485 / 去关掉那个程序）。

    走 `serial_for_url` 而不是 `serial.Serial`：pymodbus 自己就是这么打开串口的
    （`transport/serialtransport.py:33`），所以探针与真启动**认的是同一套 URL 语法**
    （`COM3`、`socket://host:port` 都行）。`exclusive=True` 与它一致：同一串口被别的进程
    占着时，这里就开不出来。
    """
    endpoint = socket_endpoint(port)
    if endpoint is not None:
        # `socket://host:port`：从站那一侧会把它当 **TCP 监听**（pymodbus 的显式分支，
        # `transport/transport.py:151-160`），所以"这个资源归谁"问的是那个 TCP 端口——
        # 按 TCP 那套两级探针走（bind + connect），而不是拿串口的方式去 connect 它
        # （那样在从站还没起来时必然"连接被拒"，等于把正常启动判成打不开）。
        check_port_is_free(endpoint[0], endpoint[1], channel_name)
        return
    who = f"channel {channel_name}: " if channel_name else ""
    try:
        handle = serial.serial_for_url(port, exclusive=True, timeout=_PROBE_TIMEOUT)
    except (serial.SerialException, OSError, ValueError) as exc:
        raise SerialOpenError(
            f"{who}cannot open serial port {port!r} "
            f"({type(exc).__name__}: {exc}); check that the adapter is plugged in and "
            f"that no other program (another simulator? a terminal?) holds this port"
        ) from exc
    handle.close()


class ModbusRtuBackend(ModbusBackendBase):
    """一个 RTU 通道 = 一个 ModbusSerialServer = 一个 loop 线程里的串口。"""

    _pdu_offset = 1                      # RTU：单元号 1 字节之后就是 PDU

    async def _probe(self) -> None:
        """串口归属探针。走 `asyncio.to_thread`：打开串口是阻塞调用（拔线/驱动异常时更久），
        落在 loop 上会把同 loop 的其它协程冻住——与 TCP 探针同一条口径。"""
        await asyncio.to_thread(check_serial_is_free, self.channel.port,
                                self.channel_name)

    def _create_server(self, devices) -> ModbusSerialServer:
        channel = self.channel
        return ModbusSerialServer(
            devices,
            framer=FramerType.RTU,
            port=channel.port,
            baudrate=channel.baudrate,
            parity=channel.parity,
            bytesize=channel.bytesize,
            stopbits=channel.stopbits,
            timeout=channel.timeout,
            ignore_missing_devices=False,
            trace_packet=self._trace_packet,
            trace_connect=self._trace_connect,
        )

    async def _await_ready(self, server, task) -> None:
        """等"串口真的开了"。串口没有 listener 可等，判据是 transport 有没有建起来。

        与 TCP 的 `_await_ready` 同一个形状（也同一个理由：`sleep(0)` 不够，失败要两三个
        loop 迭代才落地），只是"起来了"的信号从 listener 换成 `server.transport`——
        pymodbus 的 `listen()` 在成功建起串口连接之后才把它赋上。

        失败时把 pymodbus 那句通用的话**翻译成人话**：真实原因已经在探针那一句里报过了，
        这里只说"哪一路、哪个串口、没起来"，别让操作员对着 `Could not start listen,
        please check address.` 猜。
        """
        deadline = time.monotonic() + _START_TIMEOUT
        while True:
            if task.done():
                exc = task.exception()
                if exc is None:
                    raise SerialOpenError(
                        f"channel {self.channel_name}: the RTU server on "
                        f"{self.channel.port!r} stopped right after start")
                raise SerialOpenError(
                    f"channel {self.channel_name}: the RTU server on "
                    f"{self.channel.port!r} could not start ({exc}); the serial port "
                    f"probe above has the real reason") from exc
            if getattr(server, "transport", None) is not None:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"channel {self.channel_name}: the serial port "
                    f"{self.channel.port!r} did not come up within {_START_TIMEOUT}s")
            await asyncio.sleep(_POLL)

    def _parse_frame(self, frame: bytes) -> tuple[int | None, int | None, str]:
        """RTU 帧 -> `(unit_id, function_code, 摘要)`：句式与 TCP 那版逐字一致，
        报文窗里两条传输的摘要因此长得一样（现场不必学两套读法）。"""
        if len(frame) < 4:                   # 单元号 + fc + CRC(2) 是最短的帧
            return None, None, f"short ({len(frame)} bytes): {frame.hex(' ')}"
        unit, function = frame[0], frame[1]
        body = frame[2:-2]                   # 去掉单元号与 CRC，剩下的就是 PDU
        if function & 0x80:
            code = body[0] if body else "?"
            return unit, function & 0x7F, f"unit {unit} fc {function & 0x7F} EXCEPTION {code}"
        return unit, function, f"unit {unit} fc {function} {len(body)}B {body[:8].hex(' ')}"

    def _split_frames(self, raw: bytes):
        """一次到达的 rx 字节块 -> 逐帧产出（RTU：单元号 + PDU + CRC）。

        只管**请求**帧（rx 方向 = 主站发下来的命令）：读类请求是定长 8 字节、报告从站 ID
        是 4 字节、写多个（15/16）由报文字节数决定，每一个长度都量得出来。碰到量不出来的
        帧型（诊断回显、自定义功能码……）就**整块停手**——与 TCP 那条"长度字段撒谎就整块
        停手"同一个口径：猜"下一帧大概从这里开始"会把后面的真报文一条条切歪，而切歪的
        代价是把"主站写的是哪个点"认到别的偏移上，静默停掉一段曲线。

        为什么必须自己切：pymodbus 在**组帧之前**就 trace，一个块里可能挤了两条写请求
        （TCP 那条实测过一次到达 24 字节），而 `write_targets` 按契约只解第一条。串口一次
        轮询能读到上百字节，这种形状只会更多。
        """
        offset = 0
        while True:
            if len(raw) - offset < 4:        # 连帧头都不完整：等下一次重放
                return
            function = raw[offset + 1]
            if function in _FIXED_REQUEST_LENGTH:
                length = _FIXED_REQUEST_LENGTH[function]
            elif function in _VARIABLE_REQUEST:
                if len(raw) - offset < 7:    # 到"字节数"那一位为止
                    return
                length = _HEADER_AND_CRC + raw[offset + 6]
            else:
                return                       # 量不出来的帧型：这一块到此为止，不猜
            if len(raw) - offset < length:
                return                       # 帧还没到齐（重放的那一次会带上余量）
            yield raw[offset:offset + length]
            offset += length

    def describe(self) -> str:
        """主站该连哪：串口那一串参数（`COM3 19200 8E1`）——串口上没有别的能对得上的东西。

        与命令行 `--list` 那一行用同一个 `serial_summary()`（同一台设备不该有第二种写法）。
        """
        return serial_summary(self.channel)
