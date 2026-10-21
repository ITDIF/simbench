"""MQTT 通用模板：topic/payload、CSV/JSON、校验、模拟后端（假 broker 会话）。"""

from __future__ import annotations

import asyncio

from simbench.backend.mqtt_client import MqttBackend
from simbench.project.csv_io import read_csv, write_csv
from simbench.project.json_io import from_dict, to_dict
from simbench.project.model import Device, MqttChannel, Point, Project, channel_transport
from simbench.project.mqtt_topic import (
    decode_payload,
    encode_payload,
    set_topic,
    value_topic,
)
from simbench.project.validate import error_lines, validate_project

HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,"
          "initial,waveform,wave_param,readonly")


def channel(prefix="simbench", qos=1) -> MqttChannel:
    return MqttChannel("mqtt-1", host="broker", port=1883, topic_prefix=prefix,
                       qos=qos, devices=[
                           Device("D1", 1, [Point("温度", "hr", 0, "uint16",
                                                  initial="25")])])


class FakeSession:
    """假 broker 会话：记录订阅/发布，`deliver` 把消息交给后端回调。"""

    def __init__(self, host, port, on_message, on_state=None):
        self.host, self.port, self.on_message = host, port, on_message
        self.on_state = on_state
        self.connected = False
        self.subscribed: list[tuple[str, int]] = []
        self.published: list[tuple[str, str, int, bool]] = []
        self.closed = False

    def connect(self, timeout=5.0):
        self.connected = True

    def subscribe(self, topic, qos=1):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=1, retain=True):
        self.published.append((topic, payload, qos, retain))

    def close(self):
        self.closed = True


def test_topic_and_payload_template():
    assert value_topic("simbench", "mqtt-1", "D1", "hr", 0) == "simbench/mqtt-1/D1/hr/0"
    assert set_topic("simbench", "mqtt-1", "D1", "hr", 0) == \
        "simbench/mqtt-1/D1/hr/0/set"
    # topic 段里的分隔符/通配符要转义，不然一条点位会变成另一条的子树
    assert value_topic("p", "a/b", "d+", "h#", 1) == "p/a_b/d_/h_/1"
    assert decode_payload(encode_payload(25.5)) == 25.5
    assert decode_payload(b"true") is True
    assert decode_payload(b"25") == 25
    assert decode_payload(b'"FAN-1"') == "FAN-1"


def test_csv_round_trip_and_json_round_trip():
    text = (f"{HEADER}\n"
            "mqtt-1@mqtt:broker:1883,D1,1,温度,hr,0,uint16,ABCD,25,const,,false\n")
    project = read_csv(text)
    (ch,) = project.channels
    assert isinstance(ch, MqttChannel) and channel_transport(ch) == "mqtt"
    assert (ch.host, ch.port, ch.topic_prefix, ch.qos) == ("broker", 1883, "simbench", 1)
    # 导出/再导入逐字一致
    assert "mqtt-1@mqtt:broker:1883" in write_csv(project)
    again = read_csv(write_csv(project))
    assert isinstance(again.channels[0], MqttChannel)
    # JSON 往返
    back = from_dict(to_dict(project))
    assert isinstance(back.channels[0], MqttChannel)
    assert back.channels[0].topic_prefix == "simbench"

    # 非默认前缀/QoS 也写进 CSV
    custom = channel(prefix="plant", qos=2)
    assert "mqtt-1@mqtt:broker:1883 plant 2" in write_csv(
        Project(channels=[custom]))
    # retain=False / raw payload 也能往返
    custom = channel()
    custom.retain = False
    custom.payload = "raw"
    text = write_csv(Project(channels=[custom]))
    assert "mqtt-1@mqtt:broker:1883 simbench 1 0 raw" in text
    back = read_csv(text).channels[0]
    assert (back.retain, back.payload) == (False, "raw")


def test_validation_checks_qos_prefix_and_duplicate_topics():
    assert error_lines(validate_project(Project(channels=[channel()]))) == []
    bad = channel(qos=5)
    codes = [issue.code for issue in validate_project(Project(channels=[bad]))]
    assert "mqtt_qos" in codes
    bad = channel()
    bad.payload = "xml"
    codes = [issue.code for issue in validate_project(Project(channels=[bad]))]
    assert "mqtt_payload" in codes

    duplicate = channel()
    duplicate.devices[0].points.append(
        Point("另一个", "hr", 0, "uint16", initial="1"))
    codes = [issue.code for issue in validate_project(Project(channels=[duplicate]))]
    assert "mqtt_topic" in codes


