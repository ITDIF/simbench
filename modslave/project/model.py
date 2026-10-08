"""点表数据模型。本文件不得 import pymodbus 或 PySide6。"""

from __future__ import annotations

from dataclasses import dataclass, field

BOOL, INT16, UINT16, REG16 = "bool", "int16", "uint16", "reg16"
INT32, UINT32, INT64, UINT64 = "int32", "uint32", "int64", "uint64"
FLOAT32, FLOAT64, STRING = "float32", "float64", "string"

DATATYPES = (BOOL, INT16, UINT16, INT32, UINT32, INT64, UINT64,
             FLOAT32, FLOAT64, STRING, REG16)
AREAS = ("co", "di", "hr", "ir")
BIT_AREAS = ("co", "di")
WAVEFORMS = ("const", "ramp", "sine", "random", "step")

WORD_ABCD, WORD_CDAB, WORD_BADC, WORD_DCBA = "ABCD", "CDAB", "BADC", "DCBA"
WORD_ORDERS = (WORD_ABCD, WORD_CDAB, WORD_BADC, WORD_DCBA)

# struct 大端码 -> 占用寄存器数（bool/string 另外处理）
_STRUCT: dict[str, str] = {
    INT16: "h", UINT16: "H", REG16: "H",
    INT32: "i", UINT32: "I", FLOAT32: "f",
    INT64: "q", UINT64: "Q", FLOAT64: "d",
}
_REG_COUNT: dict[str, int] = {
    INT16: 1, UINT16: 1, REG16: 1,
    INT32: 2, UINT32: 2, FLOAT32: 2,
    INT64: 4, UINT64: 4, FLOAT64: 4,
}
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    INT16: (-32768, 32767), UINT16: (0, 65535), REG16: (0, 65535),
    INT32: (-2**31, 2**31 - 1), UINT32: (0, 2**32 - 1),
    INT64: (-2**63, 2**63 - 1), UINT64: (0, 2**64 - 1),
}
_FLOAT_LIMIT = {FLOAT32: 3.4028234663852886e38, FLOAT64: 1.7976931348623157e308}
# bool 初值只认白名单里的写法，其余（含空串）一律 ValueError：默默当 False 的错值
# 会被当成平台 bug 去调，宁可启动时报错也不许猜
_BOOL_TRUE: set[str] = {"1", "true", "yes", "y", "on"}
_BOOL_FALSE: set[str] = {"0", "false", "no", "n", "off"}

# 用户原值进错误消息要限长：401 位的脏值原样打出来能把整屏刷掉。
_BRIEF_LIMIT = 80


def brief(value, limit: int = _BRIEF_LIMIT) -> str:
    """原值 repr 的截断形式，只砍尾巴：超过 `limit` 个字符取前段并补 `...`。

    放在 model 层是因为截断口径只有一个：codec 与 model 自己抛的消息都会被
    validate.py 一次收集起来打印（Task 5），任何一条不带限长的原值都能把真正的
    错误挤出屏幕。判定口径不受影响——异常类型与消息前缀由调用方保持不变；
    短的（≤limit）原样输出，现场排查主站写来的脏值靠的就是这些原值。
    """
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def area_is_bit(area: str) -> bool:
    """co/di 是位区：一个点位 = 一个线圈/离散输入。"""
    return area in BIT_AREAS


def _ascii_bytes(value: str) -> bytes:
    """string 初值的编码口径：非 ascii 抛 ValueError，别让 UnicodeEncodeError 逃出去。

    validate.py 算重叠那段没有 try，中文初值必须是一个能被收集成校验错误的
    ValueError，而不是一路 traceback 到启动命令。
    """
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string must be ascii: {brief(value)}") from exc


