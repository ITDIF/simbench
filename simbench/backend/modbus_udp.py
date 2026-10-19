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

from .base import BackendStartError
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
                    f"on any local adapter ({exc.strerror}); add it to a local adapter, or "
                    f"apply the project's network declaration with "
                    f"`python -m simbench net up --point-table <point table>`, or fix the "
                    f"host in the point table") from exc
            raise BackendStartError(
                f"{who}UDP port {host}:{port} is already in use "
                f"({exc.strerror or exc}); another instance (or another program) holds it") \
                from exc


class ModbusUdpBackend(ModbusTcpBackend):
    """UDP 上的 Modbus 从站（MBAP 帧，与 TCP 同一份切帧/解帧）。"""

    TRANSPORT = "udp"
    SERVER_CLASS = ModbusUdpServer

    async def _open_extra(self, server) -> list:
        """UDP 的"声明里额外要听的地址"：**通配绑定已经覆盖了**，不另起监听。

        为什么与 TCP 那支不一样：TCP 那一支靠 `loop.create_server` 给每个地址再挂一个监听，
        而 UDP 要"再起一个 server"——可 pymodbus 的两个 server **各建一份 `SimRuntime`**
        （`SimCore(devices)` 里是 `SimRuntime(device)`），于是两个地址读到两份寄存器：主站
        从 A 地址写、界面按主 server 读回旧值，两边都不报错（实测踩过）。真正共享得走它那套
        legacy context，那与本项目的 `SimDevice` 路是两套东西，不值得为它分叉。

        好在**大概率用不着**：点表里 UDP 通道的 host 默认就是 `0.0.0.0`，那本来就在所有本机
        地址上听（声明里那几个设备 IP 也就都在里面了）。只有"绑死某一块网卡 + 声明里另有
        地址"这一种组合真覆盖不到——那种情况**说一句**（静默少听一个地址是现场最难查的）。
        """
        if not self.extra_endpoints or self._binds_every_address():
            return []
        self._log_sink("warning",
                       f"channel {self.channel_name}: UDP 这一路绑在 "
                       f"{self.channel.host}:{self.port} 上，声明里的额外地址 "
                       f"{self.extra_endpoints} 没有挂上（UDP 不支持再挂监听；把 host 改成 "
                       f"0.0.0.0 就能一并听到）")
        return []

    def _binds_every_address(self) -> bool:
        """这一路是不是绑在所有本机地址上（`0.0.0.0` / `::` / 空 = 所有网卡）。"""
        return self.channel.host.strip() in ("", "0.0.0.0", "::", "*")

    @property
    def endpoints(self) -> list[tuple[str, int]]:
        """**只报真在听的那些**：UDP 不挂额外监听（见 `_open_extra`），而 `endpoints` 的
        语义是"这个后端在听的地址"——把声明里那几个没挂上的地址报出去就是撒谎（界面那一栏
        与 `--list` 都会照着念）。通配绑定时它们**确实**在听，但那是"这一个地址覆盖了它们"，
        不是"多挂了几个监听"。
        """
        return [(self.channel.host, self.port)]

    def describe(self) -> str:
        return f"{self.channel.host}:{self.port} (UDP)"
