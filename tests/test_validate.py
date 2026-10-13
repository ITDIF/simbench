# tests/test_validate.py
"""启动前校验：一次报全 + 一条脏值不许把清单打成 traceback。"""

from dataclasses import FrozenInstanceError

import pytest

from simbench.project import validate
from simbench.project.model import (
    DATATYPES, Device, Point, Project, TcpChannel, WAVEFORMS, word_orders_allowed,
)
from simbench.project.validate import Issue, error_lines, validate_project


def _p(**kw):
    base = dict(name="p", area="hr", address=0, datatype="uint16", initial="0")
    base.update(kw)
    return Point(**base)


def _proj(*points, port=502, unit=11, name="D1", channel="c1"):
    dev = Device(name=name, unit_id=unit, points=list(points))
    ch = TcpChannel(name=channel, host="127.0.0.1", port=port, devices=[dev])
    return Project(channels=[ch])


def codes(project):
    return {(i.code, i.where) for i in validate_project(project) if i.level == "error"}


# ---- 计划 Task 5 Step 1 的用例 ----

def test_clean_project_has_no_issues():
    assert validate_project(_proj(_p(address=0), _p(name="q", address=5))) == []


def test_overlap_in_same_area():
    # 同一区里撞上同一个寄存器才算重叠（计划里这行写的 address=1 是笔误：
    # uint16 只占 1 个寄存器，紧邻本来就合法——事实 5“点表不需要把地址铺满”）
    assert ("overlap", "c1/D1/q") in codes(_proj(_p(), _p(name="q", address=0)))
    assert codes(_proj(_p(), _p(name="q", address=1))) == set()


def test_same_address_in_different_area_is_ok():
    assert codes(_proj(_p(area="hr"), _p(name="q", area="ir"))) == set()


def test_float32_span_overlaps_next_point():
    got = codes(_proj(_p(datatype="float32", initial="1.0"), _p(name="q", address=1)))
    assert ("overlap", "c1/D1/q") in got


def test_bit_area_overlap_is_checked_per_bit_not_per_register():
    """两个线圈号相同才算重叠；16 个线圈共用一个寄存器是正常的。"""
    assert codes(_proj(_p(area="co", datatype="bool", address=3),
                       _p(name="q", area="co", datatype="bool", address=3))) == \
        {("overlap", "c1/D1/q")}
    assert codes(_proj(_p(area="co", datatype="bool", address=0),
                       _p(name="q", area="co", datatype="bool", address=1))) == set()


def test_type_area_mismatch():
    got = codes(_proj(_p(datatype="bool", area="hr"),
                      _p(name="q", area="co", datatype="uint16")))
    assert ("area_type", "c1/D1/p") in got
    assert ("area_type", "c1/D1/q") in got


def test_area_type_mismatch_still_reserves_its_address():
    """判完类型/区不匹配不许顺手 return：本点位照样占位，
    否则后面那个“自己没毛病”的点位藏着重叠报不出来，只好转给 pymodbus 抛无行号的 TypeError。
    """
    assert codes(_proj(_p(name="bad", datatype="bool", address=4),
                       _p(name="ok", address=4))) == {
        ("area_type", "c1/D1/bad"), ("overlap", "c1/D1/ok")}


def test_unknown_datatype_and_area():
    got = codes(_proj(_p(datatype="wchar"), _p(name="q", address=5, area="xr")))
    assert ("datatype", "c1/D1/p") in got
    assert ("area", "c1/D1/q") in got


def test_initial_out_of_range_is_error_with_row_number():
    pt = _p(datatype="int16", initial="40000")
    pt.row = 7
    issue = [i for i in validate_project(_proj(pt)) if i.code == "initial"][0]
    assert "int16" in issue.message and "line 7" in issue.where


def test_word_order_on_64bit_is_error_on_16bit_is_warning():
    assert ("word_order", "c1/D1/p") in codes(
        _proj(_p(datatype="int64", word_order="CDAB", initial="1")))
    issues = validate_project(_proj(_p(datatype="int16", word_order="CDAB")))
    assert [i for i in issues if i.level == "error"] == []
    assert any(i.code == "word_order_ignored" for i in issues)


