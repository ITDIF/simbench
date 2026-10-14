"""能被 **Modbus Poll** 读：按它的请求形状做字节级验收。

为什么单独一组：`tests/test_integration_tcp.py` 用的是 pymodbus 客户端（它替我们拼帧、替我们
解异常），而 Modbus Poll 是**现场最常用的那把尺子**——它读不到时，现场的结论是"模拟器坏了"。
这一组直接**手拼 MBAP + PDU**（不经过任何客户端库），把 Modbus Poll 会发的几种请求原样打
过去，逐字节看应答：读四种区、125 格上限、跨 126 格、写单个/多个、只读点、unit 不对、
以及"点表有空洞时整条请求失败"这条现场最容易被坑的行为。
"""

from __future__ import annotations

import pathlib
import socket
import struct

import pytest

from simbench.backend import make_backend
from simbench.csv_io_entry import load_point_table
from simbench.project.model import Device, Point, TcpChannel

_CALL_TIMEOUT = 10.0
ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "virtual-device.csv"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ---------- 手拼帧（与 Modbus Poll 发出去的东西同一层）----------

def mbap(unit: int, pdu: bytes) -> bytes:
    """MBAP 头 + PDU。事务号与协议号固定：Modbus Poll 也是自己给这两个数。"""
    return struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu


def read_request(unit: int, fc: int, address: int, count: int) -> bytes:
    return mbap(unit, struct.pack(">BHH", fc, address, count))


def write_single(unit: int, fc: int, address: int, value: int) -> bytes:
    return mbap(unit, struct.pack(">BHH", fc, address, value))


def write_multiple_registers(unit: int, address: int, values: list[int]) -> bytes:
    payload = b"".join(struct.pack(">H", value) for value in values)
    return mbap(unit, struct.pack(">BHHB", 16, address, len(values), len(payload)) + payload)


def write_multiple_coils(unit: int, address: int, bits: list[int]) -> bytes:
    packed = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            packed[index // 8] |= 1 << (index % 8)
    return mbap(unit, struct.pack(">BHHB", 15, address, len(bits), len(packed)) + bytes(packed))


class Client:
    """一条 TCP 连接上的裸收发（`Modbus Poll` 就是这么一个循环）。"""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)

    def ask(self, request: bytes, timeout: float = 1.5) -> bytes:
        self.sock.sendall(request)
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv(4096)
        except TimeoutError:
            return b""

    def close(self) -> None:
        self.sock.close()


def result(response: bytes):
    """应答 -> `(kind, value)`：`kind` 是 `"regs"` / `"bits"` / `"echo"` / `"exception"` / `"none"`。"""
    if not response:
        return "none", None
    function = response[7]
    if function & 0x80:
        return "exception", response[8]
    if function in (1, 2):
        return "bits", response[9]
    if function in (5, 6, 15, 16):
        return "echo", response[8:]
    count = response[8] // 2
    return "regs", list(struct.unpack(f">{count}H", response[9:9 + response[8]]))


def bits_of(byte: int, count: int) -> list[int]:
    return [(byte >> index) & 1 for index in range(count)]


@pytest.fixture
def demo(engine_runner):
    """起示例那台虚拟设备（真端口、真 datastore），给一组裸连接。"""
    port = free_port()
    project, _notes, issues = load_point_table(str(DEMO))
    assert not [i for i in issues if i.level == "error"]
    project.channels[0].port = port
    project.channels[0].host = "127.0.0.1"
    backend = make_backend(project.channels[0])
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "poll demo start")
    yield port, backend
    engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "poll demo stop")


# ---------- 读：Modbus Poll 的四种显示模式 ----------

def test_a_default_ten_register_read_works_on_the_virtual_device(demo):
    """Modbus Poll 起手就是"读 10 个保持寄存器"：这一条必须一次成功（示例表是连续的）。"""
    port, _backend = demo
    client = Client(port)
    try:
        kind, regs = result(client.ask(read_request(11, 3, 0, 10)))
        assert kind == "regs" and len(regs) == 10, regs
        # 前两格是 float32 CDAB 的 20.0：ABCD 是 [0x41A0, 0x0000]，CDAB 把两格换位
        assert regs[:2] == [0x0000, 0x41A0], [hex(value) for value in regs[:2]]
        assert regs[4] == 11 and regs[9] == 65, regs                     # 风机状态 / 湿度
    finally:
        client.close()


