"""版本号只有一处来源（`simbench/__about__.py`）。

为什么要一条用例：这之前版本号在三个地方各说各话——`packaging/version_info.txt` 里写死
`1.0.0.0`、`simbench/__init__.py` 里根本没有 `__version__`、CHANGELOG 不存在。现场报问题时
第一个要问的就是"你那个 exe 是哪个版本"，而 exe 属性页那份（`version_info.txt`）一旦和代码
漂开，答案就是错的——那是**发出去的**东西，改不了。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

from simbench.__about__ import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
INFO = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")


def test_the_exe_version_resource_matches_the_code():
    """`packaging/version_info.txt` 是 exe 属性页里那一份：必须与代码同源。"""
    numbers = re.search(r"filevers=\((\d+), (\d+), (\d+), (\d+)\)", INFO)
    assert numbers, "version_info.txt 里找不到 filevers"
    packed = ".".join(numbers.groups()[:3])
    assert packed == __version__, f"exe 资源写的是 {packed}，代码里是 {__version__}"
    assert f"FileVersion', '{__version__}.0'" in INFO, "FileVersion 字符串那一处没跟上"


def test_the_command_line_reports_the_same_version():
    """`--version` 是现场最快的一问（不用开窗口）。"""
    out = subprocess.run([sys.executable, "-m", "simbench", "--version"],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    assert out.stdout.strip() == f"simbench {__version__}"


def test_the_version_looks_like_a_version():
    """三位数字：够用又不会被"1.0.0.0 vs 1.0.0"这种漂移咬到。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_there_is_a_changelog_and_it_mentions_this_version():
    """发布必须留下"这一版改了什么"：现场升级前会先看它。"""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {__version__}" in changelog, "CHANGELOG 里没有当前版本的条目"
