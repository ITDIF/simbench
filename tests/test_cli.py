"""命令行入口：装载点表 -> 一次报全 -> 起 TCP 从站给真主站读。

期望值全部来自 spec §5/§6 与计划开头「已实测事实」，不是从实现倒推：
CDAB 浮点的寄存器序列在 `tests/test_integration_tcp.py` 里已经钉成 `[0x0000, 0x4148]`，
本文件再按字面量钉一次，免得 codec 改动把这条用例变成自证。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from modslave import __main__ as cli
from modslave.backend.modbus_tcp import ModbusTcpBackend, PortInUseError
from modslave.codec.point_codec import encode
from modslave.csv_io_entry import load_point_table
from modslave.project.validate import error_lines
from tests.conftest import free_port

# 从仓库根取示例点表：不依赖 pytest 的 cwd（在 IDE 里单跑一个文件时也一样成立）
ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")
CO_CDAB = [0x0000, 0x4148]              # encode(12.5, "float32", "CDAB")，字面量钉住
HEADER = ("channel,device,unit_id,name,area,address,datatype,word_order,initial,"
          "waveform,wave_param,readonly\n")


def run_cli(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    """跑一次 `python -m modslave ...`。stdout/stderr 一律按 utf-8 解（见中文那条用例）。"""
    return subprocess.run([sys.executable, "-m", "modslave", *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(ROOT), timeout=timeout)


def start_server(port: int):
    """真起一个 CLI 子进程（`--quiet`）；起来没有只靠"主站读得通"判，不猜延迟。"""
    return subprocess.Popen(
        [sys.executable, "-m", "modslave", CSV, "--host", "127.0.0.1",
         "--port", str(port), "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT))


def drain(proc) -> tuple[str, str]:
    """把管道读干净。子进程没死透就 kill 兜底——任何情况下都不许把整个套件挂住。"""
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            out, err = proc.communicate(timeout=10)
    return out or "", err or ""


def terminate(proc) -> tuple[str, str]:
    """terminate -> wait -> kill -> wait，四条路都封死，不留孤儿进程。"""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
    return drain(proc)


def master_still_answering(port: int) -> bool:
    """这个端口上还有没有从站在**对外应答**——判据是一次完整报文往返。

    实测：子进程被硬杀之后，这个端口上的 connect 先成功一次、随后一直挂到超时或回
    10054（RST），并不是干净的"拒绝"。所以 `socket.connect_ex()` 与"能不能连上"两种
    写法都会把"已释放"判错（`connect_ex` 配 settimeout 更是恒返回 10035，永远"已释放"，
    用例直接假绿）。只有读得通才算还在服务。
    """
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=0.5)
    read = None
    try:
        if client.connect():
            read = client.read_holding_registers(0, count=1, device_id=11)
    except Exception:                                       # noqa: BLE001
        read = None
    finally:
        with contextlib.suppress(Exception):
            client.close()
    return read is not None and not read.isError()


def wait_until_answering(proc, port: int,
                         timeout: float = 25.0) -> ModbusTcpClient:
    """重试到"真主站读得通"，返回已连接的 client；子进程中途死了就直接失败。

    失败消息带上子进程自己的输出：起不来通常是 bind 或点表问题，光看"连不上"没法定位。
    """
    deadline = time.monotonic() + timeout
    last = "never attempted"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = drain(proc)
            raise AssertionError(
                f"modslave exited early with rc={proc.returncode}\n"
                f"--- stdout ---\n{out}\n--- stderr ---\n{err}")
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=1.0)
        try:
            if client.connect():
                read = client.read_holding_registers(0, count=2, device_id=11)
                if read is not None and not read.isError():
                    return client                 # 交给调用点，由它 close
                last = f"connect ok, read -> {read}"
            else:
                last = "connect() returned False"
        except Exception as exc:                                    # noqa: BLE001
            last = repr(exc)
        with contextlib.suppress(Exception):
            client.close()
        time.sleep(0.1)
    raise AssertionError(f":{port} never answered a master within {timeout}s ({last})")


async def wait_for_listener(port: int, task, attempts: int = 400) -> None:
    """loop 线程里等监听起来：只能用 `asyncio.open_connection`，阻塞 socket 会把被测的
    `_serve` 自己的 loop 挡住（这条用例测的就是这个 loop）。
    """
    for _ in range(attempts):
        if task.done():
            task.result()                # 起服务自己就炸了：抛真实异常，不干等 20 秒
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise AssertionError(f":{port} never came up")


# ---- 装载口 ----

def test_load_point_table_from_example_csv():
    project, notes, issues = load_point_table(CSV, port_overrides={"tcp-5020": 0})
    assert project.channels[0].devices[0].unit_id == 11
    assert len(project.channels[0].devices) == 2
    assert {dev.unit_id for dev in project.channels[0].devices} == {11, 12}
    assert error_lines(issues) == []
    assert any("port" in n for n in notes)      # 端口是从通道名猜出来的，必须提醒


def test_bad_csv_is_reported_with_line_number(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(HEADER +
                    "tcp-1,D,11,x,hr,notanumber,uint16,ABCD,0,const,,false\n",
                    encoding="utf-8")
    _proj, _notes, issues = load_point_table(str(path), port_overrides={"tcp-1": 5020})
    assert any("line 2" in str(i) for i in issues)


def test_dirty_project_json_is_a_parse_issue_not_a_traceback(tmp_path):
    """project.json 读不懂也要走 parse Issue：装载口只 `except ValueError` 是仓库契约。"""
    path = tmp_path / "dirty.json"
    path.write_text(json.dumps({"version": 1, "tick_ms": 100, "channels": [
        {"name": "tcp-1", "host": "0.0.0.0", "port": 5020, "devices": [
            {"name": "D", "unit_id": 11,
             "points": [{"name": "x", "area": "hr", "address": 0}]}]}]}),
        encoding="utf-8")
    project, _notes, issues = load_point_table(str(path))
    assert project.channels == []                       # 空工程占位，下游不必判 None
    assert [i.code for i in issues] == ["parse"]
    assert error_lines(issues)

    done = run_cli(str(path), "--check")
    assert done.returncode == 2
    assert "[parse]" in done.stderr
    assert "Traceback" not in done.stderr


# ---- 命令行 ----

def test_module_runs_and_reports_validation_errors(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(HEADER +
                    "tcp-1,D,300,x,hr,0,uint16,ABCD,0,const,,false\n", encoding="utf-8")
    done = run_cli(str(path), "--check")
    assert done.returncode == 2
    assert "unit_range" in done.stdout or "unit_range" in done.stderr
    assert "Traceback" not in done.stderr


def test_missing_file_is_one_clean_error_line(tmp_path):
    done = run_cli(str(tmp_path / "nope.csv"))
    assert done.returncode == 2
    assert "nope.csv" in done.stderr
    assert "Traceback" not in done.stderr


def test_list_prints_every_unit_id():
    done = run_cli(CSV, "--list")
    assert done.returncode == 0
    assert "unit 11" in done.stdout and "unit 12" in done.stdout


def test_check_passes_on_the_example_point_table():
    done = run_cli(CSV, "--check")
    assert done.returncode == 0
    assert "valid" in done.stdout


def test_chinese_point_names_survive_the_windows_codepage():
    """点位名是中文：控制台默认码页（cp936/cp437）会把它们打成乱码，入口必须自己兜住。

    子进程的 stdout 是管道不是 console，正是 `reconfigure` 会走到的那条分支——
    解不出来就不能算过，所以这里固定按 utf-8 解码并断言原文出现。
    """
    done = run_cli(CSV, "--list")
    assert done.returncode == 0
    assert done.stdout.strip() != ""
    assert "CO浓度" in done.stdout and "累计电量" in done.stdout
    assert "Traceback" not in done.stderr


def test_cli_serves_a_real_master_read():
    """命令行真起一个从站，用同步 client 当第三方主站读一次 CDAB 浮点。

    终止走 terminate（硬停），所以这里测的是"端口随进程释放"：老端口不再对外应答，
    而且**同一端口能再起一次并读出同一个值**——那是下一次启动会不会撞端口的真判据。
    Ctrl+C 那条 stop() 走完的路径由 `test_cancelling_the_serve_task_releases_the_port`
    钉住，人手按一次 Ctrl+C 仍留在 Task 12 Step 8 的人工冒烟里。
    """
    port = free_port()
    proc = start_server(port)
    try:
        client = wait_until_answering(proc, port)
        try:
            read = client.read_holding_registers(0, count=2, device_id=11)
        finally:
            client.close()
        assert not read.isError(), f"read failed: {read}"
        assert read.registers == CO_CDAB
        assert read.registers == encode(12.5, "float32", "CDAB")
    finally:
        output = terminate(proc)
    assert not master_still_answering(port), (
        f"port {port} is still being served after the process exited\n"
        f"--- stdout ---\n{output[0]}\n--- stderr ---\n{output[1]}")

    second = start_server(port)
    try:
        client = wait_until_answering(second, port)
        try:
            again = client.read_holding_registers(0, count=2, device_id=11)
        finally:
            client.close()
        assert again.registers == CO_CDAB, f"port {port} could not serve a second time"
    finally:
        terminate(second)


def test_cancelling_the_serve_task_releases_the_port():
    """Ctrl+C 的实质是"loop 还活着时 main_task.cancel()"，这条把那个动作钉成用例。

    真控制台的 CTRL_C_EVENT 在本机这种环境里送不进去（实测：`os.kill(pid, CTRL_C_EVENT)`
    调用成功但子进程毫无反应；换 CTRL_BREAK_EVENT 则进程按 0xC000013A 直接结束，Python 的
    finally 一次都没跑），所以"人手按一次 Ctrl+C"留在 Task 12 Step 8 的 QModMaster 冒烟里。
    本条覆盖 asyncio 收到 SIGINT 之后的同一段代码：任务被取消 -> `_serve` 的 finally 在
    还活着的 loop 上把 backend.stop() 走完 -> 端口不再对外应答 -> 同端口能再起第二次。
    留半个没关的 listener 在这里的表现就是第二次 start 炸 bind。
    """
    port = free_port()
    project, _notes, issues = load_point_table(CSV, port_overrides={"tcp-5020": port})
    assert error_lines(issues) == []
    channel = project.channels[0]
    channel.host = "127.0.0.1"
    assert channel.port == port                   # 覆盖是真的落到了通道上

    async def scenario():
        task = asyncio.create_task(cli._serve(channel, quiet=True))
        await wait_for_listener(port, task)
        task.cancel()                             # 与 SIGINT 处理函数同一个动作
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not master_still_answering(port), "backend.stop() 没走完：监听还留在这个进程里"

    async def rebind():
        backend = ModbusTcpBackend(channel)
        await backend.start()                     # 端口没真放开就在这里炸
        try:
            return backend.read_registers(11, "hr", 0, 2)
        finally:
            await backend.stop()

    assert asyncio.run(rebind()) == CO_CDAB       # 同端口确实起得来，值也还在


def test_second_backend_on_the_same_port_is_refused():
    """同一 `(host, port)` 上第二个实例必须**起不来**，不是"两个都成功"。

    上面那条判据在这里帮不上忙：它测的是"第一个已经停了"，而现场的故障是第一个还活着
    （改了点表再起一次，老进程没退干净）。实测 pymodbus 允许两个 server 绑同一端口都
    "启动成功"、netstat 叠出 3 个 LISTENING、主站 30 次读全拿到旧点表的值——所以这条
    用可失败的断言钉住：第二个 `start()` 抛端口占用类错误，端口号必须在消息里；而且
    第一个 `stop()` 之后第二个确实起来、读到的是**新**值。
    """
    port = free_port()
    project, _notes, issues = load_point_table(CSV, port_overrides={"tcp-5020": port})
    assert error_lines(issues) == []
    channel = project.channels[0]
    channel.host = "127.0.0.1"
    point = channel.devices[0].points[0]
    assert (point.area, point.address, point.datatype) == ("hr", 0, "float32")
    assert point.initial == "12.5"

    zombie = ModbusTcpBackend(channel)            # A：还挂着上一版点表
    fresh = ModbusTcpBackend(channel)             # B：模拟"改了点表再启动一次"

    async def scenario():
        await zombie.start()
        point.initial = "99.5"                    # 改点表：CDAB 浮点两个寄存器都会变
        old = zombie.read_registers(11, "hr", 0, 2)
        with pytest.raises(PortInUseError) as excinfo:
            await fresh.start()                   # 同一个端口：必须起不来
        refused = str(port) in str(excinfo.value)
        left_clean = fresh._server is None
        await zombie.stop()
        await fresh.start()                       # 端口真放开后第二个才允许起来
        new = fresh.read_registers(11, "hr", 0, 2)
        await fresh.stop()
        return old, refused, left_clean, new

    # 全程同一个 loop：backend 的 server 属于起它的那个 loop，跨 asyncio.run 复用会把
    # stop() 打到已经关掉的 loop 上（实测 AttributeError: '_stop_serving'）。
    old, refused, left_clean, new = asyncio.run(scenario())
    assert refused, "端口号没出现在错误消息里，现场对不上是哪个端口"
    assert left_clean, "起不来的那个实例留下了半个后端"
    assert old == CO_CDAB                         # A 一直在按旧点表应答
    assert new == encode(99.5, "float32", "CDAB")  # B 起来后读到的是新点表


def test_cli_refuses_a_port_that_is_already_being_served():
    """命令行撞端口是一行错误、不是 traceback：现场先杀僵尸进程才有线索。

    这条判据在加探针之前**永远不会失败**——第二次启动是"成功"的，然后两个监听叠在一起、
    主站继续读旧点表，日志里两边都写着 listening on 同一个端口。
    """
    port = free_port()
    proc = start_server(port)
    try:
        client = wait_until_answering(proc, port)
        client.close()
        second = run_cli(CSV, "--host", "127.0.0.1", "--port", str(port), "--quiet")
    finally:
        terminate(proc)
    assert second.returncode == 2, (second.stdout, second.stderr)
    assert "[error]" in second.stderr and str(port) in second.stderr, second.stderr
    assert "Traceback" not in second.stderr, second.stderr
