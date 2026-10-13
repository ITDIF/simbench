r"""生成 exe 的图标与版本资源（打包用，不进发布包）。

两件事：

1. `.ico`：把 `theme.app_icon()` 的画法在 16/32/48/64/128/256 六档上各画一遍，按 ICO 的
   容器格式（ICONDIR + 每条 ICONDIRENTRY + PNG 负载）打成一个文件。**每一档都自己画**
   （不是把 128 拉伸）：放大位图在 256 上会糊，而 exe 图标就是现场第一眼看到的东西。
2. 版本资源：PyInstaller 的 `version=` 读的是 `version_info.txt`（VSVersionInfo 那段 DSL），
   它是个静态文件，不在这里生成——这个脚本只管图标。

为什么要脚本而不是把 .ico 塞进仓库：图标是**产物**，它必须跟着 `theme.app_icon()` 那张画
走（改一次配色/形状，重打一次包就该跟着变）。`spec` 会在构建时调 `ensure_icon()`，
所以直接 `pyinstaller packaging/simbench.spec` 也不会缺图标。

用法（`packaging/build.ps1` 会自己调，不用手跑）：

    .\.venv\Scripts\python.exe packaging/make_icon.py
"""

from __future__ import annotations

import os
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
ICON_PATH = HERE / "simbench.ico"
SIZES = (16, 32, 48, 64, 128, 256)


def ico_bytes(images: dict[int, bytes]) -> bytes:
    """`{边长: PNG 字节}` -> `.ico` 字节。**纯函数**（用例直接喂几个假 PNG 就能验容器格式）。

    格式（MSDN 的 ICONDIR）：6 字节头 + 每条 16 字节的目录项 + 各张图的数据。边长 256 在
    目录项里写 **0**（一个字节装不下 256）——这是 ICO 的约定，不是笔误。
    """
    ordered = sorted(images)
    header = struct.pack("<HHH", 0, 1, len(ordered))       # reserved / type=icon / 张数
    offset = 6 + 16 * len(ordered)
    entries = b""
    payload = b""
    for size in ordered:
        data = images[size]
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0,
                               1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return header + entries + payload


def render_pngs(sizes=SIZES) -> dict[int, bytes]:
    """用 Qt 把那张图标按各档尺寸画出来，回 PNG 字节。**需要 PySide6**。

    平台用 offscreen：这个脚本在构建机上跑，不该弹窗口（也没有窗口可弹）。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    from PySide6 import QtCore, QtGui

    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    from simbench.gui import theme

    out: dict[int, bytes] = {}
    for size in sizes:
        pixmap = theme.app_icon(size).pixmap(size, size)
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.WriteOnly)
        pixmap.toImage().save(buffer, "PNG")
        out[size] = bytes(buffer.data())
    del app
    return out


def ensure_icon(path=ICON_PATH) -> pathlib.Path:
    """图标不在就生成它，回图标路径（`spec` 在构建时调它）。"""
    target = pathlib.Path(path)
    if not target.exists():
        target.write_bytes(ico_bytes(render_pngs()))
    return target


if __name__ == "__main__":
    written = ensure_icon()
    print(f"{written} ({written.stat().st_size} bytes)")