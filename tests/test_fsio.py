"""落盘的原子性（`simbench/project/fsio.py`）。

判据全是"某一刻盘上的字节"，不是"函数返回了什么"——原子写的价值只体现在盘上：写到一半失败，
现场那份点表必须还是**上一次**的那一份，而且不许留下半个临时文件。

这一组同时钉住三条最容易在重构里丢掉的细节：

1. **`.bak` 是追加后缀**（`x.csv` -> `x.csv.bak`），不是替换后缀——否则 `.csv` 与 `.xlsx`
   会共用 `x.bak`，两种格式互相盖掉对方的备份；
2. **备份失败不许拦保存**：备份是保险，保存才是目的；
3. **CSV 的行尾不随平台变**（`newline=""`），所以换行交给谁翻译必须写死在 `write_csv_file` 里。
"""

from __future__ import annotations

import pathlib

import pytest

from simbench.project import csv_io, formats, fsio
from simbench.project.model import Device, Point, Project, TcpChannel


def example() -> Project:
    return Project(channels=[TcpChannel("tcp-5020", port=5020, devices=[
        Device("D1", 11, [Point("温度", "hr", 0, "uint16", initial="25")])])])


def leftovers(folder: pathlib.Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_the_first_write_leaves_no_backup(tmp_path):
    """原来没有这份文件：写下去就是新文件，不该凭空多出一份 `.bak`。"""
    target = tmp_path / "a.csv"
    fsio.write_text_atomic(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.csv"]


def test_overwriting_keeps_the_previous_content_as_bak(tmp_path):
    """覆盖时留一份**上一次**的内容：这是"我刚刚存坏了"唯一能回退的地方。"""
    target = tmp_path / "a.csv"
    fsio.write_text_atomic(target, "first\n")
    fsio.write_text_atomic(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert fsio.backup_path(target).read_text(encoding="utf-8") == "first\n"
    # 再存一次：备份跟着走（只留一代，不滚雪球）
    fsio.write_text_atomic(target, "third\n")
    assert fsio.backup_path(target).read_text(encoding="utf-8") == "second\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.csv", "a.csv.bak"]


def test_the_backup_name_appends_the_suffix_not_replaces_it():
    """`x.csv` 与 `x.xlsx` 的备份不许撞在一起（`with_suffix` 就会撞）。"""
    assert fsio.backup_path("x.csv").name == "x.csv.bak"
    assert fsio.backup_path("x.xlsx").name == "x.xlsx.bak"


def test_a_failure_mid_write_keeps_the_old_file_and_leaves_no_tmp(tmp_path, monkeypatch):
    """写一半炸了：目标文件还是旧内容，且目录里不许留下半个临时文件。"""
    target = tmp_path / "a.csv"
    fsio.write_text_atomic(target, "good\n")

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsio.os, "replace", boom)
    with pytest.raises(OSError):
        fsio.write_text_atomic(target, "half-written\n")
    assert target.read_text(encoding="utf-8") == "good\n"
    assert leftovers(tmp_path) == []


def test_a_failed_backup_does_not_block_the_save(tmp_path, monkeypatch):
    """备份失败（目录只读、磁盘满）不许拦保存：保存是目的，备份是保险。"""
    target = tmp_path / "a.csv"
    fsio.write_text_atomic(target, "first\n")

    def boom(*args, **kwargs):
        raise OSError(13, "Access is denied")

    monkeypatch.setattr(fsio.shutil, "copy2", boom)
    fsio.write_text_atomic(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert fsio.backup_path(target).exists() is False       # 备份没成，但保存成了


def test_a_locked_target_is_reported_in_plain_words(tmp_path, monkeypatch):
    """目标被别的程序占着（Windows 上 Excel 锁文件）：不许只甩一句 WinError。"""
    target = tmp_path / "a.csv"
    fsio.write_text_atomic(target, "first\n")

    def locked(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(fsio.os, "replace", locked)
    with pytest.raises(PermissionError) as excinfo:
        fsio.write_text_atomic(target, "second\n")
    message = str(excinfo.value)
    assert "打开" in message and "Access is denied" in message
    assert "没有改动原文件" in message


def test_csv_bytes_keep_lf_wherever_they_are_written(tmp_path):
    """CSV 的行尾是它自己那份合同（`\n`）：不许因为改了落盘方式就变成平台换行。"""
    target = tmp_path / "a.csv"
    csv_io.write_csv_file(example(), target)
    raw = target.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")


@pytest.mark.parametrize("suffix", [".csv", ".xlsx", ".json"])
def test_every_format_saves_atomically_with_a_backup(tmp_path, suffix):
    """三种格式走的是同一条路：第二次保存留 `.bak`，目标文件仍然能读回同一份工程。"""
    target = tmp_path / f"p{suffix}"
    formats.write_project(example(), target)
    first = target.read_bytes()
    formats.write_project(example(), target)
    assert fsio.backup_path(target).read_bytes() == first
    back, _notes = formats.read_project(target)
    assert csv_io.rows_of(back) == csv_io.rows_of(example())
    assert leftovers(tmp_path) == []


def test_the_xlsx_backup_is_itself_a_readable_workbook(tmp_path):
    """`.xlsx` 的备份不能是"半截的 zip"：它得**真能打开**（否则等于没备份）。

    顺带钉住一条容易踩的事实：Excel 与 openpyxl 都按**后缀**认格式，`.bak` 那个名字双击
    打不开（不是文件坏了）——改回 `.xlsx` 就行。所以这里两种读法都验：按文件对象读（证明
    内容完好）、复制成 `.xlsx` 再按路径读（证明现场那条"改个名就能用"的路走得通）。
    """
    import shutil

    import openpyxl

    target = tmp_path / "p.xlsx"
    formats.write_project(example(), target)
    formats.write_project(example(), target)
    backup = fsio.backup_path(target)
    with open(backup, "rb") as handle:
        assert openpyxl.load_workbook(handle).active["A1"].value == "channel"
    renamed = tmp_path / "restored.xlsx"
    shutil.copy2(backup, renamed)
    assert openpyxl.load_workbook(renamed).active["A1"].value == "channel"
