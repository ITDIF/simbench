"""改工程结构那一层（`project/edit.py`）：分配器、增删六条、骨架、拒绝与回滚。

这一层不碰 Qt、不占端口，所以断言全部打在**工程对象**上：拒绝之后工程必须与改动前
一字不差（`Project` 是 dataclass，深比较就是逐字段比），接受之后必须只剩**预期**的那处
变化。界面上那三种「先拒后回滚」的表现都由此派生，那一边只验"话有没有说出来"。
"""

from __future__ import annotations

import copy

import pytest

from simbench.project import edit
from simbench.project.model import (Device, Point, Project, S7Channel, SerialChannel,
                                    TcpChannel)
from simbench.project.validate import error_lines, validate_project


def channel(devices=(), name="tcp-5020", port=5020):
    return TcpChannel(name=name, host="0.0.0.0", port=port, devices=list(devices))


def device(unit=1, points=(), name="设备1"):
    return Device(name=name, unit_id=unit, points=list(points))


def point(name="点位1", area="hr", address=0, datatype="uint16", initial="0", bit=None):
    return Point(name=name, area=area, address=address, datatype=datatype,
                 initial=initial, bit=bit)


def errors(project):
    return error_lines(validate_project(project))


# ---------- 分配器 ----------

def test_the_first_tcp_channel_is_named_and_ported_from_the_convention():
    """TCP 的名字带端口是既有约定（CSV 没有端口列，导入按名字尾数认）。"""
    project = Project()
    assert edit.next_channel_name(project, "tcp") == "tcp-5020"
    assert edit.next_port(project) == 5020


def test_a_second_tcp_channel_does_not_take_the_same_port():
    project = Project(channels=[channel()])
    assert edit.next_port(project) == 5021
    assert edit.next_channel_name(project, "tcp") == "tcp-5021"


def test_the_s7_default_port_is_the_convention_not_the_tcp_one():
    """S7 照惯例给 102（Windows 上普通用户也能绑），不许被 TCP 那个 5020 带跑。"""
    project = Project()
    assert edit.default_channel(project, "s7").port == 102
    project.channels.append(edit.default_channel(project, "s7"))
    assert edit.default_channel(project, "s7").port == 103


def test_a_serial_channel_keeps_its_port_name_empty():
    """串口名留空是有意的（`model.SERIAL_DEFAULTS` 那条）：不替现场猜一个真实 COM 口。"""
    built = edit.default_channel(Project(), "rtu")
    assert isinstance(built, SerialChannel)
    assert built.port == ""
    assert built.baudrate == 9600


def test_device_names_and_units_avoid_what_is_already_there():
    ch = channel([device(unit=1, name="设备1"), device(unit=3, name="设备3")])
    assert edit.next_unit_id(ch) == 2                 # 1 与 3 被占，补最小的空档
    assert edit.next_device_name(ch) == "设备2"


def test_the_unit_allocator_refuses_when_every_unit_is_taken():
    ch = channel([device(unit=unit) for unit in range(1, 248)])
    with pytest.raises(edit.EditRefused):
        edit.next_unit_id(ch)


def test_point_names_are_unique_by_default():
    dev = device(points=[point(name="点位1"), point(name="点位2", address=1)])
    assert edit.next_point_name(dev) == "点位3"


def test_a_new_register_point_lands_after_the_last_one_span_included():
    """uint32 占两格，所以下一个点必须落在 2 之后——只看首地址会算出重叠。"""
    dev = device(points=[point(address=0, datatype="uint32")])
    assert edit.next_point_address(dev, "hr", "tcp") == (2, None)


def test_a_new_coil_lands_after_the_last_bit():
    dev = device(points=[point(area="co", datatype="bool", address=4)])
    assert edit.next_point_address(dev, "co", "tcp") == (5, None)


def test_a_new_s7_word_lands_on_a_byte_boundary_and_never_carries_a_bit():
    """非 bool 带 bit 会被 validate 拒（`s7_bit`），所以这里必须回 None 并向上取整到字节。"""
    dev = device(points=[point(area="db1", address=0, datatype="bool", bit=3)])
    assert edit.next_point_address(dev, "db1", "s7") == (1, None)


def test_a_new_s7_bool_takes_the_next_free_bit():
    dev = device(points=[point(area="db1", address=0, datatype="bool", bit=0)])
    assert edit.next_point_address(dev, "db1", "s7", bit_point=True) == (0, 1)


def test_the_allocator_refuses_past_the_address_ceiling():
    dev = device(points=[point(address=65535, datatype="uint32")])
    with pytest.raises(edit.EditRefused):
        edit.next_point_address(dev, "hr", "tcp")


