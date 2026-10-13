"""声明里的地址真的被听上，而且**各个地址读写的是同一份寄存器**。

现场形状：点表把通道写在 `0.0.0.0:5020`（本机自测用），声明说设备在
`10.136.107.240:502`。主站按设备 IP 连，所以后端除了通道自己那个监听，还得把这一路
声明里的地址也听上。

多挂监听最危险的走法是"每个地址一份副本"：主站从 A 地址写、界面按主 server 读回旧值，
两边都不报错——正是这个项目最怕的那种静默错。这里用真主站把"同一份"这件事钉住，
两侧都写一遍、都读一遍。判定用真套接字，不用打桩。
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import socket
import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from simbench.backend.modbus_tcp import ModbusTcpBackend, PortInUseError
from simbench.engine.session import Session
from simbench.project.model import Device, Point, TcpChannel
from tests.conftest import free_port

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _channel(port: int) -> TcpChannel:
    """一路最小点表：一个保持寄存器（可写）+ 一台设备。"""
    return TcpChannel(name="tcp-x", host="127.0.0.1", port=port,
                      devices=[Device(name="D", unit_id=11, points=[
                          Point("值", "hr", 0, "uint16", initial="7")])])


def _start(engine_runner, channel, extra=()):
    backend = ModbusTcpBackend(channel, tick_ms=0, extra_endpoints=extra)
    engine_runner.call(lambda: backend.start(), 10.0, "start backend")
    return backend


def _stop(engine_runner, backend):
    engine_runner.call(lambda: backend.stop(), 10.0, "stop backend")


def _serving(host: str, port: int) -> bool:
    with contextlib.closing(socket.socket()) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, port)) == 0


def _read(client, address=0):
    return client.read_holding_registers(address, count=1, device_id=11)


def test_both_endpoints_serve_one_datastore(engine_runner):
    near, far = free_port(), free_port()
    backend = _start(engine_runner, _channel(near), [("127.0.0.1", far)])
    try:
        assert backend.endpoints == [("127.0.0.1", near), ("127.0.0.1", far)]
        with contextlib.closing(ModbusTcpClient("127.0.0.1", port=near)) as first, \
             contextlib.closing(ModbusTcpClient("127.0.0.1", port=far)) as second:
            assert _read(first).registers == [7]
            assert second.write_register(0, 4242, device_id=11).registers == [4242]
            assert _read(first).registers == [4242], "额外地址写的值没进主 server 那份"
            assert first.write_register(0, 99, device_id=11).registers == [99]
            assert _read(second).registers == [99], "主地址写的值在额外地址上看不见"
    finally:
        _stop(engine_runner, backend)


def test_only_the_channel_listener_when_there_is_no_declaration(engine_runner):
    near = free_port()
    backend = _start(engine_runner, _channel(near))
    try:
        assert backend.endpoints == [("127.0.0.1", near)]
    finally:
        _stop(engine_runner, backend)


def test_stopping_releases_every_listener(engine_runner):
    near, far = free_port(), free_port()
    backend = _start(engine_runner, _channel(near), [("127.0.0.1", far)])
    assert _serving("127.0.0.1", far)
    _stop(engine_runner, backend)
    assert not _serving("127.0.0.1", near), "主监听没关干净"
    assert not _serving("127.0.0.1", far), "额外监听没关干净（端口会占到进程结束）"


def test_a_busy_declared_endpoint_is_refused_and_nothing_stays_listening(engine_runner):
    near, far = free_port(), free_port()
    hog = socket.socket()
    hog.bind(("127.0.0.1", far))
    hog.listen(8)
    backend = ModbusTcpBackend(_channel(near), tick_ms=0,
                               extra_endpoints=[("127.0.0.1", far)])
    try:
        with pytest.raises(PortInUseError) as exc:
            engine_runner.call(lambda: backend.start(), 10.0, "start backend")
        assert str(far) in str(exc.value), exc.value
        assert not _serving("127.0.0.1", near), "起不来却把主监听留在地上"
    finally:
        hog.close()
        _stop(engine_runner, backend)


def test_an_address_that_is_not_on_this_machine_names_the_fix(engine_runner):
    """声明里有、机器上没有的地址（`net up` 还没跑）：报出地址本身并说清去哪配。"""
    near = free_port()
    backend = ModbusTcpBackend(
        _channel(near), tick_ms=0,
        extra_endpoints=[("192.0.2.123", free_port())])          # TEST-NET-1，必不在本机
    try:
        with pytest.raises(PortInUseError) as exc:
            engine_runner.call(lambda: backend.start(), 10.0, "start backend")
        message = str(exc.value)
        assert "192.0.2.123" in message and "net up" in message, message
        assert not _serving("127.0.0.1", near), "起不来却把主监听留在地上"
    finally:
        _stop(engine_runner, backend)


# ---------- 接线：起从站 / 起通道时把声明里的地址带上 ----------

def _project_file(tmp_path, near: int, far: int, declared: bool, name="t.json") -> pathlib.Path:
    """一份最小工程 + （可选）它旁边的声明：设备名与声明里的名字都叫 D。"""
    path = tmp_path / name
    path.write_text(json.dumps({
        # tick_ms 必须落在 validate 的 10..60000 里（0 是启动闸门拦下的非法值）；
        # 点位是空参数的 const，tick 起来也不会去写它，读回来还是初值。
        "version": 1, "tick_ms": 10,
        "channels": [{"name": "tcp-x", "type": "tcp", "host": "127.0.0.1", "port": near,
                      "devices": [{"name": "D", "unit_id": 11, "points": [
                          {"name": "值", "area": "hr", "address": 0,
                           "datatype": "uint16", "word_order": "ABCD", "initial": "7",
                           "waveform": "const", "wave_param": {}, "readonly": False}]}]}]}),
        encoding="utf-8")
    if declared:
        (tmp_path / "t.net.json").write_text(json.dumps(
            {"adapter_name": "simbench 模拟网卡",
             "devices": [{"name": "D", "ip": "127.0.0.1", "port": far}]},
            ensure_ascii=False), encoding="utf-8")
    return path


def test_a_started_channel_listens_on_the_declared_addresses(engine_runner, tmp_path):
    """界面那条路：`Session.start_channel` 也要把声明里的地址带上，状态栏说清连哪。"""
    near, far = free_port(), free_port()
    path = _project_file(tmp_path, near, far, declared=True)
    from simbench.csv_io_entry import load_point_table
    project, notes, issues = load_point_table(str(path))
    assert not [issue for issue in issues if issue.level == "error"], issues
    project.channels[0].port = near

    session = Session(runner=engine_runner)
    session.load(project, source=str(path))
    session.start_channel("tcp-x")
    try:
        assert session.channel("tcp-x").detail == f"127.0.0.1:{near}、127.0.0.1:{far}"
        with contextlib.closing(ModbusTcpClient("127.0.0.1", port=far)) as master:
            assert _read(master).registers == [7], "声明里那个地址没在听"
    finally:
        session.stop_channel("tcp-x")
    assert not _serving("127.0.0.1", far), "停通道没把额外监听收掉"


def test_a_started_channel_without_a_declaration_keeps_one_endpoint(engine_runner, tmp_path):
    near, far = free_port(), free_port()
    path = _project_file(tmp_path, near, far, declared=False)
    from simbench.csv_io_entry import load_point_table
    project, _notes, _issues = load_point_table(str(path))

    session = Session(runner=engine_runner)
    session.load(project, source=str(path))
    session.start_channel("tcp-x")
    try:
        assert session.channel("tcp-x").detail == f"127.0.0.1:{near}"
        assert not _serving("127.0.0.1", far)
    finally:
        session.stop_channel("tcp-x")


def test_the_command_line_slave_also_serves_the_declared_address(tmp_path):
    """命令行那条路（`python -m simbench x.json`）走的是另一个入口，单独钉一次。"""
    near, far = free_port(), free_port()
    path = _project_file(tmp_path, near, far, declared=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "simbench", str(path), "--host", "127.0.0.1",
         "--port", str(near), "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT))
    try:
        master = _wait_until_answering(near)
        try:
            assert _read(master).registers == [7]
        finally:
            master.close()
        with contextlib.closing(ModbusTcpClient("127.0.0.1", port=far)) as declared:
            assert _read(declared).registers == [7], "命令行起的从站没听声明里那个地址"
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        out, err = proc.communicate(timeout=10)
    assert f"127.0.0.1:{far}" in out, f"启动那行没报出该连哪：\n{out}\n{err}"
    assert not _serving("127.0.0.1", far)


def _wait_until_answering(port: int, timeout: float = 20.0) -> ModbusTcpClient:
    """主站读得通才算起来了：不猜延迟，也不看日志。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client = ModbusTcpClient("127.0.0.1", port=port)
        if client.connect():
            return client
        client.close()
        time.sleep(0.05)
    raise AssertionError(f"slave on 127.0.0.1:{port} never started")
