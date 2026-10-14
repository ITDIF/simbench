"""新加的三种传输（E7：UDP / RTU-over-TCP / 串口 ASCII）——点表这一侧的形状。

后端那一侧的真主站验收在 `test_backend_udp.py` / `test_backend_rtutcp.py` /
`test_backend_ascii.py`；这里只钉"一份工程能把这三路写清楚、读回来、认出是哪一种"。

为什么单开一个文件：**加一种传输最容易漏的地方不是后端，而是"怎么把它写进点表、
怎么读回来、界面上怎么显示"**——那几处加漏了，表现是"这一类通道导出一遍就变成另一种"。
"""

from __future__ import annotations

import pytest

from simbench.backend import make_backend
from simbench.backend.modbus_ascii import ModbusAsciiBackend
from simbench.backend.modbus_rtutcp import RtuOverTcpBackend
from simbench.backend.modbus_tcp import ModbusTcpBackend
from simbench.backend.modbus_udp import ModbusUdpBackend
from simbench.project import edit
from simbench.project.csv_io import read_csv, write_csv
from simbench.project.json_io import from_dict, to_dict, to_json
from simbench.project.model import (
    MODBUS_TRANSPORTS,
    Device,
    Point,
    Project,
    SerialChannel,
    TcpChannel,
    channel_transport,
    is_serial,
    is_socket,
)
from simbench.project.validate import error_lines, validate_project

HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,"
          "initial,waveform,wave_param,readonly")


def one_point() -> list[Device]:
    return [Device("D1", 11, [Point("温度", "hr", 0, "uint16", initial="25")])]


def csv_with(channel_name: str) -> str:
    return f"{HEADER}\n{channel_name},D1,11,温度,hr,0,uint16,ABCD,25,const,,false\n"


# ---------- 传输名与载体 ----------

def test_the_six_transports_are_named_once():
    """传输名与"有没有 host:port""是不是串口"这两件事只在一张表里说（`model.py`）。"""
    assert MODBUS_TRANSPORTS == ("tcp", "udp", "rtutcp", "rtu", "ascii")
    assert [is_socket(t) for t in MODBUS_TRANSPORTS] == [True, True, True, False, False]
    assert [is_serial(t) for t in MODBUS_TRANSPORTS] == [False, False, False, True, True]


# ---------- CSV：写全 → 读回来是同一种传输 ----------

@pytest.mark.parametrize("channel_name, transport, kind", [
    ("tcp-5020@tcp:0.0.0.0:5020", "tcp", TcpChannel),
    ("udp-5020@udp:0.0.0.0:5020", "udp", TcpChannel),
    ("gw-1@rtutcp:192.168.0.9:4001", "rtutcp", TcpChannel),
    ("com-485-1@COM3 19200 8E1", "rtu", SerialChannel),
    ("com-485-1@COM3 19200 8E1 ascii", "ascii", SerialChannel),
])
def test_a_channel_name_carries_its_transport_both_ways(channel_name, transport, kind):
    project, _notes = read_csv(csv_with(channel_name), with_notes=True)
    (ch,) = project.channels
    assert isinstance(ch, kind) and channel_transport(ch) == transport
    # 导出一遍：写法逐字一致（写不全 = 导出再导入会变成另一种传输）
    text = write_csv(project)
    assert channel_name in text, text
    again, _notes = read_csv(text, with_notes=True)
    assert channel_transport(again.channels[0]) == transport


def test_an_ascii_channel_keeps_its_frame_type_through_json():
    project, _notes = read_csv(csv_with("com-485-1@COM3 19200 8E1 ascii"), with_notes=True)
    again = from_dict(to_dict(project))
    (ch,) = again.channels
    assert (type(ch).__name__, ch.framing) == ("SerialChannel", "ascii")
    assert '"type": "ascii"' in to_json(project), "JSON 里 `type` 写的就是传输名"


def test_an_old_rtu_json_still_loads():
    """老工程文件（`type: rtu`、没有 framing 键）照读：帧类型缺省 rtu。"""
    raw = {"version": 1, "tick_ms": 100, "channels": [
        {"name": "com-485-1", "type": "rtu", "port": "COM3", "baudrate": 9600,
         "parity": "E", "bytesize": 8, "stopbits": 1, "timeout": 0.3, "devices": []}]}
    (ch,) = from_dict(raw).channels
    assert (ch.framing, channel_transport(ch)) == ("rtu", "rtu")


# ---------- 校验：三种新传输都要过 ----------

@pytest.mark.parametrize("channel_name", [
    "udp-5020@udp:0.0.0.0:5020",
    "gw-1@rtutcp:192.168.0.9:4001",
    "com-485-1@COM3 19200 8E1 ascii",
])
def test_a_new_transport_passes_validation(channel_name):
    project, _notes = read_csv(csv_with(channel_name), with_notes=True)
    assert error_lines(validate_project(project)) == []


def test_a_bad_serial_framing_is_reported():
    """帧类型写成一个不存在的词：报 `serial_framing`（第四段只认 rtu/ascii）。"""
    serial = SerialChannel("com-485-1", port="COM3", framing="binary")
    issues = validate_project(Project(channels=[serial]))
    assert [i.code for i in issues if i.code == "serial_framing"], [str(i) for i in issues]


# ---------- 从零建：六种都能建出一条默认通道 ----------

@pytest.mark.parametrize("transport", edit.TRANSPORTS)
def test_every_transport_can_be_created_from_scratch(transport):
    """六种传输都能从零建出一条合法通道。

    串口两种要显式给串口名：本工具**不替现场猜 COM 口**（见 `edit.new_project` 的说明），
    没给的话那一条错误是刻意留着的。
    """
    values = {"port": "COM3"} if transport in ("rtu", "ascii") else {}
    project = edit.new_project(transport, **values)
    (ch,) = project.channels
    assert channel_transport(ch) == transport
    assert error_lines(validate_project(project)) == [], f"{transport} 建出来就不合法"


# ---------- 后端工厂 ----------

@pytest.mark.parametrize("channel_name, expected", [
    ("tcp-1@tcp:127.0.0.1:5020", ModbusTcpBackend),
    ("udp-1@udp:127.0.0.1:5021", ModbusUdpBackend),
    ("gw-1@rtutcp:127.0.0.1:5022", RtuOverTcpBackend),
])
def test_the_factory_picks_the_right_backend(channel_name, expected):
    project, _notes = read_csv(csv_with(channel_name), with_notes=True)
    backend = make_backend(project.channels[0])
    assert type(backend) is expected


@pytest.mark.parametrize("channel_name, expected", [
    ("com-485-1@COM3 9600", "rtu"),
    ("com-485-1@COM3 9600 8E1 ascii", "ascii"),
])
def test_the_factory_picks_the_serial_framing(channel_name, expected):
    project, _notes = read_csv(csv_with(channel_name), with_notes=True)
    backend = make_backend(project.channels[0])
    assert expected == backend.TRANSPORT
    if expected == "ascii":
        assert isinstance(backend, ModbusAsciiBackend), "ASCII 通道拿到的是 RTU 后端"
