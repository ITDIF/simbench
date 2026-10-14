"""依赖文件的界线：运行时一份、开发/测试/打包一份。

为什么值得一条用例：`requirements.txt` 是现场照着敲的那一条命令（README 第一段就是它）。
混进 pytest / pys7 / pyinstaller 之后，现场会装上三样永远用不到的东西——其中 pyinstaller
还带一堆构建期依赖。反过来，开发机忘了装 pyinstaller 时 `build.ps1` 会当场说清楚
（不在这一份里，那条检查在脚本里）。
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = (ROOT / "requirements.txt").read_text(encoding="utf-8")
DEV = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

TEST_ONLY = ("pytest", "pys7", "pyinstaller")
RUNTIME_NEEDS = ("pymodbus", "pyserial", "PySide6", "python-snap7", "setuptools")


def _requirements(text: str) -> list[str]:
    """一行一条的依赖名（去注释、去空行、去 `-r` 那种指令行）。"""
    out = []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if body and not body.startswith("-"):
            out.append(body.split("=")[0].split(">")[0].split("<")[0].strip())
    return out


def test_the_runtime_file_carries_only_what_the_shop_needs():
    names = _requirements(RUNTIME)
    for need in RUNTIME_NEEDS:
        assert need in names, (need, names)
    for unwanted in TEST_ONLY:
        assert unwanted not in names, (unwanted, names)


def test_the_dev_file_pulls_the_runtime_ones_in_and_adds_the_toolchain():
    assert "-r requirements.txt" in DEV
    names = _requirements(DEV)
    for tool in TEST_ONLY:
        assert tool in names, (tool, names)
    for tool in ("ruff", "mypy"):                    # 静态检查那一对也在里面（CI 与本地同一版）
        assert tool in names, (tool, names)


def test_every_direct_dependency_is_pinned_to_one_version():
    """直接依赖一律 `==`：`>=` 会让"半年后在另一台机器上装到另一套东西"。

    传递依赖不在这里管——它们由 `requirements.lock.txt` 兜（想一模一样复现就用那一份）。
    """
    for line in (RUNTIME + DEV).splitlines():
        body = line.split("#", 1)[0].strip()
        if not body or body.startswith("-"):
            continue
        assert "==" in body, f"这一条没钉版本：{body}"


def test_the_lock_file_agrees_with_the_pinned_lines():
    """锁文件是 `pip freeze` 的原文：直接依赖那几行的版本必须与 requirements 一致。"""
    lock = {name.lower(): version
            for name, version in (line.strip().split("==", 1)
                                  for line in (ROOT / "requirements.lock.txt")
                                  .read_text(encoding="utf-8").splitlines()
                                  if "==" in line and not line.startswith("#"))}
    for line in (RUNTIME + DEV).splitlines():
        body = line.split("#", 1)[0].strip()
        if "==" not in body:
            continue
        name, version = body.split("==", 1)
        # 包名大小写不同：pip 自己会归一化，这里是同一件事
        assert lock.get(name.strip().lower()) == version.strip(), \
            f"{name} 在锁文件里是 {lock.get(name.strip().lower())}，requirements 里是 {version}"
