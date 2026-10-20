"""启动挂钩：起从站/开界面之前，按点表旁边的网络声明和通道 host 把网卡对一遍。

这一层是"用户说要自动"那件事的落点，所以它的**失败口径**比别处松：网络没配好不许把
模拟器拦在门外。主站连不上是现场立刻看得见的现象，而"因为网卡没配好所以干脆不起服务"
只会让人以为软件坏了。所以这里只报话，不抛异常、不改退出码。

几条口径（每条都对应一次现场判断）：

1. **没有声明文件时默认一声不吭**。IP 是环境相关的（现场、同事机器各不同），"这个工程没
   声明网络"是合法状态而不是错误。**例外**是通道 host 写成了具体 IPv4：那等于把设备那一侧
   的 IP 塞进了通道自己的监听地址，本机不一定有它——这时把它配到专用网卡上（只加不删）。
   声明文件**坏了**（半截 JSON、端口写成字符串）则报一句原话，但同样不拦启动。
2. **地址来源取并集**。声明文件存在时，声明地址和通道 host 都可能是模拟器要监听的地址；
   以前有声明就只看声明，界面上改了 host 之后新 IP 永远配不上，表现成"第一次/第二次都起不来"。
3. **`SIMBENCH_NET_AUTO=0` 是总闸**。测试与 CI 靠它保证**绝不碰真网卡**。
4. **已经一致时不提权**。`net up` 自己会先看差异再决定要不要提权，但这里先算一遍是为了
   少一次没必要的 UAC。核对成功后进程内缓存一小段时间，重复点启动不再跑 PowerShell。
5. **只认专用网卡**。目标永远是 `simbench 模拟网卡`，绝不落到现场真实网卡上。
6. **启动前最后一道门是 bind**。地址在 `Get-NetIPAddress` 里不等于现在能监听；主窗会把
   最终地址集合交给 `wait_until_bindable()`，不能监听时给出具体原因，而不是把英文 bind
   错误直接甩给下层。
"""

from __future__ import annotations

import io
import ipaddress
import os
import socket
import threading
import time
from pathlib import Path

from ..project.model import channel_transport, is_s7, is_socket
from . import endpoints as netendpoints
from . import spec as netspec
from .device import MANAGED_ADAPTER_NAME, find_managed_adapter, inf_available, loopback_inf_path
from .errors import NetError
from .ledger import Ledger
from .plan import foreign_owners, plan_addresses
from .state import read_adapters, read_all_addresses

# 启动挂钩的总闸。空值=开；`0/no/off/false/none` 都算关。
AUTO_ENV = "SIMBENCH_NET_AUTO"
_OFF = frozenset({"0", "no", "off", "false", "none"})

# `net` 子命令每条输出都带这个前缀；这里回传前剥掉，调用方打自己那层前缀。
_PREFIX = "[net] "

# 这些 host 都表示"本机"或"所有本机地址"：不需要额外往网卡上加地址，也就不需要提示。
_LOCAL_HOSTS = frozenset({"", "0.0.0.0", "::", "*", "localhost", "127.0.0.1", "::1"})

# `net up` 成功之后最多等多久让系统把地址真正挂上（`New-NetIPAddress` 返回 != 立刻能 bind）。
_APPLY_WAIT = 5.0

# 没有声明、只能从通道 host 取地址时的掩码位数。现场惯例 /24。
_DEFAULT_PREFIX = 24

# 一次完整核对成功后，重复启动在这么长时间内直接走 bind 快路，不再起 PowerShell。
# 地址被外部改掉时 bind 探针会失败，快路自动失效。
_VERIFIED_TTL = 300.0
_verified_lock = threading.Lock()
_verified: dict[tuple, tuple[float, str]] = {}


def auto_enabled() -> bool:
    """启动挂钩开着吗。测试与 CI 用 `SIMBENCH_NET_AUTO=0` 关掉。"""
    return os.environ.get(AUTO_ENV, "").strip().lower() not in _OFF


def forget_verified() -> None:
    """清掉"已核对"缓存。用例与诊断工具用。"""
    with _verified_lock:
        _verified.clear()


