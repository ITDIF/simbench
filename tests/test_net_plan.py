"""地址差异计算的口径。全部是纯函数，不碰真实网卡。

这里守的是"现场不许误删"那三条：只加不猜、只删自己的、非 Manual 不算数。
每一条都配一个"反过来会出事"的用例——判定一旦改松，红的是这里，不是现场。
"""

from __future__ import annotations

from simbench.net.plan import foreign_owners, plan_addresses
from simbench.net.state import DHCP, MANUAL, WELL_KNOWN, Address


def _addr(ip: str, prefix: int = 24, origin: int = MANUAL) -> Address:
    return Address(ip=ip, prefix_length=prefix, if_index=15, origin=origin)


def test_missing_addresses_are_the_onos_to_add():
    result = plan_addresses("以太网 5", [("10.0.0.1", 24), ("10.0.0.2", 24)],
                            [], set())
    assert result.to_add == ("10.0.0.1", "10.0.0.2")
    assert not result.clean


def test_everything_present_and_matching_is_clean():
    result = plan_addresses("以太网 5", [("10.0.0.1", 24)],
                            [_addr("10.0.0.1")], set())
    assert result.clean and result.to_add == () and result.to_remove == ()
    assert result.summary() == ["已一致，无需改动"]


def test_prefix_mismatch_is_reported_not_silently_rewritten():
    """掩码不同不静默改：改掩码会把同网段的路由行为一起改掉，得人定。"""
    result = plan_addresses("以太网 5", [("10.0.0.1", 25)],
                            [_addr("10.0.0.1", prefix=24)], set())
    assert result.to_add == ()
    assert result.prefix_conflicts == ("10.0.0.1/25（网卡上是 24）",)
    assert not result.clean


def test_only_ledger_owned_addresses_are_removed():
    """工程不要了、账本记过、且确实在网卡上——三条同时成立才回收。"""
    result = plan_addresses("以太网 5", [("10.0.0.1", 24)],
                            [_addr("10.0.0.1"), _addr("10.0.0.9")], {"10.0.0.9"})
    assert result.to_remove == ("10.0.0.9",)


def test_addresses_we_never_added_are_never_removed():
    """网卡上别人手工配的地址，只报数，绝不进 to_remove。"""
    result = plan_addresses("以太网 5", [], [_addr("10.0.0.9")], set())
    assert result.to_remove == ()
    assert result.foreign_manual == ("10.0.0.9",)


def test_corrupt_ledger_direction_is_safe():
    """账本不可信时 owned 为空 —— 结果是一个都不删，而不是猜着删。"""
    result = plan_addresses("以太网 5", [], [_addr("10.0.0.9")], set())
    assert result.to_remove == ()


def test_automatic_addresses_do_not_count_as_configured():
    """169.254.x 这类自动地址撞上工程要的地址也不算配好，但它也不许被删。"""
    result = plan_addresses("以太网 5", [("169.254.1.1", 16)],
                            [_addr("169.254.1.1", prefix=16, origin=WELL_KNOWN)], set())
    assert result.to_add == ("169.254.1.1",)
    assert result.to_remove == ()
    assert result.system_managed == 1


def test_dhcp_lease_is_system_managed_too():
    result = plan_addresses("以太网 5", [], [_addr("10.1.2.3", origin=DHCP)], set())
    assert result.system_managed == 1
    assert result.foreign_manual == ()


def test_ledger_address_already_gone_is_reported_for_cleanup():
    result = plan_addresses("以太网 5", [], [], {"10.0.0.9"})
    assert result.to_remove == ()
    assert result.ledger_stale == ("10.0.0.9",)


def test_addresses_held_by_another_adapter_are_reported_with_its_name():
    """同一个 IP 不能挂在两块网卡上：先报出来，别让 Windows 甩一句英文。"""
    owners = {"10.136.107.3": "以太网 5", "10.136.107.9": "以太网 5"}
    got = foreign_owners([("10.136.107.3", 24), ("10.136.107.240", 24)], owners)
    assert got == {"10.136.107.3": "以太网 5"}


def test_nothing_foreign_when_no_overlap():
    assert foreign_owners([("10.0.0.1", 24)], {"10.0.0.2": "别的"}) == {}


def test_empty_desired_list_is_not_an_error():
    assert foreign_owners([], {"10.0.0.2": "别的"}) == {}
