"""`PacketStore`：报文环形缓冲 + 四个过滤桶。Qt-free，CLI 与 GUI 共用一份语义。

钉住的四件事：

1. **默认过滤器是 `全部`**（用户口径：默认给全貌，不给"跟随当前选中"）。
2. **桶的判据**：`请求`=rx、`响应`=tx，异常响应**同时**属于 `响应` 与 `异常`——
   单独成桶的话，过滤"响应"就看不见异常，而现场最想看的恰恰是它。
3. **容量与丢弃可见**：越界容量是编程错误（`ValueError`），丢掉的条数要能报出来
   （"报文窗只剩最近 N 条"必须是明说的，不能静默）。
4. **暂停不收也不通知**，`_notify` 吞掉订阅者异常（widget 先没不许带停采集）。

和 Session 的接缝只有一处：Session 用 `isinstance(event, PacketEvent)` 包一层再喂进来，
因为事件流里还有 `ChannelState`——状态事件灌进报文缓冲就是"报文窗里冒出启停记录"。
"""

import threading
import time

import pytest
from pymodbus.client import ModbusTcpClient

from simbench.backend.modbus_tcp import parse_packet
from simbench.csv_io_entry import load_point_table
from simbench.engine.packets import (
    ALL,
    EXCEPTIONS,
    FILTERS,
    REQUESTS,
    RESPONSES,
    PacketStore,
    filters_for,
)
from simbench.engine.runner import EngineRunner
from simbench.engine.session import Session
from simbench.events import PacketEvent
from tests.conftest import free_port


def evt(direction="rx", unit=11, fc=3, mono=0.0, channel="tcp-5020"):
    return PacketEvent(channel, direction, b"\x00\x01", unit, fc, f"unit {unit} fc {fc}", mono)


def test_default_filter_is_everything():
    store = PacketStore()
    assert store.active_filter == ALL
    store.add(evt())
    store.add(evt("tx", 77, 0x83))
    assert [p.direction for p in store.rows()] == ["rx", "tx"]


def test_buckets_are_disjoint_and_exhaustive():
    """除"全部"之外每个事件只落一个桶，且异常帧同时属于响应。"""
    assert filters_for(evt()) == {ALL, REQUESTS}
    assert filters_for(evt("tx", 1, 3)) == {ALL, RESPONSES}
    assert filters_for(evt("tx", 77, 0x83)) == {ALL, RESPONSES, EXCEPTIONS}


def test_a_frame_without_a_function_code_is_not_an_exception():
    """短帧/半帧的 `function_code` 是 None（backend 的 parse_packet 就是这么回的）：
    判异常要按 `(fc or 0) & 0x80`，不许在 None 上做位运算。"""
    assert filters_for(evt("tx", 11, None)) == {ALL, RESPONSES}


def test_the_exception_shape_the_backend_really_emits_is_an_exception():
    """M1 的 `parse_packet` 已经把异常帧的功能码**脱掉 0x80**（实测回 `(77, 3,
    'unit 77 fc 3 EXCEPTION 4')`），所以只看 `fc & 0x80` 的判据对真从站的异常响应恒不成立
    ——GUI 的"异常"标签会一条都没有，而现场最想看的恰恰是它。

    这里用真 `parse_packet` 造事件、不抄它的文案：backend 的措辞一改这条就红，
    不会让 filters_for 悄悄和契约漂移。
    """
    unit, function, summary = parse_packet(b"\x00\x03\x00\x00\x00\x03\x4d\x83\x04")
    assert (unit, function) == (77, 3) and "EXCEPTION 4" in summary   # M1 已钉的形状
    event = PacketEvent("tcp-5020", "tx", b"\x4d\x83\x04", unit, function, summary, 0.0)
    assert filters_for(event) == {ALL, RESPONSES, EXCEPTIONS}


def test_a_raw_exception_function_code_is_still_an_exception():
    """另一种真形状：线上字节的功能码带 0x80（M5 的 RTU 监听、以后直接灌裸帧的路）。"""
    assert filters_for(evt("tx", 77, 0x83)) == {ALL, RESPONSES, EXCEPTIONS}


