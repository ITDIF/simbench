"""点位值的渲染与通道后端的小工具（纯函数，无状态）。

_render 把寄存器/字节还原成界面文案；_span_of 与 _detail 是读写两侧共用的小判定。
从 session.py 拆出，让读侧与写侧都能直接用，而不必互相 import。
"""

from __future__ import annotations

from ..codec import s7_codec
from ..codec.point_codec import decode
from ..project.model import Point, is_s7, s7_span_bytes


def _render(pt: Point, regs: list[int] | None,
            transport: str = "tcp") -> tuple[str, tuple[int, ...]]:
    """寄存器 -> 界面文案。回读失败（regs 为 None/空）时回落 CSV 初值。

    回落本身有两种含义——①通道没起，本来就该显示初值；②通道在跑但读不到，显示初值
    就是演给界面一个"设备报了这个值"。这一层分不出来（它拿不到通道状态），所以
    `rows()` 用 `_read_back` 的 `failed` 补 `Row.stale` 把两者分开；这里不许改成
    显示 0 或空串（那等于用一个假的 0 替掉"没读到"）。

    寄存器一律回 `tuple`（A–I 表 G）：`Row.registers` 与 `WriteResult.registers`
    共用这一份产出，两种容器并存会让上层的"这行变了没有"判等时好时坏。
    """
    if not regs:
        return pt.initial, ()
    try:
        if is_s7(transport):
            # `regs` 在 S7 这一路装的是**字节**（`_read_back` 的约定），按 S7 类型解
            value = s7_codec.decode(bytes(regs), pt.datatype,
                                    s7_span_bytes(pt.datatype, pt.initial))
        else:
            value = decode(list(regs), pt.datatype, pt.word_order)
    except ValueError:
        return pt.initial, tuple(regs)
    if isinstance(value, bool):
        return ("true" if value else "false"), tuple(regs)
    if isinstance(value, float):
        return (f"{value:g}", tuple(regs))
    return (str(value), tuple(regs))


def _span_of(pt: Point, transport: str) -> int:
    """这个点占多大：Modbus 是寄存器格数，S7 是字节数（归属与交还都用它）。"""
    return s7_span_bytes(pt.datatype, pt.initial) if is_s7(transport) else pt.span


def _detail(backend) -> str:
    """通道状态那一栏显示的"主站该连哪"：由后端自己说，因为两条传输的答案是两种东西。

    TCP 是"所有监听地址、多了截断"，串口是那一串串口参数（`COM3 19200 8E1`）——判定住在
    `BackendBase.describe()` 里（子类覆盖），这一层只转发，免得"界面的说法"和
    "后端的说法"各长一套。TCP 那个格式不能动：界面与用例按 `rpartition(":")` 取端口。
    """
    return backend.describe()
