"""命令行起从站：python -m modslave examples/covi-tunnel.csv --port 5020

这是 M1 的验收入口，也是"没有 GUI 也能在 CI 里跑一遍"的最小外壳。
本文件不 import pymodbus：TCP 后端住在 backend/，分层由 tests/test_architecture.py 守。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from .backend.modbus_tcp import ModbusTcpBackend, PortInUseError
from .csv_io_entry import load_point_table
from .project.validate import error_lines, validate_project


def _reconfigure_streams() -> None:
    """Windows 控制台默认码页（cp936/cp437）会把中文打成乱码，入口自己兜住。

    点位名、设备名、报错消息都是中文，乱码的报错等于没有报错。只改**本进程**这两个
    流的编码，不去动用户的全局环境变量（PYTHONIOENCODING / chcp）——那是别人的机器。
    `errors="backslashreplace"` 是第二道保险：真遇到编不出的字符也不许在打印路径上崩。

    reconfigure 不是所有句柄都有或被允许（被重定向、被 pytest 接管、已关闭的管道），
    失败就退化用原句柄继续跑：CLI 宁可输出难看一点，也不能因为设置编码而启动失败。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _units(channel) -> str:
    return ", ".join(str(dev.unit_id) for dev in channel.devices)


def _line(direction: str, raw: bytes, unit, function, summary: str, mono: float) -> None:
    """一帧一行：相对时间戳 + RX/TX + unit/fc/长度/hex（异常响应由 summary 说清）。"""
    print(f"{mono:8.3f} {direction.upper()} {summary} | {raw.hex(' ')}", flush=True)


def _print_table(channel) -> None:
    print(f"channel {channel.name} ({channel.host}:{channel.port})")
    for dev in channel.devices:
        print(f"  device {dev.name} (unit {dev.unit_id})")
        for pt in dev.points:
            print(f"    {pt.area:<3} {pt.address:<5} {pt.datatype:<8} "
                  f"{pt.word_order:<5} {str(pt.initial):<12} "
                  f"{'RO' if pt.readonly else '  '} {pt.name}")


def _report(notes, issues) -> list[str]:
    """note/warning 进 stderr 不拦启动，error 也打在这里，返回拦住启动的那几条。"""
    for note in notes:
        print(f"[note] {note}", file=sys.stderr)
    for issue in issues:
        print(f"[{issue.level}] {issue}", file=sys.stderr)
    return error_lines(issues)


def _apply_overrides(project, host: str | None, port: int | None) -> bool:
    """`--host`/`--port` 覆盖所有通道；给了参数就返回 True，调用方据此重跑校验。

    判据用 `is not None`，不用真值：`--port 0`、`--host ""` 都是非法值，必须原样落到
    validate 里变成 error，而不是在这里被当成"没填"静默忽略（同裁决 A/B：宁可报错）。
    """
    if host is None and port is None:
        return False
    for channel in project.channels:
        if host is not None:
            channel.host = host
        if port is not None:
            channel.port = port
    return True


async def _serve(channel, quiet: bool) -> None:
    backend = ModbusTcpBackend(channel, packet_sink=None if quiet else _line)
    await backend.start()
    print(f"[modslave] listening on {channel.host}:{backend.port} "
          f"unit ids {_units(channel)}", flush=True)
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        # Windows 上 add_signal_handler 不可用，Ctrl+C 走 KeyboardInterrupt（见 main）
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    try:
        await stop.wait()
    finally:
        # stop() 是协程，只能在还活着的 loop 上调。Ctrl+C 时 asyncio 先取消本任务、收尾的
        # 那轮 loop 再把 finally 跑完，监听套接字因此真被关掉，下一次启动不撞端口——这条
        # 由 tests/test_cli.py::test_cancelling_the_serve_task_releases_the_port 钉住
        # （同一个动作：任务被取消 + loop 还活着）。不指望进程退出顺手替我们关。
        await backend.stop()


def main(argv: list[str] | None = None) -> int:
    _reconfigure_streams()
    parser = argparse.ArgumentParser(prog="python -m modslave")
    parser.add_argument("point_table", help="CSV 或 project.json")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check", action="store_true", help="只校验，不监听")
    parser.add_argument("--quiet", action="store_true", help="不打印报文")
    parser.add_argument("--list", action="store_true", help="打印点表后退出")
    args = parser.parse_args(argv)

    try:
        project, notes, issues = load_point_table(args.point_table)
    except OSError as exc:
        # 文件读不到是操作错误：一句带路径的人话就够，不要甩一屏 traceback
        detail = exc.strerror or exc
        print(f"[error] [file] {args.point_table}: cannot read point table: {detail}",
              file=sys.stderr)
        return 2

    # 命令行覆盖排在校验之前一步：点表自己合法不代表 `--port 0`、`--host ""` 合法，
    # 而"绝不带着非法配置起监听"这条闸门不能因为参数是命令行给的就绕过去
    if not error_lines(issues) and _apply_overrides(project, args.host, args.port):
        issues = validate_project(project)

    errors = _report(notes, issues)
    if errors:
        print(f"[modslave] {len(errors)} error(s); refusing to start", file=sys.stderr)
        return 2
    if not project.channels:
        print("[modslave] the point table has no channel; check its first column",
              file=sys.stderr)
        return 2

    if args.list:
        for channel in project.channels:
            _print_table(channel)
        return 0
    if args.check:
        print("[modslave] point table is valid")
        return 0
    if len(project.channels) > 1:
        print("[modslave] M1 serves one channel at a time; using "
              f"{project.channels[0].name}", file=sys.stderr)

    try:
        asyncio.run(_serve(project.channels[0], args.quiet))
    except KeyboardInterrupt:
        print("\n[modslave] stopped", flush=True)
    except PortInUseError as exc:
        # 撞端口按一行错误报，不甩 traceback（和读不到文件同一个口径）：现场第一件事就是
        # "再起一次"，而这里静默成功比报错更糟——两个监听叠在一起，主站继续读旧点表。
        print(f"[error] [port] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