def test_filter_names_are_exactly_the_four_labels():
    assert FILTERS == ("全部", "请求", "响应", "异常")


def test_set_filter_rejects_unknown_name():
    store = PacketStore()
    with pytest.raises(ValueError, match="unknown packet filter"):
        store.set_filter("最近一小时")
    assert store.active_filter == ALL          # 拒绝时不改原状态


def test_filter_narrows_rows():
    store = PacketStore()
    store.add(evt())
    store.add(evt("tx", 77, 0x83))
    store.set_filter(EXCEPTIONS)
    assert [p.direction for p in store.rows()] == ["tx"]
    store.set_filter(REQUESTS)
    assert [p.direction for p in store.rows()] == ["rx"]


def test_ring_buffer_drops_oldest_and_counts_it():
    store = PacketStore(capacity=3)
    for i in range(5):
        store.add(evt(mono=float(i)))
    assert [p.mono for p in store.rows()] == [2.0, 3.0, 4.0]
    assert store.dropped == 2


def test_pausing_stops_ingest_but_keeps_rows_readable():
    store = PacketStore()
    store.add(evt(mono=1.0))
    store.paused = True
    store.add(evt(mono=2.0))
    assert [p.mono for p in store.rows()] == [1.0]
    store.paused = False
    store.add(evt(mono=3.0))
    assert [p.mono for p in store.rows()] == [1.0, 3.0]


def test_clear_keeps_capacity():
    store = PacketStore(capacity=4)
    store.add(evt())
    store.clear()
    assert store.rows() == [] and store.dropped == 0
    store.add(evt(mono=9.0))
    assert [p.mono for p in store.rows()] == [9.0]


def test_bad_capacity_is_refused():
    with pytest.raises(ValueError, match="capacity"):
        PacketStore(capacity=0)


def test_listener_notified_once_per_accepted_packet():
    store = PacketStore()
    hits: list[int] = []
    store.subscribe(lambda: hits.append(1))
    store.add(evt())
    store.paused = True
    store.add(evt())                       # 暂停时不收，也就不通知
    assert len(hits) == 1


def test_unsubscribe_stops_notifications():
    """面板关窗要能退订，否则测试与真实关窗都会留下悬空回调（往已销毁的 widget 上刷）。"""
    store = PacketStore()
    hits: list[int] = []
    store.subscribe(hits.append)
    store.unsubscribe(hits.append)
    store.add(evt())
    assert hits == []
    store.unsubscribe(hits.append)             # 重复退订是安全的


def test_a_broken_listener_does_not_stop_the_store():
    store = PacketStore()
    store.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("widget gone")))
    assert store.add(evt()) is True
    assert len(store.rows()) == 1


def test_a_broken_listener_does_not_block_the_healthy_one():
    """一个订阅者炸了，后面那个照样收到：界面重绘失败不该把日志落盘一起带停。"""
    store = PacketStore()
    seen: list[int] = []
    store.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("widget gone")))
    store.subscribe(lambda: seen.append(1))
    store.add(evt())
    store.set_filter(REQUESTS)
    store.clear()
    assert seen == [1, 1, 1]


def test_rows_is_safe_while_the_pump_thread_keeps_appending():
    """GUI 10Hz 读 + 引擎 pump 线程写，两条线程碰同一份缓冲。

    过滤分支要遍历整段：实测不加保护时推导式中途被插一条就抛
    `RuntimeError: Mutation detected while iterating a deque`——
    模拟器自己的竞态不许在现场演成"报文窗崩了/主站读不到"。
    """
    store = PacketStore(capacity=200)
    store.set_filter(REQUESTS)             # 走"需要遍历整段"的那条读路径
    stop = threading.Event()
    failures: list[BaseException] = []
    produced = [0]

    def producer() -> None:
        i = 0
        while not stop.is_set():
            store.add(evt("rx" if i % 2 else "tx", mono=float(i)))
            i += 1
        produced[0] = i

    def consumer() -> None:
        try:
            while not stop.is_set():
                store.rows()
        except BaseException as exc:
            failures.append(exc)
            stop.set()

    threads = [threading.Thread(target=producer, daemon=True),
               threading.Thread(target=producer, daemon=True),
               threading.Thread(target=consumer, daemon=True)]
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(5)
    assert failures == [], f"读写竞态把异常抛到了采集路径上：{failures}"
    assert produced[0] > 0


