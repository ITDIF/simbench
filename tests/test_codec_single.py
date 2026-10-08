# tests/test_codec_single.py
import pytest
from modslave.codec.point_codec import decode, encode

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
