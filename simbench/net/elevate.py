"""用管理员身份重跑自己（UAC 提权）的**唯一**一处编排。

为什么单独一个文件（2026-10-14 深度审查 O10）：这一段是 `net/cli.py` 里**唯一**能脱离真 UAC
单测的部分（`tests/test_frozen_entry.py` 用假 `sys.frozen` 钉住两种形态），却和 argparse 定义、
四个子命令的实现挤在同一个近 700 行的文件里。抽出来之后：

* `cli.py` 只管"解析 + 分发 + 打印"；
* 这一层只管"怎么把自己以管理员身份叫起来、结果怎么拿回来"；
* 想验它的人不用 import 整个 CLI。

三种形态的口径（都写在下面各自的函数里）：源码运行、冻结运行（onedir / onefile）、
以及"参数走任务文件而不是命令行"（网卡名里带空格，命令行拼串会被拆成两个参数）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import NetError


def _self_command_prefix() -> list[str]:
    """提权重跑自己时的那条命令：`[0]` 是程序（`-FilePath`），后面是它的 argv 前缀。

    两种形态都得能重启自己，判据只有 `sys.frozen` 一个：

    - **源码运行**：`[0]` 是解释器，argv 是 `-m simbench net …`；
    - **冻结运行**（PyInstaller 的 onedir / onefile 都置 `sys.frozen`）：`[0]` 就是本程序，
      `-m simbench` 一个字都不成立（`simbench.exe -m simbench net` 会被 argparse 当成
      未知参数拒掉），所以 argv 直接从 `net` 起。

    这条不是"猜冻结会怎样"：原来的 `-FilePath '{sys.executable}'` + `-m simbench net` 在
    源码里成立，冻结后必碎，把两种形态收在这一个函数里才只有一处要改。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "net"]
    return [sys.executable, "-m", "simbench", "net"]


def _run_elevated(args, cmd: str | list[str], job: dict) -> dict:
    """用管理员身份重跑自己。UAC 由用户确认；结果经 JSON 文件回传。

    参数走**任务文件**而不是命令行：`Start-Process -ArgumentList` 是用空格把数组拼成
    一条命令行的，参数里带空格（网卡名"simbench 模拟网卡"就有）会被拆成两个参数，
    子进程 argparse 直接报错退出 2——实测踩过。写 JSON 文件就把引号问题整个绕开了。
    """
    # 临时文件的名字里带 **pid**：固定名字（`simbench_net_job.json`）在两个实例同时用时会互相
    # 覆盖，而"读到上一次剩下的结果"会让人以为这次提权成功了。结果文件默认也放临时目录——
    # 原来默认落在**当前工作目录**（`Path.cwd()/_simbench_net_result.json`），那会在别人的目录里
    # 留下一个没人认领的文件。
    stem = f"simbench_net_{os.getpid()}"
    result_path = (Path(args.result) if args.result
                   else Path(tempfile.gettempdir()) / f"{stem}_result.json")
    log_path = Path(tempfile.gettempdir()) / f"{stem}_elevated.log"
    for stale in (result_path, log_path):
        if stale.exists():
            stale.unlink()

    job_path = Path(tempfile.gettempdir()) / f"{stem}_job.json"
    # 子命令是**一段**（`up`）还是**两段**（`device create`）由调用方给：两段那种必须原样
    # 出现在子进程的命令行上（否则 argparse 认不出 device 下面的动作），而任务文件里的
    # `cmd` 只能记第一段，重放时才落得回 `_cmd_device` 那条分发上。
    parts = [cmd] if isinstance(cmd, str) else list(cmd)
    job.update({"cmd": parts[0], "result": str(result_path), "log": str(log_path)})
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")

    # 谁来跑、怎么把 `net` 叫起来都由这一个函数给（源码 vs 冻结两种形态，见它的说明）：
    # `command[0]` 是 `-FilePath`，其余才是子进程的 argv——`sys.executable` 只出现一次。
    command = _self_command_prefix()
    passthrough = [*command[1:], *parts, "--elevated", "--job", str(job_path)]
    quoted = ",".join("'" + a.replace("'", "''") + "'" for a in passthrough)
    script = (f"$p = Start-Process -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
              f"-WorkingDirectory '{Path.cwd()}' "
              f"-FilePath '{command[0]}' -ArgumentList @({quoted}); exit $p.ExitCode")
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, timeout=600)

    if not result_path.exists():
        detail = ""
        if log_path.exists():
            detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
        detail = detail or (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise NetError(f"提权进程没有返回结果（退出码 {proc.returncode}）"
                       + (f"：{detail[-400:]}" if detail else "；可能是 UAC 被取消"))
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not payload.get("ok", False):
        raise NetError(payload.get("error") or "提权进程报告失败")
    return payload
