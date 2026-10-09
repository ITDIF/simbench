"""project.json 读写：往返全等、版本闸门、row 不外泄、手改的脏 initial 只能落 validate。"""

import json

import pytest

from modslave.project.json_io import VERSION, from_dict, load, save, to_dict, to_json
from modslave.project.model import Device, Point, Project, TcpChannel
from modslave.project.validate import error_lines, validate_project


def sample():
    pt = Point(name="CO", area="hr", address=0, datatype="float32", initial="12.5",
               word_order="CDAB", waveform="sine",
               wave_param={"base": "20", "amp": "15", "period": "60"}, readonly=True)
    dev = Device(name="COVI-1", unit_id=11, points=[pt])
    ch = TcpChannel(name="tcp-502", host="0.0.0.0", port=502, devices=[dev])
    return Project(channels=[ch], tick_ms=200)


def test_roundtrip_is_identical():
    proj = sample()
    assert from_dict(to_dict(proj)) == proj


def test_json_text_roundtrip_keeps_every_field():
    pt = from_dict(json.loads(to_json(sample()))).channels[0].devices[0].points[0]
    assert (pt.name, pt.area, pt.address, pt.datatype, pt.initial) == \
        ("CO", "hr", 0, "float32", "12.5")
    assert (pt.word_order, pt.waveform, pt.wave_param, pt.readonly) == \
        ("CDAB", "sine", {"base": "20", "amp": "15", "period": "60"}, True)


def test_tick_ms_and_channel_defaults_survive():
    back = from_dict(to_dict(sample()))
    assert back.tick_ms == 200
    assert (back.channels[0].host, back.channels[0].port) == ("0.0.0.0", 502)


def test_row_number_is_not_serialized():
    proj = sample()
    proj.channels[0].devices[0].points[0].row = 9
    data = to_dict(proj)
    assert "row" not in data["channels"][0]["devices"][0]["points"][0]
    assert from_dict(data).channels[0].devices[0].points[0].row is None


def test_file_roundtrip(tmp_path):
    path = tmp_path / "project.json"
    save(sample(), path)
    assert load(path) == sample()


def test_wrong_version_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 99, "channels": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.json")


def test_channel_without_type_defaults_to_tcp():
    data = to_dict(sample())
    data["channels"][0].pop("type")
    assert from_dict(data).channels[0].type == "tcp"


def test_point_without_optional_fields_uses_defaults():
    data = {"version": 1, "tick_ms": 100, "channels": [{"name": "c", "port": 502,
            "devices": [{"name": "D", "unit_id": 1,
                         "points": [{"name": "p", "area": "hr", "address": 0,
                                     "datatype": "uint16"}]}]}]}
    pt = from_dict(data).channels[0].devices[0].points[0]
    assert (pt.initial, pt.word_order, pt.waveform, pt.readonly) == ("0", "ABCD", "const", False)


# ---- 收尾自查 + 裁决 B 的连带点：project.json 可能被现场手改过 ----

def _point_raw(value, datatype, area="hr"):
    """一条 initial 被改成任意 JSON 值（number/bool/null）的工程，模拟手改过的文件。"""
    return {"version": 1, "tick_ms": 100,
            "channels": [{"name": "c", "host": "0.0.0.0", "port": 502,
                          "devices": [{"name": "D", "unit_id": 1,
                                       "points": [{"name": "p", "area": area,
                                                   "address": 0, "datatype": datatype,
                                                   "initial": value}]}]}]}


def _only_point(data):
    return data.channels[0].devices[0].points[0]


def test_version_constant_is_one_and_written_into_every_export():
    assert VERSION == 1
    assert to_dict(sample())["version"] == 1
    assert json.loads(to_json(sample()))["version"] == 1