def _cache_key(point_table, desired) -> tuple:
    raw = str(point_table or "")
    path = os.path.abspath(raw) if raw else ""
    try:
        stamp = Path(raw).stat().st_mtime_ns if raw else 0
    except OSError:
        stamp = 0
    return (path, stamp, tuple(sorted((ip, int(prefix)) for ip, prefix in desired)))


def _remember_verified(point_table, desired, label: str) -> None:
    if not desired:
        return
    with _verified_lock:
        _verified[_cache_key(point_table, desired)] = (time.monotonic() + _VERIFIED_TTL, label)


def _verified_label(point_table, desired) -> str | None:
    """缓存里有没有一份仍然有效的"已核对"结论；同时用 bind 探针确认地址还在。"""
    if not desired:
        return None
    key = _cache_key(point_table, desired)
    now = time.monotonic()
    with _verified_lock:
        entry = _verified.get(key)
        if entry is None:
            return None
        expires, label = entry
        if now >= expires:
            _verified.pop(key, None)
            return None
    if any(not _binds_locally(ip) for ip, _prefix in desired):
        with _verified_lock:
            _verified.pop(key, None)
        return None
    return label


def _is_local_host(host: str) -> bool:
    text = (host or "").strip().lower()
    if text in _LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _host_ip(host: str) -> tuple[str | None, str]:
    """host 是不是一个可以自动配到网卡上的 IPv4 字面量。"""
    text = (host or "").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None, "不是 IPv4 字面量（看起来像主机名），不会自动配到网卡上"
    if address.version != 4:
        return None, "是 IPv6 地址，自动配网目前只支持 IPv4"
    return str(address), ""


def _wait_until_bindable(addresses: list[str], timeout: float = _APPLY_WAIT) -> list[str]:
    """等系统把刚配上的地址真正生效；返回**还没生效**的那几个。

    判据是 `_binds_locally`（bind 一次）。`New-NetIPAddress` 返回之后，地址到真正可以 bind
    之间有一个很短的窗口（DAD/网卡状态）；不等就会"第一次点启动失败、第二次成功"。
    """
    pending = list(dict.fromkeys(addresses))
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        pending = [address for address in pending if not _binds_locally(address)]
        if pending:
            time.sleep(0.05)
    return pending


def wait_until_bindable(addresses: list[str], timeout: float = _APPLY_WAIT) -> list[str]:
    """公开的最终启动门：返回仍然不能监听的地址。"""
    return _wait_until_bindable(list(addresses), timeout)


def _after_apply(lines: list[str], addresses: list[str],
                 timeout: float = _APPLY_WAIT) -> list[str]:
    """`net up` 之后补一句：地址还没生效就说出来，别让操作员以为已经好了。"""
    pending = _wait_until_bindable(addresses, timeout)
    if pending:
        return [*lines, f"地址已配置但还没生效（{', '.join(pending)}）；"
                        "稍等几秒再启动；仍不行请检查地址冲突或网卡状态"]
    return lines


class _ThreadLocalStream(io.TextIOBase):
    """按线程分流的标准流代理：当前线程若登记了缓冲，写入进缓冲；其余线程原样走真流。

    为什么不用 `contextlib.redirect_stdout`：它是**进程全局**替换 `sys.stdout`。启动路径跑在
    `run_waiting` 的工作线程里，一旦替换，引擎线程和其它线程的输出会被吞进这里的缓冲。
    这里只在第一次用到时装一次代理，之后各线程互不干扰。
    """

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def capture(self, buffer):
        self._local.buffer = buffer

    def release(self):
        self._local.buffer = None

    def _target(self):
        return getattr(self._local, "buffer", None) or self._real

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        return self._target().flush()

    def isatty(self):
        return False

    def fileno(self):
        return self._real.fileno()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")


_stream_lock = threading.Lock()


def _install_stream_proxies() -> None:
    import sys

    with _stream_lock:
        if not isinstance(sys.stdout, _ThreadLocalStream):
            sys.stdout = _ThreadLocalStream(sys.stdout)
        if not isinstance(sys.stderr, _ThreadLocalStream):
            sys.stderr = _ThreadLocalStream(sys.stderr)


