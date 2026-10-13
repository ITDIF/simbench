"""S7 点位值 <-> 字节序列。与 Modbus 那一层（`point_codec`）并列。

三处与 Modbus 不同的地方，各自都有理由：

1. **地址单位是字节**，不是 16 位寄存器；`bool` 是**一位**，它的那一位由 `Point.bit` 带着
   （`DBX0.3`），`address` 仍是字节偏移——不是"裸位号"。
2. **没有字序**：S7 一律大端，所以这里没有 `word_order` 参数。字序那套（ABCD/CDAB/…）在
   S7 通道上不参与编解码，validate 只给一条"会被忽略"的警告。
3. **类型名是 S7 自己的**（`bool/byte/char/word/int/dword/dint/real/string`）：与 Modbus 的
   `uint16/int32/float32/...` 明确分开，避免"同一个词两条传输两种含义"。

数值类型的范围检查复用 `model.parse_value`（把 S7 名映射回等价的 Modbus 名）——同一套边界
只写一遍；`byte`（无符号 8 位）与 `char` 是 S7 独有，另写。`string` 是**裸 ASCII 定长**
（长度按点表初值算），与"真 S7 STRING"（前两字节是最大长度/当前长度）不同：模拟器对外只
需要"主站读写这一段字节"，带 STRING 头反而会与点表的初值口径打架（spec 里写明这条）。
"""

from __future__ import annotations

import ctypes
import struct

from ..project.model import (
    BOOL, FLOAT32, INT16, INT32, UINT16, UINT32, brief, parse_value, s7_span_bytes,
)
from .waveform import clamp_to_datatype

# S7 类型 -> 等价的 Modbus 类型名：只为了让范围检查复用 `parse_value` 那一份（边界不抄两遍）
_MIRROR = {"word": UINT16, "int": INT16, "dword": UINT32, "dint": INT32,
           "real": FLOAT32, "bool": BOOL}
_FORMAT = {"word": ">H", "int": ">h", "dword": ">I", "dint": ">i", "real": ">f"}
_FIXED = {"byte": 1, "char": 1, "word": 2, "int": 2, "dword": 4, "dint": 4,
          "real": 4, "bool": 1}


def span_bytes(datatype: str, initial: str = "") -> int:
    """这个类型占几个字节（bool 也回 1；string 按初值长度）。与 `model.s7_span_bytes` 同一处真相。"""
    _check_datatype(datatype)
    return s7_span_bytes(datatype, initial)


def clamp_to_type(value, datatype: str):
    """把波形算出来的值钳进这个 S7 类型的区间，返回 `(值, 是否钳过)`。

    与 Modbus 那条 `clamp_to_datatype` 同口径（越界写出去的错值比报一条日志严重得多）：
    数值类型复用那一份边界（镜像回等价的 Modbus 名），`byte` 是 S7 独有（0..255）另写，
    `bool`/`char`/`string` 没有区间。
    """
    _check_datatype(datatype)
    if datatype == BOOL:
        return bool(value), False
    if datatype in ("char", "string"):
        return value, False
    if datatype == "byte":
        if value != value:                       # NaN：钳到下界并报告，别把 NaN 写进区域
            return 0, True
        if value < 0:
            return 0, True
        if value > 255:
            return 255, True
        return int(value), False
    return clamp_to_datatype(value, _MIRROR[datatype])


def encode(value, datatype: str, length: int | None = None) -> bytes:
    """原生值 -> 字节。`length` 只对 string 有意义（缺省按值本身的长度）。"""
    _check_datatype(datatype)
    if datatype == BOOL:
        return b"\x01" if parse_value(BOOL, value) else b"\x00"
    if datatype == "byte":
        return bytes([_uint8(value)])
    if datatype == "char":
        return _char(value)
    if datatype == "string":
        return _string(value, length)
    return struct.pack(_FORMAT[datatype], _coerce(value, datatype))


def decode(data, datatype: str, length: int | None = None):
    """字节 -> 原生值。字符串回 ASCII 文本（以第一个 NUL 为止）。"""
    _check_datatype(datatype)
    raw = _check_data(data, datatype, length)
    if datatype == BOOL:
        return bool(raw[0] & 0x01)
    if datatype == "byte":
        return raw[0]
    if datatype == "char":
        return _single_char(raw[0])
    if datatype == "string":
        return _ascii(raw)
    return struct.unpack(_FORMAT[datatype], raw)[0]


