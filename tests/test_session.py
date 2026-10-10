"""Session：GUI 唯一能对话的对象——点表读写、通道启停、拉模型刷新都在这里收口。

钉住的五件事：

1. **拉模型**：`rows()` 在通道运行时回读寄存器、未运行时用 CSV 初值；主站写进去的值
   下一次 pull 就该看见。M1 的 backend 只有 `packet_sink` / `on_connection` 两个钩子
   （这是刻意的），所以这里不引入任何"主站写入"回调，也不让引擎自己轮询。
2. **启动门槛**：`start_channel()` 前 `validate_project` 一次报全，不过就抛 `ValueError`
   （消息带错误条数与头几条），**且通道保持 stopped**——不许"启动成功但点表是错的"。
3. **拒绝姿态**：类型不符 / 越界 / 空串 / 不在点表 / 未知 area / 通道没起，一律
   `WriteResult(False, ...)`，并且**寄存器一个字节都不变**——每条都真去读回来证明。
4. **readonly 是挡主站的墙**：引擎侧 `set_point` 改得动只读点（M1 口径），真主站写同一
   地址回异常码 2；`di`/`ir` 界面可改，而协议上主站根本没有写它们的功能码。
5. **`stale` 把"没读到"和"设备报了 0"分开**：通道在跑且回读失败才置位，没启动不算异常；
   `registers` 一律是 **tuple**（A–I 表 G），两种容器并存会让上层的"这行变了没有"时好时坏。

用例一律走真端口 + 真 `ModbusTcpClient`（不是 mock）：这一层值钱的地方就是"界面点一下、
平台那边真读得到"，mock 只能证明代码自洽，证明不了它能跟真网关说话。
"""

import socket
import threading
import time

import pytest
from pymodbus.client import ModbusTcpClient

from modslave.backend.events import ChannelState, PacketEvent
from modslave.backend.modbus_tcp import PortInUseError, check_port_is_free
from modslave.codec.point_codec import encode
from modslave.csv_io_entry import load_point_table
from modslave.engine.runner import EngineRunner
from modslave.engine.session import Row, Session, WriteResult
from modslave.project.model import Project
from tests.conftest import free_port

CSV = "examples/covi-tunnel.csv"          # M1 的点表：unit 11 有 CDAB 浮点与只读点


def loaded(port: int | None = None) -> Project:
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    if port:
        project.channels[0].port = port
    return project


def connect(port: int) -> ModbusTcpClient:
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect(), f"真主站连不上 127.0.0.1:{port}"
    return client


def value_of(session: Session, name: str, channel: str = "tcp-5020") -> str:
    return next(r.value_text for r in session.rows(channel) if r.name == name)


@pytest.fixture
def session():
    """runner 由测试持有：`Session.shutdown()` 只停自己拥有的 runner（M2 定的归属口径），
    所以这里必须自己 `runner.stop()`，否则每个用例都留下两个 daemon 线程。"""
    runner = EngineRunner()
    s = Session(runner=runner)
    yield s
    s.shutdown()
    runner.stop()


# ---------- 装载与索引 ----------

def test_load_replaces_project_and_reports_issues(session):
    issues = session.load(loaded())
    assert issues == []
    assert session.dirty is False
    assert {r.unit_id for r in session.rows("tcp-5020")} == {11, 12}


def test_rows_are_indexed_in_the_point_table_order(session):
    """`load()` 一次建好索引，渲染路径只查表：行序必须就是点表行序。"""
    session.load(loaded())
    names = [r.name for r in session.rows("tcp-5020")]
    assert names == ["CO浓度", "VI风速", "风机状态", "累计电量", "铭牌",
                     "启动命令", "运行反馈", "固件版本", "回路状态"]
    assert all(isinstance(r, Row) for r in session.rows("tcp-5020"))
    # key 是 GUI 定位一行的身份，四元组顺序即 (channel, unit, area, address)
    first = session.rows("tcp-5020")[0]
    assert first.key == ("tcp-5020", 11, "hr", 0)


