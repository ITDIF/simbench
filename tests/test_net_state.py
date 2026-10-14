"""读系统状态那条路的解析与认网卡口径。

执行（跑 PowerShell）那一层不在这里测——它薄到能肉眼核对；这里测的是它吐回来的东西
怎么被解读：单条 JSON 要能认、`PrefixOrigin` 数值要映射对、认网卡认不唯一时必须报错。
按 GUID 之外的写法（别名、序号）是给现场手敲用的，写错不许猜。
"""

from __future__ import annotations

import json

import pytest

from simbench.net.cli import _parse_ip_spec
from simbench.net.ledger import Ledger
from simbench.net.state import (
    DHCP,
    MANUAL,
    NetError,
    parse_adapters,
    parse_addresses,
    resolve_adapter,
)

_ADAPTERS = [
    {"Name": "以太网 5", "InterfaceDescription": "Microsoft KM-TEST 环回适配器",
     "ifIndex": 15, "InterfaceGuid": "{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}",
     "Status": "Up", "MacAddress": "02-00-4C-4F-4F-50"},
    {"Name": "WLAN", "InterfaceDescription": "Realtek RTL8852BE WiFi 6",
     "ifIndex": 4, "InterfaceGuid": "{11111111-2222-3333-4444-555555555555}",
     "Status": "Up", "MacAddress": "9C-2F-9D-9E-35-F1"},
]


def test_single_adapter_object_is_normalised_to_a_list():
    """ConvertTo-Json 在只有一条记录时不吐数组，只吐一个对象。"""
    adapters = parse_adapters(json.dumps(_ADAPTERS[0]))
    assert len(adapters) == 1
    assert adapters[0].label == "以太网 5 (Microsoft KM-TEST 环回适配器)"
    assert adapters[0].up


def test_empty_output_parses_to_nothing():
    assert parse_adapters("") == []
    assert parse_addresses("") == []


def test_prefix_origin_numbers_map_to_manual_and_system():
    addresses = parse_addresses(json.dumps([
        {"InterfaceIndex": 15, "IPAddress": "10.136.107.3",
         "PrefixLength": 24, "PrefixOrigin": MANUAL},
        {"InterfaceIndex": 15, "IPAddress": "169.254.1.1",
         "PrefixLength": 16, "PrefixOrigin": 2},
        {"InterfaceIndex": 15, "IPAddress": "10.1.2.3",
         "PrefixLength": 24, "PrefixOrigin": DHCP},
    ]))
    manual = [a for a in addresses if a.manual]
    assert [a.ip for a in manual] == ["10.136.107.3"]
    assert manual[0].cidr == "10.136.107.3/24"
    assert len(addresses) - len(manual) == 2


def test_broken_json_and_missing_fields_are_human_errors():
    with pytest.raises(NetError, match="不是合法 JSON"):
        parse_adapters("{not json")
    with pytest.raises(NetError, match="字段缺失"):
        parse_addresses(json.dumps([{"InterfaceIndex": 15}]))


@pytest.mark.parametrize("key", [
    "{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}",   # 带花括号
    "6fe2fee9-e26b-43f3-a977-d4fcff8200af",     # 不带花括号、小写
    "15",                                        # ifIndex
    "以太网 5",                                  # 别名
    "Microsoft KM-TEST 环回适配器",              # 描述
])
def test_adapter_can_be_named_five_ways(key):
    adapters = parse_adapters(json.dumps(_ADAPTERS))
    assert resolve_adapter(adapters, key).if_index == 15


def test_unknown_adapter_lists_what_is_available():
    adapters = parse_adapters(json.dumps(_ADAPTERS))
    with pytest.raises(NetError, match="找不到网卡"):
        resolve_adapter(adapters, "以太网 9")


def test_ambiguous_adapter_refuses_instead_of_guessing():
    """两块网卡同名时必须报错：猜错等于往别的网卡上写地址。"""
    dup = [_ADAPTERS[0], {**_ADAPTERS[1], "Name": "以太网 5"}]
    adapters = parse_adapters(json.dumps(dup))
    with pytest.raises(NetError, match="不唯一"):
        resolve_adapter(adapters, "以太网 5")


@pytest.mark.parametrize("spec,expected", [
    ("10.136.107.3", ("10.136.107.3", 24)),        # 不给掩码按 /24
    ("10.136.107.3/32", ("10.136.107.3", 32)),
    (" 10.136.107.3/24 ", ("10.136.107.3", 24)),   # 两侧空白要吃掉
])
def test_ip_spec_parsing(spec, expected):
    assert _parse_ip_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "10.136.107.300", "10.0.0.0/33", "::1"])
def test_bad_ip_specs_are_rejected(spec):
    with pytest.raises(NetError):
        _parse_ip_spec(spec)


def test_ledger_roundtrip_records_only_our_addresses(tmp_path):
    path = tmp_path / "net-ledger.json"
    ledger = Ledger()
    ledger.record("{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}",
                  [("10.136.107.3", 24), ("10.136.107.5", 24)], "2026-10-12T14:00:00")
    ledger.save(path)

    reloaded = Ledger.load(path)
    assert reloaded.corrupt == ""
    assert reloaded.owned("{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}") == {
        "10.136.107.3", "10.136.107.5"}
    assert reloaded.owned("{11111111-2222-3333-4444-555555555555}") == set()

    reloaded.drop("{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}", ["10.136.107.3"])
    assert reloaded.owned("{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}") == {"10.136.107.5"}


def test_missing_ledger_means_nothing_owned(tmp_path):
    assert Ledger.load(tmp_path / "nope.json").owned("{X}") == set()


def test_corrupt_ledger_owns_nothing_and_says_so(tmp_path):
    path = tmp_path / "net-ledger.json"
    path.write_text("{半截文件", encoding="utf-8")
    ledger = Ledger.load(path)
    assert ledger.owned("{6FE2FEE9-E26B-43F3-A977-D4FCFF8200AF}") == set()
    assert "账本读不出来" in ledger.corrupt
