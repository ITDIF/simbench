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
