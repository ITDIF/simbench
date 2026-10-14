"""证据落点（`simbench/diagnostics.py`）：崩溃、日志、会话文件。

这几条用例钉的是"现场出问题时能不能带回来东西"：目录口径、日志追加与轮转、崩溃钩子真写盘、
以及"装不上不许拦启动"。全局状态（`sys.excepthook` / `threading.excepthook` / `faulthandler`）
用完一律还原，免得污染后面的用例。
"""

from __future__ import annotations

import datetime as dt
import faulthandler
import sys
import threading

import pytest

from simbench import diagnostics


@pytest.fixture(autouse=True)
def restore_global_hooks():
    """把三个全局钩子原样还回去（它们本来就该是进程级的）。"""
    excepthook, thread_hook = sys.excepthook, threading.excepthook
    was_enabled = faulthandler.is_enabled()
    yield
    sys.excepthook, threading.excepthook = excepthook, thread_hook
    diagnostics._installed.clear()
    if not was_enabled:
        faulthandler.disable()


# ---------- 目录口径 ----------

def test_the_log_dir_follows_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv(diagnostics.LOG_DIR_ENV, str(tmp_path / "elsewhere"))
    assert diagnostics.log_dir() == tmp_path / "elsewhere"
    assert diagnostics.session_log_path() == \
        tmp_path / "elsewhere" / f"simbench-{dt.date.today().isoformat()}.log"


def test_the_log_dir_defaults_under_local_app_data(monkeypatch, tmp_path):
    """没给环境变量时落在用户目录（exe 旁边常常写不进去：U 盘、只读共享盘、Program Files）。"""
    monkeypatch.delenv(diagnostics.LOG_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert diagnostics.log_dir() == tmp_path / "AppData" / "simbench" / "logs"


# ---------- LogFile ----------

def test_the_log_file_appends_lines(tmp_path):
    log = diagnostics.LogFile(tmp_path / "sub" / "s.log").open()
    assert log.broken == ""
    log.append("first")
    log.append("second\n")
    log.close()
    body = (tmp_path / "sub" / "s.log").read_text(encoding="utf-8")
    assert body == "first\nsecond\n"


def test_an_unwritable_log_file_reports_once_and_stays_quiet(tmp_path):
    """写不进去的日志句柄不许把每一次日志调用都变成异常（那是"日志把工具拖死"）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    log = diagnostics.LogFile(blocker / "s.log").open()      # 父目录是个文件：建不出来
    assert "cannot open" in log.broken
    log.append("whatever")                                   # 不抛
    assert "cannot open" in log.broken, "打开失败那条理由不该被后面的写失败盖掉"


def test_a_full_log_file_rotates_to_prev(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_BYTES", 8)
    log = diagnostics.LogFile(tmp_path / "s.log").open()
    log.append("12345678")
    log.close()
    again = diagnostics.LogFile(tmp_path / "s.log").open()
    again.append("fresh")
    again.close()
    assert (tmp_path / "s-prev.log").read_text(encoding="utf-8") == "12345678\n"
    assert (tmp_path / "s.log").read_text(encoding="utf-8") == "fresh\n"


# ---------- 崩溃钩子 ----------

def test_an_uncaught_exception_lands_in_the_log_file(tmp_path):
    log = diagnostics.install_crash_handlers(diagnostics.LogFile(tmp_path / "crash.log"),
                                            version="9.9.9")
    assert log is not None
    try:
        raise ValueError("boom-in-test")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    body = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "simbench 9.9.9 started" in body
    assert "uncaught exception" in body and "boom-in-test" in body
    assert "Traceback (most recent call last)" in body


def test_an_uncaught_exception_in_a_thread_lands_too(tmp_path):
    """引擎线程里炸的那一类：`sys.excepthook` 抓不到，得靠 `threading.excepthook`。"""
    log = diagnostics.install_crash_handlers(diagnostics.LogFile(tmp_path / "crash.log"))
    assert log is not None
    args = threading.ExceptHookArgs((ValueError, ValueError("boom-in-thread"), None,
                                     threading.current_thread()))
    threading.excepthook(args)
    body = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "boom-in-thread" in body and "in thread" in body


def test_the_native_fault_handler_is_enabled_when_it_can_be(tmp_path):
    """段错误这类原生崩溃只剩它能留证据。"""
    log = diagnostics.install_crash_handlers(diagnostics.LogFile(tmp_path / "crash.log"))
    assert log is not None
    assert faulthandler.is_enabled()


def test_an_unwritable_log_file_is_reported_but_never_fatal(tmp_path, capsys):
    """装不上不许拦启动：回 `None`，理由写进 stderr（有 stderr 的话）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert diagnostics.install_crash_handlers(diagnostics.LogFile(blocker / "c.log")) is None
    assert "crash logging is off" in capsys.readouterr().err


def test_installing_twice_keeps_a_reference_to_the_handle(tmp_path):
    """句柄被 GC 掉的话，faulthandler 会往一个已经关掉的 fd 上写——那种失败只在真崩了才暴露。"""
    first = diagnostics.install_crash_handlers(diagnostics.LogFile(tmp_path / "a.log"))
    second = diagnostics.install_crash_handlers(diagnostics.LogFile(tmp_path / "b.log"))
    assert first is not None and second is not None
    assert diagnostics._installed["log"] is second
