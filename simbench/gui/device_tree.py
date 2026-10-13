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


class DeviceTree(QtWidgets.QTreeWidget):
    """两级树：通道行带状态灯，设备行只带名字与 unit id。"""

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
                top.addChild(child)
        # 宽度在内容进去之后再定：clear() 会把列宽复位成默认值，先设等于白设
        self.setColumnWidth(0, _NAME_WIDTH)
        self.setColumnWidth(1, _MARK_WIDTH)

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
