"""把"取一行快照"从 GUI 主线程挪到一条后台线程：界面只读最近一份缓存。

为什么要它（实测，见 `docs/superpowers/plans/2026-10-14-deep-review-batch.md` §3）：
`Session.rows()` 在 4000 点的表上一次要 16.3ms（加过 `_row_cached` 之后**还剩**这么多，
几乎全是引擎那一跳的整批回读），而 GUI 每 100ms 调一次、**跑在主线程上**。点表一大，
界面就跟着引擎的节拍走；引擎要是卡住不回声，那次调用要等到 `_READ_TIMEOUT`（2 秒）才回来，
期间界面是死的。

三条口径：

* **只快照当前显示的那一路**：界面一次只显示一个通道（与"表里显示什么"一致），别的通道
  一次都不读——多读一路就多一份没人看的成本；
* **换通道/写值那一次同步取**：操作员按下"看这一路"、或者刚改完一个值，表里就该有内容，
  不该先空一拍（那就是本项目最防的"看着像设备没值"）；
* **取失败不清空**：引擎没回话时保留上一份，把失败计数与最后一次错误留下来。
  "读不到"由 `Session.rows()` 那一层的 `Row.stale` 表达；这里要是清空，界面就是在演
  "设备报了这个值"。
"""

from __future__ import annotations

import threading
import time

from .session import Row, Session

# 后台取快照的节拍：与主窗心跳同一个数（100ms）。取一次 16ms 的表在后台跑，主线程这一跳
# 只剩模型 diff。
DEFAULT_INTERVAL = 0.1


class RowsPump:
    """`Session` 之上的只读缓存层。它自己也满足 `point_source.PointSource`。

    `rows(channel)` 只是"把最近一份快照给它"，所以主线程那一条路径**永远不等引擎**；
    真正的取数在 `_loop` 那条线程里按 `interval` 跑。
    """

    def __init__(self, session: Session, interval: float = DEFAULT_INTERVAL) -> None:
        self._session = session
        self.interval = interval
        self._lock = threading.Lock()
        self._channel = ""
        self._rows: list[Row] = []
        self._fetched_at = 0.0
        # 上一份缓存是按哪个结构版本取的（见 `Session.revision`）：装载/改结构/增删通道之后
        # 必须重新取，否则表格里还是上一份点表的行——那正是"界面演一份不存在的点表"。
        self._revision = -1
        self.failures = 0
        self.last_error = ""
        self._announced = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 数据源接口（PointTableModel 只认这三个）----------

    @property
    def project(self):
        """`PointTableModel.attach` 会读它（要按工程里的通道名选默认通道）。"""
        return self._session.project

    def rows(self, channel: str) -> list[Row]:
        """最近一份快照。**换通道、或者工程结构变了**时同步取一次。

        换通道要同步取：操作员按下"看这一路"，表里就该有内容，不该先空一拍。
        结构变了也要：界面刚加/删了一个点位，表里必须立刻反映——缓存里的行是上一份点表的
        形状，拿它去显示等于演一份不存在的点表。
        """
        if channel != self._channel or self._revision != self._session.revision:
            self._channel = channel
            self.fetch_now()
        with self._lock:
            return self._rows

    def write(self, channel: str, unit_id: int, area: str, address: int, text: str,
              bit: int | None = None) -> tuple[bool, str]:
        """转发给 Session；**写成了就同步取一次**——刚改的那个值该马上看见。"""
        ok, error = self._session.write(channel, unit_id, area, address, text, bit)
        if ok:
            self.fetch_now()
        return ok, error

    # ---------- 取数 ----------

    def fetch_now(self) -> list[Row]:
        """同步取一次（换通道、写值、以及用例里"我现在就要看到引擎的真值"）。"""
        channel = self._channel
        if not channel or self._session.closed:
            return self._rows
        revision = self._session.revision
        try:
            rows = self._session.rows(channel)
        except Exception as exc:                       # noqa: BLE001
            self._note_failure(channel, exc)
            return self._rows
        with self._lock:
            self._rows = rows
            self._fetched_at = time.monotonic()
            self._revision = revision
        return rows

    def channel(self) -> str:
        return self._channel

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="simbench-rows-pump",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------- 内部 ----------

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            channel = self._channel
            if not channel:
                continue
            if self._session.closed:
                # Session 收工了（关窗/换工程）：这条线程自己退场。留着就会一直对着一个
                # 已经停掉的 runner 调 `rows()`，每跳留一条错误。
                return
            revision = self._session.revision
            try:
                rows = self._session.rows(channel)
            except Exception as exc:                   # noqa: BLE001
                self._note_failure(channel, exc)
                continue
            with self._lock:
                self._rows = rows
                self._fetched_at = time.monotonic()
                self._revision = revision

    def _note_failure(self, channel: str, exc: Exception) -> None:
        """失败**计数 + 报一次**：连续失败不许刷屏（10Hz 能刷干净日志窗）。"""
        self.failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self._announced:
            return
        self._announced = True
        # 经 Session 的事件流报（它走 pump 线程 → bridge）：这条线程**不许碰 widget**
        self._session.publish_log(
            "warning", f"点位表快照失败（保留上一份显示）：{channel}: {self.last_error}")
