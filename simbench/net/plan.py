"""地址差异计算。**纯函数**：不读系统、不写系统，输入输出都是数据。

这是"要不要动系统"的唯一判据，所以它必须能脱离真实网卡单测——现场误删别人地址
这种事不该由"当时网卡上正好有什么"来决定。

三条口径：

1. **只加不猜**：工程要的地址，网卡上缺就加；已存在就什么都不做（幂等）。同一个 IP
   但掩码不同不静默改，单独报出来让人定（改掩码会把同网段的路由行为一起改掉）。
2. **只删自己的**：能删的只有"账本记过 + 现在真的在网卡上 + 工程已经不要了"三项同时
   成立的地址。网卡上原有地址、系统自动地址、别人手工加的地址，数量可以报告，绝不碰。
3. **不是 Manual 的地址不算数**：`169.254.x` 这类自动地址、DHCP 租约，即使 IP 撞上
   工程要的地址也不算"已经配好"，但也不许删——那是系统的东西。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .state import Address


@dataclass(frozen=True)
class NetPlan:
    adapter: str
    to_add: tuple[str, ...] = ()
    to_remove: tuple[str, ...] = ()
    prefix_conflicts: tuple[str, ...] = ()
    foreign_manual: tuple[str, ...] = ()
    system_managed: int = 0
    ledger_stale: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        """什么都不用做：既不需要加，也没有我们该收回去的地址。"""
        return not (self.to_add or self.to_remove or self.prefix_conflicts)

    def summary(self) -> list[str]:
        lines = []
        if self.to_add:
            lines.append(f"待添加 {len(self.to_add)} 个：" + "、".join(self.to_add))
        if self.to_remove:
            lines.append(f"待收回 {len(self.to_remove)} 个（本工程加的）："
                         + "、".join(self.to_remove))
        if self.prefix_conflicts:
            lines.append(f"掩码不一致 {len(self.prefix_conflicts)} 个（不自动改）："
                         + "、".join(self.prefix_conflicts))
        if self.foreign_manual:
            lines.append(f"网卡上另有 {len(self.foreign_manual)} 个手工地址不属于本工程（不动）")
        if self.system_managed:
            lines.append(f"系统自动/DHCP 地址 {self.system_managed} 个（不动）")
        if self.ledger_stale:
            lines.append(f"账本里 {len(self.ledger_stale)} 条地址已不在网卡上，"
                         "下次应用时一并销账")
        lines.extend(self.notes)
        if not lines:
            lines.append("已一致，无需改动")
        return lines


def plan_addresses(adapter: str,
                   desired: list[tuple[str, int]],
                   present: list[Address],
                   owned: set[str]) -> NetPlan:
    """算差异。`desired` 是 (ip, prefix) 列表，`owned` 是本工程记过账的 IP。"""
    want = dict(desired)
    manual = [a for a in present if a.manual]
    manual_by_ip = {a.ip: a for a in manual}

    to_add = tuple(ip for ip in sorted(want) if ip not in manual_by_ip)

    conflicts = tuple(
        f"{ip}/{want[ip]}（网卡上是 {manual_by_ip[ip].prefix_length}）"
        for ip in sorted(want)
        if ip in manual_by_ip and manual_by_ip[ip].prefix_length != want[ip]
    )

    to_remove = tuple(sorted(ip for ip in owned if ip in manual_by_ip and ip not in want))

    foreign = tuple(sorted(a.ip for a in manual if a.ip not in want and a.ip not in owned))
    ledger_stale = tuple(sorted(ip for ip in owned if ip not in manual_by_ip))

    return NetPlan(
        adapter=adapter,
        to_add=to_add,
        to_remove=to_remove,
        prefix_conflicts=conflicts,
        foreign_manual=foreign,
        system_managed=len(present) - len(manual),
        ledger_stale=ledger_stale,
    )


def foreign_owners(desired: list[tuple[str, int]],
                   others: dict[str, str]) -> dict[str, str]:
    """想要的地址里，哪些已经被**别的**网卡占着。

    同一个 IP 不能同时挂在两块网卡上：不先查出来，直接加只会得到一句英文的
    `The object already exists.`，现场完全看不出是撞了哪块网卡（实测踩过）。
    抽成纯函数是因为这条判据决定"要不要动系统"，必须能脱离真实机器单测。

    `others` 是 {ip: 网卡名}，调用方负责排除目标网卡自己。
    """
    return {ip: others[ip] for ip, _ in desired if ip in others}
