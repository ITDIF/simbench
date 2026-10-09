# tests/test_raw_writer.py
import asyncio

import pytest
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simcore import SimCore
from modslave.backend.raw_writer import RawRegisterWriter


def core():
    hr = [SimData(0, 1, [1, 2], DataType.REGISTERS),
          SimData(20, 1, [9], DataType.REGISTERS, readonly=True)]
    ir = [SimData(0, 1, [7], DataType.REGISTERS, readonly=True)]
    co = [SimData(0, 1, [False] * 8, DataType.BITS)]
    di = [SimData(0, 1, [True] * 4, DataType.BITS)]
    return SimCore([SimDevice(id=11, simdata=(co, di, hr, ir))])


def test_read_defined_registers():
    assert RawRegisterWriter(core()).read_registers(11, "hr", 0, 2) == [1, 2]


def test_read_returns_none_outside_defined_range():
    writer = RawRegisterWriter(core())
    assert writer.read_registers(11, "hr", 5, 1) is None
    assert writer.read_registers(11, "hr", 0, 3) is None      # 跨过块尾


def test_unknown_unit_returns_none_and_bad_area_is_a_programming_error():
    """area 是代码写的，不是数据写的：拼错要炸出来，不能静默返回 None。"""
    writer = RawRegisterWriter(core())
    assert writer.read_registers(77, "hr", 0, 1) is None
    with pytest.raises(ValueError, match="xr"):
        writer.read_registers(11, "xr", 0, 1)


def test_write_visible_to_protocol_read_path():
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_registers(11, "hr", 1, [77]) is True
    assert asyncio.run(store.async_getValues(11, 3, 1, 1)) == [77]


def test_write_into_readonly_block_succeeds_for_engine_but_master_is_refused():
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_registers(11, "ir", 0, [555]) is True
    assert asyncio.run(store.async_getValues(11, 4, 0, 1)) == [555]
    assert asyncio.run(store.async_setValues(11, 4, 0, [999])) == 2   # 主站路径仍是 2
    assert asyncio.run(store.async_getValues(11, 4, 0, 1)) == [555]


def test_write_outside_defined_range_returns_false():
    writer = RawRegisterWriter(core())
    assert writer.write_registers(11, "hr", 9, [1]) is False
    assert writer.write_registers(11, "hr", 1, [1, 2, 3]) is False


def test_values_are_masked_to_16_bits():
    store = core()
    RawRegisterWriter(store).write_registers(11, "hr", 0, [0x1FFFF])
    assert asyncio.run(store.async_getValues(11, 3, 0, 1)) == [0xFFFF]


def test_bit_write_and_read():
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_bits(11, "co", 3, True) is True
    assert writer.read_bits(11, "co", 0, 8) == [False, False, False, True,
                                                False, False, False, False]


def test_bit_write_keeps_siblings():
    store = core()
    writer = RawRegisterWriter(store)
    writer.write_bits(11, "co", 0, True)
    writer.write_bits(11, "co", 3, True)
    writer.write_bits(11, "co", 0, False)
    assert writer.read_bits(11, "co", 0, 8) == [False, False, False, True,
                                                False, False, False, False]


def test_discrete_inputs_are_writable_by_engine():
    writer = RawRegisterWriter(core())
    assert writer.write_bits(11, "di", 1, True) is True
    assert writer.read_bits(11, "di", 0, 4)[1] is True


def test_bit_write_outside_block_returns_false():
    writer = RawRegisterWriter(core())
    assert writer.write_bits(11, "co", 100, True) is False
    assert writer.read_bits(11, "co", 100, 1) is None
