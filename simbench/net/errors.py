"""net 的异常类型：**叶子模块**，不 import 任何东西。

为什么要单独一个文件（2026-10-16）：`NetError` 原住在 `state.py` 里，而 `state.py` 顶层
`import ctypes` / `import subprocess`（它是"跟系统打交道"的那一层）。结果是——凡是只需要
**指认这类错误**的模块都被迫把系统层拖进来：

* `endpoints.py` 的文档串写着"纯函数，不读系统"，却为了 `except NetError` 而 import `state`；
* `spec.py` 只是一个文件格式模块，同样如此。

放成叶子之后，`endpoints.py` / `spec.py` 真的只依赖点表与标准库；`state.py` 也从这个文件取
它自己抛的那个类型。改名的连带成本就是这一句话：**谁要用 `NetError`，就从这个文件取。**
"""

from __future__ import annotations


class NetError(RuntimeError):
    """读系统网络状态失败。消息是给人看的一句中文，不带 traceback。"""
