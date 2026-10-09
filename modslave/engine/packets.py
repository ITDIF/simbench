"""报文环形缓冲 + 过滤。Qt-free：CLI 与 GUI 共用，暂停/清除/过滤只有一份语义。"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from ..backend.events import PacketEvent

ALL = "全部"
REQUESTS = "请求"
RESPONSES = "响应"
EXCEPTIONS = "异常"
FILTERS: tuple[str, ...] = (ALL, REQUESTS, RESPONSES, EXCEPTIONS)   # 顺序即标签顺序

# backend 的 `parse_packet` 在摘要里给异常响应打的标记（实测句子是
# `unit 77 fc 3 EXCEPTION 4`）。它是**判据的第二来源**，与 backend 的耦合由
# `test_the_exception_shape_the_backend_really_emits_is_an_exception` 用真 parse_packet
# 造事件钉住：那边文案一改，这条就红，不会让这里悄悄失效。
_EXCEPTION_MARK = "EXCEPTION"


def _is_exception(event: PacketEvent) -> bool:
    """异常帧的两种真形状都要认。

    1. 线上字节的功能码带 0x80（`fc & 0x80`）——裸帧/M5 的 RTU 监听那条路会这样进来。
    2. M1 已钉的 backend 契约：`parse_packet` 把 0x80 **脱掉**放进 `function_code`，
       把 `EXCEPTION <码>` 写进 `parsed`（实测真主站写只读点回的是 `fc=3` +
       `"unit 11 fc 6 EXCEPTION 2"`）。

    只判第 1 种（计划正文的写法）的话，真从站的异常响应在"异常"桶里**一条都没有**：
    报文窗于是对平台说"没有异常"，而从站明明刚回了异常码 2——正是本项目最防的那种
    "把没读到演成没发生"。第 2 种的正常响应摘要只有 `unit/fc/字节数/十六进制`，
    不含 EXCEPTION 这个词，所以不会误判。
    """
    if (event.function_code or 0) & 0x80:
        return True
    return _EXCEPTION_MARK in (event.parsed or "")


def filters_for(event: PacketEvent) -> set[str]:
    """一个事件属于哪些桶。

    方向以 pymodbus 的 `trace_packet(is_outgoing)` 为唯一依据（实测 RX 在解帧前、TX 在
    发送前）。异常响应也是"发出去的一帧"，所以它同时属于 响应 与 异常——不是单独一桶，
    否则过滤"响应"时会看不见异常，而现场最想看的恰恰就是它。
    """
    buckets = {ALL, RESPONSES if event.direction == "tx" else REQUESTS}
    if _is_exception(event):
        buckets.add(EXCEPTIONS)
    return buckets


class PacketStore:
    """容量固定、按到达顺序追加；`rows()` 只返回当前过滤器下可见的行。

    写的一方是引擎的事件 pump 线程（`Session` 把 `PacketEvent` 喂进来），读的一方是
    GUI 主线程（10Hz 拉整表）。实测不保护时 `rows()` 的推导式会被中途插进来的一条
    打断，抛 `RuntimeError: deque mutated during iteration`——模拟器自己的竞态在现场
    会被当成"报文窗崩了/平台读不到报文"，所以这里用一把锁把"变更"和"复制整段"隔开。
    锁只盖住缓冲：订阅者回调一律在锁**外**调用（回调里可能再读 `rows()`，
    而且一个慢的重绘不该把采集路径卡住——那是丢报文的原因）。
    """

    def __init__(self, capacity: int = 5000) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._items: deque[PacketEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.capacity = capacity
        self.dropped = 0
        self.paused = False
        self.active_filter = ALL
        self._listeners: list[Callable[[], None]] = []

    def add(self, event: PacketEvent) -> bool:
        """暂停时不落缓冲。返回是否收下——GUI 用它决定要不要重绘。"""
        if self.paused:
            return False
        with self._lock:
            overflow = len(self._items) == self._items.maxlen
            self._items.append(event)
            if overflow:
                self.dropped += 1
        self._notify()
        return True

    def set_filter(self, name: str) -> None:
        if name not in FILTERS:
            raise ValueError(f"unknown packet filter {name!r}, use one of {FILTERS}")
        self.active_filter = name
        self._notify()

    def rows(self) -> list[PacketEvent]:
        name = self.active_filter
        with self._lock:
            items = list(self._items)      # 原子复制；过滤在副本上做，不再碰活缓冲
        if name == ALL:
            return items
        return [e for e in items if name in filters_for(e)]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.dropped = 0
        self._notify()

    def subscribe(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:                        # noqa: BLE001
                pass    # 界面回调炸了不许带停采集；窗口销毁前会自己退订
