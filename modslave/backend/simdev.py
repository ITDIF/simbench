"""点表 -> pymodbus SimDevice。这是本工具唯一允许 import pymodbus 的地方之一。"""

from __future__ import annotations

from pymodbus.simulator import DataType, SimData, SimDevice

from ..codec.point_codec import encode
from ..project.model import Point, Project, area_is_bit

_INDEX = {"co": 0, "di": 1, "hr": 2, "ir": 3}
# SimDevice 的元组顺序（与 _INDEX 一致）；空区补占位时要按区选 datatype。
_AREAS = ("co", "di", "hr", "ir")


def area_to_index(area: str) -> int:
    return _INDEX[area]


def placeholder_for(area: str) -> SimData:
    """空区的占位条目（裁决 1 方案 ①）。

    pymodbus 3.15.0 不接受四区里的空 list：`SimDevice.__check_block` 直接
    `x_block[0].address` → IndexError，`SimCore` 建 `SimRuntime` 时 `build_device()`
    里 `__create_block` / `__create_block_bits` 又各取一次 `[0]`。**只有四区都非空的
    设备才能起**是不可接受的——隧道现场最常见的就是"只有保持寄存器"的 COVI /
    照明回路设备。所以空区补一条地址 0 的占位：该区没有任何真实点位，地址 0 必然
    空闲，不会与点冲突，也不会触发重叠异常。

    datatype 分两种，是实测逼出来的（口径原文要求四区同写 INVALID）：

    - 寄存器区（hr/ir）：`DataType.INVALID` → 标志位就是 INVALID，主站读地址 0 得
      异常码 2（未定义地址），与点表空洞同口径。
    - 位区（co/di）：**只能 `DataType.BITS`**。`SimDevice.__check_simple_blocks` 对
      co/di 里的非 BITS 条目直接 `TypeError: ... not DataType.BITS, not allowed`，
      且 `__create_block_bits` 把块首寄存器标志写死成 BITS，于是位区地址 0..15 读回
      False 而不是异常码 2 —— 与寄存器区不对称（tests/test_simdev.py 两条
      `..._at_datastore_layer` 用例钉住，Task 11 用真主站再验一次）。
    """
    if area_is_bit(area):
        return SimData(0, 1, 0, DataType.BITS)
    return SimData(0, 1, 0, DataType.INVALID)


def simdata_for_point(point: Point) -> SimData:
    registers = encode(point.typed_initial(), point.datatype, point.word_order)
    if area_is_bit(point.area):
        return SimData(point.address, 1, [bool(registers[0])], DataType.BITS,
                       readonly=point.readonly)
    return SimData(point.address, 1, [int(r) for r in registers], DataType.REGISTERS,
                   readonly=point.readonly)


def build_sim_devices(project: Project) -> list[SimDevice]:
    """一个 unit id 只能属于一个通道；同 unit 的点位合并进同一个 SimDevice。"""
    owner: dict[int, str] = {}
    by_unit: dict[int, tuple[list, list, list, list]] = {}
    for ch in project.channels:
        for dev in ch.devices:
            unit = dev.unit_id
            if unit in owner and owner[unit] != ch.name:
                raise ValueError(
                    f"unit_id {unit} used by more than one channel "
                    f"({owner[unit]} and {ch.name})")
            owner.setdefault(unit, ch.name)
            blocks = by_unit.setdefault(unit, ([], [], [], []))
            for pt in dev.points:
                blocks[area_to_index(pt.area)].append(simdata_for_point(pt))
    devices = []
    for unit_id in sorted(by_unit):
        blocks = by_unit[unit_id]
        for i, area in enumerate(_AREAS):
            if not blocks[i]:
                blocks[i].append(placeholder_for(area))
            blocks[i].sort(key=lambda e: e.address)
        devices.append(SimDevice(id=unit_id, simdata=tuple(blocks)))
    return devices
