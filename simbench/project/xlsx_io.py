"""Excel（`.xlsx`）点表读写。**同一套语法与判定**，只是换了个容器。

设计口径（为什么这个文件这么短）：它不自己解析点表，而是**把表格转成 CSV 文本再交给
`csv_io.read_csv`**——通道名里那套 `name@tcp:host:port` 语法、"端口按尾数猜了要说"、
逐行的列数/整数/布尔判定、带行号的报错，一个字都不用重写，两边的语义也不可能漂。

两条与现场 Excel 有关的处理：

* **表头不必在第一行**：现场的表上面常有一行标题（"XX 隧道 点表 V1.2"）。这里找**第一行**
  12 格与表头完全一致的那一行当表头，找不到就把整张表原样交给 `csv_io` 去报它那句
  `header mismatch`（两处说法一致，不另写一套）。找到表头时用**空行补齐**前面几行，
  行号因此与 Excel 里看到的行号一致——报错说"第 7 行"，人去 Excel 第 7 行就能改。
* **单元格只做最小转换**：数字按数字读（`12.5` 不会变成 `12.500000`），Excel 的布尔格
  读成 `true`/`false`（`csv_io` 的白名单认这两个词），空单元格读成空串。

一条**已知代价**（写在这里免得现场当成 bug）：Excel 的数值只有 15 位有效数字，17 位的
uint64（`72623859790382856`）**存不精确**。我们这边读写是逐字的（XML 里就是那串数字），
但用户一旦在 Excel 里改过那一格再存，精度就没了——要传这种值用 CSV 或 `project.json`。
"""

from __future__ import annotations

import csv
import io
import math
import pathlib

from . import csv_io

# Excel 里数值只有 15 位有效数字；超过这个位数的整数不当数字写（免得 Excel 一存就变），
# 与"只在 str(值) 与原文逐字相同才按数字写"是同一条口径的两半。
_EXACT_DIGITS = 15
SHEET_NAME = "点表"


def _openpyxl():
    """延迟 import：`.xlsx` 支持要 openpyxl，没装时给一句带安装提示的话。

    与 S7 那条（`snap7` 没装只影响 S7 通道）同一个姿态：只用 CSV/JSON 的人不该被它拦住。
    """
    try:
        import openpyxl
    except ImportError as exc:                       # pragma: no cover - 装了就进不来
        raise ValueError(
            "reading or writing .xlsx needs openpyxl: pip install openpyxl") from exc
    return openpyxl


def _cell_text(value, notes=None, at: str = "") -> str:
    """单元格 -> 字符串（与 CSV 那一格等价的写法）。

    `notes`/`at` 给"这一格在 Excel 里已经丢过精度"那条提醒用：Excel 的数字只有 15 位有效
    数字，而我们点表里合法存在 17 位的 uint64（累计电量）。现场最可能的路径是"CSV 导进
    Excel 改一改、再存成 .xlsx"——那一步 Excel 自己就把那串数字变成了浮点。这里读出来是
    `7.262385979038285e+16` 这种科学计数串，validate 会拒（"initial … is not an integer"），
    但只说那句现场不知道**为什么**：所以顺手在 note 里点明是 Excel 的精度。
    """
    if value is None:
        return ""
    if isinstance(value, bool):                      # Excel 里的 TRUE/FALSE 是 bool
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        if value.is_integer() and abs(value) < 10 ** _EXACT_DIGITS:
            return str(int(value))                   # 12.0 写成 12（Excel 里常这么存）
        if notes is not None and abs(value) >= 10 ** _EXACT_DIGITS:
            notes.append(f"{at}: Excel 的数字只有 15 位有效数字，这一格已经丢过精度"
                         f"（读到 {value!r}）；要精确传这种值请把单元格改成文本，"
                         f"或者用 CSV/project.json")
        return repr(value)
    return str(value)


