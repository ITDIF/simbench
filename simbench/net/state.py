"""读系统网络现状。**只读**，不需要管理员。

PowerShell 只在本文件里调用；输出强制 UTF-8，中文适配器名（"以太网 5"）原样带回。
解析与执行分开成两组函数：执行那一层薄到可以直接肉眼核对，判定逻辑全在纯函数里，
测试用固定 JSON 打，不必依赖本机网卡长什么样。

`PrefixOrigin` 的数值口径来自 MSFT_NetIPAddress：1=Manual、2=WellKnown、3=Dhcp。
**非 Manual 一律视为"不是我们的"**（169.254.x 那种自动地址、DHCP 租约都不许碰），
这条口径是防"清理时误删别人地址"的第一道闸。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from dataclasses import dataclass

from .errors import NetError

MANUAL, WELL_KNOWN, DHCP = 1, 2, 3

_PS = ("powershell", "-NoProfile", "-NonInteractive", "-Command")
_ENCODING_PRELUDE = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "

_ADAPTER_FIELDS = ("Name", "InterfaceDescription", "ifIndex",
                   "InterfaceGuid", "Status", "MacAddress", "PnPDeviceID")
_ADDRESS_FIELDS = ("InterfaceIndex", "IPAddress", "PrefixLength", "PrefixOrigin")


@dataclass(frozen=True)
class Adapter:
    name: str
    description: str
    if_index: int
    guid: str
    status: str
    mac: str
    pnp_device_id: str = ""     # 形如 ROOT\NET\0001；认"这块是不是我们建的"要靠它

    @property
    def label(self) -> str:
        return f"{self.name} ({self.description})"

    @property
    def up(self) -> bool:
        return self.status.lower() == "up"


@dataclass(frozen=True)
class Address:
    ip: str
    prefix_length: int
    if_index: int
    origin: int

    @property
    def manual(self) -> bool:
        """手工配的地址才可能是我们加的；自动/租约地址不许碰。"""
        return self.origin == MANUAL

    @property
    def cidr(self) -> str:
        return f"{self.ip}/{self.prefix_length}"


def is_elevated() -> bool:
    """当前进程有没有管理员。非 Windows 一律回 False（这一层本来就是 Windows 专用）。"""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                      # noqa: BLE001
        # 取不到就按「不是管理员」算：这里只影响要不要提权这一件事
        return False


def no_window_kwargs() -> dict:
    """起控制台子进程时统一用的 kwargs：**窗口版 GUI 里不许闪一个 cmd**。

    `simbench-gui.exe` 是 `console=False` 构建，而 `powershell` / `pnputil` 是**控制台程序**：
    从窗口版进程里直接 `subprocess.run` 会给它们各开一个控制台窗口（2026-10-19 现场：点
    「启动」闪一个 cmd、还卡几秒）。`CREATE_NO_WINDOW` 让子进程没有窗口，stdout/stderr 照旧
    走管道；POSIX 上没有这个标志，回空字典（本包本来就只服务 Windows，留着是为了别的用例能跑）。
    """
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}


def run_powershell(script: str, timeout: float = 60.0) -> str:
    """跑一段 PowerShell 并回 stdout。失败时抛 NetError，stderr 原文附在后面。"""
    try:
        proc = subprocess.run([*_PS, _ENCODING_PRELUDE + script],
                              capture_output=True, timeout=timeout,
                              **no_window_kwargs())
    except FileNotFoundError as exc:
        raise NetError("找不到 powershell；这一层需要 Windows PowerShell") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetError(f"读取网卡状态超时（{timeout:.0f}s）") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NetError(detail[-1] if detail else f"powershell 退出码 {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def as_list(payload) -> list:
    """ConvertTo-Json 在只有一条记录时不吐数组，只吐一个对象；这里统一成列表。"""
    if payload is None:
        return []
    return payload if isinstance(payload, list) else [payload]


def parse_adapters(text: str) -> list[Adapter]:
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise NetError(f"网卡清单不是合法 JSON：{exc}") from exc
    adapters = []
    for item in as_list(payload):
        try:
            adapters.append(Adapter(
                name=str(item.get("Name") or ""),
                description=str(item.get("InterfaceDescription") or ""),
                if_index=int(item.get("ifIndex")),
                guid=str(item.get("InterfaceGuid") or ""),
                status=str(item.get("Status") or ""),
                mac=str(item.get("MacAddress") or ""),
                pnp_device_id=str(item.get("PnPDeviceID") or ""),
            ))
        except (TypeError, ValueError) as exc:
            raise NetError(f"网卡记录字段缺失：{item!r}") from exc
    return adapters


def parse_addresses(text: str) -> list[Address]:
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise NetError(f"地址清单不是合法 JSON：{exc}") from exc
    addresses = []
    for item in as_list(payload):
        try:
            addresses.append(Address(
                ip=str(item.get("IPAddress") or ""),
                prefix_length=int(item.get("PrefixLength")),
                if_index=int(item.get("InterfaceIndex")),
                origin=int(item.get("PrefixOrigin")),
            ))
        except (TypeError, ValueError) as exc:
            raise NetError(f"地址记录字段缺失：{item!r}") from exc
    return addresses


def read_adapters() -> list[Adapter]:
    script = ("Get-NetAdapter -IncludeHidden | "
              f"Select-Object {','.join(_ADAPTER_FIELDS)} | ConvertTo-Json -Compress")
    return parse_adapters(run_powershell(script))


def read_addresses(if_index: int) -> list[Address]:
    script = (f"Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex {int(if_index)} | "
              f"Select-Object {','.join(_ADDRESS_FIELDS)} | ConvertTo-Json -Compress")
    return parse_addresses(run_powershell(script))


def read_all_addresses() -> list[Address]:
    """一次读全机 IPv4 地址（不分网卡）。

    加地址前必须知道"这个 IP 是不是已经在**别的**网卡上了"：Windows 不允许同一个 IP
    同时存在于两块网卡上，直接加会得到一句 `The object already exists.`——现场看到这句
    英文完全不知道是撞了哪块。所以先把占用关系查出来，报成一句人话。
    """
    script = ("Get-NetIPAddress -AddressFamily IPv4 | "
              f"Select-Object {','.join(_ADDRESS_FIELDS)} | ConvertTo-Json -Compress")
    return parse_addresses(run_powershell(script))


def resolve_adapter(adapters: list[Adapter], key: str) -> Adapter:
    """按 GUID / ifIndex / 别名三种写法认一块网卡；认不出或撞车时把人话错误抛出去。

    GUID 是首选标识（别名会随系统语言变），但现场手敲时按序号或别名更方便，
    所以三种都收：**不唯一就报错，绝不猜**——猜错等于往别的网卡上写地址。
    """
    needle = (key or "").strip()
    if not needle:
        raise NetError("没有指定网卡")
    lowered = needle.lower()
    bare = lowered.strip("{}")
    hits = [a for a in adapters
            if a.guid.lower().strip("{}") == bare
            or a.name.lower() == lowered
            or a.description.lower() == lowered
            or (needle.isdigit() and a.if_index == int(needle))]
    if not hits:
        known = "、".join(sorted(a.label for a in adapters)) or "（一个都没读到）"
        raise NetError(f"找不到网卡 {key!r}；当前可用：{known}")
    if len(hits) > 1:
        raise NetError(f"网卡 {key!r} 不唯一，命中 {len(hits)} 块；请用 GUID 指定")
    return hits[0]
