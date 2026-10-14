"""冻结包的**唯一**入口：两个 exe 共用这一份代码，靠"有没有控制台"分派。

为什么不是两个入口脚本：PyInstaller 一个 spec 里的多个 `Analysis` 会把 PySide6 整棵依赖树
各分析一遍（构建时间翻倍、两个 COLLECT 也会各存一份几百 MB 的 Qt）；同一次分析里建两个
`EXE`（只差 `console=`）才是"共用一份运行时、只有黑框不一样"的最小代价。代价是入口得自己
判断"我是哪一个"，判据见 `_is_gui_exe`。

源码运行时（`python packaging/entry.py …`）走的是 `main()`，与 `python -m simbench` 完全一致。
"""

from __future__ import annotations

import pathlib
import sys


def _is_gui_exe() -> bool:
    """这个进程是不是"开界面"那一个。

    主判据是**有没有控制台**：spec 里 `simbench-gui` 是 `console=False`（WINDOWS 子系统），
    PyInstaller 在无控制台时把 `sys.stdout`/`sys.stderr` 置成 `None`，而 `simbench.exe` 是
    控制台构建、两个流都在。副判据是 exe 名字以 `-gui` 结尾——万一有人把某个构建的控制台
    开关改反了，名字仍然按界面走。两条都指向"开界面"，不冲突。

    顺带一句：无控制台那个 exe 里 `print` 是**静默丢弃**（`sys.stdout is None` 时 CPython 的
    `print` 直接返回），所以要看文字结果（`--check` / `--list` / 监听日志）得用 `simbench.exe`。
    """
    if getattr(sys, "stdout", None) is None or getattr(sys, "stderr", None) is None:
        return True
    return pathlib.Path(sys.executable).stem.lower().endswith("-gui")


def main() -> int:
    from simbench.__main__ import gui_main
    from simbench.__main__ import main as cli_main

    if _is_gui_exe():
        return gui_main()
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())