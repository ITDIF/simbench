"""专用网卡的判定口径。

两条现场决定钉在这里：

1. **永不借用机器上已有的网卡**——机器上那块手工装的环回适配器（本机是"以太网 5"）
   描述和我们建的完全一样，按描述认会把别人的配置当成自己的，两边互相踩。所以认领
   只认两条证据：账本里记过的 GUID，或者名字正好是 `simbench 模拟网卡`。
2. **固定名字**——它同时是"账本丢了也能认回来"的兜底依据，所以判定必须是精确匹配。

判定输入全是数据，测试不打桩系统。
"""

from __future__ import annotations

import json

from simbench.net.device import (MANAGED_ADAPTER_NAME, find_managed_adapter,
                                 foreign_loopbacks, is_loopback_adapter,
                                 parse_devices, prepare_report)
from simbench.net.state import Adapter

OURS = Adapter(name=MANAGED_ADAPTER_NAME, description="Microsoft KM-TEST 环回适配器",
               if_index=20, guid="{AAAA1111-0000-0000-0000-000000000001}",
               status="Up", mac="02-00-4C-4F-4F-60", pnp_device_id=r"ROOT\NET\0001")
FOREIGN = Adapter(name="以太网 5", description="Microsoft KM-TEST 环回适配器",
                  if_index=15, guid="{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}",
                  status="Up", mac="02-00-4C-4F-4F-50", pnp_device_id=r"ROOT\NET\0000")
WLAN = Adapter(name="WLAN", description="Realtek RTL8852BE WiFi 6 802.11ax PCIe Adapter",
               if_index=4, guid="{11111111}", status="Up", mac="9C-2F-9D-9E-35-F1")


def test_loopback_is_recognised_by_description_in_both_languages():
    assert is_loopback_adapter(FOREIGN)          # 中文描述
    english = Adapter(name="Ethernet 6", description="Microsoft KM-TEST Loopback Adapter",
                      if_index=18, guid="{BBBB}", status="Up", mac="")
    assert is_loopback_adapter(english)
    assert not is_loopback_adapter(WLAN)


def test_existing_foreign_loopback_is_never_adopted():
    """这是那两条决定的合体：机器上有一块一模一样的环回适配器，也不许认成自己的。"""
    assert find_managed_adapter([FOREIGN, WLAN]) is None
    assert find_managed_adapter([FOREIGN, WLAN], owned_guids=set()) is None


def test_ours_is_recognised_by_exact_name():
    assert find_managed_adapter([FOREIGN, OURS, WLAN]).if_index == 20


def test_ours_is_recognised_by_ledger_guid_even_if_renamed():
    """账本是第一证据：用户把名字改掉了也认得出。"""
    renamed = Adapter(name="我的模拟网卡", description="Microsoft KM-TEST 环回适配器",
                      if_index=20, guid=OURS.guid, status="Up", mac="")
    assert find_managed_adapter([renamed], owned_guids={OURS.guid}).if_index == 20
    assert find_managed_adapter([renamed]) is None      # 名字对不上又没账本就认不出


def test_name_match_is_exact_not_prefix():
    """名字是认领依据，模糊匹配会把"simbench 模拟网卡 2"这种也算进来。"""
    lookalike = Adapter(name=MANAGED_ADAPTER_NAME + " 2",
                        description="Microsoft KM-TEST 环回适配器",
                        if_index=21, guid="{CCCC}", status="Up", mac="")
    assert find_managed_adapter([lookalike]) is None


def test_foreign_loopbacks_are_reported_but_excluded():
    assert [a.if_index for a in foreign_loopbacks([FOREIGN, OURS, WLAN], OURS)] == [15]
    assert foreign_loopbacks([FOREIGN, WLAN], None) == [FOREIGN]


def test_parse_devices_keeps_only_root_net():
    devices = parse_devices(json.dumps([
        {"InstanceId": r"ROOT\NET\0000", "FriendlyName": "环回适配器", "Status": "OK"},
        {"InstanceId": r"PCI\VEN_10EC&DEV_8168\1&2", "FriendlyName": "WLAN", "Status": "OK"},
    ]))
    assert [d.instance_id for d in devices if d.is_root_enumerated] == [r"ROOT\NET\0000"]


def test_both_root_namespaces_are_recognised():
    r"""手工向导装出来的是 ROOT\NET\NNNN，simbench 自己建的是 ROOT\*MSLOOP\NNNN。

    只认前者会让"明明建出来了却报失败"——实测踩过：每次创建都成功，但检测在
    ROOT\NET 里找，于是连报四次失败、在地上留了四块网卡。
    """
    devices = parse_devices(json.dumps([
        {"InstanceId": r"ROOT\NET\0000", "FriendlyName": "手工装的", "Status": "OK"},
        {"InstanceId": r"ROOT\*MSLOOP\0000", "FriendlyName": "simbench 建的", "Status": "OK"},
        {"InstanceId": r"ROOT\VMWARE\0000", "FriendlyName": "VMware", "Status": "OK"},
    ]))
    found = [d.instance_id for d in devices if d.is_root_enumerated]
    assert found == [r"ROOT\NET\0000", r"ROOT\*MSLOOP\0000"]


def test_report_when_our_adapter_is_ready():
    lines = prepare_report([OURS, FOREIGN, WLAN], elevated=False, inf_ok=True)
    assert lines[0].startswith("已就绪：" + MANAGED_ADAPTER_NAME)
    assert "不会被使用" in "、".join(lines)          # 顺带说明别人的那块不动


def test_report_without_our_adapter_offers_creation():
    without = "、".join(prepare_report([FOREIGN, WLAN], elevated=False, inf_ok=True))
    assert "还没建" in without and "UAC" in without
    assert "已就绪" not in without

    with_admin = "、".join(prepare_report([FOREIGN, WLAN], elevated=True, inf_ok=True))
    assert "可以直接创建" in with_admin and "UAC" not in with_admin


def test_report_without_inf_points_at_the_manual_wizard():
    body = "、".join(prepare_report([WLAN], elevated=True, inf_ok=False))
    assert "不能自动创建" in body
    assert "添加硬件" in body and "KM-TEST" in body
    assert MANAGED_ADAPTER_NAME in body             # 手动建完也要按这个名字
    assert "可以直接创建" not in body
