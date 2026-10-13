"""S7 编解码：字节级断言（S7 一律大端、没有字序），位读写只动那一位。

数值字面量按 spec/计划钉死：`12.5f` = `41 48 00 00`、`-1` 的 dint = `FF FF FF FF`——
这些是"主站读到的到底是什么"的直接证据，不许从实现倒推。
"""

import pytest

from simbench.codec.s7_codec import decode, encode, read_bit, span_bytes, write_bit


def test_real_is_big_endian_ieee754():
    assert encode(12.5, "real") == bytes([0x41, 0x48, 0x00, 0x00])
    assert decode(bytes([0x41, 0x48, 0x00, 0x00]), "real") == pytest.approx(12.5)
    assert encode(-1.5, "real") == bytes([0xBF, 0xC0, 0x00, 0x00])


def test_signed_and_unsigned_are_big_endian():
    assert encode(-1, "dint") == b"\xff\xff\xff\xff"
    assert encode(65535, "word") == b"\xff\xff"
    assert encode(-32768, "int") == b"\x80\x00"
    assert encode(4294967295, "dword") == b"\xff\xff\xff\xff"
    assert decode(b"\xff\xff\xff\xff", "dint") == -1
    assert decode(b"\x80\x00", "int") == -32768


def test_byte_and_char_are_single_bytes():
    assert encode(255, "byte") == b"\xff"
    assert decode(b"\x00", "byte") == 0
    assert encode("A", "char") == b"A"
    assert decode(b"7", "char") == "7"
    with pytest.raises(ValueError, match="char needs exactly one character"):
        encode("AB", "char")
    with pytest.raises(ValueError, match="char is not ascii"):
        decode(b"\xb5", "char")


def test_string_is_bare_ascii_padded_with_nul():
    """`string` 是**裸 ASCII 定长**（与真 S7 STRING 的头两字节不同，见模块 docstring）。"""
    assert encode("AB", "string", 4) == b"AB\x00\x00"
    assert decode(b"AB\x00\x00", "string", 4) == "AB"
    assert encode("FAN-01", "string") == b"FAN-01"        # 不给长度就按值本身
    with pytest.raises(ValueError, match="at most 1 byte"):
        encode("AB", "string", 1)
    with pytest.raises(ValueError, match="not ascii"):
        encode("温度", "string")


@pytest.mark.parametrize("datatype, value, hint", [
    ("byte", 256, "byte out of range"),
    ("byte", -1, "byte out of range"),
    ("word", 70000, "word out of range"),
    ("int", 40000, "int out of range"),
    ("dint", 2 ** 31, "dint out of range"),
])
def test_out_of_range_names_the_s7_type_not_the_modbus_one(datatype, value, hint):
    """范围检查复用 Modbus 那一份边界，但**消息里的类型名必须是 S7 的**：
    点表里写的是 `word`，报 `uint16 out of range` 会让现场去找一个根本没有的类型。"""
    with pytest.raises(ValueError, match=hint):
        encode(value, datatype)


def test_dirty_input_is_refused_instead_of_truncated():
    with pytest.raises(ValueError, match="word needs 2 bytes, got 1"):
        decode(b"\x01", "word")
    with pytest.raises(ValueError, match="needs bytes"):
        decode("01", "word")
    with pytest.raises(ValueError, match="unknown S7 datatype"):
        encode(1, "uint16")            # Modbus 的名字在 S7 这一层不认
    with pytest.raises(ValueError, match="string needs at least 1 byte"):
        decode(b"", "string")


def test_a_string_value_may_come_from_the_gui_or_the_waveform_tick():
    """GUI 给的是字符串、波形 tick 给的是数字：两条路都收（与 Modbus codec 同口径）。"""
    assert encode("12.5", "real") == encode(12.5, "real")
    assert encode(12.5, "real") == encode("12.5", "real")


def test_bits_are_read_and_written_one_at_a_time():
    buffer = bytearray(2)
    write_bit(buffer, 3, True)             # DBX0.3
    assert buffer == bytearray([0b0000_1000, 0])
    assert read_bit(buffer, 3) is True and read_bit(buffer, 0) is False
    write_bit(buffer, 11, True)            # DBX1.3
    assert buffer == bytearray([0b0000_1000, 0b0000_1000])
    write_bit(buffer, 3, False)            # 清掉只清那一位
    assert buffer == bytearray([0, 0b0000_1000])


def test_bit_helpers_refuse_dirty_input():
    with pytest.raises(ValueError, match="bit index must be an int"):
        read_bit(b"\x00", True)           # bool 不算下标
    with pytest.raises(ValueError, match="outside 0..7"):
        write_bit(bytearray(1), 8, True)
    with pytest.raises(ValueError, match="needs a bytearray"):
        write_bit(b"\x00", 0, True)       # bytes 不可写：改了也看不见，等于静默失败


def test_span_bytes_follows_the_point_table():
    assert span_bytes("bool", "true") == 1
    assert span_bytes("word", "1") == 2
    assert span_bytes("real", "1.0") == 4
    assert span_bytes("string", "FAN-01") == 6
    assert span_bytes("string", "") == 1
