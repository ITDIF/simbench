"""引擎往 GUI 抛的事件对象。字段是契约：加字段就加默认值，不要改名。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PacketEvent:
    channel: str
    direction: str          # "rx" | "tx"
    raw: bytes
    unit_id: int | None = None
    function_code: int | None = None
    parsed: str = ""
    mono: float = 0.0
    # PDU 在 `raw` 里的起点（`write_targets` 靠它找地址与数量）：Modbus TCP 是 MBAP 之后的
    # 第 7 字节，RTU 是单元号之后的第 1 字节。默认 7 = 既有 TCP 形状——报文窗那条路
    # （`Session._packet_sink` 造的事件）不带这个信息，也不参与接管判定；串口后端在自己
    # 那一路显式给 1（`modbus_base._own_write_frame`）。
    pdu_offset: int = 7


@dataclass
class ChannelState:
    channel: str
    state: str              # "starting" | "running" | "stopped" | "error"
    detail: str = ""


@dataclass
class LogLine:
    level: str
    text: str


@dataclass
class PointChanged:
    channel: str
    unit_id: int
    area: str
    address: int
    value: object
    registers: list[int] = field(default_factory=list)