def register_span(datatype: str, value: str = "0") -> int:
    if datatype == BOOL:
        return 1
    if datatype == STRING:
        if not isinstance(value, str):
            raise ValueError(
                f"string needs a str value, got {type(value).__name__}: {brief(value)}")
        return max(1, -(-len(_ascii_bytes(value)) // 2))
    if datatype not in _REG_COUNT:
        raise ValueError(f"unknown datatype {datatype!r}")
    return _REG_COUNT[datatype]


def struct_format(datatype: str) -> str:
    """大端 struct 串，codec 直接用。"""
    if datatype not in _STRUCT:
        raise ValueError(f"unknown datatype {datatype!r}")
    return ">" + _STRUCT[datatype]


def word_orders_allowed(datatype: str) -> tuple[str, ...]:
    if datatype in (INT32, UINT32, FLOAT32):
        return WORD_ORDERS
    if datatype in (INT64, UINT64, FLOAT64):
        return (WORD_ABCD, WORD_DCBA)
    return (WORD_ABCD,)


def parse_value(datatype: str, text: str):
    """字符串 -> 原生 Python 值；类型不对或越界抛 ValueError，消息里带类型名。

    只收字符串（BOOL 额外允许直接传 Python bool）：CSV 和 project.json 里的初值一律是
    字符串，收到 int/float/None 说明调用方传错了类型；截断或 str() 出来的值看着合法，
    静默错值会被当成平台 bug 去调。未知与越界同样报错，不替用户猜。
    """
    if datatype == BOOL and isinstance(text, bool):
        return text
    if not isinstance(text, str):
        raise ValueError(
            f"{datatype} needs a string value, got {type(text).__name__}: {brief(text)}")
    if datatype == BOOL:
        key = text.strip().lower()
        if key in _BOOL_TRUE:
            return True
        if key in _BOOL_FALSE:
            return False
        raise ValueError(
            f"bool needs one of 1/0/true/false/yes/no/on/off, got {brief(text)}")
    if datatype == STRING:
        _ascii_bytes(text)                        # 只为校验，口径与 register_span 一致
        return text
    fmt = struct_format(datatype)
    if fmt.endswith(("f", "d")):
        try:
            num = float(text)
        except ValueError:
            raise ValueError(f"{datatype} needs a number, got {brief(text)}") from None
        if num != num:
            raise ValueError(f"{datatype} rejects nan: {brief(text)}")
        limit = _FLOAT_LIMIT[datatype]
        if abs(num) > limit:                      # inf 溢出后自然落到这条
            raise ValueError(
                f"{datatype} out of range, |value| must be <= {limit}: {brief(text)}")
        return num
    try:
        num = int(text)
    except ValueError:
        raise ValueError(f"{datatype} needs an integer, got {brief(text)}") from None
    lo, hi = _INT_BOUNDS[datatype]
    if not lo <= num <= hi:
        raise ValueError(f"{datatype} out of range [{lo}, {hi}]: {brief(text)}")
    return num


@dataclass
class Point:
    name: str
    area: str
    address: int
    datatype: str
    initial: str = "0"
    word_order: str = WORD_ABCD
    waveform: str = "const"
    wave_param: dict[str, str] = field(default_factory=dict)
    readonly: bool = False
    row: int | None = None       # CSV 行号，只在校验报错时用，不进 project.json

    @property
    def span(self) -> int:
        return 1 if area_is_bit(self.area) else register_span(self.datatype, self.initial)

    def typed_initial(self):
        return parse_value(self.datatype, self.initial)


@dataclass
class Device:
    name: str
    unit_id: int
    points: list[Point] = field(default_factory=list)


@dataclass
class TcpChannel:
    name: str
    host: str = "0.0.0.0"
    port: int = 502
    devices: list[Device] = field(default_factory=list)
    type: str = "tcp"


@dataclass
class Project:
    channels: list[TcpChannel] = field(default_factory=list)
    tick_ms: int = 100

    def iter_points(self):
        for ch in self.channels:
            for dev in ch.devices:
                for pt in dev.points:
                    yield ch, dev, pt

    def find_channel(self, name: str) -> TcpChannel:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(name)
