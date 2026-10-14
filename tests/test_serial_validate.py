"""串口通道的启动前校验（M5 Task 2）：参数白名单、重复串口、类型闸门。

四条口径：

1. 判定只在 `validate.py` 一份（面板只做转换、loader 只做类型与形状），所以这里逐项钉
   边界值，而不是在面板或 loader 里各判一套；
2. 白名单而不是范围：波特率 9500 是"看着像数"的脏值，真跑起来是收发全乱、报文窗一片
   CRC 错，比当场报错难查得多；
3. 两个通道抢同一个串口 = error（与 TCP 的 `port_duplicated` 同一个形状、不同的 code）：
   现象是"一路能收一路全错帧"，而 Windows 只会说 Access is denied；
4. 既有 TCP 工程一条新错误都不许多——这是 M5 不许碰 M1/M2 行为的回归网。
"""

import pytest

from simbench.csv_io_entry import load_point_table
from simbench.project.model import (
    Device,
    Point,
    Project,
    SerialChannel,
    TcpChannel,
)
from simbench.project.validate import error_lines, validate_project

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def codes(project) -> list[tuple[str, str]]:
    return [(i.code, i.where) for i in validate_project(project) if i.level == "error"]


def serial(**kwargs) -> Project:
    kwargs.setdefault("name", "com-485-1")
    kwargs.setdefault("port", "COM3")
    return Project(channels=[SerialChannel(**kwargs)])


def test_a_default_serial_channel_is_clean():
    assert codes(serial()) == []


def test_an_rtu_channel_with_points_is_clean():
    project = Project(channels=[SerialChannel("com-485-1", port="COM3", devices=[
        Device("COVI-1", 11, [Point("温度", "hr", 0, "uint16", initial="7")])])])
    assert codes(project) == []


@pytest.mark.parametrize("kwargs, expected", [
    ({"baudrate": 9500}, ("baudrate", "com-485-1")),
    ({"baudrate": "9600"}, ("baudrate", "com-485-1")),
    ({"parity": "X"}, ("parity", "com-485-1")),
    ({"parity": "e"}, ("parity", "com-485-1")),
    ({"bytesize": 9}, ("bytesize", "com-485-1")),
    ({"stopbits": 3}, ("stopbits", "com-485-1")),
    ({"timeout": 0}, ("timeout", "com-485-1")),
    ({"timeout": -1.0}, ("timeout", "com-485-1")),
    ({"timeout": "0.3"}, ("timeout", "com-485-1")),
    ({"port": ""}, ("serial_port", "com-485-1")),
    ({"port": "   "}, ("serial_port", "com-485-1")),
    ({"port": 502}, ("serial_port", "com-485-1")),
    ({"port": "COM 3"}, ("serial_port", "com-485-1")),
    ({"port": "COM@3"}, ("serial_port", "com-485-1")),
])
def test_each_serial_parameter_has_its_own_boundary(kwargs, expected):
    assert expected in codes(serial(**kwargs))


def test_a_serial_port_that_would_break_the_csv_grammar_is_rejected():
    """串口名里带空格或 `@`：CSV 那条 `name@COM3 9600 8N1` 语法会解析不回来（导出再导入丢设备）。"""
    issues = validate_project(serial(port="COM 3"))
    assert "whitespace" in str(issues[0])


def test_two_channels_may_not_share_one_serial_port():
    project = Project(channels=[SerialChannel("com-485-1", port="COM3"),
                                SerialChannel("com-485-2", port="COM3")])
    assert ("serial_duplicated", "com-485-2") in codes(project)


def test_the_serial_duplicate_check_is_not_the_tcp_one():
    """两套判定分开：串口冲突报的是 serial_duplicated，不是 port_duplicated（话术完全不同）。"""
    got = [code for code, _ in codes(Project(channels=[
        SerialChannel("a", port="COM3"), SerialChannel("b", port="COM3")]))]
    assert got == ["serial_duplicated"]


def test_a_channel_name_may_not_carry_the_csv_serial_mark():
    """`@` 是 CSV 里"这路是串口"的标记：名字里再带一个，导出一份再导回来会变成另一路。"""
    for channel in (TcpChannel("a@b"), SerialChannel("a@b")):
        assert ("channel_name", "a@b") in codes(Project(channels=[channel]))


def test_an_unsupported_type_is_still_reported():
    assert ("channel_type", "x") in codes(Project(channels=[TcpChannel("x", type="udp")]))
    # 用 TCP 的字段却写着 rtu（只可能来自代码或手拼的测试）：报 type 不匹配，不拿它去判串口参数
    assert ("channel_type", "com1") in codes(
        Project(channels=[TcpChannel("com1", host="", port=502, type="rtu")]))


def test_an_rtu_channel_still_gets_the_point_level_checks():
    """串口通道的**点位**判定与 TCP 一模一样：unit 越界、地址重叠都照报。"""
    project = Project(channels=[SerialChannel("com-485-1", port="COM3", devices=[
        Device("D", 300, [Point("x", "hr", 0, "uint16", initial="1"),
                          Point("y", "hr", 0, "uint16", initial="1")])])])
    got = [code for code, _ in codes(project)]
    assert "unit_range" in got and "overlap" in got


def test_a_tcp_project_gets_no_serial_issue_at_all():
    """回归：既有工程（全是 TCP）一条串口相关的错误/警告都不许多出来。"""
    project, _notes, issues = load_point_table(CSV)
    assert error_lines(issues) == []
    serial_codes = {"serial_port", "baudrate", "parity", "bytesize", "stopbits",
                    "timeout", "serial_duplicated", "channel_name"}
    assert not [i for i in issues if i.code in serial_codes]


def test_a_serial_point_table_loads_from_json_and_validates_clean(tmp_path):
    """JSON 那一整条路：写一份带串口通道的工程 -> 读回来 -> 校验没有 error。"""
    from simbench.project.json_io import save
    project = Project(channels=[SerialChannel("com-485-1", port="COM3", devices=[
        Device("COVI-1", 11, [Point("温度", "hr", 0, "uint16", initial="7")])])])
    path = tmp_path / "project.json"
    save(project, path)
    loaded, notes, issues = load_point_table(str(path))
    assert notes == []
    assert error_lines(issues) == []
    assert loaded.channels[0].port == "COM3"
