r"""冻结包冒烟：拿**真主站**去打 `dist/` 里的 exe（不是打源码）。

为什么要单独一份：`tests/` 那 1250 条用例跑的是源码，它们永远测不到"打包漏了一个 DLL"这类
只有冻结形态才出现的事。这个脚本是"打包之后到底还能不能用"的唯一证据，输出直接贴进
`packaging/SMOKE.md`。

跑的形态（默认 `dist/simbench/simbench.exe`，另加 `dist/simbench-onefile.exe`）：

1. `--check` / `--list` / 缺文件；
2. Excel 点表（`.xlsx`）读得进（**这条才验得到 `openpyxl` 真进包了**）；
2b. 主站模式（`--scan` / `--verify`：同一个 exe 一边当从站一边去读它）；
2c. 虚拟设备示例（`examples/virtual-device.csv`：Modbus Poll 的入口）；
3. TCP 从站 + pymodbus 真主站读/写；
4. RTU 从站（`socket://` 虚拟串口）+ pymodbus 真串口主站读/写；
5. S7 从站 + `pys7` 真主站读/写（**这条才验得到 `snap7.dll` 真进包了**）；
6. `net show`（只读那条）；
7. `simbench-gui.exe` 起得来、活着、收得掉。

用法（仓库根，用仓库的 venv 跑——主站库都在里面）：

    .\.venv\Scripts\python.exe packaging\smoke.py
    .\.venv\Scripts\python.exe packaging\smoke.py --dist dist\simbench --skip-gui
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import logging
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

TCP_TABLE = """channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
tcp-5020,COVI-1,11,回路温度,hr,0,float32,CDAB,12.5,const,,true
tcp-5020,COVI-1,11,风机状态,hr,2,uint16,ABCD,11,const,,false
"""

RTU_TABLE = """channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
com-485-1@socket://127.0.0.1:{port},COVI-1,11,风机转速,hr,0,uint16,ABCD,7,const,,false
"""

S7_TABLE = """channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
s7-1@127.0.0.1:{port} 0/1,D1,11,温度,db1,0,real,ABCD,12.5,const,,false
s7-1@127.0.0.1:{port} 0/1,D1,11,状态字,db1,4,word,ABCD,11,const,,false
s7-1@127.0.0.1:{port} 0/1,D1,11,启动,db1,6.3,bool,ABCD,true,const,,false
"""

# 主站模式那条用的表：通道名把"在哪儿"写全（`name@tcp:host:port`），
# 于是**同一个 exe** 一边当从站、一边当主站去扫它/核对它
MASTER_TABLE = """channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
tcp-1@tcp:127.0.0.1:{port},D1,11,温度,hr,0,uint16,ABCD,25,const,,false
tcp-1@tcp:127.0.0.1:{port},D2,12,别家,hr,0,uint16,ABCD,7,const,,false
"""

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""),
          flush=True)


def free_port() -> int:
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run(exe: pathlib.Path, *args, timeout: int = 90) -> subprocess.CompletedProcess:
    """跑一次 exe 并收完输出。冻结包是控制台程序，stdout 一律按 utf-8 解（`_reconfigure_streams`）。"""
    return subprocess.run([str(exe), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def spawn(exe: pathlib.Path, *args) -> subprocess.Popen:
    return subprocess.Popen([str(exe), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace")


def close(proc: subprocess.Popen) -> tuple[str, str]:
    """收掉一个后台子进程（**连子进程一起**）并把它的输出读干净。

    `taskkill /T` 这条是必须的：onefile 形态的 bootloader 会再起一个真身进程，只 terminate
    父进程会留下孤儿——实测连跑几次冒烟攒了 3 个 `simbench-onefile.exe`，它们占着 exe 文件
    （下一次打包直接 `PermissionError: WinError 5`）与临时端口。任何一步失败都不许把整个
    冒烟拖死，所以全部 suppress。
    """
    out = err = ""
    with contextlib.suppress(Exception):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=30)
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            out, err = proc.communicate(timeout=10)
    except Exception:
        pass
    return out or "", err or ""


def wait_for(predicate, timeout: float = 15.0, what: str = "") -> bool:
    """有界轮询：起监听是几百毫秒的事，不猜延迟、也不裸 sleep。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


# ---------- 命令行那三条 ----------

