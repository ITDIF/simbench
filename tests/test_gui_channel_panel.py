"""通道面板 `gui/channel_panel.py`：每通道一行 host/port + 独立启停 + 定宽状态标签。

为什么必须有这一层：spec §11 把"通道启停"列进 M2，只有工具栏"全部启动/全部停止"时，
现场接了一路 485、想单起一个 TCP 通道就做不到。

钉住的三件事：

1. **启停是每通道独立的**，一个通道启动不许带动另一个。
2. **跑着的通道锁住 host/port 输入框**（`disabled`，不是隐藏）：那时改的就不是正在
   提供服务的那个监听，界面允许你改 = 演一出"改了就会生效"的假事。
3. **状态变化不挪布局**：未启动/启动中/运行/失败四态只换文字与颜色，标签是定宽槽位，
   文案必须**装得下**（不许加宽、不许截断省略号）。

`apply_edits` 的判定分工（避免和 `validate_project` 两处各判一套）：面板只做**转换**
（输入框里的文字能不能变成一个端口号），端口范围/host 非空/端口重复这些**判定**全部
留在 `project/validate.py` 那一份里——先写回工程再验，验出面上的改动新增了错就整体回滚，
所以现场看到的错误文案永远和启动前自检是同一套话。
"""

import pytest
from PySide6 import QtWidgets

from simbench.backend.events import ChannelState, LogLine, PacketEvent
from simbench.csv_io_entry import load_point_table
from simbench.engine.runner import EngineRunner
from simbench.engine.session import Session
from simbench.gui.channel_panel import STATUS_LABELS, ChannelPanel
from simbench.project.model import TcpChannel
from tests.conftest import free_port

CSV = "examples/covi-tunnel.csv"


@pytest.fixture
def project():
    loaded, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"]
    loaded.channels[0].port = free_port()
    loaded.channels.append(TcpChannel(name="tcp-2", host="127.0.0.1",
                                      port=free_port(), devices=[]))
    return loaded


@pytest.fixture(scope="module")
def engine():
    """一个模块一条引擎线程 + 一份 Session（生产形态就是一个 App 一个 runner、一个会话）。

    实测口径：每条用例自建一对线程会往同一次 pytest 里塞 24 个 asyncio loop，和 M1
    真主站门禁的 `.result(10)` / `join(5)` 抢调度——全量 45s 并偶发引擎线程停摆；
    共用之后只建 1 个 loop，全量 21s 且连跑三次不红。
    用例之间的隔离由 `built` 负责：每条都 `load()` 一份新工程，load 会停掉上一条
    留在共享 Session 里的通道，并把 dirty 清回 False。
    """
    runner = EngineRunner()
    runner.start()
    session = Session(runner=runner)
    yield session
    session.shutdown()
    runner.stop()


@pytest.fixture
def built(qapp, project, engine):
    """session 先 load 再 rebuild：面板的启停都靠 session 里的通道表，
    只喂面板一个 project 而不装载，点"启动"会砸在 KeyError 上（计划正文就漏了这步）。

    `load()` 顺带把上一条用例留在共享 Session 里的通道停掉、并把 dirty 清回 False，
    所以共享会话不会把上一条用例的运行状态或"未保存改动"漏给下一条。
    """
    engine.load(project)
    panel = ChannelPanel(engine)
    panel.rebuild(engine.project)
    holder = QtWidgets.QWidget()
    QtWidgets.QVBoxLayout(holder).addWidget(panel)
    holder.resize(680, 160)
    holder.show()
    qapp.processEvents()
    yield panel, engine, qapp
    # 通道必须在本条用例结束时就停掉。等"下一条用例 load() 时顺手停"会留出一个
    # 窗口期：上一个从站还在监听，后面任何用例的 client 都可能连到它身上，
    # 表现成"跨用例串扰"而不是"泄漏"（实测过一次 test_uint64_big_endian 读到
    # 别的用例写进去的值）。conftest 的监听泄漏检查器就是为了钉住这件事。
    engine.stop_all()
    panel.deleteLater()
    holder.deleteLater()
    qapp.processEvents()


def by_channel(panel):
    return {row.channel: row for row in panel.rows()}


# ---------- 每通道一行 ----------

def test_one_row_per_channel_with_both_buttons(built):
    panel, _session, _qapp = built
    rows = panel.rows()
    assert [r.channel for r in rows] == ["tcp-5020", "tcp-2"]
    for row in rows:
        assert row.start_button.text() == "启动"
        assert row.stop_button.text() == "停止"
        assert row.start_button.isVisible() and row.stop_button.isVisible()


