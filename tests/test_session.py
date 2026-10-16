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

import dataclasses
import socket
import threading
import time

import pytest
from pymodbus.client import ModbusTcpClient

from simbench.backend.modbus_tcp import PortInUseError, check_port_is_free
from simbench.codec.point_codec import encode
from simbench.csv_io_entry import load_point_table
from simbench.engine.runner import EngineRunner
from simbench.engine.session import Row, Session, WriteResult
from simbench.events import ChannelState, LogLine, PacketEvent
from simbench.project.model import Device, Point, Project, TcpChannel
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
    # key 是 GUI 定位一行的身份：五元组顺序即 (channel, unit, area, address, bit)，
    # 最后那位是 S7 的位（Modbus 的点恒为 None）——同字节上的两个 S7 bool 靠它分开
    first = session.rows("tcp-5020")[0]
    assert first.key == ("tcp-5020", 11, "hr", 0, None)


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

    `stale`/`source` 必须留在末尾并带默认值：前面 12 个位置参数的顺序一旦变，
    `point_source.FakeEngine` 构造出来的行会**静默串列**（把 waveform 当 readonly 之类），
    界面上是一格看着合法的错值——比崩掉难查得多。
    """
    assert tuple(Row.__dataclass_fields__) == (
        "channel", "device", "unit_id", "name", "area", "address", "datatype",
        "word_order", "value_text", "registers", "readonly", "waveform", "stale",
        "source", "bit")
    default = Row("tcp-5020", "COVI-1", 11, "CO浓度", "hr", 0, "float32", "CDAB",
                  "12.5", (0x0000, 0x4148), True, "const")
    assert default.stale is False, "stale 要有默认值，否则现有构造点全得跟着改"
    # 这两句才是"没串列"的证据：位置构造少给一个参数时，串进去的值看着完全合法
    assert default.readonly is True and default.waveform == "const"


def test_the_fields_added_later_all_sit_at_the_tail_with_defaults():
    """后加的字段（`stale` / `source` / `bit`）都在**末尾**且带默认值：老的位置构造
    （`FakeEngine` 与一批用例）不会把它们接到前面的字段上。

    这条原来是"`source` 是最后一位"，加了 `bit`（S7 的位，属于身份）之后改成
    "尾巴那三个是它们"——契约没变松：**新字段只许往后加、且必须带默认值**。
    """
    fields = list(dataclasses.fields(Row))
    assert [field.name for field in fields[-3:]] == ["stale", "source", "bit"]
    assert [field.default for field in fields[-3:]] == [False, "waveform", None]


def test_rows_carry_the_ownership_of_a_running_channel(session):
    """ζ：没起通道时那一列只能是 `waveform`——那时显示的是点表初值，标"已停"是界面在
    演一出没发生的接管。起之后默认也还是 `waveform`（谁都没动过值）。
    """
    session.load(loaded(free_port()))
    assert [r.source for r in session.rows("tcp-5020")] == ["waveform"] * 9
    session.start_channel("tcp-5020")
    rows = session.rows("tcp-5020")
    assert len(rows) == 9
    assert [r.source for r in rows] == ["waveform"] * 9, "刚起来的通道不该有任何一格是 manual"
    assert session.manual_count("tcp-5020") == 0



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


def test_a_failed_stop_keeps_the_handle_so_the_next_stop_can_close_it(
        session, monkeypatch):
    """引擎不回话时"停止"不许假装成功：句柄留住、状态是 error、再点一次真关掉。

    旧写法把异常吞掉照样 `backend = None`，于是监听还占着端口而 Session 里再没有句柄——
    界面上写着 ○ 停止、外面真主站照样读到值，操作员只剩重启进程一条路。留住句柄才有
    第二次尝试，而第二次是真能成功的（卡住的只是那一次 `call`）。
    """
    port = free_port()
    states: list[ChannelState] = []
    session.subscribe(lambda e: isinstance(e, ChannelState) and states.append(e))
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    monkeypatch.setattr(session._runner, "call",
                        lambda *a, **k: (_ for _ in ()).throw(
                            TimeoutError("engine wedged")))
    with pytest.raises(RuntimeError) as exc:
        session.stop_channel("tcp-5020")
    monkeypatch.undo()
    assert "engine wedged" in str(exc.value)
    runtime = session.channel("tcp-5020")
    assert runtime.backend is not None, "关不掉就该留住句柄，否则第二次停止没东西可关"
    assert runtime.running and "engine wedged" in runtime.last_error
    assert states[-1].state == "error", "端口还在应答，界面不许发 stopped"
    with pytest.raises(PortInUseError):
        check_port_is_free("127.0.0.1", port)     # 端口确实还被它占着，与界面一致

    session.stop_channel("tcp-5020")              # 第二下真的关掉
    assert not runtime.running and runtime.backend is None
    check_port_is_free("127.0.0.1", port)


def test_stop_all_names_the_channel_it_could_not_close_and_still_stops_the_rest(
        session, monkeypatch):
    """"全部停止"关不掉一个通道：不连累别的通道，也不许给它跟着发 stopped。

    与 `start_all` 同一条姿态——一次报全、逐个交代。只发一句"停止了"而端口还在应答，
    操作员会照着"已停"去接下一路 485。
    """
    port_a, port_b = free_port(), free_port()
    project = loaded(port_a)
    project.channels.append(another_channel(port_b))
    session.load(project)
    assert session.start_all() == []
    states: list[ChannelState] = []
    session.subscribe(lambda e: isinstance(e, ChannelState) and states.append(e))
    real_call = session._runner.call

    def wedged(factory, seconds, what):
        if "tcp-5020" in what:
            raise TimeoutError("engine wedged")
        return real_call(factory, seconds, what)

    monkeypatch.setattr(session._runner, "call", wedged)
    failures = session.stop_all()
    monkeypatch.undo()
    assert len(failures) == 1 and "tcp-5020" in failures[0], failures
    assert "engine wedged" in failures[0]
    assert {s.channel: s.state for s in states} == {"tcp-5020": "error",
                                                    "tcp-alt": "stopped"}
    assert session.channel("tcp-alt").running is False
    assert session.channel("tcp-5020").running

    assert session.stop_all() == []               # 再点一次，剩下的那个也关掉
    assert not session.channel("tcp-5020").running
    check_port_is_free("127.0.0.1", port_a)


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
    """当前活着的 `simbench-engine*` 线程**身份**（ident）。

    为什么不按名字做集合差：同一次 pytest 里可能有用例（或真 GUI）留着同名的线程，
    按名字差会把"本用例新建的那两个"减掉，变成一条会假失败的断言。
    点名判"是引擎线程"、ident 判"是本次新建的那一个"，两件事各管各的。
    """
    return {t.ident for t in threading.enumerate()
            if t.name.startswith("simbench-engine") and t.is_alive()}


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


def another_channel(port: int) -> TcpChannel:
    """第二条通道：unit 21 不与示例点表里的 11/12 撞，端口另取一个空闲的。"""
    return TcpChannel(name="tcp-alt", host="127.0.0.1", port=port, devices=[
        Device(name="照明回路-9", unit_id=21,
               points=[Point("温度", "hr", 0, "uint16", initial="7")])])


def test_start_all_collects_a_refused_port_and_still_starts_the_rest(session):
    """"全部启动"里一个通道撞端口，不许把下一个通道一起带走。

    运行期失败抛的是 `PortInUseError`（RuntimeError 系）而不是 `ValueError`：只收
    ValueError 的话第一个坏通道会把 `start_all` 打断——现场接两路只起得来一路，而界面
    连一句"另一个我没试过"都不说，操作员只会当成模拟器坏了。
    占位方在 `127.0.0.1`、我们要起的是点表里的 `0.0.0.0`，正是裸 bind 看不见的那一种冲突。
    """
    with socket.socket() as hog:
        hog.bind(("127.0.0.1", 0))
        hog.listen(128)
        busy = hog.getsockname()[1]
        project = loaded(busy)
        assert project.channels[0].host == "0.0.0.0"
        project.channels.append(another_channel(free_port()))
        session.load(project)
        failures = session.start_all()
        assert len(failures) == 1, failures
        assert "may already be in use" in failures[0], failures[0]
        assert "channel tcp-5020" in failures[0], failures[0]
        assert not session.channel("tcp-5020").running
        assert session.channel("tcp-alt").running, "坏通道把后面的通道一起拦停了"
        session.stop_all()


def test_stop_all_publishes_a_stopped_state_for_every_channel(session):
    """工具栏"全部停止"必须逐个发 stopped：只停后端不发事件，界面上还挂着 ●。

    `stop_channel` 一直会发，`stop_all` 漏了——通道已经停了而界面说它还在跑，正是本项目
    最防的那类演出，所以这里按"每个通道一条 stopped"钉住，并顺手钉住幂等顺序：
    已经停着的通道也照样给一条，界面上一次点击全都对上号。
    """
    project = loaded(free_port())
    project.channels.append(another_channel(free_port()))
    states: list[ChannelState] = []
    session.subscribe(lambda e: isinstance(e, ChannelState) and states.append(e))
    session.load(project)
    session.start_all()
    assert [session.channel(n).running for n in session.channel_names()] == [True, True]
    states.clear()
    session.stop_all()
    assert sorted(s.state for s in states) == ["stopped", "stopped"]
    assert sorted(s.channel for s in states) == ["tcp-5020", "tcp-alt"]
    assert not any(session.channel(n).running for n in session.channel_names())


def test_shutdown_unsubscribes_from_a_shared_runner(engine_runner):
    """关掉的 Session 必须从**共享** runner 上摘掉自己，之后再也收不到引擎事件。

    这条是拿实测代价换来的：runner 共享时只 subscribe 不 unsubscribe，每建一个界面再关掉
    就永久多留一份订阅，之后每条报文都要穿过所有历史 Session 一遍、还往已经销毁的 widget
    上投递——加界面用例之后全量从 20 秒涨到 8 分钟，并把真主站用例拖到读超时。
    """
    before = list(engine_runner._sinks)
    gone: list[object] = []
    session = Session(runner=engine_runner)
    session.load(loaded(free_port()))
    session.subscribe(gone.append)
    assert len(engine_runner._sinks) == len(before) + 1
    session.shutdown()
    # shutdown 里 stop_all 会先把 stopped 发完（那是给还开着的界面用的），所以清一次再断言
    gone.clear()
    assert engine_runner._sinks == before, "shutdown 没把自己从共享 runner 上摘掉"

    engine_runner.publish(PacketEvent("tcp-x", "rx", bytes([0, 1]), 11, 3, "x", 0.0))
    engine_runner.drain(2.0)
    assert gone == [], "已经关掉的 Session 还在收引擎事件"

    survivor = Session(runner=engine_runner)     # 摘干净不许把后面的 Session 一起带走
    survivor.load(loaded(free_port()))
    survivor.start_channel("tcp-5020")
    assert survivor.channel("tcp-5020").running
    survivor.shutdown()


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


def oversized_string_project(port: int) -> Project:
    """把 `铭牌` 排在两个邻居**前面**，初值 "AB" 只撑出 1 格跨度——A2 的形状。

    `Point.span` 是初值算出来的（`model.register_span`：string 取 ceil(len/2)），所以把它
    改成更长的文本会编出 4 格，多出的 3 格正踩在 `状态字`(hr 1) 与 `累计电量`(hr 2..5) 上：
    照写就是"一次编辑接管三个点"，而 `restore_waveform` 只还得回 `pt.span` 的那 1 格。
    """
    dev = Device(name="风机-1", unit_id=11, points=[
        Point("铭牌", "hr", 0, "string", initial="AB"),
        Point("状态字", "hr", 1, "uint16", initial="11"),
        Point("累计电量", "hr", 2, "uint64", initial="72623859790382856"),
        Point("启动", "co", 0, "bool", initial="false")])
    return Project(channels=[TcpChannel(name="tcp", host="127.0.0.1", port=port,
                                        devices=[dev])])


def test_a_longer_string_edit_is_refused_before_it_reaches_the_engine(session):
    """超跨度编辑在 `encode` 之后、引擎之前就拒掉：不写寄存器、不标接管、不动 Row。

    消息得带上点位名和**那两个数**（这个点几格、这个值要几格），并且把该改的地方指到点表
    的初值上——操作员在编辑框里删短文本是治不了的，跨度不在这一格里。
    """
    port = free_port()
    session.load(oversized_string_project(port))
    session.start_channel("tcp")
    res = session.set_point("tcp", 11, "hr", 0, "ABCDEFGH")    # 4 格 > span 1
    assert not res.ok, res
    assert "铭牌" in res.error, res.error
    assert "1 register(s)" in res.error and "needs 4" in res.error, res.error
    assert "初值" in res.error, res.error
    assert res.value_text == "" and res.registers == (), res


def test_a_refused_oversized_edit_leaves_the_neighbours_and_the_count_alone(session):
    """被拒的那一次编辑**一个字节都没写**、也一个点都没接管：邻居的寄存器还是初值，
    接管计数还是 0。少了这一半断言，"先写后判"的写法照样能让上一条绿。"""
    port = free_port()
    session.load(oversized_string_project(port))
    session.start_channel("tcp")
    backend = session.channel("tcp").backend
    assert not session.set_point("tcp", 11, "hr", 0, "ABCDEFGH").ok
    expected = (encode("AB", "string", "ABCD") + encode(11, "uint16", "ABCD")
                + encode(72623859790382856, "uint64", "ABCD"))
    client = connect(port)
    try:
        assert client.read_holding_registers(0, count=6, device_id=11).registers == expected
    finally:
        client.close()
    assert session.manual_count("tcp") == 0, "被拒的编辑还是把点接管了"
    assert backend._sources.cleared() == 0
    assert [r.source for r in session.rows("tcp")] == ["waveform"] * 4


def test_a_same_or_shorter_string_edit_still_lands(session):
    """闸门是 `>` 不是 `!=`：示例点表的 `铭牌` 初值 "FAN-01" 撑出 3 格跨度，等长（6 字符）
    与更短的文本都还在这个点自己那三格里，照旧要写得成——判成 `!=` 会把缩短一起拒掉。"""
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert len(encode("PUMP07", "string", "ABCD")) == 3      # 前提：真的与 span 等长
    same = session.set_point("tcp-5020", 11, "hr", 9, "PUMP07")
    assert same.ok, same.error
    assert same.value_text == "PUMP07"
    assert session.restore_waveform("tcp-5020", 11, "hr", 9, None) is True
    shorter = session.set_point("tcp-5020", 11, "hr", 9, "AB")
    assert shorter.ok, shorter.error
    assert session.manual_count("tcp-5020") == 1


def test_a_shorter_string_edit_clears_the_tail_of_its_own_span(session):
    """改短要把它**自己那一段的尾巴清成零**（2026-10-13 全面审查定的口径，M4 计划判给
    "人定"的那条老账）。

    不补零的话，主站按跨度读回来是 "AB" + 上一版留下的尾巴（"ABMP07"）——那是界面在报一个
    没人写过的值。补的范围**只到 `pt.span`**：补到别处就是 A2 那个"一次编辑接管三个点"的事故
    （邻居的格子被零写掉）。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert session.set_point("tcp-5020", 11, "hr", 9, "PUMP07").ok
    shorter = session.set_point("tcp-5020", 11, "hr", 9, "AB")
    assert shorter.ok, shorter.error
    assert shorter.registers == (0x4142, 0, 0), shorter.registers
    client = connect(port)
    try:
        assert client.read_holding_registers(9, count=3, device_id=11).registers \
            == [0x4142, 0, 0], "改短之后尾巴还留着上一版的值"
    finally:
        client.close()