def test_backend_publishes_initial_values_and_receives_set_messages():
    ch = channel()
    backend = MqttBackend(ch, session_factory=FakeSession)
    asyncio.run(backend.start())
    session = backend._server
    try:
        assert session.connected
        assert session.subscribed == [("simbench/mqtt-1/D1/hr/0/set", 1)]
        assert session.published == [("simbench/mqtt-1/D1/hr/0",
                                      '{"value":25}', 1, True)]
        assert backend.describe().startswith("broker:1883")
        assert backend.endpoints == []

        backend._apply_set("simbench/mqtt-1/D1/hr/0/set", b'{"value":31}')
        assert backend._values[(1, "hr", 0)] == 31
        assert session.published[-1] == ("simbench/mqtt-1/D1/hr/0",
                                         '{"value":31}', 1, True)
        assert backend._sources.is_waveform("mqtt-1", 1, "hr", 0) is False

        # 同一个值不重复发（波形 tick 每 100ms 一次，不能让 broker 被刷屏）
        before = len(session.published)
        backend._write_point(1, ch.devices[0].points[0], 31)
        assert len(session.published) == before
    finally:
        asyncio.run(backend.stop())
    assert session.closed


def test_disconnect_and_reconnect_are_logged():
    logs: list[tuple[str, str]] = []
    backend = MqttBackend(channel(), session_factory=FakeSession,
                          log_sink=lambda level, text: logs.append((level, text)))
    asyncio.run(backend.start())
    try:
        backend._server.on_state(False, "keepalive timeout")
        backend._server.on_state(True, "connected")
        text = "\n".join(line for _level, line in logs)
        assert "断开" in text and "已连接" in text, logs
    finally:
        asyncio.run(backend.stop())


def test_connection_state_reaches_the_status_sink():
    states: list[tuple[str, str]] = []
    backend = MqttBackend(channel(), session_factory=FakeSession,
                          state_sink=lambda state, detail: states.append((state, detail)))
    asyncio.run(backend.start())
    try:
        backend._publish_state(False, "keepalive timeout")
        backend._publish_state(True, "connected")
        assert [state for state, _ in states] == ["error", "running"]
        assert "重连" in states[0][1]
    finally:
        asyncio.run(backend.stop())


def test_raw_payload_and_retain_false_are_published():
    ch = channel()
    ch.retain = False
    ch.payload = "raw"
    backend = MqttBackend(ch, session_factory=FakeSession)
    asyncio.run(backend.start())
    try:
        assert backend._server.published[0] == ("simbench/mqtt-1/D1/hr/0",
                                                "25", 1, False)
        backend._apply_set("simbench/mqtt-1/D1/hr/0/set", b"31")
        assert backend._values[(1, "hr", 0)] == 31
        assert backend._server.published[-1] == ("simbench/mqtt-1/D1/hr/0",
                                                 "31", 1, False)
    finally:
        asyncio.run(backend.stop())


def test_readback_feeds_the_point_table():
    ch = channel()
    ch.devices[0].points.append(Point("报警", "di", 0, "bool", initial="false"))
    backend = MqttBackend(ch, session_factory=FakeSession)
    asyncio.run(backend.start())
    try:
        assert backend.read_registers(1, "hr", 0, 1) == [25]
        assert backend.read_bits(1, "di", 0, 1) == [False]
        backend._apply_set("simbench/mqtt-1/D1/hr/0/set", b'{"value":31}')
        backend._apply_set("simbench/mqtt-1/D1/di/0/set", b'{"value":true}')
        assert backend.read_registers(1, "hr", 0, 1) == [31]
        assert backend.read_bits(1, "di", 0, 1) == [True]
    finally:
        asyncio.run(backend.stop())


def test_master_reads_subscribed_values_and_probes():
    from simbench.master.mqtt import MqttMasterLink
    from simbench.master.scan import verify_points

    link = MqttMasterLink(channel(), session_factory=FakeSession)
    link.open()
    try:
        assert link._session.subscribed == [("simbench/mqtt-1/D1/hr/0", 1)]
        link._on_message("simbench/mqtt-1/D1/hr/0", b'{"value":25}')
        result = link.read(1, "hr", 0, 1)
        assert result.ok and result.values == [25], result
        assert link.probe(1).ok
        assert not link.read(2, "hr", 0, 1).ok
        (row,) = verify_points(link, Project(channels=[channel()]))
        assert row.status == "ok", row
    finally:
        link.close()


def test_the_new_channel_form_builds_an_mqtt_channel(qapp):
    from simbench.gui.channel_form import ChannelForm
    from simbench.project import edit

    form = ChannelForm(kinds=("mqtt",), start="mqtt")
    try:
        form.pick("mqtt")
        form.name_edit.setText("mqtt-9")
        form.first_edit.setText("broker")
        form.second_edit.setText("1883")
        form.third_edit.setText("2")
        ch = edit.default_channel(Project(), "mqtt", **form.values())
        assert isinstance(ch, MqttChannel)
        assert (ch.host, ch.port, ch.qos) == ("broker", 1883, 2)
    finally:
        form.deleteLater()


def test_a_bad_set_payload_is_logged_and_ignored():
    logs: list[tuple[str, str]] = []
    backend = MqttBackend(channel(), session_factory=FakeSession,
                          log_sink=lambda level, text: logs.append((level, text)))
    asyncio.run(backend.start())
    try:
        before = list(backend._server.published)
        backend._apply_set("simbench/mqtt-1/D1/hr/0/set", b'{"value":"abc"}')
        assert backend._server.published == before
        assert logs and "payload" in logs[-1][1]
    finally:
        asyncio.run(backend.stop())
