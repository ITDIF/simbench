"""启动挂钩：起从站/开界面之前，按点表旁边那份网络声明把网卡对一遍。

这一层是"用户说要自动"那件事的落点，所以它的**失败口径**比别处松：网络没配好不许把
模拟器拦在门外。主站连不上是现场立刻看得见的现象，而"因为网卡没配好所以干脆不起服务"
只会让人以为软件坏了。所以这里只报话，不抛异常、不改退出码。

四条口径（每一条都对应一次现场判断）：

1. **没有声明文件就一声不吭**。IP 是环境相关的（现场、同事机器各不同），"这个工程没声明
   网络"是合法状态而不是错误；每次启动都念一遍只会让人学会忽略这行提示。声明文件**坏了**
   （半截 JSON、端口写成字符串）则报一句原话——那是真写错了，但同样不拦启动。
2. **`SIMBENCH_NET_AUTO=0` 是总闸**。测试与 CI 靠它保证**绝不碰真网卡**：用例会装载
   `examples/` 下的点表，而现场那份点表旁边早晚会躺着一份 `.net.json`——没有这道闸，
   跑一次 pytest 就会真去改机器上的网卡地址。
3. **已经一致时不提权**。`net up` 自己会先看差异再决定要不要提权，但这里先算一遍是为了
   少一次没必要的 UAC：每次启动弹一次管理员确认，用户很快就会把整个功能关掉。
4. **只认专用网卡**。目标永远是 `simbench 模拟网卡`，绝不落到现场真实网卡上；认不出来时
   宁可报一句人话，也不去改别人的网卡（口径见 `device.py`）。

调用方只拿"要打给人看的几行"（`prepare` 的返回值），自己决定打到 stderr、状态栏还是日志。
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path

from ..project.model import channel_transport
from . import endpoints as netendpoints
from . import spec as netspec
from .device import (MANAGED_ADAPTER_NAME, find_managed_adapter, inf_available,
                     loopback_inf_path)
from .ledger import Ledger
from .plan import foreign_owners, plan_addresses
from .state import NetError, read_addresses, read_adapters, read_all_addresses

# 启动挂钩的总闸。空值=开；`0/no/off/false/none` 都算关（写哪种都认，免得为个字面量吵架）。
AUTO_ENV = "SIMBENCH_NET_AUTO"
_OFF = frozenset({"0", "no", "off", "false", "none"})

# `net` 子命令每条输出都带这个前缀；这里回传前剥掉，调用方打自己那层前缀。
_PREFIX = "[net] "


def auto_enabled() -> bool:
    """启动挂钩开着吗。测试与 CI 用 `SIMBENCH_NET_AUTO=0` 关掉。"""
    return os.environ.get(AUTO_ENV, "").strip().lower() not in _OFF


def apply_up(point_table) -> tuple[int, list[str]]:
    """跑一次 `net up`，返回 (退出码, 输出行)。

    进程内复用同一条命令，而不是在这里再写一遍"建网卡 + 加地址"：那两件事都要提权，
    合成一次才只弹一次 UAC（`_cmd_up` 的说明里写了同一条理由），而复制一份实现迟早
    会和命令行那条走岔。

    单独抽成模块级函数是为了让测试能整段换掉它——真的执行会弹 UAC 并改系统。
    """
    from .cli import main as net_main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = net_main(["up", "--point-table", str(point_table)])
    lines = [line.strip() for line in buffer.getvalue().splitlines() if line.strip()]
    return code, [_strip(line) for line in lines]


def _strip(line: str) -> str:
    return line[len(_PREFIX):] if line.startswith(_PREFIX) else line


def prepare(point_table, *, apply: bool = True, project=None) -> list[str]:
    """启动时自动那一路：把点表旁边的网络声明对上系统现状，返回要打给人看的几行。

    - 空列表 = 不用提（没有声明文件，或总闸关着）。
    - `apply=False` 只读：一条系统调用都不写（总闸仍然管用）。人主动看现状要用
      `status()`，两者差别见各自的说明。
    """
    if not auto_enabled() or not point_table:
        return []
    declaration, desired, problem = _declared(point_table)
    if problem:
        return [problem]
    # 先按**通道类型**过滤（纯串口工程一个地址都不配），再判空：过滤完为空就与"声明里没有
    # 设备地址"同一条路——一声不吭，而且**连系统都不读**（下面 `_prepare` 那几次读网卡/
    # 账本调用一个都不发）。审查发现的那条就是漏了这个过滤：一份纯串口的点表旁边躺着旧
    # `.net.json` 时，会把 ping 得通、上面没有从站的地址配到网卡上。
    desired = _tcp_endpoints(desired, declaration, project)
    if not desired:
        return []
    try:
        return _prepare(Path(point_table), desired, apply) \
            + _unmatched_line(declaration, project)
    except NetError as exc:
        return [f"网络准备失败：{exc}；模拟照常启动"]
    except Exception as exc:
        # 这里**故意**兜到 Exception，是全局唯一一处：网络这一步永远不许把模拟器拦在门外。
        # 没预料到的异常（缺 DLL、PowerShell 被策略挡掉、账本文件权限）也只该损失这条提示，
        # 不该让整个工具起不来；真出问题时上面那句原话会把异常点名，不吞成静默。
        return [f"网络准备时出了意外（{exc}），已跳过；模拟照常启动"]


def status(point_table, project=None) -> list[str]:
    """只读现状，给人**主动问**"现在网络怎么样"用（网络面板、`net show` 一类）。

    与 `prepare` 只有两处不同，都是"人问"与"自动"该有的差别：

    - **不受总闸管**：`SIMBENCH_NET_AUTO=0` 管的是"启动时别自动动手"，不是"不许看"；
    - **没有声明时明说一句**：面板上一片空白看不出是"没问题"还是"没声明"，
      而这两种情况要人做的事完全不同（一个什么都不用做，一个先去 `net init`）。
    """
    if not point_table:
        return ["还没有点表：先打开或导入一份点表，网络这一页才有对象"]
    declaration, desired, problem = _declared(point_table)
    if problem:
        return [problem]
    if declaration is None:
        return [f"点表旁边没有网络声明（{netspec.companion_path(point_table)}）；"
                "要按设备 IP 配网先生成一份"]
    if not desired:
        return ["网络声明里没有设备地址（devices 是空的）"]
    # 与 `prepare` 同一条过滤，但这里要给一句**为什么没对象**：面板上一片空白看不出是
    # "没问题"还是"这一页根本不管这事"（同"没有声明时明说一句"那条口径）。
    desired = _tcp_endpoints(desired, declaration, project)
    if not desired:
        return ["声明里的地址都不在 TCP 通道上（本工程只串口通道）：串口没有设备 IP:端口"
                "这回事，网络这一页没有对象"]
    try:
        # 第一行报"这份声明在哪"：现场最常见的问题是"我改的那份"和"它在看的那份"不是
        # 同一个文件（点表被复制过、声明放在别处），而这在差异清单里看不出来。
        return ([f"声明：{netspec.companion_path(point_table)}"]
                + _prepare(Path(point_table), desired, apply=False)
                + _unmatched_line(declaration, project))
    except NetError as exc:
        return [f"网络读不出来：{exc}"]


def _declared(point_table):
    """(声明, 去重后的地址清单, 一句要报的话)。没有声明文件时回 `(None, [], "")`。"""
    try:
        declaration = netspec.load_companion(point_table)
        desired = netspec.unique_addresses(declaration) if declaration else []
    except NetError as exc:
        # 声明写错了只报一句原话：它不该拦住模拟器（点表本身合法就起得来），但要让人看见，
        # 否则现场只会觉得"工具没反应"。
        return None, [], f"网络声明有问题：{exc}"
    return declaration, desired, ""


def _tcp_endpoints(desired, declaration, project) -> list[tuple[str, int]]:
    """只保留"名字挂在某个 TCP 通道上"的那几个声明地址。

    为什么（2026-10-13 全面审查发现）：串口通道没有"设备 IP:端口"这回事，
    `endpoints.for_channel` 已经对 RTU 回空、不额外挂监听；但**自动配网**这一侧原先只看声明
    文件，于是"一份纯串口的点表 + 旁边躺着一份旧 `.net.json`"会把一批地址配到网卡上——
    那些地址 ping 得通、上面却没有任何从站，正是本项目最防的"看着在、其实没有"。名字是唯一
    的纽带（与 `endpoints.for_channel` 同一条判据），所以按"名字出现在某个 TCP 通道里"过滤；
    纯串口工程直接回空，一行都不配。
    """
    if project is None or declaration is None:
        return list(desired)
    tcp_names = {device.name for channel in project.channels
                 if channel_transport(channel) == "tcp" for device in channel.devices}
    if not tcp_names:
        return []
    # 匹配按 **ip** 而不是 `(ip, port)`：`desired` 里每个元素是 `(ip, 掩码位数)`
    # （`spec.unique_addresses` 给的是"要配到网卡上的地址"，不是监听端口），拿端口去比
    # 永远比不上——实测过：这么写会把 TCP 工程的地址也一起滤掉，等于把这一整条判据变成
    # "什么都不配"。名字才是纽带（与 `endpoints.for_channel` 同一条），地址取声明里那个。
    return [endpoint for endpoint in desired
            if any(device.name in tcp_names and device.ip == endpoint[0]
                   for device in declaration.devices)]


def _unmatched_line(declaration, project) -> list[str]:
    """声明里点表找不到的那几台报一句：它们不会被监听，主站连上去只会连不通。"""
    missing = netendpoints.unmatched_names(declaration, project)
    if not missing:
        return []
    shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
    return [f"声明里有 {len(missing)} 台设备在点表里找不到（{shown}），它们不会被监听"
            "——名字要跟点表里的设备名一模一样"]


def _prepare(point_table: Path, desired: list[tuple[str, int]], apply: bool) -> list[str]:
    ledger = Ledger.load()
    adapters = read_adapters()
    adapter = find_managed_adapter(adapters, ledger.owned_adapter_guids())

    if adapter is None:
        if not inf_available():
            return [f"还没建专用网卡 {MANAGED_ADAPTER_NAME}，而系统里没有 "
                    f"{loopback_inf_path().name}（环回适配器驱动），这一步要人工处理"]
        if not apply:
            return [f"需要建专用网卡 {MANAGED_ADAPTER_NAME} 并配 {len(desired)} 个地址",
                    _command(point_table)]
        return _report(apply_up(point_table), f"专用网卡 {MANAGED_ADAPTER_NAME} 已就绪")

    plan = plan_addresses(adapter.label, desired, read_addresses(adapter.if_index),
                          ledger.owned(adapter.guid))
    # 只关心**声明里那几个**地址有没有被别的网卡占着：机器上别的网卡当然有一堆地址
    # （WLAN 的、虚拟机的），把它们算进来会让"已经一致"永远判不出来。
    blocked = _blocked_elsewhere(adapter.if_index, desired)

    if plan.clean and not blocked:
        return [f"网络已就绪：{adapter.label}（{len(desired)} 个地址）"]

    occupied = [f"{ip} 已被 {blocked[ip]} 占用，需要人工处理" for ip in sorted(blocked)[:5]]

    # 一个都加不上时不做提权：`net up` 也加不上（同一个 IP 不能同时挂两块网卡），
    # 弹一次 UAC 只换来一句失败，不如直接说是谁占着。
    if not [ip for ip in plan.to_add if ip not in blocked]:
        return [f"网络对不上：{adapter.label}"] + plan.summary() + occupied

    if not apply:
        return [f"网络待处理：{adapter.label}"] + plan.summary() + occupied + [
            _command(point_table)]

    return _report(apply_up(point_table), f"网络已就绪：{adapter.label}")


def _blocked_elsewhere(if_index: int, desired: list[tuple[str, int]]) -> dict[str, str]:
    """声明要的地址里，哪些已经挂在**别的**网卡上：{ip: 网卡名}。"""
    names = {adapter.if_index: adapter.name for adapter in read_adapters()}
    others = {address.ip: names.get(address.if_index, f"ifIndex {address.if_index}")
              for address in read_all_addresses() if address.if_index != if_index}
    return foreign_owners(desired, others)


def _report(result: tuple[int, list[str]], ready: str) -> list[str]:
    """`net up` 的输出转成给启动路径看的话；成功且无话可说时就报一句"已就绪"。"""
    code, lines = result
    if code == 0:
        return lines or [ready]
    # `net up` 的部分成功（有几个地址加不上）也是回 2：把它的原话留下，再补一句"接下来去哪看"。
    return lines + ["网络没完全配好；模拟照常启动，可用 `python -m simbench net show` 看现状"]


def _command(point_table: Path) -> str:
    return f"应用：python -m simbench net up --point-table {point_table}"