@pytest.fixture
def session_store():
    """`yield (session, store, port)`：真 Session 喂真缓冲，收尾把 runner 也收掉。

    runner 由本夹具持有并传进 Session，所以 `session.shutdown()` **不会**停它
    （`_owns_runner` 为 False，M2 的归属口径）——不在这里 `runner.stop()`，
    每条端到端用例都会留下 `simbench-engine` + `-events` 两个线程。
    """
    project, _notes, issues = load_point_table("examples/covi-tunnel.csv")
    assert not [i for i in issues if i.level == "error"]
    port = free_port()
    project.channels[0].port = port
    store = PacketStore()
    runner = EngineRunner()
    session = Session(runner=runner, packet_store=store)
    try:
        session.load(project)
        yield session, store, port
    finally:
        session.shutdown()
        runner.stop()


def test_session_feeds_the_store_with_the_real_channel_name(session_store):
    """端到端一句：起服务、主站读一次，缓冲里就该有 rx/tx 两条，且通道名是真的。"""
    session, store, port = session_store
    session.start_channel("tcp-5020")
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        client.read_holding_registers(0, count=1, device_id=11)
    finally:
        client.close()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(store.rows()) < 2:
        time.sleep(0.02)
    directions = [p.direction for p in store.rows()]
    assert "rx" in directions and "tx" in directions
    assert {p.channel for p in store.rows()} == {"tcp-5020"}


def test_session_does_not_put_channel_state_into_the_packet_store(session_store):
    """Session 的事件流里除 PacketEvent 还有 ChannelState：状态事件**不许**灌进报文缓冲，
    否则报文窗会出现"启停记录"，而过滤桶按功能码判会把一条没有功能码的状态当响应。"""
    session, store, _port = session_store
    session.start_channel("tcp-5020")
    session.stop_channel("tcp-5020")
    assert store.rows() == [], [vars(p) for p in store.rows()]


def test_a_real_master_exception_shows_up_in_response_and_exception_only(session_store):
    """真主站跑一轮"正常读 + 写只读被拒"，四个桶各数一遍。

    这条是上面那条单元用例的现场版：过滤器判据如果只按"功能码带 0x80"写（计划正文的
    写法），异常桶在这里会是 **0 条**——报文窗于是对平台说"没有异常"，
    而屏幕上方的从站明明刚回了异常码 2。这种"把没读到演成没发生"正是本项目要防的形状。
    """
    session, store, port = session_store
    session.start_channel("tcp-5020")
    client = ModbusTcpClient("127.0.0.1", port=port)
    assert client.connect()
    try:
        assert not client.read_holding_registers(4, count=1,
                                                 device_id=11).isError()
        denied = client.write_register(0, 1, device_id=11)
        assert denied.isError() and denied.exception_code == 2
    finally:
        client.close()
    session.wait_idle()
    store.set_filter(REQUESTS)
    assert [e.function_code for e in store.rows()] == [3, 6]        # 两帧请求
    store.set_filter(RESPONSES)
    assert len(store.rows()) == 2                                # 正常响应 + 异常响应
    store.set_filter(EXCEPTIONS)
    exceptions = store.rows()
    assert len(exceptions) == 1, [vars(e) for e in exceptions]
    assert exceptions[0].direction == "tx" and "EXCEPTION 2" in exceptions[0].parsed
    assert exceptions[0].channel == "tcp-5020"
    store.set_filter(ALL)
    assert len(store.rows()) == 4
