# tests/test_raw_writer.py
import asyncio

import pytest
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simcore import SimCore

from simbench.backend.raw_writer import RawRegisterWriter


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


# ---- 裁决 4：位值必须是真 bool（与 write_registers 的严格口径对称）----

@pytest.mark.parametrize("value", ["0", "1", "no", "yes", "TURE", None, [], {}, "",
                                    0, 1, 1.0])
def test_write_bits_only_accepts_a_python_bool(value):
    """实测：`write_bits(11,'co',0,"0")` 把线圈置 True，`"no"` 同样置 True。

    `"0"`/`"no"` 是非空字符串恒真，`None`/`[]`/`""` 恒假——未识别值静默给一个 bool 正是
    裁决 A 禁的那种事：引擎写下去的位和它想写的相反，主站读回来的是一个"合法但错误"的
    开关量，现场只会当成平台 bug 调。不许改成 `bool(value)`，要当场炸。
    """
    store = core()
    writer = RawRegisterWriter(store)
    with pytest.raises(ValueError) as excinfo:
        writer.write_bits(11, "co", 0, value)
    assert str(excinfo.value) == f"co bit value must be a bool, got {value!r}"
    # 抛错不许留下半个写入：地址 0..7 仍是初值 False
    assert writer.read_bits(11, "co", 0, 8) == [False] * 8


@pytest.mark.parametrize("value", [True, False])
def test_write_bits_still_accepts_real_bools(value):
    """严格 gate 不许误杀：真 bool 照旧能写，且写后读回同一个 bool。"""
    writer = RawRegisterWriter(core())
    assert writer.write_bits(11, "co", 3, value) is True
    assert writer.read_bits(11, "co", 3, 1) == [value]


# ---- 裁决 5：容器 gate 与空值列表，句式与 codec 的 `_check_registers` 一致 ----

@pytest.mark.parametrize("values", [5, "x", None, {}, {1, 2}, 1.5])
def test_write_registers_rejects_non_container_values(values):
    """实测逃逸/静默两种都有：`write_registers(11,'hr',0,5)` 抛 TypeError，
    `set()`/`{}`/`()` 静默返回 False。判据与 codec 同口径：不是 list/tuple 就 ValueError。
    """
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError) as excinfo:
        writer.write_registers(11, "hr", 0, values)
    assert str(excinfo.value) == \
        f"hr registers must be ints in 0..65535, got {values!r}"


def test_write_registers_accepts_a_tuple_of_ints():
    """codec 的容器 gate 收 list 与 tuple，这里必须同宽，否则引擎传 tuple 就自家人挨炸。"""
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_registers(11, "hr", 0, (7, 8)) is True
    assert asyncio.run(store.async_getValues(11, 3, 0, 2)) == [7, 8]


def test_write_registers_with_empty_list_is_rejected():
    """空值列表以前静默返回 False：Task 11 的批量写会把"根本没写"当成一次普通失败。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match="empty value list"):
        writer.write_registers(11, "hr", 0, [])
    with pytest.raises(ValueError, match="empty value list"):
        writer.write_registers(11, "hr", 0, ())


# ---- 裁决 5 的同一判据补到整数参数：address / bit_address / count ----

@pytest.mark.parametrize("address", [None, "0", 1.5, True, [0]])
def test_write_registers_rejects_non_int_address(address):
    """实测 `write_registers(11,'hr',None,[1])` 抛 TypeError 逃逸；`True` 更糟——
    `bool` 是 int 的子类，地址被静默当成 1，写进隔壁那个寄存器。

    引擎传进来的地址是算出来的（`base + offset`），算错一步就是脏值；判据与值列表
    同口径（非 int / bool 混入 → ValueError），消息点名是哪个参数。
    """
    store = core()
    writer = RawRegisterWriter(store)
    with pytest.raises(ValueError) as excinfo:
        writer.write_registers(11, "hr", address, [1])
    assert str(excinfo.value) == f"address must be an int, got {address!r}"
    assert asyncio.run(store.async_getValues(11, 3, 0, 2)) == [1, 2]   # 一个字节都没动


@pytest.mark.parametrize("count", [None, "2", 1.5, True, [1]])
def test_read_registers_rejects_non_int_count(count):
    """`count=True` 实测回 `[1]`（当成 1 个寄存器），脏 count 读到的是错位的数据。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError) as excinfo:
        writer.read_registers(11, "hr", 0, count)
    assert str(excinfo.value) == f"count must be an int, got {count!r}"


@pytest.mark.parametrize("address", [None, "0", 1.5, True, [0]])
def test_read_registers_rejects_non_int_address(address):
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError) as excinfo:
        writer.read_registers(11, "hr", address, 1)
    assert str(excinfo.value) == f"address must be an int, got {address!r}"


