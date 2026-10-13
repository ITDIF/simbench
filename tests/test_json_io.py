"""project.json 读写：往返全等、版本闸门、row 不外泄、手改的脏 initial 只能落 validate。"""

import json

import pytest

from simbench.project.json_io import VERSION, from_dict, load, save, to_dict, to_json
from simbench.project.model import Device, Point, Project, TcpChannel
from simbench.project.validate import error_lines, validate_project


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


# ---- 裁决 2：整数字段不许静默截断（`int()` 会把 1.9 变成 1）----

def _doc():
    """一份最小合法 project.json，测试按需把某一个字段换成脏值。"""
    return {"version": 1, "tick_ms": 100,
            "channels": [{"name": "c", "host": "0.0.0.0", "port": 502,
                          "devices": [{"name": "D", "unit_id": 1,
                                        "points": [{"name": "p", "area": "hr",
                                                   "address": 0, "datatype": "uint16",
                                                   "initial": "0"}]}]}]}


def _point_part(doc):
    return doc["channels"][0]["devices"][0]["points"][0]


@pytest.mark.parametrize("value", [1.9, None, [3], {}, "0", True, False])
def test_address_must_be_a_real_int(value):
    """`int(1.9)`=1、`int(True)`=1、`int(None)` 直接 TypeError：四种入口现在统一 ValueError。

    地址被截断等于读写错寄存器，脏值必须当场问人，不许由模拟器替用户猜一个"看着合法"
    的地址（与裁决 B 对 initial 的口径同向，也补掉裁决 O 只堵了 validate 的半边）。
    """
    data = _doc()
    _point_part(data)["address"] = value
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == f"point 'p': address must be an integer, got {value!r}"


@pytest.mark.parametrize("value", [11.9, True, None, [1], "1"])
def test_unit_id_must_be_a_real_int(value):
    """`unit_id: 11.9`→11、`true`→1 会让从站答错站号：主站扫到一台不存在的设备。"""
    data = _doc()
    data["channels"][0]["devices"][0]["unit_id"] = value
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == f"device 'D': unit_id must be an integer, got {value!r}"


@pytest.mark.parametrize("value", [502.7, None, "502", [502]])
def test_port_must_be_a_real_int(value):
    """`port: 502.7`→502 是静默改了监听端口；端口绑错现场只看见"连不上"。"""
    data = _doc()
    data["channels"][0]["port"] = value
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == f"channel 'c': port must be an integer, got {value!r}"


@pytest.mark.parametrize("value", [100.5, None, "100", True])
def test_tick_ms_must_be_a_real_int(value):
    """tick_ms 截断会让波形周期静默漂移，同样走 _require_int。"""
    data = _doc()
    data["tick_ms"] = value
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == f"project: tick_ms must be an integer, got {value!r}"


@pytest.mark.parametrize("value", [0, 1, 65535])
def test_plain_integers_still_load_unchanged(value):
    """严格 gate 不许误杀合法值：address 与 unit_id 的整数照常原样读进来。"""
    data = _doc()
    _point_part(data)["address"] = value
    data["channels"][0]["devices"][0]["unit_id"] = value
    proj = from_dict(data)
    assert proj.channels[0].devices[0].unit_id == value
    assert proj.channels[0].devices[0].points[0].address == value


def test_very_long_dirty_int_is_truncated_in_the_message():
    """裁决 N：_require_int 的消息也必须走 brief()，几百个字符的脏值不许刷屏。"""
    data = _doc()
    _point_part(data)["address"] = "9" * 400
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    message = str(excinfo.value)
    assert message.startswith("point 'p': address must be an integer, got '")
    assert "..." in message
    assert len(message) < 200


# ---- 裁决 3：形状不对也必须是 ValueError，Task 12 的装载口只 except ValueError ----

def test_top_level_must_be_an_object():
    with pytest.raises(ValueError) as excinfo:
        from_dict([{"version": 1}])
    assert str(excinfo.value) == "project.json project must be an object, got list"


def test_top_level_scalar_must_be_an_object():
    with pytest.raises(ValueError) as excinfo:
        from_dict("project")
    assert str(excinfo.value) == "project.json project must be an object, got str"


