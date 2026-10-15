"""主窗共用的两个小纯函数：状态栏的一行摘要、装载失败的 Issue。

从 `main_window.py` 拆出（2026-10-15 结构优化）：三个 mixin（`window_menus` / `window_edit` /
`window_io`）都要用它们，而它们不能 import 主窗自己（那是循环）。`main_window` 仍然以原名
再导出。
"""

from __future__ import annotations

import pathlib

from ..project.validate import Issue


def _one_line(text: str) -> str:
    """状态栏只给第一行：多行栈留在日志窗里，状态栏撑高就是布局挪动。"""
    return text.splitlines()[0] if text else ""


def _parse_issue(path, exc) -> Issue:
    """装载失败按 parse 类 Issue 报：和 `csv_io_entry` 同一个 code，两处说法一致。"""
    return Issue("error", "parse", pathlib.Path(path).name, str(exc))
