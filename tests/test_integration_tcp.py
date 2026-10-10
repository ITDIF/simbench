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

from modslave.backend.modbus_tcp import (
    _DropDatastoreNoise, ModbusTcpBackend, PortInUseError, _bind_probe,
    _connect_probe, _is_being_served, check_port_is_free, configure_library_logging,
    parse_packet,
)
from modslave.codec.point_codec import encode
from modslave.project.model import Device, Point, Project, TcpChannel
from modslave.project.validate import validate_project
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

    只点名 `modslave*` 线程：探针用的 `asyncio.to_thread` 工作线程本来就有生有灭，拿
    "全部线程数不变"去判会把一次正常启动误判成泄漏。共享 runner 自己那两条 loop/pump
    线程横跨整个会话，属于预期内，所以比的是"集合没多出新的"。
    """
    def engine_threads():
        return {t.name for t in threading.enumerate() if t.name.startswith("modslave")}

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
    会**两个都成功**（`_await_listener` 只看自己 transport 是否为 None，证明不了端口归属），
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


def test_await_listener_reports_a_late_bind_failure():
    """探针之后仍可能晚到失败（TOCTOU、保留端口 10013）：`_await_listener` 照旧抛真异常。

    `serve_forever()` 只有两种提前结束：bind 失败与被 shutdown。所以 task 一结束就说明
    从站没起来，这里把真实异常原样抛出去，不许"start 成功但外面连不上"（静默失败）。
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
            await backend._await_listener(NoListener(), task)

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
    import modslave.backend.modbus_tcp as mbt
    from modslave.engine.runner import EngineRunner

    inside_probe = threading.Event()

    def slow_probe(host, port, channel_name=None):
        inside_probe.set()
        time.sleep(0.3)                      # 模拟 connect 探针的阻塞窗口

    monkeypatch.setattr(mbt, "check_port_is_free", slow_probe)
    runner = EngineRunner(name="modslave-probe-loop")
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

             "s.bind(('127.0.0.1', int(os.environ['MODSLAVE_HOG_PORT'])))\n"

             "s.listen(5)\n"

             "print('READY', flush=True)\n"

             "time.sleep(60)\n"],

            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,

            encoding="utf-8", env=dict(os.environ, MODSLAVE_HOG_PORT=str(port)))

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