def test_rows_for_unknown_channel_is_empty(session):
    session.load(loaded())
    assert session.rows("nope") == []
    assert session.channel("nope") is None


def test_rows_are_unreadable_before_start_so_the_csv_initial_shows(session):
    """ζ：一个通道都没起时表格照样可读（显示点表与初值）。"""
    session.load(loaded(free_port()))
    assert value_of(session, "CO浓度") == "12.5"
    assert value_of(session, "启动命令") == "false"
    # 没起时不回读，寄存器列是空的；空的是 tuple（A–I 表 G：registers 定死一种容器）
    assert next(r.registers for r in session.rows("tcp-5020")
                if r.name == "CO浓度") == ()


def test_row_field_order_is_the_positional_contract_task_5_builds_on():
    """`FakeEngine` 按**位置**构造 Row，字段顺序就是契约，改动要先过这里。

    `stale` 必须留在末尾并带默认值：前面 12 个位置参数的顺序一旦变，
    `point_source.FakeEngine` 构造出来的行会**静默串列**（把 waveform 当 readonly 之类），
    界面上是一格看着合法的错值——比崩掉难查得多。
    """
    assert tuple(Row.__dataclass_fields__) == (
        "channel", "device", "unit_id", "name", "area", "address", "datatype",
        "word_order", "value_text", "registers", "readonly", "waveform", "stale")
    default = Row("tcp-5020", "COVI-1", 11, "CO浓度", "hr", 0, "float32", "CDAB",
                  "12.5", (0x0000, 0x4148), True, "const")
    assert default.stale is False, "stale 要有默认值，否则现有构造点全得跟着改"


# ---------- 启动门槛 ----------

def test_start_is_refused_when_validation_fails(session):
    project = loaded()
    project.channels[0].port = 0
    session.load(project)
    with pytest.raises(ValueError) as exc:
        session.start_channel("tcp-5020")
    assert "refusing to start" in str(exc.value)
    assert not session.channel("tcp-5020").running


def test_start_refusal_lists_the_count_and_several_errors_and_leaves_nothing_running(
        session):
    """一次报全：消息里要有条数、有头几条，而且通道一个都没起来、没留下半个后端。"""
    project = loaded()
    project.channels[0].port = 0
    project.tick_ms = 5                    # 第二条 error，验证"头几条"不是一条
    session.load(project)
    with pytest.raises(ValueError) as exc:
        session.start_channel("tcp-5020")
    message = str(exc.value)
    assert "2 error(s)" in message, message
    assert "[port]" in message and "[tick_ms]" in message, message
    runtime = session.channel("tcp-5020")
    assert not runtime.running and runtime.backend is None
    assert runtime.detail == ""


# ---------- 启停与状态事件 ----------

def test_channel_lifecycle_and_state_events(session):
    seen: list[str] = []
    session.subscribe(lambda e: seen.append(getattr(e, "state", "") or ""))
    session.load(loaded(free_port()))
    session.start_channel("tcp-5020")
    assert session.channel("tcp-5020").running
    session.stop_channel("tcp-5020")
    assert not session.channel("tcp-5020").running
    assert "running" in seen and "stopped" in seen


def test_running_state_detail_names_the_host_and_port(session):
    """设备树那一格只有 `host:port` 才够现场对号入座（状态只改文字不改几何）。"""
    port = free_port()
    states: list[ChannelState] = []
    session.subscribe(lambda e: isinstance(e, ChannelState) and states.append(e))
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert session.channel("tcp-5020").detail == f"0.0.0.0:{port}"
    assert states[-1].state == "running" and states[-1].channel == "tcp-5020"


