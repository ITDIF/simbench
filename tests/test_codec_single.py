# tests/test_codec_single.py
import pytest
from modslave.codec import point_codec
from modslave.codec.point_codec import decode, encode
from modslave.project.model import BOOL, DATATYPES, STRING

@pytest.mark.parametrize("dt,value,regs", [
    ("int16", -2, [0xFFFE]),
    ("int16", -32768, [0x8000]),
    ("uint16", 65535, [0xFFFF]),
    ("reg16", 4000, [4000]),
])
def test_encode_single(dt, value, regs):
    assert encode(value, dt, "ABCD") == regs

@pytest.mark.parametrize("dt,value,regs", [
    ("int16", -2, [0xFFFE]),
    ("uint16", 65535, [0xFFFF]),
    ("reg16", 4000, [4000]),
])
def test_decode_single(dt, value, regs):
    assert decode(regs, dt, "ABCD") == value

@pytest.mark.parametrize("dt", ["int16", "uint16", "reg16"])
def test_single_roundtrip(dt):
    for value in (0, 1, 2**15 - 1):
        if dt == "int16" or value <= 32767:
            assert decode(encode(value, dt, "ABCD"), dt, "ABCD") == value

def test_out_of_range_single_register_is_rejected():
    with pytest.raises(ValueError, match="reg16"):
        encode(-1, "reg16", "ABCD")
    with pytest.raises(ValueError, match="int16"):
        encode(32768, "int16", "ABCD")

def test_bool_uses_bit_path():
    assert encode(True, "bool", "ABCD") == [1]
    assert encode(False, "bool", "ABCD") == [0]
    assert decode([1], "bool", "ABCD") is True
    assert decode([0], "bool", "ABCD") is False


@pytest.mark.parametrize("dt", [
    dt for dt in DATATYPES if dt not in (BOOL, STRING)])
def test_bool_is_not_a_number_for_every_numeric_datatype(dt):
    """True 不是数值语义：float32 静默编成 1.0 就是在编数据，int16 早就拒了，口径统一成拒。"""
    with pytest.raises(ValueError, match=f"{dt} needs a number, got bool: True"):
        encode(True, dt, "ABCD")
    with pytest.raises(ValueError, match=f"{dt} needs a number, got bool: False"):
        encode(False, dt, "ABCD")


def test_string_keeps_its_own_value_message():
    """string 不接 bool 走的是 _encode_string 的口径，别被“needs a number”串味。"""
    with pytest.raises(ValueError, match=r"string needs a str value, got bool: True"):
        encode(True, "string", "ABCD")


def test_numeric_list_is_derived_from_model_datatypes():
    """_NUMERIC 手抄一份就是第二份真相：model 加类型后 codec 会反过来报 “unknown datatype”。"""
    expected = tuple(d for d in DATATYPES if d not in (BOOL, STRING))
    assert point_codec._NUMERIC == expected
    assert set(point_codec._NUMERIC) == set(DATATYPES) - {BOOL, STRING}


# ---- decode 入口拒脏寄存器（宁可报错，绝不静默给错值）----

@pytest.mark.parametrize("regs", [
    [0x12345],       # 掩码后是 9029：主站越界写不能读成一个合法数
    [-1],            # 掩码后是 65535
    [1.5],           # 截断成 1
    [True],          # bool 不是寄存器
    [None],
    [0, 70000],      # 脏的那一个不在首位也要抓到
    "12",            # 连容器类型都不设防：迭代出来是 '1' '2'
    12,              # 不是 list/tuple
])
def test_decode_rejects_registers_outside_reg16(regs):
    for dt in ("int16", "uint16", "reg16"):
        with pytest.raises(ValueError, match="registers must be ints in 0..65535"):
            decode(regs, dt, "ABCD")


def test_decode_dirty_register_message_shows_the_input():
    """消息要能把脏值原样吐出来，现场排查主站写入才有据可查。"""
    pattern = r"int16 registers must be ints in 0\.\.65535, got \[74565\]"
    with pytest.raises(ValueError, match=pattern):
        decode([0x12345], "int16")


def test_bool_and_string_branches_also_check_registers():
    """三个分支同一拒绝姿态，别只守数值分支。"""
    with pytest.raises(ValueError, match="bool registers must be ints"):
        decode([1.0], "bool")
    with pytest.raises(ValueError, match="string registers must be ints"):
        decode([-1], "string")


@pytest.mark.parametrize("bad_order", ["CDAB", "BADC", "DCBA"])
def test_bool_rejects_word_order_it_does_not_use(bad_order):
    """位区不参与字序不等于可以乱填：string/int16 错填字序立刻报错，bool 不许沉默。"""
    with pytest.raises(ValueError, match="bool only supports"):
        encode(True, "bool", bad_order)
    with pytest.raises(ValueError, match="bool only supports"):
        decode([1], "bool", bad_order)