def test_a_bit_point_still_takes_exactly_one_address(session):
    """新闸门比的是 `len(registers)` 与 `pt.span`，位区两者恒为 1：开关量编辑照旧只占一格
    （把它写成 `>=` 就是把所有开关量的界面编辑一起拒了，同时上面那条等长用例也红）。"""
    port = free_port()
    session.load(oversized_string_project(port))
    session.start_channel("tcp")
    backend = session.channel("tcp").backend
    res = session.set_point("tcp", 11, "co", 0, "true")
    assert res.ok, res.error
    assert res.registers == (1,)
    assert backend._sources.cleared() == 1
    assert session.manual_count("tcp") == 1
    client = connect(port)
    try:
        assert client.read_coils(0, count=1, device_id=11).bits[0] is True
    finally:
        client.close()


# ---------- 值归属：接管与恢复（M4 spec §7） ----------

def test_the_session_hands_the_project_tick_ms_to_the_backend(session):
    """界面这一路把工程的 `tick_ms` 交给 backend（决策 8）：backend 自己默认 0，所以
    "起了通道"和"起了通道并且要它自己动"是两件事，起任务的一方必须是被交代了节拍的那一方
    （另一条是无头 CLI 的 `__main__._serve`，它把同一个工程值交下去）。

    停通道时任务要一起收掉，而且顺序在 `server.shutdown()` 之前——`stop()` 后面会把
    `_writer` 置 None，晚一步取消就是"停通道时崩一次"。
    """
    port = free_port()
    project = loaded(port)
    project.tick_ms = 25
    session.load(project)
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    assert backend.tick_ms == 25, "工程的 tick_ms 没传给 backend：界面上的曲线不会自己动"
    assert backend._tick_task is not None and not backend._tick_task.done()
    session.stop_channel("tcp-5020")
    assert backend._tick_task is None, "通道停了还留着 tick 任务：值还在自己动"


