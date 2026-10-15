"""读/写两个 mixin 的**宿主契约**：它们用到的那些名字，由 `Session` 提供。

为什么要有这一层（2026-10-15）：`session.py` 拆成读侧（`reads.py`）与写侧（`writes.py`）
两个 mixin 之后，mypy 单独检查这两个文件时看不见 `Session` 上那些名字（`_channels` /
`publish_log` ... 由 `__init__` 建、或在 `Session` 本体里实现），于是报一屏
`"ReadMixin" has no attribute ...`——CI 的 `mypy` 那一步会直接红。

这里把这份接口**声明一次**，两个 mixin 都继承它：

* 属性只写注解，真值由 `Session.__init__` 放上去；
* 方法写成 `...` 占位——它们在 `Session`（或 `ReadMixin`）里实现，而 `Session` 自己的
  定义在 MRO 上排在这些占位之前，所以占位在运行时**永远不会被调到**。

换句话说：这一层是给类型检查器看的，运行时不加任何行为，也不做任何校验。
"""

from __future__ import annotations

from typing import Any

from ..project.model import Point
from .records import ChannelRuntime
from .runner import EngineRunner


class SessionHost:
    """`Session` 与两个 mixin 共用的宿主声明（只声明，不实现）。"""

    _lock: Any            # `threading.RLock` 在 typeshed 里是工厂函数，不能当类型用
    _runner: EngineRunner
    _channels: dict[str, ChannelRuntime]
    _points: dict[tuple[str, int], dict[tuple[str, int, int | None], Point]]
    _rows_meta: dict[str, list[tuple[int, str, Point]]]
    _render_cache: dict[tuple, tuple]
    _read_failed: bool
    _dirty: bool

    # 下面四个的真身：`publish_log` / `_transport` / `_require` 在 `Session`，
    # `_read_back` 在 `ReadMixin`（写侧也要回读，所以两个 mixin 都得看得见它）。
    #
    # 体用 `raise` 而不是 `...`：占位在 MRO 上永远排在真身后面，正常调不到；真要调到了，
    # 说明继承顺序被改坏了，这时候**当场喊出来**比静默回一个 `None` 强得多。
    def publish_log(self, level: str, text: str) -> None:
        raise NotImplementedError("由 Session 实现")

    def _transport(self, channel: str) -> str:
        raise NotImplementedError("由 Session 实现")

    def _require(self, name: str) -> tuple[ChannelRuntime, object]:
        raise NotImplementedError("由 Session 实现")

    def _read_back(self, runtime: ChannelRuntime | None, pt: Point, unit_id: int,
                   transport: str = "tcp") -> tuple[list[int] | None, bool]:
        raise NotImplementedError("由 ReadMixin 实现")
