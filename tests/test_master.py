"""主站模式（E1）：扫 unit + 逐点核对。

**验收用的是我们自己的从站**（`ModbusTcpBackend` + 真端口 + 真主站客户端）：这样一个进程里
从站与主站都是真的，读回来的值来自真 datastore。另外用桩链路钉住四类结论的分类逻辑
（`no_unit` / `error` / `mismatch` / `ok`）——那些形状真从站造起来反而绕。
"""

from __future__ import annotations

import pathlib
import socket

import pytest

from simbench.backend import make_backend
from simbench.csv_io_entry import load_point_table
from simbench.master import report
from simbench.master.modbus import MasterLink
from simbench.master.scan import VerifyRow, scan_units, verify_points
from simbench.project.model import (
    Device,
    Point,
    Project,
    SerialChannel,
    TcpChannel,
    channel_transport,
)
from simbench.project.validate import error_lines, validate_project

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")
_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StubLink:
    """桩链路：按 `(unit, area, address)` 给值，`None` 表示"没应答"。"""

    def __init__(self, table: dict, *, exception: set | None = None) -> None:
        self.table = table
        self.exception = exception or set()
        self.reads: list[tuple] = []

    def read(self, unit_id, area, address, count):
        from simbench.master.modbus import ReadResult

        self.reads.append((unit_id, area, address, count))
        if (unit_id, area, address) in self.exception:
            return ReadResult(values=None, detail="exception code 2")
        values = self.table.get((unit_id, area, address))
        if values is None:
            return ReadResult(values=None, detail="no response (timeout)")
        return ReadResult(values=list(values[:count]), detail="")

    def probe(self, unit_id):
        return self.read(unit_id, "hr", 0, 1)


def point(**kw) -> Point:
    base = {"name": "温度", "area": "hr", "address": 0, "datatype": "uint16",
            "initial": "25"}
    base.update(kw)
    return Point(**base)


def project_with(*points: Point, unit: int = 11) -> Project:
    return Project(channels=[TcpChannel("tcp-1", host="127.0.0.1", port=5020,
                                        devices=[Device("D1", unit, list(points))])])


# ---------- 桩：四类结论 ----------

def test_a_matching_point_is_ok():
    link = StubLink({(11, "hr", 0): [25]})
    (row,) = verify_points(link, project_with(point()))
    assert (row.status, row.actual) == ("ok", "25")


def test_a_different_value_is_a_mismatch():
    link = StubLink({(11, "hr", 0): [26]})
    (row,) = verify_points(link, project_with(point()))
    assert row.status == "mismatch" and "期望" in row.detail


def test_a_silent_unit_is_no_unit():
    (row,) = verify_points(StubLink({}), project_with(point()))
    assert row.status == "no_unit" and "timeout" in row.detail


def test_an_exception_code_is_an_error():
    link = StubLink({}, exception={(11, "hr", 0)})
    (row,) = verify_points(link, project_with(point()))
    assert row.status == "error" and "exception code 2" in row.detail


def test_the_read_asks_for_exactly_the_points_span():
    """读最少的那几格：读多了会把邻居的值混进来（那种"对上了"是假的）。"""
    link = StubLink({(11, "hr", 0): [0x0000, 0x4148]})
    verify_points(link, project_with(point(datatype="float32", initial="12.5")))
    assert link.reads == [(11, "hr", 0, 2)]


def test_a_coil_is_read_as_a_bit():
    link = StubLink({(11, "co", 3): [1]})
    (row,) = verify_points(link, project_with(
        point(area="co", address=3, datatype="bool", initial="true")))
    assert (row.status, row.actual) == ("ok", "true")
    assert link.reads == [(11, "co", 3, 1)]


def test_a_point_whose_initial_is_broken_says_so():
    """初值自己就编不出来（点表里有错）：如实说，不许报成"对不上"。"""
    (row,) = verify_points(StubLink({(11, "hr", 0): [1]}),
                          project_with(point(datatype="uint16", initial="abc")))
    assert row.status == "error" and "算不出" in row.detail


def test_scan_reports_only_the_units_that_answer():
    link = StubLink({(11, "hr", 0): [1], (42, "hr", 0): [1]})
    assert scan_units(link, units=[1, 11, 12, 42]) == [11, 42]


