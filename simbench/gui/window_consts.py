"""主窗的模块级常量（尺寸、刷新节拍、探针与摘要文案）。

拆出来的原因（2026-10-15 结构优化）：`main_window` 与三个 mixin 都要读这些值，放在主窗模块里会让 mixin 反向依赖主窗（循环）。`main_window` 仍然把它们重新导出，老的 `main_window._START_SIZE` 等写法不变。
"""

from __future__ import annotations

from ..engine import ports

_TICK_MS = 100
# 起手尺寸 = 当前屏幕**可用区域**的这个比例（2026-10-14 用户口径：一半偏小，桌上还要摆
# 主站工具/浏览器；2/3 是"看得全 + 还留得下别的东西"那一档）。分子/分母写在一起，
# 免得"一半"这种说法散在注释与用例里各说各的。
_START_SIZE = (2, 3)
_NOTICE_MS = 8000                       # 拒绝理由要在状态栏留够一读的时间，别一闪就没
# 界面上每点一次导入/换工程都会留下一批已经没人引用的 widget；引用环得破，但**只能在
# 这条线程上破**（口径见 spec §13：引擎线程上触发的一次自动回收如果碰到界面刚丢弃的
# QWidget 包装器，shiboken 会在非 GUI 线程上 `~QWidget → QWindow::close()`，而它等的主
# 线程正卡在 `runner.call` 上等引擎回话——互等成环，整个软件冻死）。
_GC_EVERY_S = 5.0
_STATUS_SLOT_WIDTH = 320                # 状态摘要的定宽槽（实测最长两条通道 ≈ 200px）
_LOG_LINES = 5000                       # 日志窗只留最近多少行（与报文窗的 5000 帧同一量级）
# "这个端口现在空着吗"的探针（新建工程 / 添加通道算默认端口时用）。放成模块级常量是为了
# **用例能换掉它**：不换的话，"新建一个 TCP 工程得到 tcp-5020"这类断言就取决于跑测试的
# 机器上 5020 空不空——那种红最难查。
PORT_PROBE = ports.is_free
_TREE_STRETCH, _TABLE_STRETCH = 1, 4
_FIRST_SPLIT = [600, 400]   # 上栏:下栏 首屏比例（见 _build_layout）
_STATUS_WORD = {"stopped": "停止", "starting": "启动中", "running": "运行",
                "error": "失败"}
# 计数式摘要里状态的固定顺序：不按下标排，同一份现况每次渲染才是同一个字符串
_STATUS_ORDER = ("running", "stopped", "error", "starting")
