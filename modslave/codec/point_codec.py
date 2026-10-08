"""点位值 <-> 原始 reg16 数组。字序置换在这一层，pymodbus 只见到 reg16。"""

from __future__ import annotations

import struct

from ..project.model import (
    BOOL, FLOAT32, FLOAT64, STRING, WORD_ABCD, WORD_ORDERS, parse_value,
    register_span, struct_format, word_orders_allowed,
)

_NUMERIC = ("int16", "uint16", "reg16", "int32", "uint32", "int64", "uint64",
            "float32", "float64")
_FLOATS = (FLOAT32, FLOAT64)


def encode(value, datatype: str, word_order: str = WORD_ABCD) -> list[int]:
    """原生值 -> reg16 列表。位区（bool）走独立路径，不参与字序。"""
    if datatype == BOOL:
        return [1 if parse_value(BOOL, value) else 0]
    if datatype == STRING:
        _check_word_order(datatype, word_order)
        return _encode_string(value)
    if datatype not in _NUMERIC:
        raise ValueError(f"unknown datatype {datatype!r}")
    raw = struct.pack(struct_format(datatype), _coerce(value, datatype))
    return _bytes_to_regs(raw, datatype, word_order)


def decode(registers: list[int], datatype: str, word_order: str = WORD_ABCD):
    """reg16 列表 -> 原生值。"""
    if datatype == BOOL:
        _check_register_count(datatype, registers)
        return bool(registers[0])
    if datatype == STRING:
        _check_word_order(datatype, word_order)
        return _decode_string(registers)
    if datatype not in _NUMERIC:
        raise ValueError(f"unknown datatype {datatype!r}")
    _check_register_count(datatype, registers)
    raw = _regs_to_bytes(list(registers), datatype, word_order)
    value = struct.unpack(struct_format(datatype), raw)[0]
    return float(value) if datatype in _FLOATS else value


def _check_register_count(datatype: str, registers: list[int]) -> None:
    """bool 与数值分支共用同一句长度错误，别让空寄存器数组抛裸 IndexError。"""
    need = register_span(datatype)
    if len(registers) != need:
        raise ValueError(f"{datatype} needs {need} registers, got {len(registers)}")


def _coerce(value, datatype: str):
    """字符串或数字统一成 struct.pack 收得的类型，顺带做范围检查。"""
    if datatype in _FLOATS:
        text = value if isinstance(value, str) else repr(float(value))
        return parse_value(datatype, text)
    text = value if isinstance(value, str) else str(value)
    return parse_value(datatype, text)


def _encode_string(value) -> list[int]:
    if not isinstance(value, str):
        raise ValueError(
            f"string needs a str value, got {type(value).__name__}: {value!r}")
    text = value
    if "\x00" in text:
        raise ValueError("string must not contain nul: it would be truncated on read")
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string must be ascii: {text!r}") from exc
    if len(raw) % 2:
        raw += b"\x00"
    if not raw:                       # 空串也占一个寄存器，与 register_span 一致
        raw = b"\x00\x00"
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def _decode_string(registers: list[int]) -> str:
    raw = b"".join((int(r) & 0xFFFF).to_bytes(2, "big") for r in registers)
    return raw.split(b"\x00", 1)[0].decode("ascii")


def _permute_words(chunks: list[bytes], word_order: str) -> list[bytes]:
    if word_order == "ABCD":
        return chunks
    if word_order == "CDAB":
        return chunks[::-1]                      # 寄存器顺序交换
    if word_order == "BADC":
        return [c[::-1] for c in chunks]          # 寄存器内字节交换
    return [c[::-1] for c in chunks][::-1]        # DCBA：两种交换都做


def _bytes_to_regs(raw: bytes, datatype: str, word_order: str) -> list[int]:
    _check_word_order(datatype, word_order)
    chunks = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    return [int.from_bytes(c, "big") for c in _permute_words(chunks, word_order)]


def _regs_to_bytes(registers: list[int], datatype: str, word_order: str) -> bytes:
    _check_word_order(datatype, word_order)
    chunks = [(int(r) & 0xFFFF).to_bytes(2, "big") for r in registers]
    return b"".join(_permute_words(chunks, word_order))


def _check_word_order(datatype: str, word_order: str) -> None:
    if word_order not in WORD_ORDERS:
        raise ValueError(f"unknown word_order {word_order!r}")
    allowed = word_orders_allowed(datatype)
    if word_order not in allowed:
        raise ValueError(
            f"{datatype} only supports {allowed}, got {word_order!r}")