def _cell_for(text: str):
    """字符串 -> 单元格的值：**只有逐字往返相同的才按数字写**。

    `str(int(body)) == body` 这一条就是判据：`12`/`-5` 写成数字（Excel 里右对齐、能直接算），
    而 `0008`、`12.50`、`1e5` 这些原样落成文本——否则 Excel 一存就变成 `8`、`12.5`、`100000`，
    那是**静默改点表**（初值那一列尤其不能变）。
    """
    body = text.strip()
    if not body:
        return text
    if body.lstrip("-").isdigit():
        if str(int(body)) == body and len(body.lstrip("-")) <= _EXACT_DIGITS:
            return int(body)
        return text
    try:
        value = float(body)
    except ValueError:
        return text
    if math.isfinite(value) and str(value) == body:
        return value
    return text


def sheet_rows(path, notes: list[str] | None = None) -> list[list[str]]:
    """第一张工作表 -> 二维字符串表（空行也留着：行号要和 Excel 里看到的一致）。

    `notes` 给了就顺手收"某一格丢过精度"的提醒（见 `_cell_text`）。
    """
    openpyxl = _openpyxl()
    workbook = openpyxl.load_workbook(pathlib.Path(path), read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        letter = openpyxl.utils.get_column_letter
        rows = []
        # `read_only=True` 下 iter_rows 给的是 ReadOnlyCell（空单元格是 `EmptyCell`，连
        # `.column` 都没有），所以列号用 enumerate 数，不去问单元格。
        for index, row in enumerate(sheet.iter_rows(), start=1):
            rows.append([_cell_text(cell.value, notes,
                                    f"line {index} (column {letter(column)})")
                         for column, cell in enumerate(row, start=1)])
        return rows
    finally:
        workbook.close()


def _header_index(rows: list[list[str]]) -> int | None:
    """表头在第几行（0-based）；没有就回 None（交给 `csv_io` 报它那句 header mismatch）。"""
    for index, row in enumerate(rows):
        if [cell.strip().lower() for cell in row] == csv_io.HEADER:
            return index
    return None


def read_xlsx_file(path, *, ports: dict[str, int] | None = None, address_offset: int = 0,
                   with_notes: bool = False):
    """`.xlsx` -> 与 `csv_io.read_csv` **完全同形**的返回值（默认 Project，`with_notes` 才带 notes）。"""
    precision: list[str] = []
    rows = sheet_rows(path, precision)
    index = _header_index(rows)
    if index is None:
        # 找不到表头：原样交下去，让 `csv_io` 用它那句"expected …, got …"报（两边同一个句式）
        return csv_io.read_csv(_as_text(rows), ports=ports,
                               address_offset=address_offset, with_notes=with_notes)
    text = _as_text(rows[index:], leading_blank_lines=index)
    project, notes = csv_io.read_csv(text, ports=ports, address_offset=address_offset,
                                     with_notes=True)
    if index:
        notes.insert(0, f"表头在第 {index + 1} 行，前面 {index} 行按标题跳过")
    notes.extend(precision)
    return (project, notes) if with_notes else project


def _as_text(rows: list[list[str]], leading_blank_lines: int = 0) -> str:
    """二维表 -> CSV 文本。**空行补齐**前面几行：CSV 的行号就是 Excel 的行号。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for _ in range(leading_blank_lines):
        writer.writerow([])
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def write_xlsx_file(project, path) -> None:
    """工程 -> `.xlsx`：行形状与 CSV 完全一致（`csv_io.rows_of`），表头冻结、列给个够用的宽度。"""
    openpyxl = _openpyxl()
    rows = csv_io.rows_of(project)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME
    for row in rows:
        sheet.append([_cell_for(cell) for cell in row])
    sheet.freeze_panes = "A2"                        # 表头冻住：几十行往下翻还看得见列名
    for column, width in zip(sheet.columns, _COLUMN_WIDTHS):
        sheet.column_dimensions[column[0].column_letter].width = width
    workbook.save(pathlib.Path(path))


# 列宽只求"打开就能看"：够装下列名与常见内容，不追求与界面表格一致
_COLUMN_WIDTHS = (22, 16, 9, 22, 8, 8, 12, 10, 8, 22, 22, 10, 26, 9)