"""工具栏与窗口图标：用 QPainter **现画**，仓库里不带一套 png。

从 `theme.py` 拆出（2026-10-15 结构整理）：`theme` 管"颜色 / 字体 / 控件样式"，
图标的几何形状是另一件事（两百来行）。两者只在一处相交——图标颜色要按深浅模式取一组
亮一档的（深底上"保存"那种深灰会直接消失在底色里），所以这里的依赖方向是
`icons → theme`，没有反向依赖。

为什么现画而不是带 png：仓库里没有图片资源目录，几百字节的矢量形状不引入打包路径问题，
也就不会出现"装完之后按钮是空白的"。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui

from .theme import current_mode, resolve_mode

_ICON_SIZE = 18
_ICON_2X = 2                # 图标按 2 倍图建再交给 Qt 缩放：HiDPI 上不糊

_ICON_COLORS = {
    "light": {"play": "#1a7f37", "stop": "#c62828", "in": "#1f6fb2", "out": "#1f6fb2",
              "folder": "#b26a00", "save": "#42546a", "check": "#0f766e",
              "wave": "#1f6fb2", "dark": "#42546a", "new": "#7c3aed"},
    "dark": {"play": "#3fb950", "stop": "#f85149", "in": "#58a6ff", "out": "#58a6ff",
             "folder": "#d29922", "save": "#adbac7", "check": "#39c5bb",
             "wave": "#58a6ff", "dark": "#adbac7", "new": "#a78bfa"},
}


def _pixmap() -> QtGui.QPixmap:
    pix = QtGui.QPixmap(_ICON_SIZE * _ICON_2X, _ICON_SIZE * _ICON_2X)
    pix.setDevicePixelRatio(_ICON_2X)
    pix.fill(QtCore.Qt.transparent)
    return pix


def _painter(pix: QtGui.QPixmap, color: str) -> QtGui.QPainter:
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtGui.QColor(color))
    return painter


def _draw_play(painter: QtGui.QPainter) -> None:
    painter.drawPolygon([QtCore.QPointF(5.0, 3.0), QtCore.QPointF(15.0, 9.0),
                         QtCore.QPointF(5.0, 15.0)])


def _draw_stop(painter: QtGui.QPainter) -> None:
    painter.drawRoundedRect(QtCore.QRectF(4.5, 4.5, 9.0, 9.0), 1.5, 1.5)


def _draw_arrow(painter: QtGui.QPainter, down: bool) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    if down:
        painter.drawLine(QtCore.QPointF(9.0, 2.5), QtCore.QPointF(9.0, 11.0))
        painter.drawPolyline([QtCore.QPointF(5.2, 7.6), QtCore.QPointF(9.0, 11.4),
                              QtCore.QPointF(12.8, 7.6)])
        painter.drawPolyline([QtCore.QPointF(3.0, 12.5), QtCore.QPointF(3.0, 15.2),
                              QtCore.QPointF(15.0, 15.2), QtCore.QPointF(15.0, 12.5)])
    else:
        painter.drawLine(QtCore.QPointF(9.0, 15.5), QtCore.QPointF(9.0, 7.0))
        painter.drawPolyline([QtCore.QPointF(5.2, 10.4), QtCore.QPointF(9.0, 6.6),
                              QtCore.QPointF(12.8, 10.4)])
        painter.drawPolyline([QtCore.QPointF(3.0, 5.5), QtCore.QPointF(3.0, 2.8),
                              QtCore.QPointF(15.0, 2.8), QtCore.QPointF(15.0, 5.5)])


def _draw_folder(painter: QtGui.QPainter) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    path = QtGui.QPainterPath()
    path.moveTo(2.5, 13.5)
    path.lineTo(2.5, 5.0)
    path.lineTo(7.0, 5.0)
    path.lineTo(8.6, 6.8)
    path.lineTo(15.5, 6.8)
    path.lineTo(15.5, 13.5)
    path.closeSubpath()
    painter.drawPath(path)


def _draw_save(painter: QtGui.QPainter) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawRoundedRect(QtCore.QRectF(3.0, 3.0, 12.0, 12.0), 2.0, 2.0)
    painter.drawRect(QtCore.QRectF(6.0, 3.0, 6.0, 4.5))
    painter.drawRect(QtCore.QRectF(5.5, 10.0, 7.0, 5.0))


def _draw_check(painter: QtGui.QPainter) -> None:
    pen = painter.pen()
    pen.setWidthF(2.4)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawPolyline([QtCore.QPointF(3.5, 9.5), QtCore.QPointF(7.5, 13.0),
                          QtCore.QPointF(14.5, 5.0)])


def _draw_wave(painter: QtGui.QPainter) -> None:
    painter.setBrush(QtCore.Qt.NoBrush)
    path = QtGui.QPainterPath()
    path.moveTo(2.5, 12.0)
    path.lineTo(5.5, 12.0)
    path.lineTo(7.0, 5.5)
    path.lineTo(9.0, 14.5)
    path.lineTo(11.0, 3.5)
    path.lineTo(13.0, 12.0)
    path.lineTo(15.5, 12.0)
    painter.drawPath(path)


def _draw_moon(painter: QtGui.QPainter) -> None:
    """"深色主题"那个按钮：一轮月牙（自己画的，两个圆弧咬出来）。"""
    path = QtGui.QPainterPath()
    path.addEllipse(QtCore.QRectF(3.0, 3.0, 12.0, 12.0))
    bite = QtGui.QPainterPath()
    bite.addEllipse(QtCore.QRectF(7.5, 1.5, 12.0, 12.0))
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawPath(path.subtracted(bite))


def _draw_new(painter: QtGui.QPainter) -> None:
    """"新建工程"那个按钮：一张纸 + 右上角折角。

    不用一个 `+`：`+` 在这个界面上更像"加一行"，而这是**换一份空工程**（把当前这份放下）。
    折角是"一份新文件"最省笔墨的形状，也和 `folder`/`save` 那几个图标同一套线宽。
    """
    painter.setBrush(QtCore.Qt.NoBrush)
    path = QtGui.QPainterPath()
    path.moveTo(3.5, 2.5)
    path.lineTo(10.0, 2.5)
    path.lineTo(14.5, 7.0)
    path.lineTo(14.5, 15.5)
    path.lineTo(3.5, 15.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawPolyline([QtCore.QPointF(10.0, 2.5), QtCore.QPointF(10.0, 7.0),
                          QtCore.QPointF(14.5, 7.0)])


_DRAW = {"play": _draw_play, "stop": _draw_stop,
         "in": lambda p: _draw_arrow(p, down=True),
         "out": lambda p: _draw_arrow(p, down=False),
         "folder": _draw_folder, "save": _draw_save, "check": _draw_check,
         "wave": _draw_wave, "dark": _draw_moon, "new": _draw_new}


def icon(name: str, mode: str | None = None) -> QtGui.QIcon:
    """按名字取一个工具栏图标。名字不在表里就抛——空白按钮是现场最难查的一种"没反应"。"""
    draw = _DRAW.get(name)
    if draw is None:
        raise KeyError(f"unknown icon {name!r}")
    which = mode if mode in _ICON_COLORS else (current_mode() or resolve_mode())
    pix = _pixmap()
    painter = _painter(pix, _ICON_COLORS[which][name])
    try:
        draw(painter)
    finally:
        painter.end()
    return QtGui.QIcon(pix)


def icon_size() -> QtCore.QSize:
    return QtCore.QSize(_ICON_SIZE, _ICON_SIZE)


def app_icon(size: int = 128) -> QtGui.QIcon:
    """窗口/任务栏图标：深蓝底圆角方块 + 一条波形（和工具栏那个 `wave` 同一个形状）。

    两种模式共用一张：任务栏底很少是界面底色，跟着主题变反而更不好认。

    `size` 是**画**出来的边长（不是把 128 那张拉伸）：`packaging/make_icon.py` 用它生成
    exe 图标那几档尺寸，放大位图在 256 上会糊。
    """
    size = int(size)
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor("#123a5c"))
    painter.drawRoundedRect(QtCore.QRectF(6, 6, size - 12, size - 12), 26, 26)
    painter.scale(size / _ICON_SIZE, size / _ICON_SIZE)
    pen = QtGui.QPen(QtGui.QColor("#7fd1ff"))
    pen.setWidthF(1.6)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    _draw_wave(painter)
    painter.end()
    return QtGui.QIcon(pix)
