"""核对结果的呈现与导出：文本给控制台/日志页，CSV/Excel 给现场。

为什么不走 `project/formats.py` 那张格式表：那张表管的是**点表**（12 列的输入格式），
这份是**报告**（核对结论），列不一样、也不该能被当点表导入回来——混进去的话，用户会
拿一份报告去"导入点表"，而那份东西恰好长得像点表（12 列）时会静默读进一堆垃圾。
所以这里自己定列，只共用"落盘要原子 + 备份"那条（`project/fsio.py`）。
"""

from __future__ import annotations

import csv
import io
import pathlib

from ..project import fsio
from .scan import VerifyRow

HEADER = ("channel", "device", "unit_id", "name", "area", "address", "datatype",
          "expected", "actual", "status", "detail")
STATUS_LABELS = {"ok": "对得上", "mismatch": "对不上", "no_unit": "没应答", "error": "读出错"}


def rows_of(rows: list[VerifyRow]) -> list[list[str]]:
    """报告的表形状：第一行表头，一行一个点位（CSV 与 Excel 共用同一份，不许各写一遍）。"""
    out = [list(HEADER)]
    for row in rows:
        out.append([row.channel, row.device, str(row.unit_id), row.name, row.area,
                    str(row.address), row.datatype, row.expected, row.actual,
                    row.status, row.detail])
    return out


def to_text(rows: list[VerifyRow], limit: int = 0) -> str:
    """给人看的一段：先一行计数，再逐条列"不是 ok 的那些"（对得上的不刷屏）。

    `limit` 给了就只列前 N 条（界面里那块框有限，命令行走这里之外的分支）。
    """
    counts = dict.fromkeys(STATUS_LABELS, 0)
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    head = ("核对完成：" + "，".join(f"{STATUS_LABELS[status]} {counts[status]}"
                                    for status in ("ok", "mismatch", "no_unit", "error")
                                    if counts.get(status)))
    bad = [row for row in rows if row.status != "ok"]
    if not bad:
        return head
    lines = [head, ""]
    for row in (bad[:limit] if limit else bad):
        where = f"{row.channel}/{row.device}/{row.name} ({row.area} {row.address})"
        lines.append(f"  {STATUS_LABELS[row.status]}：{where} 期望 {row.expected}，"
                     f"读到 {row.actual}" + (f"（{row.detail}）" if row.detail else ""))
    if limit and len(bad) > limit:
        lines.append(f"  …… 还有 {len(bad) - limit} 条，见导出的报告")
    return "\n".join(lines)


def to_csv(rows: list[VerifyRow]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows_of(rows):
        writer.writerow(row)
    return buf.getvalue()


def write(path, rows: list[VerifyRow]) -> pathlib.Path:
    """按后缀导出一份报告（`.csv` / `.xlsx`）。"""
    target = pathlib.Path(path)
    suffix = target.suffix.lower()
    if suffix == ".csv":
        return fsio.write_text_atomic(target, to_csv(rows), newline="")
    if suffix == ".xlsx":
        return _write_xlsx(target, rows)
    raise ValueError(f"报告只支持 .csv 与 .xlsx，拿到的是 {suffix or target.name!r}")


def _write_xlsx(target: pathlib.Path, rows: list[VerifyRow]) -> pathlib.Path:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "核对"
    for row in rows_of(rows):
        sheet.append(row)
    sheet.freeze_panes = "A2"
    return fsio.write_atomic(target, workbook.save, binary=True)
