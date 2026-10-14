"""现场的"证据落点"：崩溃、Qt 警告、日志页那几百行，都往一个文件里放。

为什么需要（2026-10-14 深度审查）：界面版 exe 是**无控制台**构建，`sys.stdout/stderr` 是
`None`（`packaging/entry.py` 自己写着），任何未捕获的异常、任何 Qt 自己的报错，**没有任何
落点**——现场看到的是"窗口一闪就没了"，回头只能靠复述。而日志页那份文本被
`setMaximumBlockCount` 限住、关窗即失。对一个要去现场抓问题的工具，这等于每次都空手而归。

三条落点：

| 落点 | 装在哪 | 抓什么 |
|---|---|---|
| 会话日志（一天一个文件） | `log_dir()/simbench-YYYY-MM-DD.log` | 日志页里出现的每一行（引擎事件、拒绝、失败） |
| 崩溃钩子（`sys.excepthook` + `threading.excepthook`） | 同上 | 未捕获异常（含引擎线程里的），带完整 traceback |
| `faulthandler` | 同上（同一个文件句柄） | 解释器级崩溃（段错误）时的**原生栈** |

目录口径：`SIMBENCH_LOG_DIR` 显式给就用它（测试与"把日志放到共享盘"都用这一条）；否则
Windows 用 `%LOCALAPPDATA%\\simbench\\logs`，其它平台用 `~/.local/state/simbench/logs`。
选用户目录而不是 exe 旁边：现场那个目录（U 盘、只读共享盘、Program Files）常常写不进去。

**装不上不许拦启动**：日志落点失败（目录只读、磁盘满、组策略拦）是"少了一条证据"，不是
"工具不能用"。所以 `install_crash_handlers()` 失败时回 `None`，把原因写进 stderr（有的话），
继续跑——但绝不静默：返回值就是"装没装上"的证据，界面会在日志页写一行。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import faulthandler
import os
import pathlib
import sys
import threading
import traceback
from typing import IO

LOG_DIR_ENV = "SIMBENCH_LOG_DIR"
# 单份日志的上限。到顶就换一份带序号的（`...-2.log`）：现场连跑几天时，日志不该无限长大，
# 也不该在最关键的时候因为"写不进去"而丢证据。
MAX_BYTES = 8 * 1024 * 1024
_APP_FOLDER = "simbench"


def log_dir() -> pathlib.Path:
    """证据放哪：`SIMBENCH_LOG_DIR` > 平台默认（见模块头）。只算路径，不建目录。"""
    explicit = os.environ.get(LOG_DIR_ENV)
    if explicit:
        return pathlib.Path(explicit)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = pathlib.Path(base) if base else pathlib.Path.home()
        return root / _APP_FOLDER / "logs"
    return pathlib.Path.home() / ".local" / "state" / _APP_FOLDER / "logs"


def session_log_path(day: dt.date | None = None) -> pathlib.Path:
    """当天那一份会话日志的路径（一天一个文件，便于"把昨天那份发我"）。"""
    return log_dir() / f"simbench-{(day or dt.date.today()).isoformat()}.log"


class LogFile:
    """往会话日志里追加行。行缓冲 + 每条 flush：崩溃前那几行才是最要紧的。

    写失败**只报一次**（同一份实例上），随后自己静音：一个写不进去的日志句柄不该把每一次
    日志调用都变成一次异常——那是"日志把工具拖死"的经典形状。
    """

    def __init__(self, path: pathlib.Path | None = None) -> None:
        self.path = pathlib.Path(path) if path is not None else session_log_path()
        self.broken: str = ""
        self._handle: IO[str] | None = None

    def open(self) -> "LogFile":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_too_big()
            self._handle = open(self.path, "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            self.broken = f"{self.path}: cannot open the log file ({exc})"
        return self

    def _rotate_if_too_big(self) -> None:
        with contextlib.suppress(OSError):
            if self.path.is_file() and self.path.stat().st_size >= MAX_BYTES:
                self.path.replace(self.path.with_name(self.path.stem + "-prev.log"))

    def append(self, text: str) -> None:
        """写一行（没带换行就补一个）。任何失败都咽下，但记在 `broken` 里。"""
        if self._handle is None:
            return
        try:
            self._handle.write(text if text.endswith("\n") else text + "\n")
        except OSError as exc:
            self.broken = f"{self.path}: cannot append to the log file ({exc})"
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None

    def close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None

    def __repr__(self) -> str:                       # pragma: no cover - 只为排查好看
        state = "open" if self._handle is not None else "closed"
        return f"<LogFile {self.path} {state} broken={self.broken!r}>"


_installed: dict[str, object] = {}


def install_crash_handlers(log_file: LogFile | None = None,
                           version: str = "") -> LogFile | None:
    """装崩溃钩子。回**真正装上**的那份 `LogFile`（装不上回 `None`，理由在 stderr）。

    三个钩子写的是**同一个文件句柄**：`faulthandler` 要的是 fd（它在信号处理里裸写），
    excepthook 走 `append`。句柄存在 `_installed` 里——被 GC 掉的句柄会让 faulthandler
    写到一个已经关掉的 fd 上（那种失败只在真的崩了那一刻才暴露，等于没装）。
    """
    log = log_file if log_file is not None else LogFile()
    if log._handle is None:                          # 传进来还没开：这一层负责开
        log.open()
    if log._handle is None:
        print(f"[simbench] crash logging is off: {log.broken}", file=sys.stderr)
        return None
    log.append("")
    log.append(f"==== simbench {version or '?'} started "
               f"{dt.datetime.now().isoformat(timespec='seconds')} "
               f"(python {sys.version.split()[0]}, pid {os.getpid()}) ====")
    _installed["log"] = log
    _installed["previous"] = sys.excepthook

    def hook(kind, value, tb) -> None:
        log.append("---- uncaught exception ----")
        log.append("".join(traceback.format_exception(kind, value, tb)).rstrip("\n"))
        previous = _installed.get("previous")
        if callable(previous) and previous is not sys.__excepthook__:
            with contextlib.suppress(Exception):
                previous(kind, value, tb)

    sys.excepthook = hook

    def thread_hook(args) -> None:                   # 引擎线程里炸的那一类
        log.append("---- uncaught exception in thread "
                   f"{getattr(args.thread, 'name', '?')} ----")
        log.append("".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)).rstrip("\n"))

    threading.excepthook = thread_hook
    with contextlib.suppress(OSError, ValueError):
        faulthandler.enable(log._handle)             # 段错误这类原生崩溃只剩它能留证据
    return log
