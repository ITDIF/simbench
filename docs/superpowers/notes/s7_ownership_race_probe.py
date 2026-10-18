r"""S7 归属竞态探针：主站的写会不会被同一跳波形顶掉？顶掉的窗口有多宽？

跑法：`.venv\Scripts\python.exe -u docs/superpowers/notes/s7_ownership_race_probe.py [轮数]`
结论与输出记录在 `docs/architecture/system-model.md` 第十节（不在同目录另开一份 .md：
这轮的结论已经成文在那一节，重复一份只会两处不同步）。

背景：`backend/ownership.py:3-8` 写了"运行态两张表只在引擎 loop 线程读写"，而 S7 的
`_on_event` 是 snap7 原生库从**它自己的线程**回调的（`s7_server.py:240-241` 自己也这么声明）。
`SourceStore._manual` 没有锁。问四件事：

1. **回调到底在哪个线程**：起真后端、真主站写一次，记录每个事件的线程名与 EvtCode，
   看写事件（`_EVENT_WRITE = 0x40000`）是不是落在非 loop 线程上；
2. **窗口在不在**：`_tick_once` 的"查归属"（`base.py:411`）与"写值"（`base.py:432`）之间
   隔着 `waveform_value` 与 `_clamp`，把主站的写 + `own()` 注入到 `_clamp`（:426）里，
   就复现出"标记落地前那一跳已经把主站的值覆盖掉"；
3. **后果会不会自愈**：覆盖之后该点已是 manual，后续每一跳都跳过它，所以值是**冻住**的，
   不是过一拍就回来；
4. **不强制的话多久撞一次**：第 (c) 段不注入，真主站连写 N 轮，每轮先交还波形再写一个
   独异的值，读回缓冲分类统计（保住 / 真竞态 / 归属没标上）。

实测结果（2026-10-16，HEAD cbd2322，本机）：(1) 写事件在 `Dummy-3`；(2)(3) 一次注入就复现，
缓冲停在波形值 20.0 且归属为 manual；(4) 200 轮 0 次撞上——与窗口宽度实测（见
`s7_tick_window_probe.py`）换算出的 1.6e-4 ~ 5.1e-3 相符。

注意 (b) 段的注入是在 tick 线程里跑的，它**模拟**交错而不是制造交错：它把"主站值落盘 +
标接管"这两步放进 :426 那一刻，等价于 DLL 线程恰好在那一刻回调。它证明"窗口一旦撞上会怎样"，
不证明"会撞上"——后者是 (c) 段的事。
"""
from __future__ import annotations

import inspect
import socket
import statistics
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pyS7 import S7Client

from simbench.backend.s7_server import S7Backend
from simbench.engine.runner import EngineRunner
from simbench.project.model import Device, Point, S7Channel

WAVE_VALUE = 20.0        # const 波形每跳写的值
MASTER_VALUE = 99.0      # 主站写的值；两者不同才分得出谁赢了
CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def make_channel(port: int) -> S7Channel:
    """一个 DB1 real 点，const 波形 —— 每跳都写 WAVE_VALUE，便于判定谁赢了。"""
    return S7Channel("s7-1", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("温度", "db1", 0, "real", initial="12.5",
                  waveform="const", wave_param={"value": str(WAVE_VALUE)}),
        ])])


def read_db1_real(backend) -> float:
    """从区域缓冲直接读那 4 个字节（不经主站，免得读事件干扰归属统计）。"""
    entry = backend._area_of(backend._flatten_points()[0][1])
    _name, _db, _code, _index, buf, _points = entry
    return struct.unpack(">f", bytes(buf[0:4]))[0]