def _run_net(argv: list[str]) -> tuple[int, list[str]]:
    """在当前线程里跑一次 `net` 子命令，收回它的输出行（去掉 `[net] ` 前缀）。

    只影响本线程：其它线程的输出照常直通真正的 stdout/stderr。
    """
    import sys

    from .cli import main as net_main

    _install_stream_proxies()
    buffer = io.StringIO()
    out, err = sys.stdout, sys.stderr
    out.capture(buffer)
    err.capture(buffer)
    try:
        code = net_main(argv)
    finally:
        out.release()
        err.release()
    lines = [line.strip() for line in buffer.getvalue().splitlines() if line.strip()]
    return code, [_strip(line) for line in lines]


def apply_up(point_table, extra_addresses: list[str] = (), *,
             wait: float = _APPLY_WAIT) -> tuple[int, list[str]]:
    """跑一次 `net up --point-table`，可附带几个只来自通道 host 的地址。

    进程内复用同一条命令，而不是在这里再写一遍"建网卡 + 加地址"：那两件事都要提权，
    合成一次才只弹一次 UAC，而复制一份实现迟早会和命令行那条走岔。
    """
    extra = list(dict.fromkeys(str(ip) for ip in extra_addresses))
    argv = ["up", "--point-table", str(point_table)]
    for address in extra:
        argv += ["--ip", address]
    code, lines = _run_net(argv)
    if code in (0, 2):
        _decl, desired, _problem = _declared(point_table)
        addresses = [ip for ip, _prefix in desired] + extra
        lines = _after_apply(lines, addresses, wait)
    return code, lines


def apply_up_ips(addresses: list[str], *, wait: float = _APPLY_WAIT,
                 keep_stale: bool = True) -> tuple[int, list[str]]:
    """跑一次 `net up --ip ...`（没有声明、或只配通道 host 时用）。与 `apply_up` 同形。

    `keep_stale=True` 是 host 那条路的默认口径：通道 host 只表达"这一路要的地址"，
    不能据此收回别的实例/别的点表正在用的地址。
    """
    values = list(dict.fromkeys(str(ip) for ip in addresses))
    argv = ["up"]
    for address in values:
        argv += ["--ip", address]
    if keep_stale:
        argv.append("--keep-stale")
    code, lines = _run_net(argv)
    if code in (0, 2):
        lines = _after_apply(lines, values, wait)
    return code, lines


def _strip(line: str) -> str:
    return line[len(_PREFIX):] if line.startswith(_PREFIX) else line


# 声明文件的解析结果按 (路径, mtime) 记一份：一次启动里 `_wanted` / `apply_up` /
# `_after_apply` / `required_addresses` 会各自要一遍同一份声明，以前每次都重新读盘解析。
# 文件改过 mtime 就变，所以不会拿到旧内容；只存最近几份，防止无限增长。
_DECL_CACHE_MAX = 8
_decl_lock = threading.Lock()
_decl_cache: dict[tuple, tuple[object, str | None]] = {}


def _decl_key(point_table) -> tuple:
    raw = str(point_table or "")
    try:
        stamp = Path(raw).stat().st_mtime_ns if raw else 0
    except OSError:
        stamp = 0
    target = netspec.companion_path(point_table) if raw else None
    try:
        decl_stamp = target.stat().st_mtime_ns if target and target.is_file() else 0
    except OSError:
        decl_stamp = 0
    return (os.path.abspath(raw) if raw else "", stamp, decl_stamp)


def _load_declaration(point_table):
    """(声明或 None, 解析出的错误原文或 None)。同一份文件只解析一次（见上）。"""
    key = _decl_key(point_table)
    with _decl_lock:
        hit = _decl_cache.get(key)
    if hit is not None:
        return hit
    try:
        value = (netspec.load_companion(point_table), None)
    except NetError as exc:
        value = (None, str(exc))
    with _decl_lock:
        if len(_decl_cache) >= _DECL_CACHE_MAX:
            _decl_cache.pop(next(iter(_decl_cache)))
        _decl_cache[key] = value
    return value