def test_coils_and_discrete_inputs_and_input_registers(demo):
    port, _backend = demo
    client = Client(port)
    try:
        kind, byte = result(client.ask(read_request(11, 1, 0, 4)))       # 线圈
        assert kind == "bits" and bits_of(byte, 4) == [0, 1, 0, 0]
        kind, byte = result(client.ask(read_request(11, 2, 0, 4)))       # 离散输入
        assert kind == "bits" and bits_of(byte, 4) == [1, 0, 0, 0]
        kind, regs = result(client.ask(read_request(11, 4, 0, 4)))       # 输入寄存器
        assert kind == "regs" and regs[:2] == [7, 0], regs               # 7 与 3600 的高半
    finally:
        client.close()


def test_the_modbus_limit_is_125_and_126_is_refused(engine_runner):
    """一次最多 125 格（Modbus 协议上限）：Modbus Poll 也是这么切的。

    用一张"整块 300 格"的点表来验——示例那台虚拟设备只有 18 格，读 125 会跨到块外
    （那是下面"空洞"那两条的事）。
    """
    port = free_port()
    device = Device("D1", 11, [Point(f"P{index}", "hr", index, "uint16", initial="1")
                               for index in range(300)])
    backend = make_backend(TcpChannel("tcp-demo", host="127.0.0.1", port=port,
                                      devices=[device]))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "block start")
    client = Client(port)
    try:
        assert result(client.ask(read_request(11, 3, 0, 125)))[0] == "regs"
        assert result(client.ask(read_request(11, 3, 0, 126))) == ("exception", 1)
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "block stop")


def test_a_wrong_unit_id_answers_an_exception_not_silence(demo):
    """unit 不对：本模拟器回异常码 4（"设备在，但不认这个 unit"），不是超时。

    这一条在扫 unit 时要留意：**有应答（哪怕是异常）**就说明"这个 unit 号被回应了"。
    """
    port, _backend = demo
    client = Client(port)
    try:
        assert result(client.ask(read_request(12, 3, 0, 1))) == ("exception", 4)
    finally:
        client.close()


# ---------- 写：Modbus Poll 的两个写面板 ----------

def test_single_and_multiple_writes_change_the_values(demo):
    port, backend = demo
    client = Client(port)
    try:
        assert result(client.ask(write_single(11, 6, 4, 42)))[0] == "echo"      # 写一个
        assert result(client.ask(read_request(11, 3, 4, 1)))[1] == [42]
        assert result(client.ask(write_multiple_registers(11, 9, [7, 8])))[0] == "echo"
        assert result(client.ask(read_request(11, 3, 9, 2)))[1] == [7, 8]
        assert result(client.ask(write_multiple_coils(11, 2, [1, 1])))[0] == "echo"
        kind, byte = result(client.ask(read_request(11, 1, 2, 2)))
        assert bits_of(byte, 2) == [1, 1]
    finally:
        client.close()


def test_a_readonly_point_refuses_writes(demo):
    """只读点（累计电量 / 铭牌）：写它回异常码 2——Modbus Poll 那一栏会标红，这是**对的**。"""
    port, _backend = demo
    client = Client(port)
    try:
        assert result(client.ask(write_single(11, 6, 5, 1))) == ("exception", 2)
    finally:
        client.close()


def test_a_master_write_shows_up_as_manual_in_the_table(demo):
    """主站写过的点，界面那一路会显示"已停"（归属）：UDP/TCP 两条传输同一条口径。"""
    port, backend = demo
    client = Client(port)
    try:
        assert backend.source_is_waveform(11, "hr", 4) is True, "还没人写它，该归波形"
        client.ask(write_single(11, 6, 4, 42))            # 主站写风机状态那一格
        assert backend.source_is_waveform(11, "hr", 4) is False, "主站写过的点没切成 manual"
        assert backend.read_registers(11, "hr", 4, 1) == [42]
    finally:
        client.close()


# ---------- 空洞：现场最容易被坑的那一条 ----------

def gappy_project(port: int):
    return TcpChannel("tcp-demo", host="127.0.0.1", port=port, devices=[Device("D1", 11, [
        Point("A", "hr", 0, "uint16", initial="111"),
        Point("B", "hr", 5, "uint16", initial="222"),
    ])])


