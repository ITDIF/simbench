"""引擎层共用的记录类型：点位行、写入结果、通道运行态，以及等引擎回话的上限。

从 `session.py` 拆出来（2026-10-15 结构优化）：读侧（`reads.py`）、写侧（`writes.py`）与
门面（`session.py`）都要用这些类型，放在门面里会造成互相 import。`session.py` 仍然把
它们再导出，老路径（`from simbench.engine.session import Row`）保持可用。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..backend import BackendBase

# 等引擎回话的三条上限。**口径**：GUI 能走到的每一条等待都用 `runner.call(...)`，
# 不用裸 `submit(...).result(...)`——`call` 在超时会把引擎线程当时的栈帧一起写进异常
# 消息，而裸 TimeoutError 在现场只剩"界面没响应"这一条线索（本项目最防的"看不出是谁卡的"）。
#   `_START_TIMEOUT` 10s：backend 内部 `_await_ready` 自己 5s 就放弃，10s 严格在它之上，
#              所以这里等到的必然是"已经报过错了"而不是"还在等"。
#   `_STOP_TIMEOUT`  10s：实测 `stop()` 里 serve_forever 在 0.2ms 内收尾；10s 的含义是
#              "根本没关掉"，宁可带上栈帧报错也不许把旧监听静默留着占端口。
#   `_WRITE_TIMEOUT`  5s：写一个点是内存操作（改 SimRuntime 的寄存器列表），5s 不回
#              说明引擎线程被别的东西占住了，再继续等下去只会让操作员多点几次同一格。
_START_TIMEOUT = 10.0
_STOP_TIMEOUT = 10.0
_WRITE_TIMEOUT = 5.0
# 整批回读的上限（见 `_snapshot_for`）：GUI 心跳每 100ms 调一次，等太久等于把界面卡住。
# 正常一次回读是毫秒级，2 秒只可能意味着引擎线程没回话。
_READ_TIMEOUT = 2.0


@dataclass(frozen=True)
class Row:
    """点位表一行。字段顺序是**位置参数契约**：Task 5 的 `point_source.FakeEngine`
    按这个顺序构造，改名或换序都要先看 `tests/test_session.py` 那条顺序断言。

    `registers` 定死 `tuple[int, ...]`（A–I 表 G）：`_render` 与替身给的是同一种容器，
    上层才能用 `!=` 判"这行变了没有"。回读失败时它是**空的**，不是 `[0]`。

    `stale` 只在**通道在跑且回读失败**时为真。"没启动"不是异常（那时显示 CSV 初值
    是正确行为），"启动了却没读到"才是——两者回落的是同一份初值文字，界面上分不出来，
    而把"没读到"演成"设备报了这个值"正是本项目最防的那类假象，所以这里必须带出来。

    `source` 是 spec §7 那唯一一条值归属：`waveform` = 曲线还在驱动这个点，`manual` =
    被界面或主站接管过、波形一个字都不再写。它**只能放在最后一位且带默认值**：
    `FakeEngine`/本文件的 `rows()` 都按位置构造，插进 `waveform` 前面会把 `readonly`
    与 `waveform` 串成一列看着合法的错值——那种错测试全绿、现场读错。
    """

    channel: str
    device: str
    unit_id: int
    name: str
    area: str
    address: int
    datatype: str
    word_order: str
    value_text: str
    registers: tuple[int, ...]
    readonly: bool
    waveform: str
    stale: bool = False
    source: str = "waveform"
    # S7 的位（`DBX6.2` 里的 2）：**身份的一部分**，放最后一位且带默认值——`Row` 的字段
    # 顺序是位置参数契约（`FakeEngine` 与一批用例按位置构造），插在前面会把 `readonly`
    # 与 `waveform` 串成一列看着合法的错值。Modbus 的点恒为 None。
    bit: int | None = None

    @property
    def key(self) -> tuple[str, int, str, int, int | None]:
        """点位身份：同字节上的两个 S7 bool 是**两个**身份（带位才分得开）。"""
        return (self.channel, self.unit_id, self.area, self.address, self.bit)


@dataclass
class WriteResult:
    ok: bool
    error: str = ""
    value_text: str = ""
    registers: tuple[int, ...] = ()


@dataclass
class ChannelRuntime:
    name: str
    running: bool = False
    detail: str = ""
    connections: int = 0
    # 两条传输共用这个字段：具体是哪个后端由 `backend.make_backend()` 按通道类型选，
    # 所以这里的类型是基类而不是某个传输（写死 TCP 那个类，读代码的人会以为串口不复用）。
    backend: BackendBase | None = None
    last_error: str = ""
