"""串口通道的模型与往返（M5 Task 1）：默认值、传输判定、JSON 的键、CSV 的通道名语法。

三条口径在这里钉住：

1. `SerialChannel` 的字段就是 spec §5 那份 project.json 的键（多一个少一个都算改契约）；
2. "这路是什么传输"只由 `channel_transport()` 一处回答——散成若干 `isinstance` 的写法，
   加第三种通道时必然漏掉一处，而漏掉的表现是"某一处按 TCP 处理了一路串口"；
3. CSV 的通道列用 `name@COM3 19200 8E1`（段之间**空格**，逗号在 CSV 里要加引号）表示串口
   通道，导出与导入是同一套语法：CSV 往返不丢参数。丢参数的表现是"导出一份、导回来变成
   TCP 通道"，现场最难发现的那种。
"""

import json

import pytest

from simbench.project.csv_io import parse_serial_name, read_csv, write_csv
from simbench.project.json_io import from_dict, to_dict, to_json
from simbench.project.model import (
    SERIAL_DEFAULTS,
    Project,
    SerialChannel,
    TcpChannel,
    channel_transport,
    format_frame,
    parse_frame,
)
from simbench.project.validate import validate_project

HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,initial,"
          "waveform,wave_param,readonly")


def csv_with(channel: str) -> str:
    """一张只差通道名的点表：每行一个点位，方便逐条改通道那一格。"""
    return (HEADER + "\n"
            + f"{channel},COVI-1,11,x,hr,0,uint16,ABCD,1,const,,false\n")


# ---------- 模型 ----------

def test_a_serial_channel_starts_from_the_spec_defaults():
    """新建一路串口通道时只有名字是必填：其余全是 spec §5 里那几个默认值。"""
    ch = SerialChannel("com-485-1")
    assert (ch.port, ch.baudrate, ch.parity, ch.bytesize, ch.stopbits, ch.timeout) \
        == (SERIAL_DEFAULTS["port"], SERIAL_DEFAULTS["baudrate"], SERIAL_DEFAULTS["parity"],
            SERIAL_DEFAULTS["bytesize"], SERIAL_DEFAULTS["stopbits"], SERIAL_DEFAULTS["timeout"])
    assert ch.devices == []


def test_a_serial_channel_has_no_default_port():
    """串口名没有默认值（2026-10-13 审查定的口径）：新建出来是空串，由 validate 拦下。

    留一个 COM1 当默认值的话，"手写 project.json 少写一个键"就静默指向本机真串口——
    那是替操作员猜了一个会动硬件的值。
    """
    assert SERIAL_DEFAULTS["port"] == ""
    assert SerialChannel("com-485-1").port == ""
    project = Project(channels=[SerialChannel("com-485-1")])
    assert ("serial_port", "com-485-1") in [
        (i.code, i.where) for i in validate_project(project) if i.level == "error"]


def test_channel_transport_is_the_only_place_that_tells_the_two_apart():
    assert channel_transport(TcpChannel("tcp-5020")) == "tcp"
    assert channel_transport(SerialChannel("com-485-1")) == "rtu"
    # 手写坏/还没支持的 type 原样回，由 validate 报 channel_type（不在这里猜一个）
    assert channel_transport(TcpChannel("x", type="udp")) == "udp"


@pytest.mark.parametrize("text", ["8N1", "8E1", "7O2", "8n1"])
def test_the_frame_format_round_trips(text):
    frame = parse_frame(text)
    assert frame is not None
    assert format_frame(frame["bytesize"], frame["parity"], frame["stopbits"]) \
        == text.upper()


@pytest.mark.parametrize("text", ["", "8X1", "81", "8N1N", "81N", "N81"])
def test_a_frame_that_is_not_written_as_8N1_is_not_parsed(text):
    """形状不对回 None：判定不在这里（`9N1` 形状是对的，由 validate 报"数据位只能是 7/8"）。"""
    assert parse_frame(text) is None


def test_a_frame_with_a_wrong_value_still_parses_so_validate_can_report_it():
    assert parse_frame("9N1") == {"bytesize": 9, "parity": "N", "stopbits": 1}


# ---------- project.json ----------

SPEC_SAMPLE = {
    "version": 1, "tick_ms": 100,
    "channels": [{"name": "com-485-1", "type": "rtu", "port": "COM3", "baudrate": 9600,
                  "parity": "E", "bytesize": 8, "stopbits": 1, "timeout": 0.3,
                  "devices": []}],
}


def test_the_spec_sample_loads_as_a_serial_channel():
    """spec §5 里那段 rtu 示例逐键可读，且类型上真的是 `SerialChannel`（不是带 type 的 TCP）。"""
    project = from_dict(SPEC_SAMPLE)
    (ch,) = project.channels
    assert isinstance(ch, SerialChannel)
    assert (ch.name, ch.port, ch.baudrate, ch.parity) == ("com-485-1", "COM3", 9600, "E")
    assert to_dict(project) == SPEC_SAMPLE, "读进来再写出去不是同一份 JSON"


