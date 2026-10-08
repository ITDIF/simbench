# tests/test_codec_64bit.py
import pytest
from modslave.codec.point_codec import decode, encode

V64 = 0x0102030405060708

def test_uint64_abcd_is_plain_big_endian():
    assert encode(V64, "uint64", "ABCD") == [0x0102, 0x0304, 0x0506, 0x0708]

def test_uint64_dcba_is_full_byte_reverse():
    """DCBA = 8 字节整体反转后切 reg，不是字序反转（那等同 CDAB）。

    0x0102030405060708 -> 08 07 06 05 04 03 02 01 -> [0x0807,0x0605,0x0403,0x0201]。
    实现走同一个 `_permute_words`，64 位没有特殊分支，spec §6 与计划 Step 3 同口径。
    """
    assert encode(V64, "uint64", "DCBA") == [0x0807, 0x0605, 0x0403, 0x0201]

@pytest.mark.parametrize("order", ["ABCD", "DCBA"])
def test_uint64_roundtrip(order):
    for value in (0, 1, V64, 2**64 - 1):
        assert decode(encode(value, "uint64", order), "uint64", order) == value

@pytest.mark.parametrize("order", ["ABCD", "DCBA"])
def test_int64_roundtrip_including_negative_min(order):
    for value in (0, -1, 2**63 - 1, -(2**63)):
        assert decode(encode(value, "int64", order), "int64", order) == value

@pytest.mark.parametrize("order", ["ABCD", "DCBA"])
def test_float64_roundtrip(order):
    assert decode(encode(1.2345678901234, "float64", order), "float64", order) \
        == pytest.approx(1.2345678901234)

def test_64bit_rejects_cdab_and_badc():
    """业界对 64 位的 CDAB/BADC 没有公认定义，我们不自发明。"""
    for bad in ("CDAB", "BADC"):
        with pytest.raises(ValueError, match="ABCD.*DCBA"):
            encode(1, "int64", bad)
        with pytest.raises(ValueError, match="ABCD.*DCBA"):
            decode([0, 0, 0, 0], "int64", bad)

def test_64bit_needs_four_registers():
    with pytest.raises(ValueError, match="needs 4 registers"):
        decode([1, 2], "uint64", "ABCD")


# ---- string ----

def test_string_abcd_pads_to_even_bytes():
    assert encode("abc", "string") == [0x6162, 0x6300]

def test_string_two_bytes_per_register_big_endian():
    assert encode("AB", "string") == [0x4142]

@pytest.mark.parametrize("text", ["A", "AB", "ABC", "ABCDE", "FAN-01", ""])
def test_string_roundtrip_strips_padding(text):
    regs = encode(text, "string")
    assert len(regs) == max(1, -(-len(text) // 2))
    assert decode(regs, "string") == text

def test_string_rejects_non_ascii():
    with pytest.raises(ValueError, match="ascii"):
        encode("中", "string")

def test_string_only_supports_abcd():
    with pytest.raises(ValueError, match="ABCD"):
        encode("ab", "string", "CDAB")

def test_string_rejects_embedded_nul():
    with pytest.raises(ValueError, match="nul"):
        encode("a\x00b", "string")

def test_string_encode_rejects_non_string_value():
    """与 register_span/parse_value 同一口径：非 str 不许 str() 成 "123" 静默编码。"""
    with pytest.raises(ValueError, match=r"string needs a str value, got int: 123"):
        encode(123, "string")


# ---- bool ----

def test_bool_decode_empty_registers_is_value_error():
    with pytest.raises(ValueError, match=r"bool needs 1 registers, got 0"):
        decode([], "bool")
