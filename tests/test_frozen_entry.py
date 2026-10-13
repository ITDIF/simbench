"""冻结（PyInstaller 打包）之后最容易碎的两处：提权重启自己、GUI 入口的 argv 形状。

这两条都属于"源码里永远绿、打包后必碎"的形状，所以按**形态**钉，而不是等真打包才发现：

- 提权重跑自己那条路，源码里是 `python -m simbench net …`，冻结后 `sys.executable` 就是
  exe 本身，`-m simbench` 一个字都不成立。判据只有 `sys.frozen` 一个，这里 monkeypatch
  它 + 假的 `subprocess.run` 抓出真被拼进 `Start-Process` 的那串参数；
- `simbench-gui.exe`（无控制台）双击要开界面，但敲了 `--check`/`--list`/`net` 时不许
  拿一个空窗口把结果吃掉。`gui_main` 只负责摆 argv，所以这里把 `main` 换成一个收集器，
  不建 Qt、不开端口。

真打包产物（`dist/`）上的冒烟在 `packaging/SMOKE.md` 里，这一段只守"命令怎么拼"。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

from simbench import __main__ as cli
from simbench.net import cli as net_cli

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")


# ---- 提权重跑自己：命令行前缀按运行形态分派 ----

def test_source_run_relaunches_through_the_interpreter():
    """源码运行：`<python> -m simbench net`。这条是既有行为的回归钉。"""
    assert net_cli._self_command_prefix() == [sys.executable, "-m", "simbench", "net"]


def test_frozen_run_relaunches_the_exe_directly(monkeypatch):
    """冻结运行：`<exe> net`（不带 `-m simbench`，冻结包里没有 `-m` 这条路）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\apps\simbench\simbench.exe")
    assert net_cli._self_command_prefix() == [r"C:\apps\simbench\simbench.exe", "net"]


def _argument_list(script: str) -> list[str]:
    """从 PowerShell 脚本里把 `-ArgumentList @(...)` 那段拆回来（逐项去引号）。"""
    body = script.split("-ArgumentList @(", 1)[1].split("); exit", 1)[0]
    return [part.strip().strip("'") for part in body.split(",")]


def _capture_relaunch(monkeypatch, tmp_path, frozen: bool):
    """跑一次 `_run_elevated`，把 `Start-Process` 的脚本与拼出的参数抓回来。

    `subprocess.run` 被换成一个"记下来 + 写回结果文件"的假货：真的那一条会弹 UAC，
    不能在用例里跑；结果文件必须写，否则 `_run_elevated` 会走"提权进程没有返回结果"。
    """
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", r"C:\apps\simbench\simbench.exe")
    else:
        monkeypatch.setattr(sys, "frozen", False, raising=False)

    result_path = tmp_path / "result.json"
    seen: dict[str, object] = {}

    def fake_run(argv, capture_output=True, timeout=None):
        seen["argv"] = list(argv)
        script = argv[-1]
        seen["script"] = script
        result_path.write_text(json.dumps({"ok": True, "message": "done"}),
                               encoding="utf-8")

        class _Proc:
            returncode = 0
            stdout = b""
            stderr = b""

        return _Proc()

    monkeypatch.setattr(net_cli.subprocess, "run", fake_run)

    class _Args:
        result = str(result_path)

    payload = net_cli._run_elevated(_Args(), ["device", "create"], {"action": "create"})
    assert payload["ok"] is True
    return seen["script"], seen["argv"]


def test_relaunch_argv_when_running_from_source(monkeypatch, tmp_path):
    script, argv = _capture_relaunch(monkeypatch, tmp_path, frozen=False)
    job = str(pathlib.Path(tempfile.gettempdir()) / "simbench_net_job.json")
    assert _argument_list(script) == ["-m", "simbench", "net", "device", "create",
                                      "--elevated", "--job", job]
    assert argv[:1] == ["powershell"]
    assert f"-FilePath '{sys.executable}'" in script


def test_relaunch_argv_when_frozen_drops_the_module_flag(monkeypatch, tmp_path):
    """冻结后 `-m simbench` 必须消失，`net` 直接跟在 exe 后面。"""
    script, _ = _capture_relaunch(monkeypatch, tmp_path, frozen=True)
    job = str(pathlib.Path(tempfile.gettempdir()) / "simbench_net_job.json")
    assert _argument_list(script) == ["net", "device", "create", "--elevated", "--job", job]
    assert "-m" not in script
    assert r"-FilePath 'C:\apps\simbench\simbench.exe'" in script


# ---- GUI 入口：只摆 argv，不建窗口 ----

def _collect(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: (seen.append(argv), 0)[1])
    return seen


def test_gui_entry_opens_the_window_when_nothing_else_is_asked(monkeypatch):
    seen = _collect(monkeypatch)
    assert cli.gui_main([]) == 0
    assert seen == [["--gui"]]


def test_gui_entry_keeps_a_point_table_argument(monkeypatch):
    seen = _collect(monkeypatch)
    assert cli.gui_main(["examples/covi-tunnel.csv"]) == 0
    assert seen == [["--gui", "examples/covi-tunnel.csv"]]


def test_gui_entry_does_not_double_the_gui_flag(monkeypatch):
    seen = _collect(monkeypatch)
    assert cli.gui_main(["--gui", "x.csv"]) == 0
    assert seen == [["--gui", "x.csv"]]


def test_gui_entry_lets_validation_and_listing_win(monkeypatch):
    """敲了 `--check`/`--list`/`net` 就是命令行那件事，不许被自动补成开界面。"""
    seen = _collect(monkeypatch)
    assert cli.gui_main(["--check", "x.csv"]) == 0
    assert cli.gui_main(["--list", "x.csv"]) == 0
    assert cli.gui_main(["net", "show"]) == 0
    assert seen == [["--check", "x.csv"], ["--list", "x.csv"], ["net", "show"]]


def test_gui_entry_really_runs_check_on_a_real_table(capsys):
    """走真 `main` 一次：`--check` 那条路不建 Qt 也不开端口，结果必须在 stdout 上。"""
    assert cli.gui_main(["--check", CSV]) == 0
    assert "point table is valid" in capsys.readouterr().out