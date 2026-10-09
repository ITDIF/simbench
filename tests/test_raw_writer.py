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


def test_out_of_range_register_write_is_rejected():
    """裁决 2：越界值拒绝、不掩码。

    `& 0xFFFF` 与裁决 J（decode 入口不许静默截断脏寄存器）口径相反：掩码会把
    70000 变成 4464 写进寄存器，主站读到的是一个合法但错误的值。引擎的写入值都来自
    codec，天然落在 0..65535 内，出现越界值只可能是引擎/调用方的 bug —— 正是要当场
    炸出来的那种。
    """
    store = core()
    writer = RawRegisterWriter(store)
    with pytest.raises(ValueError,
                       match=r"hr registers must be ints in 0\.\.65535, got \[70000\]"):
        writer.write_registers(11, "hr", 0, [70000])
    assert asyncio.run(store.async_getValues(11, 3, 0, 1)) == [1]   # 原值没被写进去


def test_bool_and_non_int_register_values_are_rejected():
    """bool 是 int 的子类，`write_bits` 才管位；寄存器列表里出现 bool/str/float 一律炸。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match=r"hr registers must be ints"):
        writer.write_registers(11, "hr", 0, [True])
    with pytest.raises(ValueError, match=r"hr registers must be ints"):
        writer.write_registers(11, "hr", 0, [-1])
    with pytest.raises(ValueError, match=r"ir registers must be ints"):
        writer.write_registers(11, "ir", 0, ["1"])
    with pytest.raises(ValueError, match=r"hr registers must be ints"):
        writer.write_registers(11, "hr", 1, [1.5])
    # 顺序也要钉住：值列表是 bug 时，就算 unit 不存在照样抛，不许被 False 吞掉。
    with pytest.raises(ValueError, match=r"hr registers must be ints"):
        writer.write_registers(77, "hr", 0, [70000])


def test_rejected_write_does_not_touch_any_address():
    """先校验整表再落地：[1, 70000] 不许把地址 0 写进去一半（半写比不写更难查）。"""
    store = core()
    writer = RawRegisterWriter(store)
    with pytest.raises(ValueError):
        writer.write_registers(11, "hr", 0, [1, 70000])
    assert asyncio.run(store.async_getValues(11, 3, 0, 2)) == [1, 2]


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