def test_the_row_shows_the_channels_own_address(built):
    panel, session, _qapp = built
    rows = by_channel(panel)
    channel = session.project.find_channel("tcp-2")
    assert rows["tcp-2"].host_edit.text() == channel.host
    assert rows["tcp-2"].port_edit.text() == str(channel.port)


def test_rebuild_replaces_the_rows_instead_of_doubling_them(built):
    """换工程后重画：行数还是通道数，不留上一份工程的残行（残行会指向已经不存在的通道）。"""
    panel, session, _qapp = built
    panel.rebuild(session.project)
    assert [r.channel for r in panel.rows()] == ["tcp-5020", "tcp-2"]


# ---------- 独立启停 ----------

def test_the_row_packs_its_widgets_to_the_left(built):
    """行内容左对齐：通道名与第一格之间不许留一块空白（多出来的宽度归**行尾**）。

    原来没有尾部拉伸列，宽度全落进第一格"通道名"里——截图里整行被摊成"名字贴最左、
    参数与按钮贴最右"两撮，中间一大片空。这里按几何量，不靠肉眼：名字与第一格挨着，
    且第一格仍在面板的左半边。
    """
    panel, _session, qapp = built
    qapp.processEvents()
    assert panel.rows(), "这条用例要的是'有行可量'"
    for row in panel.rows():
        gap = row.host_edit.geometry().left() - row.name_label.geometry().right()
        assert 0 <= gap <= 16, (row.channel, gap)
        assert row.host_edit.geometry().left() < panel.width() // 2, (
            f"{row.channel}: 第一格被推到右半边了，说明宽度还是全落在通道名那一格上")


def test_start_is_per_channel(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    assert session.channel("tcp-2").running
    assert not session.channel("tcp-5020").running, "一个通道启动不该带动另一个"


def test_stop_is_per_channel(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    rows["tcp-2"].stop_button.click()
    qapp.processEvents()
    assert not session.channel("tcp-2").running
    assert rows["tcp-2"].status.text() == STATUS_LABELS["stopped"]


def test_a_channel_that_fails_to_start_shows_failure_and_leaves_the_other_alone(
        built, monkeypatch):
    """起不来要变红字、另一个通道不受影响，而且失败原因要留在面板上（现场靠它认端口）。"""
    panel, session, qapp = built
    rows = by_channel(panel)
    real = session.start_channel

    def refuse(name):
        if name == "tcp-2":
            raise RuntimeError("port 502 may already be in use")
        return real(name)

    monkeypatch.setattr(session, "start_channel", refuse)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    assert rows["tcp-2"].status.text() == STATUS_LABELS["error"]
    assert "port 502" in panel.last_error
    assert session.channel("tcp-2").running is False


def test_start_refusal_is_reported_and_logged(built):
    """点表有 error 时 `start_channel` 抛 ValueError：面板既要留下 last_error，
    也要把同一句话作为 `LogLine` 发出去——状态栏/日志窗和那一行必须是同一份话术。"""
    panel, session, _qapp = built
    seen: list[object] = []
    session.subscribe(seen.append)
    session.project.tick_ms = 5                       # 一条 project 级 error，一次报全
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    assert "refusing to start" in panel.last_error
    assert rows["tcp-2"].status.text() == STATUS_LABELS["error"], "点了启动没成，不许装作还在停止态"
    assert [e for e in seen if isinstance(e, LogLine) and e.level == "error"], seen
    assert rows["tcp-2"].start_button.isEnabled(), "被拒的通道还能再点，按钮不许消失"


# ---------- 跑着的通道锁住地址 ----------

def test_running_channel_locks_its_address_fields(built):
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    assert not rows["tcp-2"].port_edit.isEnabled(), "跑着还能改端口，改的就不是运行的那个了"
    assert not rows["tcp-2"].host_edit.isEnabled()
    assert rows["tcp-2"].stop_button.isEnabled()
    assert rows["tcp-2"].port_edit.isVisible(), "锁定是 disabled，不是藏起来"
    rows["tcp-2"].stop_button.click()
    qapp.processEvents()
    assert rows["tcp-2"].port_edit.isEnabled()
    assert not rows["tcp-2"].stop_button.isEnabled(), "没跑着还留着可点的停止按钮"


def test_a_running_channel_refuses_an_edit_even_with_a_legal_value(built):
    """在跑的通道改地址：合法值也拒，并说明原因（不是"改成功了但没生效"）。"""
    panel, session, qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].start_button.click()
    qapp.processEvents()
    port = free_port()
    rows["tcp-2"].port_edit.setText(str(port))
    assert panel.apply_edits("tcp-2") is None
    assert "running" in panel.last_error
    assert session.project.find_channel("tcp-2").port != port


# ---------- 地址编辑的判定分工 ----------

def test_bad_port_is_refused_and_reported(built):
    panel, session, _qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].port_edit.setText("COM3")
    assert panel.apply_edits("tcp-2") is None
    assert "port" in panel.last_error
    assert session.project.find_channel("tcp-2").port != "COM3"


