"""S7 通道的模型与往返（Task 1）：默认值、传输判定、CSV 的 `name@host:port 0/1` 语法、JSON 键。

三条要钉的：

1. `S7Channel` 的字段就是计划里那几个（`name/host/port/rack/slot/devices`），`type` 不设字段；
2. CSV 通道名语法与串口那条**共用 `@` 但不会互相抢**——pyserial 的 URL（`socket://`）与纯
   端口名（`COM3`）仍然走串口，`host:port` 才是 S7；反过来问会把一路 S7 静默变成串口；
3. 往返不丢参数，尤其是 `bool` 点位的**位**（`4.3` 这种地址格）与 `rack/slot`。
"""

import json

import pytest

from simbench.project.csv_io import parse_s7_name, read_csv, write_csv
from simbench.project.json_io import from_dict, to_dict, to_json
from simbench.project.model import (
    S7_DEFAULTS,
    Device,
    Point,
    Project,
    S7Channel,
    SerialChannel,
    TcpChannel,
    channel_transport,
    parse_s7_address,
    parse_s7_area,
    s7_area_name,
    s7_span_bytes,
    s7_summary,
)

HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,initial,"
          "waveform,wave_param,readonly")


def csv_with(channel: str, points: str | None = None) -> str:
    points = points or "温度,db1,0,real,ABCD,12.5,const,,false\n"
    return HEADER + "\n" + "".join(
        f"{channel},D1,11,{row}\n" for row in points.splitlines() if row)


def test_a_s7_channel_starts_from_the_defaults():
    ch = S7Channel("s7-1")
    assert (ch.host, ch.port, ch.rack, ch.slot) == (S7_DEFAULTS["host"], 102, 0, 1)
    assert ch.devices == []


def test_channel_transport_knows_three_kinds():
    assert channel_transport(TcpChannel("tcp-1")) == "tcp"
    assert channel_transport(SerialChannel("com-1")) == "rtu"
    assert channel_transport(S7Channel("s7-1")) == "s7"
    assert channel_transport(TcpChannel("x", type="udp")) == "udp"


@pytest.mark.parametrize("text, expected", [
    ("db1", ("db", 1)), ("DB12", ("db", 12)), ("mk", ("mk", None)),
    ("PE", ("pe", None)), ("pa", ("pa", None)),
    ("db0", ("db", 0)), ("xx", None), ("", None), (None, None),
])
def test_area_parsing_is_conversion_only(text, expected):
    """`db0` 这种"形状对、值不合法"的照样解析出来——拦它是 validate 的活。"""
    assert parse_s7_area(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("5", (5, None)), ("0.3", (0, 3)), ("0.9", (0, 9)),
    ("a.b", None), ("1.2.3", None), ("", None), (None, None),
])
def test_address_parsing_is_conversion_only(text, expected):
    assert parse_s7_address(text) == expected


def test_area_name_and_summary_round_trip():
    assert s7_area_name("db", 3) == "db3"
    assert s7_area_name("mk", None) == "mk"
    assert s7_span_bytes("real", "1") == 4
    assert s7_span_bytes("string", "FAN-01") == 6
    assert s7_summary(S7Channel("s7-1", host="192.168.0.10")) \
        == "192.168.0.10:102 rack 0 slot 1"


# ---------- CSV ----------

def test_a_s7_channel_comes_from_the_channel_column():
    (ch,) = read_csv(csv_with("s7-1@192.168.0.10:102 0/1")).channels
    assert isinstance(ch, S7Channel)
    assert (ch.name, ch.host, ch.port, ch.rack, ch.slot) \
        == ("s7-1", "192.168.0.10", 102, 0, 1)


def test_rack_and_slot_default_when_omitted():
    (ch,) = read_csv(csv_with("s7-1@192.168.0.10:1102")).channels
    assert (ch.host, ch.port, ch.rack, ch.slot) == ("192.168.0.10", 1102, 0, 1)


def test_the_csv_round_trip_keeps_the_bits_and_the_rack_slot():
    """导出再导入必须还原成同一路通道：`bool` 的位（`4.3`）与 rack/slot 一个都不许丢。"""
    text = csv_with("s7-1@192.168.0.10:102 2/3",
                    "温度,db1,0,real,ABCD,12.5,const,,false\n"
                    "启动,db1,4.3,bool,ABCD,true,const,,false\n")
    project = read_csv(text)
    assert "s7-1@192.168.0.10:102 2/3" in write_csv(project)
    assert "db1,4.3,bool" in write_csv(project)
    assert read_csv(write_csv(project)).channels[0] == project.channels[0]


def test_the_default_rack_slot_is_not_written():
    out = write_csv(read_csv(csv_with("s7-1@10.0.0.1:102")))
    assert out.splitlines()[1].startswith("s7-1@10.0.0.1:102,")


