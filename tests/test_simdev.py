# tests/test_simdev.py
import asyncio

import pytest
from pymodbus.constants import ExcCodes
from pymodbus.simulator import DataType, SimDevice
from pymodbus.simulator.simcore import SimCore
from modslave.backend.simdev import area_to_index, build_sim_devices, simdata_for_point
from modslave.codec.point_codec import encode
from modslave.project.model import Device, Point, Project, TcpChannel


def _proj(*points, unit=11, name="c1"):
    dev = Device(name="D1", unit_id=unit, points=list(points))
    return Project(channels=[TcpChannel(name=name, host="127.0.0.1", port=502,
                                        devices=[dev])])


def test_area_index_is_tuple_order():
    assert (area_to_index("co"), area_to_index("di"),
            area_to_index("hr"), area_to_index("ir")) == (0, 1, 2, 3)


def test_hr_simdata_carries_encoded_initial_and_count_is_one():
    pt = Point("CO", "hr", 0, "float32", initial="12.5", word_order="CDAB")
    entry = simdata_for_point(pt)
    assert entry.count == 1
    assert entry.datatype is DataType.REGISTERS
    assert entry.values == encode(12.5, "float32", "CDAB") == [0x0000, 0x4148]


def test_bit_point_uses_bits_datatype_and_one_value():
    pt = Point("启动", "co", 3, "bool", initial="true")
    entry = simdata_for_point(pt)
    assert entry.datatype is DataType.BITS
    assert entry.values == [True] and entry.address == 3


def test_readonly_flag_is_passed_through():
    pt = Point("版本", "ir", 0, "uint16", initial="7", readonly=True)
    assert simdata_for_point(pt).readonly is True


def test_device_is_built_as_four_blocks():
    proj = _proj(Point("h", "hr", 0, "uint16", initial="1"),
                 Point("i", "ir", 0, "uint16", initial="2"),
                 Point("c", "co", 0, "bool", initial="false"),
                 Point("d", "di", 0, "bool", initial="true"))
    device = build_sim_devices(proj)[0]
    assert isinstance(device, SimDevice) and device.id == 11
    assert isinstance(device.simdata, tuple) and len(device.simdata) == 4
    assert device.simdata[0][0].datatype is DataType.BITS
    assert device.simdata[2][0].values == [1]
    assert device.simdata[3][0].values == [2]


def test_same_block_entries_are_sorted_by_address():
    proj = _proj(Point("b", "hr", 10, "uint16", initial="1"),
                 Point("a", "hr", 0, "uint16", initial="2"))
    entries = build_sim_devices(proj)[0].simdata[2]
    assert [e.address for e in entries] == [0, 10]


def test_empty_areas_get_one_placeholder_at_address_zero():
    """裁决 1 方案 ①：空区补一条地址 0 的占位，pymodbus 才肯建设备。

    上轮口径写的是"寄存器区与位区都是 `SimData(0, 1, 0, DataType.INVALID)`"，**实测
    做不到**：`SimDevice.__check_simple_blocks` 对 co/di 两个 list 里的任何非
    DataType.BITS 条目直接 `TypeError: ... not DataType.BITS, not allowed`。
    所以位区的占位只能是 BITS（其余字段 address=0 / count=1 / values=0 不变），
    读回来的不对称行为由下面两条 datastore 用例钉住。
    """
    proj = _proj(Point("h", "hr", 0, "uint16"))
    blocks = build_sim_devices(proj)[0].simdata
    assert [len(b) for b in blocks] == [1, 1, 1, 1]
    for entry in (blocks[0][0], blocks[1][0], blocks[3][0]):
        assert (entry.address, entry.count, entry.values) == (0, 1, 0)
    assert blocks[0][0].datatype is DataType.BITS       # 空 co
    assert blocks[1][0].datatype is DataType.BITS       # 空 di
    assert blocks[3][0].datatype is DataType.INVALID    # 空 ir
    assert blocks[2][0].datatype is DataType.REGISTERS  # hr 里那条是真点位，不是占位


def test_register_only_device_is_accepted_by_sim_core():
    """隧道现场最常见的 COVI / 照明回路设备只有保持寄存器，占位让它能起。"""
    proj = _proj(Point("温度", "hr", 0, "float32", initial="12.5"))
    core = SimCore(build_sim_devices(proj))
    assert core.device_ids() == [11]
    assert asyncio.run(core.async_getValues(11, 3, 0, 2)) == encode(12.5, "float32", "ABCD")


