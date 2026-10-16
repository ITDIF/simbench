"""标准按钮说中文：`gui/i18n.py` 那份翻译器装得上、缺文件也不炸。

为什么值得单独钉（2026-10-17 现场反馈"弹窗按钮有些还是英文"）：`OK` / `Cancel` / `Yes` /
`No` 这些按钮的文字**不来自我们的代码**，而来自 Qt 的 `qtbase_zh_CN.qm`。没有用例的话，
"哪天有人把安装那一步挪走"在界面上只表现成"按钮又变回英文"——那是典型的没人会写断言的回归。
"""

from __future__ import annotations

from PySide6 import QtWidgets

from simbench.gui import i18n


def save_discard_cancel() -> list[str]:
    box = QtWidgets.QMessageBox()
    box.setStandardButtons(QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                           | QtWidgets.QMessageBox.Cancel)
    return [box.button(b).text() for b in (QtWidgets.QMessageBox.Save,
                                           QtWidgets.QMessageBox.Discard,
                                           QtWidgets.QMessageBox.Cancel)]


def test_the_standard_buttons_speak_chinese(qapp):
    """装上之后三处标准按钮都是中文（这三处正是界面里会弹出来的那三种）。"""
    assert i18n.install_chinese_ui(qapp) is True, \
        "PySide6 自带的 qtbase_zh_CN.qm 没找到——装了它按钮才是中文"
    assert save_discard_cancel() == ["保存", "丢弃", "取消"]

    box = QtWidgets.QMessageBox()
    box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
    assert box.button(QtWidgets.QMessageBox.Yes).text().startswith("是")
    assert box.button(QtWidgets.QMessageBox.No).text().startswith("否")

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    assert buttons.button(QtWidgets.QDialogButtonBox.Ok).text() == "确定"
    assert buttons.button(QtWidgets.QDialogButtonBox.Cancel).text() == "取消"


def test_installing_twice_changes_nothing(qapp):
    """幂等：装过再装一次不报错、也不叠第二份（叠了不影响显示，但那是没人看得见的垃圾）。"""
    assert i18n.install_chinese_ui(qapp) is True
    before = save_discard_cancel()
    assert i18n.install_chinese_ui(qapp) is True
    assert save_discard_cancel() == before


def test_a_missing_translation_file_only_means_english_buttons(monkeypatch, tmp_path, qapp):
    """翻译文件不在（裁剪过的 Qt、有人删了 translations 目录）：回 `False`，**不抛**。

    宁可按钮退回英文（界面照旧能用），也不许因为一个文案让窗口起不来。
    """
    monkeypatch.setattr(i18n, "_installed", None)
    monkeypatch.setattr(i18n, "_folders", lambda: [str(tmp_path)])
    assert i18n.install_chinese_ui(qapp) is False
