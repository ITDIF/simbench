"""MQTT 真实 broker 端到端：amqtt 在本进程起 broker，跑真实的 paho 客户端。

假会话用例只能验证逻辑接线；这一条验证 paho 的连接、订阅、retain、`/set` 回调与
"模拟后端 + 主站链路"在**真 broker** 上能对上。amqtt 只在开发依赖里，没装就跳过。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid

import pytest

pytest.importorskip("amqtt.broker")
import paho.mqtt.client as mqtt
from amqtt.broker import Broker

from simbench.backend.mqtt_client import MqttBackend
from simbench.master.mqtt import MqttMasterLink
from simbench.project.model import Device, MqttChannel, Point


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _broker_config(port: int) -> dict:
    return {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin":
                    {"allow_anonymous": True}},
    }


@pytest.fixture
def broker():
    port = _free_port()
    ready = threading.Event()
    box: dict = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop = asyncio.Event()
        box["loop"], box["stop"] = loop, stop

        async def main() -> None:
            server = Broker(_broker_config(port))
            await server.start()
            ready.set()
            await stop.wait()
            await server.shutdown()

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    thread = threading.Thread(target=serve, name="amqtt-broker", daemon=True)
    thread.start()
    assert ready.wait(10), "amqtt broker 没起来"
    yield port
    box["loop"].call_soon_threadsafe(box["stop"].set)
    thread.join(8)


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="mqtt-backend-loop",
                              daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def _call(loop, coro, timeout=10):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def test_a_real_broker_round_trip(broker, loop_thread):
    prefix = f"simbench-e2e-{uuid.uuid4().hex[:8]}"
    channel = MqttChannel(
        "mqtt-1", host="127.0.0.1", port=broker, topic_prefix=prefix, qos=1,
        devices=[Device("D1", 1, [Point("温度", "hr", 0, "uint16", initial="25")])])
    backend = MqttBackend(channel)
    _call(loop_thread, backend.start())
    try:
        link = MqttMasterLink(channel, timeout=3.0)
        link.open()
        try:
            assert link.read(1, "hr", 0, 1).values == [25]
        finally:
            link.close()

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"set-{uuid.uuid4().hex[:8]}")
        client.connect("127.0.0.1", broker, 30)
        client.loop_start()
        try:
            info = client.publish(f"{prefix}/mqtt-1/D1/hr/0/set",
                                  '{"value":42}', qos=1)
            info.wait_for_publish(3.0)
        finally:
            client.disconnect()
            client.loop_stop()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if backend.read_registers(1, "hr", 0, 1) == [42]:
                break
            time.sleep(0.05)
        assert backend.read_registers(1, "hr", 0, 1) == [42]

        link = MqttMasterLink(channel, timeout=3.0)
        link.open()
        try:
            assert link.read(1, "hr", 0, 1).values == [42]
        finally:
            link.close()
    finally:
        _call(loop_thread, backend.stop())
        # 清掉 retained，避免影响同 broker 上的后续用例
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect("127.0.0.1", broker, 30)
        client.loop_start()
        client.publish(f"{prefix}/mqtt-1/D1/hr/0", b"", qos=1, retain=True)
        client.disconnect()
        client.loop_stop()
