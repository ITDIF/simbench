"""点位值 <-> 原始 reg16 数组。字序置换在这一层，pymodbus 只见到 reg16。"""

from __future__ import annotations

import struct

from ..project.model import (
    BOOL, DATATYPES, FLOAT32, FLOAT64, STRING, WORD_ABCD, WORD_ORDERS,
    parse_value, register_span, struct_format, word_orders_allowed,
)

# 类型清单的唯一真相在 model.DATATYPES：手抄一份漂移过一次就会变成 codec 反过来报
# "unknown datatype" 的误导错误（model 加了类型，codec 说不认识）。
# _FLOATS 保持显式：它绑的是 "decode 返回 float" 的语义，不是类型清单。
_NUMERIC = tuple(d for d in DATATYPES if d not in (BOOL, STRING))
_FLOATS = (FLOAT32, FLOAT64)


def encode(value, datatype: str, word_order: str = WORD_ABCD) -> list[int]:
    """原生值 -> reg16 列表。位区（bool）走独立路径，不参与字序。"""
    _check_word_order(datatype, word_order)
    if datatype == BOOL:
        return [1 if parse_value(BOOL, value) else 0]
    if datatype == STRING:
        return _encode_string(value)
    if datatype not in _NUMERIC:
        raise ValueError(f"unknown datatype {datatype!r}")
    raw = struct.pack(struct_format(datatype), _coerce(value, datatype))
    return _bytes_to_regs(raw, datatype, word_order)


def decode(registers: list[int], datatype: str, word_order: str = WORD_ABCD):
    """reg16 列表 -> 原生值。"""
    _check_word_order(datatype, word_order)
    _check_registers(registers, datatype)
    if datatype == BOOL:
        _check_register_count(datatype, registers)
        return bool(registers[0])
    if datatype == STRING:
        return _decode_string(registers)
    if datatype not in _NUMERIC:
        raise ValueError(f"unknown datatype {datatype!r}")
    _check_register_count(datatype, registers)
    raw = _regs_to_bytes(list(registers), datatype, word_order)
    value = struct.unpack(struct_format(datatype), raw)[0]
    return float(value) if datatype in _FLOATS else value


def _check_registers(registers, datatype: str) -> None:
    """decode 入口拒绝脏寄存器：& 0xFFFF 只许用在产出侧，入口掩码等于静默给错值。

    模拟器把 0x12345 截成 0x2345、把 -1 截成 65535 读数，平台侧看是一个合法但错误的
    遥测量，会被当成平台 bug 调试；主站越界写必须由这里抛错暴露出来。
    """
    if not isinstance(registers, (list, tuple)) or any(
            not isinstance(r, int) or isinstance(r, bool) or not 0 <= r <= 0xFFFF
            for r in registers):
        raise ValueError(
            f"{datatype} registers must be ints in 0..65535, got {registers!r}")


def _check_register_count(datatype: str, registers: list[int]) -> None:
    """bool 与数值分支共用同一句长度错误，别让空寄存器数组抛裸 IndexError。"""
    need = register_span(datatype)
    if len(registers) != need:
        raise ValueError(f"{datatype} needs {need} registers, got {len(registers)}")


def _coerce(value, datatype: str):
    """字符串或数字统一成 struct.pack 收得的类型，顺带做范围检查。"""
    if isinstance(value, bool):
        # bool 是 int 的子类：不挡掉，float 分支会把 True 静默编成 1.0（凭空造数），
        # 而 int 分支报的是 got 'True' 这种二次加工过的值——两条路径口径必须先统一成拒
        raise ValueError(f"{datatype} needs a number, got bool: {value!r}")
    if datatype in _FLOATS:
        try:
            text = value if isinstance(value, str) else repr(float(value))
        except (TypeError, OverflowError) as exc:
            # None 走 int 分支是 ValueError，走 float 分支不能变成 traceback：
            # 同一个脏值两条路径口径必须一致，否则 validate 的“一次报全”会被打成崩
            raise ValueError(
                f"{datatype} needs a real number, "
                f"got {type(value).__name__}: {value!r}") from exc
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
    """定长寄存器补零是惯例：尾部补零被吃掉，切片长度由调用方负责。

    空列表单独判非空，不走 _check_register_count：那个函数用 register_span(datatype)
    不传 value，对 string 恒等于 1，会把 3 寄存器的铭牌判成非法。
    """
    if not registers:
        raise ValueError(f"string needs at least 1 registers, got {len(registers)}")
    raw = b"".join((int(r) & 0xFFFF).to_bytes(2, "big") for r in registers)
    try:
        return raw.split(b"\x00", 1)[0].decode("ascii")
    except UnicodeDecodeError as exc:
        # 主站写 GBK/UTF-8 中文是真路径：errors="replace" 会把脏数据读成看着合法的
        # 乱码串，裸 UnicodeDecodeError 又不带 datatype，只能报领域 ValueError
        raise ValueError(f"string is not ascii, got bytes {raw!r}") from exc


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