def test_restart_the_same_channel_immediately_reclaims_the_port(session):
    """端口不泄漏：停掉之后**立刻**再起（不靠 sleep），且新监听真的在服务。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    session.stop_channel("tcp-5020")
    check_port_is_free("127.0.0.1", port)      # M1 的裸 bind 探针：端口又归我们了
    session.start_channel("tcp-5020")
    assert session.channel("tcp-5020").running
    client = connect(port)
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
    finally:
        client.close()


def test_start_is_idempotent_and_stop_all_covers_every_channel(session):
    session.load(loaded(free_port()))
    session.start_channel("tcp-5020")
    runtime = session.channel("tcp-5020")
    backend = runtime.backend
    session.start_channel("tcp-5020")          # 已在跑：不再占一次端口
    assert runtime.backend is backend
    session.stop_all()
    assert not runtime.running


def test_a_runtime_start_failure_becomes_an_error_state_not_a_half_started_channel(
        session):
    """运行期错误（真端口被占 10048 那一类）由 backend 抛出：Session 落 last_error、
    发 ChannelState(error) 并把异常原样抛给调用方——不许"日志说起了、外面连不上"。

    host 设成 127.0.0.1 是必须的：实测 Windows 上"通配地址绑到已被具体地址占用的端口"
    是**成功**的（`bind(('0.0.0.0', port))` 对 hog 在 `127.0.0.1:port` 不报错），所以
    只有通道与占用者同一地址，M1 那个裸 bind 探针才看得见冲突。真实现场最常见的形态
    "上一个实例也在 0.0.0.0:5020"照样看得见（两边都是通配）。
    """
    with socket.socket() as hog:
        hog.bind(("127.0.0.1", 0))
        port = hog.getsockname()[1]
        hog.listen(1)
        project = loaded(port)
        project.channels[0].host = "127.0.0.1"
        states: list[ChannelState] = []
        session.subscribe(lambda e: isinstance(e, ChannelState) and states.append(e))
        session.load(project)
        with pytest.raises(PortInUseError, match="may already be in use"):
            session.start_channel("tcp-5020")
        runtime = session.channel("tcp-5020")
        assert not runtime.running and runtime.backend is None
        assert str(port) in runtime.last_error
        assert [s.state for s in states] == ["error"]
        assert states[0].channel == "tcp-5020" and str(port) in states[0].detail


def engine_idents() -> set[int]:
    """当前活着的 `modslave-engine*` 线程**身份**（ident）。

    为什么不按名字做集合差：同一次 pytest 里可能有用例（或真 GUI）留着同名的线程，
    按名字差会把"本用例新建的那两个"减掉，变成一条会假失败的断言。
    点名判"是引擎线程"、ident 判"是本次新建的那一个"，两件事各管各的。
    """
    return {t.ident for t in threading.enumerate()
            if t.name.startswith("modslave-engine") and t.is_alive()}


def test_shutdown_of_an_owned_runner_leaves_no_engine_thread():
    """`Session()` 不接 runner 时自己拥有它：shutdown 必须把两个线程都收掉。"""
    before = engine_idents()
    port = free_port()
    owned = Session()
    try:
        owned.load(loaded(port))
        owned.start_channel("tcp-5020")
        assert len(engine_idents() - before) == 2, (
            "Session 自建 runner 应该带来 loop + events 两个引擎线程")
    finally:
        owned.shutdown()
    assert engine_idents() - before == set(), "shutdown 后仍留着本用例新建的引擎线程"
    check_port_is_free("127.0.0.1", port)


def test_start_all_collects_refusals_instead_of_raising_the_first_one(session):
    """工具栏"全部启动"要拿到整张清单，不是撞上第一个就崩（和 validate 同一个姿态）。"""
    project = loaded()
    project.channels[0].port = 0
    session.load(project)
    assert len(session.start_all()) == 1
    assert not session.channel("tcp-5020").running


# ---------- 点表读：真主站交叉验证 ----------

def test_master_read_seeds_the_row_snapshot(session):
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    row = next(r for r in session.rows("tcp-5020") if r.name == "CO浓度")
    assert row.value_text == "12.5"          # 初值来自 CSV，字序 CDAB
    assert row.registers == (0x0000, 0x4148)


def test_a_real_master_reads_exactly_what_the_codec_encodes(session):
    """交叉验证（不是 mock）：真主站读 hr 0..1 的寄存器序列 == encode("12.5",float32,CDAB)。

    这一条是"模拟器和平台读的是同一个东西"的正证：CDAB 换错字序，平台读到的是 2e-41
    这种天价，而界面上一切正常。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    expected = encode("12.5", "float32", "CDAB")
    client = connect(port)
    try:
        assert client.read_holding_registers(0, count=2, device_id=11).registers == expected
    finally:
        client.close()
    assert next(r.registers for r in session.rows("tcp-5020")
                if r.name == "CO浓度") == tuple(expected)