def test_the_ceiling_check_counts_the_whole_span_not_just_the_first_cell():
    """validate 只看**首地址**：65535 上放一个占两格的点它不报，真跑起来主站读到越界。

    所以分配这一层必须按跨度算（今天界面只用跨度 1 的类型，这是给下一个调用方的闸）。
    """
    dev = device(points=[point(address=65530, datatype="uint16")])      # 占 65530，末尾 65531
    # 一个 uint32 需要 2 格：落在 65531 会到 65532（可以）
    assert edit.next_point_address(dev, "hr", "tcp", span=2) == (65531, None)
    tight = device(points=[point(address=65500, datatype="uint16")])
    with pytest.raises(edit.EditRefused) as excinfo:
        for _ in range(40):                       # 一路加到贴住上限
            address, _bit = edit.next_point_address(tight, "hr", "tcp", span=4)
            tight.points.append(point(address=address, datatype="uint32"))
    assert "past 65535" in str(excinfo.value)


def test_the_diff_counts_repeats_so_a_third_duplicate_name_is_still_refused():
    """差集按条数比：工程里本来就有两条同名通道时，再加第三条照样得拦。

    用集合比差集的话，新来的那一条与旧的字符串一模一样，会被当成"早就有"吞掉——
    "加了不该加的"就是这么发生的。
    """
    project = Project(channels=[channel(name="x", port=5020), channel(name="x", port=5021)])
    assert len([line for line in errors(project) if "channel_duplicated" in line]) == 1
    before = copy.deepcopy(project)
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.add_channel(project, channel(name="x", port=5022))
    assert "channel_duplicated" in str(excinfo.value)
    assert project == before


def test_a_bad_object_leaves_no_half_mutation_behind():
    """改的过程或验的过程自己炸了（对象根本不是通道）：工程里也不许留下它。"""
    project = Project()

    class NotAChannel:
        pass

    with pytest.raises(Exception):
        edit.add_channel(project, NotAChannel())
    assert project.channels == []


# ---------- 骨架 ----------

def test_the_tcp_skeleton_is_clean_and_has_one_of_everything():
    project = edit.new_project("tcp")
    assert errors(project) == []
    assert [ch.name for ch in project.channels] == ["tcp-5020"]
    assert [dev.unit_id for dev in project.channels[0].devices] == [1]
    assert [(pt.area, pt.address, pt.datatype) for pt in project.channels[0].devices[0].points] \
        == [("hr", 0, "uint16")]


def test_the_s7_skeleton_is_clean_and_starts_on_db1():
    project = edit.new_project("s7")
    assert errors(project) == []
    assert [(pt.area, pt.address, pt.datatype, pt.bit)
            for pt in project.channels[0].devices[0].points] == [("db1", 0, "word", None)]


def test_the_serial_skeleton_refuses_an_empty_port_instead_of_guessing_a_com():
    """串口名留空是**故意**的（不替现场猜一个真实 COM 口），所以从零建串口通道会被挡住——
    而且是 `validate` 自己那句话，不是这里另写一条"必填"。填上串口名就建得出来。"""
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.new_project("rtu")
    assert "serial_port" in str(excinfo.value)
    project = edit.new_project("rtu", port="COM3")
    assert errors(project) == []
    assert project.channels[0].port == "COM3"


def test_new_project_takes_the_name_and_parameters_from_the_caller():
    """对话框交回来的名字与端口原样生效（默认值只是"没人改"时的那一份）。"""
    project = edit.new_project("tcp", name="网关-1", host="127.0.0.1", port=15020)
    channel = project.channels[0]
    assert (channel.name, channel.host, channel.port) == ("网关-1", "127.0.0.1", 15020)
    assert errors(project) == []


def test_the_blank_skeleton_is_empty_and_still_valid():
    project = edit.new_project(edit.BLANK)
    assert project.channels == []
    assert errors(project) == []


# ---------- 增删 ----------

def test_add_channel_accepts_a_clean_one():
    project = Project()
    edit.add_channel(project, channel())
    assert [ch.name for ch in project.channels] == ["tcp-5020"]


def test_add_channel_refuses_a_duplicated_name_and_leaves_the_project_untouched():
    project = Project(channels=[channel()])
    before = copy.deepcopy(project)
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.add_channel(project, channel(name="tcp-5020", port=5021))
    assert "channel_duplicated" in str(excinfo.value)
    assert project == before