def smoke_cli(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    table = write(tmp / "tcp.csv", TCP_TABLE)

    proc = run(exe, str(table), "--check")
    record("--check 说点表合法", proc.returncode == 0 and "point table is valid" in proc.stdout,
           f"rc={proc.returncode}")

    proc = run(exe, str(table), "--list")
    record("--list 打出中文点位名", proc.returncode == 0 and "回路温度" in proc.stdout,
           f"rc={proc.returncode}")

    proc = run(exe, str(tmp / "nope.csv"), "--check")
    record("读不到文件报一行错且退出码 2",
           proc.returncode == 2 and "[error] [file]" in proc.stderr and "Traceback" not in proc.stderr,
           f"rc={proc.returncode}")

    proc = run(exe, "net", "show")
    record("net show（只读那一条）", proc.returncode == 0 and "ifIndex" in proc.stdout,
           f"rc={proc.returncode}")


# ---------- Excel 点表 ----------

def smoke_xlsx(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    """`.xlsx` 点表读得进——**这条才验得到 `openpyxl` 真进包了**。

    为什么非要单独一条：源码那上千条用例跑的是 venv 里的库（`import openpyxl` 一定成功），
    打包漏了它照样全绿；只有拿**冻结包**去读一个真 `.xlsx` 才证明它跟着进了包。
    表头上面故意压一行标题：顺手把"表头不在第一行"那条真路也走一遍（现场的表常这样）。
    """
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["某某隧道 点表 V1.2"])
    for row in csv.reader(TCP_TABLE.splitlines()):
        sheet.append(row)
    target = tmp / "points.xlsx"
    workbook.save(target)

    proc = run(exe, str(target), "--check", timeout=120)
    record("Excel 点表：冻结包读得进（openpyxl 真进包 + 表头在标题行之下也认）",
           proc.returncode == 0 and "point table is valid" in proc.stdout
           and "表头在第 2 行" in proc.stderr,
           f"rc={proc.returncode} {proc.stderr.strip()[-200:]}")


# ---------- 三条通道 + 三个真主站 ----------

def smoke_tcp(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    from pymodbus.client import ModbusTcpClient

    port = free_port()
    table = write(tmp / "tcp-listen.csv", TCP_TABLE)
    proc = spawn(exe, str(table), "--host", "127.0.0.1", "--port", str(port), "--quiet")
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=2.0)
    try:
        ok = wait_for(client.connect, 15, "tcp listener")
        if not ok:
            out, err = close(proc)
            record("TCP 从站起来并应答真主站", False, (err or out)[-300:])
            return
        # hr2 是 uint16 初值 11（单格好读）；hr0 是两个寄存器的 float32 CDAB，
        # 期望值就是 tests/test_cli.py 里钉死的那个字面量 [0x0000, 0x4148]。
        status = client.read_holding_registers(2, count=1, device_id=11).registers
        raw_float = client.read_holding_registers(0, count=2, device_id=11).registers
        client.write_register(2, 999, device_id=11)
        back = client.read_holding_registers(2, count=1, device_id=11).registers
        record("TCP：读初值 + 写进去能读回来（含 float32 CDAB 的字节序）",
               status == [11] and raw_float == [0x0000, 0x4148] and back == [999],
               f"hr2={status} float={raw_float} back={back}")
    finally:
        client.close()
        close(proc)


def smoke_rtu(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    from pymodbus import FramerType
    from pymodbus.client import ModbusSerialClient

    port = free_port()
    table = write(tmp / "rtu.csv", RTU_TABLE.format(port=port))
    proc = spawn(exe, str(table), "--quiet")
    client = ModbusSerialClient(port=f"socket://127.0.0.1:{port}", framer=FramerType.RTU,
                                baudrate=9600, timeout=2.0)
    try:
        ok = wait_for(client.connect, 15, "rtu listener")
        if not ok:
            out, err = close(proc)
            record("RTU 从站起来并应答真主站", False, (err or out)[-300:])
            return
        read = client.read_holding_registers(0, count=1, device_id=11).registers
        client.write_register(0, 4242, device_id=11)
        back = client.read_holding_registers(0, count=1, device_id=11).registers
        record("RTU（socket:// 虚拟串口）：读初值 + 写进去能读回来",
               read == [7] and back == [4242], f"read={read} back={back}")
    finally:
        client.close()
        close(proc)


def smoke_s7(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    from pyS7 import S7Client

    # 等监听起来的那几秒里，pys7 自己会把"连不上"当 INFO/ERROR 打到根 logger 上；
    # 那是**我们正等着**的正常中间态，不该混进这份记录里（`pyS7.client` 是它自己的名字）。
    logging.getLogger("pyS7").setLevel(logging.CRITICAL)

    port = free_port()
    table = write(tmp / "s7.csv", S7_TABLE.format(port=port))
    proc = spawn(exe, str(table), "--quiet")
    client = S7Client("127.0.0.1", rack=0, slot=1, port=port, timeout=3.0)
    try:
        # `pys7` 要先显式 connect（不像 pymodbus 的 client 会自己重连），所以把"连上 + 读到
        # 12.5"合成一个条件一起等——起监听是几百毫秒的事，不猜延迟。
        def readable() -> bool:
            if not client.is_connected:
                client.connect()
            return abs(client.read(["DB1,R0"])[0] - 12.5) < 1e-6

        ok = wait_for(readable, 20, "s7 listener")
        if not ok:
            out, err = close(proc)
            record("S7 从站起来并应答真主站", False, (err or out)[-300:])
            return
        gap = abs(client.read(["DB1,R0"])[0] - 12.5)
        words = client.read(["DB1,W4", "DB1,X6.3"])
        client.write(["DB1,W4"], [1234])
        back = client.read(["DB1,W4"])
        record("S7（snap7.dll 真进包）：读初值 + 写进去能读回来",
               gap < 1e-6 and words == [11, True] and back == [1234],
               f"|Δ|={gap} words={words} back={back}")
    finally:
        close(proc)


# ---------- 虚拟设备示例（Modbus Poll 的入口）----------

def smoke_virtual_device(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    """冻结包里的"虚拟设备"示例**能起、能用真主站读整块**，`--fill-gaps` 也在。

    Modbus Poll 是现场的尺子，它的起手动作就是"读 10 格"：示例点表因此是连续的。
    这一条把"示例能过校验 + 冻结包里读得到 10 格 + `--fill-gaps` 那个旗标进了包"钉住。
    """
    from pymodbus.client import ModbusTcpClient

    table = ROOT / "examples" / "virtual-device.csv"
    check = run(exe, str(table), "--check")
    if check.returncode != 0 or "point table is valid" not in check.stdout:
        record("虚拟设备示例：冻结包 --check 通过", False, check.stderr.strip()[-200:])
        return
    gap = run(exe, str(table), "--fill-gaps")
    record("虚拟设备示例：示例本来就是连续的（--fill-gaps 说「不用补」）",
           "没有空洞" in gap.stdout, gap.stdout.strip()[-120:])

    port = free_port()
    served = write(tmp / "virtual.csv",
                   table.read_text(encoding="utf-8").replace(
                       "channel,device", "channel,device"))
    proc = spawn(exe, str(served), "--host", "127.0.0.1", "--port", str(port), "--quiet")
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=2.0)
    try:
        if not wait_for(client.connect, 15, "virtual device listener"):
            out, err = close(proc)
            record("虚拟设备示例：冻结包里真主站读得到 10 格", False, (err or out)[-300:])
            return
        # Modbus Poll 起手那一下：读 10 个保持寄存器（示例里 hr 0..17 是连续的）
        first = client.read_holding_registers(0, count=10, device_id=11).registers
        second = client.read_holding_registers(4, count=1, device_id=11).registers
        record("虚拟设备示例：冻结包里真主站读得到 10 格（hr 0..9）",
               len(first) == 10 and second == [11], f"regs={first}")
    finally:
        client.close()
        close(proc)


# ---------- 主站模式 ----------

def smoke_master(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    """冻结包里的主站模式：**同一个 exe** 一边当从站，一边 `--scan` / `--verify` 去读它。

    这条同时验到三件事：`--scan` / `--verify` 两个旗标进了包、`simbench/master/` 那一包被
    打进去了（漏了的话 import 就炸）、报告能写到盘上。
    """
    port = free_port()
    table = write(tmp / "master.csv", MASTER_TABLE.format(port=port))
    slave = spawn(exe, str(table), "--quiet")
    try:
        if not wait_for(lambda: run(exe, str(table), "--scan", "--units", "10-13",
                                    "--host", "127.0.0.1").stdout.count("11") > 0,
                        timeout=30, what="master slave up"):
            out, err = close(slave)
            record("主站模式：冻结包扫得到自己起的那个从站", False, (err or out)[-300:])
            return
        report_path = tmp / "report.csv"
        verify = run(exe, str(table), "--verify", "--result", str(report_path))
        ok = (verify.returncode == 0 and report_path.exists()
              and "温度" in report_path.read_text(encoding="utf-8"))
        record("主站模式：--verify 在冻结包里核对并写出报告", ok,
               f"rc={verify.returncode} {verify.stdout.strip()[-160:]}")
    finally:
        close(slave)


# ---------- GUI ----------

def _gui_alive(gui: pathlib.Path, args: list[str], name: str) -> None:
    """起 8 秒再收：只要它没自己退出就算过。窗口内容（布局/DPI）在 M2 的人工清单里。"""
    proc = spawn(gui, *args)
    time.sleep(8)
    alive = proc.poll() is None
    out, err = close(proc)
    record(name, alive, (err or out)[-300:])


def smoke_gui(exe_dir: pathlib.Path, tmp: pathlib.Path) -> None:
    gui = exe_dir / "simbench-gui.exe"
    if not gui.exists():
        record("simbench-gui.exe 在产物里", False, str(gui))
        return
    # **不带参数**这一条才是关键：`entry.py` 靠"没有控制台"认出自己是界面那一个，
    # 而走命令行那条路会立刻以 "point table is required" 退出（rc=2）。所以"它没退"
    # 同时钉住了分派正确与 Qt 在冻结包里起得来——这不是"能启动就算过"的空检查。
    _gui_alive(gui, [], "simbench-gui.exe 不带参数就开界面（分派正确 + Qt 起得来）")
    # README 里写的那条用法：点表路径跟在后面
    _gui_alive(gui, [str(write(tmp / "gui.csv", TCP_TABLE))],
               "simbench-gui.exe 收点表参数")


def smoke_onefile(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    if not exe.exists():
        record("onefile 形态存在", False, str(exe))
        return
    started = time.monotonic()
    proc = run(exe, str(write(tmp / "of.csv", TCP_TABLE)), "--check", timeout=180)
    elapsed = time.monotonic() - started
    record(f"onefile：--check 通过（{elapsed:.1f}s 含解压）",
           proc.returncode == 0 and "point table is valid" in proc.stdout,
           f"rc={proc.returncode}")
    # 解压之后再打一次 S7：onefile 的 _MEIPASS 是临时目录，DLL 得跟着解压出来
    smoke_onefile_s7(exe, tmp)


def smoke_onefile_s7(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    from pyS7 import S7Client

    logging.getLogger("pyS7").setLevel(logging.CRITICAL)

    port = free_port()
    table = write(tmp / "of-s7.csv", S7_TABLE.format(port=port))
    proc = spawn(exe, str(table), "--quiet")
    client = S7Client("127.0.0.1", rack=0, slot=1, port=port, timeout=3.0)

    def readable() -> bool:
        if not client.is_connected:
            client.connect()
        return abs(client.read(["DB1,R0"])[0] - 12.5) < 1e-6

    try:
        ok = wait_for(readable, 30, "onefile s7")
        if not ok:
            out, err = close(proc)
            record("onefile：S7 读得到（DLL 从临时目录解压出来）", False, (err or out)[-300:])
            return
        record("onefile：S7 读得到（DLL 从临时目录解压出来）", True)
    finally:
        close(proc)


def smoke_mqtt(exe: pathlib.Path, tmp: pathlib.Path) -> None:
    """MQTT 通用模板：点表能解析，冻结包里的 paho 也真能走到连接那一步。"""
    table = ROOT / "examples" / "mqtt-template.csv"
    proc = run(exe, str(table), "--check")
    record("MQTT 示例：冻结包 --check 通过",
           proc.returncode == 0 and "point table is valid" in proc.stdout,
           (proc.stderr or proc.stdout)[-200:])
    # 没有 broker 时应在 5s 内给一句人话；关键是**不许**是 paho 没进包的 ImportError。
    proc = run(exe, str(table), "--scan", "--units", "1", timeout=30)
    text = (proc.stdout or "") + (proc.stderr or "")
    record("MQTT：paho 真进包（连接 broker 的路径走得通）",
           "mqtt" in text.lower() and "ModuleNotFoundError" not in text
           and "ImportError" not in text, text[-240:])


def main() -> int:
    # 控制台默认码页（cp936）打不出一些字符时的替换不应该污染这份记录，能设就设成 utf-8
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="smoke.py")
    parser.add_argument("--dist", default=str(ROOT / "dist" / "simbench"))
    parser.add_argument("--onefile", default=str(ROOT / "dist" / "simbench-onefile.exe"))
    parser.add_argument("--skip-gui", action="store_true")
    args = parser.parse_args()

    exe_dir = pathlib.Path(args.dist)
    exe = exe_dir / "simbench.exe"
    if not exe.exists():
        print(f"找不到 {exe}；先跑 packaging\\build.ps1", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="simbench-smoke-") as folder:
        tmp = pathlib.Path(folder)
        smoke_cli(exe, tmp)
        smoke_xlsx(exe, tmp)
        smoke_tcp(exe, tmp)
        smoke_rtu(exe, tmp)
        smoke_s7(exe, tmp)
        smoke_virtual_device(exe, tmp)
        smoke_master(exe, tmp)
        smoke_mqtt(exe, tmp)
        if not args.skip_gui:
            smoke_gui(exe_dir, tmp)
        smoke_onefile(pathlib.Path(args.onefile), tmp)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    for name in failed:
        print(f"  未通过：{name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())