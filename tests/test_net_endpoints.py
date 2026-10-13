"""声明里的地址 -> 这一路通道额外听哪些 (ip, port)。

三条守在这里的口径：

1. **名字对得上才算这一路的**。按顺序配对会在点表增删一台设备之后把 IP 整体错位一格，
   而那种错配是静默的——现场只看到"设备读数全不对"；
2. **通道自己听着的不重复挂**。通道 host 是通配时，同端口的声明地址已经被它盖住，
   再挂一次启动就会 10048 撞回来；
3. **声明坏了不拦从站**。监听层照常按通道自家的地址起，报错是网络那一步的活。

判定全是纯函数 + 纯数据：本文件一条系统调用、一条网络连接都不做。
"""

from __future__ import annotations

import json

from simbench.net import endpoints, startup
from simbench.project.model import Device, Point, Project, SerialChannel, TcpChannel

WILDCARD_HOST = "0.0.0.0"


def _table(tmp_path, devices=("风机-1", "照明回路-1"), channel="tcp-5020",
           host=WILDCARD_HOST, port=5020) -> TcpChannel:
    """点表 + 它旁边的声明；返回这一路通道。"""
    table = tmp_path / f"{channel}.csv"
    table.write_text("point table, 内容与监听无关\n", encoding="utf-8")
    (tmp_path / f"{channel}.net.json").write_text(json.dumps({
        "adapter_name": "simbench 模拟网卡",
        "devices": [{"name": name, "ip": f"10.136.107.{10 + i}"}
                    for i, name in enumerate(devices)],
    }, ensure_ascii=False), encoding="utf-8")
    return TcpChannel(name=channel, host=host, port=port,
                      devices=[Device(name=name, unit_id=10 + i,
                                      points=[Point("x", "hr", 0, "uint16")])
                               for i, name in enumerate(devices)])


def test_no_declaration_means_nothing_to_add(tmp_path):
    channel = TcpChannel(name="tcp-1", host="127.0.0.1", port=5020)
    assert endpoints.for_channel(channel, tmp_path / "nope.csv") == []
    assert endpoints.declared(tmp_path / "nope.csv") == []


def test_declared_dedupes_the_same_endpoint(tmp_path):
    """两台设备声明同一个 (ip, port) 是配置错误（`unique_addresses` 会拦），
    但"声明里有哪些端点"是纯查询，不许跟着炸。"""
    channel = _table(tmp_path, devices=("a", "b"))
    (tmp_path / f"{channel.name}.net.json").write_text(json.dumps({
        "devices": [{"name": "a", "ip": "10.136.107.10"},
                    {"name": "b", "ip": "10.136.107.10"}]},
        ensure_ascii=False), encoding="utf-8")
    assert endpoints.declared(tmp_path / f"{channel.name}.csv") == [("10.136.107.10", 502)]


def test_endpoints_come_from_the_declaration(tmp_path):
    channel = _table(tmp_path)
    assert endpoints.declared(tmp_path / f"{channel.name}.csv") == [
        ("10.136.107.10", 502), ("10.136.107.11", 502)]


def test_names_must_match_the_point_table(tmp_path):
    """声明里有、点表里没有的名字不算这一路的：多通道工程里另一半属于别的通道。"""
    channel = _table(tmp_path, devices=("风机-1",))
    (tmp_path / f"{channel.name}.net.json").write_text(json.dumps({
        "devices": [{"name": "风机-1", "ip": "10.136.107.10"},
                    {"name": "别人的设备", "ip": "10.136.107.99"}]},
        ensure_ascii=False), encoding="utf-8")
    assert endpoints.for_channel(channel, tmp_path / f"{channel.name}.csv") == [
        ("10.136.107.10", 502)]


def test_a_wildcard_host_already_covers_the_same_port(tmp_path):
    """通道绑 0.0.0.0:502 时，声明里的 10.136.107.10:502 已经在听——再挂一次就 10048。"""
    channel = _table(tmp_path, host=WILDCARD_HOST, port=502)
    assert endpoints.for_channel(channel, tmp_path / f"{channel.name}.csv") == []


def test_a_wildcard_host_does_not_cover_another_port(tmp_path):
    channel = _table(tmp_path, host=WILDCARD_HOST, port=5020)
    assert endpoints.for_channel(channel, tmp_path / f"{channel.name}.csv") == [
        ("10.136.107.10", 502), ("10.136.107.11", 502)]


def test_a_specific_host_covers_only_its_own_address(tmp_path):
    channel = _table(tmp_path, host="10.136.107.10", port=502)
    assert endpoints.for_channel(channel, tmp_path / f"{channel.name}.csv") == [
        ("10.136.107.11", 502)]


