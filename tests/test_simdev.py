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


def test_empty_bit_areas_get_placeholder_at_high_address():
    """裁决 1 方案 ②：空区补占位，位区的占位地址抬到 65520。

    上轮口径写的是"寄存器区与位区都是 `SimData(0, 1, 0, DataType.INVALID)`"，**实测
    做不到**：`SimDevice.__check_simple_blocks` 对 co/di 两个 list 里的任何非
    DataType.BITS 条目直接 `TypeError: ... not DataType.BITS, not allowed`。
    所以位区的占位只能是 BITS。但 BITS 占位放在地址 0 等于对外承认"这台设备有
    线圈 0..15"（`__create_block_bits` 把块首寄存器标志写死成 BITS），所以位区的占位
    地址必须抬到 65520（`_BIT_PLACEHOLDER_ADDRESS`），寄存器区的占位仍留在地址 0
    （INVALID 标志本来就让主站收到异常码 2，不需要挪）。
    其余字段（count=1 / values=0）不变；占位的对外效果由下面两条 datastore
    用例钉住。
    """
    proj = _proj(Point("h", "hr", 0, "uint16"))
    blocks = build_sim_devices(proj)[0].simdata
    assert [len(b) for b in blocks] == [1, 1, 1, 1]
    for entry in (blocks[0][0], blocks[1][0]):
        assert (entry.address, entry.count, entry.values) == (65520, 1, 0)
    assert (blocks[3][0].address, blocks[3][0].count, blocks[3][0].values) == (0, 1, 0)
    assert blocks[0][0].datatype is DataType.BITS       # 空 co
    assert blocks[1][0].datatype is DataType.BITS       # 空 di
    assert blocks[3][0].datatype is DataType.INVALID    # 空 ir
    assert blocks[2][0].datatype is DataType.REGISTERS  # hr 里那条是真点位，不是占位


def test_area_with_real_points_gets_no_placeholder():
    """占位只补给"该区一个真点位都没有"的区：co 有真点位时不许多出 65520 那条。

    钉住这一点才有"占位不可能与真点位撞地址"——否则一台线圈铺到 65520 的设备会
    被 `SimDevice.__check_block` 判成 `TypeError: SimData address ... is overlapping!`，
    而那是一条不带点位名的底层异常。空着的寄存器区仍拿地址 0 的 INVALID 占位。
    """
    proj = _proj(Point("启动", "co", 3, "bool", initial="false"),
                 Point("运行", "di", 7, "bool", initial="true"))
    blocks = build_sim_devices(proj)[0].simdata
    assert [e.address for e in blocks[0]] == [3]        # 真 co，无 65520 占位
    assert [e.address for e in blocks[1]] == [7]        # 真 di，无 65520 占位
    assert [e.address for e in blocks[2]] == [0]        # 空 hr -> 寄存器区占位
    assert [e.address for e in blocks[3]] == [0]        # 空 ir -> 寄存器区占位
    assert blocks[2][0].datatype is DataType.INVALID
    assert blocks[0][0].datatype is DataType.BITS


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


def test_empty_bit_area_reads_and_writes_illegal_address_at_datastore_layer():
    """实测：空 co/di 区读地址 0..15 与写线圈 0 都回异常码 2 —— 与寄存器区同口径。

    占位地址从 0 抬到 65520（`_BIT_PLACEHOLDER_ADDRESS`）之后，位区块首落在 4095 号
    寄存器上，`SimRuntime.get_bit_block` 对 address 0 直接算出 `offset < 0` 就回
    ILLEGAL_ADDRESS，读写两条路都一样。抬地址之前这里是 `[False] * 16` + 写成功
    （`async_setValues` 返回 `None`），等于一台压根没有线圈的假从站对"风机启动"回
    ACK 并读回 True——假设备对外撒谎，比显示错值更糟。
    **这是 datastore 层断言**（走的是主站读写路径实际调用的同一套
    `SimCore.async_getValues` / `SimCore.async_setValues`），
    Task 11 落地 TCP backend 后要用真主站 `read_coils(0, count=16)` /
    `write_coil(0, True)` 再验一次。
    """
    proj = _proj(Point("h", "hr", 0, "uint16", initial="7"))
    core = SimCore(build_sim_devices(proj))
    assert asyncio.run(core.async_getValues(11, 1, 0, 16)) == ExcCodes.ILLEGAL_ADDRESS
    assert asyncio.run(core.async_getValues(11, 2, 0, 16)) == ExcCodes.ILLEGAL_ADDRESS
    assert asyncio.run(core.async_setValues(11, 5, 0, [True])) == ExcCodes.ILLEGAL_ADDRESS
    assert asyncio.run(core.async_getValues(11, 1, 0, 1)) == ExcCodes.ILLEGAL_ADDRESS
    assert asyncio.run(core.async_getValues(11, 3, 0, 1)) == [7]      # 真值不受占位影响
    # 位区唯一"已定义"的窗口就是占位那 16 位，块仍只有 2 个寄存器（不会炸内存）
    assert asyncio.run(core.async_getValues(11, 1, 65520, 16)) == [False] * 16
    assert len(core.devices[11].block["c"][2]) == 2


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
