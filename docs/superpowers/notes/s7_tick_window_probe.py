r"""S7 竞态窗口宽度探针：`_tick_once` 里「查归属」到「写值」之间隔了多少微秒。

跑法：`.venv\Scripts\python.exe -u docs/superpowers/notes/s7_tick_window_probe.py [tick_ms] [点位数]`
结论与输出记录在 `docs/architecture/system-model.md` §10.2。

**别把单位写回 `µs`**：这台机控制台是 GBK，`µ`(U+00B5) 编不出来，`print` 会当场
`UnicodeEncodeError` 打断输出（实测踩过，所以这里一律写 `us`）。`≈ → ｜ · ×` 都在 GBK 里，安全。

为什么要量宽度：竞态要撞上，必须是主站的写与 `own()` 恰好落在 `base.py:411`（查归属）与
`base.py:432`（写值）之间那一段。把**窗口宽度 ÷ tick 周期**当作单次主站写被撞的概率，
频率就不是猜的——`s7_ownership_race_probe.py` 的 (4) 段跑 200 轮 0 命中，需要一个独立估算
来说明"0 命中"是概率低而不是实验没做对。

量法：包住 `SourceStore.is_waveform` 与 `BackendBase._write_point`，记录同一个点位两者之间的
时间差。包住的是实例属性而不是类，所以只影响这一个 backend。

实测（2026-10-16，HEAD cbd2322，本机）：
  tick=100ms / 200 点（3800 样本）：均值 15.8us、中位 14.2、p99 80.3、最大 341.6
  tick=50ms  / 1 点（39 样本）  ：均值 118.8us、中位 119.2、p99=最大=257.1
换算出单次概率 1.6e-4 ~ 5.1e-3。点位少的那一组均值反而大，样本只有 39 个、且每跳都要走
一遍冷路径，别把它当"点越少窗口越宽"的规律读。
"""
from __future__ import annotations

import socket
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from simbench.backend.s7_server import S7Backend
from simbench.engine.runner import EngineRunner
from simbench.project.model import Device, Point, S7Channel

CALL_TIMEOUT = 10.0
SAMPLE_SECONDS = 2.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    tick_ms = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    n_points = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    port = free_port()
    runner = EngineRunner()
    runner.start()

    points = [Point(f"p{i}", "db1", i * 4, "real", initial="1.0",
                    waveform="const", wave_param={"value": str(20 + i)})
              for i in range(n_points)]
    channel = S7Channel("s7-1", host="127.0.0.1", port=port,
                        devices=[Device("D1", 11, points)])
    backend = S7Backend(channel, tick_ms=tick_ms)

    # ---------- 包住"查"与"写"，量同一点上两者的时间差 ----------
    gaps: list[float] = []          # us
    pending: dict[int, int] = {}    # address -> perf_counter_ns（查的那一刻）
    sources = backend._sources
    orig_is_waveform = sources.is_waveform
    orig_write_point = backend._write_point

    def timed_is_waveform(channel_name, unit_id, area, address, bit=None):
        pending[address] = time.perf_counter_ns()
        return orig_is_waveform(channel_name, unit_id, area, address, bit=bit)

    def timed_write_point(key, pt, value):
        started = pending.get(pt.address)
        wrote = orig_write_point(key, pt, value)
        if started is not None:
            gaps.append((time.perf_counter_ns() - started) / 1000.0)
            del pending[pt.address]
        return wrote

    sources.is_waveform = timed_is_waveform
    backend._write_point = timed_write_point

    async def _start():
        await backend.start()

    async def _stop():
        await backend.stop()

    runner.call(_start, CALL_TIMEOUT, "start")
    time.sleep(SAMPLE_SECONDS)
    runner.call(_stop, CALL_TIMEOUT, "stop")
    runner.stop()

    if not gaps:
        print("没采到样本：这一路一次 tick 都没跑起来，本探针无结论")
        return
    gaps.sort()
    tick_us = tick_ms * 1000.0
    mean = statistics.fmean(gaps)
    p50 = statistics.median(gaps)
    p99 = gaps[min(int(len(gaps) * 0.99), len(gaps) - 1)]
    worst = gaps[-1]
    print(f"tick_ms={tick_ms}  点位数={n_points}  样本={len(gaps)}")
    print(f"  窗口宽度 (us)：均值 {mean:.1f}  中位 {p50:.1f}  p99 {p99:.1f}  最大 {worst:.1f}")
    print(f"  tick 周期 = {tick_us:.0f} us")
    print(f"  单次主站写撞上的概率 ≈ 窗口/周期：均值口径 {mean / tick_us:.2e}"
          f"  最坏口径 {worst / tick_us:.2e}")
    print(f"  换算：均值口径约每 {tick_us / mean:,.0f} 次主站写撞 1 次；"
          f"最坏口径约每 {tick_us / worst:,.0f} 次撞 1 次")
    print("  口径提醒：这是「同一点上查与写的时间差」，隐含假设是主站的写+标记在这一段内"
          "均匀到达。DLL 回调自身的排队延迟没有计进来，所以这是一个下界方向的近似。")


if __name__ == "__main__":
    main()
