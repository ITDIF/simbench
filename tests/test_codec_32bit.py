# tests/test_codec_32bit.py
import pytest

from simbench.codec.point_codec import decode, encode

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
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="float32"):
            encode(bad, "float32", "ABCD")

def test_decode_checks_register_count():
    with pytest.raises(ValueError, match="needs 2 registers"):
        decode([1], "int32", "ABCD")

def test_unknown_datatype():
    with pytest.raises(ValueError, match="wchar"):
        encode(1, "wchar", "ABCD")


@pytest.mark.parametrize("bad_order", ["CDAB", "BADC", "DCBA", "XYZ"])
def test_datatype_is_judged_before_word_order(bad_order):
    """类型名填错时错误必须指向类型：`wchar only supports ('ABCD',)` 把人往字序上引，
    而真正的错是 datatype。模拟器的毛病不许伪装成别的问题。"""
    with pytest.raises(ValueError, match=r"unknown datatype 'wchar'"):
        encode(1, "wchar", bad_order)
    with pytest.raises(ValueError, match=r"unknown datatype 'wchar'"):
        decode([0, 0], "wchar", bad_order)


def test_known_datatype_still_reports_word_order():
    """类型没错时字序洞照旧堵着，两条判定不能互相顶掉。"""
    with pytest.raises(ValueError, match=r"unknown word_order 'XYZ'"):
        encode(1, "int16", "XYZ")
    with pytest.raises(ValueError, match=r"unknown word_order 'XYZ'"):
        decode([0, 0], "int16", "XYZ")
    with pytest.raises(ValueError, match=r"int16 only supports \('ABCD',\)"):
        encode(1, "int16", "CDAB")
    with pytest.raises(ValueError, match=r"float64 only supports"):
        decode([0, 0], "float64", "CDAB")


# ---- 浮点分支不许漏底层异常（Task 5 的“一次报全”只收 ValueError）----

@pytest.mark.parametrize("bad", [None, [], object(), {}])
def test_float_encode_wraps_type_error(bad):
    """encode(None,'int16') 早就抛 ValueError，同一个 None 走浮点路径不许变 traceback。"""
    with pytest.raises(ValueError, match=r"float32 needs a real number, got"):
        encode(bad, "float32", "ABCD")


def test_float_encode_wraps_overflow_error():
    with pytest.raises(ValueError, match=r"float32 needs a real number, got int"):
        encode(10 ** 400, "float32", "ABCD")


def test_float_encode_keeps_the_underlying_exception_as_cause():
    """包成领域错但不能把现场信息丢了：__cause__ 必须还是那个底层异常。"""
    with pytest.raises(ValueError) as ok:
        encode(None, "float32", "ABCD")
    assert isinstance(ok.value.__cause__, TypeError)
    with pytest.raises(ValueError) as ok:
        encode(10 ** 400, "float64", "ABCD")
    assert isinstance(ok.value.__cause__, OverflowError)


def test_bool_is_not_silently_encoded_as_one_point_zero():
    """True 走 float32 曾经是 [0x3F80, 0x0000]（1.0）：静默造数，必须和 int16 一样拒。"""
    for dt in ("int16", "float32", "float64"):
        with pytest.raises(ValueError, match=f"{dt} needs a number, got bool: True"):
            encode(True, dt, "ABCD")
    with pytest.raises(ValueError, match="float32 needs a number, got bool"):
        encode(True, "float32", "CDAB")