def test_address_out_of_range():
    assert ("address", "c1/D1/p") in codes(_proj(_p(address=65536)))
    assert ("address", "c1/D1/p") in codes(_proj(_p(address=-1)))


def test_unit_id_range_and_duplicate():
    ch = TcpChannel(name="c1", host="127.0.0.1", port=502, devices=[
        Device(name="A", unit_id=300, points=[_p()]),
        Device(name="B", unit_id=247, points=[_p()]),
        Device(name="C", unit_id=247, points=[_p()])])
    got = {i.code for i in validate_project(Project(channels=[ch]))
           if i.level == "error"}
    assert {"unit_range", "unit_duplicated"} <= got


def test_port_zero_and_out_of_range_are_errors():
    assert ("port", "c1") in codes(_proj(_p(), port=0))
    assert ("port", "c1") in codes(_proj(_p(), port=70000))


def test_duplicate_channel_names():
    ch = TcpChannel(name="c1", host="127.0.0.1", port=502, devices=[])
    ch2 = TcpChannel(name="c1", host="127.0.0.1", port=503, devices=[])
    got = {i.code for i in validate_project(Project(channels=[ch, ch2]))
           if i.level == "error"}
    assert "channel_duplicated" in got


def test_two_channels_bound_to_the_same_host_and_port_are_reported_up_front():
    """同 host+port 在启动第二阶段才炸 bind，Windows 只给一句 10048 不带通道名：
    现场看不出是点表配置错。静态就能查出的冲突必须静态报（裁决 R）。
    """
    chs = [TcpChannel(name="tcp-502", host="0.0.0.0", port=502, devices=[]),
           TcpChannel(name="tcp-502-again", host="0.0.0.0", port=502, devices=[])]
    issues = [i for i in validate_project(Project(channels=chs))
              if i.code == "port_duplicated"]
    assert len(issues) == 1
    assert issues[0].level == "error"
    assert issues[0].where == "tcp-502-again"        # 后加入那个是占用别人的一方
    assert issues[0].message == "port 502 on host 0.0.0.0 already used by channel tcp-502"


def test_same_port_on_different_hosts_is_not_a_conflict():
    """0.0.0.0:502 与 127.0.0.1:502 各自监听，不算撞；只有同 host 同 port 才算。"""
    chs = [TcpChannel(name="wildcard", host="0.0.0.0", port=502, devices=[]),
           TcpChannel(name="loopback", host="127.0.0.1", port=502, devices=[])]
    assert codes(Project(channels=chs)) == set()
    # 第三个通道挤回 0.0.0.0:502：只有它一条，指向最先占位的 wildcard，loopback 不被牵连
    chs.append(TcpChannel(name="third", host="0.0.0.0", port=502, devices=[]))
    conflicts = [i for i in validate_project(Project(channels=chs))
                 if i.code == "port_duplicated"]
    assert [(i.where, i.message) for i in conflicts] == [
        ("third", "port 502 on host 0.0.0.0 already used by channel wildcard")]


def test_dirty_port_does_not_stack_a_second_port_duplicated():
    """port 已被 "port" error 拦下（不是整数/越界），不再重复报冲突。"""
    chs = [TcpChannel(name="a", host="0.0.0.0", port="COM3", devices=[]),
           TcpChannel(name="b", host="0.0.0.0", port="COM3", devices=[]),
           TcpChannel(name="c", host="0.0.0.0", port=70000, devices=[]),
           TcpChannel(name="d", host="0.0.0.0", port=70000, devices=[])]
    got = [i.code for i in validate_project(Project(channels=chs)) if i.level == "error"]
    assert got == ["port", "port", "port", "port"]


