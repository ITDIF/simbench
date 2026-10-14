"""backend 包的装配口：**唯一一处**"按通道类型选后端"。

调用方（`engine/session.py`、`__main__._serve`）只认这个函数，不认具体后端类："选哪个
后端"这件事散在几处的那种写法，加第三种通道时必然漏掉一处。

两个后端共用的那一半（波形 tick、值归属、报文/日志钩子、启停顺序）在 `modbus_base.py`：
`ModbusTcpBackend` 与 `ModbusRtuBackend` 各自只填五个钩子（探针、建 server、等起来、
切帧、解析），所以"改一处波形逻辑、两条传输一起生效"是**结构上**成立的。
"""

from __future__ import annotations

from ..project.model import channel_transport

# 再导出给调用方（`engine/session`、`__main__` 都从这里 import）：`as` 成同一个名字是
# 显式声明「我是给外面用的」，不是漏删的 import（ruff 的 F401 也认这个写法）。
from .modbus_ascii import ModbusAsciiBackend
from .modbus_base import BackendStartError as BackendStartError
from .modbus_rtu import ModbusRtuBackend
from .modbus_rtutcp import RtuOverTcpBackend
from .modbus_tcp import ModbusTcpBackend
from .modbus_udp import ModbusUdpBackend
from .s7_server import S7Backend


class UnsupportedTransportError(RuntimeError):
    """这一路通道的传输在当前构建里没有后端。

    已知的五种各有后端：socket 三种（`tcp` / `udp` / `rtutcp`）、串口两种（`rtu` / `ascii`）、
    外加 `s7`。真走到这里说明点表里写了一个我们不认识的 `type`（validate 会先报它）。
    """


def make_backend(channel, *, packet_sink=None, on_connection=None, tick_ms: int = 0,
                 log_sink=None, extra_endpoints=()):
    """按 `channel_transport()` 造后端。

    `extra_endpoints`（声明里额外要听的地址）只对 TCP 有意义：串口上没有"设备 IP:端口"
    这回事，传进来也不看（RTU 后端根本没有这个参数）。
    """
    transport = channel_transport(channel)
    if transport in ("tcp", "udp", "rtutcp"):
        # 三种 socket 传输共用同一个构造签名（`extra_endpoints` 只对它们有意义）
        backend_class = {"tcp": ModbusTcpBackend, "udp": ModbusUdpBackend,
                         "rtutcp": RtuOverTcpBackend}[transport]
        return backend_class(channel, packet_sink=packet_sink,
                             on_connection=on_connection, tick_ms=tick_ms,
                             log_sink=log_sink, extra_endpoints=extra_endpoints)
    if transport in ("rtu", "ascii"):
        serial_class = ModbusRtuBackend if transport == "rtu" else ModbusAsciiBackend
        return serial_class(channel, packet_sink=packet_sink,
                            on_connection=on_connection, tick_ms=tick_ms,
                            log_sink=log_sink)
    if transport == "s7":
        # S7 也没有"额外监听"那一维：一个 `host:port` 而已。
        return S7Backend(channel, packet_sink=packet_sink,
                         on_connection=on_connection, tick_ms=tick_ms,
                         log_sink=log_sink)
    raise UnsupportedTransportError(
        f"channel {channel.name}: unknown channel type {transport!r}")
