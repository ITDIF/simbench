# simbench/gui/device_tree.py
"""通道→设备两级树。状态只改前景色与一个定宽符号，不改任何度量（几何恒定）。

为什么状态列只放一个符号而不是 `运行中`：那一列的宽度是定死的（用户口径 4：槽位装不下
改文案，不加宽不截断），中文状态词长短不一，一旦让文字决定列宽，`stopped→running`
就会把整棵树推宽，右边点位表跟着挪。文字版本在通道面板那一行给（`● 运行`，同样是定宽槽）。

符号与颜色必须成对出现：只靠颜色区分"运行/停止"在色弱的操作员那里等于没有状态，
而只靠符号的话 `error` 和 `starting` 在扫一眼时容易混。四态各一个符号，一个颜色。

两列都是定宽槽（240 / 40），所以最后一列的"自动铺满"必须关掉：开着它，列宽会跟视口那次
瞬时布局走，同一棵树先后量到 40 与 398 两种宽度，右边那张点位表也跟着挪。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

_STATUS_MARK = {"stopped": "○", "starting": "◐", "running": "●", "error": "▲"}

# 通道名列定宽：设备名/通道名的长短不该让状态列跟着挪（口径 3）。
_NAME_WIDTH = 240
_MARK_WIDTH = 40
_ROLE_CHANNEL = QtCore.Qt.UserRole
_ROLE_UNIT = QtCore.Qt.UserRole + 1


class DeviceTree(QtWidgets.QTreeWidget):
    """两级树：通道行带状态灯，设备行只带名字与 unit id。右键菜单是"加/删"的入口。

    为什么加/删住在这里：通道与设备**就是这棵树上的东西**（点位在表里，所以点位的加删
    在表上）。工具栏里没有一排小按钮——那排按钮的每一条都要配图标，而图标与定宽槽正是
    几条界面用例逐格量的对象，能不添就不添。
    """

    add_channel_requested = QtCore.Signal()
    add_device_requested = QtCore.Signal(str)          # 通道名
    add_point_requested = QtCore.Signal(str, int)      # 通道名 + unit_id
    remove_channel_requested = QtCore.Signal(str)
    remove_device_requested = QtCore.Signal(str, int)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["通道 / 设备", "状态"])
        self.setColumnCount(2)
        # 关掉"最后一列自动铺满"：QTreeView 默认打开它，而它会在视口**瞬时**比两列之和大
        # 的时候把状态列从 40 撑出去（实测首屏那次布局后 40 -> 398），下次 rebuild 设回 40，
        # 于是同一个界面在不同时刻量到两个列宽——"状态一变布局就跳"的另一种来源。
        # 状态列本来就是个定宽槽（`_MARK_WIDTH`），铺满它没有任何意义。
        self.header().setStretchLastSection(False)
        self._items: dict[str, QtWidgets.QTreeWidgetItem] = {}

    # ---------- 结构 ----------

    def rebuild(self, project) -> None:
        """按工程重画。清空会触发一次"当前项变成 None"，主窗的选中处理必须受得住。"""
        self._items.clear()
        self.clear()
        for channel in project.channels:
            top = QtWidgets.QTreeWidgetItem([channel.name, _STATUS_MARK["stopped"]])
            top.setData(0, _ROLE_CHANNEL, channel.name)
            top.setExpanded(True)
            self.addTopLevelItem(top)
            self._items[channel.name] = top
            for device in channel.devices:
                child = QtWidgets.QTreeWidgetItem(
                    [f"{device.name} (unit {device.unit_id})", ""])
                child.setData(0, _ROLE_CHANNEL, channel.name)
                child.setData(0, _ROLE_UNIT, device.unit_id)
                top.addChild(child)
        # 宽度在内容进去之后再定：clear() 会把列宽复位成默认值，先设等于白设
        self.setColumnWidth(0, _NAME_WIDTH)
        self.setColumnWidth(1, _MARK_WIDTH)

    def item_for(self, channel: str) -> QtWidgets.QTreeWidgetItem | None:
        """通道那一项（主窗在换工程/增删之后用它把选中项挪回去）。没有就回 None。"""
        return self._items.get(channel)

    def unit_of(self, item: QtWidgets.QTreeWidgetItem | None) -> int | None:
        """这一项是不是设备行：是就回 unit_id，通道行/空处回 None。"""
        if item is None:
            return None
        return item.data(0, _ROLE_UNIT)

    # ---------- 右键菜单 ----------

    def menu_actions(self, item: QtWidgets.QTreeWidgetItem | None) -> list[QtGui.QAction]:
        """那一项的右键动作清单：`contextMenuEvent` 与用例走**同一个**构造口。

        空白处给"添加通道"（空工程里这是唯一能起步的地方），通道行给"添加设备/删除通道"，
        设备行给"添加点位/删除设备"。设备行必须有"添加点位"：点位在表里，而**一个点位都
        没有的设备在表上没有任何一行可以右键**——不从这里给，第一个点位就永远加不出来。
        """
        menu = self._menu_for(item)
        return [] if menu is None else menu.actions()

    def contextMenuEvent(self, event) -> None:                # noqa: N802
        menu = self._menu_for(self.itemAt(event.pos()))
        if menu is None:
            event.ignore()
            return
        self._popup(menu, event.globalPos())

    def _popup(self, menu, pos) -> None:
        """弹层唯一的入口（与 `point_table._popup` 同一写法）：`exec` 会吞掉用例的模态嵌套。"""
        menu.exec(pos)
        menu.deleteLater()

    def _menu_for(self, item) -> QtWidgets.QMenu | None:
        channel = self.channel_of(item)
        unit = self.unit_of(item)
        menu = QtWidgets.QMenu(self)
        if not channel:
            action = menu.addAction("添加通道…")
            action.triggered.connect(
                lambda _checked=False: self.add_channel_requested.emit())
            return menu
        if unit is None:
            action = menu.addAction("添加设备…")
            action.triggered.connect(
                lambda _checked=False, name=channel: self.add_device_requested.emit(name))
            drop = menu.addAction(f"删除通道 {channel}…")
            drop.triggered.connect(
                lambda _checked=False, name=channel:
                self.remove_channel_requested.emit(name))
            return menu
        action = menu.addAction("添加点位…")
        action.triggered.connect(
            lambda _checked=False, name=channel, unit_id=unit:
            self.add_point_requested.emit(name, unit_id))
        drop = menu.addAction("删除设备…")
        drop.triggered.connect(
            lambda _checked=False, name=channel, unit_id=unit:
            self.remove_device_requested.emit(name, unit_id))
        return menu

    def channel_of(self, item: QtWidgets.QTreeWidgetItem | None) -> str:
        """这一项属于哪个通道。None / 没有归属都回空串（不是"第一个通道"）。"""
        if item is None:
            return ""
        return item.data(0, _ROLE_CHANNEL) or ""

    def state_of(self, channel: str) -> str:
        """当前显示的状态符号反查成状态名，给用例断言用（省掉 `item.text(1)` 到处写）。"""
        item = self._items.get(channel)
        if item is None:
            return ""
        for name, mark in _STATUS_MARK.items():
            if item.text(1) == mark:
                return name
        return ""

    # ---------- 状态 ----------

    def set_status(self, channel: str, state: str) -> None:
        """未知通道直接返回：状态事件可能比 rebuild 先到（换工程的瞬间）。"""
        item = self._items.get(channel)
        if item is None:
            return
        if state not in _STATUS_MARK:
            # 未知状态不许留一个旧符号冒充现况——那是界面在演一出没发生的戏
            raise ValueError(f"unknown channel state {state!r}")
        item.setText(1, _STATUS_MARK[state])          # 定宽列：换符号不改宽度
        # 颜色按当前主题模式取（深浅两套各一组，见 theme.status_colors）
        brush = QtGui.QBrush(theme.status_colors()[state])
        item.setForeground(0, brush)
        item.setForeground(1, brush)
        # tooltip 只给"为什么失败"，不给成功：操作员需要在树上看的是状态，
        # 详情在日志窗里已经有一行，这里重复一遍会把提示框变成第二个日志。
        item.setToolTip(0, "" if state != "error" else "启动失败，原因见日志页")
