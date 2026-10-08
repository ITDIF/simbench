# tests/test_codec_32bit.py
import pytest
from modslave.codec.point_codec import decode, encode

V = 0x12345678          # A=0x12 B=0x34 C=0x56 D=0x78
EXPECT = {
    "ABCD": [0x1234, 0x5678],
    "CDAB": [0x5678, 0x1234],
    "BADC": [0x3412, 0x7856],
    "DCBA": [0x7856, 0x3412],
}

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_encode_int32_matches_hand_calculated_table(order):
    assert encode(V, "int32", order) == EXPECT[order]

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_decode_int32_matches_hand_calculated_table(order):
    assert decode(EXPECT[order], "int32", order) == V

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_roundtrip_int32_and_uint32(order):
    for value in (0, 1, V, 2**31 - 1):
        assert decode(encode(value, "int32", order), "int32", order) == value
    assert decode(encode(2**32 - 1, "uint32", order), "uint32", order) == 2**32 - 1

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_roundtrip_float32(order):
    got = decode(encode(-12.5, "float32", order), "float32", order)
    assert got == pytest.approx(-12.5, abs=1e-6)

def test_float32_cdab_is_word_swapped():
    """现场最常见的坑：浮点读出来是天价。CDAB 必须能识别。"""
    assert encode(1.0, "float32", "CDAB") == [0x0000, 0x3F80]
    assert encode(1.0, "float32", "ABCD") == [0x3F80, 0x0000]

def test_int32_out_of_range():
    with pytest.raises(ValueError, match="int32"):
        encode(2**31, "int32", "ABCD")

def test_float32_rejects_nan_and_inf():
    import math
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="float32"):
            encode(bad, "float32", "ABCD")

def test_decode_checks_register_count():
    with pytest.raises(ValueError, match="needs 2 registers"):
        decode([1], "int32", "ABCD")

def test_unknown_datatype():
    with pytest.raises(ValueError, match="wchar"):
        encode(1, "wchar", "ABCD")