def test_non_string_host_does_not_crash_the_conflict_check():
    """host 手改成 [] 时 (host, port) 连键都做不了：脏 host 不参与冲突判定，也不误判。"""
    chs = [TcpChannel(name="l1", host=[], port=502, devices=[]),
           TcpChannel(name="l2", host=[], port=502, devices=[]),
           TcpChannel(name="n", host=None, port=502, devices=[]),
           TcpChannel(name="ok", host="0.0.0.0", port=502, devices=[]),
           TcpChannel(name="dup", host="0.0.0.0", port=502, devices=[])]
    got = [(i.code, i.where) for i in validate_project(Project(channels=chs))
           if i.level == "error"]
    assert got == [("host", "l1"), ("host", "l2"), ("host", "n"),
                   ("port_duplicated", "dup")]


def test_dirty_host_is_one_error_and_never_a_traceback():
    """裁决 T：host 空串/None/列表各落一条 host error，校验器自己不许先崩。"""
    for dirty, rendered in (("", "''"), (None, "None"), ([], "[]")):
        ch = TcpChannel(name="c1", host=dirty, port=502, devices=[])
        issues = validate_project(Project(channels=[ch]))
        assert [(i.code, i.where, i.level) for i in issues] == [("host", "c1", "error")], dirty
        assert issues[0].message == f"host must be a non-empty string, got {rendered}"


def test_normal_hosts_are_accepted():
    for host in ("127.0.0.1", "0.0.0.0"):
        ch = TcpChannel(name="c1", host=host, port=502, devices=[])
        assert validate_project(Project(channels=[ch])) == [], host


def test_host_literal_syntax_is_not_validated_here():
    """裁决 T：`999.1.1.1` 不在这里判——绑不上端口时 OSError 已经够明确，
    而且字符串 IP 合法性判断容易误杀（主机名、IPv6 带 %zone、前导零写法都合法），
    我们不在校验层造第二套判定。它照样参与同 host+port 的冲突判定。
    """
    ch = TcpChannel(name="c1", host="999.1.1.1", port=502, devices=[])
    assert validate_project(Project(channels=[ch])) == []
    chs = [TcpChannel(name="a", host="999.1.1.1", port=502, devices=[]),
           TcpChannel(name="b", host="999.1.1.1", port=502, devices=[])]
    assert [(i.code, i.where) for i in validate_project(Project(channels=chs))] == \
        [("port_duplicated", "b")]


def test_dirty_hosts_stay_out_of_the_conflict_check():
    """两个 host="" 的通道各说自己的 host error，不再叠一条指向空 host 的 port_duplicated：
    同 `port_ok` 的裁决 O 口径，脏值一条说完。
    """
    chs = [TcpChannel(name="a", host="", port=502, devices=[]),
           TcpChannel(name="b", host="", port=502, devices=[])]
    got = [(i.code, i.where) for i in validate_project(Project(channels=chs))
           if i.level == "error"]
    assert got == [("host", "a"), ("host", "b")]


def test_wave_param_missing_extra_and_bad_range():
    a = _p(waveform="sine", wave_param={"base": "0", "amp": "1"})
    b = _p(name="b", address=5, waveform="sine",
           wave_param={"base": "0", "amp": "1", "period": "0"})
    c = _p(name="c", address=8, waveform="random",
           wave_param={"min": "20", "max": "10"})
    d = _p(name="d", address=12, waveform="sine",
           wave_param={"base": "0", "amp": "1", "period": "4", "freq": "1"})
    got = {i.code for i in validate_project(_proj(a, b, c, d)) if i.level == "error"}
    assert "wave_param" in got


def test_unknown_waveform():
    assert ("waveform", "c1/D1/p") in codes(_proj(_p(waveform="triangle")))


def test_error_lines_format():
    lines = error_lines(validate_project(_proj(_p(waveform="triangle"))))
    assert lines == ["[waveform] c1/D1/p: unknown waveform 'triangle'"]


# ---- 评审点名要补的路径（计划正文没有对应测试）----

