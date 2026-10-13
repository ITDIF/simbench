"""拿真实主站打这个从站。断言里的期望值全部来自 spec §5/§6 与探针实测，不是从实现倒推。

这条文件是 M1 的门禁：模拟器的 bug 会被当成平台 bug 去调，所以八个功能码、异常路径、
字序、多 unit 路由、总线扫描、反复连接全部钉成自动回归。
每处期望值后面的 `事实 n` 指计划开头"已实测事实（三轮探针结论）"的第 n 条。
"""

import asyncio
import contextlib
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest
from pymodbus.client import ModbusTcpClient

from simbench.backend.events import LogLine
from simbench.backend.modbus_tcp import (
    _DropDatastoreNoise, ModbusTcpBackend, PortInUseError, _bind_probe,
    _connect_probe, _is_being_served, check_port_is_free, configure_library_logging,
    parse_packet,
)
from simbench.codec.point_codec import encode
from simbench.engine.session import Session
from simbench.project.model import Device, Point, Project, TcpChannel
from simbench.project.validate import validate_project
from tests.conftest import free_port

# 一张典型隧道设备点表：四个区都有真点位，字序/类型/只读各覆盖到
POINTS = [
    Point("CO浓度", "hr", 0, "float32", initial="12.5", word_order="CDAB", readonly=True),
    Point("VI风速", "hr", 2, "float32", initial="3.25", word_order="ABCD"),
    Point("状态字", "hr", 4, "uint16", initial="11"),
    Point("累计电量", "hr", 5, "uint64", initial="72623859790382856", word_order="ABCD"),
    Point("铭牌", "hr", 9, "string", initial="FAN-01"),
    Point("启动", "co", 0, "bool", initial="false"),
    Point("故障", "di", 0, "bool", initial="true"),
    Point("版本", "ir", 0, "uint16", initial="7", readonly=True),
]

# 隧道现场最常见的另一台设备：只有保持寄存器，co/di/ir 三个区是空的（走占位）
BARE_POINTS = [Point("温度", "hr", 0, "uint16", initial="7")]


def regs_to_float(registers) -> float:
    """主站按 ABCD（大端）解读两个寄存器。"""
    return struct.unpack(">f", struct.pack(">HH", *registers))[0]


def cdab_to_float(registers) -> float:
    """主站按 CDAB 解读：先交换两个字再大端 unpack。

    计划原文在这里用的是 `regs_to_float`，**实测对不上**：`encode(12.5,'float32','CDAB')`
    = `[0x0000, 0x4148]`，按 ABCD unpack 是 `2.34e-41`（实测输出见报告），只有交换两个字
    才是 12.5。CDAB 的定义就是"低字在前"，主站侧必须换回去——这条不是把期望值改成"能过"，
    而是把 helper 补齐成主站真实会做的动作，寄存器序列本身仍然按事实 1 钉死。
    """
    return struct.unpack(">f", struct.pack(">HH", registers[1], registers[0]))[0]


def _points(items):
    return [Point(p.name, p.area, p.address, p.datatype, initial=p.initial,
                  word_order=p.word_order, readonly=p.readonly) for p in items]


def tunnel_project(port: int, unit_id: int = 11) -> Project:
    dev = Device(name="风机-1", unit_id=unit_id, points=_points(POINTS))
    other = Device(name="COVI-2", unit_id=unit_id + 1,
                   points=[Point("CO", "hr", 0, "uint16", initial="99")])
    return Project(channels=[TcpChannel(name="tcp", host="127.0.0.1", port=port,
                                        devices=[dev, other])])


def bare_project(port: int, unit_id: int = 11) -> Project:
    dev = Device(name="照明回路", unit_id=unit_id, points=_points(BARE_POINTS))
    return Project(channels=[TcpChannel(name="tcp", host="127.0.0.1", port=port,
                                        devices=[dev])])


def connect(port: int) -> ModbusTcpClient:
    """端口就绪由 loop 线程异步完成，重试到连上为止（最多 5 秒）。

    `start()` 现在已经等到 listener 起来才返回，这里保留重试只是保险：真连不上时
    这条 `AssertionError` 说清是哪个端口，不让人去猜"夹具是不是没起来"。
    """
    deadline = time.monotonic() + 5.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        client = ModbusTcpClient("127.0.0.1", port=port)
        try:
            if client.connect():
                return client
        except Exception as exc:            # noqa: BLE001
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"could not connect to :{port} ({last})")