def test_load_of_exported_file_without_version_raises(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load(path)


def test_json_number_initial_is_stringified_by_loader_and_rejected_by_validate():
    """`"initial": 100.5`（JSON number）：loader 不抛、不截断，交给 validate 落 [initial]。

    裁决 B 定死 parse_value 只收字符串，所以脏值必须在这里被 parse_value 拦住；
    loader 若改成 str() 之外还截断，这条就会变成静默的 100（= 平台 bug）。
    """
    proj = from_dict(_point_raw(100.5, "int16"))
    pt = _only_point(proj)
    assert pt.initial == "100.5"                      # 原样成字符串，没有变成 "100"
    lines = error_lines(validate_project(proj))
    assert len(lines) == 1
    assert lines[0].startswith("[initial] ")
    assert "int16" in lines[0]
    assert "Traceback" not in lines[0]


def test_json_bool_true_initial_is_still_read_as_true():
    """`"initial": true` + datatype=bool：`str(True)`="True" 必须走 bool 白名单认成 True。

    这条是给 parse_value 的白名单上的锁：将来谁把白名单改成大小写敏感或删掉 "true"，
    这个行为会静默翻转成 False（只读点位变成"初值 False"），测试先炸。
    """
    proj = from_dict(_point_raw(True, "bool", area="co"))
    pt = _only_point(proj)
    assert pt.initial == "True"
    assert pt.typed_initial() is True
    assert error_lines(validate_project(proj)) == []


@pytest.mark.parametrize("datatype,area", [("int16", "hr"), ("bool", "co")])
def test_json_null_initial_never_crashes_and_only_reports_initial(datatype, area):
    """`"initial": None`：`str(None)`="None"，int16 与 bool 两条路都只能落一条 [initial]。"""
    proj = from_dict(_point_raw(None, datatype, area=area))
    assert _only_point(proj).initial == "None"
    lines = error_lines(validate_project(proj))
    assert len(lines) == 1
    assert lines[0].startswith("[initial] ")
    assert datatype in lines[0]


def test_row_key_is_absent_from_json_text_and_saved_file(tmp_path):
    proj = sample()
    _only_point(proj).row = 7
    assert "row" not in to_json(proj)
    path = tmp_path / "project.json"
    save(proj, path)
    text = path.read_text(encoding="utf-8")
    assert "row" not in text
    assert '"initial": "12.5"' in text                # 初值在文件里始终是字符串


# ---- readonly 的布尔口径（与 CSV 那条同一个洞）----

def _readonly_raw(value, area="hr", datatype="uint16"):
    """一条 readonly 被改成任意 JSON 值的工程，模拟手改过的 project.json。"""
    return {"version": 1, "tick_ms": 100,
            "channels": [{"name": "c", "host": "0.0.0.0", "port": 502,
                          "devices": [{"name": "D", "unit_id": 1,
                                       "points": [{"name": "p", "area": area,
                                                   "address": 0, "datatype": datatype,
                                                   "initial": "0",
                                                   "readonly": value}]}]}]}


@pytest.mark.parametrize("value", [
    "false", "FALSE", " No ", "n", "off", "0",        # 字符串本身合法，判成 False
    False, "true", "TRUE", " Yes ", "y", "on", "1", True])   # 其余判成 True
def test_readonly_accepts_python_bool_and_whitelisted_strings(value):
    """`"false"` 是非空字符串，`bool()` 一律判 True：只读点位会被静默点亮。

    改成白名单判定后两侧都不能翻：翻成全 True 主站写不进任何点，翻成全 False 就把
    我们自己设的只读保护悄悄关掉（与 CSV 那条同一个事故方向）。
    """
    expected = value is True or str(value).strip().lower() in \
        {"true", "yes", "y", "on", "1"}
    assert _only_point(from_dict(_readonly_raw(value))).readonly is expected


@pytest.mark.parametrize("value", [None, 2, 1, 0, "", "TURE", "01", [], [True], 1.0])
def test_readonly_rejects_everything_else(value):
    """null、数字、拼错的 token、空串、列表都抛 ValueError：`bool()` 对它们要么静默
    False 要么静默 True，而静默错值会被当成平台 bug 去调（宁可报错，绝不猜）。"""
    with pytest.raises(ValueError) as excinfo:
        from_dict(_readonly_raw(value))
    assert str(excinfo.value) == f"readonly must be a boolean, got {value!r}"


@pytest.mark.parametrize("area,datatype", [("hr", "uint16"), ("ir", "uint16")])
def test_dirty_readonly_raises_at_the_loader_not_as_a_validate_line(area, datatype):
    """脏 readonly 在 loader 就炸，不是一条 [readonly] 校验清单：load() 的调用点只
    expect ValueError，所以消息必须自己把字段名说清楚，不能只说 "is not valid"。"""
    with pytest.raises(ValueError, match="readonly"):
        from_dict(_readonly_raw("ture", area=area, datatype=datatype))


def test_missing_readonly_still_defaults_to_false():
    """缺字段仍默认 False：我们自己的导出永远写这个字段，缺它说明是手改的，但默认
    False 不改变任何已有值的语义，保留（与上面 defaults 那条同口径）。"""
    data = _readonly_raw(True)
    del data["channels"][0]["devices"][0]["points"][0]["readonly"]
    assert _only_point(from_dict(data)).readonly is False


def test_very_long_dirty_readonly_is_truncated_in_the_message():
    """截断口径同裁决 N：几百个字符的脏 readonly 不许把真正的线索挤出屏幕。"""
    junk = "T" * 400
    with pytest.raises(ValueError) as excinfo:
        from_dict(_readonly_raw(junk))
    message = str(excinfo.value)
    assert message.startswith("readonly must be a boolean, got '")
    assert "..." in message
    assert len(message) < 200


def test_exported_readonly_roundtrips_unchanged():
    """写出去的是 JSON 布尔（不是字符串），严格口径必须先认得自己的导出格式，否则
    save/load 往返会把工程判成非法文件。"""
    assert _only_point(from_dict(to_dict(sample()))).readonly is True
    writable = Project(channels=[TcpChannel(name="c", devices=[Device(
        name="D", unit_id=1, points=[Point("p", "hr", 0, "uint16", readonly=False)])])])
    assert _only_point(from_dict(to_dict(writable))).readonly is False