def test_a_gui_edit_hands_the_point_over_and_the_row_says_so(session):
    """spec §7：界面改过的点切成 `manual`，波形列显示"已停"，接管计数跟着加一。

    按**点**计而不是按地址计：一个 uint64 点被接管在 `SourceStore.cleared()` 里是 4 格，
    而操作员改的是"一个点"——状态栏那句"有 N 处被接管"数错了单位，人就不信那个数了。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    assert session.set_point("tcp-5020", 11, "hr", 4, "37").ok
    rows = session.rows("tcp-5020")
    assert [r.source for r in rows if r.name == "风机状态"] == ["manual"]
    assert [r.source for r in rows if r.name != "风机状态"] == ["waveform"] * 8
    assert session.manual_count("tcp-5020") == 1
    assert backend._sources.cleared() == 1


def test_the_takeover_is_recorded_on_the_engine_thread_not_the_gui_one(session,
                                                                        monkeypatch):
    """`SourceStore` 是 loop 线程的可变状态：GUI 直接往里插一脚就是跨线程共享（spec §13
    那条 GC 死锁的同族）。`set_point` 的寄存器写本来就投在 loop 线程，接管必须跟着
    **同一个** lambda 做完，所以这里比的是线程 ident 而不是"值对不对"。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    real = backend.mark_manual
    seen: list[int] = []

    def record(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(backend, "mark_manual", record)
    assert session.set_point("tcp-5020", 11, "hr", 4, "37").ok
    assert seen == [session._runner.thread_ident], (
        f"接管是在 {seen} 线程上写的，引擎线程是 {session._runner.thread_ident}")


def test_a_refused_gui_edit_does_not_take_the_point_over(session):
    """写都没写成，状态不许跟着翻：那等于"一次失败的编辑把波形停了"，
    和主站写只读点被拒是同一类误判（决策 1 的②）。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    assert not session.set_point("tcp-5020", 11, "hr", 4, "abc").ok
    assert session.manual_count("tcp-5020") == 0
    assert [r.source for r in session.rows("tcp-5020")] == ["waveform"] * 9


def test_a_write_the_backend_refused_does_not_take_the_point_over(session, monkeypatch):
    """比上面深一层：寄存器写**投进引擎之后**回 `False`（点表与 datastore 对不上——
    Task 6 改完区/地址而通道没重启就是这个形状），那次写压根没落到寄存器上，
    状态同样一个字都不许动。

    接管写在同一个 lambda 里，所以"没写成也标 manual"这条变异只有从这一层才照得出来：
    上面那条在 codec 就被挡住了，压根没走到 `_awrite`。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    monkeypatch.setattr(session.channel("tcp-5020").backend, "write_registers",
                        lambda *_a, **_k: False)
    res = session.set_point("tcp-5020", 11, "hr", 4, "37")
    assert not res.ok and "outside the point table" in res.error, res
    assert session.manual_count("tcp-5020") == 0
    assert [r.source for r in session.rows("tcp-5020")] == ["waveform"] * 9


def test_restore_waveform_hands_the_whole_point_back(session):
    """`Session.restore_waveform` 的签名只有基础地址，跨度得自己算（决策 6）：
    uint64 占 4 格，少传跨度就留 3 格在 manual 上，那三格地址此后再没有任何东西去动它们。

    这里先用界面写一个 4 格的点（`mark_manual` 的 count 也必须跟着寄存器数走，否则按地址数
    的 `cleared()` 当场少报几格），再整点交还波形，并要求日志点名恢复的是
    哪个点——静默把值交还波形等于操作员看不见发生了什么。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    assert session.set_point("tcp-5020", 11, "hr", 5, "12346789").ok   # 累计电量 uint64
    assert backend._sources.cleared() == 4, "接管只标了基础格：跨度没跟着寄存器数走"
    assert session.manual_count("tcp-5020") == 1                        # 按点数是 1
    logs: list[LogLine] = []
    session.subscribe(lambda e: isinstance(e, LogLine) and logs.append(e))
    assert session.restore_waveform("tcp-5020", 11, "hr", 5, None) is True
    assert backend._sources.cleared() == 0, "恢复留下了尾巴"
    assert session.manual_count("tcp-5020") == 0
    assert [r.source for r in session.rows("tcp-5020") if r.name == "累计电量"] == \
        ["waveform"]
    assert any("累计电量" in line.text for line in logs), logs
    # 本来就在波形手上：没恢复过任何东西，不许谎报成功
    assert session.restore_waveform("tcp-5020", 11, "hr", 5, None) is False


def test_restore_waveform_refuses_without_raising(session):
    """两条拒绝都要是一句带点位名的话，不许从 Qt 槽位里逃出 `AttributeError`：
    通道没在跑 / 地址根本不在点表，是操作员每天撞到的两件事（决策 6 与 M1 的
    `_require_writer` 同形问题）。没起的通道谈不上接管，所以 `manual_count` 回 0。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    logs: list[LogLine] = []
    session.subscribe(lambda e: isinstance(e, LogLine) and logs.append(e))
    assert session.restore_waveform("tcp-5020", 11, "hr", 30, None) is False
    assert any("not in the point table" in line.text for line in logs), logs
    logs.clear()
    session.stop_channel("tcp-5020")
    assert session.restore_waveform("tcp-5020", 11, "hr", 4, None) is False
    assert any("not running" in line.text and "风机状态" in line.text
               for line in logs), logs
    assert session.manual_count("tcp-5020") == 0


def test_the_restore_is_handed_back_on_the_engine_thread_not_the_gui_one(session,
                                                                         monkeypatch):
    """恢复侧的线程纪律，与接管侧（`test_the_takeover_is_recorded_on_the_engine_thread_
    not_the_gui_one`）成对：`SourceStore` 只许 loop 线程写（spec §13 那条 GC 死锁的同族）。

    为什么必须有这条：把 `Session.restore_waveform` 里的 `runner.call(...)` 换成主线程
    直调，状态照样恢复、计数照样归零——session+GUI 84 条全绿，跨线程共享却已经发生。
    判得了的只有线程身份：在 backend 的恢复路径里记录 ident，它必须是引擎线程的，
    且不是调用方（GUI/测试主线程）的。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    assert session.set_point("tcp-5020", 11, "hr", 4, "37").ok
    assert session.manual_count("tcp-5020") == 1
    real = backend.restore_waveform
    seen: list[int] = []

    def record(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(backend, "restore_waveform", record)
    assert session.restore_waveform("tcp-5020", 11, "hr", 4, None) is True
    assert seen and seen[0] != threading.get_ident(), (
        "恢复在调用方线程上直接跑了：主线程直插 loop 线程的 SourceStore")
    assert seen == [session._runner.thread_ident], (
        f"恢复是在 {seen} 线程上做的，引擎线程是 {session._runner.thread_ident}")
    assert session.manual_count("tcp-5020") == 0


def test_restoring_puts_the_point_back_under_the_waveform(session):
    """接管 → 波形不顶 → 恢复 → 波形又拿回去，整条路要能在一个用例里走完。

    tick 是 backend 的事、住在 loop 线程，所以这里用 `runner.call` 走一步；`now` 固定
    给 0.0，`const` 波形在任何 `t` 上都是同一个值，断言与真实时钟无关。
    """
    project = loaded(free_port())
    p = next(pt for pt in project.channels[0].devices[0].points if pt.name == "风机状态")
    p.waveform, p.wave_param = "const", {"value": "77"}
    session.load(project)
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend

    async def _step() -> int:            # `runner.call` 只收协程工厂，与 `_arestore` 同形
        return backend._tick_once(0.0)

    def tick():
        return session._runner.call(_step, 10, "test tick")
    try:
        assert session.set_point("tcp-5020", 11, "hr", 4, "5").ok
        tick()
        assert value_of(session, "风机状态") == "5"          # 被接管，波形一个字都不写
        assert session.restore_waveform("tcp-5020", 11, "hr", 4, None) is True
        tick()
        assert value_of(session, "风机状态") == "77"         # 还给波形就重新动
        assert session.restore_waveform("tcp-5020", 11, "hr", 4, None) is False
    finally:
        session.stop_channel("tcp-5020")


def test_restore_on_a_stopped_channel_is_a_named_refusal_not_an_attribute_error(session):
    """没起的通道上点"恢复波形"：给一句带点位名的拒绝，别从 Qt 槽里抛 traceback。

    与 β（拒绝要有名字）同口径：`backend` 这时是 None，裸调就是 `AttributeError`，
    界面表现为"点了没反应"。
    """
    session.load(loaded(free_port()))
    lines: list[LogLine] = []
    session.subscribe(lambda e: isinstance(e, LogLine) and lines.append(e))
    assert session.restore_waveform("tcp-5020", 11, "hr", 4, None) is False
    assert any("not running" in line.text and "风机状态" in line.text
               for line in lines), lines


def test_rows_reads_the_whole_table_in_one_cross_thread_call(session, monkeypatch):
    """跑着时 `rows()` 只跨**一次**线程（整批快照）；没跑时一次都不跨。

    钉的是"运行态只在 loop 线程读写"那条规矩（`backend/ownership.py` 开头那一段）：原来
    `rows()` 逐个点直读后端缓冲，属于跨线程直读——单个 int 安全，但 float32/uint64 这种
    **跨两格**的值在两格之间被 tick 改写时，界面上会出现一帧"谁都没写过"的数。
    收成一次快照之后，这一跳要么全看到、要么整批标 `stale`。
    """
    port = free_port()
    session.load(loaded(port))
    calls: list[str] = []
    real_call = session._runner.call

    def counting(factory, seconds, what):
        calls.append(what)
        return real_call(factory, seconds, what)

    monkeypatch.setattr(session._runner, "call", counting)
    assert len(session.rows("tcp-5020")) == 9
    assert [row.stale for row in session.rows("tcp-5020")] == [False] * 9
    assert calls == [], "没跑的时候一次跨线程调用都不该发"

    session.start_channel("tcp-5020")
    calls.clear()
    assert len(session.rows("tcp-5020")) == 9
    assert len(calls) == 1, calls
    assert calls[0].startswith("read rows of channel tcp-5020")


def test_a_wedged_engine_marks_the_rows_stale_and_says_so_only_once(session, monkeypatch):
    """引擎不回话：整批按"没读到"处理（界面灰显），**只报一次**错，值不假装是新的。

    每 100ms 一条日志能把日志窗刷干净，而"表全是灰的"本身就是看得见的信号；
    `manual_count` 这时回 `None`（"不知道"），不许拿 0 顶替成"已全部交还"。
    """
    port = free_port()
    session.load(loaded(port))
    session.start_channel("tcp-5020")
    logs: list[LogLine] = []
    session.subscribe(lambda event: isinstance(event, LogLine) and logs.append(event))
    monkeypatch.setattr(session._runner, "call",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("engine wedged")))

    rows = session.rows("tcp-5020")
    assert len(rows) == 9 and all(row.stale for row in rows), "没读到就必须整批标灰"
    assert all(row.stale for row in session.rows("tcp-5020")), "第二跳也还是灰的"
    errors = [line for line in logs if line.level == "error"]
    assert len(errors) == 1, [line.text for line in errors]
    assert "did not answer" in errors[0].text
    assert session.manual_count("tcp-5020") is None


def test_a_mixed_project_runs_every_protocol_together(session):
    """一张表里三种协议（TCP + 串口 + S7）：一起起、各自服务、状态互不牵连。

    命令行那条由 `test_cli.py::test_the_command_line_serves_every_channel_of_a_mixed_project`
    端到端钉住（三类真主站各读一次）；这一条在会话层钉"三路都在跑、谁的状态是谁的"——
    现场一张表里混装是常态，任何"只认一种协议"的地方都会在这里露出来。
    """
    from simbench.project.model import S7Channel, SerialChannel
    tcp_port, rtu_port, s7_port = free_port(), free_port(), free_port()
    project = Project(channels=[
        TcpChannel("tcp-x", host="127.0.0.1", port=tcp_port, devices=[
            Device("D1", 11, [Point("温度", "hr", 0, "uint16", initial="11")])]),
        SerialChannel("com-485-1", port=f"socket://127.0.0.1:{rtu_port}", devices=[
            Device("D2", 12, [Point("转速", "hr", 0, "uint16", initial="22")])]),
        S7Channel("s7-x", host="127.0.0.1", port=s7_port, devices=[
            Device("D3", 21, [Point("出温", "db1", 0, "word", initial="33")])]),
    ])
    session.load(project)
    assert session.start_all() == []
    assert [session.channel(name).running for name in session.channel_names()] == [True] * 3
    assert session.rows("com-485-1")[0].value_text == "22"
    assert session.rows("s7-x")[0].value_text == "33"

    # 停一路：另外两路照旧（"独立启停"这条在混装工程上同样成立）
    session.stop_channel("com-485-1")
    assert [session.channel(name).running for name in session.channel_names()] == [True, False,
                                                                                  True]
    session.stop_all()
    assert [session.channel(name).running for name in session.channel_names()] == [False] * 3


def test_dirty_flag_tracks_edits(session):
    session.load(loaded(free_port()))
    session.start_channel("tcp-5020")
    assert session.dirty is False
    session.mark_dirty()
    assert session.dirty is True
    session.load(loaded(free_port()))            # 换工程 = 重新装载，脏标记归零
    assert session.dirty is False


def test_clear_dirty_resets_the_flag(session):
    """`clear_dirty()` 只有一件事可做：改动已经落盘了，把标题栏的 `*` 收回去。

    单独钉这条是因为它是**唯一一个会把"未保存"状态抹掉的入口**：主窗在另存为取消时
    误调它，用户就看到一个存过了的假象（口径 ε 最坏的那种失效）。这里只改标记，
    不碰寄存器、不碰工程。
    """
    session.load(loaded(free_port()))
    session.mark_dirty()
    assert session.dirty is True
    session.clear_dirty()
    assert session.dirty is False
    session.clear_dirty()                        # 重复调用必须安全
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


def test_reloading_the_same_project_keeps_the_unsaved_mark(session):
    """同一份对象重装载 = **原地刷新**：改动还在这份工程里，脏标记不许清。

    界面增删之后走的正是这条路（改完 → `load(同一份)` 刷视图）。清掉的话，标题上的 `*`
    就此消失、关窗也不再问——"加完通道顺手关门"就把改动丢了。换一份**对象**才是换工程
    （`test_gui_channel_panel` 的夹具就靠那一条隔离用例之间的"未保存改动"）。
    """
    port = free_port()
    project = loaded(port)
    session.load(project)
    assert session.dirty is False
    project.tick_ms = 200                     # 随便改一处
    session.mark_dirty()
    session.load(project)                     # 同一份对象
    assert session.dirty is True, "原地刷新把'有未保存改动'抹掉了"
    session.load(loaded(free_port()))         # 另一份对象：换工程
    assert session.dirty is False