def wait_for(predicate, timeout: float = 5.0, what: str = "") -> None:
    """等一个由 loop 线程异步送达的条件（连接关闭回调一类），到点就失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s: {what}")


@pytest.fixture
def master(open_backend):
    port = free_port()
    with open_backend(tunnel_project(port).channels[0]) as backend:
        client = connect(port)
        try:
            yield client, backend
        finally:
            client.close()


@pytest.fixture
def bare_master(open_backend):
    """只有保持寄存器的一台设备：空 co/di/ir 三个区全靠占位条目。"""
    port = free_port()
    with open_backend(bare_project(port).channels[0]) as backend:
        client = connect(port)
        try:
            yield client, backend
        finally:
            client.close()


def test_project_under_test_is_valid():
    assert validate_project(tunnel_project(5020)) == []
    assert validate_project(bare_project(5020)) == []


def test_float32_cdab_initial_value_reaches_master(master):
    """CDAB 读成天价是现场最常见的坑，这条是核心断言。

    寄存器序列按事实 1 钉死（`count=1` + 值列表，不做重复倍数），数值按"低字在前"的
    CDAB 定义换回去才是 12.5。
    """
    client, _ = master
    got = client.read_holding_registers(0, count=2, device_id=11)
    assert got.registers == [0x0000, 0x4148]
    assert cdab_to_float(got.registers) == pytest.approx(12.5)
    # 反向钉一次：不懂换字序的主站（按 ABCD 读）拿到的就是 2e-41 这种天价
    assert regs_to_float(got.registers) != pytest.approx(12.5)


def test_big_endian_float32(master):
    client, _ = master
    got = client.read_holding_registers(2, count=2, device_id=11)
    assert got.registers == [0x4050, 0x0000]
    assert regs_to_float(got.registers) == pytest.approx(3.25)


def test_uint64_big_endian(master):
    """64 位 ABCD：72623859790382856 = 0x0102030405060708。"""
    client, _ = master
    got = client.read_holding_registers(5, count=4, device_id=11)
    assert got.registers == [0x0102, 0x0304, 0x0506, 0x0708]


def test_string_point(master):
    """string 只支持 ABCD（spec 修正 1）：FAN-01 -> 46 41 4e 2d 30 31。"""
    client, _ = master
    got = client.read_holding_registers(9, count=3, device_id=11)
    assert got.registers == [0x4641, 0x4e2d, 0x3031]
    text = b"".join(r.to_bytes(2, "big") for r in got.registers)
    assert text.split(b"\x00")[0] == b"FAN-01"


def test_function_code_matrix(master):
    """01/02/03/04/05/06/15/16 全过一遍真主站。"""
    client, _ = master
    unit = 11
    assert client.read_coils(0, count=1, device_id=unit).bits[0] is False        # 01
    assert client.read_discrete_inputs(0, count=1, device_id=unit).bits[0] is True   # 02
    assert client.read_holding_registers(4, count=1, device_id=unit).registers == [11]    # 03
    assert client.read_input_registers(0, count=1, device_id=unit).registers == [7]       # 04

    assert client.write_coil(0, True, device_id=unit).bits[0] is True            # 05
    assert client.read_coils(0, count=1, device_id=unit).bits[0] is True
    assert client.write_coils(0, [False], device_id=unit) is not None            # 15
    assert client.read_coils(0, count=1, device_id=unit).bits[0] is False

    assert client.write_register(4, 777, device_id=unit).registers == [777]      # 06
    assert client.read_holding_registers(4, count=1, device_id=unit).registers == [777]
    assert client.write_registers(4, [1, 2], device_id=unit) is not None         # 16
    assert client.read_holding_registers(4, count=2, device_id=unit).registers == [1, 2]


def test_engine_side_write_is_visible_to_master(master):
    """引擎（Task 9 的直写路径）写的值，真主站立刻读得到——事实 3 的端到端版。"""
    client, backend = master
    assert backend.write_registers(11, "hr", 4, [999]) is True
    assert client.read_holding_registers(4, count=1, device_id=11).registers == [999]


def test_master_write_is_visible_to_engine(master):
    """反过来的那条链路：平台下发被设备接受后，引擎侧读得回同一个值。

    M1 验收里"平台下发"就靠这条最小证明——主站写进去的值只改协议路径可读的寄存器列表，
    引擎若读到旧值，GUI 的当前值就是假的。
    """
    client, backend = master
    assert client.write_register(4, 4242, device_id=11).registers == [4242]      # FC06
    assert backend.read_registers(11, "hr", 4, 1) == [4242]
    assert client.write_registers(4, [7, 8], device_id=11) is not None           # FC16
    assert backend.read_registers(11, "hr", 4, 2) == [7, 8]
    assert client.write_coil(0, True, device_id=11).bits[0] is True              # FC05
    assert backend.read_bits(11, "co", 0, 1) == [True]


def test_readonly_point_blocks_master_but_not_engine(master):
    """CO浓度 标了 readonly：主站写它必须收到异常码 2，引擎自己照样能刷值（事实 3）。"""
    client, backend = master
    rejected = client.write_registers(0, [1, 2], device_id=11)
    assert rejected.isError() and rejected.exception_code == 2
    assert backend.write_registers(11, "hr", 0, encode(44.0, "float32", "CDAB")) is True
    assert cdab_to_float(
        client.read_holding_registers(0, count=2, device_id=11).registers) == \
        pytest.approx(44.0)


def test_undefined_register_address_returns_exception_2(master):
    """点表不需要把地址铺满：空洞被 `__create_block` 标成 INVALID，主站读它就是 2（事实 5）。"""
    client, _ = master
    gap = client.read_holding_registers(30, count=1, device_id=11)
    assert gap.isError() and gap.exception_code == 2
    # 多字点位内部的寄存器也算"已定义"：float32@CDAB 占 0..1，读第 2 个照常给值
    inside = client.read_holding_registers(1, count=1, device_id=11)
    assert not inside.isError() and inside.registers == [0x4148]


def test_bit_area_gap_is_readable_and_writable(master):
    """实测的不对称（事实 6）：位区空洞读回 False 且可写，寄存器区空洞回异常码 2。

    这条把差异钉住，免得将来当 bug 改回去——重叠校验对位区必须按"位"判就是因为这个。
    """
    client, _ = master
    gap = client.read_coils(4, count=4, device_id=11)
    assert not gap.isError() and gap.bits[:4] == [False] * 4
    assert client.write_coil(4, True, device_id=11).bits[0] is True
    assert client.read_coils(4, count=1, device_id=11).bits[0] is True
    # 同一台设备的寄存器区仍然是 2，两边口径不同这件事本身也要钉住
    assert client.read_holding_registers(30, count=1, device_id=11).exception_code == 2


def test_unknown_unit_id_answers_exception_4_and_server_survives(master):
    """不建 id=0 兜底设备：未知 unit id 走 devices[0] 回落 -> KeyError -> 异常码 4（事实 4）。"""
    client, _ = master
    miss = client.read_holding_registers(0, count=1, device_id=77)
    assert miss.isError() and miss.exception_code == 4
    alive = client.read_holding_registers(0, count=1, device_id=11)
    assert not alive.isError()


def test_empty_input_register_area_answers_exception_2_to_a_real_master(bare_master):
    """空 ir 区（占位条目地址 0 / DataType.INVALID）对真主站回异常码 2。

    这是 `tests/test_simdev.py::test_empty_register_area_reads_illegal_address_at_datastore_layer`
    的真主站版本：datastore 层那条走 `SimCore.async_getValues`，本条走完整 TCP 协议栈，
    两层都钉住才排得掉"帧解析对了但区选错了"这类只在链路上出现的问题。
    """
    client, _ = bare_master
    for count in (1, 2):
        miss = client.read_input_registers(0, count=count, device_id=11)
        assert miss.isError() and miss.exception_code == 2
    assert client.read_holding_registers(0, count=1, device_id=11).registers == [7]


def test_empty_bit_area_answers_exception_2_to_a_real_master(bare_master):
    """空 co/di 区对真主站读写一律回异常码 2 —— 与寄存器区同口径（裁决 1 方案 ②）。

    真主站版本对应 `test_simdev.py::test_empty_bit_area_reads_and_writes_illegal_address_at_datastore_layer`。
    占位地址没抬到 65520 之前，实测这里是 `16 个 False` + `write_coil(0, True)` 回 ACK：
    风机启动指令打到一台压根没有线圈的假从站上、平台收到"成功"，假设备对外撒谎比显示
    错值更糟。
    """
    client, _ = bare_master
    for rr in (client.read_coils(0, count=16, device_id=11),
               client.read_discrete_inputs(0, count=16, device_id=11),
               client.read_coils(0, count=1, device_id=11),
               client.write_coil(0, True, device_id=11),
               client.write_coils(0, [True] * 8, device_id=11)):
        assert rr.isError() and rr.exception_code == 2
    assert client.read_holding_registers(0, count=1, device_id=11).registers == [7]


def test_placeholder_bit_window_is_the_pinned_residual_cost(bare_master):
    """残余代价必须对外可见：占位让位 65520..65535 这一小段"已定义"。

    这是裁决 1 方案 ② 的代价（`simdev.placeholder_for` 的 docstring 写明了）。真设备的
    第一个线圈不会在那儿，而且该区一旦有真点位就不再补占位；这里钉住它，是为了让将来
    改占位地址的人必须先回答"这段窗口对外算什么"，而不是悄悄改掉对外行为。
    """
    client, backend = bare_master
    ok = client.read_coils(65520, count=16, device_id=11)
    assert not ok.isError() and ok.bits[:16] == [False] * 16
    assert client.write_coil(65520, True, device_id=11).bits[0] is True
    assert client.read_coils(65520, count=1, device_id=11).bits[0] is True
    # 块只有 2 个寄存器：占位放在高地址不会把 4095 个寄存器铺出来（事实 1）
    assert backend.read_bits(11, "co", 65520, 16) == [True] + [False] * 15


def test_bus_scan_does_not_flood_the_log(master, caplog):
    """总线扫描 1..247：异常码照回，栈不许刷进日志（事实 4 的 492 行就是这么来的）。

    两条断言配对使用才不会假绿：没有栈 + 确实抓到了日志记录。只判前者的话，"logger 整个
    被关掉/根本没接 handler"也能过，而那条用例就再也没在守任何东西。
    范围按完整地址空间 1..247 扫（现场用工具点一次"扫描从站"就是这个量级，只扫 1..50
    会把 51 号以后的 unit 留在测试盲区里）。
    """
    client, _ = master
    with caplog.at_level(logging.DEBUG, logger="pymodbus.logging"):
        for unit in range(1, 248):
            got = client.read_holding_registers(0, count=1, device_id=unit)
            if unit not in (11, 12):
                assert got.isError() and got.exception_code == 4
        texts = [str(r.getMessage()) for r in caplog.records]
    assert not any("Traceback" in t for t in texts)
    assert not any("Datastore unable to fulfill request" in t for t in texts)
    assert len(texts) > 0, "caplog 没抓到任何 pymodbus 记录，上面的断言是空的"
    # 服务照旧活着
    assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]


def test_internal_datastore_failure_is_not_swallowed(master, caplog, monkeypatch):
    """datastore 里换成**别的**异常时，日志必须还看得见——降噪只许挡已知的那一种。

    `requesthandler.py` 那句 `Log.error("Datastore unable to fulfill request: {}; {}")`
    外面是一个裸 `except Exception`：datastore 路径上任何异常都汇到这一句、回异常码 4。
    真出现内部 bug（M4 波形算出坏寄存器、点表映射越界）时，如果按消息前缀一刀切，主站
    收到 4、日志一条不剩，而"异常码 4"与"unit 不在线"在报文上完全同形，现场无从分辨
    （spec §13 列的第一风险）。这里拿真主站打一条 RuntimeError 进来钉住这个边界。
    """
    client, backend = master
    core = backend._server.context

    async def internal_bug(*args, **kwargs):
        raise RuntimeError("internal engine bug")

    monkeypatch.setattr(core, "async_getValues", internal_bug)
    with caplog.at_level(logging.DEBUG, logger="pymodbus.logging"):
        got = client.read_holding_registers(0, count=1, device_id=11)
    assert got.isError() and got.exception_code == 4
    texts = [str(r.getMessage()) for r in caplog.records]
    assert any("internal engine bug" in t for t in texts), texts
    assert any("Traceback" in t for t in texts), texts
    # 服务照旧活着：先撤掉注入的异常，否则读到的还是刚才那条 4
    monkeypatch.undo()
    assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]


def test_configure_library_logging_is_idempotent():
    """`configure_library_logging()` 每次 start() 都调一遍（GUI 停再起）：filter 不许堆叠。"""
    for _ in range(3):
        configure_library_logging()
    for name in ("pymodbus", "pymodbus.logging", "pymodbus_internal"):
        logger = logging.getLogger(name)
        assert logger.level == logging.ERROR
        assert sum(isinstance(f, _DropDatastoreNoise) for f in logger.filters) == 1, name


def test_multi_unit_routing_is_isolated(master):
    """同一监听端口下按 unit id 分流：12 号写 0 号寄存器不影响 11 号的 float32@CDAB。"""
    client, _ = master
    assert client.read_holding_registers(0, count=1, device_id=12).registers == [99]
    client.write_register(0, 1, device_id=12)
    assert client.read_holding_registers(0, count=1, device_id=12).registers == [1]
    # unit 11 的同名地址不受影响
    assert cdab_to_float(
        client.read_holding_registers(0, count=2, device_id=11).registers) == \
        pytest.approx(12.5)


def test_packet_sink_sees_both_directions(open_backend):
    port = free_port()
    seen: list[tuple] = []
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=lambda *args: seen.append(args)) as backend:
        client = connect(port)
        client.read_holding_registers(0, count=1, device_id=11)
        client.close()
    directions = [row[0] for row in seen]
    assert "rx" in directions and "tx" in directions
    rx = next(row for row in seen if row[0] == "rx")
    assert rx[2] == 11 and rx[3] == 3            # unit_id / function_code
    assert backend.port == port


def test_packet_sink_carries_the_contract_fields(open_backend):
    """sink 收到的六个位置是 Task 11/M3 报文窗的消费合同：方向、原字节、unit、功能码、摘要、时刻。"""
    port = free_port()
    seen: list[tuple] = []
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=lambda *args: seen.append(args)):
        client = connect(port)
        client.read_holding_registers(4, count=1, device_id=11)
        client.close()
    rx = next(row for row in seen if row[0] == "rx")
    tx = next(row for row in seen if row[0] == "tx")
    direction, raw, unit, function, summary, mono = rx
    assert direction == "rx"
    assert isinstance(raw, bytes) and raw[7] == 3
    assert (unit, function) == (11, 3)
    assert summary.startswith("unit 11 fc 3")
    assert tx[2] == 11 and tx[3] == 3 and tx[0] == "tx"
    assert mono >= 0.0


def test_packet_sink_sees_the_exception_response(open_backend):
    """异常响应在报文窗里要一眼可读：功能码脱掉 0x80、摘要带上 EXCEPTION 码。"""
    port = free_port()
    seen: list[tuple] = []
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=lambda *args: seen.append(args)):
        client = connect(port)
        assert client.write_registers(0, [1, 2], device_id=11).exception_code == 2
        client.close()
    tx = [row for row in seen if row[0] == "tx" and row[2] == 11]
    assert any("EXCEPTION 2" in row[4] for row in tx), tx


def test_repeated_connect_and_disconnect_is_clean(open_backend):
    """连上→读→关 反复 5 次：不泄漏连接、不死锁，open/close 回调一一配对。

    `close` 是 loop 线程在 `connection_lost` 里发的，比主线程的 `client.close()` 晚一步，
    所以要等到出现第 5 个为止（有上限），不能假设出了 with 就齐了。
    """
    port = free_port()
    events: list[str] = []
    with open_backend(tunnel_project(port).channels[0],
                      on_connection=events.append) as backend:
        for _ in range(5):
            client = connect(port)
            assert client.read_holding_registers(4, count=1, device_id=11).registers == [11]
            client.close()
        wait_for(lambda: events.count("close") == 5,
                 what="five close callbacks for five connections")
        # 反复连上/断开没把 datastore 或监听弄坏：引擎侧仍读得到初值
        assert backend.read_registers(11, "hr", 4, 1) == [11]
    assert events.count("open") == 5
    assert events.count("close") == 5


def test_engine_calls_after_stop_are_a_domain_error(open_backend):
    """停了之后再调读写：要一句带通道名的话，不要 `AttributeError: 'NoneType'`。

    GUI/命令行都会遇到"通道已停但引擎 tick 还有一发在路上"（M2 的关通道顺序问题）；
    `AttributeError` 看着像模拟器的 bug，而真实原因是"这个通道没在跑"。
    """
    port = free_port()
    with open_backend(tunnel_project(port).channels[0]) as backend:
        assert backend.read_registers(11, "hr", 4, 1) == [11]
    for call in (lambda: backend.read_registers(11, "hr", 4, 1),
                 lambda: backend.write_registers(11, "hr", 4, [1]),
                 lambda: backend.read_bits(11, "co", 0, 1),
                 lambda: backend.write_bits(11, "co", 0, True)):
        with pytest.raises(RuntimeError, match="tcp .*not started"):
            call()


def test_open_backend_fixture_adds_no_threads_and_frees_the_port(open_backend,
                                                                engine_runner):
    """起停一个 backend 不留引擎线程、端口能重新 bind。

    只点名 `simbench*` 线程：探针用的 `asyncio.to_thread` 工作线程本来就有生有灭，拿
    "全部线程数不变"去判会把一次正常启动误判成泄漏。共享 runner 自己那两条 loop/pump
    线程横跨整个会话，属于预期内，所以比的是"集合没多出新的"。
    """
    def engine_threads():
        return {t.name for t in threading.enumerate() if t.name.startswith("simbench")}

    before = engine_threads()
    port = free_port()
    with open_backend(tunnel_project(port).channels[0]):
        client = connect(port)
        client.read_holding_registers(4, count=1, device_id=11)
        client.close()
    assert engine_threads() == before, "起停一个 backend 多出了引擎线程"
    check_port_is_free("127.0.0.1", port)                           # 端口已释放


def test_start_refuses_a_port_that_is_already_listened():
    """端口已经有人在监听时，start() 必须在把端口交给 pymodbus 之前就拒绝。

    实测：pymodbus 3.15.0 在 Windows 上连开两个 `ModbusTcpServer` 绑同一 `(host, port)`
    会**两个都成功**（`_await_ready` 只看自己 transport 是否为 None，证明不了端口归属），
    netstat 能叠出 3 个 LISTENING；主站的连接被先起来那个吃掉，于是"改了点位重启模拟器"
    之后平台读到的还是旧值，而且两条日志都说 listening on 同一端口——没有任何线索指向
    上一个没退干净的实例。所以这里自己拿裸 `socket.bind` 探一次。

    措辞必须是"可能正被占用"：bind 与真正 listen 之间有窗口，探针只能证明"这端口现在
    不归我们"，不能证明占用者是谁。
    """
    with socket.socket() as hog:
        hog.bind(("127.0.0.1", 0))
        port = hog.getsockname()[1]
        hog.listen(1)
        backend = ModbusTcpBackend(tunnel_project(port).channels[0])

        async def scenario():
            await backend.start()

        with pytest.raises(PortInUseError, match="may already be in use") as excinfo:
            asyncio.run(scenario())
        message = str(excinfo.value)
        assert str(port) in message                       # 带端口号，现场一眼对上
        assert "WinError" in message or "errno" in message
        assert backend._server is None                    # 没留下半个后端


def test_await_ready_reports_a_late_bind_failure():
    """探针之后仍可能晚到失败（TOCTOU、保留端口 10013）：`_await_ready` 照旧抛真异常。

    `serve_forever()` 只有两种提前结束：bind 失败与被 shutdown。所以 task 一结束就说明
    从站没起来，这里把真实异常原样抛出去，不许"start 成功但外面连不上"（静默失败）。

    名字在 M5 Task 3 抽基类时从 `_await_ready` 改成 `_await_ready`（函数名保留旧称呼是
    因为它在文档里被引用过）：两条传输的"起来了"判据不同（TCP 等 listener、RTU 等串口
    transport），但**这一条纪律是同一条**——所以它是基类的钩子，断言一个字没动。
    """
    port = free_port()
    backend = ModbusTcpBackend(tunnel_project(port).channels[0])

    class NoListener:
        transport = None

    async def failing_serve_forever():
        raise RuntimeError("Could not start listen, please check address.")

    async def scenario():
        task = asyncio.create_task(failing_serve_forever())
        with pytest.raises(RuntimeError, match="Could not start listen"):
            await backend._await_ready(NoListener(), task)

    asyncio.run(scenario())


def test_stop_is_idempotent_and_start_does_not_rebind():
    """重复 start 不再占一次端口；重复 stop 安全返回（GUI 停再起、异常兜底都会走到）。"""
    port = free_port()
    backend = ModbusTcpBackend(tunnel_project(port).channels[0])

    async def scenario():
        await backend.start()
        await backend.start()          # 已经在跑：直接返回，不会再 bind 一次
        await backend.stop()
        await backend.stop()
        await backend.stop()

    asyncio.run(scenario())
    check_port_is_free("127.0.0.1", port)          # 重复 start/stop 没留下监听


def test_parse_packet_handles_short_and_exception_frames():
    """报文摘要的两个边角：短帧不炸、异常帧脱 0x80。"""
    unit, function, summary = parse_packet(b"\x00\x01\x00\x00")
    assert (unit, function) == (None, None)
    assert summary.startswith("short (4 bytes):")
    assert parse_packet(b"\x00\x03\x00\x00\x00\x03\x4d\x83\x04") == \
        (77, 3, "unit 77 fc 3 EXCEPTION 4")
    assert parse_packet(b"\x00\x01\x00\x00\x00\x06\x0b\x03\x00\x00\x00\x02") == \
        (11, 3, "unit 11 fc 3 4B 00 00 00 02")


def test_the_port_probe_must_not_block_the_engine_loop(monkeypatch):
    """端口探针是阻塞式 socket 调用，不许在引擎 loop 线程上原地跑。

    卡住 loop 的后果不是"这一次启动慢一点"：GUI 的每次改值、每条报文投递都排在这条
    loop 上，探针堵多久界面就冻多久；两个通道一起启动时后一个还得等前一个探完。
    判据：探针故意做慢（0.3s），这段时间同一 loop 上的另一个协程必须照常推进。
    """
    import simbench.backend.modbus_tcp as mbt
    from simbench.engine.runner import EngineRunner

    inside_probe = threading.Event()

    def slow_probe(host, port, channel_name=None):
        inside_probe.set()
        time.sleep(0.3)                      # 模拟 connect 探针的阻塞窗口

    monkeypatch.setattr(mbt, "check_port_is_free", slow_probe)
    runner = EngineRunner(name="simbench-probe-loop")
    runner.start()
    ticks: list[int] = []

    async def ticker():
        for _ in range(30):
            await asyncio.sleep(0.02)
            ticks.append(1)

    async def start_and_stop(channel):
        backend = ModbusTcpBackend(channel)
        await backend.start()
        await backend.stop()

    port = free_port()
    channel = tunnel_project(port).channels[0]
    tick_future = runner.submit(ticker)
    start_future = runner.submit(lambda: start_and_stop(channel))
    try:
        assert inside_probe.wait(5.0), "start() 没走到端口探针"
        seen_during_probe = 0
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            seen_during_probe = max(seen_during_probe, len(ticks))
            time.sleep(0.01)
        assert seen_during_probe >= 3, (
            f"探针期间 loop 只推进了 {seen_during_probe} 次：探针把引擎线程整段占住了")
        start_future.result(10)
        tick_future.result(10)
    finally:
        runner.stop()


def test_the_bind_probe_lets_a_loopback_listener_through_and_the_connect_probe_refuses(

        ):

    """A–I 表 H 行挂的那笔账：bind 探针看不见"对方绑 127.0.0.1、我们绑 0.0.0.0"。



    先用 `_bind_probe` 单独证明**洞确实存在**（它放行），再证明 `check_port_is_free`

    拒得下来。少了前一半，这条用例就只是在测"探针会不会报错"，看不出它补的是哪个洞，

    以后有人把 connect 探针当冗余删掉时也没人拦得住。

    """

    with socket.socket() as hog:

        hog.bind(("127.0.0.1", 0))

        port = hog.getsockname()[1]

        hog.listen(128)               # backlog 给足：满队列会让握手包被丢掉，探针看不见对方

        _bind_probe("0.0.0.0", port, None)          # 洞：裸 bind 在这里放行

        with pytest.raises(PortInUseError) as excinfo:

            check_port_is_free("0.0.0.0", port, "tcp")

        message = str(excinfo.value)

        assert str(port) in message

        assert "connect probe" in message            # 说清是哪一级探针拦的

        assert "previous simulator instance" in message







def test_the_connect_probe_also_catches_a_wildcard_owner_when_we_bind_loopback(

        open_backend):

    """反方向的同一个洞：别人绑 `0.0.0.0:P` 在服务，我们绑 `127.0.0.1:P` 也起得来。



    用的是**真 backend**而不是裸 socket——现场就是这个形状（上一个模拟器实例）。

    """

    port = free_port()

    wildcard = tunnel_project(port)

    wildcard.channels[0].host = "0.0.0.0"

    with open_backend(wildcard.channels[0]):

        _bind_probe("127.0.0.1", port, None)         # 洞：仍然放行

        with pytest.raises(PortInUseError) as excinfo:

            check_port_is_free("127.0.0.1", port, "tcp")

        assert "connect probe" in str(excinfo.value)

        assert str(port) in str(excinfo.value)







def test_an_accepted_but_hung_up_connection_is_not_counted_as_serving():
    """连上就被挂断：不许判成"有人在服务"。

    真监听者会把连接留住，而"接了立刻关"的形态（旧实例正在退出、内核还把 SYN 交给它）
    必须放行——否则"杀掉旧实例立刻起新的"会被误拒，那比原来的双监听更让人怀疑模拟器。
    accept 只能发生在有人 connect 之后，所以这里让后台线程持续 accept+close，
    再断言它确实接过探针那一下（否则测的就不是"接了又挂断"）。
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(8)
    taken = []
    stop = threading.Event()

    def accept_and_hang_up():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            taken.append(1)
            conn.close()                     # 挂断：环回上探针的 connect 会"成功"

    thread = threading.Thread(target=accept_and_hang_up, daemon=True)
    thread.start()
    try:
        assert _is_being_served("127.0.0.1", port) is False
        assert taken, "探针的 connect 根本没被 accept，测的不是\"接了又挂断\""
    finally:
        stop.set()
        listener.close()
        thread.join(2)