def test_every_row_reads_back_from_a_running_channel(session):
    """回读失败会回落 CSV 初值——所以"通道在跑但某行拿不到寄存器"必须当场被抓住，
    否则一格静默的错值会被当成平台读错。`stale` 就是这件事的显式表达（Task 5 补的），
    健康通道跑起来后**一行都不许灰**：灰显一旦成为常态，它就再也区分不出"读不到"了。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    rows = session.rows("tcp-5020")
    silent = [r.name for r in rows if not r.registers]
    assert silent == [], f"这些行回读失败却仍显示初值：{silent}"
    assert [r.name for r in rows if r.stale] == [], "健康通道上的行被标成了 stale"


def test_rows_reflect_a_master_write_on_the_next_pull(session):
    """拉模型的正证：主站写进去之后，下一次 rows() 就该是新值（GUI 10Hz 拉）。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    client = connect(port)
    try:
        client.write_register(4, 55, device_id=11)
    finally:
        client.close()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if value_of(session, "风机状态") == "55":
            return
        time.sleep(0.02)
    pytest.fail(f"rows() 没能在 2s 内反映主站写入，读到 {value_of(session, '风机状态')!r}")


# ---------- 回读失败与 stale ----------

def test_rows_are_marked_stale_when_the_running_backend_cannot_answer(session, monkeypatch):
    """通道在跑但后端读不到：值仍显示初值，但必须标 stale。

    `stale` 是**唯一**能把"没读到"和"设备报了这个值"分开的东西：`_render` 在回读失败时
    照样回落 CSV 初值，界面上那一格看着完全正常。不置 stale 就等于让模拟器自己的毛病
    被平台当成"值没变"，这条就是那层区分本身。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    runtime = session.channel("tcp-5020")

    def blow_up(*_args, **_kwargs):
        raise OSError("backend gone")

    monkeypatch.setattr(runtime.backend, "read_registers", blow_up)
    rows = session.rows("tcp-5020")
    stale = {r.name for r in rows if r.stale}
    assert "CO浓度" in stale, "寄存器区回读失败没体现在 stale 上"
    assert not any(r.stale for r in rows if r.area == "co"), "位区走另一条回读路径"
    assert not any(r.stale for r in rows if r.area == "di"), "位区走另一条回读路径"


def test_a_stale_row_still_shows_the_csv_initial_not_a_zero(session, monkeypatch):
    """读不到时那一格仍是初值 + stale：改成显示 0 或空串就是"设备报了 0"的演出。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    monkeypatch.setattr(session.channel("tcp-5020").backend, "read_registers",
                        lambda *_a, **_k: None)
    row = next(r for r in session.rows("tcp-5020") if r.name == "CO浓度")
    assert row.stale is True
    assert row.value_text == "12.5", "回落初值是对的，但必须由 stale 说明它不是读数"
    assert row.registers == (), "回读失败还不许冒充有一段寄存器"