def _declared(point_table):
    """(声明, 去重后的地址清单, 一句要报的话)。没有声明文件时回 `(None, [], "")`。"""
    declaration, error = _load_declaration(point_table)
    if error is not None:
        return None, [], f"网络声明有问题：{error}"
    desired = netspec.unique_addresses(declaration) if declaration else []
    return declaration, desired, ""


def _socket_endpoints(desired, declaration, project) -> list[tuple[str, int]]:
    """只保留"名字挂在某个 socket 通道上"的声明地址。

    监听层 `endpoints.for_channel` 支持 tcp/udp/rtutcp；配网层必须用同一条判据，
    否则声明里的 UDP/RTUTCP 地址会被监听层尝试绑定，却不会先配到网卡上。
    """
    if project is None or declaration is None:
        return list(desired)
    names = {device.name for channel in project.channels
             if is_socket(channel_transport(channel)) for device in channel.devices}
    if not names:
        return []
    return [endpoint for endpoint in desired
            if any(device.name in names and device.ip == endpoint[0]
                   for device in declaration.devices)]


# 兼容旧名字：它原来只认 TCP，现在判据已经扩到三种 socket 传输。
_tcp_endpoints = _socket_endpoints


def _has_host(transport: str) -> bool:
    """这一路有没有 host:port 要监听（tcp/udp/rtutcp/s7）。"""
    return is_socket(transport) or is_s7(transport)


def _concrete_hosts(project) -> list[tuple[str, str, object, str]]:
    """项目里"把具体地址写进通道 host"的那些通道（纯看模型，**不读系统**）。"""
    if project is None:
        return []
    found = []
    for channel in project.channels:
        transport = channel_transport(channel)
        if not _has_host(transport):
            continue
        host = str(getattr(channel, "host", "") or "").strip()
        if _is_local_host(host):
            continue
        found.append((channel.name, host, getattr(channel, "port", None), transport))
    return found


def _host_addresses(hosts) -> tuple[dict[str, str], list[str]]:
    """把通道 host 分成"可以配的 IPv4"和"要报给人听的原因"。"""
    addresses: dict[str, str] = {}
    notes: list[str] = []
    for name, host, port, _transport in hosts:
        ip, why = _host_ip(host)
        if ip is not None:
            addresses.setdefault(ip, f"通道 {name} 的 host {host}:{port}")
        else:
            notes.append(f"通道 {name} 的 host {host}:{port} {why}")
    return addresses, notes


def _wanted(point_table, project):
    """(声明, 期望地址 {ip: prefix}, host 来源的 IP, 声明来源的 IP, 提示, 错误)。

    这是配网层唯一一份"这个工程到底要哪些地址"的判定。声明与通道 host 取并集，
    不再二选一；host 只接受 IPv4 字面量，主机名/IPv6 变成一句能读的提示。
    """
    declaration, declared, problem = (None, [], "")
    if point_table:
        declaration, declared, problem = _declared(point_table)
        if problem:
            return declaration, {}, set(), set(), [], problem
    declared_ips = {}
    if declaration is not None:
        for ip, prefix in _socket_endpoints(declared, declaration, project):
            declared_ips[ip] = prefix
    host_map, notes = _host_addresses(_concrete_hosts(project))
    host_ips = set(host_map)
    wanted = dict(declared_ips)
    for ip in host_ips:
        wanted.setdefault(ip, _DEFAULT_PREFIX)
    return declaration, wanted, host_ips, set(declared_ips), notes, ""


def required_addresses(point_table, project=None) -> list[str]:
    """启动前必须能 bind 的地址集合；不读系统，不产生副作用。"""
    if not auto_enabled():
        return []
    _decl, wanted, _host_ips, _declared_ips, _notes, problem = _wanted(point_table, project)
    if problem:
        return []
    return sorted(wanted)