def test_a_tick_writes_the_waveform_value_into_the_registers(open_backend):
    """一步 tick 之后，**第三方主站**读到的就是算出来的值。

    选 `CO浓度` 是故意的：它是 `readonly=true`。波形必须能写进只读点（走 raw_writer
    直接改列表），否则"只读的模拟量"这一整类点位（隧道里 CO/VI/照度几乎全是只读）
    就永远冻在初值上。主站写同一个地址仍回异常码 2，那是另一条用例（M1 已钉）。
    时钟由 `_tick_once(now=...)` 注入，不靠 `sleep`：这条要证的是"值算对写对"，
    "定时器会自己响"由下一条钉。
    """
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[0]           # CO浓度 hr0 float32 CDAB
    point.waveform, point.wave_param = "const", {"value": "44.5"}
    with open_backend(project.channels[0]) as backend:
        backend._tick_once(now=0.0)
        client = connect(project.channels[0].port)
        try:
            got = client.read_holding_registers(0, count=2, device_id=11).registers
            assert cdab_to_float(got) == pytest.approx(44.5), got
        finally:
            client.close()


def test_the_tick_task_dies_with_the_backend(engine_runner):
    """stop 之后不许留一个还在写寄存器的任务。

    "通道已经停了、值还在自己动"是这个工具最难查的一类假象：主站读到的是已经不该
    再存在的模拟量。这里断言任务已取消，而不是断言"过一会儿值不再变"——后者要 sleep
    且慢测试会假绿。
    """
    project = tunnel_project(free_port())
    project.channels[0].devices[0].points[0].waveform = "sine"
    project.channels[0].devices[0].points[0].wave_param = {
        "base": "20", "amp": "15", "period": "60"}
    backend = ModbusTcpBackend(project.channels[0], tick_ms=20)
    engine_runner.call(lambda: backend.start(), 10, "test open")
    try:
        assert backend._tick_task is not None and not backend._tick_task.done()
    finally:
        engine_runner.call(lambda: backend.stop(), 10, "test close")
    assert backend._tick_task is None, "stop 没把 tick 任务收掉"
    # 停了之后打一步 tick：要的是"通道没在跑"那句带通道名的话，不是 `'NoneType' object ...`
    with pytest.raises(RuntimeError, match="tcp .*not started"):
        backend._tick_once(now=time.monotonic())


