"""点表/工程的落盘：**先写临时文件，再原子替换**，覆盖前留一份 `.bak`。

为什么要这一层（2026-10-14 深度审查）：原来两条保存都是 `Path(path).write_text(...)`，
也就是"先把目标文件截断成 0 字节，再往里写"。写到一半断电、磁盘满、进程被杀——现场唯一那份
点表就废了，连"半份"都不是。`net/ledger.py` 早就用了"临时文件 + `os.replace`"的写法（注释里
写着"半截文件不会留在地上"），这一层把它收成一份，点表 / 工程 / 网络声明 / 网络台账都走它。

五条口径：

* **备份是 best-effort**：覆盖已有文件时先把它复制成 `<原名>.bak`；备份失败（目录只读、
  磁盘满）**不拦保存**——保存本身才是用户的目的，备份是保险；
* **不搬原文件**：临时文件写在**同一个目录**里（跨盘 `rename` 会退化成复制，就丢了原子性）；
* **失败要留原样**：写的过程炸了，目标文件必须还是**旧内容**，临时文件清掉，不留 `.tmp`；
* **换行与编码由调用方说**：CSV 那条路必须显式给 `newline=""`——它自己的行尾就是 `\n`，
  再让平台翻译一次会变成 `\r\n`（"同一份工程在两台机器上产出不同字节"）；
* **被别的程序占着要给一句人话**：Windows 上目标被 Excel 之类锁住时 `os.replace` 抛
  `PermissionError`（WinError 5），照原样抛出去现场只看到一句 "Access is denied"，所以这里
  翻成"文件可能正被另一个程序打开着"。

一条使用提醒（`.xlsx` 的备份）：Excel 与 openpyxl 都**按后缀**认格式，所以 `x.xlsx.bak`
双击打不开——那不是文件坏了，把后缀改回 `.xlsx` 就能开（内容就是一个完整的工作簿）。
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import tempfile
from collections.abc import Callable
from typing import Any, IO


def backup_path(path) -> pathlib.Path:
    """这一份文件的备份名：`x.csv` -> `x.csv.bak`（**追加**后缀，不是 `with_suffix`）。

    追加而不是替换后缀：`x.csv` 与 `x.xlsx` 的备份都会变成 `x.bak`，两种格式互相盖掉对方。
    """
    target = pathlib.Path(path)
    return target.with_name(target.name + ".bak")


def _humanise(exc: OSError, target: pathlib.Path) -> OSError:
    """把"写不动"翻成现场能行动的一句话（原始 OSError 留在 `__cause__` 里）。"""
    if isinstance(exc, PermissionError):
        return PermissionError(
            f"{target}：写不进去（{exc.strerror or exc}）。文件可能正被另一个程序打开着"
            f"（Excel、记事本、杀毒软件都可能），关掉它再存一次；这次保存没有改动原文件")
    return exc


def _keep_backup(target: pathlib.Path) -> None:
    """best-effort 备份：失败一律咽下（保存才是目的），但不许把异常带给调用方。"""
    with contextlib.suppress(OSError):
        shutil.copy2(target, backup_path(target))


def write_atomic(path, write_into: Callable[[Any], None], *, binary: bool = True,
                 backup: bool = True, encoding: str = "utf-8",
                 newline: str | None = None) -> pathlib.Path:
    """把 `write_into(handle)` 写的东西**原子地**落到 `path`，返回实际写入的路径。

    `binary=True` 给 openpyxl 那一类（`workbook.save(handle)` 要二进制流）；文本走
    `write_text_atomic`（它把编码与换行的口径收在一处）。
    """
    target = pathlib.Path(path)
    folder = target.parent if str(target.parent) else pathlib.Path(".")
    if backup and target.exists():
        _keep_backup(target)
    handle_fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                           dir=str(folder))
    tmp = pathlib.Path(tmp_name)
    try:
        if binary:
            handle: IO = os.fdopen(handle_fd, "wb")
        else:
            handle = os.fdopen(handle_fd, "w", encoding=encoding, newline=newline)
        with handle:
            write_into(handle)
        os.replace(tmp, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise _humanise(exc, target) from exc
    except BaseException:
        # 写的过程里抛了别的（编码错误、openpyxl 自己的异常）：临时文件不许留在地上
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def write_text_atomic(path, text: str, *, encoding: str = "utf-8",
                      newline: str | None = None, backup: bool = True) -> pathlib.Path:
    """文本版：`newline` 与 `Path.write_text` 同义，**默认 `None`**（= 平台默认换行）。"""
    return write_atomic(path, lambda handle: handle.write(text), binary=False,
                        backup=backup, encoding=encoding, newline=newline)


def write_bytes_atomic(path, data: bytes, *, backup: bool = True) -> pathlib.Path:
    """字节版（暂时只有测试与将来的二进制格式用得上）。"""
    return write_atomic(path, lambda handle: handle.write(data), binary=True,
                        backup=backup)
