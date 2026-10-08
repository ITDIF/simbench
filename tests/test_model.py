# tests/test_model.py
import struct

import pytest
from modslave.project.model import (
    BOOL, DATATYPES, FLOAT32, FLOAT64, INT16, INT64, REG16, STRING, UINT16,
    UINT64, Device, Point, Project, TcpChannel, area_is_bit, parse_value,
    register_span, struct_format, word_orders_allowed,
)

def test_areas_and_bit_areas():
    assert area_is_bit("co") and area_is_bit("di")
    assert not area_is_bit("hr") and not area_is_bit("ir")

def test_register_span():
    assert register_span(INT16) == 1
    assert register_span(FLOAT32) == 2
    assert register_span(UINT64) == 4

def test_string_span_counts_pairs_rounded_up():
    assert register_span(STRING, "") == 1         # 空串也至少占一个寄存器
    assert register_span(STRING, "a") == 1
    assert register_span(STRING, "ab") == 1
    assert register_span(STRING, "abc") == 2      # 3 字节补到 4
    assert register_span(STRING, "aaaaa") == 3    # 5 字节补到 6

def test_word_orders_allowed_by_width():
    assert word_orders_allowed(FLOAT32) == ("ABCD", "CDAB", "BADC", "DCBA")
    assert word_orders_allowed(UINT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(INT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(FLOAT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(INT16) == ("ABCD",)
    assert word_orders_allowed(STRING) == ("ABCD",)

def test_struct_format_is_big_endian():
    assert struct_format(INT16) == ">h"
    assert struct_format(UINT16) == ">H"
    assert struct_format(REG16) == ">H"
    assert struct_format(FLOAT32) == ">f"
    assert struct_format(FLOAT64) == ">d"
    with pytest.raises(ValueError):               # bool 没有 struct 码
        struct_format(BOOL)

def test_unknown_datatype_rejected():
    with pytest.raises(ValueError):
        register_span("wchar")
    with pytest.raises(ValueError):
        struct_format("wchar")

def test_bool_parses_explicit_whitelists():
    for text in ("1", "true", "yes", "y", "on"):
        assert parse_value(BOOL, text) is True
    for text in ("0", "false", "no", "n", "off"):
        assert parse_value(BOOL, text) is False
    assert parse_value(BOOL, "TRUE") is True      # 大小写与空格吃掉
    assert parse_value(BOOL, " No ") is False

def test_bool_rejects_anything_outside_whitelists():
    # 拼错/多字符/空值一律报错，不许当成 False（静默错值会被当成平台 bug）
    for text in ("TURE", "01", "-1", "maybe", "", "   ", "2", "oneline"):
        with pytest.raises(ValueError):
            parse_value(BOOL, text)

def test_bool_accepts_python_bool_but_not_int():
    assert parse_value(BOOL, True) is True
    assert parse_value(BOOL, False) is False
    for value in (0, 1, None):
        with pytest.raises(ValueError):
            parse_value(BOOL, value)

def test_numeric_types_reject_non_string_input():
    # 以前 parse_value('int16', 42.9) 截断成 42、parse_value('string', 123) 变成 '123'
    for datatype, value in ((INT16, 42.9), (INT16, 42), (UINT64, 1),
                            (FLOAT32, 1.5), (STRING, 123), (REG16, None)):
        with pytest.raises(ValueError):
            parse_value(datatype, value)
    with pytest.raises(ValueError, match="needs a string value"):
        parse_value(INT16, 42.9)
    assert parse_value(INT16, "42") == 42         # 字符串照常工作

def test_float_parses_normal_values():
    assert parse_value(FLOAT32, "12.5") == 12.5
    assert parse_value(FLOAT64, "-2.25") == -2.25

def test_float_rejects_nan_with_nan_message():
    for datatype in (FLOAT32, FLOAT64):
        with pytest.raises(ValueError) as exc:
            parse_value(datatype, "nan")
        # 填的是 nan 才能说 nan；不能把溢出的 inf 也混进这条消息
        assert str(exc.value) == f"{datatype} rejects nan: 'nan'"

def test_float_out_of_range_message_shows_the_limit():
    # 1e309 不是 nan/inf 的拼写问题，是 float64 溢出，消息必须说越界并给上限
    with pytest.raises(ValueError) as exc:
        parse_value(FLOAT64, "1e309")
    assert str(exc.value).startswith(
        "float64 out of range, |value| must be <= 1.7976931348623157e+308:")
    with pytest.raises(ValueError) as exc:
        parse_value(FLOAT32, "3.5e38")
    assert str(exc.value).startswith(
        "float32 out of range, |value| must be <= 3.4028234663852886e+38:")
    with pytest.raises(ValueError, match="out of range"):
        parse_value(FLOAT32, "inf")               # inf 自然落在越界这条

def test_float32_exact_max_still_packable():
    value = parse_value(FLOAT32, "3.4028234663852886e38")
    assert struct.pack(struct_format(FLOAT32), value) != b""

def test_int_out_of_range_message_shows_bounds():
    with pytest.raises(ValueError, match=r"\[-32768, 32767\]"):
        parse_value(INT16, "32768")

def test_string_requires_ascii():
    assert parse_value(STRING, "Fan1") == "Fan1"
    with pytest.raises(ValueError) as exc:
        parse_value(STRING, "风机")
    assert str(exc.value) == "string must be ascii: '风机'"
    # span 在算重叠前先编码长度，中文初值不许抛裸 UnicodeEncodeError
    # （UnicodeEncodeError 是 ValueError 的子类，所以必须显式排除它）
    with pytest.raises(ValueError) as exc:
        register_span(STRING, "风机")
    assert not isinstance(exc.value, UnicodeEncodeError)
    assert str(exc.value) == "string must be ascii: '风机'"

def test_point_defaults():
    p = Point(name="x", area="hr", address=0, datatype=INT16)
    assert p.initial == "0" and p.word_order == "ABCD"
    assert p.waveform == "const" and p.readonly is False and p.row is None
    assert p.wave_param == {}
    # default_factory 唯一真正在防的坑：两个实例不能共享同一个 dict
    q = Point(name="y", area="hr", address=1, datatype=INT16)
    p.wave_param["amplitude"] = "10"
    assert q.wave_param == {}

def test_point_span_follows_datatype_and_area():
    # 位区固定 1：co 上放 FLOAT32（寄存器区是 2）才能证明走的是 area 分支
    assert Point("a", "co", 0, "bool").span == 1
    assert Point("a", "di", 0, FLOAT32).span == 1
    # 寄存器区按类型
    assert Point("a", "hr", 0, "bool").span == 1
    assert Point("a", "hr", 0, FLOAT32).span == 2
    assert Point("a", "hr", 0, UINT64).span == 4

def test_typed_initial_returns_native_values():
    assert Point("a", "hr", 0, INT16, "42").typed_initial() == 42
    assert Point("a", "hr", 0, FLOAT32, "12.5").typed_initial() == 12.5
    assert Point("a", "hr", 0, STRING, "Fan1").typed_initial() == "Fan1"
    assert Point("a", "co", 0, "bool", "on").typed_initial() is True
    assert Point("a", "co", 0, "bool").typed_initial() is False   # 默认 initial="0"

def test_typed_initial_raises_instead_of_guessing():
    with pytest.raises(ValueError):
        Point("a", "co", 0, "bool", "TURE").typed_initial()
    with pytest.raises(ValueError):
        Point("a", "hr", 0, FLOAT32, "3.5e38").typed_initial()
    with pytest.raises(ValueError):
        Point("a", "hr", 0, INT16, "42.9").typed_initial()
    with pytest.raises(ValueError) as exc:
        Point("a", "hr", 0, STRING, "风机").span
    assert not isinstance(exc.value, UnicodeEncodeError)

def test_project_helpers():
    pt = Point("a", "hr", 0, INT16)
    proj = Project(channels=[TcpChannel(name="c1", host="127.0.0.1", port=502,
                                        devices=[Device(name="D1", unit_id=11,
                                                        points=[pt])])])
    assert [p.name for _, _, p in proj.iter_points()] == ["a"]
    assert proj.find_channel("c1").port == 502
    with pytest.raises(KeyError):
        proj.find_channel("nope")

def test_datatypes_are_plain_strings():
    assert isinstance(DATATYPES, tuple) and "bool" in DATATYPES
