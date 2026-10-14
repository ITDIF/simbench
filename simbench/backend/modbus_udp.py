"""pymodbus Modbus UDP 从站：与 TCP 同一层实现，只差"服务器类"与归属探针。

为什么要它（2026-10-14 E7）：现场有用 UDP 的网关与老平台（无连接、丢一帧自己重试）。
帧格式与 TCP **完全一样**（MBAP），所以这里只覆盖三件事：

* `SERVER_CLASS = ModbusUdpServer`（同一份 datastore、同一套 trace 钩子）；
* **归属探针换成 bind**：UDP 没有握手，TCP 那种"连一下看有没有人应答"的探针在这里毫无
  意义（永远连得上）；能不能绑上才是"这个端口归谁"的判据；
* **声明里的额外地址暂不支持**：TCP 那一支靠 `loop.create_server` 再挂几个监听，UDP 要走
  `create_datagram_endpoint`（每个地址一个协议对象），本轮不做——但**不许静默**：启动时
  在日志里说一句"这一路只监听通道自己那个地址"。

pymodbus 的 import 只允许出现在 backend/（tests/test_architecture.py 守这条线）。
"""

from __future__ import annotations

import contextlib
import socket

from pymodbus.server import ModbusUdpServer

from .modbus_base import BackendStartError
from .modbus_tcp import ModbusTcpBackend

# "这个地址不在本机"的错误码（与 TCP 那一支同一张表，Windows 是 WSAEADDRNOTAVAIL）。
_ADDRESS_NOT_AVAILABLE = frozenset({10049, 99})


def check_udp_port_is_free(host: str, port: int, channel_name: str | None = None) -> None:
    """UDP 的归属探针：**bind 一次**（没有握手，只能这样问）。

    只能绑上才算"归我们"：Windows 上两个进程绑同一个 UDP 端口，第二家会吃
    `WSAEADDRINUSE`；而"这个地址不在本机"要单独说清楚（要去做 `net up`，不是去杀进程）。
    """
    who = f"channel {channel_name}: " if channel_name else ""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            if code in _ADDRESS_NOT_AVAILABLE or exc.errno in _ADDRESS_NOT_AVAILABLE:
                raise BackendStartError(
                    f"{who}cannot bind UDP {host}:{port}: this address is not configured "
                    f"on any local adapter ({exc.strerror}); run "
                    f"`python -m simbench net up --point-table <point table>` first, or fix "
                    f"the host in the point table") from exc
            raise BackendStartError(
                f"{who}UDP port {host}:{port} is already in use "
                f"({exc.strerror or exc}); another instance (or another program) holds it") \
                from exc


class ModbusUdpBackend(ModbusTcpBackend):
    """UDP 上的 Modbus 从站（MBAP 帧，与 TCP 同一份切帧/解帧）。"""

    TRANSPORT = "udp"
    SERVER_CLASS = ModbusUdpServer

    async def _probe(self) -> None:
        import asyncio

        await asyncio.to_thread(check_udp_port_is_free, self.channel.host, self.port,
                                self.channel_name)

    async def _open_extra(self, server) -> list:
        """UDP 暂不支持"声明里的额外地址"：**说一句**再返回空（静默丢等于悄悄少听一个地址）。"""
        if self.extra_endpoints:
            self._log_sink("warning",
                           f"channel {self.channel_name}: UDP 这一路只监听 "
                           f"{self.channel.host}:{self.port}；声明里的额外地址 "
                           f"{self.extra_endpoints} 这次没有挂上（UDP 的额外监听还没做）")
        return []

    def describe(self) -> str:
        return f"{self.channel.host}:{self.port} (UDP)"
