"""引擎线程：一个专属线程跑 asyncio loop，M1 的 conftest 私有实现的正式版。

为什么不合并到 backend：backend 只管协议，runner 只管"把协程跑在别的线程上、
并把事件按序送出去"。测试与 GUI 都只跟这个类说话。
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

_MAX_JOIN_SECONDS = 5.0


class EngineRunner:
    def __init__(self, name: str = "modslave-engine") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sinks: list[Callable[[Any], None]] = []
        self._queue: queue.Queue = queue.Queue()
        self._pump_stop = threading.Event()
        self._pump: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._name)
        self._pump = threading.Thread(target=self._pump_events, daemon=True,
                                      name=f"{self._name}-events")
        self._thread.start()
        self._pump.start()
        while self._loop is None and not ready.is_set():
            ready.wait(0.05)
            if self._thread is not None and not self._thread.is_alive():
                raise RuntimeError("engine thread died before the loop started")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        stuck: list[threading.Thread] = []
        if loop is not None and thread is not None and thread.is_alive():
            asyncio.run_coroutine_threadsafe(_noop(), loop)  # 唤醒一次，确保 stop 排队有序
            loop.call_soon_threadsafe(loop.stop)
            thread.join(_MAX_JOIN_SECONDS)
            if thread.is_alive():
                stuck.append(thread)
        if self._pump is not None:
            self._pump_stop.set()
            self._pump.join(_MAX_JOIN_SECONDS)
            if self._pump.is_alive():
                stuck.append(self._pump)
        self._loop, self._thread, self._pump = None, None, None
        if stuck:
            # 两条都试完才抛：第一条就抛会把 pump 留在原地，那个没人收的僵尸线程
            # 会让后面每条用例的线程断言都错。
            names = ", ".join(repr(t.name) for t in stuck)
            raise RuntimeError(f"engine thread(s) {names} did not stop within "
                               f"{_MAX_JOIN_SECONDS}s; they still hold the loop "
                               f"and any bound port")

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread else None

    @property
    def thread_name(self) -> str:
        return self._name

    # ---------- 提交协程 ----------

    def submit(self, coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Future:
        """传工厂而不是协程对象：loop 没起来时协程从未被 await 过，不会有悬空 warning。"""
        loop = self._loop
        if loop is None or not self.alive:
            raise RuntimeError("engine runner is not running")
        return asyncio.run_coroutine_threadsafe(coro_factory(), loop)

    def call(self, coro_factory, seconds: float, what: str):
        """`submit(...).result(seconds)`，但超时把引擎线程当时卡在哪一起报出来。

        裸 `TimeoutError` 在现场等于"引擎没响应，但不知道卡在哪一步"，只能靠复现碰运气；
        带上前 3 帧之后，一张截图就能定位。
        """
        future = self.submit(coro_factory)
        try:
            return future.result(seconds)
        except TimeoutError as exc:
            raise TimeoutError(f"{what}: engine thread did not finish within "
                               f"{seconds}s (alive={self.alive})"
                               f"{self._frames_hint()}") from exc

    def _frames_hint(self) -> str:
        thread = self._thread
        if thread is None:
            return ""
        frame = sys._current_frames().get(thread.ident)
        if frame is None:
            return "; engine frame not available"
        tail = "".join(f"  | {line.rstrip()}\n"
                       for line in traceback.format_stack(frame)[-3:])
        return f"; engine stack tail:\n{tail}"

    # ---------- 事件外发 ----------

    def subscribe(self, sink: Callable[[Any], None]) -> None:
        self._sinks.append(sink)

    def drain(self, seconds: float = 2.0) -> bool:
        """等事件队列排空（测试与 GUI 关闭时用）。True=排空，False=超时。

        只能从非 pump 线程调；pump 线程自己调会死锁，所以这里用队列长度判空，
        再补一次短 sleep 让最后一个 sink 调用返回。
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._queue.empty():
                time.sleep(0.01)
                if self._queue.empty():
                    return True
            time.sleep(0.005)
        return self._queue.empty()

    def publish(self, event: Any) -> None:
        """引擎线程内调用。绝不直接在调用方线程跑订阅者回调——那是 UI 崩溃的来源。

        投递顺序由队列保证；UI 侧再经 EngineBridge 转成 Qt queued signal。
        """
        self._queue.put(event)

    def _pump_events(self) -> None:
        """取事件逐个投递；停机时先把已经排队的事件发完再退。

        `stop()` 置位后不许立刻 return：队列里剩下的都是引擎线程**已经 publish** 的事件，
        丢掉就是"通道已经停了但设备树还挂在 running"这种现场最难查的形状。实测按"置位即退"
        写会在 120 次 start/stop 里丢 3 次，最坏一次 30 条只送到 1 条（pump 还没轮到就退了）。
        置位后把轮询收紧到 10ms，队列真空了才退出，`stop()` 的 join 照样在毫秒级返回。
        """
        while True:
            stopping = self._pump_stop.is_set()
            try:
                event = self._queue.get(timeout=0.01 if stopping else 0.05)
            except queue.Empty:
                if stopping:
                    return
                continue
            self._deliver(event)

    def _deliver(self, event: Any) -> None:
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:                        # noqa: BLE001
                pass    # UI 回调炸了不许带停引擎；错误由 GUI 自己负责显示


async def _noop() -> None:
    return None