def test_valid_port_reaches_the_project(built):
    panel, session, _qapp = built
    rows = by_channel(panel)
    port = free_port()
    rows["tcp-2"].port_edit.setText(str(port))
    assert panel.apply_edits("tcp-2") == port
    assert session.project.find_channel("tcp-2").port == port


def test_empty_host_is_refused(built):
    panel, session, _qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].host_edit.setText("   ")
    assert panel.apply_edits("tcp-2") is None
    assert "host" in panel.last_error
    assert session.project.find_channel("tcp-2").host == "127.0.0.1"


def test_an_out_of_range_port_is_refused_by_the_validator_not_a_second_copy(built):
    """范围判定只有 `validate_project` 一份：面板不许自己再抄一个 1..65535。

    抄了就会出现"面板放过、启动前自检拦下"或反之的两套口径，而现场只看得到其中一套。
    断言用 validator 的原话（`must be in 1..65535`），面板改文案就会红。
    """
    panel, session, _qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].port_edit.setText("70000")
    assert panel.apply_edits("tcp-2") is None
    assert "must be in 1..65535" in panel.last_error
    assert session.project.find_channel("tcp-2").port != 70000


def test_a_port_already_used_on_the_same_host_is_refused(built):
    """同 host+port 的冲突是 validate 的 `port_duplicated`（裁决 R）：验出新增错就当没改。

    反方向也要钉住：**不同 host 同端口是合法配置**，面板不许为了"看着像冲突"自己再加
    一套判据——两份判定迟早漂移，漂了就是"面板放行的配置启动时被拦下"那种假 bug。
    """
    panel, session, _qapp = built
    rows = by_channel(panel)
    other = session.project.find_channel("tcp-5020")
    taken = other.port
    rows["tcp-2"].host_edit.setText(other.host)
    rows["tcp-2"].port_edit.setText(str(taken))
    assert panel.apply_edits("tcp-2") is None
    assert "already used" in panel.last_error
    channel = session.project.find_channel("tcp-2")
    assert channel.port != taken and channel.host == "127.0.0.1", "host/port 要一起回滚"
    rows["tcp-2"].port_edit.setText(str(taken))        # host 还是 127.0.0.1
    assert panel.apply_edits("tcp-2") == taken
    assert session.project.find_channel("tcp-2").port == taken


def test_a_refused_edit_snaps_the_fields_back_to_what_is_really_in_effect(built):
    """被拒的改动要把输入框也退回原值：留着一个没生效的新端口在下一次编辑时被当成现值，
    下一次"合法"的改动就会基于一个根本不存在的地址。"""
    panel, session, _qapp = built
    rows = by_channel(panel)
    before = session.project.find_channel("tcp-2").port
    rows["tcp-2"].port_edit.setText("70000")
    assert panel.apply_edits("tcp-2") is None
    assert rows["tcp-2"].port_edit.text() == str(before)


def test_a_successful_edit_clears_the_previous_error(built):
    panel, _session, _qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].port_edit.setText("COM3")
    panel.apply_edits("tcp-2")
    assert panel.last_error
    rows["tcp-2"].port_edit.setText(str(free_port()))
    assert panel.apply_edits("tcp-2") is not None
    assert panel.last_error == ""


def test_a_successful_edit_marks_the_project_dirty(built):
    """口径 ε：改了 host/port 就是**未保存的工程改动**，必须置 dirty。

    主窗靠 `Session.dirty` 决定标题带不带 `*`、关窗问不问一句。这里不置位的话，
    现场改完端口直接关窗，下次开的是 CSV 里的旧端口——和"改了没生效"同一个症状。
    """
    panel, session, _qapp = built
    assert session.dirty is False
    rows = by_channel(panel)
    rows["tcp-2"].port_edit.setText(str(free_port()))
    assert panel.apply_edits("tcp-2") is not None
    assert session.dirty is True