def test_json_round_trip_keeps_serial_and_tcp_channels_apart():
    project = Project(channels=[TcpChannel("tcp-5020", port=5020),
                                SerialChannel("com-485-1", port="COM7", baudrate=115200)])
    text = to_json(project)
    again = from_dict(json.loads(text))
    assert [type(ch).__name__ for ch in again.channels] == ["TcpChannel", "SerialChannel"]
    assert to_json(again) == text, "同一份工程反复存盘必须产出同样的字节"


@pytest.mark.parametrize("key, value", [("baudrate", "9600"), ("bytesize", "8"),
                                        ("stopbits", 1.5), ("timeout", "0.3")])
def test_a_serial_number_typed_as_a_string_is_rejected_at_load(key, value):
    """数字字段只认 JSON 数字：`int("9600")` 看着也能用，但那是在替用户猜类型（同 port/address）。"""
    data = json.loads(json.dumps(SPEC_SAMPLE))
    data["channels"][0][key] = value
    with pytest.raises(ValueError, match=key):
        from_dict(data)


# ---------- CSV 通道名语法 ----------

def test_a_serial_channel_comes_from_the_channel_column():
    project = read_csv(csv_with("com-485-1@COM3 19200 8E1"))
    (ch,) = project.channels
    assert isinstance(ch, SerialChannel)
    assert (ch.name, ch.port, ch.baudrate, ch.parity, ch.bytesize, ch.stopbits) \
        == ("com-485-1", "COM3", 19200, "E", 8, 1)


def test_a_serial_channel_with_only_a_port_gets_the_defaults():
    (ch,) = read_csv(csv_with("com-485-1@COM3")).channels
    assert (ch.port, ch.baudrate, ch.parity) == ("COM3", 9600, "N")


def test_the_csv_round_trip_keeps_every_serial_parameter():
    """导出再导入必须还原成同一路串口通道——参数丢了会变成"另一台设备"，且不报错。"""
    project = read_csv(csv_with("com-485-1@COM3 19200 8E1"))
    text = write_csv(project)
    assert "com-485-1@COM3 19200 8E1" in text
    again = read_csv(text)
    assert again.channels[0] == project.channels[0]


def test_a_plain_channel_name_is_still_a_tcp_channel_with_a_guessed_port():
    """回归：既有的一列（`tcp-502`）一个字都没变——仍按名字猜端口，仍记一条 note。"""
    project, ports_before = read_csv(csv_with("tcp-502"), with_notes=True)
    (ch,) = project.channels
    assert (type(ch).__name__, ch.host, ch.port) == ("TcpChannel", "0.0.0.0", 502)
    assert any("port guessed as 502" in note for note in ports_before)


@pytest.mark.parametrize("bad, hint", [
    ("@COM3", "no name"),
    ("com3@", "at most three parts"),
    ("com3@COM3 9600 8E1 9", "at most three parts"),
    ("com3@COM3 abc", "is not an integer"),
    ("com3@COM3 9600 8X1", "must look like 8N1"),
])
def test_a_broken_serial_channel_name_is_rejected_with_its_line_number(bad, hint):
    """写坏的一格不是"值不合法"而是"读不懂"：带行号当场拒，别让它变成一路怪通道。"""
    with pytest.raises(ValueError) as excinfo:
        read_csv(csv_with(bad))
    assert "line 2" in str(excinfo.value)
    assert hint in str(excinfo.value)


def test_the_same_channel_name_may_not_carry_two_serial_settings():
    text = (HEADER + "\n"
            + "com-485-1@COM3,COVI-1,11,a,hr,0,uint16,ABCD,1,const,,false\n"
            + "com-485-1@COM4,COVI-1,11,b,hr,1,uint16,ABCD,1,const,,false\n")
    with pytest.raises(ValueError, match="two different serial settings"):
        read_csv(text)


def test_a_channel_that_started_as_tcp_may_not_turn_serial_mid_table():
    text = (HEADER + "\n"
            + "com-485-1,COVI-1,11,a,hr,0,uint16,ABCD,1,const,,false\n"
            + "com-485-1@COM3,COVI-1,11,b,hr,1,uint16,ABCD,1,const,,false\n")
    with pytest.raises(ValueError, match="already appeared"):
        read_csv(text)


def test_parse_serial_name_reads_the_same_grammar_the_writer_emits():
    name, channel = parse_serial_name("com-485-1@COM3 9600 8N1", 1)
    assert name == "com-485-1"
    assert (channel.port, channel.baudrate, channel.parity) == ("COM3", 9600, "N")
    assert parse_serial_name("tcp-502", 1) is None
