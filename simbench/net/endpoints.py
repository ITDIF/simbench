"""网络声明 -> 这一路通道要额外监听哪些 (ip, port)。

主站连的是"设备那一侧的地址"：声明说 `10.136.107.240` 是风机-1，主站就会去连
`10.136.107.240:502`。所以监听不能只在点表自己的 `host:port` 上——那张点表通常写
`0.0.0.0:5020`，是给本机自测用的，主站按设备 IP 连 502 时上面什么都没有。

**名字是唯一的纽带**：`net init` 就是按点表里的设备名生成声明的，所以名字对得上才算
这一路的。对不上的不当成错误（多通道工程里另一半名字属于别的通道），但也不许静默——
`unmatched_names()` 把这几台交出去，启动挂钩与网络面板各报一句。

按名字配对而不是按顺序配对：顺序配在点表增删一台设备之后会把 IP 整体错位一格，
那种错配是静默的，现场只看到"设备读数全不对"。

三条"不重复听"的口径（否则启动就会撞上自己刚绑过的地址）：

1. 通道自己的 `host:port` 本来就在听，同一个端点不再挂第二个；
2. 通道 host 是通配（`0.0.0.0`/`::`/空）时，**同端口的**声明地址已经被它盖住
   （Windows 上通配与具体地址绑同一个端口会 10048），不再重复挂；
3. 同一 (ip, port) 只挂一次。
"""

from __future__ import annotations

from pathlib import Path

from ..project.model import channel_transport, is_socket
from . import spec as netspec
from .errors import NetError

# 通道 host 取这些值时视为"所有本机地址都在听"
_WILDCARD = frozenset({"", "0.0.0.0", "::", "*"})


def declared(point_table) -> list[tuple[str, int]]:
    """声明里出现过的 (ip, port)，去重排序。没有声明、或声明读不出来都回空列表。"""
    declaration = _load(point_table)
    if declaration is None:
        return []
    return sorted({(device.ip, device.port) for device in declaration.devices})


def for_channel(channel, point_table) -> list[tuple[str, int]]:
    """这一路通道要**额外**监听哪些 (ip, port)（已经听到的那些不算）。

    纯函数，不读系统：要不要听只由点表与声明决定，与"现在网卡上有什么地址"无关——
    后一件事是 `net check` 的活，不是监听层的。
    """
    # 串口通道不额外挂监听：RTU 是串口上的一条总线，没有"设备 IP:端口"这回事。
    # 声明里同名的设备因此一直"没被监听"，但那是这一层该有的结论，不是错误。
    if not is_socket(channel_transport(channel)):
        return []
    declaration = _load(point_table)
    if declaration is None or not declaration.devices:
        return []
    names = {device.name for device in channel.devices}
    wanted = {(device.ip, device.port)
              for device in declaration.devices if device.name in names}
    return sorted(endpoint for endpoint in wanted
                  if not covered(channel.host, channel.port, *endpoint))


def unmatched_names(declaration, project) -> list[str]:
    """声明里有、点表里找不到的设备名。空列表 = 名字全对得上。

    只算名字，不看 IP：判据与 `for_channel` 同一条（名字是唯一的纽带），
    否则两处各判一次，迟早分叉成"监听说对上了、面板说找不到"。
    """
    if declaration is None or project is None:
        return []
    known = {device.name for channel in project.channels for device in channel.devices}
    return sorted(device.name for device in declaration.devices
                  if device.name not in known)


def covered(host: str, port: int, ip: str, endpoint_port: int) -> bool:
    """`ip:endpoint_port` 是否已经被监听 `host:port` 的那一份盖住了。"""
    if endpoint_port != port:
        return False
    if (host or "").strip() in _WILDCARD:
        return True
    return ip == host


def _load(point_table):
    """读点表旁边的声明。读不出来（半截 JSON）回 None——那是网络那一步要报的错，
    监听层照常按通道自己的地址起，不许因此把从站拦在门外。"""
    if not point_table:
        return None
    try:
        return netspec.load_companion(Path(point_table))
    except NetError:
        return None