def test_stale_does_not_stick_once_the_backend_answers_again(session, monkeypatch):
    """灰显不许粘住：读得回来就该恢复正常，否则界面迟早整表发灰、stale 失去分辨力。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    real = backend.read_registers
    broken = {"yes": True}

    def flaky(unit_id, area, address, count):
        if broken["yes"]:
            raise OSError("backend gone")
        return real(unit_id, area, address, count)

    monkeypatch.setattr(backend, "read_registers", flaky)
    assert any(r.stale for r in session.rows("tcp-5020"))
    broken["yes"] = False
    rows = session.rows("tcp-5020")
    assert [r.name for r in rows if r.stale] == [], "读得回来了还在灰"
    assert next(r.registers for r in rows if r.name == "CO浓度") == (0x0000, 0x4148)


def test_a_bit_area_that_cannot_answer_is_stale_on_the_bit_path(session, monkeypatch):
    """位区是另一条回读路（`read_bits`）：它失败时同样要置 stale，别只钉寄存器区。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    monkeypatch.setattr(session.channel("tcp-5020").backend, "read_bits",
                        lambda *_a, **_k: None)
    rows = session.rows("tcp-5020")
    bits = [r for r in rows if r.area in ("co", "di")]
    assert bits and all(r.stale for r in bits), [r.name for r in bits]
    assert not any(r.stale for r in rows if r.area == "hr"), "寄存器区没坏，不该跟着灰"


def test_rows_are_not_stale_before_the_channel_starts(session):
    session.load(loaded(free_port()))
    assert not any(r.stale for r in session.rows("tcp-5020")), "没启动不是异常，不该灰显"


def test_a_write_that_cannot_be_read_back_is_refused_and_greys_the_row(session, monkeypatch):
    """写进去了但读不回来：`set_point` 仍报失败（M2-H2 已定），且那一行要灰——
    界面继续显示旧值 + 不灰，等于说"设备还是这个值"，而寄存器其实已经变了。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    monkeypatch.setattr(session.channel("tcp-5020").backend, "read_registers",
                        lambda *_a, **_k: None)
    res = session.set_point("tcp-5020", 11, "hr", 4, "37")
    assert not res.ok and "read-back failed" in res.error
    assert res.value_text == "" and res.registers == ()
    row = next(r for r in session.rows("tcp-5020") if r.name == "风机状态")
    assert (row.value_text, row.stale) == ("11", True)


# ---------- 点表写 ----------

def test_set_point_writes_and_the_master_reads_it_back(session):
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    ok = session.set_point("tcp-5020", 11, "hr", 4, "37")
    assert ok.ok and ok.value_text == "37"
    assert isinstance(ok, WriteResult)
    client = connect(port)
    try:
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [37]
    finally:
        client.close()


def test_readonly_is_a_wall_against_the_master_not_against_the_session(session):
    """M1 口径：readonly 是挡主站的墙。引擎侧改得动，真主站写同一地址回异常码 2。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    refused = session.set_point("tcp-5020", 11, "hr", 0, "9")
    assert refused.ok                          # 引擎侧写只读点是允许的
    client = connect(port)
    try:
        bad = client.write_register(0, 123, device_id=11)
        assert bad.isError() and bad.exception_code == 2
        # 主站被拒之后，界面上的值还是引擎写进去的那个 9（不是被 123 改掉）
        assert value_of(session, "CO浓度") == "9"
        assert client.read_holding_registers(0, count=2, device_id=11).registers == \
            encode("9", "float32", "CDAB")
    finally:
        client.close()