@pytest.mark.parametrize("value", [None, "0", 1.5, True, [0]])
def test_write_bits_rejects_non_int_bit_address(value):
    """位号同理：`write_bits(11,'co',None,True)` 实测 `TypeError: unsupported operand
    type(s) for //`；`bit_address=True` 会静默把线圈 1 点亮。

    `True` 在这里走的是地址参数（不是位值），所以照样按整数判据拒。
    """
    store = core()
    writer = RawRegisterWriter(store)
    with pytest.raises(ValueError) as excinfo:
        writer.write_bits(11, "co", value, True)
    assert str(excinfo.value) == f"bit_address must be an int, got {value!r}"
    assert writer.read_bits(11, "co", 0, 8) == [False] * 8


@pytest.mark.parametrize("count", [None, "2", 1.5, True, [1]])
def test_read_bits_rejects_non_int_count(count):
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError) as excinfo:
        writer.read_bits(11, "co", 0, count)
    assert str(excinfo.value) == f"count must be an int, got {count!r}"


@pytest.mark.parametrize("address", [None, "0", 1.5, True, [0]])
def test_read_bits_rejects_non_int_bit_address(address):
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError) as excinfo:
        writer.read_bits(11, "co", address, 1)
    assert str(excinfo.value) == f"bit_address must be an int, got {address!r}"


def test_programming_errors_still_beat_the_unknown_unit_shortcut():
    """脏参数是 bug，unit 不存在是运行期状态：前者必须抛，不许被 `return None/False` 吞。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match="address must be an int"):
        writer.read_registers(77, "hr", None, 1)
    with pytest.raises(ValueError, match="address must be an int"):
        writer.write_registers(77, "hr", None, [1])


def test_clean_integers_still_work_after_the_gate():
    """gate 不许误杀：真 int（含负数与 0）走原来的判定，不新增异常。"""
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_registers(11, "hr", 1, [77]) is True
    assert writer.read_registers(11, "hr", 1, 1) == [77]
    assert writer.read_registers(11, "hr", -1, 1) is None       # 越界仍是 None
    assert writer.write_registers(11, "hr", -1, [1]) is False   # 越界仍是 False
    assert writer.write_bits(11, "co", 0, True) is True


@pytest.mark.parametrize("area", ["co", "di"])
def test_register_api_refuses_bit_areas(area):
    """同一个"地址 0"在两条路径下含义不同，所以不许混用。

    实测混用的后果：`write_registers(11,'co',0,[0x0002])` 返回 True，点亮的是
    **线圈 1**（寄存器 0 的第 1 位），而 `write_bits(11,'co',0,...)` 的 0 才是
    主站眼里的线圈 0。M1 没有引擎调用所以没爆，M4 一旦按点位地址调 write_registers
    就是静默错值（想关风机、写成开）。
    """
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match=f"{area} is a bit area; use write_bits"):
        writer.write_registers(11, area, 0, [1])
    with pytest.raises(ValueError, match=f"{area} is a bit area; use read_bits"):
        writer.read_registers(11, area, 0, 1)


def test_bit_api_still_works_after_the_register_gate():
    writer = RawRegisterWriter(core())
    assert writer.write_bits(11, "co", 2, True) is True
    assert writer.read_bits(11, "co", 0, 4)[2] is True
    assert writer.read_bits(11, "di", 0, 4) == [True, True, True, True]


def test_register_api_still_works_for_register_areas():
    writer = RawRegisterWriter(core())
    assert writer.write_registers(11, "hr", 0, [5, 6]) is True
    assert writer.read_registers(11, "hr", 0, 2) == [5, 6]


def test_area_gate_beats_the_unknown_unit_shortcut():
    """编程错误优先于运行期数据错误：unit 不存在时也不许把位区静默回 False。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match="bit area"):
        writer.write_registers(77, "co", 0, [1])


@pytest.mark.parametrize("area", ["hr", "ir"])
def test_bit_api_refuses_register_areas(area):
    """镜像方向同样不许混用：拿位接口写 hr，"地址 0"会变成寄存器 0 的第 0 位。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match=f"{area} is a register area; use write_registers"):
        writer.write_bits(11, area, 0, True)
    with pytest.raises(ValueError, match=f"{area} is a register area; use read_registers"):
        writer.read_bits(11, area, 0, 1)


def test_unknown_area_still_reports_unknown_area_not_area_kind():
    """拼错的区名要报"不认识这个区"，不能被类别门控抢先说成 register area。"""
    writer = RawRegisterWriter(core())
    with pytest.raises(ValueError, match="unknown area"):
        writer.write_bits(11, "xr", 0, True)
    with pytest.raises(ValueError, match="unknown area"):
        writer.read_registers(11, "xr", 0, 1)