def test_non_ascii_string_initial_is_collected_not_raised():
    """裁决 I/D：中文初值必须变成一条校验错误，不能把一次报全打成 traceback。"""
    pts = [_p(name="plate", datatype="string", initial="风机"),
           _p(name="bad", address=8, datatype="int16", initial="40000")]
    # 不抛异常，而且后面那个点位的错误也一并报出来（顺序即点位顺序）
    assert codes(_proj(*pts)) == {("initial", "c1/D1/plate"), ("initial", "c1/D1/bad")}
    issues = validate_project(_proj(*pts))
    assert "风机" in issues[0].message


def test_string_initial_occupies_its_registers_for_overlap():
    """算得出跨度时字符串照样占位："ABCD" 是 4 字节 2 个寄存器，1 号位已被占住。"""
    assert codes(_proj(_p(datatype="string", initial="ABCD"),
                       _p(name="q", address=1))) == {("overlap", "c1/D1/q")}


def test_wave_spec_and_waveforms_have_one_truth():
    """裁决 H：合法波形有两个来源就会漂移，validate 在导入期把关。"""
    assert validate.WAVEFORMS is WAVEFORMS               # 不是又抄一份清单
    assert set(validate.WAVE_SPEC) == set(WAVEFORMS)


def test_every_problem_is_reported_in_one_pass():
    """一次报全：类型/区不匹配的点位不许顺带吞掉别人的重叠与初值错误。"""
    ch = TcpChannel(name="c1", host="127.0.0.1", port=0, devices=[
        Device(name="D1", unit_id=300, points=[
            _p(name="a", area="hr", datatype="bool", initial="TURE"),
            _p(name="b", waveform="triangle"),
            _p(name="c", datatype="int16", initial="40000"),
            _p(name="d", address=5, datatype="wchar"),
        ])])
    got = {i.code for i in validate_project(Project(channels=[ch]))
           if i.level == "error"}
    assert {"port", "unit_range", "area_type", "initial", "overlap",
            "waveform", "datatype"} <= got


def test_a_dirty_long_initial_cannot_flood_the_report():
    """裁决 N 到 Task 5 才真正兑现：一个 300 位的脏值只占一行。"""
    prefix = "int16 out of range [-32768, 32767]: "
    issue = [i for i in validate_project(_proj(_p(datatype="int16", initial="9" * 300)))
             if i.code == "initial"][0]
    assert issue.message.startswith(prefix) and issue.message.endswith("...")
    assert len(issue.message) <= len(prefix) + 80 + len("...")


def test_word_order_errors_follow_the_model_allow_list():
    """字序判定与 model.word_orders_allowed 同源，不在这里另列类型名清单。"""
    assert ("word_order", "c1/D1/p") in codes(          # spec 修正 1：string 只支持 ABCD
        _proj(_p(datatype="string", initial="AB", word_order="CDAB")))
    assert codes(_proj(_p(datatype="int64", initial="1", word_order="DCBA"))) == set()
    assert ("word_order", "c1/D1/p") in codes(          # 拼错的字序 token 也是 error
        _proj(_p(word_order="ABDC")))
    assert word_orders_allowed("string") == ("ABCD",)


def test_single_register_group_is_derived_from_the_model():
    """_SINGLE 手抄一份类型名就是第二份真相（同裁决 M 对 codec._NUMERIC 的要求）。"""
    assert validate._SINGLE == {d for d in DATATYPES
                                if d != "string" and word_orders_allowed(d) == ("ABCD",)}
    assert validate._SINGLE == {"bool", "int16", "uint16", "reg16"}
    assert "string" not in validate._SINGLE       # string 填错字序是 error，不是 warning