def test_bit_points_go_through_the_bit_path_not_the_register_one(session):
    """位区判定在 Session 里做对：M1 已定 `write_registers(11,'co',...)` 直接抛 ValueError，
    所以走错分支的表现为"改一个开关量失败"，这里用真主站的读回钉住走的是 write_bits。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    res = session.set_point("tcp-5020", 11, "co", 0, "true")
    assert res.ok, res.error
    assert res.value_text == "true" and res.registers == (1,)
    client = connect(port)
    try:
        assert client.read_coils(0, count=1, device_id=11).bits[0] is True
    finally:
        client.close()


def test_input_areas_are_editable_from_the_session_only(session):
    """`di`/`ir` 界面可改、主站写不了：协议上没有写 1x/3x 的功能码，
    主站能做的只有写 co/hr——所以这两区的当前值只有模拟器自己能改。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    client = connect(port)
    try:
        assert client.read_discrete_inputs(0, count=1, device_id=11).bits[0] is True
        assert client.read_input_registers(0, count=1, device_id=11).registers == [7]
        # 主站能写的两个功能码打不到 1x/3x：write_coil 改的是 co，write_register 改的是 hr
        assert client.write_coil(0, False, device_id=11).bits[0] is False
        assert client.write_register(0, 1, device_id=11).isError()
        assert client.read_discrete_inputs(0, count=1, device_id=11).bits[0] is True
        assert client.read_input_registers(0, count=1, device_id=11).registers == [7]
        # 引擎侧改得动，且改完主站立刻读到新值
        assert session.set_point("tcp-5020", 11, "di", 0, "false").ok
        assert session.set_point("tcp-5020", 11, "ir", 0, "99").ok
        assert client.read_discrete_inputs(0, count=1, device_id=11).bits[0] is False
        assert client.read_input_registers(0, count=1, device_id=11).registers == [99]
    finally:
        client.close()


def test_set_point_rejects_bad_input_without_touching_registers(session):
    """三种脏输入逐条读回：拒绝必须是"一个字节都没变"，不是"改了再改回来"。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    client = connect(port)
    try:
        for text, needle in [("abc", "uint16 needs an integer"),
                             ("99999999", "uint16 out of range"),
                             ("", "uint16 needs an integer")]:
            res = session.set_point("tcp-5020", 11, "hr", 4, text)
            assert not res.ok, (text, res)
            assert needle in res.error, (text, res.error)
            assert res.value_text == "" and res.registers == (), res
            assert client.read_holding_registers(4, count=1,
                                                 device_id=11).registers == [11]
            assert value_of(session, "风机状态") == "11"
        assert session.dirty is False          # 被拒的改动不算编辑
    finally:
        client.close()


def test_set_point_refuses_an_unlisted_address_or_area_without_touching_registers(
        session):
    """不在点表里的地址、以及压根不存在的区名，都在查表这一步就拒——
    不许把脏 area 传到 backend 去换一句 `unknown area` 的 traceback。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    client = connect(port)
    try:
        for area, address in [("hr", 30), ("xx", 0), ("", 0), ("hr", -1)]:
            res = session.set_point("tcp-5020", 11, area, address, "1")
            assert not res.ok, (area, address, res)
            assert "not in the point table" in res.error, (area, address, res.error)
            assert not res.error.startswith("engine write failed"), res.error
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
        assert client.read_holding_registers(0, count=1, device_id=11).registers == [0]
        # 未知 unit 同样查表就拒，不去猜"回落第一个设备"那种错答案
        res = session.set_point("tcp-5020", 99, "hr", 4, "1")
        assert not res.ok and "not in the point table" in res.error
        assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
    finally:
        client.close()


def test_set_point_when_channel_stopped_is_a_domain_error(session):
    session.load(loaded(free_port()))
    res = session.set_point("tcp-5020", 11, "hr", 4, "1")
    assert not res.ok and "not running" in res.error


def test_set_point_on_an_unknown_channel_is_a_keyerror(session):
    """通道名都不是：那是编程错误，不是用户输入错误，照 M1 口径炸出来。"""
    session.load(loaded(free_port()))
    with pytest.raises(KeyError):
        session.set_point("nope", 11, "hr", 4, "1")