def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    port = free_port()
    runner = EngineRunner()
    runner.start()
    print(f"主线程         = {threading.current_thread().name}")
    print(f"引擎 loop 线程 = {runner._thread.name}")

    backend = S7Backend(make_channel(port), tick_ms=50,
                        log_sink=lambda level, text: print(f"    [log:{level}] {text}"))

    def acall(fn, what):
        """`runner.call` 要的是**协程工厂**（`runner.py:108` 用 run_coroutine_threadsafe），
        而 `restore_waveform` 这类是同步方法 —— 照 `engine/writes.py:_arestore` 那样包一层。
        对 async 的（`start`）也要成立，所以拿到可等待对象就 await。"""
        async def _wrap():
            result = fn()
            if inspect.isawaitable(result):
                result = await result
            return result
        return runner.call(_wrap, CALL_TIMEOUT, what)

    # ---------- (1)(2)(3) 的公共前置：记录每个事件来自哪个线程 ----------
    seen: list[tuple[str, int]] = []
    orig_on_event = backend._on_event

    def spy(event):
        seen.append((threading.current_thread().name,
                     int(getattr(event, "EvtCode", 0) or 0)))
        return orig_on_event(event)

    backend._on_event = spy
    acall(backend.start, "start")
    print(f"后端起在       = {port}\n")

    cli = S7Client("127.0.0.1", rack=0, slot=1, port=port, timeout=3.0)
    cli.connect()
    cli.write(["DB1,R0"], [MASTER_VALUE])
    # 等的是**归属真落地**，不是"收到过任意事件"：连接事件也算事件，早先的版本被它骗过，
    # 于是归属检查跑在了写事件之前，读出 waveform 还以为标记没生效。
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and backend._sources.is_waveform("s7-1", 0, "db1", 0):
        time.sleep(0.01)

    print("=== (1) 回调线程身份 ===")
    for name, code in seen:
        print(f"    EvtCode={code:#06x}  线程={name}")
    off_loop = sorted({n for n, _c in seen if n != runner._thread.name})
    print(f"  非 loop 线程上的回调 = {off_loop or '（无）'}")
    print(f"  => 回调确实跨线程    = {bool(off_loop)}")
    print(f"  写完后该点归属       = "
          f"{'waveform' if backend._sources.is_waveform('s7-1', 0, 'db1', 0) else 'manual'}")
    print(f"  此刻缓冲值           = {read_db1_real(backend)}"
          f"  （波形 {WAVE_VALUE} / 主站写的 {MASTER_VALUE}）\n")

    # ---------- (1b) 事件送达延迟：这才是易感区间的大头 ----------
    # 10.2 的"窗口/周期"模型只算了 Python 侧查与写之间那段（几十微秒），而单次命中的实测
    # 概率比它高好几倍 —— 说明真正让 tick 有机会顶掉主站值的，是"原生写完成"到"事件回调
    # 把归属标上"之间那段**我们看不见的延迟**。这里把它量出来：client.write() 返回之后，
    # 轮询到归属翻转，中间的间隔就是这段延迟的下界（写本身在此之前已由原生层做完）。
    print("=== (1b) 事件送达延迟（写返回 → 归属标记落地）===")
    latents: list[float] = []
    for k in range(15):
        acall(lambda: backend.restore_waveform(0, "db1", 0, count=4), "restore")
        time.sleep(0.09)                  # 让波形先写一拍，并等上一轮的事件彻底排空
        t0 = time.perf_counter_ns()
        cli.write(["DB1,R0"], [float(2000 + k)])
        while backend._sources.is_waveform("s7-1", 0, "db1", 0):
            if time.perf_counter_ns() - t0 > 2_000_000_000:
                break
            time.sleep(0.000001)          # 1us 粒度轮询，别用 sleep(0.01) 把分辨率吃掉
        latents.append((time.perf_counter_ns() - t0) / 1e6)   # ms
    latents.sort()
    print(f"  15 次 (ms)：中位 {statistics.median(latents):.3f}  "
          f"p90 {latents[int(len(latents) * 0.9)]:.3f}  最大 {latents[-1]:.3f}")
    print("  对照：tick 周期 50ms，Python 侧查-写窗口约 85us = 0.085ms")
    print("  => 若这段延迟远大于 0.085ms，易感区间就由它决定，"
          "10.2 那个『窗口/周期』模型就是低估\n")

    # ---------- (2)(3) 强制交错：把主站的写塞进「查归属」与「写值」之间 ----------
    print("=== (2)(3) 强制交错 ===")
    acall(lambda: backend.restore_waveform(0, "db1", 0, count=4), "restore")
    time.sleep(0.2)
    print(f"  交还波形后缓冲值     = {read_db1_real(backend)}  （期望 {WAVE_VALUE}）")

    armed = {"go": False, "fired": 0}
    orig_clamp = backend._clamp

    def clamp_with_injected_master(value, pt):
        """`_clamp` 在 `base.py:426`，正落在 is_waveform(:411) 与 _write_point(:432) 之间。
        在这里做 DLL 线程那一刻会做的两件事：主站的值落进区域缓冲 + 标接管。"""
        if armed["go"]:
            armed["go"] = False
            armed["fired"] += 1
            import ctypes

            entry = backend._area_of(pt)
            _n, _db, code, index, buf, _pts = entry
            backend._server.lock_area(code, index)
            try:
                ctypes.memmove(ctypes.byref(buf, 0), struct.pack(">f", MASTER_VALUE), 4)
            finally:
                backend._server.unlock_area(code, index)
            backend._own_intersecting(0, "db1", 0, 4, span_of=lambda p: 4)
            print(f"    [注入] 主站值 {MASTER_VALUE} 已落盘；该点归属="
                  f"{'waveform' if backend._sources.is_waveform('s7-1', 0, 'db1', 0) else 'manual'}")
        return orig_clamp(value, pt)

    backend._clamp = clamp_with_injected_master
    armed["go"] = True
    time.sleep(0.4)
    backend._clamp = orig_clamp
    time.sleep(0.3)              # 再跑几跳：该点若已 manual，tick 应当跳过它
    after = read_db1_real(backend)
    # fired=0 意味着注入自己抛了异常、被 `_tick_loop` 那道"故意瞎捕"的 except 吞掉
    # （base.py:396-397），那这一段就没有意义。所以要把次数打出来，别信一个静默失败的实验。
    print(f"  注入真的执行过吗     = {armed['fired']} 次"
          f"（0 次 = 注入自身抛错被吞，本段无效）")
    print(f"  注入之后缓冲值       = {after}")
    print(f"  该点归属             = "
          f"{'waveform' if backend._sources.is_waveform('s7-1', 0, 'db1', 0) else 'manual'}")
    print("  判定                 = "
          + (f"{after} -> 主站值被波形顶掉并冻住（竞态后果成立）"
             if abs(after - WAVE_VALUE) < 1e-3 else f"{after} -> 主站值保住了") + "\n")

    # ---------- (4) 自然频率：不注入 ----------
    print(f"=== (4) 自然频率（{trials} 轮，不强制交错）===")
    hits = {"race": 0, "lost_mark": 0, "other": 0}
    for i in range(trials):
        acall(lambda: backend.restore_waveform(0, "db1", 0, count=4), "restore")
        time.sleep(0.08)                  # 让波形先写上一拍
        v = float(1000 + i)               # 每轮写不同的值，避免与波形值巧合相同
        cli.write(["DB1,R0"], [v])
        time.sleep(0.15)                  # 跨 2~3 拍 tick
        got = read_db1_real(backend)
        still_waveform = backend._sources.is_waveform("s7-1", 0, "db1", 0)
        if abs(got - v) < 1e-3:
            continue                      # 正常：主站的值保住了
        # 撞上了。再分是哪种：归属已标 manual => tick 在标记落地**之前**就把值顶掉（真竞态）；
        # 仍是 waveform => 写事件压根没把归属标上（另一种失效，不属于本条竞态）。
        if abs(got - WAVE_VALUE) < 1e-3:
            kind = "race" if not still_waveform else "lost_mark"
        else:
            kind = "other"
        hits[kind] += 1
        print(f"    第 {i:3d} 轮：写 {v} → 读回 {got}"
              f"｜归属={'waveform' if still_waveform else 'manual'}｜归类={kind}")
    print(f"  {trials} 轮：保住 {trials - sum(hits.values())} 次；"
          f"真竞态 {hits['race']} 次；归属没标上 {hits['lost_mark']} 次；其它 {hits['other']} 次")

    cli.disconnect()
    acall(backend.stop, "stop")
    runner.stop()


if __name__ == "__main__":
    main()
