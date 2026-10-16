"""引擎往 GUI 抛的事件对象。字段是契约：加字段就加默认值，不要改名。

**为什么在根上**（2026-10-16 从 `backend/events.py` 挪出来）：这几类是**引擎与界面之间的
契约**——产出方是 backend（后端收发包时造 `PacketEvent`、起停时造 `ChannelState`），消费方
是 engine 与 gui。原先住在 backend 包里，代价直接落在架构守卫上：`tests/test_architecture.py`
那条"界面不许 import backend"的线必须为它开一个口子（`gui → backend.events`）。挪到根上
（与 `swallow.py` / `settings.py` 同级）之后没有这条异常，三方都能取，方向上也谁都不欠谁。

**不能挪进 `engine/`**：backend 是这些事件的**产出方**，那样会变成 backend → engine，把依赖
方向弄反。
"""

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
    # 那一路显式给 1（`base._own_write_frame`）。
    pdu_offset: int = 7
    # 进缓冲的顺序号（**由 `PacketStore.add` 发**，不是 backend 自己填的）：报文窗靠它做
    # 差集，只给"新来的那一段"发 `rowsInserted`、只给"被环形缓冲淘汰的那一段"发
    # `rowsRemoved`。没有它就只能整表比内容——缓冲一满，每跳都要重画 5000 行（实测 /
    # 65.6ms 一跳），而且**选中行会跟着内容一起漂**（差的是一行，界面看不出差在哪）。
    seq: int = 0


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
