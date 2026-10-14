"""网络声明文件：解析、落盘、去重。

IP 是环境相关的，跟点表分开存，所以这份文件自己就是一份"真相"，解析必须严格：
缺字段、重名、同 IP 两种掩码都要在启动前报出来，而不是等到加地址时才炸。
"""

from __future__ import annotations

import json

import pytest

from simbench.net.spec import (
    DEFAULT_PORT,
    DeviceNet,
    NetworkSpec,
    companion_path,
    load,
    load_companion,
    loads,
    save,
    unique_addresses,
)
from simbench.net.state import NetError


def test_parses_a_minimal_declaration():
    spec = loads(json.dumps({"adapter_name": "simbench 模拟网卡",
                             "devices": [{"name": "风机-1", "ip": "10.136.107.3"}]}))
    assert spec.adapter_name == "simbench 模拟网卡"
    assert spec.devices[0].ip == "10.136.107.3"
    assert spec.devices[0].port == DEFAULT_PORT
    assert spec.addresses() == [("10.136.107.3", 24)]      # 默认 /24，与 apply 口径一致


def test_missing_name_falls_back_to_ip():
    spec = loads(json.dumps({"devices": [{"ip": "10.0.0.1"}]}))
    assert spec.devices[0].name == "10.0.0.1"


def test_no_devices_is_empty_not_an_error():
    assert loads("{}").devices == []
    assert loads("").devices == []


@pytest.mark.parametrize("text,expected", [
    ("{不是 json", "不是合法 JSON"),
    ("[]", "顶层应该是一个对象"),
    ('{"devices": {}}', "devices 应该是数组"),
    ('{"devices": ["x"]}', "不是对象"),
    ('{"devices": [{"name": "a"}]}', "没有 ip"),
    ('{"devices": [{"ip": "10.0.0.1", "port": "abc"}]}', "不是整数"),
])
def test_bad_declarations_are_human_errors(text, expected):
    with pytest.raises(NetError, match=expected):
        loads(text)


def test_duplicate_ip_is_rejected_before_touching_the_system():
    spec = NetworkSpec("网卡", [DeviceNet("a", "10.0.0.1"), DeviceNet("b", "10.0.0.1")])
    with pytest.raises(NetError, match="被声明了两次"):
        unique_addresses(spec)


def test_same_ip_two_prefixes_is_rejected():
    spec = NetworkSpec("网卡", [DeviceNet("a", "10.0.0.1", prefix=24),
                                DeviceNet("b", "10.0.0.1", prefix=25)])
    with pytest.raises(NetError, match="两种掩码"):
        unique_addresses(spec)


def test_owner_lookup_is_used_in_error_messages():
    spec = NetworkSpec("网卡", [DeviceNet("风机-1", "10.0.0.1")])
    assert spec.owner_of("10.0.0.1") == "风机-1"
    assert spec.owner_of("10.0.0.9") == ""


def test_non_default_port_and_prefix_round_trip(tmp_path):
    spec = NetworkSpec("网卡", [DeviceNet("a", "10.0.0.1", port=5020, prefix=16)])
    path = save(spec, tmp_path / "x.net.json")
    again = load(path)
    assert again.devices[0].port == 5020 and again.devices[0].prefix == 16


def test_companion_path_sits_next_to_the_point_table():
    assert companion_path("examples/covi-tunnel.csv").name == "covi-tunnel.net.json"
    assert companion_path("p/project.json").name == "project.net.json"


def test_missing_companion_is_not_an_error(tmp_path):
    assert load_companion(tmp_path / "nope.csv") is None


def test_loading_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(NetError, match="读不到网络声明"):
        load(tmp_path / "nope.net.json")