def test_scan_reports_progress():
    seen: list[tuple[int, int]] = []
    scan_units(StubLink({}), units=[1, 2, 3], progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_s7_channels_are_reported_as_not_supported_yet():
    from simbench.project.model import S7Channel
    project = Project(channels=[S7Channel("s7-1", host="127.0.0.1", port=102, devices=[
        Device("D1", 11, [Point("温度", "db1", 0, "real", initial="12.5")])])])
    (row,) = verify_points(StubLink({}), project)
    assert row.status == "error" and "S7" in row.detail


# ---------- 报告 ----------

def test_the_text_report_counts_and_lists_only_the_bad_ones():
    rows = [VerifyRow("c", "d", 11, "a", "hr", 0, "uint16", "1", "1", "ok"),
            VerifyRow("c", "d", 11, "b", "hr", 1, "uint16", "2", "3", "mismatch",
                      "期望 [2]，读到 [3]")]
    text = report.to_text(rows)
    assert "对得上 1" in text and "对不上 1" in text
    assert "b" in text and "a" not in text.split("\n", 1)[1]


def test_a_report_exports_to_csv_and_xlsx(tmp_path):
    rows = [VerifyRow("c", "d", 11, "a", "hr", 0, "uint16", "1", "2", "mismatch", "x")]
    for suffix in (".csv", ".xlsx"):
        target = report.write(tmp_path / f"r{suffix}", rows)
        assert target.exists() and target.stat().st_size > 0
        assert not (tmp_path / f"r{suffix}.bak").exists(), "第一份报告不该有备份"


def test_an_unknown_report_suffix_is_refused(tmp_path):
    with pytest.raises(ValueError, match="只支持"):
        report.write(tmp_path / "r.txt", [])


# ---------- 真从站 + 真主站：端到端 ----------

def real_project(port: int) -> Project:
    return Project(channels=[TcpChannel("tcp-1", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("温度", "hr", 0, "uint16", initial="25"),
            Point("累积", "hr", 5, "uint64", initial="72623859790382856"),
        ]),
        Device("D2", 12, [Point("别家", "hr", 0, "uint16", initial="7")]),
    ])])


def test_a_real_slave_is_scanned_and_verified(engine_runner):
    """端到端：起真从站 → 扫 unit（11、12 应答）→ 逐点核对（对得上）。"""
    port = free_port()
    project = real_project(port)
    assert error_lines(validate_project(project)) == []
    backend = make_backend(project.channels[0])
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "master scan: start")
    link = MasterLink.for_channel(project.channels[0])
    try:
        link.open()
        assert scan_units(link, units=[10, 11, 12, 13]) == [11, 12]
        rows = verify_points(link, project)
        assert [row.status for row in rows] == ["ok", "ok", "ok"], \
            [(row.name, row.status, row.detail) for row in rows]
    finally:
        link.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "master scan: stop")


def test_a_wrong_point_in_the_table_shows_up_as_a_mismatch(engine_runner):
    """点表把地址写错一位：核对必须报"对不上"（这就是这一整功能的意义）。"""
    port = free_port()
    project = real_project(port)
    backend = make_backend(project.channels[0])
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "master verify: start")
    # 温度那一格在从站上是 25，这里故意把点表初值写成 26（等价于地址/类型写错）
    project.channels[0].devices[0].points[0].initial = "26"
    link = MasterLink.for_channel(project.channels[0])
    try:
        link.open()
        rows = verify_points(link, project)
        assert rows[0].status == "mismatch", [(r.name, r.status) for r in rows]
        assert rows[1].status == "ok" and rows[2].status == "ok"
    finally:
        link.close()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "master verify: stop")


def test_the_link_description_names_the_transport():
    tcp = TcpChannel("tcp-1", host="127.0.0.1", port=5020)
    assert MasterLink.for_channel(tcp).describe() == "127.0.0.1:5020 (tcp)"
    serial = SerialChannel("com-1", port="COM3", baudrate=19200, parity="E")
    link = MasterLink.for_channel(serial)
    assert link.describe() == "COM3 19200 8E1 (rtu)"
    assert channel_transport(serial) == "rtu"


def test_an_s7_channel_cannot_be_used_as_a_master_link():
    from simbench.project.model import S7Channel
    with pytest.raises(ValueError, match="S7"):
        MasterLink.for_channel(S7Channel("s7-1", host="127.0.0.1", port=102))

# ---------- 命令行：--scan / --verify ----------

CSV_HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,"
              "initial,waveform,wave_param,readonly")


def write_table(tmp_path, port: int, *, initial: str = "25") -> pathlib.Path:
    target = tmp_path / "master.csv"
    target.write_text(
        f"{CSV_HEADER}\n"
        f"tcp-1@tcp:127.0.0.1:{port},D1,11,温度,hr,0,uint16,ABCD,{initial},const,,false\n"
        f"tcp-1@tcp:127.0.0.1:{port},D2,12,别家,hr,0,uint16,ABCD,7,const,,false\n",
        encoding="utf-8")
    return target


def test_the_command_line_scans_and_verifies(engine_runner, tmp_path, capsys):
    """命令行那两条：扫 unit、逐点核对、写报告，走的是真从站（起在同一台机器上）。"""
    from simbench.__main__ import main

    port = free_port()
    table = write_table(tmp_path, port)
    project, _notes, issues = load_point_table(str(table))
    assert not [i for i in issues if i.level == "error"]
    backend = make_backend(project.channels[0])
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "cli master: start")
    try:
        assert main([str(table), "--scan", "--units", "10-13"]) == 0
        out = capsys.readouterr()
        assert "11" in out.out and "12" in out.out and "13" not in out.out

        report_path = tmp_path / "report.csv"
        assert main([str(table), "--verify", "--result", str(report_path)]) == 0
        out = capsys.readouterr()
        assert "对得上 2" in out.out
        assert "温度" in report_path.read_text(encoding="utf-8")
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "cli master: stop")