def describe_unbindable(addresses: list[str], limit: int = 5) -> list[str]:
    """给"等不到 bind"的地址补一句人话。只在失败路径调用，允许读系统。"""
    pending = list(dict.fromkeys(addresses))
    if not pending:
        return []
    try:
        adapters = read_adapters()
        records = read_all_addresses()
    except Exception as exc:                 # noqa: BLE001
        return [f"地址暂时不能监听：{', '.join(pending[:limit])}（读取网卡状态失败：{exc}）"]
    by_if = {adapter.if_index: adapter for adapter in adapters}
    by_ip = {address.ip: address for address in records}
    lines = []
    for ip in pending[:limit]:
        record = by_ip.get(ip)
        if record is None:
            lines.append(f"{ip} 还没有出现在系统网卡上；网络准备可能没有成功")
            continue
        adapter = by_if.get(record.if_index)
        where = adapter.label if adapter is not None else f"ifIndex {record.if_index}"
        state = (record.state or "").lower()
        if adapter is not None and not adapter.up:
            lines.append(f"{ip} 所在网卡 {where} 当前状态是 {adapter.status}；请先启用网卡")
        elif state and state not in ("preferred", "unknown"):
            lines.append(f"{ip} 在 {where} 上的状态是 {record.state}（可能正在生效或地址冲突）")
        else:
            lines.append(f"{ip} 在 {where} 上，但系统还没允许监听；稍等几秒，仍不行请检查网卡/驱动")
    if len(pending) > limit:
        lines.append(f"…另有 {len(pending) - limit} 个地址同样不可用")
    return lines


def prepare(point_table, *, apply: bool = True, project=None) -> list[str]:
    """把声明地址与通道 host 对上系统现状，返回要打给人看的几行。

    - 空列表 = 不用提（没有声明、没有具体 host，或总闸关着）。
    - `apply=False` 只读：一条系统写都不发（总闸仍然管用）。
    """
    if not auto_enabled() or not point_table:
        return []
    declaration, wanted, host_ips, declared_ips, notes, problem = _wanted(point_table, project)
    if problem:
        return [problem]
    if not wanted:
        return list(notes)
    desired = list(wanted.items())
    if apply:
        label = _verified_label(point_table, desired)
        if label is not None:
            return [f"网络已就绪：{label}（{len(desired)} 个地址）",
                    *notes, *_unmatched_line(declaration, project)]
    try:
        if not declared_ips:
            return _prepare_concrete_hosts(point_table, project, apply)
        extra = sorted(host_ips - declared_ips)
        lead = []
        if extra:
            lead = [f"通道 host 另需 {len(extra)} 个地址：{'、'.join(extra)}（与声明一起配置）"]
        lines = _prepare(Path(point_table), desired, apply,
                         run=lambda: apply_up(point_table, extra),
                         command=_command_with_hosts(Path(point_table), extra),
                         remove_stale=True)
        return lead + lines + notes + _unmatched_line(declaration, project)
    except NetError as exc:
        return [f"网络准备失败：{exc}；模拟照常启动", *notes]
    except Exception as exc:               # noqa: BLE001
        return [f"网络准备时出了意外（{exc}），已跳过；模拟照常启动", *notes]


