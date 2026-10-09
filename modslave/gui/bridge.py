"""引擎回调 → Qt signal。工作线程只管 emit，投递交给 Qt。

实测（M2 前置探针）：工作线程 emit 200 次，主线程收到 200 次、顺序不乱、
0 次落在错误线程。所以不需要 qasync，也不需要自己写 QTimer 轮询队列。

这个文件是 Qt 与非 Qt 的唯一接缝：Session/Runner 的回调只准调 `forward()`，
它只做 emit，不碰任何 widget——碰 widget 的都在 model/view 里，由它们连到这些
signal 上，连接天然是 queued（发送方不是本对象所在线程），所以 UI 更新始终在主线程。
"""

from __future__ import annotations

from PySide6 import QtCore

from ..backend.events import ChannelState, LogLine, PacketEvent, PointChanged


class EngineBridge(QtCore.QObject):
    """住在主线程的信号载体。Session 的回调里只准调这几个 emit。"""

    packet = QtCore.Signal(object)          # PacketEvent
    point_changed = QtCore.Signal(object)   # PointChanged
    channel_state = QtCore.Signal(object)   # ChannelState
    logged = QtCore.Signal(object)          # LogLine

    def forward(self, event) -> None:
        """Session/Runner 的唯一入口。线程安全：只做 emit，不碰 widget。"""
        if isinstance(event, PacketEvent):
            self.packet.emit(event)
        elif isinstance(event, PointChanged):
            self.point_changed.emit(event)
        elif isinstance(event, ChannelState):
            self.channel_state.emit(event)
        elif isinstance(event, LogLine):
            self.logged.emit(event)
        else:                           # 未知事件不许静默丢弃
            self.logged.emit(LogLine("warning", f"unknown engine event {type(event).__name__}"))