def test_dirty_wave_param_values_are_reported_not_raised():
    """非数字、None、列表型 values 都必须落成 wave_param 错误，而不是 traceback。"""
    pts = [
        _p(name="a", waveform="sine", wave_param={"base": "0", "amp": "x", "period": "4"}),
        _p(name="b", address=5, waveform="sine",
           wave_param={"base": None, "amp": "1", "period": "4"}),
        _p(name="c", address=8, waveform="random", wave_param={"min": "0", "max": None}),
        _p(name="d", address=12, waveform="step",
           wave_param={"values": ["1", "2"], "interval": "1"}),
        _p(name="e", address=16, waveform="step", wave_param={"values": " , ", "interval": "1"}),
    ]
    msgs = [i.message for i in validate_project(_proj(*pts)) if i.code == "wave_param"]
    not_number = [m for m in msgs if "is not a number" in m]
    assert len(not_number) == 3                   # amp 'x' / base None / max None
    assert any("'x'" in m for m in not_number)
    assert any(m.endswith("None") for m in not_number)
    assert any("must be a comma separated string" in m for m in msgs)
    assert any("step values list is empty" in m for m in msgs)
    # CSV 原文（没解析成 dict 就进来）只许说一条“形状不对”，不许逐字符喷假错误
    shape = [i for i in validate_project(_proj(_p(wave_param="base=20;amp=15")))
             if i.code == "wave_param"]
    assert len(shape) == 1
    assert "must be a k=v mapping" in shape[0].message


def test_non_integer_numbers_are_errors_not_tracebacks():
    """手改 project.json 把端口写成 COM3、地址写成字符串时，校验自己不许先崩。"""
    ch = TcpChannel(name="c1", host="127.0.0.1", port="COM3", devices=[
        Device(name="D1", unit_id="11", points=[_p(address="0")])])
    issues = validate_project(Project(channels=[ch], tick_ms="100"))
    got = {i.code for i in issues if i.level == "error"}
    assert {"port", "unit_range", "address", "tick_ms"} <= got
    assert all(i.level in ("error", "warning") for i in issues)


def test_dirty_unit_id_is_one_error_and_never_a_traceback():
    """unit_id=[] 时 `dev.unit_id in units` 自己就 TypeError：校验器成了那个被当平台 bug 的源头。

    非整数只落一条 unit_range，重复判定同样 gate 在 _is_int 上（同 port/address 的裁决 O）。
    """
    for dirty in ([], ["1"], "11", True, None, 1.5):
        ch = TcpChannel(name="c1", host="127.0.0.1", port=502,
                        devices=[Device(name="D1", unit_id=dirty, points=[])])
        issues = validate_project(Project(channels=[ch]))
        assert [i.code for i in issues] == ["unit_range"], dirty
        assert issues[0].level == "error"


def test_non_integer_unit_id_stays_out_of_the_duplicate_table():
    """True 与 1 哈希相同：脏值进的了 units 就会把合法设备误判成 unit_duplicated。"""
    ch = TcpChannel(name="c1", host="127.0.0.1", port=502, devices=[
        Device(name="T", unit_id=True, points=[]),
        Device(name="S", unit_id="1", points=[]),
        Device(name="N", unit_id=1, points=[])])
    got = [i.code for i in validate_project(Project(channels=[ch])) if i.level == "error"]
    assert got == ["unit_range", "unit_range"]


def test_tick_ms_out_of_range_is_a_project_level_error():
    assert ("tick_ms", "project") in codes(
        Project(channels=[TcpChannel(name="c1", devices=[])], tick_ms=5))


def test_unsupported_channel_type_is_reported():
    ch = TcpChannel(name="com1", host="", port=502, type="rtu", devices=[])
    assert ("channel_type", "com1") in codes(Project(channels=[ch]))


def test_warnings_stay_out_of_the_blocking_list():
    issues = validate_project(_proj(_p(datatype="int16", word_order="CDAB")))
    assert error_lines(issues) == []
    assert [i.level for i in issues] == ["warning"]


def test_issue_is_frozen_and_renders_the_blocking_line():
    issue = Issue("error", "waveform", "c1/D1/p", "unknown waveform 'triangle'")
    assert str(issue) == "[waveform] c1/D1/p: unknown waveform 'triangle'"
    try:
        issue.where = "elsewhere"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("Issue 必须是 frozen dataclass：清单生成后不许被改写")


# ---- M4 Task 7：点位名不许为空（属性面板能造出这个形状） ----

