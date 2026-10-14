"""扫 unit + 按点表逐点核对（纯逻辑：注入一个"能读"的对象就能测）。

两件事：

* `scan_units(link, units)`：从 1 扫到 247（或调用方给的范围），拿"读一次 `hr 0`"当探针。
  报的是**应答了的** unit —— 不该有、但答了，和"该有、却没答"都只在核对那一步才有意义；
* `verify_points(link, project)`：按点表逐点读回来，与点表**初值**比（初值就是现场的期望值：
  点表是照着设备说明书写的），给出四类结论：

| 状态 | 含义 |
|---|---|
| `ok` | 读回来与点表初值一致 |
| `mismatch` | 读回来了，但与初值不同（**先看这一列**：地址/类型写错的多半在这） |
| `no_unit` | 这个 unit 在读的那一刻没应答（设备没装、unit 填错、总线不通） |
| `error` | 协议层报错（异常码 / 读到一半失败） |

**只读，不写**：核对过程一个寄存器都不改。要改值到界面上去改（那里有归属与跨度判定）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..project.model import Point, Project, area_is_bit, channel_transport, is_s7

# 扫 unit 的默认范围：Modbus 的 unit id 是 1..247（0 是广播、248..255 保留）
UNIT_RANGE = range(1, 248)


@dataclass(frozen=True)
class VerifyRow:
    """一个点位的核对结论。字段就按报告里那几列排。"""

    channel: str
    device: str
    unit_id: int
    name: str
    area: str
    address: int
    datatype: str
    expected: str
    actual: str
    status: str                     # ok / mismatch / no_unit / error
    detail: str = ""


class UnsupportedForMaster(Exception):
    """这一路主站模式做不了（S7 还没做，串口也能做但要有真串口）。"""


def scan_units(link, units: Iterable[int] = UNIT_RANGE,
               progress: Callable[[int, int], None] | None = None) -> list[int]:
    """扫一遍，回**应答了的** unit 清单（升序）。`progress(已扫, 总数)` 给界面报进度。"""
    wanted = list(units)
    found: list[int] = []
    for index, unit_id in enumerate(wanted, 1):
        if link.probe(unit_id).ok:
            found.append(unit_id)
        if progress is not None:
            progress(index, len(wanted))
    return found


def verify_points(link, project: Project,
                  progress: Callable[[VerifyRow, int, int], None] | None = None
                  ) -> list[VerifyRow]:
    """按点表逐点读回来与初值比。`progress(行, 已完成, 总数)` 给界面报进度。"""
    rows: list[VerifyRow] = []
    total = sum(1 for _ in project.iter_points())
    for index, (channel, device, point) in enumerate(project.iter_points(), 1):
        row = _verify_one(link, channel, device, point)
        rows.append(row)
        if progress is not None:
            progress(row, index, total)
    return rows


def _verify_one(link, channel, device, point: Point) -> VerifyRow:
    def make(expected: str, actual: str, status: str, detail: str = "") -> VerifyRow:
        return VerifyRow(channel.name, device.name, device.unit_id, point.name,
                         point.area, point.address, point.datatype, expected, actual,
                         status, detail)

    try:
        transport = channel_transport(channel)
    except Exception as exc:                       # noqa: BLE001
        return make(str(point.initial), "-", "error", f"{type(exc).__name__}: {exc}")
    if is_s7(transport):
        return make(str(point.initial), "-", "error",
                    "S7 通道的主站模式还没做（本轮只支持 Modbus 那五种传输）")

    count = 1 if area_is_bit(point.area) else max(1, point.span)
    result = link.read(device.unit_id, point.area, point.address, count)
    if not result.ok:
        status = "error" if "exception" in result.detail else "no_unit"
        return make(str(point.initial), "-", status, result.detail)

    expected_registers = _expected_registers(point)
    if expected_registers is None:
        # 初值自己就编不出来（点表里有错）：那一次核对没有意义，如实说
        return make(str(point.initial), _hex(result.values), "error",
                    "点表里的初值/类型算不出寄存器（先修点表）")
    if result.values == expected_registers:
        return make(str(point.initial), _text(result.values, point), "ok")
    return make(str(point.initial), _text(result.values, point), "mismatch",
                f"期望 {expected_registers}，读到 {result.values}")


def _expected_registers(point: Point) -> list[int] | None:
    """把点表里的初值编成寄存器：编不出来（初值非法）回 `None`。"""
    from ..codec.point_codec import encode

    try:
        values = encode(str(point.initial), point.datatype, point.word_order)
    except ValueError:
        return None
    if area_is_bit(point.area):
        return [1 if value else 0 for value in values]
    return [int(value) & 0xFFFF for value in values]


def _text(values: list[int], point: Point) -> str:
    """读回来的值 -> 人看的字（按点位类型解回去；解不出来就原样给寄存器）。"""
    from ..codec.point_codec import decode

    if area_is_bit(point.area):
        return "true" if values and values[0] else "false"
    try:
        return str(decode(values, point.datatype, point.word_order))
    except ValueError:
        return _hex(values)


def _hex(values: list[int]) -> str:
    return " ".join(f"{value:04X}" for value in values or [])
