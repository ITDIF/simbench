"""点位表的数据源协议 + 测试替身。Qt-free。

`gui/point_table.PointTableModel` 只认这两个方法：`rows(channel)` 出一批行、
`write(...)` 改一个点。真实现是 `engine.session.Session`，替身是本文件的 `FakeEngine`
——这样 GUI 用例不必每次起 socket，而 Session 那条路仍由 `tests/test_session.py`
用真主站钉住。

**替身不许自成一套真相**：`FakeEngine` 的类型/范围判定直接调真 codec（`encode`），
错误文案也和 `Session.set_point` 逐字对齐（点位名由表格模型统一加，见 point_table.setData）。
两边一旦分叉，界面用例绿着、现场却给出另一种话，等于把模拟器的毛病留给平台调。
"""

from __future__ import annotations

from typing import Protocol

from ..codec.point_codec import encode
from .session import Row, Session


class PointSource(Protocol):
    """点位表模型需要的最小接口。`Session` 与 `FakeEngine` 都满足它。"""

    def rows(self, channel: str) -> list[Row]: ...

    def write(self, channel: str, unit_id: int, area: str, address: int,
              text: str) -> tuple[bool, str]: ...


def session_source(session: Session) -> PointSource:
    """把"真 Session 就是合法数据源"这件事写在类型上，不做任何包装。"""
    return session


class FakeEngine:
    """内存点位源：写进字典、读回字典；编码校验走真 codec，所以错误路径也是真的。

    它**没有**通道启停概念：所有行都用 CSV 初值（`stale` 恒 False），因为"没起"在这里
    不是异常。"起了却没读到"那条路只有真 Session 能演，由 `test_session.py` 的 stale
    用例钉住，不在替身里造假——替身里造出来的灰显会骗过界面用例。
    """

    def __init__(self, project) -> None:
        self.project = project
        self.writes: list[tuple[str, int, str, int, str]] = []
        self.values: dict[tuple[str, int, str, int], str] = {}
        self._rows: list[Row] = []
        for ch in project.channels:
            for dev in ch.devices:
                for pt in dev.points:
                    # 位置参数顺序就是 Row 的契约（tests/test_session.py 钉着）：
                    # 换序会静默把 waveform 当 readonly 之类串成一列看着合法的错值。
                    self._rows.append(Row(ch.name, dev.name, dev.unit_id, pt.name,
                                          pt.area, pt.address, pt.datatype,
                                          pt.word_order, str(pt.initial),
                                          _registers(pt), pt.readonly, pt.waveform))

    def rows(self, channel: str) -> list[Row]:
        out = []
        for row in self._rows:
            if row.channel != channel:
                continue
            text = self.values.get((channel, row.unit_id, row.area, row.address))
            out.append(_with_value(row, text) if text is not None else row)
        return out

    def write(self, channel, unit_id, area, address, text):
        row = next((r for r in self._rows if r.channel == channel
                    and r.unit_id == unit_id and r.area == area
                    and r.address == address), None)
        if row is None:
            # 与 Session.set_point 同一句式：不在点表里就这一句，不许换成 traceback
            return (False, f"{area} address {address} is not in the point table")
        try:
            encode(text, row.datatype, row.word_order)
        except ValueError as exc:
            return (False, str(exc))          # 文案原样交给模型，点位名在那里统一加
        self.writes.append((channel, unit_id, area, address, text))
        self.values[(channel, unit_id, area, address)] = text
        return (True, "")


def _registers(point_or_row, value: str | None = None) -> tuple[int, ...]:
    """编码一个点的当前寄存器。编码不出来就是**空**，不是 0。

    空元组在这里的含义和 `Session._render` 回落初值时的空序列一致：上层拿它判"这一行
    有没有寄存器可显示"，两种容器（tuple/list）并存会让那个判断时好时坏（A–I 表 G）。
    """
    datatype = getattr(point_or_row, "datatype", "")
    word_order = getattr(point_or_row, "word_order", "ABCD")
    text = value if value is not None else str(getattr(point_or_row, "initial", "0"))
    try:
        return tuple(encode(text, datatype, word_order))
    except ValueError:
        return ()


def _with_value(row: Row, text: str) -> Row:
    return Row(row.channel, row.device, row.unit_id, row.name, row.area, row.address,
               row.datatype, row.word_order, text, _registers(row, text),
               row.readonly, row.waveform, row.stale)