def test_bit_only_device_is_accepted_by_sim_core():
    """对称的那一半：只有线圈的设备（空 hr/ir 拿 INVALID 占位）也能起。"""
    proj = _proj(Point("启动", "co", 5, "bool", initial="true"))
    core = SimCore(build_sim_devices(proj))
    assert asyncio.run(core.async_getValues(11, 1, 5, 1)) == [True]
    assert asyncio.run(core.async_getValues(11, 3, 0, 1)) == ExcCodes.ILLEGAL_ADDRESS


def test_empty_register_area_reads_illegal_address_at_datastore_layer():
    """空 hr/ir 区读地址 0 的实测结果：异常码 2（ExcCodes.ILLEGAL_ADDRESS）。

    占位条目把地址 0 标成 DataType.INVALID，`SimRuntime.__check_block` 见到
    `flags[0] & RunTimeFlag_TYPE == INVALID` 就回 ILLEGAL_ADDRESS，主站侧就是异常码 2。
    **这是 datastore 层断言**（走的是主站读路径实际调用的同一套
    `SimCore.async_getValues` / `SimRuntime.async_getValues`），
    Task 11 落地 TCP backend 后要用真主站 `read_input_registers(0)` 再验一次。
    """
    proj = _proj(Point("h", "hr", 0, "uint16", initial="7"))
    core = SimCore(build_sim_devices(proj))
    assert asyncio.run(core.async_getValues(11, 4, 0, 1)) == ExcCodes.ILLEGAL_ADDRESS
    assert asyncio.run(core.async_getValues(11, 4, 0, 2)) == ExcCodes.ILLEGAL_ADDRESS
    assert asyncio.run(core.async_getValues(11, 3, 0, 1)) == [7]  # 真点位不受影响


def test_empty_bit_area_reads_false_not_illegal_at_datastore_layer():
    """实测：空 co/di 区读地址 0 回 [False]*16，**不是**异常码 2 —— 与寄存器区不对称。

    原因有两条，都是 pymodbus 3.15.0 的既有实现：
      1. 位区里不许出现非 DataType.BITS 的条目（`__check_simple_blocks` 直接 TypeError），
         所以占位没法像寄存器区那样标成 INVALID；
      2. `__create_block_bits` 把块首寄存器的标志写死成 `DataType.BITS`
         （`flags = [BITS] + [0]*n + [INVALID]`），占位因此是一块"已定义的 16 位"。
    于是位区地址 0..15 读回 False、位 16 起才回到异常码 2（下面第三条断言）。
    **这条不对称会决定 Task 11 那条"空洞"用例怎么写**，且这里是 datastore 层断言，
    Task 11 要用真主站 `read_coils(0, count=16)` / `read_discrete_inputs(0, count=16)`
    再验一次。
    """
    proj = _proj(Point("h", "hr", 0, "uint16", initial="7"))
    core = SimCore(build_sim_devices(proj))
    assert asyncio.run(core.async_getValues(11, 1, 0, 16)) == [False] * 16  # 空 co
    assert asyncio.run(core.async_getValues(11, 2, 0, 16)) == [False] * 16  # 空 di
    assert asyncio.run(core.async_getValues(11, 1, 16, 1)) == ExcCodes.ILLEGAL_ADDRESS


def test_multiple_units_and_channels():
    p1 = _proj(Point("a", "hr", 0, "uint16", initial="1"), unit=11)
    p2 = _proj(Point("b", "hr", 0, "uint16", initial="2"), unit=12, name="c2")
    proj = Project(channels=[p1.channels[0], p2.channels[0]])
    devices = build_sim_devices(proj)
    assert [d.id for d in devices] == [11, 12]


def test_no_id_zero_device():
    """未定义的 unit id 必须不应答，所以绝不建 id=0 兜底设备。"""
    devices = build_sim_devices(_proj(Point("a", "hr", 0, "uint16")))
    assert 0 not in [d.id for d in devices]


def test_string_point_span_is_respected():
    """FAN-01 的 ASCII 是 46 41 4e 2d 30 31，两字节大端拼 reg。"""
    pt = Point("铭牌", "hr", 20, "string", initial="FAN-01")
    assert simdata_for_point(pt).values == [0x4641, 0x4e2d, 0x3031]


def test_same_unit_id_across_channels_is_rejected():
    p1 = _proj(Point("a", "hr", 0, "uint16"), unit=11, name="c1")
    p2 = _proj(Point("b", "hr", 0, "uint16"), unit=11, name="c2")
    proj = Project(channels=[p1.channels[0], p2.channels[0]])
    with pytest.raises(ValueError, match="unit_id 11"):
        build_sim_devices(proj)
