r"""M5 技术前提探针：真 RTU 从站 + 真 RTU 主站在 socket:// 上跑通（不需要任何硬件）。

跑法：`.venv\Scripts\python.exe -u docs/superpowers/notes/rtu_socket_probe.py`
结论与输出见同目录的 `2026-10-12-m5-rtu-probes.md`。

问六件事（都是 M5 计划 §2 的依据）：
1. 真 ModbusSerialServer（FramerType.RTU）在 `port="socket://host:port"` 上会不会自建 TCP 监听；
2. 真 ModbusSerialClient（同一个 URL）能不能读到值、写到值；
3. 未定义地址 / 未定义 unit id 分别回哪个异常码；
4. `trace_packet` 在 serial server 上生效吗（spec §12 的 M5 待确认项 1）；
5. `trace_connect` 与 `active_connections` 在客户端断开后是什么形状；
6. 未定义 unit 那条 `KeyError: 0` 噪声是不是与 TCP 同形、能不能被既有 filter 压掉。
"""
from __future__ import annotations

import asyncio
import pathlib
import socket
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from pymodbus import FramerType
from pymodbus.client import ModbusSerialClient
from pymodbus.server import ModbusSerialServer

from simbench.backend.modbus_tcp import configure_library_logging
from simbench.backend.simdev import build_sim_devices
from simbench.project.model import Device, Point, Project, TcpChannel

traced: list[bytes] = []
connects: list[bool] = []


def project() -> Project:
    """一条 RTU 通道、一个从站：够覆盖"读值/写字序/写回/异常"四种形状。"""
    return Project(channels=[TcpChannel(name="com-485-1", devices=[
        Device(name="COVI-1", unit_id=11, points=[
            Point("CO浓度", "hr", 0, "float32", initial="12.5", word_order="CDAB"),
            Point("风机状态", "hr", 4, "uint16", initial="11")])])])


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    configure_library_logging()              # 压掉 pymodbus 报文级日志与那条已知噪声
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    port = free_port()

    async def setup():
        server = ModbusSerialServer(
            build_sim_devices(project()), framer=FramerType.RTU,
            port=f"socket://127.0.0.1:{port}",
            baudrate=9600, parity="N", bytesize=8, stopbits=1, timeout=0.3,
            ignore_missing_devices=False,
            trace_packet=lambda sending, data: (traced.append(bytes(data)), data)[1],
            trace_connect=lambda connected: connects.append(bool(connected)))
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.2)
        return server, task

    # 从站住在自己的 loop 线程里（与产品的 EngineRunner 同一个形状）
    server, _task = asyncio.run_coroutine_threadsafe(setup(), loop).result(10)
    print(f"server up on socket://127.0.0.1:{port}", flush=True)

    # 主站在主线程，走 pyserial 的 socket:// 串口对象
    client = ModbusSerialClient(port=f"socket://127.0.0.1:{port}",
                               framer=FramerType.RTU, baudrate=9600, timeout=1.0)
    print("connect ->", client.connect(), flush=True)
    time.sleep(0.1)
    try:
        rsp = client.read_holding_registers(4, count=1, device_id=11)
        print("fc03 hr[4] (uint16 11) ->", getattr(rsp, "registers", None), flush=True)
        rsp = client.read_holding_registers(0, count=2, device_id=11)
        print("fc03 hr[0:2] (float32 CDAB 12.5) ->", getattr(rsp, "registers", None), flush=True)
        rsp = client.read_holding_registers(99, count=1, device_id=11)
        print("fc03 undefined address ->", rsp, flush=True)
        rsp = client.write_register(4, 1234, device_id=11)
        print("fc06 write 1234 ->", rsp, flush=True)
        rsp = client.read_holding_registers(4, count=1, device_id=11)
        print("fc03 read back ->", getattr(rsp, "registers", None), flush=True)
    finally:
        client.close()
        time.sleep(0.1)
        asyncio.run_coroutine_threadsafe(server.shutdown(), loop).result(5)

    print("trace_packet frames:", len(traced), [f.hex(" ") for f in traced], flush=True)
    print("trace_connect:", connects, flush=True)
    print("active_connections after client.close():", dict(server.active_connections), flush=True)
    loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
