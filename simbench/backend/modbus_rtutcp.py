"""RTU over TCP 从站：**TCP 上跑 RTU 帧**（现场那些以太网转串口网关的形状）。

为什么要它（2026-10-14 E7）：网关的串口那一侧是 RTU，网络这一侧是裸 TCP——两端都不知道
"这是串口"，只是把 RTU 字节流原样塞进 TCP。主站侧用 `ModbusTcpClient(framer=RTU)` 连它。

实现上只覆盖三件事（其余全继承 TCP 那一支）：

* `FRAMER = FramerType.RTU`（pymodbus 自己按 RTU 组帧/校验 CRC）；
* 切帧/解帧换成 **RTU 那一份**（同一个帧形状，函数在 `modbus_rtu` 里，E7 提成模块级的）；
* `_pdu_offset = 1`（RTU 帧里 PDU 从单元号之后开始，与 TCP 的 MBAP 头 7 字节不同）。

`check_port_is_free` 那套两级探针照旧适用：载体确实是 TCP。
"""

from __future__ import annotations

from pymodbus import FramerType

from .modbus_rtu import parse_rtu_frame, split_rtu_frames
from .modbus_tcp import ModbusTcpBackend


class RtuOverTcpBackend(ModbusTcpBackend):
    """TCP 载体 + RTU 帧（`ModbusTcpServer(framer=FramerType.RTU)`）。"""

    TRANSPORT = "rtutcp"
    FRAMER = FramerType.RTU
    _pdu_offset = 1                    # RTU 帧：单元号 1 字节之后就是 PDU

    def _parse_frame(self, frame: bytes):
        return parse_rtu_frame(frame)

    def _split_frames(self, raw: bytes):
        return split_rtu_frames(raw)

    def describe(self) -> str:
        return f"{super().describe()} (RTU over TCP)"