def test_the_tick_cancel_runs_before_the_shutdown_not_after(engine_runner,
                                                            monkeypatch):
    """钉住 `stop()` 里"先取消 tick 任务"这条**顺序**本身（承诺在
    `simbench/backend/modbus_tcp.py` 的 `stop()` 开头那段注释里，不在行号里）。

    为什么"任务 done 了"顶不掉这条：早取消与晚取消的任务都是 `done()==True`，把
    cancel 挪到 `server.shutdown()` 之后 837 条照样全绿。能分辨的只有**结束形态**——
    先取消：任务在 `sleep` 里收到 `CancelledError`，`cancelled()` 为真且永远不带异常；
    后取消：`_writer` 已经置 None 而 shutdown 的 await 让 loop 转出去，计时器一响，
    `_tick_once` 就踩进"通道没在跑"的 `RuntimeError` 把任务带崩——正是注释里承诺防住的
    "停通道时崩一次 / 曲线静默停"。这里让 shutdown 主动让出 5 个 tick 周期（50ms vs
    tick 10ms），顺序错了就**必然**踩响，不赌真实时序。
    """
    port = free_port()
    project = tunnel_project(port)
    project.channels[0].devices[0].points[0].waveform = "sine"   # CO浓度，tick 一直在写
    project.channels[0].devices[0].points[0].wave_param = {
        "base": "20", "amp": "15", "period": "60"}
    backend = ModbusTcpBackend(project.channels[0], tick_ms=10)
    engine_runner.call(lambda: backend.start(), 10, "test open")
    task = backend._tick_task
    assert task is not None and not task.done(), "tick 任务没起来，这条测不到顺序"

    server = backend._server
    real_shutdown = server.shutdown

    async def yielding_shutdown():
        await asyncio.sleep(0.05)     # 晚取消的变异只能在这个窗口里现形
        await real_shutdown()

    monkeypatch.setattr(server, "shutdown", yielding_shutdown)
    try:
        engine_runner.call(lambda: backend.stop(), 10, "test close")
        engine_runner.call(lambda: asyncio.sleep(0.05), 10, "settle")
        assert task.done(), "stop 之后 tick 任务仍没结束"
        assert task.cancelled(), (
            f"tick 任务不是被取消收掉的（exception={task.exception()!r}）：cancel 排到了 "
            f"_writer 置 None / shutdown 之后，停通道时任务踩了 'channel is not started'")
    finally:
        engine_runner.call(lambda: backend.stop(), 10, "test close again")


def test_a_backend_that_was_not_handed_a_tick_ms_has_no_tick_task(open_backend):
    """`tick_ms=0` 是默认，也就是**不起任务**：自己把 backend 建起来的一方（用例、以及没被
    交代工程的调用方）形状因此一个字都没变。把工程的 `tick_ms` 交下来的有两条路——
    `Session.start_channel` 与无头 CLI 的 `__main__._serve`（决策 8）——这里钉住"没交就没有"，
    否则默认起任务会把现有全部用例的形状换掉。
    """
    port = free_port()
    with open_backend(tunnel_project(port).channels[0]) as backend:
        assert backend.tick_ms == 0
        assert backend._tick_task is None, "tick_ms=0 也起了任务：M1 的冒烟形状变了"


async def _one_tick(backend, now: float) -> int:
    """把一步 tick 投回 loop 线程：它写寄存器、读归属，都是引擎线程的活。"""
    return backend._tick_once(now=now)


def test_a_point_the_master_took_over_gets_not_a_single_word_from_the_tick(
        engine_runner, open_backend):
    """spec §7 的另一半：接管之后 tick 一个字都不写，否则"下发没生效"又回来了；
    同通道**其余**点必须照写，不能因为一个点被接管就连坐。

    接管状态是真主站的 FC06 打出来的（rx 钩子在 loop 线程上标），不是测试手工塞的键——
    塞进去的键证明不了"报文识别 + 落到点上"这一整条路。
    """
    port = free_port()
    project = tunnel_project(port)
    points = project.channels[0].devices[0].points
    for pt, value in ((points[0], "44.5"), (points[2], "999")):    # CO浓度 hr0 / 状态字 hr4
        pt.waveform, pt.wave_param = "const", {"value": value}
    with open_backend(project.channels[0]) as backend:
        client = connect(port)
        try:
            assert client.write_register(4, 88, device_id=11).registers == [88]
            written = engine_runner.call(lambda: _one_tick(backend, 0.0), 10,
                                        "one tick")
            assert written == 1, f"只该写掉 CO浓度 那一个点，写了 {written} 个"
            got = client.read_holding_registers(4, count=1, device_id=11).registers
            assert got == [88], got
            assert cdab_to_float(client.read_holding_registers(
                0, count=2, device_id=11).registers) == pytest.approx(44.5)
        finally:
            client.close()


