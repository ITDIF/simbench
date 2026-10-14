"""点表 -> pymodbus SimDevice。这是本工具唯一允许 import pymodbus 的地方之一。"""

from __future__ import annotations

from pymodbus.simulator import DataType, SimData, SimDevice

from ..codec.point_codec import encode
from ..project.model import AREAS, BIT_AREAS, Point, Project, area_is_bit, brief

_INDEX = {"co": 0, "di": 1, "hr": 2, "ir": 3}
if {area: i for i, area in enumerate(AREAS)} != {**_INDEX}:
    # `_INDEX` 的键是第二份区清单、值是 SimDevice 元组的槽位（元组顺序就是 AREAS 顺序）。
    # 加区漏改这里会让新区的点位在 build_sim_devices 里 KeyError 或塞错槽位；下标和键一起
    # 钉，改 AREAS 顺序也拦得住（同裁决 H/M：两份真相一定会漂移，那就导入即炸）。
    raise RuntimeError("_INDEX drifted from model.AREAS")

# 位区占位的地址：抬到高位，避开真设备第一个线圈。pymodbus 的 `__create_block_bits`
# 会把块首寄存器标成 BITS，放在 0 就等于对外承认这台设备有线圈 0..15。
_BIT_PLACEHOLDER_ADDRESS = 65520


def area_to_index(area: str) -> int:
    return _INDEX[area]


def placeholder_for(area: str) -> SimData:
    """空区的占位条目（裁决 1 方案 ②：位区的占位地址抬到 `_BIT_PLACEHOLDER_ADDRESS`）。

    pymodbus 3.15.0 不接受四区里的空 list：`SimDevice.__check_block` 直接
    `x_block[0].address` → IndexError，`SimCore` 建 `SimRuntime` 时 `build_device()`
    里 `__create_block` / `__create_block_bits` 又各取一次 `[0]`。**只有四区都非空的
    设备才能起**是不可接受的——隧道现场最常见的就是"只有保持寄存器"的 COVI /
    照明回路设备。所以空区补一条占位：占位只在**该区没有任何真实点位**时加入，
    因此不会与真点位撞地址（位区那个地址是 65520，寄存器区是 0）。

    两种 datatype 的口径不同，都是实测逼出来的：

    - 寄存器区（hr/ir）：地址 0 + `DataType.INVALID` → 标志位就是 INVALID，主站读
      地址 0 得异常码 2（未定义地址），与点表空洞同口径，所以地址不用挪。
    - 位区（co/di）：**只能 `DataType.BITS`**。`SimDevice.__check_simple_blocks` 对
      co/di 里的非 BITS 条目直接 `TypeError: ... not DataType.BITS, not allowed`，而
      `__create_block_bits` 又把块首寄存器标志写死成 BITS。占位放地址 0 实测的后果
      是：`read_coils(0,16)` 回 16 个 False、`write_coil(0, True)` **回 ACK**
      （`async_setValues` 返回 `None`）且写后能读回 True——风机启动指令打到一台压根
      没有线圈的假从站上，平台收到"成功"。假设备对外撒谎比显示错值更糟，所以位区
      占位抬到 65520：块首落在 4095 号寄存器，`get_bit_block` 对地址 0 算出
      `offset < 0` 直接回异常码 2，读写两条路与寄存器区同口径。块仍只有 2 个寄存器
      （实测 `len(block["c"][2]) == 2`），不会因为高地址而铺出 4095 个寄存器。
      残余代价：位 65520..65535 这一小段对外是"已定义"的，任何真设备的第一线圈都不
      会在那儿，且该区一旦有真点位就不再补占位。

    datastore 层的两条钉桩用例在 `tests/test_simdev.py`（`..._at_datastore_layer`），
    真主站版本在 `tests/test_integration_tcp.py`
    （`test_empty_input_register_area_answers_exception_2_to_a_real_master`、
    `test_empty_bit_area_answers_exception_2_to_a_real_master`）。
    """
    if area_is_bit(area):
        return SimData(_BIT_PLACEHOLDER_ADDRESS, 1, 0, DataType.BITS)
    return SimData(0, 1, 0, DataType.INVALID)


def simdata_for_point(point: Point) -> SimData:
    """一个点位 -> 一条 SimData；area 判不出来就当场问人（裁决 6）。

    实测旧行为：`area="holding"` 走 `area_is_bit` 的 else 分支，**静默按 REGISTERS 建
    块**，一路到 `build_sim_devices` 才在 `area_to_index` 抛 `KeyError`——非领域异常、
    不带点位名，Task 12 的装载口（只 `except ValueError`）会当场 traceback。三分支显式
    判定把口径钉死：位区、寄存器区、未知，各走各的，未知区不许被当成寄存器区上线。
    """
    if point.area not in AREAS:
        raise ValueError(f"unknown area {brief(point.area)} "
                         f"for point {brief(point.name)}")
    registers = encode(point.typed_initial(), point.datatype, point.word_order)
    if point.area in BIT_AREAS:
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
                # 先建 SimData 再定位区：下标表达式 `area_to_index(pt.area)` 要是先求值，
                # 脏 area 就会以 `KeyError` 冒出来（非领域异常、不带点位名，裁决 6）。
                entry = simdata_for_point(pt)
                blocks[area_to_index(pt.area)].append(entry)
    devices = []
    for unit_id in sorted(by_unit):
        blocks = by_unit[unit_id]
        # AREAS 就是 SimDevice 元组的顺序（co/di/hr/ir），不在这里抄第二份清单（裁决 M）
        for i, area in enumerate(AREAS):
            if not blocks[i]:
                blocks[i].append(placeholder_for(area))
            blocks[i].sort(key=lambda e: e.address)
        devices.append(SimDevice(id=unit_id, simdata=tuple(blocks)))
    return devices
