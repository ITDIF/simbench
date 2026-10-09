"""CSV 点表读写：列顺序即合同，解析错误必须带 CSV 行号，坏文件不许装作读出一个空工程。"""

import pytest

from modslave.project import csv_io, model
from modslave.project.csv_io import (
    HEADER, format_wave_param, parse_wave_param, read_csv, read_csv_file,
    write_csv, write_csv_file,
)
from modslave.project.model import Device, Point, Project, TcpChannel
from modslave.project.validate import error_lines, validate_project

ROWS = """channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
tcp-502,COVI-1,11,CO浓度,hr,0,float32,CDAB,12.5,sine,base=20;amp=15;period=60,true
tcp-502,COVI-1,11,风速,hr,2,uint16,ABCD,0,const,,false
tcp-502,风机-1,12,启动,co,3,bool,ABCD,false,const,,false
tcp-502,风机-1,12,运行状态,di,0,bool,ABCD,true,const,,true
"""

ALL_PORTS = {"tcp-502": 502}


def test_groups_by_channel_then_device():
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    assert len(proj.channels) == 1
    ch = proj.channels[0]
    assert (ch.name, ch.host, ch.port, ch.type) == ("tcp-502", "0.0.0.0", 502, "tcp")
    assert [d.name for d in ch.devices] == ["COVI-1", "风机-1"]
    assert [d.unit_id for d in ch.devices] == [11, 12]
    assert [p.name for p in ch.devices[0].points] == ["CO浓度", "风速"]


def test_parses_wave_param_and_flags():
    pt = read_csv(ROWS, ports={"tcp-502": 502}).channels[0].devices[0].points[0]
    assert (pt.word_order, pt.waveform, pt.readonly) == ("CDAB", "sine", True)
    assert pt.wave_param == {"base": "20", "amp": "15", "period": "60"}
    assert pt.row == 2


def test_row_numbers_follow_csv_lines():
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    rows = [p.row for ch in proj.channels for d in ch.devices for p in d.points]
    assert rows == [2, 3, 4, 5]


def test_values_are_trimmed_and_case_normalised():
    text = ROWS.replace("tcp-502,COVI-1,11,CO浓度,hr,0,float32,CDAB",
                        "tcp-502,COVI-1,11,CO浓度, HR ,0, Float32 , cdab ")
    pt = read_csv(text, ports={"tcp-502": 502}).channels[0].devices[0].points[0]
    assert (pt.area, pt.datatype, pt.word_order) == ("hr", "float32", "CDAB")


def test_bad_address_reports_line_number():
    bad = ROWS + "tcp-502,风机-1,12,X,hr,notanumber,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError, match="line 6"):
        read_csv(bad, ports={"tcp-502": 502})


def test_bad_unit_id_reports_line_number():
    bad = ROWS + "tcp-502,风机-1,x,X,hr,9,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError, match="line 6"):
        read_csv(bad, ports={"tcp-502": 502})


def test_wrong_column_count_reports_line_number():
    bad = ROWS + "tcp-502,风机-1,12,X,hr,9,uint16\n"
    with pytest.raises(ValueError, match="line 6"):
        read_csv(bad, ports={"tcp-502": 502})


def test_wrong_header_is_rejected():
    with pytest.raises(ValueError, match="header"):
        read_csv("channel,device\nx,y\n", ports={})


def test_empty_file_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        read_csv("", ports={})


def test_blank_lines_and_comment_lines_are_skipped():
    text = ROWS.replace("tcp-502,COVI-1,11,风速", "# note\n\ntcp-502,COVI-1,11,风速")
    proj = read_csv(text, ports={"tcp-502": 502})
    assert len(proj.channels[0].devices[0].points) == 2


def test_device_name_conflict_on_same_unit_is_reported():
    text = ROWS + "tcp-502,别的名,11,电压,hr,4,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError, match="unit_id 11"):
        read_csv(text, ports={"tcp-502": 502})