@pytest.mark.parametrize("value", [{"a": 1}, "c", 3, None])
def test_channels_must_be_a_list(value):
    """`channels` 是 dict 时旧写法直接 `AttributeError`/逐键解析，现在一条说清是哪一层。"""
    data = _doc()
    data["channels"] = value
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == \
        f"project.json channels must be a list, got {type(value).__name__}"


@pytest.mark.parametrize("section,label", [("channels", "channel"),
                                            ("devices", "device"),
                                            ("points", "point")])
def test_each_level_must_be_an_object(section, label):
    """channel / device / point 三个层级都必须是 JSON object：脏一层就说清是哪一层。"""
    data = _doc()
    holder = data
    if section == "devices":
        holder = data["channels"][0]
    elif section == "points":
        holder = data["channels"][0]["devices"][0]
    holder[section] = ["x"]
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == f"project.json {label} must be an object, got str"


def test_devices_must_be_a_list():
    """实测过的逃逸口：`"devices": {...}` 之前抛 `TypeError: string indices must be integers`。"""
    data = _doc()
    data["channels"][0]["devices"] = {"name": "D"}
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == "project.json devices must be a list, got dict"


def test_points_must_be_a_list():
    data = _doc()
    data["channels"][0]["devices"][0]["points"] = {"name": "p"}
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == "project.json points must be a list, got dict"


def test_wave_param_must_be_an_object():
    """`wave_param: [1, 2]` 之前让 `dict()` 抛 TypeError 逃出 loader。"""
    data = _doc()
    _point_part(data)["wave_param"] = [1, 2]
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == "project.json wave_param must be an object, got list"


# ---- 裁决 N 在本轮的漏网之鱼：version 的渲染 ----

def test_dirty_version_is_truncated_in_the_message():
    data = _doc()
    junk = "V" * 400
    data["version"] = junk
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    message = str(excinfo.value)
    assert message.startswith("unsupported project.json version '")
    assert "..." in message
    assert junk[:100] not in message
    assert len(message) < 200


# ---- loader 契约的最后一类：缺必填键（现场手改 project.json 删掉一行）----

@pytest.mark.parametrize("key", ["name", "area", "address", "datatype"])
def test_point_missing_required_key_is_a_domain_error(key):
    """`raw["area"]` 直接下标取值时缺键抛 `KeyError`，与 loader 的 ValueError 契约不符。

    loader 的契约（`load` 的 docstring + Task 12 的装载口）是"文件读不到 → OSError，
    读出来了但读不懂 → ValueError"，装载口只 `except ValueError`：缺键是纯粹的"读不懂"，
    逃逸成 KeyError 就是当场 traceback，而且那条消息（`KeyError: 'area'`）不带点位与
    设备，现场根本不知道是工程文件里哪一行被删了。消息按层级点名（同 `_require_object`）。
    """
    data = _doc()
    del _point_part(data)[key]
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == \
        f"project.json point #1 of device 'D' is missing required key '{key}'"


@pytest.mark.parametrize("key", ["name", "unit_id"])
def test_device_missing_required_key_is_a_domain_error(key):
    data = _doc()
    del data["channels"][0]["devices"][0][key]
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == \
        f"project.json device #1 of channel 'c' is missing required key '{key}'"


def test_channel_missing_required_key_is_a_domain_error():
    data = _doc()
    del data["channels"][0]["name"]
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == \
        "project.json channel #1 is missing required key 'name'"


def test_shape_gate_runs_before_the_missing_key_gate():
    """`"point": "p"` 的错是"形状"不是"缺键"：消息必须先说 must be an object。

    `key not in "p"` 在字符串上是子串判定，不 gate 形状就会把形状错说成缺键错，
    现场按缺键去补字段反而补出第二个错。
    """
    data = _doc()
    data["channels"][0]["devices"][0]["points"] = ["p"]
    with pytest.raises(ValueError) as excinfo:
        from_dict(data)
    assert str(excinfo.value) == "project.json point must be an object, got str"


def test_complete_document_still_loads_after_the_missing_key_gate():
    """gate 不许误杀：`_doc()` 这种"只带必填键"的合法文档照常加载（可选键有默认值）。"""
    proj = from_dict(_doc())
    assert proj.channels[0].devices[0].points[0].name == "p"
