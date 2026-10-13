"""S7 通道的启动前校验（Task 1）：通道参数、区/类型/地址（字节+位）、**按位**的重叠。

四条口径：

1. 判定只在 `validate.py` 一份（loader 只做转换、面板只做转换），所以这里逐项钉边界；
2. **类型与传输必须匹配**：S7 通道上写 `uint16`、Modbus 通道上写 `real` 都是错误——同一个词
   在两条传输上是两种东西，混填只能靠猜；
3. `bool` 必须带位（`DBX0.3` 的那一位是地址的一部分），非 bool 不许带；
4. 重叠按**位**判：同一字节里的两个 bool 合法，而 `word@0` 与 `bool@0.0`、`word@0` 与
   `word@1` 都算撞上。
"""

import pytest

from simbench.csv_io_entry import load_point_table
from simbench.project.model import (
    Device, Point, Project, S7Channel, TcpChannel,
)
from simbench.project.validate import error_lines, validate_project

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def codes(project) -> list[tuple[str, str]]:
    return [(i.code, i.where) for i in validate_project(project) if i.level == "error"]


def warns(project) -> list[tuple[str, str]]:
    return [(i.code, i.where) for i in validate_project(project) if i.level == "warning"]


def s7(points=None, **kwargs) -> Project:
    kwargs.setdefault("name", "s7-1")
    kwargs.setdefault("host", "192.168.0.10")
    points = points if points is not None else [
        Point("温度", "db1", 0, "real", initial="12.5"),
        Point("启动", "db1", 4, "bool", initial="true", bit=3),
    ]
    return Project(channels=[S7Channel(devices=[Device("D1", 11, points)], **kwargs)])


def test_a_valid_s7_project_is_clean():
    assert codes(s7()) == []
    assert warns(s7()) == []


@pytest.mark.parametrize("kwargs, expected", [
    ({"rack": 9}, ("s7_rack", "s7-1")),
    ({"rack": -1}, ("s7_rack", "s7-1")),
    ({"rack": "0"}, ("s7_rack", "s7-1")),
    ({"slot": 0}, ("s7_slot", "s7-1")),
    ({"slot": 99}, ("s7_slot", "s7-1")),
    ({"port": 0}, ("port", "s7-1")),
    ({"port": 70000}, ("port", "s7-1")),
    ({"host": ""}, ("host", "s7-1")),
    ({"host": "10.0.0.1:102"}, ("host", "s7-1")),
    ({"host": "10.0.0.1/24"}, ("host", "s7-1")),
    ({"host": "a b"}, ("host", "s7-1")),
])
def test_channel_parameters_have_their_own_boundaries(kwargs, expected):
    assert expected in codes(s7(**kwargs))


def test_a_host_that_would_break_the_csv_grammar_is_rejected():
    issues = validate_project(s7(host="10.0.0.1:102"))
    assert "must not contain" in str(issues[0])


@pytest.mark.parametrize("points, expected", [
    ([Point("a", "db0", 0, "real", initial="1")], ("s7_area", "s7-1/D1/a")),
    ([Point("a", "db4096", 0, "real", initial="1")], ("s7_area", "s7-1/D1/a")),
    ([Point("a", "xx", 0, "real", initial="1")], ("s7_area", "s7-1/D1/a")),
    ([Point("a", "mk", -1, "real", initial="1")], ("address", "s7-1/D1/a")),
    ([Point("a", "mk", 70000, "real", initial="1")], ("address", "s7-1/D1/a")),
    ([Point("a", "db1", 0, "uint16", initial="1")], ("s7_datatype", "s7-1/D1/a")),
    ([Point("a", "db1", 0, "reg16", initial="1")], ("s7_datatype", "s7-1/D1/a")),
    ([Point("a", "mk", 0, "bool", initial="true")], ("s7_bit", "s7-1/D1/a")),
    ([Point("a", "mk", 0, "bool", initial="true", bit=8)], ("s7_bit", "s7-1/D1/a")),
    ([Point("a", "mk", 0, "word", initial="1", bit=1)], ("s7_bit", "s7-1/D1/a")),
])
def test_point_parameters_have_their_own_boundaries(points, expected):
    assert expected in codes(s7(points))


