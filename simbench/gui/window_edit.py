"""主窗的编辑动作：新建/增删通道设备点位、改值、属性面板（从 main_window 拆出）。"""

from __future__ import annotations

from PySide6 import QtWidgets

from ..engine.point_source import pumped_source
from ..project import edit
from ..project.model import channel_transport
from .dialogs import (
    format_issues,
)
from .new_project import AddChannelDialog, AddDeviceDialog, NewProjectDialog
from .point_props import STRUCTURAL_FIELDS
from .window_common import _one_line
from .window_consts import (
    _NOTICE_MS,
)


class EditMixin:
    def _new_project(self) -> None:
        """工具栏"新建工程"：空白起步，或 TCP / 串口 / S7 骨架起步。"""
        if not self._confirm_discard():
            return
        dialog = NewProjectDialog(self, probe=self.deps.probe)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.use_project(dialog.project, path=None, announce=False)
        if dialog.project.channels:
            self._log(f"[edit] 已新建工程（{len(dialog.project.channels)} 条通道）")
            # 有内容才置脏：一份空工程在关窗时拦一下问"要不要保存"是在为空气操心
            self._mark_dirty()
        else:
            self._log("[edit] 已新建空工程：在设备树上右键『添加通道』")
            self.status.showMessage("空工程：设备树上右键『添加通道』", _NOTICE_MS)

    def _add_channel(self) -> None:
        """加一路通道。**在跑的别的通道不受影响**（`Session.add_channel` 只动一个键）。

        对话框里那一步已经 `edit.add_channel`（带校验与回滚）；这里只让会话跟上，
        再把视图重画——不走 `load()`，所以不需要"先全部停止"。
        """
        dialog = AddChannelDialog(self, self.session.project, probe=self.deps.probe)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        if not self._edit_call("添加通道", lambda: self.session.add_channel(dialog.channel)):
            return
        self._after_edit(f"已添加通道 {dialog.channel.name}", keep=dialog.channel.name)

    def _add_device(self, channel: str) -> None:
        if self._guard_running([channel]):
            return
        try:
            dialog = AddDeviceDialog(self, self.session.project, channel)
        except KeyError:
            self._log(f"[refused] 添加设备：{channel} 已经不在这份工程里了")
            return
        except edit.EditRefused as exc:
            # 构造期就会算默认值（名字与 unit），算不出来时它抛的是这个（unit 用满 247 个）
            self._show_refused("添加设备", exc.reasons)
            return
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self._after_edit(f"已添加设备 {dialog.device.name}"
                         f"（unit {dialog.device.unit_id}）", reindex=channel)

    def _add_point(self, key: tuple) -> None:
        """点位表右键"添加点位…"：落在**右键那一行所属的设备**上。"""
        channel, unit_id, _area, _address, _bit = key
        self._create_point(channel, unit_id)

    def _add_point_to_device(self, channel: str, unit_id: int) -> None:
        """设备树右键"添加点位…"：空设备在表上没有行，这条是它唯一的加点入口。"""
        self._create_point(channel, unit_id)

    def _create_point(self, channel: str, unit_id: int) -> None:
        """加一个点位：按默认值落地，**然后立刻打开属性面板**让它改名/改类型。

        为什么不先弹一个"新建点位"表单：那 10 个字段的表单 `point_props` 已经有一份
        （带类型联动、定宽槽、一组几何断言）。抄第二份必然漂移，漂了的表现是"新建时
        能选的组合，打开属性面板就报错"。
        """
        if self._guard_running([channel]):
            return
        try:
            transport = channel_transport(self.session.project.find_channel(channel))
            device = edit.find_device(self.session.project, channel, unit_id)
            point = edit.default_point(device, edit.default_area(transport), transport)
            edit.add_point(self.session.project, channel, unit_id, point)
        except edit.EditRefused as exc:
            self._show_refused("添加点位", exc.reasons)
            return
        except KeyError:
            self._log(f"[refused] 添加点位：{channel} / unit {unit_id} "
                      f"已经不在这份工程里了")
            return
        self._after_edit(f"已添加点位 {point.name}（{channel} unit {unit_id}）",
                         reindex=channel)
        self._show_point_props((channel, unit_id, point.area, point.address, point.bit))

    def _remove_channel(self, channel: str) -> None:
        """删一路通道：**只停它自己**（别的通道照旧服务），先问一次再动手。"""
        try:
            target = self.session.project.find_channel(channel)
        except KeyError:
            self._log(f"[refused] 删除通道：{channel} 已经不在这份工程里了")
            return
        points = sum(len(device.points) for device in target.devices)
        running = self.session.channel(channel) is not None \
            and self.session.channel(channel).running
        note = "\n\n这一路正在运行，删除会先把它停掉（其它通道不受影响）。" if running else ""
        if not self._confirm(f"删除通道 {channel}？",
                             f"连同 {len(target.devices)} 个设备、{points} 个点位一起删除，"
                             f"删掉就回不来了。{note}"):
            return
        if not self._edit_call("删除通道",
                               lambda: edit.remove_channel(self.session.project, channel)):
            return
        if not self._edit_call("删除通道",
                              lambda: self.session.remove_channel(channel)):
            return
        self._after_edit(f"已删除通道 {channel}")

    def _remove_device(self, channel: str, unit_id: int) -> None:
        if self._guard_running([channel]):
            return
        try:
            device = edit.find_device(self.session.project, channel, unit_id)
        except KeyError:
            self._log(f"[refused] 删除设备：{channel} / unit {unit_id} "
                      f"已经不在这份工程里了")
            return
        if not self._confirm(f"删除设备 {device.name}？",
                             f"连同它的 {len(device.points)} 个点位一起删除，"
                             f"删掉就回不来了。"):
            return
        if self._edit_call("删除设备",
                           lambda: edit.remove_device(self.session.project, channel,
                                                      unit_id)):
            self._after_edit(f"已删除设备 {device.name}（{channel} unit {unit_id}）",
                             reindex=channel)

    def _remove_point(self, key: tuple) -> None:
        channel, unit_id, area, address, bit = key
        if self._guard_running([channel]):
            return
        try:
            device = edit.find_device(self.session.project, channel, unit_id)
        except KeyError:
            self._log(f"[refused] 删除点位：{channel} / unit {unit_id} "
                      f"已经不在这份工程里了")
            return
        try:
            point = edit.find_point(device, area, address, bit)
        except edit.EditRefused as exc:
            self._show_refused("删除点位", exc.reasons)
            return
        except KeyError:
            self._log(f"[refused] 删除点位：{channel} / {area} {address} "
                      f"已经不在这份工程里了")
            return
        if not self._confirm(f"删除点位 {point.name}？",
                             f"{channel} / unit {unit_id} / {area} {address}，"
                             f"删掉就回不来了。"):
            return
        if self._edit_call(f"删除点位 {point.name}",
                           lambda: edit.remove_point(self.session.project, channel,
                                                     unit_id, area, address, bit)):
            self._after_edit(f"已删除点位 {point.name}（{channel} unit {unit_id}）",
                             reindex=channel)

    def _guard_running(self, channel_names) -> bool:
        """要动的通道里只要有一条在跑，就整体拒绝并点名（口径 6）。

        为什么不"先停再改"：加/删通道要走 `Session.load()`，而它第一步就是
        `_stop_all_locked()`——为一个已停通道的编辑把正在给网关服务的那一路停掉，是新的
        现场事故（M4 对"改点位地址"已经因为同一个理由拒绝过一次）。加/删设备点位走的
        `reindex()` 也在文档里写死了：不许在那一路上跑着的时候调用。
        """
        running = [name for name in channel_names
                   if (runtime := self.session.channel(name)) is not None
                   and runtime.running]
        if not running:
            return False
        names = "、".join(running)
        self._log(f"[refused] {names} 正在运行；改点表要先停止（只影响这一路）")
        self.status.showMessage(f"{names} 正在运行，先停止再改点表", _NOTICE_MS)
        return True

    def _confirm(self, title: str, text: str) -> bool:
        """删除这种不可撤销的动作先问一次，默认按钮是"否"。"""
        answer = QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def _edit_call(self, what: str, action) -> bool:
        """跑一次结构性改动：被拒就点名进日志与状态栏，成功回 True（调用方再去刷视图）。

        两种都是"这件事做不成"而不是 bug：`EditRefused` 带校验原话（或分配器的理由），
        `KeyError` 是"目标已经不在了"（换工程那一瞬间）。两种都不许炸在 Qt 槽里。
        """
        try:
            action()
        except edit.EditRefused as exc:
            self._show_refused(what, exc.reasons)
            return False
        except (KeyError, ValueError) as exc:
            # KeyError：目标已经不在这份工程里（换工程那一瞬间）。
            # ValueError：会话拒绝了这次对账（`reindex` 不许动在跑的那一路、
            # `add_channel`/`remove_channel` 要求工程先改对）——那也是一句人话。
            self._log(f"[refused] {what}：{exc}")
            self.status.showMessage(f"{what}被拒：{_one_line(str(exc))}", _NOTICE_MS)
            return False
        return True

    def _show_refused(self, what: str, reasons) -> None:
        """拒绝理由一条都不许只留在返回值里：日志各一条，状态栏给第一句。"""
        for line in reasons:
            self._log(f"[refused] {what}: {line}")
        if reasons:
            self.status.showMessage(f"{what}被拒：{_one_line(str(reasons[0]))}", _NOTICE_MS)

    def _after_edit(self, message: str, keep: str | None = None,
                    reindex: str | None = None) -> None:
        """结构性改动落地之后：**让会话跟上**（只动该动的那一份），再把五个视图重画。

        `reindex` 给"改动落在哪一路"（加/删设备与点位）：`Session.reindex` 只重建读写索引，
        跑着的**兄弟**通道照旧服务。加/删通道由调用方自己做（`add_channel` /
        `remove_channel`，它们只动一个键），所以那种情况不传 `reindex`。

        为什么不再走 `use_project`（它内部是 `Session.load`）：那条路第一步停掉**所有**在跑
        的通道——加一个点位就把正在给网关服务的那一路停掉，是新的现场事故（这正是"在跑的
        时候不许增删"那条限制的来源）。现在只动该动的那一份。

        选中的通道也要保住：往第二条通道里加点位，表不该自己跳回第一条。
        """
        if reindex is not None:
            self.session.reindex(reindex)
        self._refresh_views(keep=keep)
        self._mark_dirty()
        self._log(f"[edit] {message}")

    def _refresh_views(self, keep: str | None = None) -> None:
        """按**当前**工程重画五个视图：不停通道、不清报文、不碰 dirty。

        与 `use_project` 的分工：那个是"换了一份工程"（停所有通道、清报文、路径/脏标记跟着
        走），这个是"同一份工程被改了"——在跑的那几路必须连状态一起保住（树上还是 ●），
        报文窗里已经收到的帧也不该被清掉。
        """
        project = self.session.project
        names = self.session.channel_names()
        states = {name: self._states.get(name, "stopped") for name in names}
        current = keep or self.point_model.channel()
        self.tree.rebuild(project)
        if names:
            self.point_model.attach(pumped_source(self.rows_pump), current)
        else:
            self.point_model.detach()
        self.channels.rebuild(project)
        self._states = states
        for name, state in states.items():
            # rebuild 之后控件是新的：状态要重新贴一遍，否则在跑的那一路会显示成"停止"
            self.tree.set_status(name, state)
            self.channels.set_status(name, state)
        self._shown_error = ""
        self._refresh_title()
        self.status_label.setText(self._summary())
        for line in format_issues(self.session.issues()):
            self._log(f"[issue] {line}")
        target = current if current in names else (names[0] if names else "")
        if target:
            self._select_channel(target)

    def _restore_waveform(self, key: tuple) -> None:
        """右键"恢复波形"：把这一行的点交还给波形，再报还剩几处在人工接管。

        交还本身由 `Session.restore_waveform` 经 `runner.call` 投回 loop 线程，两条拒绝
        都已经是带点位名的 `LogLine`（桥会写进日志页）——这里不再抄第二套拒绝文案。
        计数只在**真还回去过东西**之后说：没恢复成还说"还剩 N 处"，那一行明明仍 `已停`，
        状态栏就成了说谎的那个人。
        """
        channel, unit_id, area, address, bit = key
        if not self.session.restore_waveform(channel, unit_id, area, address, bit):
            return
        remaining = self.session.manual_count(channel)
        if remaining is None:
            # 引擎这一跳没回话（`manual_count` 明说"不知道"）：不许拿 0 顶替，
            # 那会变成"已全部交还"——一句很可能不成立的话。
            message = "已交还波形（接管计数这一跳没读到，下一跳再看）"
        elif remaining:
            message = f"已交还波形，人工接管还剩 {remaining} 处"
        else:
            message = "已交还波形，人工接管已全部交还"
        self.status.showMessage(message, _NOTICE_MS)

    def _show_point_props(self, key: tuple) -> None:
        """右键"属性…"：逐点编辑点表锁死的字段，成功后把"什么时候生效"说清楚。

        装载口径按 Task 6 裁决，不是"apply → `session.load`"：`load()` 会停掉**所有**
        通道，为一个已停通道的小编辑把正在服务网关的那一路停了就是新的现场事故。停通道
        检查、`load`/`reindex` 的分叉都住在 `point_props.apply()` 里（那里有拒绝理由），
        主窗只做事后呈现：表立刻刷一次 + 状态栏一句生效时机。措辞分三档：纯结构性改动
        只提重启；纯 live 改动只提下一拍（骗操作员去重启在跑的通道比不说更糟）；一次
        应用里两种都有就两句都说——只说一种就是另一半在撒谎。
        """
        try:
            dialog = self.deps.point_props_dialog(self, self.session.project, key)
        except KeyError as exc:
            # 菜单建好与打开面板之间行没了（换工程的瞬间）：槽函数不许炸，给一句人话。
            self._log(f"[refused] 打开点位属性失败：{exc}")
            self.status.showMessage("该行点位已不在当前点表中，属性面板未打开", _NOTICE_MS)
            return
        dialog.exec()
        changes = dialog.applied
        if changes is None:
            return                                   # 取消或整体被拒：什么都没发生
        # 改了属性就**同步取一次**：改名这类"只动 live 字段"的改动不改结构，泵的缓存不会
        # 自动作废——只 `refresh()` 的话表里还是旧名字（一行都没变，看着像没生效）。
        self.refresh_rows_now()
        count = len(changes)
        structural = set(changes) & set(STRUCTURAL_FIELDS)
        if structural and len(structural) == count:
            message = f"已改 {count} 处，重启通道后生效"
        elif structural:
            message = (f"已改 {count} 处：区/地址等重启通道后生效，"
                       f"名称/波形改动下一拍生效")
        else:
            message = f"已改 {count} 处，下一拍生效"
        self.status.showMessage(message, _NOTICE_MS)