def test_one_bad_point_does_not_stop_the_rest_of_the_tick(open_backend):
    """三种坏波形各自只跳过**它自己那个点**：Task 1 实测 `waveform_value` 对未知名字抛
    `KeyError`、`period=0` 抛 `ZeroDivisionError`，`_num`/`encode` 抛 `ValueError`。
    计划原文只兜 `ValueError`，那会让第一个坏点以 `KeyError` 把整轮 tick 打断，同通道
    其余点位一起冻住（决策 2）。

    `铭牌`（string）那一行钉的是 `encode` 那条：波形只产数值，字符串点位编不出寄存器，
    这种点每轮都该被跳过而不是把整轮带走。`启动`（co）那条钉住位区走 `write_bits`：寄存器
    入口对位区现在直接抛 `ValueError`（`co is a bit area; use write_bits`），两边的"地址 0"
    含义不同，混用的实测后果是把 0x0002 点成**线圈 1**。
    """
    port = free_port()
    project = tunnel_project(port)
    points = project.channels[0].devices[0].points
    points[0].waveform, points[0].wave_param = "const", {"value": "44.5"}   # 好：hr0
    points[1].waveform, points[1].wave_param = "spiral", {}   # KeyError
    points[2].waveform, points[2].wave_param = "sine", {
        "base": "1", "amp": "1", "period": "0"}   # ZeroDivisionError
    points[3].waveform, points[3].wave_param = "const", {"value": "abc"}   # _num
    points[4].waveform, points[4].wave_param = "const", {"value": "7"}   # encode
    points[5].waveform, points[5].wave_param = "const", {"value": "1"}   # 好：co0
    with open_backend(project.channels[0]) as backend:
        assert backend._tick_once(now=1.0) == 2, "两个好点之外的都该被跳过"
        client = connect(port)
        try:
            assert cdab_to_float(client.read_holding_registers(
                0, count=2, device_id=11).registers) == pytest.approx(44.5)
            assert client.read_coils(0, count=1, device_id=11).bits[0] is True
            assert client.read_coils(1, count=1, device_id=11).bits[0] is False, \
                "线圈 1 亮了：位区走了寄存器写入口"
            # 四个坏点一个字节都没写进去：还是各自的点表初值
            for address, initial in [(2, 0x4050), (4, 11), (5, 0x0102), (9, 0x4641)]:
                got = client.read_holding_registers(address, count=1, device_id=11)
                assert got.registers == [initial], (address, got.registers)
        finally:
            client.close()


def test_a_bad_waveform_does_not_kill_the_tick_task(engine_runner):
    """任务里一个未捕获的异常会把整条曲线静默吃掉，而通道还在跑、界面还在读——
    "波形停了但通道还在跑"就是这个形状（决策 2 要防的那件事）。

    坏点排在**第一个**：没兜住的话任务在真正写任何值之前就死掉。判据不是"没崩"而是
    "后面的点连着写进了两个不同的值"，加上任务仍然活着——只看值会漏掉"任务已死但值恰好
    被谁写过一次"，只看 done() 又证明不了它真的还在往外写。
    """
    port = free_port()
    project = tunnel_project(port)
    points = project.channels[0].devices[0].points
    points[0].waveform, points[0].wave_param = "spiral", {}        # KeyError，排在好点之前
    points[2].waveform, points[2].wave_param = "ramp", {
        "from": "0", "to": "100", "speed": "500"}                 # 状态字 hr4，每 tick 换值
    backend = ModbusTcpBackend(project.channels[0], tick_ms=20)
    engine_runner.call(lambda: backend.start(), 10, "test open")
    seen: set[int] = set()
    try:
        deadline = time.monotonic() + 5.0
        while len(seen) < 2 and time.monotonic() < deadline:
            # 主线程直读回显，与 `_read_back` 同一个口径
            regs = backend.read_registers(11, "hr", 4, 1)
            if regs:
                seen.add(regs[0])
            time.sleep(0.01)
        assert len(seen) >= 2, f"tick 任务没在持续写，只读到 {sorted(seen)}"
        assert backend._tick_task is not None and not backend._tick_task.done()
    finally:
        engine_runner.call(lambda: backend.stop(), 10, "test close")


def test_the_real_tick_measures_time_since_the_channel_started(engine_runner):
    """spec §5 的 `t`：**本通道启动以来的单调秒**，不是裸的时钟读数——此前零覆盖。

    既有 tick 用例全用 const / 快 ramp，对错参照系不敏感：`_tick_started` 停在 0.0 时
    `t` 是开机以来的秒数、loop 里换 `time.time()` 时 `t` 是纪元秒，两者照样产出**格式
    合法**的值，837 条全绿。所以故意选单程 100000s、速度 1 数值/秒的 ramp：起通道后
    头几步真 tick 写出的值只可能是个位数，而错参照系给的是大数（uint16 钳位也救不了
    它回到个位——除非相位正好撞上，概率约十万分之三）；初值 11 证明值确实来自 tick。
    停再起钉"重新起通道曲线从 0 相位开始"：第二次启动必须重新记 `_tick_started`，
    沿用旧值/零值的话第二次读到的还是开机秒数。判值不判私有属性：这是"参照系错了"
    唯一能被**观察**到的形状，私有断言留给下一条钉时钟源本身。
    """
    port = free_port()
    project = tunnel_project(port)
    point = project.channels[0].devices[0].points[2]         # 状态字 hr4 uint16，初值 11
    point.waveform, point.wave_param = "ramp", {
        "from": "0", "to": "100000", "speed": "1"}
    backend = ModbusTcpBackend(project.channels[0], tick_ms=10)

    def first_written_value(what: str) -> int:
        """等第一步真 tick 落进寄存器（值离开初值 11），返回写进去的那格。"""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            regs = backend.read_registers(11, "hr", 4, 1)
            if regs and regs[0] != 11:
                return regs[0]
            time.sleep(0.01)
        raise AssertionError(f"{what}: tick 任务没往寄存器写过")

    engine_runner.call(lambda: backend.start(), 10, "test open")
    try:
        first = first_written_value("first start")
        engine_runner.call(lambda: backend.stop(), 10, "test close")
        engine_runner.call(lambda: backend.start(), 10, "test reopen")
        second = first_written_value("restart")
    finally:
        engine_runner.call(lambda: backend.stop(), 10, "test close")
    assert first <= 3, (
        f"起通道后 t 应当只有零点几秒，真 tick 却写出 {first}：t 的参照系不是本通道启动"
        "时刻（`_tick_started` 停在 0.0，或 loop 喂的不是单调钟）")
    assert second <= 3, (
        f"重新起通道后曲线相位没归零（写出 {second}）：start() 没有重记 _tick_started")


def test_the_tick_loop_feeds_the_tick_a_monotonic_reading_not_the_wall_clock(
        engine_runner, monkeypatch):
    """`_tick_loop` 递给 `_tick_once` 的必须是 `time.monotonic()`：把时钟**参照**本身钉死。

    上一条按值只能**概率性**抓错参照——错钟给出的也是合法值，撞上相位就绿。这里记录
    真任务实际传入的 `now` 参数，与本机两个钟直接比：monotonic 是开机秒（本机量级千），
    纪元秒是十亿量级，两者相差六个数量级，不存在撞相位——判据是传给 tick 的实参，
    不是私有属性：时钟源错了照样全绿的形状只有这一条照得出。任务仍由真 `_tick_loop`
    驱动，recorder 只旁听参数并把活转回原函数。
    """
    port = free_port()
    project = tunnel_project(port)
    point = project.channels[0].devices[0].points[2]
    point.waveform, point.wave_param = "const", {"value": "7"}
    backend = ModbusTcpBackend(project.channels[0], tick_ms=10)
    recorded: list[float] = []
    real_tick = backend._tick_once

    def probe(now: float) -> int:
        recorded.append(now)
        return real_tick(now=now)

    monkeypatch.setattr(backend, "_tick_once", probe)
    engine_runner.call(lambda: backend.start(), 10, "test open")
    try:
        deadline = time.monotonic() + 5.0
        while not recorded and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        engine_runner.call(lambda: backend.stop(), 10, "test close")
    assert recorded, "tick 任务一次都没调到 _tick_once，上面测了个空"
    drift = max(abs(now - time.monotonic()) for now in recorded)
    assert drift < 60.0, (
        f"_tick_loop 的时钟参照不再是单调钟：记录的 now 与当前单调秒差 {drift:.1f}s"
        f"（纪元秒与单调秒差约 {time.time() - time.monotonic():.0f}s）")


def test_a_point_that_is_not_in_the_datastore_is_skipped_without_breaking_the_round(
        open_backend):
    """`write_registers` 回 `False`（地址不在块内）时静默跳下一个点：一个点写不到不许把
    整轮 tick 打断，那会让同通道其余点位一起冻住（要点 3）。

    这里往摊平的表**头部**塞一个假点（hr 30 是点表的空洞，`build_sim_devices` 早就按真
    点表建好了块，所以它是真写不进去的地址）：排在前面才证明"跳过它继续往下写"，
    顺带钉住返回值只数真写掉的点。
    """
    port = free_port()
    project = tunnel_project(port)
    point = project.channels[0].devices[0].points[2]               # 状态字 hr4
    point.waveform, point.wave_param = "const", {"value": "123"}
    with open_backend(project.channels[0]) as backend:
        backend._wave_points.insert(0, (11, Point("空洞", "hr", 30, "uint16",
                                                  waveform="const",
                                                  wave_param={"value": "7"})))
        assert backend._tick_once(now=0.0) == 1, "写不进去的那个点也被算成了「写掉」"
        client = connect(port)
        try:
            got = client.read_holding_registers(4, count=1, device_id=11).registers
            assert got == [123], got
        finally:
            client.close()


# ---- 运行期那一条日志：带点位名（M4 计划的报错口径）+ 同点同类 60s 一条（spec §8）----

