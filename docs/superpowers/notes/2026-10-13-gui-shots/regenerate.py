r"""把这组界面截图重新渲染一遍（`python docs/superpowers/notes/2026-10-13-gui-shots/regenerate.py`）。

要点：**不要**设 `QT_QPA_PLATFORM=offscreen` —— offscreen 那套兜底字体把每个字形都按 1em
算，截出来的字距不是现场的（`test_gui_theme.py` 里那条"列宽为什么不拿 fontMetrics 现量"
的注释就是同一个坑）。所以这个脚本要在有桌面的机器上跑，它会真的开一个窗口，抓完就关。

跑完看两张就够：`07-new-rtu.png`（提示那句有没有被切）与 `11-add-device.png`（`unit id`
那格）。这两处正是这组图当年照出来的缺陷。

按尺寸/缩放验收（不写默认参数时行为与以前完全一样，仍写回本目录）：

    python regenerate.py --size 1366x768 --scale 1.0 --out D:\tmp\shots-1366
    python regenerate.py --size 1280x720 --scale 1.5 --out D:\tmp\shots-150

`--scale` 走 `QT_SCALE_FACTOR`，必须在创建 QApplication 之前设；`--out` 目录不存在会新建。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import socket
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[3]
_OUT = HERE
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore  # noqa: E402

from simbench.gui import app as app_module  # noqa: E402
from simbench.gui.new_project import AddChannelDialog, AddDeviceDialog, NewProjectDialog  # noqa: E402
from simbench.project import edit  # noqa: E402

CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重渲染界面截图")
    parser.add_argument("--size", default="1280x780", help="窗口尺寸，例如 1366x768")
    parser.add_argument("--scale", default="1.0", help="Qt 缩放因子，例如 1.5")
    parser.add_argument("--out", default=str(HERE), help="输出目录（默认写回本目录）")
    return parser.parse_args()


def main() -> int:
    global _OUT
    args = _parse_args()
    # 缩放因子必须在 QApplication 创建之前生效；这里还没 build_app。
    os.environ["QT_SCALE_FACTOR"] = args.scale
    _OUT = pathlib.Path(args.out)
    _OUT.mkdir(parents=True, exist_ok=True)
    width, height = (int(part) for part in args.size.lower().split("x"))
    app, window, session = app_module.build_app([CSV])
    window.resize(width, height)
    _settle(app)

    def shoot(widget, name: str) -> None:
        _settle(app)
        widget.grab().save(str(_OUT / name))
        print("saved", name, widget.size().width(), "x", widget.size().height())

    shoot(window, "01-main-light.png")

    for name, item in (("02-tree-menu-channel.png", window.tree.item_for("tcp-5020")),
                       ("03-tree-menu-blank.png", None)):
        menu = window.tree._menu_for(item)
        menu.popup(QtCore.QPoint(320, 300))
        _settle(app)
        menu.grab().save(str(_OUT / name))
        menu.close()

    menu = window.point_table._menu_for(0)
    menu.popup(QtCore.QPoint(700, 300))
    _settle(app)
    menu.grab().save(str(_OUT / "04-table-menu.png"))
    menu.close()

    for kind, name in ((edit.BLANK, "05-new-blank.png"), ("tcp", "06-new-tcp.png"),
                       ("rtu", "07-new-rtu.png"), ("s7", "08-new-s7.png")):
        dialog = NewProjectDialog(window)
        dialog.form.pick(kind)
        dialog.show()
        shoot(dialog, name)
        dialog.close()
        dialog.deleteLater()

    dialog = AddChannelDialog(window, session.project)
    dialog.show()
    shoot(dialog, "09-add-channel-tcp.png")
    dialog.form.second_edit.setText("not a number")
    dialog._accept_if_applied()
    shoot(dialog, "10-add-channel-refused.png")
    dialog.close()
    dialog.deleteLater()

    dialog = AddDeviceDialog(window, session.project, "tcp-5020")
    dialog.show()
    shoot(dialog, "11-add-device.png")
    dialog.close()
    dialog.deleteLater()

    # 跑起来的状态：原始寄存器那一列要**真数据**才看得出宽度够不够（4 个寄存器的 uint64
    # 是最宽的一条）。端口用空闲的，不去抢现场那个 5020。
    session.project.channels[0].port = _free_port()
    window.use_project(session.project, CSV)
    window._start_all()
    shoot(window, "14-main-running.png")
    window._stop_all()
    _settle(app)

    window.act_theme.setChecked(True)
    window._toggle_theme(True)
    shoot(window, "12-main-dark.png")

    # 日志页（有"清空"按钮）：先塞几条像样的行，不然截出来是空的
    window._log("[info] 已新建工程（1 条通道）")
    window._log("[running] tcp-5020 0.0.0.0:5020")
    window._log("[refused] 添加点位：tcp-5020 正在运行；改点表要先停止（只影响这一路）")
    window.tabs.setCurrentWidget(window.log_tab)
    shoot(window, "16-log-tab.png")

    window.tabs.setCurrentIndex(2)
    for _ in range(20):
        app.processEvents()
    shoot(window, "13-net-panel.png")

    session.shutdown()
    return 0


def _free_port() -> int:
    """挑一个空闲端口：截图脚本不许去抢点表里写死的那个 5020（现场可能真在用）。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _settle(app) -> None:
    """`grab()` 之前把事件队列跑干净：布局与重绘都在这一轮里落地。"""
    for _ in range(6):
        app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())