MODBUS_ROW = "x,hr,0,uint16,ABCD,1,const,,false"


def test_a_serial_channel_is_not_taken_over_by_the_s7_grammar():
    """两条语法共用 `@`：`socket://` 与纯端口名仍然是串口（S7 只在有 `host:port` 时才认）。

    点表用的是**Modbus 那一行的写法**（`hr`/`uint16`）：串口通道配 S7 的区/类型是自相矛盾的
    输入，会被下面那条"漏端口"判据拦下——这里测的是"合法的串口通道没被 S7 语法抢走"。
    """
    for name in ("com-485-1@COM3 19200 8E1", "com-485-1@socket://127.0.0.1:5000"):
        (ch,) = read_csv(csv_with(name, MODBUS_ROW)).channels
        assert isinstance(ch, SerialChannel), name


def test_a_tcp_channel_still_works():
    (ch,) = read_csv(csv_with("tcp-5020", MODBUS_ROW)).channels
    assert isinstance(ch, TcpChannel) and ch.port == 5020


@pytest.mark.parametrize("bad, hint", [
    ("@192.168.0.10:102", "no name"),
    ("s7-1@host:abc", "must be written as"),
    ("s7-1@h:102 0", "must look like 0/1"),
    ("s7-1@h:102 0/1/2", "must look like 0/1"),
    ("s7-1@h:102 0/1 x", "at most two parts"),
])
def test_a_broken_s7_channel_name_is_rejected_with_its_line_number(bad, hint):
    with pytest.raises(ValueError) as excinfo:
        read_csv(csv_with(bad))
    assert "line 2" in str(excinfo.value)
    assert hint in str(excinfo.value)


def test_a_missing_port_is_explained_instead_of_quietly_becoming_a_serial_port():
    """`name@host`（有 `@`、没冒号）配上 S7 的区/类型：几乎肯定是漏了端口，要说清两种读法。

    只问通道名是分不出来的——`name@host` 与"串口名叫 host"语法一模一样；同一行的区/类型
    是现成的证据（`db1` 不是 Modbus 的写法）。
    """
    with pytest.raises(ValueError) as excinfo:
        read_csv(csv_with("s7-1@192.168.0.10"))
    message = str(excinfo.value)
    assert "line 2" in message and "no ':'" in message and "missing port" in message


def test_the_same_name_may_not_carry_two_s7_settings():
    text = (HEADER + "\n"
            + "s7-1@10.0.0.1:102,D1,11,a,db1,0,real,ABCD,1,const,,false\n"
            + "s7-1@10.0.0.2:102,D1,11,b,db1,4,real,ABCD,1,const,,false\n")
    with pytest.raises(ValueError, match="two different S7 settings"):
        read_csv(text)


def test_parse_s7_name_reads_the_grammar_the_writer_emits():
    name, channel = parse_s7_name("s7-1@192.168.0.10:102 1/2", 1)
    assert name == "s7-1"
    assert (channel.host, channel.port, channel.rack, channel.slot) \
        == ("192.168.0.10", 102, 1, 2)
    assert parse_s7_name("tcp-502", 1) is None
    assert parse_s7_name("com-1@COM3", 1) is None       # 串口那一路
    assert parse_s7_name("com-1@socket://h:1", 1) is None


# ---------- project.json ----------

def test_json_round_trip_keeps_the_s7_keys_and_the_bit():
    project = Project(channels=[S7Channel("s7-1", host="192.168.0.10", port=1102,
                                          rack=0, slot=2, devices=[
        Device("D1", 11, [Point("温度", "db1", 0, "real", initial="12.5"),
                          Point("启动", "db1", 4, "bool", initial="true", bit=3)])])])
    text = to_json(project)
    again = from_dict(json.loads(text))
    assert isinstance(again.channels[0], S7Channel)
    assert to_json(again) == text, "同一份工程反复存盘必须产出同样的字节"
    assert '"type": "s7"' in text and '"bit": 3' in text


def test_the_bit_key_only_appears_on_bit_points():
    """没有位的点不许写出 `"bit": null`（既有 project.json 会全体变样，那条往返用例会红）。"""
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 11, [Point("温度", "db1", 0, "real", initial="1")])])])
    assert "bit" not in json.dumps(to_dict(project))


@pytest.mark.parametrize("key, value", [("port", "102"), ("rack", "0"), ("slot", 1.5)])
def test_a_s7_number_typed_as_a_string_is_rejected_at_load(key, value):
    data = {"version": 1, "tick_ms": 100, "channels": [
        {"name": "s7-1", "type": "s7", "host": "10.0.0.1", "port": 102, "rack": 0,
         "slot": 1, "devices": []}]}
    data["channels"][0][key] = value
    with pytest.raises(ValueError, match=key):
        from_dict(data)
