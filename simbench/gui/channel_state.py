"""通道四态的**界面说法**：符号、中文、以及摘要里的固定顺序。**一处定义**。

为什么收成一处（2026-10-15 结构整理）：四态（`stopped / starting / running / error`，
源头是 `backend/events.ChannelState`）原先在三张表里各写了一遍——`channel_panel.STATUS_LABELS`
（"○ 停止"）、`device_tree._STATUS_MARK`（"○"）、`window_consts._STATUS_WORD`（"停止"），
而 `STATUS_LABELS` 正好就是"符号 + 空格 + 中文"。加第五个状态要同时改三处，漏一处的表现
是"某个通道的状态格空着"——界面上分不出那是没实现还是没状态。

颜色**不在这里**：它按深浅模式各配一套，归 `theme.status_colors()`；两张表的键一致这件事
由 `tests/test_gui_theme.py` 钉着。
"""

from __future__ import annotations

# 通道面板那一列用的符号。定宽列，**一格宽**：换状态不改列宽（口径 3）。
STATUS_MARK = {"stopped": "○", "starting": "◐", "running": "●", "error": "▲"}

# 状态栏与摘要用的中文。与 `STATUS_MARK` 一一对应，`STATUS_LABELS` 由两者合成。
STATUS_WORD = {"stopped": "停止", "starting": "启动中", "running": "运行",
               "error": "失败"}

# 通道面板那一格显示的文字（符号 + 中文）。原来手写一份在 `channel_panel.py`：
# 手写两份的代价是"符号改了、中文没改"这种错不会被任何一处代码发现。
STATUS_LABELS = {state: f"{STATUS_MARK[state]} {STATUS_WORD[state]}"
                 for state in STATUS_MARK}

# 计数式摘要里的固定顺序（`main_window._summary`）：不按下标排，同一份现况每次渲染
# 才是同一个字符串。
STATUS_ORDER = ("running", "stopped", "error", "starting")