def test_csv_roundtrip_is_identical():
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    assert read_csv(write_csv(proj), ports={"tcp-502": 502}) == proj


def test_read_csv_file_accepts_bom(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text("\ufeff" + ROWS, encoding="utf-8")
    proj = read_csv_file(path, ports={"tcp-502": 502})
    assert proj.channels[0].devices[0].points[0].name == "CO浓度"


def test_write_csv_file_then_read_back(tmp_path):
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    path = tmp_path / "out.csv"
    write_csv_file(proj, path)
    assert read_csv_file(path, ports={"tcp-502": 502}) == proj


def test_wave_param_helpers():
    assert parse_wave_param("base=20;amp=15") == {"base": "20", "amp": "15"}
    assert parse_wave_param("") == {}
    assert format_wave_param({"base": "20", "amp": "15"}) == "base=20;amp=15"


def test_channel_without_port_falls_back_and_notes_it():
    proj, notes = read_csv(ROWS, ports={}, with_notes=True)
    assert proj.channels[0].port == 502        # 从 tcp-502 尾数猜出来
    assert any("port" in n for n in notes)


def test_header_is_exactly_the_contract():
    assert HEADER == ["channel", "device", "unit_id", "name", "area", "address",
                      "datatype", "word_order", "initial", "waveform", "wave_param",
                      "readonly"]


# ---- 返回类型：调用方不许靠猜（Task 12 走 with_notes=True 那条路） ----

def test_default_return_is_a_project_not_a_tuple():
    assert isinstance(read_csv(ROWS, ports=ALL_PORTS), Project)


def test_with_notes_false_keeps_plain_project():
    assert isinstance(read_csv(ROWS, ports=ALL_PORTS, with_notes=False), Project)


def test_with_notes_true_returns_tuple_and_no_notes_when_ports_given():
    result = read_csv(ROWS, ports=ALL_PORTS, with_notes=True)
    assert isinstance(result, tuple) and len(result) == 2
    proj, notes = result
    assert isinstance(proj, Project)
    assert notes == []


def test_read_csv_file_forwards_with_notes(tmp_path):
    """Task 12 的装载口就是这么调的：read_csv_file(path, ports=..., with_notes=True)。"""
    path = tmp_path / "p.csv"
    write_csv_file(read_csv(ROWS, ports=ALL_PORTS), path)
    proj, notes = read_csv_file(path, ports={}, with_notes=True)
    assert isinstance(proj, Project)
    assert proj.channels[0].port == 502
    assert any("port" in n for n in notes)


# ---- 行号与消息文案：现场要能当场改对 ----

def test_bad_address_message_names_line_column_and_value():
    bad = ROWS + "tcp-502,风机-1,12,X,hr,notanumber,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(bad, ports=ALL_PORTS)
    assert str(excinfo.value) == "line 6: address 'notanumber' is not an integer"


def test_bad_unit_id_message_names_line_column_and_value():
    bad = ROWS + "tcp-502,风机-1,x,X,hr,9,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(bad, ports=ALL_PORTS)
    assert str(excinfo.value) == "line 6: unit_id 'x' is not an integer"


def test_wrong_column_count_message_says_expected_and_actual():
    bad = ROWS + "tcp-502,风机-1,12,X,hr,9,uint16\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(bad, ports=ALL_PORTS)
    assert str(excinfo.value) == f"line 6: expected {len(HEADER)} columns, got 7"


def test_header_mismatch_message_shows_both_headers():
    with pytest.raises(ValueError) as excinfo:
        read_csv("Channel,Device,Unit\nA,B,1\n", ports={})
    message = str(excinfo.value)
    assert f"expected {','.join(HEADER)}" in message      # 期望表头
    assert "got 'channel,device,unit'" in message          # 实际表头（已小写化）


def test_device_conflict_message_has_line_and_both_device_names():
    text = ROWS + "tcp-502,别的名,11,电压,hr,4,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(text, ports=ALL_PORTS)
    assert str(excinfo.value) == \
        "line 6: unit_id 11 is already used by device 'COVI-1', got '别的名'"


def test_very_long_dirty_value_is_truncated_in_the_message():
    """裁决 N/T：用户原值超 80 字符要 brief() 截断，一条脏值不许把清单刷屏。"""
    junk = "X" * 400
    bad = ROWS + f"tcp-502,风机-1,12,{junk},hr,{junk},uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(bad, ports=ALL_PORTS)
    message = str(excinfo.value)
    assert message.startswith("line 6: address ")
    assert junk[:100] not in message
    assert message.endswith("... is not an integer")
    assert len(message) < 140


def test_row_numbers_still_match_physical_lines_with_comments_and_blanks():
    text = ROWS.replace("tcp-502,COVI-1,11,风速", "# note\n\ntcp-502,COVI-1,11,风速")
    proj = read_csv(text, ports=ALL_PORTS)
    rows = [p.row for ch in proj.channels for d in ch.devices for p in d.points]
    assert rows == [2, 5, 6, 7]


# ---- 表头/注释之外，坏文件不许变成"空工程" ----

@pytest.mark.parametrize("text", ["", "\n", "\n\n", "# only a comment\n", "# a\n# b\n"])
def test_files_without_any_data_are_rejected(text):
    with pytest.raises(ValueError, match="empty"):
        read_csv(text, ports=ALL_PORTS)


def test_header_only_file_is_rejected_instead_of_returning_empty_project():
    """只有表头 = 点表是空的：读出一个零通道工程会让从站"起成功"却什么都不应答。"""
    header_only = ",".join(HEADER) + "\n"
    with pytest.raises(ValueError, match="empty") as excinfo:
        read_csv(header_only, ports=ALL_PORTS)
    assert "header" in str(excinfo.value)


def test_header_with_only_comment_rows_is_rejected():
    text = ",".join(HEADER) + "\n# 这张表还没填\n\n"
    with pytest.raises(ValueError, match="empty"):
        read_csv(text, ports=ALL_PORTS)


def test_missing_file_raises_os_error_not_value_error(tmp_path):
    """文件读不到是操作错误，跟点表内容错误分开（Task 12 的装载口按这两类分别处理）。"""
    with pytest.raises(OSError):
        read_csv_file(tmp_path / "nope.csv", ports=ALL_PORTS)


# ---- 分组、端口猜测、偏移 ----

def test_same_unit_id_in_two_channels_is_two_devices():
    text = (",".join(HEADER) + "\n"
            "tcp-5020,A,1,p1,hr,0,uint16,ABCD,0,const,,false\n"
            "tcp-5021,B,1,p2,hr,0,uint16,ABCD,0,const,,false\n")
    proj = read_csv(text, ports={"tcp-5020": 5020, "tcp-5021": 5021})
    assert [ch.name for ch in proj.channels] == ["tcp-5020", "tcp-5021"]
    assert [(ch.devices[0].name, ch.devices[0].unit_id) for ch in proj.channels] == \
        [("A", 1), ("B", 1)]


def test_non_adjacent_rows_of_the_same_unit_merge_into_one_device():
    """厂商表按别的列排过序，同一个设备的行会被别的设备夹在中间：合并只看
    (channel, unit_id)，不看行是否相连；设备顺序按首次出现。"""
    text = (",".join(HEADER) + "\n"
            "tcp-502,A,1,p1,hr,0,uint16,ABCD,0,const,,false\n"
            "tcp-502,B,2,p2,hr,0,uint16,ABCD,0,const,,false\n"
            "tcp-502,A,1,p3,hr,1,uint16,ABCD,0,const,,false\n")
    proj = read_csv(text, ports=ALL_PORTS)
    ch = proj.channels[0]
    assert [(d.name, d.unit_id, [p.name for p in d.points]) for d in ch.devices] == \
        [("A", 1, ["p1", "p3"]), ("B", 2, ["p2"])]
    assert [p.row for _, _, p in proj.iter_points()] == [2, 4, 3]


def test_channel_port_is_guessed_from_its_name_tail():
    text = (",".join(HEADER) + "\n"
            "tcp-5020,A,1,p1,hr,0,uint16,ABCD,0,const,,false\n")
    proj, notes = read_csv(text, ports={}, with_notes=True)
    assert proj.channels[0].port == 5020
    assert len(notes) == 1
    assert "tcp-5020" in notes[0] and "5020" in notes[0]


def test_channel_name_without_digits_falls_back_to_502_and_notes_it():
    text = (",".join(HEADER) + "\n"
            "tunnel-A,风机-1,12,p1,hr,0,uint16,ABCD,0,const,,false\n")
    proj, notes = read_csv(text, ports={}, with_notes=True)
    assert proj.channels[0].port == 502
    assert any("port" in n for n in notes)


def test_out_of_range_guessed_port_falls_back_to_502():
    text = (",".join(HEADER) + "\n"
            "tcp-99999999,A,1,p1,hr,0,uint16,ABCD,0,const,,false\n")
    proj = read_csv(text, ports={}, with_notes=True)[0]
    assert proj.channels[0].port == 502


def test_address_offset_is_added_to_every_point():
    """计划里的实现是 `+ address_offset`：正数把整表地址往上挪。"""
    proj = read_csv(ROWS, ports=ALL_PORTS, address_offset=1)
    got = [(p.area, p.address) for _, _, p in proj.iter_points()]
    assert got == [("hr", 1), ("hr", 3), ("co", 4), ("di", 1)]


def test_negative_address_offset_turns_a_one_based_sheet_into_zero_based():
    """40001 风格点表要整体左移一时传的是负数：40001 -> 0。"""
    text = (",".join(HEADER) + "\n"
            "tcp-502,COVI-1,11,p1,hr,40001,uint16,ABCD,0,const,,false\n"
            "tcp-502,COVI-1,11,p2,hr,40002,uint16,ABCD,0,const,,false\n")
    proj = read_csv(text, ports=ALL_PORTS, address_offset=-40001)
    assert [p.address for p in proj.channels[0].devices[0].points] == [0, 1]
    assert error_lines(validate_project(proj)) == []


def test_loader_does_not_range_check_addresses_validate_does():
    """偏移用错方向时 loader 不替用户猜：地址负数由 validate 落 [address]。"""
    proj = read_csv(ROWS, ports=ALL_PORTS, address_offset=-1)
    assert proj.channels[0].devices[0].points[0].address == -1
    lines = error_lines(validate_project(proj))
    assert any(line.startswith("[address] ") for line in lines)


# ---- 逐列口径 ----

def test_readonly_accepts_both_whitelists_case_insensitively():
    """大小写混写与两侧空格都要认：厂商表里 TRUE/Yes/off 都出现过，认不出来就是错值。"""
    true_values = ["TRUE", "true", "  true  ", "YES", "Yes", "y", "ON", "on", "1"]
    false_values = ["False", "false", " FALSE ", "NO", "no", "n", "N", "OFF", "off", "0"]
    rows = [f'tcp-502,D,1,t{ i },co,{ i },bool,ABCD,true,const,,{ value }'
            for i, value in enumerate(true_values)]
    rows += [f'tcp-502,D,1,f{ i },co,{ 100 + i },bool,ABCD,true,const,,{ value }'
             for i, value in enumerate(false_values)]
    text = ",".join(HEADER) + "\n" + "\n".join(rows) + "\n"
    proj = read_csv(text, ports=ALL_PORTS)
    assert [p.readonly for _, _, p in proj.iter_points()] == \
        [True] * len(true_values) + [False] * len(false_values)


@pytest.mark.parametrize("value", ["TURE", "01", "2", ""])
def test_unrecognised_readonly_is_rejected_with_line_number(value):
    """拼错的、数字串不像布尔的、留空的 readonly 一律报错：静默当 False 就是让
    主站能改写一个真设备只读的点位，那是事故不是格式问题（空串也不给默认值）。"""
    bad = ROWS + f"tcp-502,风机-1,12,X,hr,9,uint16,ABCD,0,const,,{value}\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(bad, ports=ALL_PORTS)
    assert str(excinfo.value) == (
        f"line 6: readonly {value!r} is not a boolean, "
        f"use one of 1/0/true/false/yes/no/on/off")


def test_readonly_roundtrip_writes_lowercase_and_reads_back_identical():
    """write_csv 写的是小写 true/false，正好落在新规则的白名单里：导出文件必须
    仍然读得回来且全等，否则我们自己的导出会把自己判成非法点表。"""
    text = (",".join(HEADER) + "\n"
            "tcp-502,D,1,p1,co,0,bool,ABCD,true,const,,TRUE\n"
            "tcp-502,D,1,p2,co,1,bool,ABCD,true,const,, 1 \n"
            "tcp-502,D,1,p3,co,2,bool,ABCD,true,const,,off\n"
            "tcp-502,D,1,p4,co,3,bool,ABCD,true,const,,n\n")
    proj = read_csv(text, ports=ALL_PORTS)
    assert [p.readonly for _, _, p in proj.iter_points()] == [True, True, False, False]
    out = write_csv(proj)
    assert [line.rsplit(",", 1)[-1] for line in out.splitlines()[1:]] == \
        ["true", "true", "false", "false"]
    assert read_csv(out, ports=ALL_PORTS) == proj


def test_sample_point_table_still_validates_clean():
    """严格 readonly 不该让任何合法点表失败：样例表读进来校验清单仍为空。"""
    assert error_lines(validate_project(read_csv(ROWS, ports=ALL_PORTS))) == []


def test_very_long_dirty_readonly_is_truncated_in_the_message():
    """与 address 同一条截断口径（裁决 N）：脏 readonly 也不许把线索挤出屏幕。"""
    junk = "T" * 400
    bad = ROWS + f"tcp-502,风机-1,12,X,hr,9,uint16,ABCD,0,const,,{junk}\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(bad, ports=ALL_PORTS)
    message = str(excinfo.value)
    assert message.startswith("line 6: readonly '")
    assert " is not a boolean, use one of 1/0/true/false/yes/no/on/off" in message
    assert "..." in message
    assert junk[:100] not in message


def test_empty_word_order_and_waveform_get_the_model_defaults():
    text = ",".join(HEADER) + "\ntcp-502,D,1,p1,hr,0,uint16,,0,,,false\n"
    pt = read_csv(text, ports=ALL_PORTS).channels[0].devices[0].points[0]
    assert (pt.word_order, pt.waveform, pt.wave_param, pt.readonly) == \
        ("ABCD", "const", {}, False)


def test_empty_initial_is_left_for_validate_to_flag():
    """loader 不判语义：initial 空串原样带过去，validate 落一条 [initial]。"""
    text = ",".join(HEADER) + "\ntcp-502,D,1,p1,hr,0,uint16,ABCD,,const,,false\n"
    proj = read_csv(text, ports=ALL_PORTS)
    pt = proj.channels[0].devices[0].points[0]
    assert pt.initial == ""
    lines = error_lines(validate_project(proj))
    assert len(lines) == 1 and lines[0].startswith("[initial] ")


def test_wave_param_forgives_spaces_around_keys_and_values():
    text = (",".join(HEADER) + "\n"
            "tcp-502,D,1,p1,hr,0,uint16,ABCD,0,sine, base = 20 ; amp=15 ,false\n")
    pt = read_csv(text, ports=ALL_PORTS).channels[0].devices[0].points[0]
    assert pt.wave_param == {"base": "20", "amp": "15"}
    assert format_wave_param(pt.wave_param) == "base=20;amp=15"


def test_wave_param_roundtrip_keeps_order_and_values():
    params = {"base": "20", "amp": "15", "period": "60", "phase": "0"}
    assert parse_wave_param(format_wave_param(params)) == params


# ---- 厂商 Excel 导出的常见形状 ----

def test_quoted_comma_in_name_survives_text_and_file_roundtrip(tmp_path):
    """中文名里带逗号：csv 模块自己加引号，往返不能吞掉半个字。"""
    text = (",".join(HEADER) + "\n"
            'tcp-502,COVI-1,11,"CO,浓度",hr,0,float32,CDAB,12.5,sine,base=20;amp=15;period=60,true\n'
            'tcp-502,COVI-1,11,"状态,描述",di,0,bool,ABCD,true,const,"",false\n')
    proj = read_csv(text, ports=ALL_PORTS)
    pts = proj.channels[0].devices[0].points
    assert [p.name for p in pts] == ["CO,浓度", "状态,描述"]
    again = read_csv(write_csv(proj), ports=ALL_PORTS)
    assert again == proj
    assert '"CO,浓度"' in write_csv(proj)                 # 写出时确实带引号
    path = tmp_path / "comma.csv"
    write_csv_file(proj, path)
    assert path.read_text(encoding="utf-8").count("CO,浓度") == 1
    assert read_csv_file(path, ports=ALL_PORTS) == proj


def test_write_csv_uses_exactly_the_header_contract_and_lf():
    text = write_csv(read_csv(ROWS, ports=ALL_PORTS))
    assert text.splitlines()[0] == ",".join(HEADER)
    assert "\r" not in text
    assert len(text.splitlines()) == 5                      # 表头 + 4 个点位


# ---- 裁决 8：三个身份列不许为空（Excel 少拖一格就会交出这样一张表）----

def _row(**overrides) -> str:
    """一行合法点表，测试按需把某一列留空。"""
    cells = {"channel": "tcp-502", "device": "COVI-1", "unit_id": "11", "name": "CO",
             "area": "hr", "address": "0", "datatype": "uint16", "word_order": "ABCD",
             "initial": "0", "waveform": "const", "wave_param": "", "readonly": "false"}
    cells.update(overrides)
    return ",".join(HEADER) + "\n" + ",".join(cells[col] for col in HEADER) + "\n"


@pytest.mark.parametrize("column", ["channel", "device", "name"])
def test_empty_identity_column_is_rejected_with_line_number(column):
    """空 channel 会建出一个名为 '' 的通道，空 device/name 把空串原样带进模型。

    后果是点位挂到无名通道上：GUI 列表、报文解析、日志三边都对不上号，而从站照样
    起得来。行号只有 loader 知道，所以这条必须停在 read_csv，不许挪进 validate。
    """
    with pytest.raises(ValueError) as excinfo:
        read_csv(_row(**{column: ""}), ports=ALL_PORTS)
    assert str(excinfo.value) == f"line 2: {column} must not be empty"


@pytest.mark.parametrize("column", ["channel", "device", "name"])
def test_whitespace_only_identity_column_is_also_empty(column):
    """单元格只有空格：`_rows` 已经 strip 过，判据必须看 strip 之后的长度。"""
    with pytest.raises(ValueError, match=f"line 2: {column} must not be empty"):
        read_csv(_row(**{column: "   "}), ports=ALL_PORTS)


def test_two_rows_with_empty_channel_do_not_merge_into_an_unnamed_channel():
    """实测旧行为：两行空 channel 会并进同一个 `''` 通道，现在两行都拒绝。"""
    text = (",".join(HEADER) + "\n"
            ",COVI-1,11,p1,hr,0,uint16,ABCD,0,const,,false\n"
            ",COVI-1,11,p2,hr,1,uint16,ABCD,0,const,,false\n")
    with pytest.raises(ValueError) as excinfo:
        read_csv(text, ports=ALL_PORTS)
    assert str(excinfo.value) == "line 2: channel must not be empty"


def test_empty_name_is_not_left_for_validate_to_notice():
    """validate 对空 name 一条 error 都不出（实测），空串能一路走到模型里：
    所以挡它的地方只能是 loader，这条断言同时钉住"两边都没静默放行"。"""
    proj = Project(channels=[TcpChannel(name="c", devices=[Device(
        name="D", unit_id=1, points=[Point("", "hr", 0, "uint16")])])])
    assert error_lines(validate_project(proj)) == []
    with pytest.raises(ValueError, match="line 2: name must not be empty"):
        read_csv(_row(name=""), ports=ALL_PORTS)


def test_other_columns_may_stay_empty():
    """只有三个身份列不许空：word_order/waveform/wave_param/initial 留空仍按默认走。"""
    proj = read_csv(_row(word_order="", waveform="", wave_param="", initial=""),
                    ports=ALL_PORTS)
    pt = proj.channels[0].devices[0].points[0]
    assert (pt.word_order, pt.waveform, pt.wave_param, pt.initial) == \
        ("ABCD", "const", {}, "")


# ---- 裁决 N 在本轮的四处漏网：消息必须走 brief() 截断 ----

def test_header_mismatch_message_truncates_the_actual_header():
    """200 列的厂商表：旧写法实测 1020 个字符（期望表头是我们自己的常量，不截）。"""
    header = ",".join(f"col{i}" for i in range(200))
    with pytest.raises(ValueError) as excinfo:
        read_csv(header + "\n", ports=ALL_PORTS)
    message = str(excinfo.value)
    assert f"expected {','.join(HEADER)}" in message
    assert "..." in message
    assert "col99" not in message
    assert len(message) < 300


def test_device_conflict_message_truncates_both_device_names():
    """实测 654 字符：冲突的两个设备名都要 brief()，判定逻辑不变。"""
    junk = "D" * 400
    text = ROWS + f"tcp-502,{junk},11,电压,hr,4,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError) as excinfo:
        read_csv(text, ports=ALL_PORTS)
    message = str(excinfo.value)
    assert message.startswith("line 6: unit_id 11 is already used by device 'COVI-1', got '")
    assert "..." in message
    assert junk[:100] not in message
    assert len(message) < 300


def point_named(project, name):
    for ch in project.channels:
        for dev in ch.devices:
            for pt in dev.points:
                if pt.name == name:
                    return pt
    raise AssertionError(f"no point named {name!r}")


@pytest.mark.parametrize("extra, expected", [("_BOOL_TRUE", True), ("_BOOL_FALSE", False)])
def test_readonly_column_and_initial_share_one_bool_whitelist(monkeypatch, extra, expected):
    """裁决 E：bool 白名单只许有一份。给 model 那张表加一个词，readonly 列必须跟着认。

    实测旧状态：`model._BOOL_TRUE/_BOOL_FALSE` 与 `csv_io._TRUE/_FALSE` 逐字符相同地并存，
    没有任何测试或导入期断言钉相等（initial 走 `parse_value(BOOL, ...)`，readonly 走 csv
    自己那张表）。将来任一边加一个 token，同一张 CSV 里两列对同一个词就给相反答案——
    `readonly` 判成"不是布尔"而 initial 判成 True，现场改点表的人会当成解析器的 bug 去调。
    """
    monkeypatch.setattr(model, extra, getattr(model, extra) | {"sure"})
    text = ROWS + "tcp-502,COVI-1,11,电压,hr,4,uint16,ABCD,0,const,,sure\n"
    project = read_csv(text, ports=ALL_PORTS)
    assert point_named(project, "电压").readonly is expected


def test_csv_io_does_not_keep_a_second_bool_table():
    """结构判据：csv_io 里不许再摆第二张表——收敛靠"没有副本"，不靠"两份恰好相等"。"""
    assert "_TRUE" not in vars(csv_io)
    assert "_FALSE" not in vars(csv_io)
