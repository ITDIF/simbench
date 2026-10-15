"""图标：每个画得出来的图标都有颜色，而且**真画得出来**。

为什么单独一个文件（2026-10-15 结构整理）：图标从 `theme.py` 拆到 `icons.py` 之后，
配色那几条断言（QSS / QPalette / 明度）和图标几何就不是同一件事了，混在一个文件里
两边都不好读。
"""

from __future__ import annotations

import pytest

from simbench.gui import icons


def test_every_drawable_icon_has_a_colour_in_both_modes():
    """两张色表的键要一样（深底上要亮一档），且覆盖 `_DRAW` 里每一个图标。

    漏一个的表现是"某个按钮空白"——现场最难查的一类"点了没反应"，所以在色表这一层钉住。
    """
    assert set(icons._ICON_COLORS["light"]) == set(icons._ICON_COLORS["dark"])
    for name in icons._DRAW:
        assert name in icons._ICON_COLORS["light"], name


def test_every_icon_can_actually_be_drawn(qapp):
    """每个名字都真画一遍：绘制函数里的坐标写错、少一个参数，在这里就炸。

    图标是**运行时现画**的（不进仓库、也不参与打包校验），所以"装完按钮是空白的"这类
    故障，这是自动化唯一能提前抓到的地方。
    """
    assert icons._DRAW, "一张图标都没有，这条用例是空跑"
    for name in icons._DRAW:
        assert not icons.icon(name).isNull(), name
    assert icons.icon_size().width() > 0


def test_an_unknown_icon_name_is_refused_loudly():
    """名字不在表里要抛：空白按钮比一条报错难查得多（口径见 `icons.icon`）。"""
    with pytest.raises(KeyError):
        icons.icon("no-such-icon")


def test_the_window_icon_renders_at_the_sizes_the_packager_asks_for(qapp):
    """窗口图标按**画**出来的尺寸生成（`packaging/make_icon.py` 要 16…256 六档）。

    不拉伸位图是这里的口径：放大位图在 256 上会糊，所以每一档都得现画一遍。
    """
    for size in (16, 32, 48, 64, 128, 256):
        icon = icons.app_icon(size)
        assert not icon.isNull(), size
        pixmap = icon.pixmap(size, size)
        assert pixmap.width() == size and pixmap.height() == size, size