def test_a_read_that_spans_a_hole_fails_the_whole_request(engine_runner):
    """点表里有空洞时：**整条请求**回异常码 2（读 1 格给，读 2 格就跨到空洞上）。

    这条不是缺陷，是"只答我声明过的格"那条口径的直接后果；但它对现场的影响很大
    （Modbus Poll 里看着像"怎么都读不到"），所以在这里钉死，并在 `--fill-gaps` 那条
    用例里给出解法。
    """
    port = free_port()
    backend = make_backend(gappy_project(port))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "gappy start")
    client = Client(port)
    try:
        assert result(client.ask(read_request(11, 3, 0, 1)))[1] == [111]
        assert result(client.ask(read_request(11, 3, 0, 2))) == ("exception", 2)
        assert result(client.ask(read_request(11, 3, 1, 1))) == ("exception", 2), \
            "空洞那一格单独读也该拒"
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "gappy stop")


def test_filling_the_gaps_makes_the_whole_block_readable(engine_runner):
    """`edit.fill_gaps` 之后同一份点表读 10 格成功——空洞变成显式的"保留"点（0）。

    为什么这样做是对的：补出来的行**看得见**（点表里真的多了那几行、界面上也显示），
    不像"隐式回 0"那样把没声明的东西演成设备报的 0。
    """
    from simbench.project import edit
    from simbench.project.model import Project
    from simbench.project.validate import error_lines, validate_project

    port = free_port()
    channel = TcpChannel("tcp-demo", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [Point("A", "hr", 0, "uint16", initial="111"),
                          Point("B", "hr", 5, "uint16", initial="222")])])
    added = edit.fill_gaps(Project(channels=[channel]))
    assert [point.address for _name, _unit, point in added] == [1, 2, 3, 4], added
    assert all(point.name == f"保留{point.address}" for _name, _unit, point in added)
    assert all(point.datatype == "reg16" and point.initial == "0"
               for _name, _unit, point in added)
    assert error_lines(validate_project(Project(channels=[channel]))) == [], "补完反而不合法了"

    backend = make_backend(channel)
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "filled start")
    client = Client(port)
    try:
        # 补到"声明末尾"为止：0..5 现在是一整块（原来读 2 格就失败）
        kind, regs = result(client.ask(read_request(11, 3, 0, 6)))
        assert kind == "regs", regs
        assert regs == [111, 0, 0, 0, 0, 222], regs
        # 再往后（6..9）仍然没声明：那一条还是异常码 2——**补洞不等于把整张表变无限大**
        assert result(client.ask(read_request(11, 3, 0, 10))) == ("exception", 2)
    finally:
        client.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "filled stop")

# ---------- 命令行那一步：--fill-gaps ----------

def test_the_command_line_fills_the_gaps_and_writes_the_table(tmp_path, capsys):
    """`--fill-gaps --result`：补完写成一份新点表（现场拿它去给 Modbus Poll 读整块）。"""
    from simbench.__main__ import main

    source = tmp_path / "gappy.csv"
    source.write_text(
        "channel,device,unit_id,name,area,address,datatype,word_order,initial,"
        "waveform,wave_param,readonly\n"
        "tcp-1@tcp:0.0.0.0:5020,D1,11,A,hr,0,uint16,ABCD,111,const,,false\n"
        "tcp-1@tcp:0.0.0.0:5020,D1,11,B,hr,5,uint16,ABCD,222,const,,false\n",
        encoding="utf-8")
    filled = tmp_path / "filled.csv"
    assert main([str(source), "--fill-gaps", "--result", str(filled)]) == 0
    out = capsys.readouterr().out
    assert "补了 4 格保留点" in out, out
    # 补完那一份读 6 格是一整块（原来读 2 格就失败）
    project, _notes, issues = load_point_table(str(filled))
    assert not [i for i in issues if i.level == "error"], [str(i) for i in issues]
    rows = sorted(point.address for point in project.channels[0].devices[0].points)
    assert rows == [0, 1, 2, 3, 4, 5]


def test_the_command_line_says_so_when_there_is_nothing_to_fill(tmp_path, capsys):
    from simbench.__main__ import main

    assert main([str(DEMO), "--fill-gaps"]) == 0, "示例那台虚拟设备本来是连续的吗？"
    assert "没有空洞" in capsys.readouterr().out
