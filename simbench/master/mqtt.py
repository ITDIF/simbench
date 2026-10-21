"""MQTT 主站链路：订阅点位的值 topic，把 payload 读回来给核对页。

与 `master/modbus.py` 的 `MasterLink` 同形（`open/close/read/probe/describe`），这样
`master/scan.verify_points` 不用知道底下是 Modbus 还是 MQTT。

MQTT 没有"读一个寄存器"的请求/应答：值由设备发布，主站只能订阅。所以这里在 `open()`
时订阅点表里所有值 topic，收到消息就缓存；`read()` 先看缓存，没有就等一小段时间。
retain=True 的设备（本工具的模拟侧就是）在订阅后立刻能拿到最后一份值。
"""

from __future__ import annotations

import contextlib
import threading
import time

from ..backend.mqtt_client import MqttSession
from ..codec.point_codec import encode
from ..project.model import area_is_bit
from ..project.mqtt_topic import decode_payload, status_topic, value_topic


class MqttMasterLink:
    """一个 MQTT 主站连接（一个通道一份）。"""

    def __init__(self, channel, *, timeout: float = 1.0,
                 session_factory=None, host: str | None = None,
                 port: int | None = None) -> None:
        self.channel = channel
        self.host = host or channel.host
        self.port = int(port or channel.port)
        self.timeout = timeout
        self._session_factory = session_factory or MqttSession
        self._session = None
        self._cache: dict[str, object] = {}
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._status_topic = status_topic(channel.topic_prefix, channel.name)
        # None = 设备没发布状态；True/False = status topic 明确说了在线/离线。
        self._online: bool | None = None
        self._points: dict[tuple[int, str, int], tuple[str, object]] = {}
        self._index()

    @classmethod
    def for_channel(cls, channel, *, timeout: float = 1.0,
                    host: str | None = None, port: int | None = None):
        return cls(channel, timeout=timeout, host=host, port=port)

    def _index(self) -> None:
        for device in self.channel.devices:
            for pt in device.points:
                topic = value_topic(self.channel.topic_prefix, self.channel.name,
                                    device.name, pt.area, pt.address)
                self._points[(device.unit_id, pt.area, pt.address)] = (topic, pt)

    def open(self) -> None:
        self._session = self._session_factory(self.host, self.port, self._on_message)
        try:
            self._session.connect(timeout=max(1.0, self.timeout))
            self._session.subscribe(self._status_topic, qos=int(self.channel.qos))
            for topic, _pt in self._points.values():
                self._session.subscribe(topic, qos=int(self.channel.qos))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()

    def _on_message(self, topic: str, payload) -> None:
        try:
            value = decode_payload(payload)
        except Exception:                     # noqa: BLE001
            return
        if topic == self._status_topic:
            if isinstance(value, dict):
                online = bool(value.get("online"))
            elif isinstance(value, str):
                online = value.strip().lower() in ("online", "true", "1", "yes", "on")
            else:
                online = bool(value)
            with self._lock:
                self._online = online
            self._arrived.set()
            return
        with self._lock:
            self._cache[topic] = value
        self._arrived.set()

    @property
    def online(self) -> bool | None:
        return self._online

    def read(self, unit_id: int, area: str, address: int,
             count: int = 1):
        from .modbus import ReadResult

        if self._online is False:
            return ReadResult(None, "设备在线状态为离线（status topic）")
        found = self._points.get((unit_id, area, address))
        if found is None:
            return ReadResult(None, f"点表里没有 unit {unit_id} 的 {area} {address}")
        topic, pt = found
        with self._lock:
            value = self._cache.get(topic)
        if value is None:
            deadline = time.monotonic() + self.timeout
            while value is None and time.monotonic() < deadline:
                self._arrived.wait(min(0.05, max(0.0, deadline - time.monotonic())))
                self._arrived.clear()
                with self._lock:
                    value = self._cache.get(topic)
        if value is None:
            return ReadResult(None, f"{topic} 在 {self.timeout:.1f}s 内没有收到值")
        try:
            text = ("true" if value else "false") if isinstance(value, bool) else str(value)
            registers = encode(text, pt.datatype, pt.word_order)
            if area_is_bit(area):
                return ReadResult([1 if registers[0] else 0], "")
            return ReadResult([int(value) & 0xFFFF for value in registers], "")
        except (TypeError, ValueError) as exc:
            return ReadResult(None, f"{topic} 的值 {value!r} 不能转成 {pt.datatype}（{exc}）")

    def probe(self, unit_id: int):
        return self.read(unit_id, "hr", 0, 1)

    def describe(self) -> str:
        return (f"{self.host}:{self.port} (mqtt, "
                f"prefix {self.channel.topic_prefix}, qos {self.channel.qos})")
