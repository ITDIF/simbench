"""吞异常的**唯一**出口：吞可以，没痕迹不行。

这个项目里有几处必须吞掉异常——Qt 槽函数里抛出去等于"点了没反应"，采集/投递路径上抛出去等于
丢报文，关窗路径上抛出去等于窗口关不掉。问题不在"吞"，而在**有的吞得一点痕迹都没有**：
一条正在崩的重绘路径、一个正在抛的订阅者回调，在日志里一个字都不留——正是本项目最防的
"把出错演成没出错"。

口径（与 `contextlib.suppress` 的分工）：

* **运行期**的吞点（投递事件、通知订阅者、界面回调）走这里：计数 + 留第一次的原话与位置，
  需要的时候把第一条报到日志/状态栏；
* `contextlib.suppress(...)` 留给**确定无害的清理动作**（关一个已经关掉的句柄、删一个临时
  文件、读一个可能不存在的键）——那种"最多是白做一次"的动作不值得留痕，写了反而是噪音。

用法（同一个吞点只留一本账，通常挂在收下"被吞者"的那个对象上）：

    self._swallowed = swallow.Swallower("channel events")
    ...
    try:
        sink(event)
    except Exception as exc:                       # noqa: BLE001
        if swallow.note(self._swallowed, exc, f"sink {sink!r}", sink=self._log):
            # 只报第一次：10Hz 的路径上每次报一遍会把日志窗刷干净
            ...
"""

from __future__ import annotations

from collections.abc import Callable


class Swallower:
    """一本"我吞过什么"的账。线程安全靠调用方（吞点本身通常已经在一条固定线程/锁里）。"""

    def __init__(self, where: str) -> None:
        self.where = where
        self.count = 0
        self.first_place = ""
        self.first_error = ""
        self.last_error = ""

    def note(self, exc: BaseException, place: str) -> bool:
        """记一笔；回 `True` 表示这是**第一次**（调用方据此决定要不要说出去）。"""
        self.count += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self.count == 1:
            self.first_place, self.first_error = place, self.last_error
            return True
        return False

    def summary(self) -> str:
        """给状态栏/日志用的一句话（没吞过就是空串）。"""
        if not self.count:
            return ""
        tail = f"（共 {self.count} 次，最后一次：{self.last_error}）" if self.count > 1 else ""
        return f"{self.where} 有回调失败：{self.first_place}: {self.first_error}{tail}"


def note(book: Swallower, exc: BaseException, place: str,
         sink: Callable[[str], None] | None = None) -> bool:
    """记账 + （给了 `sink` 且是第一次）说一句。回"是不是第一次"。"""
    first = book.note(exc, place)
    if first and sink is not None:
        sink(book.summary())
    return first
