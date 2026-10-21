"""MQTT 模拟后端：连 broker、按通用模板发布点值、订阅 `/set` 接收写入。

与 Modbus/S7 后端最大的区别：**它不监听端口**，而是作为客户端连到 broker。因此
`endpoints` 恒为空、`_create_server` 返回的是一个 MQTT 会话；`_devices()` 也没有
SimDevice 那一层（MQTT 没有寄存器块，值直接按点位发布）。

topic 与 payload 的约定只在 `project/mqtt_topic.py` 一处。这里负责：

* 启动时把每个点位的当前值以 retain=True 发布一次（晚订阅的主站也能立刻读到）；
* 波形 tick 算出新值时，值变了就发布（不变不刷屏）；
* 订阅每个点位的 `/set`，收到的payload 按点位类型转换、标成 manual、再发布一次；
* 报文窗里留 `tx` / `rx` 事件（payload 原文 + topic 摘要），与 Modbus 的报文口径一致。

依赖 `paho-mqtt`；它只在 `MqttSession` 里 import（延迟到真正建会话时），没装也能 import
本模块、跑不碰 MQTT 的路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from typing import Any

from ..codec.point_codec import decode, encode
from ..project.model import area_is_bit
from ..project.mqtt_topic import (
    decode_payload,
    encode_payload,
    set_topic,
    status_payload,
    status_topic,
    value_topic,
)
from .base import BackendBase, BackendStartError


class MqttSession:
    """paho 客户端的最小包装：连接/订阅/发布/关闭，回调只把消息转给后端。"""

    def __init__(self, host: str, port: int, on_message, *,
                 client_id: str = "", keepalive: int = 30,
                 on_state=None, will=None) -> None:
        self.host = host
        self.port = int(port)
        self._on_message = on_message
        # `on_state(connected, detail)`：断线/重连给后端留一条日志；默认不关心。
        self._on_state = on_state
        self._will = will
        self._topics: list[tuple[str, int]] = []
        self._closing = False
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._sub_mid = None
        self._error = ""
        self._client: Any = None
        self._client_id = client_id or f"simbench-{uuid.uuid4().hex[:12]}"
        self._keepalive = keepalive

    def connect(self, timeout: float = 5.0) -> None:
        import paho.mqtt.client as mqtt
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id,
            protocol=mqtt.MQTTv311)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_mqtt_message
        self._client.on_subscribe = self._on_subscribe
        if self._will is not None:
            topic, payload, qos, retain = self._will
            self._client.will_set(topic, payload, qos=qos, retain=retain)
        # 断线后自动重连（1s 起、30s 封顶）；`loop_start` 跑的就是带重连的 loop_forever。
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self.host, self.port, keepalive=self._keepalive)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            self.close()
            raise ConnectionError(f"连不上 MQTT broker {self.host}:{self.port}")
        if self._error:
            self.close()
            raise BackendStartError(self._error)

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None):
        code = getattr(reason_code, "value", reason_code)
        if code not in (0, None):
            self._error = f"MQTT broker {self.host}:{self.port} 拒绝连接：{reason_code}"
            self._connected.set()
            return
        self._error = ""
        self._connected.set()
        if self._on_state is not None:
            self._on_state(True, "connected")
        # 重连后 broker 不记得旧订阅（clean session），把启动时订过的 topic 再订一遍。
        # 这里在 paho 的网络线程上，用非阻塞的 subscribe，不能调带 SUBACK 等待的那个版本。
        for topic, qos in self._topics:
            self._client.subscribe(topic, qos=qos)

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None):
        self._connected.clear()
        if self._on_state is not None and not self._closing:
            self._on_state(False, str(reason_code))

    def _on_subscribe(self, _client, _userdata, mid, _reason_codes, _properties=None):
        # 只认当前这一次订阅的 mid：上一轮的 SUBACK 晚到不能把这一轮提前放行。
        if mid == self._sub_mid:
            self._subscribed.set()

    def _on_mqtt_message(self, _client, _userdata, message) -> None:
        try:
            self._on_message(message.topic, message.payload)
        except Exception:                     # noqa: BLE001
            # paho 的网络线程里抛出去只会进它自己的日志；后端的 `_apply_set` 已经有
            # 逐条兜底，这里不再让回调把网络线程带停。
            pass

    def subscribe(self, topic: str, qos: int = 1) -> None:
        # 等 SUBACK 再返回：启动后立刻发 `/set` 的客户端不该撞上"订阅还没生效"的窗口。
        self._subscribed.clear()
        result, mid = self._client.subscribe(topic, qos=qos)
        if result != 0:
            raise BackendStartError(f"订阅 {topic} 失败（paho code {result}）")
        self._sub_mid = mid
        if not self._subscribed.wait(3.0):
            raise BackendStartError(f"订阅 {topic} 超时（没收到 SUBACK）")
        self._topics.append((topic, qos))

    def publish(self, topic: str, payload: str, qos: int = 1,
                retain: bool = True) -> None:
        if not self._connected.is_set():
            raise ConnectionError(f"MQTT broker {self.host}:{self.port} 未连接（正在重连）")
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if qos > 0:
            info.wait_for_publish(timeout=3.0)

    def close(self) -> None:
        self._closing = True
        client, self._client = self._client, None
        self._connected.clear()
        if client is not None:
            with contextlib.suppress(Exception):
                client.disconnect()
            with contextlib.suppress(Exception):
                client.loop_stop()


class MqttBackend(BackendBase):
    """一路 MQTT 的模拟后端。`session_factory` 是可注入的会话工厂（测试用假 broker）。"""

    def __init__(self, channel, *, session_factory=None, state_sink=None,
                 **kwargs) -> None:
        super().__init__(channel, **kwargs)
        self._session_factory = session_factory or MqttSession
        # `state_sink(state, detail)`：断线/重连反映到通道状态灯（GUI 注入）。
        self._state_sink = state_sink
        self._session = None
        self._loop = None
        self._values: dict[tuple[int, str, int], object] = {}
        self._by_topic: dict[str, tuple[int, object]] = {}
        self._set_topics: dict[str, tuple[int, object]] = {}
        self._points: dict[tuple[int, str, int], object] = {}
        self._published: dict[str, str] = {}

    # ---------- BackendBase 钩子 ----------

    async def _probe(self) -> None:
        return None                       # broker 是别人的资源，不做本地端口探针

    def _devices(self):
        return None                       # MQTT 没有 SimDevice/寄存器块

    def _create_server(self, _devices):
        self._index_points()
        return self._session_factory(self.channel.host, int(self.channel.port),
                                     self._on_message,
                                     on_state=self._on_connection_state,
                                     will=self._will_config())

    def _start_server(self, server):
        # 连接/订阅是阻塞调用：放进线程里跑，别把引擎 loop 冻住（同 `_probe` 那条口径）。
        return asyncio.create_task(self._open_session(server))

    async def _open_session(self, server) -> None:
        await asyncio.to_thread(server.connect, 5.0)
        for topic in self._set_topics:
            await asyncio.to_thread(server.subscribe, topic, int(self.channel.qos))

    async def _await_ready(self, server, task) -> None:
        await task
        self._loop = asyncio.get_running_loop()
        self._session = server
        self._publish_initial()
        if self.channel.will:
            self._publish_status(True)

    def _make_writer(self, _server):
        return None                       # MQTT 没有 RawRegisterWriter

    async def _shutdown_server(self, server) -> None:
        if self.channel.will:
            try:
                self._publish_status(False)
            except ConnectionError:
                pass                     # 已经断了：broker 会按遗嘱发离线
        server.close()
        self._session = None

    def _require_ready(self) -> None:
        if self._session is None:
            raise RuntimeError(f"channel {self.channel_name} is not started")

    def _write_point(self, key, pt, value) -> bool:
        self._require_ready()
        topic = value_topic(self.channel.topic_prefix, self.channel_name,
                            self._device_name(key), pt.area, pt.address)
        payload = _encode_value(value, self.channel.payload)
        self._values[(key, pt.area, pt.address)] = value
        if self._published.get(topic) == payload:
            return True                   # 值没变：不重复发，避免每 100ms 刷一条
        self._session.publish(topic, payload, qos=int(self.channel.qos),
                              retain=bool(self.channel.retain))
        self._published[topic] = payload
        self._trace("tx", payload.encode("utf-8"), key, f"PUB {topic}")
        return True

    def read_registers(self, unit_id: int, area: str, address: int,
                       count: int) -> list[int] | None:
        """点表"当前值"那一列在这里取：MQTT 的值是发布出去的 typed 值，按点类型编回寄存器。"""
        pt = self._points.get((unit_id, area, address))
        value = self._values.get((unit_id, area, address))
        if pt is None or value is None:
            return None
        registers = encode(_typed_text(value), pt.datatype, pt.word_order)
        return [int(item) & 0xFFFF for item in registers][:max(1, count)]

    def read_bits(self, unit_id: int, area: str, address: int,
                  count: int) -> list[bool] | None:
        value = self._values.get((unit_id, area, address))
        if value is None:
            return None
        return [bool(value)] * max(1, count)

    def describe(self) -> str:
        return (f"{self.channel.host}:{self.channel.port} (mqtt, "
                f"prefix {self.channel.topic_prefix}, qos {self.channel.qos})")

    @property
    def endpoints(self) -> list[tuple[str, int]]:
        return []                         # MQTT 不是监听方

    # ---------- 内部 ----------

    def _index_points(self) -> None:
        self._values.clear()
        self._by_topic.clear()
        self._set_topics.clear()
        self._points.clear()
        for device in self.channel.devices:
            for pt in device.points:
                topic = value_topic(self.channel.topic_prefix, self.channel_name,
                                    device.name, pt.area, pt.address)
                self._by_topic[topic] = (device.unit_id, pt)
                self._points[(device.unit_id, pt.area, pt.address)] = pt
                self._set_topics[set_topic(self.channel.topic_prefix, self.channel_name,
                                          device.name, pt.area, pt.address)] = (device.unit_id, pt)
                try:
                    self._values[(device.unit_id, pt.area, pt.address)] = _typed_initial(pt)
                except ValueError:
                    self._values[(device.unit_id, pt.area, pt.address)] = pt.initial

    def _publish_initial(self) -> None:
        for (unit, area, address), value in list(self._values.items()):
            pt = self._by_topic.get(
                value_topic(self.channel.topic_prefix, self.channel_name,
                            self._device_name(unit), area, address), (None, None))[1]
            if pt is None:
                continue
            self._write_point(unit, pt, value)

    def _device_name(self, unit: int) -> str:
        for device in self.channel.devices:
            if device.unit_id == unit:
                return device.name
        return str(unit)

    def _on_connection_state(self, connected: bool, detail: str) -> None:
        """paho 网络线程回调：日志可以直接写，通道状态必须投回引擎 loop 再发。"""
        now = time.monotonic()
        if connected:
            self._emit("info", "mqtt_connected", (),
                       f"{self.channel_name}: MQTT 已连接 "
                       f"{self.channel.host}:{self.channel.port}", now)
        else:
            self._emit("warning", "mqtt_disconnected", (),
                       f"{self.channel_name}: MQTT 连接断开（{detail}），"
                       f"正在自动重连 {self.channel.host}:{self.channel.port}", now)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._publish_state, connected, detail)

    def _publish_state(self, connected: bool, detail: str) -> None:
        """引擎 loop 上发通道状态：断开=error（红灯），恢复=running（绿灯）。"""
        if self._state_sink is None:
            return
        if connected:
            self._state_sink("running", self.describe())
            if self.channel.will:
                self._publish_status(True)
        else:
            self._state_sink("error", f"MQTT 连接断开（{detail}），正在自动重连")

    def _will_config(self):
        if not self.channel.will:
            return None
        return (status_topic(self.channel.topic_prefix, self.channel_name),
                status_payload(False, self.channel.payload),
                int(self.channel.qos), True)

    def _publish_status(self, online: bool) -> None:
        topic = status_topic(self.channel.topic_prefix, self.channel_name)
        payload = status_payload(online, self.channel.payload)
        self._session.publish(topic, payload, qos=int(self.channel.qos), retain=True)
        self._trace("tx", payload.encode("utf-8"), 0, f"STATUS {topic}")

    def _on_message(self, topic: str, payload) -> None:
        """paho 网络线程 -> 引擎 loop。状态只许在 loop 上改。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._apply_set, topic, payload)

    def _apply_set(self, topic: str, payload) -> None:
        found = self._set_topics.get(topic)
        if found is None:
            return
        unit, pt = found
        try:
            raw = decode_payload(payload)
            text = ("true" if raw else "false") if isinstance(raw, bool) else str(raw)
            registers = encode(text, pt.datatype, pt.word_order)
            value = decode(registers, pt.datatype, pt.word_order)
        except (TypeError, ValueError) as exc:
            self._emit("warning", "mqtt_bad_payload", (unit, pt.area, pt.address),
                       f"{self.channel_name}: {topic} 的 payload 不能转成 "
                       f"{pt.datatype}（{exc}）；已忽略", time.monotonic())
            return
        self._sources.own(self.channel_name, unit, pt.area, pt.address,
                          count=max(1, pt.span), who="mqtt")
        try:
            self._write_point(unit, pt, value)
        except ConnectionError as exc:
            self._emit("warning", "mqtt_publish", (unit, pt.area, pt.address),
                       f"{self.channel_name}: 收到 {topic} 但回发失败：{exc}",
                       time.monotonic())
            return
        self._trace("rx", (payload if isinstance(payload, (bytes, bytearray))
                          else str(payload).encode("utf-8")), unit, f"SET {topic}")

    def _trace(self, direction: str, payload: bytes, unit: int, summary: str) -> None:
        if self._packet_sink is not None:
            self._packet_sink(direction, payload, unit, None, summary,
                              time.monotonic() - self.started_at)


def _encode_value(value, mode: str) -> str:
    """按通道的 payload 形式编码：`json` = `{"value": ...}`，`raw` = 裸标量文本。"""
    return _typed_text(value) if mode == "raw" else encode_payload(value)


def _typed_text(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def _typed_initial(pt) -> object:
    """点表初值 -> 发布用的 Python 值（bool/数字/字符串）。"""
    registers = encode(str(pt.initial), pt.datatype, pt.word_order)
    if area_is_bit(pt.area):
        return bool(registers[0])
    return decode(registers, pt.datatype, pt.word_order)