@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_point_name_is_an_error_naming_the_device_and_the_address(blank):
    """无名点位今天能一路走到运行态：面板把 `name_edit.text().strip()` 原样写回本体，
    零新增错误就应用成功，`start_channel` 于是对外服务一个没有名字的点，而这份工程导出
    成 CSV 后再也导不回来（`csv_io` 认 name 是身份列，空名回读当场 ValueError）。

    判据的家在 `validate_project` 而不是面板：面板与 `start_channel` 问的是同一份清单，
    加一条两处同时生效；`csv_io._require_text` 不接管这条——它管的是"带行号的那一句"，
    而行号只有 loader 知道（同一个空值两个地方各说一遍，改判据就得改两处）。
    """
    issues = [i for i in validate_project(_proj(_p(name=blank))) if i.code == "name"]
    assert len(issues) == 1 and issues[0].level == "error", issues
    text = str(issues[0])
    assert "D1" in text, f"没说是哪台设备的点：{text}"
    assert "hr" in text and "address 0" in text, f"没指出是无名的那一行：{text}"
    assert "must not be empty" in text, text


def test_a_blank_name_does_not_swallow_that_points_other_errors():
    """一次报全不打折（同 `test_area_type_mismatch_still_reserves_its_address`）：
    空名要说一句，但这一行自己的初值脏、它占的格子撞了别人，照样得各说一句——
    在这里 `return` 就等于让操作员修完名字再跑一轮才发现第二个错。
    """
    got = {(i.code, i.where) for i in validate_project(
        _proj(_p(name="", initial="not-a-number"), _p(name="q", address=0)))
        if i.level == "error"}
    assert ("name", "c1/D1/") in got, got
    assert ("initial", "c1/D1/") in got, got
    assert ("overlap", "c1/D1/q") in got, got


def test_a_real_name_still_needs_no_name_error():
    """只拦"空"，不拦"难看"：名字带空格、纯数字、400 字符都是点表里合法的身份，
    多拦一条就是把别人的点表拦在启动之外（同裁决 T 对 host 的口径）。
    """
    for name in ("0", " a ", "C" * 400, "CO浓度"):
        got = [i for i in validate_project(_proj(_p(name=name))) if i.code == "name"]
        assert got == [], name


def test_a_non_string_name_is_left_to_its_own_layer():
    """`name=None` 是 json 手改出来的形状，不是"空名字"：本条判据只管字符串，
    非字符串在这里说一句"must not be empty"会把真正的原因（类型不对）说反。
    """
    pt = _p(name=None)
    got = [i for i in validate_project(_proj(pt)) if i.code == "name"]
    assert got == []


def test_overlong_identity_names_do_not_flood_the_report():
    """厂商把整段说明塞进身份列时，"一次报全"的清单不许被一行撑爆。

    实测旧行为：通道名/设备名/点位名/host 原样拼进 `Issue.where` 与 `port_duplicated` 的
    消息，单行 668/647/512 字符——几十条错时屏幕上全是同一坨说明文字，真正要改的那条被
    挤出去。判定逻辑一个字都不动，只在渲染处截断（同裁决 N 对原值限长的要求）。
    """
    junk = "C" * 400
    host = "h" * 300
    ch_a = TcpChannel(name=junk, host=host, port=502, devices=[
        Device(name=junk, unit_id=11, points=[_p(name=junk), _p(name="q", address=0)])])
    ch_b = TcpChannel(name=junk + "2", host=host, port=502, devices=[
        Device(name=junk, unit_id=12, points=[_p(name=junk)])])
    project = Project(channels=[ch_a, ch_b])

    issues = validate_project(project)
    lines = error_lines(issues)
    assert lines, "这份点表至少要报出 overlap 与 port_duplicated"
    assert {(i.code, i.level) for i in issues} >= {
        ("overlap", "error"), ("port_duplicated", "error")}
    for line in lines:
        assert len(line) < 400, f"{len(line)} 字符: {line[:60]}..."
        assert junk not in line, "身份名原样进了消息"
    assert any("..." in line for line in lines), "没有一条被截断，说明限长没生效"