def test_set_point_after_stop_says_not_running_instead_of_none_attribute(session):
    """关通道与改值抢顺序时，用户要看到"这个通道没在跑"，不是 `'NoneType' object ...`。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert session.set_point("tcp-5020", 11, "hr", 4, "21").ok
    session.stop_channel("tcp-5020")
    res = session.set_point("tcp-5020", 11, "hr", 4, "22")
    assert not res.ok and "not running" in res.error
    # 重启通道是按点表重建后端（`build_sim_devices` 从 CSV 初值起），界面上改过的值
    # **不会**带过去。这条钉死它，免得将来有人把"重启还留着上次改的值"当成默认行为改上去。
    session.start_channel("tcp-5020")
    assert value_of(session, "风机状态") == "11"


def test_dirty_flag_tracks_edits(session):
    session.load(loaded(free_port()))
    session.start_channel("tcp-5020")
    assert session.dirty is False
    session.mark_dirty()
    assert session.dirty is True
    session.load(loaded(free_port()))            # 换工程 = 重新装载，脏标记归零
    assert session.dirty is False


def test_a_successful_write_marks_the_project_dirty(session):
    """ε：未保存退出要提示，前提是真改成功才算脏。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert session.dirty is False
    assert session.set_point("tcp-5020", 11, "hr", 4, "37").ok
    assert session.dirty is True


def test_write_is_the_pair_the_point_table_model_calls(session):
    """`write()` 是 GUI 侧唯一的写入口：把 `WriteResult` 收成 `(ok, error)`。

    只加一层薄封装、不改错误句式——错误文案的真相在 codec/validate 那一份里，
    表格模型再抄一套话术，同一件错在现场就有两种说法（本项目防的就是这个）。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert session.write("tcp-5020", 11, "hr", 4, "37") == (True, "")
    ok, error = session.write("tcp-5020", 11, "hr", 4, "abc")
    assert ok is False and "uint16 needs an integer" in error
    ok, error = session.write("tcp-5020", 11, "hr", 30, "1")
    assert ok is False and "not in the point table" in error


def test_issues_re_reads_the_live_project(session):
    """`issues()` 现算，不是 `load()` 那份清单的缓存。

    通道面板改完 host/port 要立刻问"这一改动让工程变坏了没有"，缓存的那一份回答的是
    装载时的老问题——两个答案不一样时，界面会放行一个启动时被拦下的配置。
    """
    session.load(loaded(free_port()))
    assert session.issues() == []
    session.project.tick_ms = 5                       # 界面能改到的工程字段
    codes = [i.code for i in session.issues()]
    assert codes == ["tick_ms"]
    assert [i.level for i in session.issues()] == ["error"]


# ---------- 事件 ----------

def test_packet_events_carry_the_channel_name(session):
    """通道名在建 backend 时用闭包绑定，所以这里必须是真通道名，不是 "?"。"""
    port = free_port()
    session.load(loaded(port))
    events: list[object] = []
    session.subscribe(events.append)
    session.start_channel("tcp-5020")
    client = connect(port)
    try:
        client.read_holding_registers(0, count=1, device_id=11)
    finally:
        client.close()
    session.wait_idle()
    packets = [e for e in events if isinstance(e, PacketEvent)]
    assert packets, "一个报文事件都没收到"
    assert {p.channel for p in packets} == {"tcp-5020"}
    assert any(p.direction == "rx" and p.unit_id == 11 and p.function_code == 3
               for p in packets), [vars(p) for p in packets]
    assert any(p.direction == "tx" for p in packets)


def test_a_broken_subscriber_does_not_break_the_session(session):
    """界面回调炸了不许把通道带停：GUI 的 widget 在事件还在路上时就被销毁是常态。"""
    port = free_port()
    session.load(loaded(port))
    session.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("widget gone")))
    seen: list[object] = []
    session.subscribe(seen.append)
    session.start_channel("tcp-5020")
    client = connect(port)
    try:
        client.read_holding_registers(4, count=1, device_id=11)
    finally:
        client.close()
    session.wait_idle()
    assert session.channel("tcp-5020").running
    assert any(isinstance(e, PacketEvent) for e in seen)
