"""命令行起从站：python -m simbench examples/covi-tunnel.csv --port 5020

这是 M1 的验收入口，也是"没有 GUI 也能在 CI 里跑一遍"的最小外壳。
本文件不 import pymodbus：TCP 后端住在 backend/，分层由 tests/test_architecture.py 守。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys

from .backend import BackendStartError, UnsupportedTransportError, make_backend
from .csv_io_entry import load_point_table
from .project.model import channel_transport, s7_summary, serial_summary
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
    transport = channel_transport(channel)
    if transport == "rtu":
        print(f"channel {channel.name} ({serial_summary(channel)})")
    elif transport == "s7":
        print(f"channel {channel.name} ({s7_summary(channel)})")
    else:
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


def _runtime_log(level: str, text: str) -> None:
    """运行期报错（波形算不出/钳位）一帧一行进 stderr，与 `_report` 同句式。

    命令行没有日志窗，这里不发出去操作员就只剩"曲线不动"一个现象可看；而 `LogDedupe`
    已经保证同点同类 60s 一条，刷屏的问题不归这一层。不受 `--quiet` 管：那是报文开关，
    报错不是报文。
    """
    print(f"[{level}] {text}", file=sys.stderr, flush=True)


def _prepare_network(point_table: str | None, project=None) -> None:
    """起监听之前按点表旁边的网络声明把网卡对一遍（口径见 `net/startup.py`）。

    只报话，**不改退出码**：网卡没配好和点表不合法不是一个量级的事，前者是现场看得见的
    现象（主站连不上），后者才该拒绝启动。没有声明文件时这里一声不吭。
    """
    if not point_table:
        return
    from .net.startup import prepare     # 延迟 import：`net` 那条子命令自己不碰点表解析

    # `project` 一起交下去：自动配网按通道类型过滤（纯串口工程不配设备 IP，
    # 见 `net/startup._tcp_endpoints`）。
    for line in prepare(point_table, project=project):
        print(f"[net] {line}", file=sys.stderr, flush=True)


def _listen_endpoints(channel, point_table) -> list[tuple[str, int]]:
    """这一路通道要**额外**监听哪些地址（口径见 `net/endpoints.py`）。

    只读、不碰系统：声明里说设备在哪个地址，主站就会往那儿连，所以那些地址也要听上。
    """
    from .net.endpoints import for_channel

    return for_channel(channel, point_table)


def _apply_overrides(project, host: str | None, port: int | None) -> bool:
    """`--host`/`--port` 覆盖**TCP 通道**；真盖上了一路就返回 True，调用方据此重跑校验。

    判据用 `is not None`，不用真值：`--port 0`、`--host ""` 都是非法值，必须原样落到
    validate 里变成 error，而不是在这里被当成"没填"静默忽略（同裁决 A/B：宁可报错）。

    串口通道不吃这两个参数（它没有 host，`port` 是串口名）：一路都没盖上就返回 False，
    调用方负责说一句"这两个参数对这份点表没作用"——静默不生效等于骗人。
    """
    if host is None and port is None:
        return False
    applied = False
    for channel in project.channels:
        if channel_transport(channel) != "tcp":
            continue
        if host is not None:
            channel.host = host
        if port is not None:
            channel.port = port
        applied = True
    return applied


async def _serve(channel, quiet: bool, tick_ms: int, extra_endpoints=()) -> None:
    """起一路无头从站。`tick_ms > 0` 时波形按节拍自己动（M4）——命令行与 GUI 起的是同一个
    工程，读到同一批点表，就不该一个有曲线一个没有。

    `tick_ms` 不给默认值：默认 0 就是"忘了传也照样起监听、只是曲线静默不动"，而那正是
    这一路 M4 之前的缺陷形状。调用方必须表态。"""
    # 选后端只走 `make_backend`（与 Session 同一个装配口）：认不出的传输（例如现在的 rtu）
    # 会抛 `UnsupportedTransportError`，由 main 兜成一行错误，不甩 traceback。
    backend = make_backend(channel, packet_sink=None if quiet else _line,
                           tick_ms=tick_ms, log_sink=_runtime_log,
                           extra_endpoints=extra_endpoints)
    await backend.start()
    # 这一句问后端要（`describe()`）：TCP 是"所有监听地址"（不是只打通道自己那个——现场
    # 主站连的是**设备那一侧**的地址，只报 0.0.0.0:5020 等于没告诉他该连哪），串口是那一串
    # 串口参数（`COM3 19200 8E1`）。两种说法分头写在两处迟早对不上，所以只有一处。
    where = backend.describe()
    verb = "listening on" if channel_transport(channel) == "tcp" else "serving on"
    print(f"[simbench] {verb} {where} unit ids {_units(channel)}", flush=True)
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
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv[:1] == ["net"]:
        # 系统网络子命令与"起从站"是两条互不相干的路：先分流，不进点表解析。
        from .net.cli import main as net_main
        return net_main(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m simbench")
    parser.add_argument("point_table", nargs="?",
                        help="CSV 或 project.json（只给 --gui 时可以省略，装载留给界面）")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check", action="store_true", help="只校验，不监听")
    parser.add_argument("--quiet", action="store_true", help="不打印报文")
    parser.add_argument("--list", action="store_true", help="打印点表后退出")
    parser.add_argument("--gui", action="store_true",
                        help="打开桌面界面而不是命令行监听")
    parser.add_argument("--no-net", action="store_true",
                        help="这次启动不碰系统网络（跳过按声明自动配 IP）")
    args = parser.parse_args(argv)
    if args.no_net:
        # 走环境变量而不是往下传参数：GUI 那条路（`build_app`）不经过这里，两边读同一个开关
        from .net.startup import AUTO_ENV
        os.environ[AUTO_ENV] = "0"
    if args.point_table is None and not args.gui:
        parser.error("point table is required")

    project = None
    if args.point_table is not None:
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
        applied = _apply_overrides(project, args.host, args.port)
        if not applied and (args.host is not None or args.port is not None):
            # 给了参数却一路都没盖上（这份点表全是串口通道）：说一句，别让人以为改到了
            print("[simbench] --host/--port only apply to TCP channels; "
                  "this point table has none", file=sys.stderr)
        if not error_lines(issues) and applied:
            issues = validate_project(project)

        errors = _report(notes, issues)
        if errors:
            print(f"[simbench] {len(errors)} error(s); refusing to start", file=sys.stderr)
            return 2
        if not project.channels:
            print("[simbench] the point table has no channel; check its first column",
                  file=sys.stderr)
            return 2

    if args.gui:
        # 闸门在上面已经走过了：`--gui` 拿到的是校验通过的点表，坏点表在这里根本不开窗口
        # （界面里那份清单是模态的，命令行不该让人对着一个弹框等 Ctrl+C）
        from .gui.app import build_app
        app, _window, session = build_app(
            [args.point_table] if args.point_table else [])
        try:
            return app.exec()
        except KeyboardInterrupt:
            session.shutdown()
            return 0

    if args.list:
        for channel in project.channels:
            _print_table(channel)
        return 0
    if args.check:
        print("[simbench] point table is valid")
        return 0
    if len(project.channels) > 1:
        print("[simbench] M1 serves one channel at a time; using "
              f"{project.channels[0].name}", file=sys.stderr)

    _prepare_network(args.point_table, project)
    try:
        asyncio.run(_serve(project.channels[0], args.quiet, project.tick_ms,
                           _listen_endpoints(project.channels[0], args.point_table)))
    except KeyboardInterrupt:
        print("\n[simbench] stopped", flush=True)
    except BackendStartError as exc:
        # 资源不归我们（端口被占、串口打不开……）按一行错误报，不甩 traceback（和读不到文件
        # 同一个口径）：现场第一件事就是"再起一次"，而这里静默成功比报错更糟——两个监听叠在
        # 一起，主站继续读旧点表。两条传输的拒绝都从 `BackendStartError` 派生，所以只认它。
        print(f"[error] [port] {exc}", file=sys.stderr)
        return 2
    except UnsupportedTransportError as exc:
        # 这一路传输在当前构建里没有后端（M5 的串口那一段正在做）：同样一行错误 + 退出码 2，
        # 不许让一个属性错误或 RuntimeError 变成 traceback 甩给现场。
        print(f"[error] [channel] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
