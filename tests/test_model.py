# tests/test_model.py
import pytest
from modslave.project.model import (
    DATATYPES, FLOAT32, INT16, STRING, UINT64, Device, Point, Project, TcpChannel,
    area_is_bit, register_span, word_orders_allowed,
)

def test_areas_and_bit_areas():
    assert area_is_bit("co") and area_is_bit("di")
    assert not area_is_bit("hr") and not area_is_bit("ir")

def test_register_span():
    assert register_span(INT16) == 1
    assert register_span(FLOAT32) == 2
    assert register_span(UINT64) == 4

def test_string_span_counts_pairs_rounded_up():
    assert register_span(STRING, "abc") == 2      # 3 字节补到 4
    assert register_span(STRING, "ab") == 1
    assert register_span(STRING, "") == 1         # 空串也至少占一个寄存器

def test_word_orders_allowed_by_width():
    assert word_orders_allowed(FLOAT32) == ("ABCD", "CDAB", "BADC", "DCBA")
    assert word_orders_allowed(UINT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(INT16) == ("ABCD",)
    assert word_orders_allowed(STRING) == ("ABCD",)

def test_unknown_datatype_rejected():
    with pytest.raises(ValueError):
        register_span("wchar")

def test_point_defaults():
    p = Point(name="x", area="hr", address=0, datatype=INT16)
    assert p.initial == "0" and p.word_order == "ABCD"
    assert p.waveform == "const" and p.readonly is False and p.row is None

def test_point_span_follows_datatype_and_area():
    assert Point("a", "hr", 0, FLOAT32).span == 2
    assert Point("a", "co", 0, "bool").span == 1

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