def test_add_channel_refuses_a_port_already_in_use():
    project = Project(channels=[channel()])
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.add_channel(project, channel(name="tcp-other", port=5020))
    assert "port_duplicated" in str(excinfo.value)


def test_a_new_channel_is_not_blocked_by_an_old_error_somewhere_else():
    """点表带着 error 也能装载：新加的这条只该被**自己引入的**错拦住。"""
    broken = SerialChannel(name="rtu-1", port="")          # 串口名为空：一条既有 error
    project = Project(channels=[broken])
    assert errors(project), "这条用例要的就是'工程里本来就有错'"
    edit.add_channel(project, channel())
    assert [ch.name for ch in project.channels] == ["rtu-1", "tcp-5020"]


def test_remove_channel_takes_it_out_and_lets_its_errors_go():
    project = Project(channels=[SerialChannel(name="rtu-1", port="")])
    edit.remove_channel(project, "rtu-1")
    assert project.channels == []
    assert errors(project) == []


def test_remove_channel_with_an_unknown_name_is_a_programming_error():
    with pytest.raises(KeyError):
        edit.remove_channel(Project(), "nope")


def test_add_device_refuses_a_duplicated_unit_and_rolls_back():
    first = device(unit=1)
    project = Project(channels=[channel([first])])
    before = copy.deepcopy(project)
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.add_device(project, "tcp-5020", device(unit=1, name="设备2"))
    assert "unit_duplicated" in str(excinfo.value)
    assert project == before


def test_remove_device_takes_its_points_with_it():
    project = Project(channels=[channel([device(unit=1, points=[point()]),
                                         device(unit=2, name="设备2")])])
    edit.remove_device(project, "tcp-5020", 1)
    assert [dev.unit_id for dev in project.channels[0].devices] == [2]


def test_add_point_lands_without_overlapping():
    dev = device(points=[point(address=0, datatype="uint32")])
    project = Project(channels=[channel([dev])])
    new = edit.default_point(dev, "hr", "tcp")
    edit.add_point(project, "tcp-5020", 1, new)
    assert [(pt.name, pt.address) for pt in dev.points] == [("点位1", 0), ("点位2", 2)]
    assert errors(project) == []


def test_add_point_that_would_overlap_is_refused_and_rolled_back():
    dev = device(points=[point(name="a", address=0, datatype="uint32")])
    project = Project(channels=[channel([dev])])
    before = copy.deepcopy(project)
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.add_point(project, "tcp-5020", 1, point(name="b", address=1))
    assert "overlap" in str(excinfo.value)
    assert project == before


def test_remove_point_takes_exactly_that_one():
    dev = device(points=[point(name="a", address=0), point(name="b", address=1)])
    project = Project(channels=[channel([dev])])
    edit.remove_point(project, "tcp-5020", 1, "hr", 1)
    assert [pt.name for pt in dev.points] == ["a"]


def test_a_bitless_removal_on_a_shared_byte_is_refused_but_a_par_one_works():
    """`DBX6.2` 与 `DBX6.3` 同占一个字节是合法的：**不给位就拒**（不知道你要哪一个），
    给了位就精确删那一个——这一层分得开了，不再需要"做不到"那种话。
    """
    dev = device(points=[Point(name="起", area="db1", address=6, datatype="bool", bit=2),
                         Point(name="停", area="db1", address=6, datatype="bool", bit=3)])
    project = Project(channels=[S7Channel(name="s7-1", host="0.0.0.0", port=102, devices=[dev])])
    before = copy.deepcopy(project)
    with pytest.raises(edit.EditRefused) as excinfo:
        edit.remove_point(project, "s7-1", 1, "db1", 6)
    assert "pass the bit" in str(excinfo.value)
    assert project == before

    edit.remove_point(project, "s7-1", 1, "db1", 6, bit=2)
    assert [pt.name for pt in dev.points] == ["停"], "删错了那一个（位没起作用）"


def test_find_point_matches_the_bit_exactly():
    dev = device(points=[Point(name="起", area="db1", address=6, datatype="bool", bit=2),
                         Point(name="停", area="db1", address=6, datatype="bool", bit=3)])
    assert edit.find_point(dev, "db1", 6, 3).name == "停"
    assert edit.find_point(dev, "db1", 6, 2).name == "起"
    with pytest.raises(KeyError):
        edit.find_point(dev, "db1", 6, 5)          # 这个位上没有点
    with pytest.raises(KeyError):
        edit.find_point(dev, "db1", 7, 2)          # 这个字节上没有点


def test_find_point_reports_a_missing_address_as_a_key_error():
    with pytest.raises(KeyError):
        edit.find_point(device(), "hr", 9)