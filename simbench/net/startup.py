"""启动挂钩：起从站/开界面之前，按点表旁边那份网络声明把网卡对一遍。

这一层是"用户说要自动"那件事的落点，所以它的**失败口径**比别处松：网络没配好不许把
模拟器拦在门外。主站连不上是现场立刻看得见的现象，而"因为网卡没配好所以干脆不起服务"
只会让人以为软件坏了。所以这里只报话，不抛异常、不改退出码。

四条口径（每一条都对应一次现场判断）：

1. **没有声明文件时默认一声不吭**。IP 是环境相关的（现场、同事机器各不同），"这个工程没
   声明网络"是合法状态而不是错误；每次启动都念一遍只会让人学会忽略这行提示。**例外**是
   通道 host 写成了具体地址（不是 `0.0.0.0`/`127.0.0.1` 这类本机地址）：那等于把设备那一侧
   的 IP 塞进了通道自己的监听地址，本机不一定有它——这时**把它配到专用网卡上**
   （`_prepare_concrete_hosts`；只加不删，理由写在那里）。声明文件**坏了**（半截 JSON、
   端口写成字符串）则报一句原话
   ——那是真写错了，但同样不拦启动。
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
from .device import MANAGED_ADAPTER_NAME, find_managed_adapter, inf_available, loopback_inf_path
from .errors import NetError
from .ledger import Ledger
from .plan import foreign_owners, plan_addresses
from .state import read_adapters, read_addresses, read_all_addresses

# 启动挂钩的总闸。空值=开；`0/no/off/false/none` 都算关（写哪种都认，免得为个字面量吵架）。
AUTO_ENV = "SIMBENCH_NET_AUTO"
_OFF = frozenset({"0", "no", "off", "false", "none"})

# `net` 子命令每条输出都带这个前缀；这里回传前剥掉，调用方打自己那层前缀。
_PREFIX = "[net] "

# 这些 host 都表示"本机"或"所有本机地址"：不需要额外往网卡上加地址，也就不需要提示。
_LOCAL_HOSTS = frozenset({"", "0.0.0.0", "::", "*", "localhost", "127.0.0.1", "::1"})

# 没有声明、只能从通道 host 取地址时的掩码位数。与 `cli._parse_ip_spec` 同一个默认（现场惯例 /24）：
# 这里不猜掩码，只是把"裸 IP"变成 `net up --ip` 能吃的样子。
_DEFAULT_PREFIX = 24


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


def apply_up_ips(addresses: list[str]) -> tuple[int, list[str]]:
    """跑一次 `net up --ip ...`（点表旁边**没有**网络声明时用）。与 `apply_up` 同形。

    为什么需要第二条：`apply_up` 走的是 `net up --point-table`，而它在没有声明时会直接报
    "点表旁边没有网络声明"（见 `cli._desired_addresses`）。既然这一路的地址来源是通道 host，
    就把它们按 `--ip` 交过去——那是同一条命令、只是地址不从声明里取。
    """
    from .cli import main as net_main

    argv = ["up"]
    for address in addresses:
        argv += ["--ip", address]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = net_main(argv)
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
    if declaration is None:
        # 没有声明本身是合法状态（口径 1），但"通道 host 写成了具体地址"不是：那等于把设备那
        # 一侧的 IP 塞进了通道自己的监听地址，本机不一定有它。见 `_prepare_concrete_hosts`。
        return _prepare_concrete_hosts(point_table, project, apply)
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
    except Exception as exc:               # noqa: BLE001
        # 这里**故意**兜到 Exception，是全局唯一一处：网络这一步永远不许把模拟器拦在门外。
        # 没预料到的异常（缺 DLL、PowerShell 被策略挡掉、账本文件权限）也只该损失这条提示，
        # 不该让整个工具起不来；真出问题时上面那句原话会把异常点名，不吞成静默。
        return [f"网络准备时出了意外（{exc}），已跳过；模拟照常启动"]


def _concrete_hosts(project) -> list[tuple[str, str, object]]:
    """项目里"把具体地址写进通道 host"的 TCP 通道（纯看模型，**不读系统**）。"""
    if project is None:
        return []
    found = []
    for channel in project.channels:
        if channel_transport(channel) != "tcp":
            continue
        host = str(getattr(channel, "host", "") or "").strip()
        if host.lower() not in _LOCAL_HOSTS:
            found.append((channel.name, host, getattr(channel, "port", None)))
    return found


def _prepare_concrete_hosts(point_table, project, apply: bool) -> list[str]:
    """没有声明、但通道 host 写成了具体地址：**把它当"要配到专用网卡上的地址"**。

    为什么（2026-10-19 现场反馈）：用户写 `10.136.107.240:5020` 就是想在这台机器的这个地址上
    起从站；本机没这个地址时 bind 必然失败，看着就像"启动时没用虚拟网卡"。专用网卡本来就是
    干这件事的，网络声明只是**地址的来源之一**——通道 host 是另一个来源（一路一个地址）。

    三条边界与声明那条完全一致（都落在 `_prepare` 里）：只认专用网卡、已经一致时不提权、
    地址被别的网卡占着只报不抢。地址在不在本机、要不要提权，都由 `_prepare` 的只读对账决定；
    真动手时走 `net up --ip`（没有声明时 `--point-table` 那条路走不通）。

    通配/回环 host（`0.0.0.0` / `127.0.0.1` / `localhost` ...）不进来：它们本来就只听本机，
    不需要往网卡上加任何地址——那条路仍然一声不吭（口径 1）。
    """
    hosts = _concrete_hosts(project)
    if not hosts:
        return []
    addresses = sorted({host for _name, host, _port in hosts})
    name, host, port = hosts[0]
    more = f"（另有 {len(hosts) - 1} 路同样）" if len(hosts) > 1 else ""
    lead = [f"通道 {name} 的 host 是具体地址 {host}:{port}{more}：把 {len(addresses)} 个地址"
            f"配到专用网卡上（模拟器要在这个地址上监听）"]
    desired = [(address, _DEFAULT_PREFIX) for address in addresses]
    try:
        lines = _prepare(Path(point_table), desired, apply,
                         run=lambda: apply_up_ips(addresses),
                         command=_command_ips(addresses),
                         remove_stale=False)
    except NetError as exc:
        return [*lead, f"网络准备失败：{exc}；模拟照常启动"]
    except Exception as exc:               # noqa: BLE001 —— 与上面同一条口径：网络不许拦启动
        return [*lead, f"网络准备时出了意外（{exc}），已跳过；模拟照常启动"]
    return lead + lines


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
        return [f"声明：{netspec.companion_path(point_table)}",
                *_prepare(Path(point_table), desired, apply=False),
                *_unmatched_line(declaration, project)]
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


def _prepare(point_table: Path, desired: list[tuple[str, int]], apply: bool,
             *, run=None, command: str | None = None,
             remove_stale: bool = True) -> list[str]:
    """只读对账 + 按需提权。`run`/`command` 让"地址从哪来"可换（声明 / 通道 host）。

    默认是声明那条（`net up --point-table`）；通道 host 那条传 `apply_up_ips` 与 `net up --ip`
    的写法过来——两条路的判据、边界与报告口径完全共用，只是地址来源不同。

    `remove_stale=False` 只给通道 host 那条用：**声明能表达"这个工程要的完整集合"**，所以
    "账本记过、但现在不要了"就可以收回；而通道 host 只能表达"这一路要的地址"，据此收回等于
    把别的实例/别的点表正在用的地址拔掉（实测预览里就会出现"待收回 10.136.107.240"）。
    所以那条路只加不删，陈旧地址留给 `net clean` 或声明那条去收。
    """
    run = run or (lambda: apply_up(point_table))
    command = command or _command(point_table)
    ledger = Ledger.load()
    adapters = read_adapters()
    adapter = find_managed_adapter(adapters, ledger.owned_adapter_guids())

    if adapter is None:
        if not inf_available():
            return [f"还没建专用网卡 {MANAGED_ADAPTER_NAME}，而系统里没有 "
                    f"{loopback_inf_path().name}（环回适配器驱动），这一步要人工处理"]
        if not apply:
            return [f"需要建专用网卡 {MANAGED_ADAPTER_NAME} 并配 {len(desired)} 个地址",
                    command]
        return _report(run(), f"专用网卡 {MANAGED_ADAPTER_NAME} 已就绪", command)

    owned = ledger.owned(adapter.guid) if remove_stale else set()
    plan = plan_addresses(adapter.label, desired, read_addresses(adapter.if_index), owned)
    # 只关心**声明里那几个**地址有没有被别的网卡占着：机器上别的网卡当然有一堆地址
    # （WLAN 的、虚拟机的），把它们算进来会让"已经一致"永远判不出来。
    blocked = _blocked_elsewhere(adapter.if_index, desired)

    if plan.clean and not blocked:
        return [f"网络已就绪：{adapter.label}（{len(desired)} 个地址）"]

    occupied = [f"{ip} 已被 {blocked[ip]} 占用，需要人工处理" for ip in sorted(blocked)[:5]]

    # 一个都加不上时不做提权：`net up` 也加不上（同一个 IP 不能同时挂两块网卡），
    # 弹一次 UAC 只换来一句失败，不如直接说是谁占着。
    if not [ip for ip in plan.to_add if ip not in blocked]:
        return [f"网络对不上：{adapter.label}", *plan.summary(), *occupied]

    if not apply:
        return [f"网络待处理：{adapter.label}", *plan.summary(), *occupied, command]

    return _report(run(), f"网络已就绪：{adapter.label}", command)


def _blocked_elsewhere(if_index: int, desired: list[tuple[str, int]]) -> dict[str, str]:
    """声明要的地址里，哪些已经挂在**别的**网卡上：{ip: 网卡名}。"""
    names = {adapter.if_index: adapter.name for adapter in read_adapters()}
    others = {address.ip: names.get(address.if_index, f"ifIndex {address.if_index}")
              for address in read_all_addresses() if address.if_index != if_index}
    return foreign_owners(desired, others)


def _report(result: tuple[int, list[str]], ready: str,
            command: str | None = None) -> list[str]:
    """`net up` 的输出转成给启动路径看的话；成功且无话可说时就报一句"已就绪"。"""
    code, lines = result
    if code == 0:
        return lines or [ready]
    # `net up` 的部分成功（有几个地址加不上）也是回 2：把它的原话留下，再补一句"接下来去哪看"。
    retry = f"（手动重试：{command}）" if command else ""
    return [*lines, "网络没完全配好；模拟照常启动，可用 `python -m simbench net show` 看现状"
                    + retry]


def _command_ips(addresses: list[str]) -> str:
    joined = " ".join(f"--ip {address}" for address in addresses)
    return f"应用：python -m simbench net up {joined}"


def _command(point_table: Path) -> str:
    return f"应用：python -m simbench net up --point-table {point_table}"
