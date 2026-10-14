"""版本号的**唯一**来源。

为什么单独一个文件（2026-10-14 深度审查）：这之前版本号有三处各说各话——
`packaging/version_info.txt` 里写死 `1.0.0.0`、`simbench/__init__.py` 里没有 `__version__`、
CHANGELOG 也不存在。于是"这个 exe 是哪个版本"只能靠文件时间猜，而现场报问题时第一个要问的
就是这一句。

口径：**只在这里改**。`packaging/version_info.txt`（exe 属性页）、`simbench --version`、
GUI 标题、崩溃日志的头，全部从这一处取值或与它比对（`tests/test_version.py` 钉住）。
"""

from __future__ import annotations

__version__ = "1.0.0"
