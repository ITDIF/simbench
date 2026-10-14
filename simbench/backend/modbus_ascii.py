"""Modbus ASCII 串口从站：帧是**可打印十六进制**（`:` + 十六进制 + CRLF + LRC）。

为什么要它（2026-10-14 E7）：现场还有一批设备/网关说的是 Modbus ASCII（少数，但确实有）。
它跑在串口上，所以串口那一套（串口名、波特率、帧格式、归属探针）与 RTU **完全共用**；
只差帧怎么切、怎么解、以及"主站写了哪个点"要从十六进制里解回来。

帧形状（每一帧）：`:` + `单元号(2 位十六进制)` + `PDU` + `LRC(2 位十六进制)` + CRLF。
PDU 里的字节与 RTU 一模一样，所以"写的是哪个点"复用 `write_targets`——但喂给它的必须是
**二进制**那一份，所以 `_own_write_frame` 在这里覆盖：先解回字节，再交给同一份判定。
"""

from __future__ import annotations

import time

from pymodbus import FramerType

from ..project.model import channel_transport  # noqa: F401  （本模块只做后端，判定在别处）
from .events import PacketEvent
from .modbus_rtu import ModbusRtuBackend
from .ownership import write_targets

ASCII_START = b":"
ASCII_END = b"\r\n"


def ascii_to_binary(frame: bytes) -> bytes | None:
    """一整帧 ASCII 报文 -> `单元号 + PDU + LRC` 的字节；形状不对回 `None`。

    只做转换：LRC 对不对**不在这里判**（pymodbus 自己会拒掉 LRC 错的帧，我们这条只用于
    "主站写了哪个点"的记账；LRC 错了那次写也落不到寄存器上，不记才是对的）。
    """
    body = frame[len(ASCII_START):]
    if not body.endswith(ASCII_END):
        return None                     # 没有结束符 = 还没收全，不是一帧（`split_ascii_frames` 也不产它）
    body = body[:-len(ASCII_END)]
    if not body or len(body) % 2:
        return None
    try:
        return bytes.fromhex(body.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None


def split_ascii_frames(raw: bytes):
    """一次到达的字节块 -> 逐帧产出（`:` 起、CRLF 止）。

    与另外两条传输同一条口径：**切不出来的部分到此为止，不猜**——猜下一帧从哪儿开始会把
    后面的真帧一条条切歪，而切歪的代价是把"主站写的是哪个点"认到别的偏移上。
    """
    offset = 0
    while True:
        start = raw.find(ASCII_START, offset)
        if start < 0:
            return
        end = raw.find(ASCII_END, start)
        if end < 0:
            return                      # 帧还没到齐（重放的那一次会带上余量）
        yield raw[start:end + len(ASCII_END)]
        offset = end + len(ASCII_END)


def parse_ascii_frame(frame: bytes) -> tuple[int | None, int | None, str]:
    """一整帧 ASCII 报文 -> `(unit_id, function_code, 摘要)`：句式与 RTU/TCP 那两版一致。"""
    raw = ascii_to_binary(frame)
    if raw is None or len(raw) < 3:     # 单元号 + fc + LRC 是最短的
        return None, None, f"short ({len(frame)} bytes): {frame[:24].decode('ascii', 'replace')}"
    unit, function = raw[0], raw[1]
    body = raw[2:-1]                    # 去掉单元号与 LRC
    if function & 0x80:
        code = body[0] if body else "?"
        return unit, function & 0x7F, f"unit {unit} fc {function & 0x7F} EXCEPTION {code}"
    return unit, function, f"unit {unit} fc {function} {len(body)}B {body[:8].hex(' ')}"


class ModbusAsciiBackend(ModbusRtuBackend):
    """串口上的 Modbus ASCII 从站（串口那一套全继承，只换帧的切法与解法）。"""

    TRANSPORT = "ascii"
    FRAMER = FramerType.ASCII

    def _parse_frame(self, frame: bytes):
        return parse_ascii_frame(frame)

    def _split_frames(self, raw: bytes):
        return split_ascii_frames(raw)

    def _own_write_frame(self, frame: bytes) -> None:
        """一整帧 rx 报文 -> 命中的点切成 manual。

        ASCII 这一路要先**解回二进制**才能用 `write_targets`（它按 PDU 的字节位置找地址与
        数量）。补两个 0 字节当 CRC 位置：那一步只看 PDU，不看尾巴。
        """
        raw = ascii_to_binary(frame)
        if raw is None or len(raw) < 3:
            return
        unit, function = raw[0], raw[1]
        pdu = raw[1:-1]                 # 去掉单元号与 LRC
        event = PacketEvent(self.channel_name, "rx", bytes([unit, *pdu, 0, 0]), unit,
                            function, "", time.monotonic() - self.started_at,
                            pdu_offset=1)
        for unit_id, area, address, count in write_targets(event) or ():
            self._own_intersecting(unit_id, area, address, count,
                                   span_of=lambda pt: pt.span)
