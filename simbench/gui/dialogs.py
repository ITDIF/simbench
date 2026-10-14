# simbench/gui/dialogs.py
"""校验错误清单 + 工程/CSV 的打开、导入与另存为。

三条口径住在这个文件里：

1. **错误一次看全，不许逐个点**（`IssueDialog` 用一个只读文本框把整张清单摊开，
   error 段在前、warning 段在后：真错被一堆提醒挤下屏就等于没报）。
2. **路径必须跟着工程回给主窗**（已定口径：`open_project` 返回 `(project, path)` 二元组，
   `save_project_as` 返回落盘路径或 None）。"另存为建议哪个文件"和"保存成功后该不该清
   `*`"都只有主窗知道；把路径挂到 project 对象的野生属性上传，下一个改对话框的人一定漏。
3. **读不懂就抛 `ProjectFileError`，不在这里转成 Issue**：装载口的分工是"csv_io/json_io
   只抛领域错误，调用方把它变成清单"（CLI 那一份在 `simbench/__main__.py`，GUI 这一份在
   `MainWindow`）。这里悄悄吞掉错误，界面就会显示一张空的、看起来完全合法的点表。
"""

from __future__ import annotations

import pathlib

from PySide6 import QtWidgets

from ..project import formats


class ProjectFileError(ValueError):
    """文件读不懂，而且知道是哪个文件：主窗据此出清单，消息里必须带文件名。

    继承 `ValueError` 是刻意的——仓库对"读出来了但读不懂"的契约就是 ValueError
    （见 `project/csv_io.py`、`project/json_io.py` 的 docstring），换个基类就等于在 GUI
    这边多开一条只有它认识的异常通道，两份判定迟早漂移。
    """

    def __init__(self, path, message: str) -> None:
        self.path = pathlib.Path(path)
        self.detail = str(message)
        super().__init__(f"{self.path.name}: {self.detail}")


def format_issues(issues) -> list[str]:
    """清单的渲染：error 在前、warning 在后，每条保留 `Issue.__str__` 原样。

    不在这里改写文案：现场会把这行字和 CLI 的输出对着看，两种说法必然引出"哪份是真的"。
    """
    errors = [str(i) for i in issues if i.level == "error"]
    warnings = [f"(warning) {i}" for i in issues if i.level == "warning"]
    return errors + warnings


class IssueDialog(QtWidgets.QDialog):
    """一条错误都不少地列出来；warnings 与 errors 分段，避免真错被淹没。"""

    def __init__(self, issues, parent=None) -> None:
        super().__init__(parent)
        errors = [i for i in issues if i.level == "error"]
        warnings = [i for i in issues if i.level == "warning"]
        self.setWindowTitle(f"点表检查：{len(errors)} 错误 / {len(warnings)} 警告")
        self.resize(760, 420)
        box = QtWidgets.QVBoxLayout(self)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        text.setPlainText("\n".join(format_issues(issues)))
        box.addWidget(text, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        box.addWidget(buttons)


def import_table(parent=None, start_dir: str = ""):
    """导入点表（CSV / Excel，两种都行）只要路径。读文件交给调用方：偏移量是选完文件之后才问的。

    `start_dir` 是"上次用过的目录"（`settings.last_dir`）：现场的点表散在几个目录里，
    每次都从默认目录找起是每天要重复十几次的事。
    """
    path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, "导入点表", start_dir or "",
                                                    formats.table_filter())
    return path or None


def choose_offset(parent=None) -> int:
    """40001 风格点表的整体偏移。默认 0：本工具的地址一律 0-based 裸地址。"""
    value, ok = QtWidgets.QInputDialog.getInt(
        parent, "地址偏移", "点表地址是 4x/3x 惯例写法时填偏移量\n"
                            "（例如 40001 风格填 -40001，让它变成 0-based）",
        0, -65535, 65535, 1)
    return value if ok else 0


def read_point_table(path, address_offset: int = 0):
    """CSV -> `(project, notes)`；`address_offset` 是导入时问出来的整体地址偏移。

    端口不在这里覆盖：`csv_io` 按通道名尾数猜并记一条 note（新格式的导出把 host/port 写在
    名字里，所以只有老点表需要猜），主窗把 note 原样写进日志——"导入时不猜端口"是不可能的，
    能做到的正确姿势是**猜了要说**，和 CLI 同一份口径。
    支持哪些后缀由 `project/formats.py` 那张表说了算（CSV / Excel 两种点表都走这条路）。
    """
    try:
        return formats.read_project(path, address_offset=address_offset)
    except (ValueError, OSError) as exc:
        raise ProjectFileError(path, exc) from exc


def open_project(parent=None, start_dir: str = ""):
    """返回 `(project | None, path | None)`；用户取消就是 `(None, None)`。

    按后缀分派（`project/formats.py` 那张表），与命令行那个入口一一对应。
    CSV 的 notes 在这里丢掉是刻意的：这条路上端口已经是工程的一部分（JSON 里存着，
    CSV 里按通道名猜出来后就摆在通道面板上可编辑），再往日志抄一遍就是噪音；
    需要 note 的是"导入点表"那条路（`read_point_table`）。
    """
    path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, "打开点表 / 工程",
                                                    start_dir or "",
                                                    formats.open_filter())
    if not path:
        return None, None
    try:
        project, _notes = formats.read_project(path)
        return project, path
    except (ValueError, OSError) as exc:
        raise ProjectFileError(path, exc) from exc


def export_table_path(parent=None, suggested: str = "") -> str | None:
    """导出**点表**要落到哪（回 None = 用户取消）。

    两种点表都给（CSV / Excel，见 `project/formats.py`），**后缀决定真写哪一种**。
    新建的工程在落盘之前**没有路径**，导不出点表就等于"新建的工程换不到现场去"，
    而点表正是这个工具跟现场交换数据的方式。原来这里直接拒绝，现在先问一次。
    """
    path, _ = QtWidgets.QFileDialog.getSaveFileName(parent, "导出点表", suggested,
                                                    formats.table_filter())
    return path or None


def save_project_to(project, path) -> pathlib.Path:
    """把工程写回**已经知道的那个路径**（按后缀分派到 json/csv）。

    "保存"与"另存为"是两件事：另存为要问路径（`save_project_as`），保存不该问——一天里
    存十次就弹十次文件名对话框，人就会开始点错（`xx (2).json` 就是这么来的）。
    """
    target = pathlib.Path(path)
    try:
        formats.write_project(project, target)
    except (ValueError, OSError) as exc:
        raise ProjectFileError(target, exc) from exc
    return target


def save_project_as(parent, project, suggested: str = ""):
    """返回落盘路径或 None（用户取消）。路径回传给主窗记住。"""
    path, _ = QtWidgets.QFileDialog.getSaveFileName(
        parent, "保存点表 / 工程", suggested, formats.save_filter())
    if not path:
        return None
    try:
        formats.write_project(project, path)
    except (ValueError, OSError) as exc:
        raise ProjectFileError(path, exc) from exc
    return path
