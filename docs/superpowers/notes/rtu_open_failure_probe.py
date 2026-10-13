r"""第二个探针：串口打不开时，pymodbus 给的是哪句话？（M5 计划 §2.5 的依据）

跑法：.venv\Scripts\python.exe -u docs/superpowers/notes/rtu_open_failure_probe.py
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from pymodbus import FramerType
from pymodbus.server import ModbusSerialServer

from simbench.backend.simdev import build_sim_devices
from simbench.project.model import Device, Point, Project, TcpChannel

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

project = Project(channels=[TcpChannel(name="com-485-1", devices=[
    Device(name="COVI-1", unit_id=11, points=[Point("x", "hr", 0, "uint16", initial="1")])])])


def direct_pyserial() -> None:
    """先看 pyserial 自己怎么说（后端探针要的就是这句话）。"""
    import serial
    try:
        serial.serial_for_url("COM99", exclusive=True, timeout=0.05)
    except Exception as exc:                     # noqa: BLE001
        print(f"[pyserial] {type(exc).__name__}: {exc}")


async def serve_failure() -> None:
    server = ModbusSerialServer(build_sim_devices(project), framer=FramerType.RTU,
                                port="COM99", baudrate=9600, timeout=0.3)
    try:
        await server.serve_forever()
    except BaseException as exc:                 # noqa: BLE001
        print(f"[pymodbus] serve_forever raised {type(exc).__name__}: {exc}")


direct_pyserial()
asyncio.run(serve_failure())
