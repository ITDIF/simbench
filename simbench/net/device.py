r"""专用网卡：只认 simbench 自己建的那一块，永不借用机器上已有的。

**为什么一律新建**：现场机器上可能已经有别人装的环回适配器（本机就有一块"以太网 5"，
配着 79 个地址）。借用它等于和别人的配置共用一个可变状态——对方改一个字、我们删一个地址，
两边互相踩。自己建一块专属的，冲突面直接归零，卸载也干净（`pnputil /remove-device`）。

认定一块网卡"是我们的"要两条证据之一：

1. 账本里记着它的适配器 GUID（我们建的，记过账）；
2. 名字正好是 `simbench 模拟网卡`（账本丢了也能认回来）。

**不按"是不是环回适配器"认**：机器上别的环回网卡（比如手工装的）描述一模一样，
按描述认会把它们当成自己的。

本机的可用性事实（实测核对，非推测）：

- Windows 自带的 `%WINDIR%\INF\netloop.inf` 把硬件 ID `*MSLOOP` 映射到 Net 类设备，
  驱动是系统里的 `loop.sys`。创建一块 KM-TEST 环回适配器**不需要第三方驱动、不需要重启**。
- Hyper-V 内部交换机（`New-VMSwitch`）在 Windows 11 **家庭版**上不存在，不能当兜底。
- 设备实例形如 `ROOT\NET\0000`（`\N` 是普通字符，所以本文件用 raw 字符串），
  `pnputil /remove-device <实例ID>` 能干净移除。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .state import Adapter, NetError, as_list, run_powershell

# 固定名字：现场一眼能认出这是工具建的、可以删。也用它作为"账本丢了"时的认领依据。
MANAGED_ADAPTER_NAME = "simbench 模拟网卡"

# 只用于"提醒机器上还有别的环回网卡"，不作为选用依据。
LOOPBACK_MARKERS = ("km-test", "loopback", "环回")

_PNP_FIELDS = ("InstanceId", "FriendlyName", "Status")


@dataclass(frozen=True)
class DeviceInstance:
    instance_id: str
    friendly_name: str
    status: str

    @property
    def is_root_enumerated(self) -> bool:
        r"""root 枚举出来的环回适配器。

        两种命名空间都要认：手工向导装出来的是 `ROOT\NET\NNNN`，simbench 自己建的
        （`SetupDiCreateDeviceInfo` 把设备名当实例路径）是 `ROOT\*MSLOOP\NNNN`。
        只认前者会让"我明明建出来了却报失败"——这个坑已经踩过一次。
        """
        upper = self.instance_id.upper()
        return upper.startswith("ROOT\\") and ("MSLOOP" in upper or upper.startswith("ROOT\\NET"))


def is_loopback_adapter(adapter: Adapter) -> bool:
    """按描述判定。名字（"以太网 5"）是用户在系统里改的，不能当判据。"""
    text = f"{adapter.description} {adapter.name}".lower()
    return any(marker in text for marker in LOOPBACK_MARKERS)


def is_managed_name(adapter: Adapter) -> bool:
    return adapter.name.strip() == MANAGED_ADAPTER_NAME


def loopback_inf_path() -> Path:
    """系统自带的环回适配器 INF。没有它就造不出网卡，这是硬前置。"""
    root = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    return Path(root) / "INF" / "netloop.inf"


def inf_available() -> bool:
    return loopback_inf_path().is_file()


def parse_devices(text: str) -> list[DeviceInstance]:
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise NetError(f"设备清单不是合法 JSON：{exc}") from exc
    devices = []
    for item in as_list(payload):
        try:
            devices.append(DeviceInstance(
                instance_id=str(item.get("InstanceId") or ""),
                friendly_name=str(item.get("FriendlyName") or ""),
                status=str(item.get("Status") or ""),
            ))
        except (TypeError, ValueError) as exc:
            raise NetError(f"设备记录字段缺失：{item!r}") from exc
    return devices


def read_root_net_devices() -> list[DeviceInstance]:
    """所有 ROOT\\NET 设备实例。只读，不需要管理员。"""
    script = ("Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | "
              f"Select-Object {','.join(_PNP_FIELDS)} | ConvertTo-Json -Compress")
    return [d for d in parse_devices(run_powershell(script)) if d.is_root_enumerated]


def find_managed_adapter(adapters: list[Adapter], owned_guids: set[str] | None = None
                         ) -> Adapter | None:
    """找 simbench 自己建的那块网卡。账本里记过的 GUID 优先，其次认名字。

    找不到就回 None —— **不会退而求其次去挑别的环回网卡**。多块同名/同账本时按
    ifIndex 取第一块，结果稳定可复现。
    """
    owned = {g.lower().strip("{}") for g in (owned_guids or set())}
    hits = [a for a in adapters
            if a.guid.lower().strip("{}") in owned or is_managed_name(a)]
    hits.sort(key=lambda a: a.if_index)
    return hits[0] if hits else None


def foreign_loopbacks(adapters: list[Adapter], managed: Adapter | None) -> list[Adapter]:
    """机器上其它环回网卡。只用来提醒"它们存在但不会被用"，绝不入选。"""
    return [a for a in adapters
            if is_loopback_adapter(a) and (managed is None or a.if_index != managed.if_index)]


def prepare_report(adapters: list[Adapter], elevated: bool, inf_ok: bool | None = None,
                   owned_guids: set[str] | None = None) -> list[str]:
    """给人和 GUI 看的结论：这台机器上准备专用网卡要走哪条路。"""
    inf_ok = inf_available() if inf_ok is None else inf_ok
    managed = find_managed_adapter(adapters, owned_guids)

    if managed is not None:
        lines = [f"已就绪：{managed.name}（ifIndex={managed.if_index}）"]
    else:
        lines = [f"专用网卡 {MANAGED_ADAPTER_NAME} 还没建"]
        if inf_ok and elevated:
            lines.append("可以直接创建（系统自带 netloop.inf 可用，当前已有管理员权限；"
                         "不重启、不装第三方驱动）")
        elif inf_ok:
            lines.append("创建时需要一次 UAC 确认（系统自带 netloop.inf 可用）")
        else:
            lines.append(f"不能自动创建：找不到系统自带的 {loopback_inf_path()}；"
                         "改用手动向导（控制面板 → 添加硬件 → 网络适配器 → "
                         "Microsoft → Microsoft KM-TEST 环回适配器），"
                         f"建好后把名字改成 {MANAGED_ADAPTER_NAME}")

    others = foreign_loopbacks(adapters, managed)
    if others:
        names = "、".join(f"{a.name}(ifIndex={a.if_index})" for a in others[:3])
        lines.append(f"机器上另有 {len(others)} 块不是 simbench 建的环回网卡（{names}），"
                     "不会被使用、也不会被改动")
    return lines