def read_bit(data, bit_index: int) -> bool:
    """取某一位（`bit_index` 是**绝对位号** = 字节偏移*8 + 位，与 `Point.bit` 一起算出来）。"""
    # 位读写面对的是**整块区域缓冲**（长度随点表），不是"一个 bool 的 1 字节"：
    # 所以这里只要求"是字节、非空"，不能套 `_check_data` 那个按类型定长的检查。
    if not _is_buffer(data):
        raise ValueError(f"bits need bytes, got {type(data).__name__}: {brief(data)}")
    raw = bytes(data)
    if not raw:
        raise ValueError("bits need at least 1 byte")
    if not _is_index(bit_index):
        raise ValueError(f"bit index must be an int, got {brief(bit_index)}")
    if not 0 <= bit_index < len(raw) * 8:
        raise ValueError(f"bit index {bit_index} outside 0..{len(raw) * 8 - 1}")
    return bool(raw[bit_index // 8] & (1 << (bit_index % 8)))


def write_bit(buffer, bit_index: int, value) -> None:
    """写某一位，**只动那一位**（邻居位保持不变）。

    `buffer` 收两类可写缓冲：`bytearray`（测试与纯内存）与 **ctypes 数组**——S7 后端的区域
    缓冲就是后者（snap7 的服务端与共享那块内存，改它就是改主站读到的值）。`bytes` 仍然拒：
    不可变，写进去等于静默失败。
    """
    if not _is_writable_buffer(buffer):
        raise ValueError(f"bit write needs a writable buffer (bytearray or ctypes array), "
                         f"got {type(buffer).__name__}")
    if not _is_index(bit_index):
        raise ValueError(f"bit index must be an int, got {brief(bit_index)}")
    if not 0 <= bit_index < len(buffer) * 8:
        raise ValueError(f"bit index {bit_index} outside 0..{len(buffer) * 8 - 1}")
    mask = 1 << (bit_index % 8)
    if parse_value(BOOL, value):
        buffer[bit_index // 8] |= mask
    else:
        buffer[bit_index // 8] &= ~mask & 0xFF


# ---------- 内部 ----------

def _check_datatype(datatype: str) -> None:
    if datatype not in _FIXED and datatype != "string":
        raise ValueError(f"unknown S7 datatype {datatype!r}")


def _check_data(data, datatype: str, length: int | None) -> bytes:
    """decode 入口拒绝脏输入：长度不对就在这儿说清，不许截断或补零凑一个值出来。"""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"{datatype} needs bytes, got {type(data).__name__}: {brief(data)}")
    raw = bytes(data)
    expected = length if (datatype == "string" and length is not None) \
        else _FIXED.get(datatype)
    if expected is not None and len(raw) != expected:
        raise ValueError(f"{datatype} needs {expected} bytes, got {len(raw)}")
    if datatype == "string" and not raw:
        raise ValueError("string needs at least 1 byte")
    return raw


def _is_buffer(data) -> bool:
    """可读的字节缓冲：bytes/bytearray/ctypes 数组（S7 的区域缓冲是最后那种）。"""
    return isinstance(data, (bytes, bytearray, ctypes.Array))


def _is_writable_buffer(buffer) -> bool:
    return isinstance(buffer, (bytearray, ctypes.Array))


def _is_index(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _uint8(value) -> int:
    if isinstance(value, bool):
        raise ValueError(f"byte needs a number, got bool: {brief(value)}")
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise ValueError(f"byte needs an integer, got {brief(value)}") from None
    if not isinstance(value, int):
        raise ValueError(f"byte needs an integer, got {brief(value)}")
    if not 0 <= value <= 255:
        raise ValueError(f"byte out of range [0, 255]: {brief(value)}")
    return value


def _single_char(raw: int) -> str:
    try:
        return bytes([raw]).decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(f"char is not ascii: {brief(raw)}") from None


def _parse_mirrored(value, datatype: str):
    """按镜像的 Modbus 类型做范围检查，但把错误消息里的类型名**换回 S7 的名字**：
    点表写的是 `word`，报出去却是 `uint16 out of range`，现场会去找一个点表里根本没有的类型。
    """
    try:
        return parse_value(_MIRROR[datatype], value)
    except ValueError as exc:
        raise ValueError(str(exc).replace(_MIRROR[datatype], datatype, 1)) from None


def _coerce(value, datatype: str):
    """字符串或数字统一成 struct.pack 收得的类型（与 `point_codec._coerce` 同口径）：
    GUI 那一侧给的是字符串、波形 tick 给的是数字，两条路都得收。"""
    if isinstance(value, bool):
        raise ValueError(f"{datatype} needs a number, got bool: {brief(value)}")
    if datatype == "real":
        text = value if isinstance(value, str) else repr(float(value))
        return _parse_mirrored(text, datatype)
    if isinstance(value, str):
        return _parse_mirrored(value, datatype)
    if not isinstance(value, int):
        raise ValueError(f"{datatype} needs a number, got {brief(value)}")
    return _parse_mirrored(str(value), datatype)


def _char(value) -> bytes:
    if not isinstance(value, str) or len(value) != 1:
        raise ValueError(f"char needs exactly one character, got {brief(value)}")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"char is not ascii: {brief(value)}") from None
    return raw


def _string(value, length: int | None) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"string needs a str value, got {type(value).__name__}: "
                         f"{brief(value)}")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"string is not ascii: {brief(value)}") from None
    if length is not None and len(raw) > length:
        raise ValueError(f"string needs at most {length} byte(s), got {len(raw)}: "
                         f"{brief(value)}")
    if length is None:
        length = max(1, len(raw))
    return raw.ljust(length, b"\x00")


def _ascii(raw: bytes) -> str:
    try:
        return raw.split(b"\x00", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(f"string is not ascii: {brief(raw)}") from None