def test_the_units_and_devices_are_still_checked():
    """设备与点位的公共判定（unit 范围、空名字、波形参数）在 S7 通道上照旧生效。"""
    project = Project(channels=[S7Channel("s7-1", devices=[
        Device("D1", 300, [Point("", "db1", 0, "real", initial="1")]),
        Device("D2", 300, [])])])
    got = [code for code, _ in codes(project)]
    assert "unit_range" in got and "unit_duplicated" in got and "name" in got


def test_a_word_order_that_s7_has_no_use_for_is_only_a_warning():
    """S7 一律大端：填 CDAB 不拦启动，只留一条"会被忽略"的警告（与 Modbus 单寄存器类型同姿态）。"""
    project = s7([Point("温度", "db1", 0, "real", initial="12.5", word_order="CDAB")])
    assert codes(project) == []
    assert ("s7_word_order", "s7-1/D1/温度") in warns(project)


# ---------- 重叠按位判 ----------

def test_two_bools_in_one_byte_are_not_an_overlap():
    project = s7([Point("b0", "db1", 0, "bool", initial="true", bit=0),
                  Point("b3", "db1", 0, "bool", initial="true", bit=3),
                  Point("b1", "db1", 1, "bool", initial="true", bit=1)])
    assert codes(project) == []


@pytest.mark.parametrize("points, overlaps", [
    ([Point("w", "db1", 0, "word", initial="1"),
      Point("b", "db1", 0, "bool", initial="true", bit=0)], True),
    ([Point("w0", "db1", 0, "word", initial="1"),
      Point("w1", "db1", 1, "word", initial="1")], True),
    ([Point("w0", "db1", 0, "word", initial="1"),
      Point("r2", "db1", 2, "real", initial="1")], False),
    ([Point("r0", "db1", 0, "real", initial="1"),
      Point("b", "db1", 3, "bool", initial="true", bit=7)], True),
    ([Point("r0", "db1", 0, "real", initial="1"),
      Point("b", "db1", 4, "bool", initial="true", bit=0)], False),
    ([Point("a", "db1", 0, "real", initial="1"),
      Point("b", "db2", 0, "real", initial="1")], False),
    ([Point("a", "mk", 0, "real", initial="1"),
      Point("b", "db1", 0, "real", initial="1")], False),
])
def test_overlap_is_bit_granular_and_per_area(points, overlaps):
    got = [code for code, _ in codes(s7(points))]
    assert ("s7_overlap" in got) is overlaps, got


def test_two_channels_may_not_share_one_host_port():
    project = Project(channels=[S7Channel("s7-1", host="10.0.0.1", port=102),
                                S7Channel("s7-2", host="10.0.0.1", port=102)])
    assert ("port_duplicated", "s7-2") in codes(project)
    # 与 TCP 通道抢同一个 host:port 同样是冲突（S7 的 102 与 TCP 的 502 也会撞）
    mixed = Project(channels=[TcpChannel("tcp-1", host="10.0.0.1", port=102),
                              S7Channel("s7-1", host="10.0.0.1", port=102)])
    assert ("port_duplicated", "s7-1") in codes(mixed)


def test_a_modbus_type_on_a_modbus_channel_is_still_fine():
    """回归：类型判断是**按传输**的——`uint16` 在 Modbus 通道上照旧合法。"""
    project = Project(channels=[TcpChannel("tcp-1", devices=[
        Device("D1", 11, [Point("x", "hr", 0, "uint16", initial="1")])])])
    assert codes(project) == []


def test_the_example_modbus_project_gets_no_s7_issue_at_all():
    """回归：既有工程（全是 Modbus）一条 S7 相关的错误/警告都不许多出来。"""
    _project, _notes, issues = load_point_table(CSV)
    assert error_lines(issues) == []
    s7_codes = {"s7_area", "s7_datatype", "s7_bit", "s7_overlap", "s7_rack", "s7_slot",
                "s7_word_order"}
    assert not [i for i in issues if i.code in s7_codes]