def test_the_channel_endpoint_is_never_listened_twice(tmp_path):
    """点表自己写着 127.0.0.1:5020、声明里也有 127.0.0.1:5020（自测配置）：只挂一次。"""
    channel = _table(tmp_path, host="127.0.0.1", port=5020)
    (tmp_path / f"{channel.name}.net.json").write_text(json.dumps({
        "devices": [{"name": "风机-1", "ip": "127.0.0.1", "port": 5020},
                    {"name": "照明回路-1", "ip": "10.136.107.11"}]},
        ensure_ascii=False), encoding="utf-8")
    assert endpoints.for_channel(channel, tmp_path / f"{channel.name}.csv") == [
        ("10.136.107.11", 502)]


def test_a_broken_declaration_does_not_stop_the_listener(tmp_path):
    """半截 JSON：网络那一步会报一句，监听层只管按通道自己的地址起。"""
    channel = _table(tmp_path)
    (tmp_path / f"{channel.name}.net.json").write_text("{半截文件", encoding="utf-8")
    assert endpoints.for_channel(channel, tmp_path / f"{channel.name}.csv") == []
    assert endpoints.declared(tmp_path / f"{channel.name}.csv") == []


def test_a_serial_channel_never_gets_extra_ip_endpoints(tmp_path):
    """串口通道没有"设备 IP:端口"这回事：声明里有同名设备也不该让这一路去挂监听。

    没有这条守卫时，`for_channel` 会去读串口通道根本没有的 `host`/`port`（AttributeError），
    而这一段只是"多听几个地址"的旁路，不该把起从站拦下来。
    """
    tcp = _table(tmp_path, devices=("风机-1",))
    table = tmp_path / f"{tcp.name}.csv"
    serial = SerialChannel("com-485-1", port="COM3", devices=[
        Device("风机-1", 11, [Point("x", "hr", 0, "uint16", initial="1")])])
    assert endpoints.for_channel(serial, table) == []
    # 对照：同一份声明对 TCP 通道是有端点的——证明上面那条不是"什么都没测到"
    assert endpoints.for_channel(tcp, table) != []


def test_missing_point_table_path_is_not_an_error():
    assert endpoints.declared(None) == []
    assert endpoints.for_channel(TcpChannel(name="tcp-1"), None) == []


def test_unmatched_names_are_reported_in_declaration_order(tmp_path):
    channel = _table(tmp_path, devices=("风机-1",))
    declaration = json.loads((tmp_path / f"{channel.name}.net.json").read_text("utf-8"))
    declaration["devices"].append({"name": "打错的设备", "ip": "10.0.0.9"})
    (tmp_path / f"{channel.name}.net.json").write_text(
        json.dumps(declaration, ensure_ascii=False), encoding="utf-8")
    project = Project(channels=[channel])
    assert endpoints.unmatched_names(
        _spec(tmp_path, channel.name), project) == ["打错的设备"]


def _spec(tmp_path, name):
    from simbench.net import spec as netspec
    return netspec.load(tmp_path / f"{name}.net.json")


# ---------- status()：人主动问的那一路 ----------

def test_status_speaks_up_when_there_is_no_declaration(tmp_path):
    """面板上一片空白看不出是"没问题"还是"没声明"，所以这里必须留一句话。"""
    channel = _table(tmp_path)
    (tmp_path / f"{channel.name}.net.json").unlink()
    lines = startup.status(tmp_path / f"{channel.name}.csv")
    assert len(lines) == 1, lines
    assert "没有网络声明" in lines[0] and ".net.json" in lines[0], lines


def test_status_ignores_the_automatic_switch(tmp_path, monkeypatch):
    """总闸管的是"启动时别自动动手"，人主动点"刷新"不许也被它挡掉。"""
    monkeypatch.setenv(startup.AUTO_ENV, "0")
    channel = _table(tmp_path)
    lines = startup.status(tmp_path / f"{channel.name}.csv")
    assert lines and "网络声明有问题" not in lines[0]


def test_status_reports_a_broken_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv(startup.AUTO_ENV, "0")
    channel = _table(tmp_path)
    (tmp_path / f"{channel.name}.net.json").write_text("{半截文件", encoding="utf-8")
    lines = startup.status(tmp_path / f"{channel.name}.csv")
    assert len(lines) == 1 and lines[0].startswith("网络声明有问题")


def test_status_without_a_point_table_says_so():
    assert startup.status(None) == ["还没有点表：先打开或导入一份点表，网络这一页才有对象"]
