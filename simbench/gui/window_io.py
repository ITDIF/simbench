"""主窗的文件动作：导入/导出点表、打开/保存工程、保存日志、离开前确认（从 main_window 拆出）。"""

from __future__ import annotations

import datetime as dt
import pathlib

from PySide6 import QtWidgets

from ..project import formats
from ..project.csv_io import port_from_name
from ..project.fsio import write_text_atomic
from ..project.model import channel_transport
from .busy import busy
from .dialogs import (
    FILE_DIALOG_OPTION,
    ProjectFileError,
    export_table_path,
    read_point_table,
    save_project_to,
)
from .window_common import _parse_issue
from .window_consts import (
    _NOTICE_MS,
)


class IoMixin:
    def _import_table(self) -> None:
        if not self._confirm_discard():
            return
        path = self.deps.import_table(self, self._start_dir())
        if not path:
            return
        offset = self.deps.choose_offset(self)
        try:
            with busy("正在读取点表…"):
                project, notes = read_point_table(path, offset)
        except ProjectFileError as exc:
            self._show_issues([_parse_issue(exc.path, exc.detail)])
            return
        self._remember_dir(path)
        # 偏移跟着这条历史记录一起存：从"最近打开"再点开同一份表时按同一个偏移读
        self.use_project(project, path, notes=notes, recent_offset=offset)

    def _open_project(self) -> None:
        if not self._confirm_discard():
            return
        try:
            project, path = self.deps.open_project(self, self._start_dir())
        except ProjectFileError as exc:
            self._show_issues([_parse_issue(exc.path, exc.detail)])
            return
        if project is None:
            return                                 # 用户取消：什么都没发生
        self._remember_dir(path)
        self.use_project(project, path)

    def _save_project(self, ask: bool = False) -> str | None:
        """保存。`ask=True` 是"另存为"（总要问路径）；否则**有路径就直接写回**。

        原来无条件走 `save_project_as`：已经存过、路径都知道了，再点一次"保存"还是弹
        文件名对话框——一天存十次就问十次，人开始点错（`xx (2).json` 就是这么来的）。
        没有路径时才问（新建的工程第一次保存）。
        """
        try:
            if self._path is not None and not ask:
                with busy("正在保存…"):
                    path = str(save_project_to(self.session.project, self._path))
                self.status.showMessage(f"已保存 {self._path.name}", _NOTICE_MS)
            else:
                path = self.deps.save_project_as(self, self.session.project,
                                       str(self._path) if self._path else "")
        except ProjectFileError as exc:
            self._log(f"[error] 保存失败：{exc}")
            self.status.showMessage("保存失败，原因见日志页", _NOTICE_MS)
            return None
        if path:
            self._path = pathlib.Path(path)
            self._remember_dir(path)
            if self.settings is not None:
                # 刚存过的工程也进历史：下一次开软件不用再翻目录去找（落盘后的地址
                # 已经是 0-based 裸地址，再打开按 0 偏移读正好）
                self.settings.add_recent(path)
                self.settings.save()
            self.session.clear_dirty()             # 落盘成功了才配清 `*`
        self._refresh_title()
        return path

    def _export_table(self) -> None:
        """导出一份**点表**给现场：写 CSV 还是写 Excel 由落盘路径的后缀说了算。

        **每次都要问一次落哪**（默认名 = 工程旁边那份 `.csv`，回车即就地落一份 CSV）。
        为什么不学"保存"那样有路径就直接写：导出对话框给的是**两种**格式，不问就等于
        "这个按钮只能出 CSV"——要 Excel 只能绕道"另存为"，而那一动作会把 Ctrl+S 的落点
        也换成点表文件（工程里的 `tick_ms` 那份设定就丢了）。导出是明确要一份**给别人的
        文件**，问一次路径才配得上"选格式"这件事；也顺带修掉"选了 Excel 却写 CSV 字节"
        （Excel 打开会报"文件格式不对"）。
        """
        suggested = str(self._path.with_suffix(".csv")) if self._path is not None else ""
        chosen = export_table_path(self, suggested or self._suggested_export())
        if not chosen:
            return
        target = pathlib.Path(chosen)
        try:
            with busy("正在导出点表…"):
                formats.write_project(self.session.project, target)
        except (OSError, ValueError) as exc:
            # ValueError 是"这份工程点表表示不了"（一个点位都没有）：那也是一句人话，
            # 不是 traceback——从 Qt 槽里逃出去的表现是"点了没反应"。
            self._log(f"[error] 导出点表失败：{exc}")
            self.status.showMessage("导出点表失败，原因见日志页", _NOTICE_MS)
            return
        self._remember_dir(target)
        self._log(f"[info] 已导出点表 -> {target}")
        for channel in self.session.project.channels:
            # 点表没有端口列：TCP 的端口是靠**通道名尾数**带过去的（`tcp-5020`）。
            # 名字与端口对不上时，这份点表导回来会读到另一个端口——导入那边"猜了要说"，
            # 导出这边也得说，否则就是一条静默变味的路（CSV 与 Excel 都是这条规则）。
            if channel_transport(channel) == "tcp" \
                    and port_from_name(channel.name) != channel.port:
                self._log(f"[note] {channel.name}: 端口 {channel.port} 不在名字里；"
                          f"这份点表按名字读回来会是 {port_from_name(channel.name)}"
                          f"（把名字改成 {channel.name}-{channel.port} 之类再导出）")

    def _save_log(self) -> None:
        """把日志窗里的文本另存成一份文件（回执给现场"把这段发我"用）。"""
        suggested = f"simbench-log-{dt.date.today().isoformat()}.txt"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "另存日志", suggested, "文本文件 (*.txt);;所有文件 (*)",
            options=FILE_DIALOG_OPTION)
        if not path:
            return
        try:
            # 不备份：这是**导出**一份给别人的文件，旁边再躺一个 .bak 只是噪音
            write_text_atomic(path, self.log_view.toPlainText(), backup=False)
        except OSError as exc:
            self._log(f"[error] 另存日志失败：{exc}")
            self.status.showMessage("另存日志失败，原因见日志页", _NOTICE_MS)
            return
        self._log(f"[info] 已导出日志 -> {path}")
        self.status.showMessage("日志已导出", _NOTICE_MS)

    def _confirm_discard(self) -> bool:
        """有未保存的改动时问一次（保存 / 丢弃 / 取消）。回 False = **整个动作放弃**。

        四个"会把当前工程换掉/丢掉"的动作共用它：关窗、新建、打开、导入。原来只有关窗问，
        另外三个是实测确认过的缺陷——`use_project` → `Session.load` 会 `_dirty = False`，
        标题上的 `*` 就这么消失、关窗也不再问，改动静默丢。`_confirming` 是重入闸：
        `_save_project` 自己会弹另存为，不许在那条路上再进一次这个框。
        """
        if not self.session.dirty or self._confirming:
            return True
        answer = QtWidgets.QMessageBox.question(
            self, "有未保存的改动",
            "点表有未保存的改动（标题栏有 *）。要先保存吗？",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Save)
        if answer == QtWidgets.QMessageBox.Cancel:
            return False
        if answer == QtWidgets.QMessageBox.Discard:
            return True
        self._confirming = True
        try:
            path = self._save_project()
        finally:
            self._confirming = False
        # 另存为里按了取消：改动一片都没落盘，这时候放动作继续就是静默丢数据
        return path is not None

    def _suggested_export(self) -> str:
        """没有工程路径时，"导出点表"对话框的起手提议：上次用过的目录 + 一个通用名字。"""
        if self.settings is None or not self.settings.last_dir:
            return ""
        return str(pathlib.Path(self.settings.last_dir) / "点表.csv")

    def _start_dir(self) -> str:
        """文件对话框该从哪个目录开：上次用过的那个（没有就是默认）。"""
        return self.settings.last_dir if self.settings is not None else ""

    def _remember_dir(self, path) -> None:
        """记下这次的目录（导入/导出/另存为都用得上），并落盘。"""
        if self.settings is None or not path:
            return
        self.settings.remember_dir(path)
        self.settings.save()