def test_a_clamped_waveform_logs_one_line_naming_the_point(open_backend):
    """`base=20;amp=1e6` 压在 float32 上合法，但 uint16 点这么配就天天飘出边界。

    两条都钉：值必须落在边界上（不许写个 70000 掩码成 4464 出去），且日志里点名
    是哪个点钳的——只写"clamped"的操作员等于没看日志。
    """
    seen: list[tuple[str, str]] = []
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[2]        # 状态字 hr4 uint16
    point.waveform, point.wave_param = "const", {"value": "70000"}
    with open_backend(project.channels[0], tick_ms=0,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        backend._tick_once(now=0.0)
        client = connect(project.channels[0].port)
        try:
            assert client.read_holding_registers(
                4, count=1, device_id=11).registers == [65535]
        finally:
            client.close()
    assert seen and seen[0][0] == "warning" and "状态字" in seen[0][1], seen


def test_a_clamped_point_gets_one_line_per_window_not_one_per_tick(open_backend):
    """tick 是 100ms 一拍：不去重时两个飘出边界的点两分钟能写 2400 行（spec §8）。

    时间照 `_tick_once(now=...)` 注入，不为这条 sleep 一分钟。三个方向都要钉：
    窗口内两点各一条（不是每 tick 一条）、两个点不互相压制、过了 60s 各再来一条。
    """
    seen: list[tuple[str, str]] = []
    project = tunnel_project(free_port())
    points = project.channels[0].devices[0].points
    points[2].waveform, points[2].wave_param = "const", {"value": "70000"}   # 状态字 uint16
    points[3].waveform, points[3].wave_param = "const", {"value": "-1"}      # 电量 uint64
    with open_backend(project.channels[0], tick_ms=0,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        for now in (0.0, 0.1, 30.0):
            assert backend._tick_once(now=now) == 2, "钳到边界的两个点都该照写"
        assert len(seen) == 2, seen
        assert {text.split(":")[0] for _level, text in seen} == {"状态字", "累计电量"}, seen
        assert backend._tick_once(now=61.0) == 2
        assert len(seen) == 4, seen        # 窗口过了：每个点再来一条，不是一串


def test_restarting_the_channel_reports_the_same_clamped_point_again(
        engine_runner, open_backend):
    """重新起通道是一次**新的运行**：曲线从 0 相位重来（Task 3 钉的 `_tick_started`），
    去重表也得跟着归零，否则操作员改完点表重启之后那一分钟是静音的，他没法确认自己
    修的那个点到底还在不在飘——而日志正是他唯一的证据。
    """
    seen: list[tuple[str, str]] = []
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[2]
    point.waveform, point.wave_param = "const", {"value": "70000"}
    with open_backend(project.channels[0], tick_ms=0,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        assert backend._tick_once(now=0.0) == 1
        assert len(seen) == 1, seen
        engine_runner.call(lambda: backend.stop(), 10, "test restart")
        engine_runner.call(lambda: backend.start(), 10, "test reopen")
        assert backend._tick_once(now=0.5) == 1     # 上一轮的时间窗还没到期
        assert len(seen) == 2, seen


def test_a_point_that_stopped_computing_is_named_instead_of_falling_silent(open_backend):
    """Task 3 把坏点的三种异常咽进无声的 `continue`：通道照旧应答、那个点冻在初值上、
    日志一句没有。spec §8 要的是"这一类运行期故障得留下一条日志、同点同类 60s 不重复刷屏"；
    **那句日志要带点位名**是 M4 计划 Global Constraints 的报错口径，不是 §8 的原文。

    四类坏点各配一个（Task 1 实测的形状：未知名 `KeyError`、`period=0`
    `ZeroDivisionError`、参数不是数 `ValueError`、字符串点位的编码 `ValueError`），外加
    一个好点。三条各自钉一个容易被改坏的地方：
    ① `written` 只数真写掉的好点（把失败也算进去时这一句会先红）——"这个点这一轮没写
       出去"与"写掉了"是两件事，Task 4 把它们分成两种处置（钳位的照写边界值、算不出的
       冻在旧值上），§8 只承诺两边各有一条日志；
    ② "算值炸"与"编码炸"是两个 kind、两句不同的话——操作员要改的东西不一样
       （曲线名/参数 vs 点表的类型），共用一个身份时后者会被前者吃掉；
    ③ 消息带异常类名，否则"为什么这个点不刷新"只剩操作员自己猜。
    """
    seen: list[tuple[str, str]] = []
    project = tunnel_project(free_port())
    points = project.channels[0].devices[0].points
    points[0].waveform, points[0].wave_param = "const", {"value": "44.5"}   # 好：hr0
    points[1].waveform, points[1].wave_param = "spiral", {}         # KeyError
    points[2].waveform, points[2].wave_param = "sine", {
        "base": "1", "amp": "1", "period": "0"}                     # ZeroDivisionError
    points[3].waveform, points[3].wave_param = "const", {"value": "abc"}   # 参数不是数
    points[4].waveform, points[4].wave_param = "const", {"value": "7"}     # 编码不了
    with open_backend(project.channels[0], tick_ms=0,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        assert backend._tick_once(now=0.0) == 1, "四个坏点不许混进写掉的点数"
        assert backend._tick_once(now=0.5) == 1
    assert len(seen) == 4, seen                      # 每点一条，不是每 tick 一条
    assert all(level == "error" for level, _ in seen), seen
    for name in ("VI风速", "状态字", "累计电量", "铭牌"):
        assert any(name in text for _, text in seen), (name, seen)
    broken = next(text for _, text in seen if "状态字" in text)
    assert "compute" in broken and "ZeroDivisionError" in broken, broken
    encoded = next(text for _, text in seen if "铭牌" in text)
    assert "encode" in encoded and "ValueError" in encoded, encoded


def test_the_same_point_reports_both_failure_stages(open_backend):
    """上一用例的四个坏点是四个不同的地址，同一个点从来没走到过第二个 stage，所以它那句
    ②（"compute 与 encode 是两个身份"）其实没被任何断言覆盖——两个身份塌成一个时，全仓库
    没有一条用例会红。这里补上那个缺口：同一个点先以 `compute` 落进表里、再以 `encode`
    落进表里。

    现场形状是操作员照着第一句日志把波形名改对（`spiral` → `const`），值却照样编不出去
    （string 点位挂着数值曲线）：地址一个字没动，炸的那一步换了。塌成一个身份时第二句会被
    第一句压住 60s，而他刚改完东西正等这句回执（spec §8 的"不重复刷屏"说的是同点**同类**，
    不是同点）。后半段反向钉：两类各自再犯一次时照旧不许刷屏——分成两个身份不能变成"这个
    点每类都爱说几遍说几遍"。
    """
    seen: list[tuple[str, str]] = []
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[4]      # 铭牌 hr9 string
    point.waveform, point.wave_param = "spiral", {}      # 算值就炸 → compute
    with open_backend(project.channels[0], tick_ms=0,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        assert backend._tick_once(now=0.0) == 0, "炸掉的点不许算作写掉"
        assert len(seen) == 1 and "compute" in seen[0][1], seen
        # 改完波形名，同一个点换在编码那一步炸：地址没动，stage 动了
        point.waveform, point.wave_param = "const", {"value": "7"}
        assert backend._tick_once(now=0.5) == 0
        assert len(seen) == 2, seen                       # 身份塌成一个时这里只剩 1 条
        assert seen[1][0] == "error" and "铭牌" in seen[1][1], seen
        assert "encode" in seen[1][1] and "ValueError" in seen[1][1], seen
        # 各自再犯一次（都还在同一个 60s 窗口里）：同类照旧压着，也不许顺着把对方挤掉
        point.waveform, point.wave_param = "spiral", {}
        assert backend._tick_once(now=0.6) == 0
        point.waveform, point.wave_param = "const", {"value": "7"}
        assert backend._tick_once(now=0.7) == 0
        assert len(seen) == 2, seen                       # 每类一条，不是合计一条


def test_a_backend_nobody_listens_to_keeps_no_dedupe_book(open_backend):
    """`log_sink=None` 是 M1/M2 绝大多数用例的形状（无头 CLI 自己接 `_runtime_log`）：
    这里 `_emit` 必须原样不做事。

    留着"照样拼消息、照样往去重表记时间戳、最后把 `None(...)` 的 TypeError 吞掉"那条路，
    等于给每一拍 tick 记一份没人看的账，而表里的时间戳还会替一个不存在的听众把后面同类的
    报错压住 60s。断言的是表里**一个条目都没有**——不看这份状态就没别的方式证明这一支
    真的短路了（`_tick_task`/`_wave_points`/`_writer` 是同一文件里既有的私有断言口径）。
    """
    port = free_port()
    project = tunnel_project(port)
    point = project.channels[0].devices[0].points[2]
    point.waveform, point.wave_param = "const", {"value": "70000"}    # 每一拍都钳位
    with open_backend(project.channels[0], tick_ms=0) as backend:
        assert backend._tick_once(now=0.0) == 1
        assert backend._dedupe._last == {}, backend._dedupe._last


def test_a_failing_log_sink_costs_a_line_not_the_whole_waveform(open_backend):
    """日志那条是**症状**，曲线是病体：sink 自己炸了（订阅者写歪、界面已经没了）时代价
    只能到"这一条没记上"为止。放它把异常带出去，代价就从"少一条日志"升级成"停曲线"
    （spec §8 只承诺运行期有一条日志，没承诺拿波形去换）——而且比一个坏点
    更糟：整轮中断，同通道其余点位一起冻住。

    坏点排在**第一个**（`1e40` 压在 float32 上必然钳位），后面还排着一个好点：
    只断言"没炸"是不够的，要断言这一轮真的把后面的点写完了。
    """
    def sink(level, text):
        raise RuntimeError("log window is gone")

    port = free_port()
    project = tunnel_project(port)
    points = project.channels[0].devices[0].points
    points[0].waveform, points[0].wave_param = "const", {"value": "1e40"}   # 钳位，触发 sink
    points[2].waveform, points[2].wave_param = "const", {"value": "123"}    # 排在后面的好点
    with open_backend(project.channels[0], tick_ms=0, log_sink=sink) as backend:
        assert backend._tick_once(now=0.0) == 2, "sink 炸了就不许少写一个点"
        client = connect(port)
        try:
            assert client.read_holding_registers(
                4, count=1, device_id=11).registers == [123]
        finally:
            client.close()


def test_an_unexpected_tick_error_is_reported_and_the_loop_keeps_running(engine_runner,
                                                                        open_backend,
                                                                        monkeypatch):
    """三种异常**之外**的东西逃逸出 `_tick_once` 时，任务一死曲线就静默停了，而通道照旧
    应答、界面照旧刷新——`_tick_loop` 那道兜底防的就是这个（spec §8：运行期的故障得留下
    一条日志，而不是留下一条不动的曲线）。

    这里让 writer 头一次写就抛 `RuntimeError`（不在按点兜的三种里）。"下一轮还在跑"要数得
    出来才算证：`calls` 记的是 writer 被调的次数（连炸掉的那些轮也算），先等日志出现、再数到
    它还在长，最后要求寄存器里真的写上了曲线值。只看 `task.done()` 证明不了它还在往外写，
    而只按值断言会被"patch 落地之前那一轮已经写成功过一次"糊过去。结尾那一段是去重仍然管
    这一类：连着炸十几轮也只留一条，否则兜底本身就成了新的刷屏源。
    """
    port = free_port()
    project = tunnel_project(port)
    point = project.channels[0].devices[0].points[2]            # 状态字 hr4 uint16
    point.waveform, point.wave_param = "const", {"value": "123"}
    seen: list[tuple[str, str]] = []
    calls: list[int] = []
    armed = {"on": True}
    with open_backend(project.channels[0], tick_ms=20,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        writer = backend._writer
        real_write = writer.write_registers

        def exploding(unit_id, area, address, values):
            calls.append(1)
            if armed["on"]:
                raise RuntimeError("datastore block vanished")
            return real_write(unit_id, area, address, values)

        monkeypatch.setattr(writer, "write_registers", exploding)
        wait_for(lambda: seen, what="一句带通道名的整轮报错")
        assert seen[0][0] == "error" and "tcp" in seen[0][1] \
            and "RuntimeError" in seen[0][1], seen
        armed["on"] = False
        before = len(calls)
        wait_for(lambda: len(calls) > before + 3, what="炸过一轮之后还有下一轮")
        wait_for(lambda: (backend.read_registers(11, "hr", 4, 1) or [None])[0] == 123,
                 what="后面的轮次真的把曲线写进了寄存器")
        assert backend._tick_task is not None and not backend._tick_task.done()
        armed["on"] = True
        before = len(calls)
        time.sleep(0.2)                        # 十来个 tick 周期，每一轮都炸
        assert len(calls) > before, "整轮报错之后 loop 不再进下一轮了"
        assert len(seen) == 1, seen             # 同一种整轮故障 60s 只留一条


def test_a_started_channel_routes_waveform_warnings_into_the_session_log(engine_runner):
    """`Session.start_channel` 必须把 `log_sink` 接到 `publish_log`：接不上时 backend 那套
    去重日志只活在测试里，界面一条都收不到（无头 CLI 有自己那一份 `_runtime_log`，接的是
    同一个 `_emit`），spec §8 承诺的"越界/算不出来要有那一条日志"就还是空的。

    走的是生产那一条完整路：`Session` 起通道 → 工程里的 `tick_ms` 交下去 → 真 tick 在引擎
    线程上算出越界值 → 同一线程把 `LogLine` 发进事件流（三个 sink 都在引擎 loop 线程上被调，
    都不许碰 widget，界面那一路是 Qt bridge，不在本条用例的范围）。断言只认事件流里的东西，
    不去读 backend 的私有表。
    """
    port = free_port()
    project = tunnel_project(port)
    project.tick_ms = 50
    point = project.channels[0].devices[0].points[2]          # 状态字 hr4 uint16
    point.waveform, point.wave_param = "const", {"value": "70000"}
    lines: list[LogLine] = []

    def on_event(event: object) -> None:
        if isinstance(event, LogLine):
            lines.append(event)

    session = Session(runner=engine_runner)
    session.subscribe(on_event)
    session.load(project)
    try:
        session.start_channel("tcp")
        wait_for(lambda: any(line.level == "warning" and "状态字" in line.text
                             for line in lines),
                 timeout=5.0, what="钳位那句经 Session 的事件流到了订阅者")
    finally:
        session.shutdown()


def raw_master(port: int) -> socket.socket:
    """裸 socket 当主站：pymodbus 的 client 一定"一问一答"，造不出"两条帧挤在同一段里"
    和"一条帧被拆成两段"这两种线上形状——而 sink 收到的正是这种字节块。
    """
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def fc06(address: int, value: int, tid: int = 1, unit: int = 11) -> bytes:
    """一条 FC06 的线上字节，MBAP 的 length 字段真算出来（`write_targets` 吃的就是这些偏移）。"""
    body = bytes([unit, 6, address >> 8, address & 0xFF, value >> 8, value & 0xFF])
    return tid.to_bytes(2, "big") + b"\x00\x00" + len(body).to_bytes(2, "big") + body


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise AssertionError("从站在回应读完之前就挂了连接")
        buf += chunk
    return buf


def read_adu(sock: socket.socket) -> bytes:
    """按 MBAP 的 length 收一整条响应：异常响应只有 9 字节，别按固定长度收。"""
    head = _recv_exact(sock, 6)
    return head + _recv_exact(sock, int.from_bytes(head[4:6], "big"))


def rx_chunks(sink: list[bytes]):
    """只把 rx 方向的字节块记下来（粘帧/半帧的判据只认主站那一路）。"""
    def record(direction: str, raw: bytes, *_extra) -> None:
        if direction == "rx":
            sink.append(raw)
    return record


def test_a_master_write_at_a_middle_register_takes_the_whole_point_over(master):
    """主站从 4 格 uint64 的**中间**那一格写起（FC16 addr=6 qty=2 打占 5..8 的那个点）：
    接管必须按点位整段记。按裸地址记的话，基础格 5 还在波形手上，下一 tick 就把主站刚写的
    6/7 一起顶掉——Task 2 把这个缺口写成了断言（`test_a_write_that_starts_mid_point_...`），
    这里钉它真的被补上了。
    """
    client, backend = master
    assert client.write_registers(6, [1, 2], device_id=11) is not None
    for address in (5, 6, 7, 8):
        assert not backend.source_is_waveform(11, "hr", address), address


def test_a_write_the_protocol_refused_does_not_take_the_waveform_off(master):
    """只读点被异常码 2 拒了，而 rx 报文照样解成"写"：不过滤就变成"一次没成功的写把曲线
    停了"——引擎侧的 `raw_writer` 写得进只读点，所以症状更难查（决策 1 的②）。
    """
    client, backend = master
    # CO浓度 hr0 是 readonly=True：协议当场拒绝这次写
    refused = client.write_registers(0, [1, 2], device_id=11)
    assert refused.isError() and refused.exception_code == 2
    assert backend.source_is_waveform(11, "hr", 0)
    assert backend._sources.cleared() == 0, "被拒的写往状态里塞了键"


def test_an_address_that_is_not_a_point_leaves_the_ownership_state_empty(master):
    """写落在点表的空洞上（hr 30 未定义，异常码 2）：那里没有"点"可以接管，不许把裸地址
    塞进状态（决策 1 的③）——塞进去的那些键没有任何点位能恢复得回来。
    """
    client, backend = master
    gap = client.write_register(30, 7, device_id=11)
    assert gap.isError() and gap.exception_code == 2
    assert backend.source_is_waveform(11, "hr", 30)
    assert backend._sources.cleared() == 0


def test_two_writes_inside_one_tcp_chunk_are_both_taken_over(open_backend):
    """两条写请求挤进同一个 TCP 段：pymodbus 在**组帧之前**就 trace，所以 sink 一次拿到
    的是这 24 个字节，而 `write_targets` 只解得出第一条。不按 MBAP 长度自己切，第二条就
    白写不接管，而且漏得没有声音。

    `wait_for` 那条断言是"这一条测的确实是粘帧"的证据：真要拆成了两段，测的就是下面那条
    而不是这条（切帧的缓冲区两种形状都得留下余量）。
    """
    port = free_port()
    seen: list[bytes] = []
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=rx_chunks(seen)) as backend:
        sock = raw_master(port)
        try:
            pair = fc06(4, 0x0058, tid=1) + fc06(2, 0x0102, tid=2)
            sock.sendall(pair)                       # 一次发出：两条帧粘在同一个字节块里
            wait_for(lambda: len(pair) in {len(chunk) for chunk in seen},
                     timeout=5.0, what="both frames delivered in one chunk")
            for address in (2, 3, 4):                # 状态字（1 格）+ VI风速（2 格）
                assert not backend.source_is_waveform(11, "hr", address), address
            assert backend.source_is_waveform(11, "hr", 0), "CO浓度 被主站的写接管了"
        finally:
            sock.close()


def test_a_write_split_across_two_chunks_is_still_taken_over(open_backend):
    """一条帧被拆成两段（只到地址字段的那 10 个字节先到）：这次写照样得判出接管。

    先等 sink 真的收到那个 10 字节的块，再断言"这会儿还没接管"——没有这一步，两段可能被
    合并成一段，测的就不是半帧了。补齐那一次 sink 收到的是**整个 12 字节的帧**（trace 的是
    transport 的累计缓冲，余量连着新字节整段重放），`_note_master_writes` 正是按这个实测
    形状决定"不留自己的余量"；`write_targets` 对半帧回 None（Task 2 钉的）。
    """
    port = free_port()
    seen: list[bytes] = []
    frame = fc06(4, 0x0058)
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=rx_chunks(seen)) as backend:
        sock = raw_master(port)
        try:
            sock.sendall(frame[:10])
            wait_for(lambda: [len(chunk) for chunk in seen] == [10],
                     timeout=5.0, what="the half frame reaches the sink on its own")
            assert backend.source_is_waveform(11, "hr", 4), "半个帧就把点接管了"
            sock.sendall(frame[10:])
            response = read_adu(sock)
            assert [len(chunk) for chunk in seen] == [10, 12], \
                "余量不是整段重放的：backend 那层的切帧判据得跟着改"
            assert not backend.source_is_waveform(11, "hr", 4), "补齐之后没判出这次写"
        finally:
            sock.close()
    assert response[7] == 6 and int.from_bytes(response[10:12], "big") == 0x0058


def test_the_frame_after_a_replayed_half_is_cut_at_the_right_offset(open_backend):
    """半帧被重放、消费掉之后，下一条帧必须重新从第 0 字节切——这一条钉住"backend 自己
    不留余量"那个决定：照直觉留一份余量拼回去，见过的那 10 个字节会被数两遍，之后每一条
    真帧都对错偏移，接管从此条条判不成（而那正是漏得没有声音的那类 bug）。

    形状是实测的：`[10]` → `[12]`（同一帧整段重放，从站答完把它自己的余量清了）→ `[12]`
    （下一条独立的新帧）。第三条打的是**另一个点**（VI风速 hr2），它被接管才证明偏移没被
    前一条带歪。
    """
    port = free_port()
    seen: list[bytes] = []
    first = fc06(4, 0x0058, tid=1)
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=rx_chunks(seen)) as backend:
        sock = raw_master(port)
        try:
            sock.sendall(first[:10])
            wait_for(lambda: len(seen) == 1, timeout=5.0, what="the half frame arrives")
            sock.sendall(first[10:])
            read_adu(sock)                              # 从站答了第一条
            sock.sendall(fc06(2, 0x4050, tid=2))
            wait_for(lambda: not backend.source_is_waveform(11, "hr", 2),
                     timeout=5.0,
                     what="a whole frame after a replayed half is still framed")
            assert [len(chunk) for chunk in seen] == [10, 12, 12], seen
            assert not backend.source_is_waveform(11, "hr", 4)
        finally:
            sock.close()


def test_a_chunk_whose_length_field_lies_does_not_take_over_anything(open_backend):
    """长度字段撒谎的字节块（扫描工具、半坏的 485 桥）不许往归属状态里塞任何一个点：它解不出
    真的写报文，而塞进去的键没有任何点位能恢复回来，界面上就是一堆来路不明的"已停"。

    撒谎的长度同样会把 `pymodbus` 自己的组帧卡住（实测它按字段死等），那条写压根落不到
    寄存器上，所以"这一段一个都不认"对得上协议现实。换一台主站之后必须照旧认得写：这条
    把"这一台不认"和"永远瞎掉"分开——垃圾不许跟着下一台连接走。
    """
    port = free_port()
    seen: list[bytes] = []
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=rx_chunks(seen)) as backend:
        sock = raw_master(port)
        sock.sendall(b"\x00\xff\x00\x00\xff\xff\x0b\x10")   # length=65535：不可能是帧
        wait_for(lambda: len(seen) == 1, timeout=5.0,
                 what="the lying header reaches the sink")
        assert backend._sources.cleared() == 0, "撒谎的帧头也敢标接管"
        sock.close()
        client = connect(port)                          # 下一台主站，同一条通道
        try:
            assert client.write_register(4, 0x0058, device_id=11).registers == [0x0058]
        finally:
            client.close()
        assert not backend.source_is_waveform(11, "hr", 4), "上一台的垃圾把这一台带瞎了"


def test_a_chunk_whose_quantity_field_lies_does_not_take_over_anything(open_backend):
    """上一条的兄弟：MBAP 的 length 规规矩矩、**数量字段**写 65535 的那条 FC16。

    253 字节的 PDU 装不下 65535 个寄存器（上限 123），协议这一步就把帧拒了，一个寄存器都没
    落到寄存器上；照数量去摊点表等于"一帧坏报文把整条通道的曲线静默停掉、日志一个字没有"
    （实测：unit 11 的 hr 区 10 个地址一次全成 manual）。
    后半段钉"上限内的写照旧要认"——只删不判的话这一条也绿。
    """
    port = free_port()
    seen: list[bytes] = []
    lying = (b"\x00\xfe\x00\x00\x00\x0b" + bytes([11])          # length=11 = unit + 10B PDU
             + b"\x10" + (0).to_bytes(2, "big")
             + (65535).to_bytes(2, "big") + b"\x04" + b"\x00\x01\x00\x02")
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=rx_chunks(seen)) as backend:
        sock = raw_master(port)
        try:
            sock.sendall(lying)
            wait_for(lambda: len(seen) == 1, timeout=5.0,
                     what="the lying-quantity frame reaches the sink")
            assert backend._sources.cleared() == 0, "撒谎的数量把整表标了接管"
            assert backend.source_is_waveform(11, "hr", 4), "曲线被一帧坏报文停了"
        finally:
            sock.close()
        client = connect(port)
        try:
            assert client.write_register(4, 0x0058, device_id=11).registers == [88]
        finally:
            client.close()
        assert not backend.source_is_waveform(11, "hr", 4), "合法数量的写也不认了"


# ---- M4 的三条真主站验收：曲线要主站看得见，下发要活过 tick ----

def test_a_real_master_reads_a_running_sine_wave(open_backend):
    """曲线必须是**主站侧**看得见：界面自己刷是一回事，网关读到的是另一回事。

    周期 1.2s、`tick_ms=50`，采 4 次每次间隔 0.4s：区间要守住，而且不能是一串常数
    （常数就是"tick 没在跑"或"值被谁顶住了"的形状）。`CO浓度` 是 CDAB 浮点，正好把
    字序一起验了。

    采样前先拿主站自己的读等第一拍：`_tick_loop` 是**先睡一个周期再算**，监听起来之后的
    头 50ms 里寄存器还是点表初值 12.5（实测首样 4/4 次都是它），照字面"起来就采 4 次"
    会把那条初值算进区间断言里。
    """
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[0]
    point.waveform, point.wave_param = "sine", {"base": "100", "amp": "20",
                                                "period": "1.2"}
    with open_backend(project.channels[0], tick_ms=50):
        client = connect(project.channels[0].port)
        seen = []
        try:
            wait_for(lambda: 80.0 <= cdab_to_float(client.read_holding_registers(
                0, count=2, device_id=11).registers) <= 120.0,
                what="第一拍 tick 把 sine 写进了寄存器")
            for _ in range(4):
                seen.append(client.read_holding_registers(0, count=2,
                                                          device_id=11).registers)
                time.sleep(0.4)
        finally:
            client.close()
    values = [cdab_to_float(pair) for pair in seen]
    assert all(80.0 <= v <= 120.0 for v in values), values
    assert len({round(v, 1) for v in values}) >= 3, f"曲线没在动：{values}"


def test_a_master_write_survives_the_waveform_tick(open_backend):
    """给 `hr4` 配一条 0.4 秒一圈的 `sine`，主站写 333 进去，0.5 秒后必须还是 333。

    这条红了就是 M4 的核心承诺破了：平台下发成功、值被模拟器顶掉，运维会去查平台
    而不是查这里。0.5s 在 `tick_ms=50` 下跨 10 个 tick、1.25 个周期，够证"没被覆盖"。
    """
    project = tunnel_project(free_port())
    point = next(pt for pt in project.channels[0].devices[0].points if pt.name == "状态字")
    point.waveform, point.wave_param = "sine", {"base": "0", "amp": "10",
                                                "period": "0.4"}
    with open_backend(project.channels[0], tick_ms=50):
        client = connect(project.channels[0].port)
        try:
            client.write_register(4, 333, device_id=11)
            time.sleep(0.5)
            assert client.read_holding_registers(4, count=1,
                                                 device_id=11).registers == [333]
        finally:
            client.close()


def test_a_master_write_of_a_coil_survives_the_waveform_tick(open_backend):
    """位区单独走一条：`write_registers` 写 `co` 会写错一位（raw_writer 的实测说明）。

    `启动` 线圈配 `step;1,0;interval=0.2`——不接管的话它每 0.2 秒翻一次，主站读到的是
    一直在抖的启动指令。写 1 之后 0.5 秒里连读 5 次，每次都必须是 1。

    **连读而不是只读一次**（实测把这条改结实过）：0.5s 正好是 `interval` 的 2.5 倍，
    单点读会撞上"曲线此刻正好在 1 那一半"，变异（tick 无视归属）当场存活；把这 0.5 秒
    铺开取样，任何一次越权写入都会被其中一次读到。
    """
    project = tunnel_project(free_port())
    coil = next(pt for pt in project.channels[0].devices[0].points if pt.area == "co")
    coil.waveform, coil.wave_param = "step", {"values": "1,0", "interval": "0.2"}
    with open_backend(project.channels[0], tick_ms=50):
        client = connect(project.channels[0].port)
        try:
            client.write_coil(coil.address, True, device_id=11)
            seen = []
            for _ in range(5):
                seen.append(client.read_coils(coil.address, count=1,
                                              device_id=11).bits[0])
                time.sleep(0.1)
            assert all(bit is True for bit in seen), \
                f"下发被波形顶回去过：{seen}"
        finally:
            client.close()


def test_the_probe_does_not_refuse_a_port_whose_previous_owner_was_killed():
    """"杀掉旧实例、立刻起新的"是现场每天做的事：这一次不许拒（两轮回环）。

    本机实测：子进程 bind+listen 在 `127.0.0.1:P`、被 kill 之后立刻

      * `bind("0.0.0.0", P)` 4/4 成功，

      * `bind("127.0.0.1", P)` 3/4 撞 WinError 10048（端口还没从内核里退干净），

      * connect 判据 4/4 说"没人在服务"。

    所以这里按 `0.0.0.0` 起，只测 connect 探针那一段；同一地址立刻重启会被 bind 探针

    挡一下是 M1 就有的行为，不归这条管。

    """

    for _ in range(2):

        port = free_port()

        child = subprocess.Popen(

            [sys.executable, "-c",

             "import socket, time, os\n"

             "s = socket.socket()\n"

             "s.bind(('127.0.0.1', int(os.environ['SIMBENCH_HOG_PORT'])))\n"

             "s.listen(5)\n"

             "print('READY', flush=True)\n"

             "time.sleep(60)\n"],

            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,

            encoding="utf-8", env=dict(os.environ, SIMBENCH_HOG_PORT=str(port)))

        try:

            assert child.stdout.readline().strip() == "READY", "占位进程没起来"

            with contextlib.closing(socket.create_connection(

                    ("127.0.0.1", port), timeout=2)):

                pass                       # 留一条已经建立过的连接，正是残影的来源

            child.kill()

            child.wait(timeout=10)

            check_port_is_free("0.0.0.0", port, "tcp")    # 不许误拒

        finally:

            with contextlib.suppress(Exception):

                child.kill()

            with contextlib.suppress(Exception):

                child.wait(timeout=5)
