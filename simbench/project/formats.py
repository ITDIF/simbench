"""点表的读写格式表：**一处登记"什么后缀用什么读写函数"**。

为什么要它（2026-10-14）："`.json` 与其余按 CSV"这套分派原来在**四处**各写一遍
（`csv_io_entry.load_point_table`、`dialogs.open_project` / `save_project_to` /
`save_project_as`），外加三个文件对话框的过滤器字符串——加一种格式要改六处，而漏一处的
表现是"打开能读、保存却写成了另一种格式"。现在只有这一张表：**加格式 = 加一行 + 一个模块**。

口径（三条，与既有两条路一致）：

* 读：读不懂抛 `ValueError`（调用方转成 `Issue` / 人话），文件读不到抛 `OSError`；
* 写：工程表示不了抛 `ValueError`，盘上写不动抛 `OSError`；
* **后缀不认识就拒绝**（`FormatError`，`ValueError` 的子类）：猜一个格式等于把 JSON 内容
  当 CSV 解析，报出来的话离真原因很远。

`read()` 一律回 `(Project, notes)`：CSV/Excel 那条路会带"端口按名字尾数猜的""表头不在第一行
"这类提醒，`project.json` 没有（它自带全部信息，`ports`/`address_offset` 对它无效——调用方
的端口覆盖在装载之后另有一层，见 `__main__._apply_overrides`）。
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass

from . import csv_io, json_io, xlsx_io
from .model import Project


class FormatError(ValueError):
    """后缀不在表里。继承 `ValueError`：调用方那套"读不懂就是 ValueError"的处理照旧管用。"""


def _read_csv(path, *, ports=None, address_offset: int = 0):
    return csv_io.read_csv_file(path, ports=ports, address_offset=address_offset,
                                with_notes=True)


def _read_xlsx(path, *, ports=None, address_offset: int = 0):
    return xlsx_io.read_xlsx_file(path, ports=ports, address_offset=address_offset,
                                  with_notes=True)


def _read_json(path, *, ports=None, address_offset: int = 0):
    # 工程文件自带端口与地址：`ports`/`address_offset` 对它无效（不是"忽略"，是它不适用）
    return json_io.load(path), []


def _write_csv(project: Project, path) -> None:
    csv_io.write_csv_file(project, path)


def _write_xlsx(project: Project, path) -> None:
    xlsx_io.write_xlsx_file(project, path)


def _write_json(project: Project, path) -> None:
    json_io.save(project, str(path))


@dataclass(frozen=True)
class Format:
    suffix: str                                   # 规范后缀，含点：`.csv`
    label: str                                    # 文件对话框里那一行的文字
    read: Callable[..., tuple[Project, list[str]]]
    write: Callable[[Project, pathlib.Path], None]
    kind: str                                     # `"table"`（点表）或 `"project"`（整份工程）


# 顺序就是文件对话框里过滤器呈现的顺序：点表两种在前（现场用得最多），工程在后
FORMATS: tuple[Format, ...] = (
    Format(".csv", "CSV 点表 (*.csv)", _read_csv, _write_csv, kind="table"),
    Format(".xlsx", "Excel 点表 (*.xlsx)", _read_xlsx, _write_xlsx, kind="table"),
    Format(".json", "工程文件 (*.json)", _read_json, _write_json, kind="project"),
)
_BY_SUFFIX = {fmt.suffix: fmt for fmt in FORMATS}


def format_for(path) -> Format:
    """按后缀取格式；不在表里抛 `FormatError`（连带把"支持哪些"写进消息）。"""
    suffix = pathlib.Path(path).suffix.lower()
    found = _BY_SUFFIX.get(suffix)
    if found is None:
        known = "、".join(fmt.suffix for fmt in FORMATS)
        raise FormatError(f"unsupported file type {suffix or pathlib.Path(path).name!r}; "
                          f"supported: {known}")
    return found


def kind_of(path) -> str | None:
    """按后缀回格式类别（"table" 点表 / "project" 工程）；不认识回 None。

    菜单里的"工程 / 点表"分组只靠这一条判据：后缀表是唯一登记处，这里不再抄一张
    .json 清单——以后加第四种格式，分组自动跟着对。
    """
    found = _BY_SUFFIX.get(pathlib.Path(path).suffix.lower())
    return None if found is None else found.kind


def read_project(path, *, ports=None, address_offset: int = 0) -> tuple[Project, list[str]]:
    """读一份点表/工程，回 `(Project, notes)`。"""
    return format_for(path).read(pathlib.Path(path), ports=ports,
                                 address_offset=address_offset)


def write_project(project: Project, path) -> pathlib.Path:
    """按后缀写一份点表/工程，回实际写入的路径。"""
    target = pathlib.Path(path)
    format_for(target).write(project, target)
    return target


def open_filter() -> str:
    """打开/导入用的过滤器：点表与工程都能开。"""
    return ";;".join([fmt.label for fmt in FORMATS] + ["所有文件 (*)"])


def save_filter() -> str:
    """另存为用的过滤器：工程在最前（那是保存一份工程的正解），点表两种跟在后面。"""
    order = [fmt for fmt in FORMATS if fmt.kind == "project"] + \
            [fmt for fmt in FORMATS if fmt.kind == "table"]
    return ";;".join([fmt.label for fmt in order] + ["所有文件 (*)"])


def table_filter() -> str:
    """只导出**点表**时用的过滤器（工程文件不是导出目标）。"""
    return ";;".join([fmt.label for fmt in FORMATS if fmt.kind == "table"]
                     + ["所有文件 (*)"])