def test_a_refused_edit_leaves_the_dirty_flag_alone(built):
    """被拒的改动什么都没换，所以也不许把工程标成"有未保存改动"。"""
    panel, session, _qapp = built
    rows = by_channel(panel)
    rows["tcp-2"].port_edit.setText("COM3")
    assert panel.apply_edits("tcp-2") is None
    assert session.dirty is False


# ---------- 口径 3/4：几何与文案 ----------

def test_geometry_is_stable_in_every_state(built):
    """状态文字换内容/换颜色不许推动行宽（用户口径：状态变化不挪布局）。"""
    panel, session, qapp = built
    rows = by_channel(panel)
    baseline = panel.geometry_snapshot()
    for state in ("starting", "running", "error", "stopped"):
        panel.set_status("tcp-2", state)
        qapp.processEvents()
        assert panel.geometry_snapshot() == baseline, f"{state} 之后通道面板几何变了"
    rows["tcp-2"].start_button.click()                 # 真起一次：走面板自己的那条路
    qapp.processEvents()
    assert panel.geometry_snapshot() == baseline, "启动后通道面板几何变了"
    assert session.channel("tcp-2").running


def test_the_status_label_is_the_same_slot_in_every_state(built):
    """四态同一格：只换文字与颜色，控件不增删、宽度不变、也绝不出现省略号（口径 4）。"""
    panel, _session, qapp = built
    label = by_channel(panel)["tcp-2"].status
    widths = set()
    for state, text in STATUS_LABELS.items():
        panel.set_status("tcp-2", state)
        qapp.processEvents()
        assert label.text() == text
        assert "…" not in label.text() and "..." not in label.text()
        advance = label.fontMetrics().horizontalAdvance(text)
        assert advance <= label.width(), (text, advance, label.width())
        widths.add(label.width())
    assert len(widths) == 1, widths


def test_the_widgets_do_not_disappear_across_states(built):
    panel, _session, qapp = built
    before = visibility_snapshot(by_channel(panel)["tcp-2"])
    for state in STATUS_LABELS:
        panel.set_status("tcp-2", state)
        qapp.processEvents()
        assert visibility_snapshot(by_channel(panel)["tcp-2"]) == before, state


def visibility_snapshot(row) -> list[tuple[int, bool]]:
    """一行里的每个控件：身份 + 可见性。增删或隐藏都会在这里露出来。"""
    widgets = [row.name_label, row.host_edit, row.port_edit,
               row.start_button, row.stop_button, row.status]
    return [(id(w), w.isVisible()) for w in widgets]


def test_set_status_rejects_an_unknown_state(built):
    """未知状态是编程错误，不许当成"没发生"静悄悄：那会留下一格旧状态冒充现况。"""
    panel, _session, _qapp = built
    with pytest.raises(ValueError, match="unknown channel state"):
        panel.set_status("tcp-2", "paused")


def test_the_main_window_can_push_a_channel_state_into_the_row(built):
    """面板自己不订阅 Session（回调可能来自引擎线程），由主窗/桥把事件转进来。"""
    panel, _session, qapp = built
    panel.on_event(ChannelState("tcp-2", "error", "port 502 may already be in use"))
    qapp.processEvents()
    row = by_channel(panel)["tcp-2"]
    assert row.status.text() == STATUS_LABELS["error"]
    assert row.port_edit.isEnabled(), "error 不是 running：那个通道没在跑，地址还得能改"


def test_on_event_ignores_events_that_are_not_channel_state(built):
    """报文/日志事件不该动那一格：否则一条报文就能把"运行中"刷成别的状态。"""
    panel, _session, _qapp = built
    before = panel.geometry_snapshot()
    panel.on_event(PacketEvent("tcp-2", "rx", b"\x00\x01", 11, 3, "unit 11 fc 3", 0.0))
    panel.on_event(LogLine("info", "hello"))
    assert panel.geometry_snapshot() == before
    assert by_channel(panel)["tcp-2"].status.text() == STATUS_LABELS["stopped"]


def test_set_status_for_a_channel_that_is_not_on_the_panel_is_ignored(built):
    """状态事件可能比 rebuild 先到（换工程的一瞬间）：没这一行就什么也不动，不崩。"""
    panel, _session, _qapp = built
    before = panel.geometry_snapshot()
    panel.set_status("tcp-9", "running")
    assert panel.geometry_snapshot() == before