def _binds_locally(host: str) -> bool:
    """这个地址现在能不能 bind 上（毫秒级、不起进程、不读系统）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
        return True
    except OSError:
        return False


def _prepare_concrete_hosts(point_table, project, apply: bool) -> list[str]:
    """没有声明、但通道 host 写成了具体地址：把它当"要配到专用网卡上的地址"。

    只加不删：通道 host 不能表达"这个工程的完整地址集合"，据此删地址会拔掉别的实例/别的
    点表正在用的地址。真正收回由声明那条或 `net clean` 负责。
    """
    _decl, wanted, _host_ips, _declared_ips, notes, problem = _wanted(point_table, project)
    if problem:
        return [problem]
    if not wanted:
        return list(notes)
    hosts = [item for item in _concrete_hosts(project) if _host_ip(item[1])[0] is not None]
    if not hosts:
        return list(notes)
    addresses = sorted(wanted)
    name, host, port, _transport = hosts[0]
    more = f"（另有 {len(hosts) - 1} 路同样）" if len(hosts) > 1 else ""
    if apply:
        label = _verified_label(point_table, list(wanted.items()))
        if label is not None:
            # 只有**完整核对过专用网卡**的地址才走这条快路。仅凭 bind 成功只能证明
            # "本机某块网卡上有它"，不能证明"它在 simbench 专用网卡上"——地址挂在
            # WLAN/物理网卡上时，静默走快路正是现场说的"没用虚拟网卡"。
            return [f"通道 {name} 的 host {host}:{port}{more} 已在专用网卡上"
                    f"（{label}），无需改动", *notes]
    lead = [f"通道 {name} 的 host 是具体地址 {host}:{port}{more}：把 {len(addresses)} 个地址"
            f"配到专用网卡上（模拟器要在这个地址上监听）"]
    try:
        lines = _prepare(Path(point_table) if point_table else None, list(wanted.items()), apply,
                         run=lambda: apply_up_ips(addresses),
                         command=_command_ips(addresses),
                         remove_stale=False)
    except NetError as exc:
        return [*lead, f"网络准备失败：{exc}；模拟照常启动", *notes]
    except Exception as exc:               # noqa: BLE001
        return [*lead, f"网络准备时出了意外（{exc}），已跳过；模拟照常启动", *notes]
    return [*lead, *lines, *notes]


def apply_for_start(point_table, project=None) -> tuple[int, list[str]]:
    """网络页"应用"按钮：按与启动相同的地址集合真正执行一次。

    有声明时把声明地址和通道 host 一起交给 `net up`；没有声明时只配 host。
    返回 `(退出码, 人话)`，异常收成一句而不是炸到 Qt 槽里。
    """
    _declaration, wanted, host_ips, declared_ips, notes, problem = _wanted(point_table, project)
    if problem:
        return 2, [problem]
    if not wanted:
        return 0, list(notes)
    try:
        if not declared_ips:
            return apply_up_ips(sorted(wanted), keep_stale=True)
        extra = sorted(host_ips - declared_ips)
        return apply_up(point_table, extra)
    except NetError as exc:
        return 2, [f"网络准备失败：{exc}"]
    except Exception as exc:               # noqa: BLE001
        return 2, [f"网络准备时出了意外（{exc}）"]


def prepare_project(project, *, apply: bool = True) -> list[str]:
    """主窗"启动"那条路：**没有点表文件**也能把通道 host 配到专用网卡上。"""
    if not auto_enabled():
        return []
    return _prepare_concrete_hosts(None, project, apply)


def status(point_table, project=None) -> list[str]:
    """只读现状，给人主动问"现在网络怎么样"用（网络面板、`net show` 一类）。"""
    if not point_table:
        return ["还没有点表：先打开或导入一份点表，网络这一页才有对象"]
    declaration, wanted, _host_ips, _declared_ips, notes, problem = _wanted(point_table, project)
    if problem:
        return [problem]
    if declaration is None:
        if not wanted:
            return [f"点表旁边没有网络声明（{netspec.companion_path(point_table)}）；"
                    "要按设备 IP 配网先生成一份", *notes]
        try:
            return ["没有网络声明：按通道 host 配网",
                    *_prepare(Path(point_table), list(wanted.items()), apply=False), *notes]
        except NetError as exc:
            return [f"网络读不出来：{exc}", *notes]
    if not wanted:
        return ["网络声明里没有设备地址（devices 是空的），也没有具体 host 要配", *notes]
    try:
        return [f"声明：{netspec.companion_path(point_table)}",
                *_prepare(Path(point_table), list(wanted.items()), apply=False),
                *notes, *_unmatched_line(declaration, project)]
    except NetError as exc:
        return [f"网络读不出来：{exc}"]
    except Exception as exc:               # noqa: BLE001
        return [f"网络读不出来：{exc}"]


def _unmatched_line(declaration, project) -> list[str]:
    """声明里点表找不到的那几台报一句：它们不会被监听，主站连上去只会连不通。"""
    missing = netendpoints.unmatched_names(declaration, project)
    if not missing:
        return []
    shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
    return [f"声明里有 {len(missing)} 台设备在点表里找不到（{shown}），它们不会被监听"
            "——名字要跟点表里的设备名一模一样"]


def _prepare(point_table: Path | None, desired: list[tuple[str, int]], apply: bool,
             *, run=None, command: str | None = None,
             remove_stale: bool = True) -> list[str]:
    """只读对账 + 按需提权。`run`/`command` 让"地址从哪来"可换。"""
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
        result = run()
        lines = _report(result, f"专用网卡 {MANAGED_ADAPTER_NAME} 已就绪", command)
        if result[0] == 0:
            pending = [ip for ip, _prefix in desired if not _binds_locally(ip)]
            if not pending:
                _remember_verified(point_table, desired, MANAGED_ADAPTER_NAME)
        return lines

    # 一次读完地址表，目标网卡与"别的网卡占着谁"都从这份快照里算：
    # 以前这里要起四次 PowerShell（read_adapters + read_addresses + 两次 _blocked_elsewhere）。
    records = read_all_addresses()
    names = {item.if_index: item.name for item in adapters}
    present = [item for item in records if item.if_index == adapter.if_index]
    others = {item.ip: names.get(item.if_index, f"ifIndex {item.if_index}")
              for item in records if item.if_index != adapter.if_index}
    owned = ledger.owned(adapter.guid) if remove_stale else set()
    plan = plan_addresses(adapter.label, desired, present, owned)
    blocked = foreign_owners(desired, others)

    if plan.clean and not blocked:
        pending = [ip for ip, _prefix in desired if not _binds_locally(ip)]
        if not pending:
            _remember_verified(point_table, desired, adapter.label)
            return [f"网络已就绪：{adapter.label}（{len(desired)} 个地址）"]
        # 地址在列表里但当前不能监听：可能是 DAD 还没结束、地址重复、网卡刚启用。
        # 网卡明确不在 Up 时仍值得跑一次 `net up`：它会先启用专用网卡。
        if apply and not adapter.up:
            result = run()
            lines = _report(result, f"网络已就绪：{adapter.label}", command)
            pending = [ip for ip, _prefix in desired if not _binds_locally(ip)]
            if result[0] == 0 and not pending:
                _remember_verified(point_table, desired, adapter.label)
                return lines
            if result[0] == 0:
                # `net up` 成功时 `_after_apply` 已经等过、并在 lines 里说过"还没生效"了，
                # 这里再补一句只是同一件事说第二遍。
                return lines
            return [*lines, f"地址已配置但当前不能监听：{'、'.join(pending[:5])}",
                    "通常是地址还在生效；稍等几秒再启动，仍不行请检查地址冲突或网卡状态"]
        # 网卡是 Up 的：再跑 net up 只会多一次 UAC，不会让 DAD/重复地址更快结束。
        return [f"网络尚未就绪：{adapter.label}",
                f"地址已配置但当前不能监听：{'、'.join(pending[:5])}",
                "通常是地址还在生效；稍等几秒再启动，仍不行请检查地址冲突或网卡状态"]

    occupied = [f"{ip} 已被 {blocked[ip]} 占用，需要人工处理" for ip in sorted(blocked)[:5]]
    if not [ip for ip in plan.to_add if ip not in blocked]:
        return [f"网络对不上：{adapter.label}", *plan.summary(), *occupied]
    if not apply:
        return [f"网络待处理：{adapter.label}", *plan.summary(), *occupied, command]
    result = run()
    lines = _report(result, f"网络已就绪：{adapter.label}", command)
    if result[0] == 0:
        pending = [ip for ip, _prefix in desired if not _binds_locally(ip)]
        if not pending:
            _remember_verified(point_table, desired, adapter.label)
    return lines


def _report(result: tuple[int, list[str]], ready: str,
            command: str | None = None) -> list[str]:
    """`net up` 的输出转成给启动路径看的话；成功且无话可说时就报一句"已就绪"。"""
    code, lines = result
    if code == 0:
        return lines or [ready]
    retry = f"（手动重试：{command}）" if command else ""
    return [*lines, "网络没完全配好；模拟照常启动，可用 `python -m simbench net show` 看现状"
                    + retry]


def _command_ips(addresses: list[str]) -> str:
    joined = " ".join(f"--ip {address}" for address in addresses)
    return f"应用：python -m simbench net up {joined}"


def _command_with_hosts(point_table: Path, extra: list[str]) -> str:
    joined = " ".join(f"--ip {address}" for address in extra)
    return f"应用：python -m simbench net up --point-table {point_table}" + (
        f" {joined}" if joined else "")


def _command(point_table: Path) -> str:
    return f"应用：python -m simbench net up --point-table {point_table}"
