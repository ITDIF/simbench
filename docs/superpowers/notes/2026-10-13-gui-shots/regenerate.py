"""把这组界面截图重新渲染一遍（`python docs/superpowers/notes/2026-10-13-gui-shots/regenerate.py`）。

要点：**不要**设 `QT_QPA_PLATFORM=offscreen` —— offscreen 那套兜底字体把每个字形都按 1em
算，截出来的字距不是现场的（`test_gui_theme.py` 里那条"列宽为什么不拿 fontMetrics 现量"
的注释就是同一个坑）。所以这个脚本要在有桌面的机器上跑，它会真的开一个窗口，抓完就关。

跑完看两张就够：`07-new-rtu.png`（提示那句有没有被切）与 `11-add-device.png`（`unit id`
那格）。这两处正是这组图当年照出来的缺陷。
"""

from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore                                       # noqa: E402

from simbench.gui import app as app_module                       # noqa: E402
from simbench.gui.new_project import (AddChannelDialog,          # noqa: E402
                                      AddDeviceDialog, NewProjectDialog)
from simbench.project import edit                                 # noqa: E402

CSV = str(ROOT / "examples" / "covi-tunnel.csv")


def main() -> int:
    app, window, session = app_module.build_app([CSV])
    window.resize(1280, 780)
    _settle(app)

    def shoot(widget, name: str) -> None:
        _settle(app)
        widget.grab().save(str(HERE / name))
        print("saved", name, widget.size().width(), "x", widget.size().height())

    shoot(window, "01-main-light.png")

    for name, item in (("02-tree-menu-channel.png", window.tree.item_for("tcp-5020")),
                       ("03-tree-menu-blank.png", None)):
        menu = window.tree._menu_for(item)
        menu.popup(QtCore.QPoint(320, 300))
        _settle(app)
        menu.grab().save(str(HERE / name))
        menu.close()

    menu = window.point_table._menu_for(0)
    menu.popup(QtCore.QPoint(700, 300))
    _settle(app)
    menu.grab().save(str(HERE / "04-table-menu.png"))
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

    window.act_theme.setChecked(True)
    window._toggle_theme(True)
    shoot(window, "12-main-dark.png")

    window.tabs.setCurrentIndex(2)
    for _ in range(20):
        app.processEvents()
    shoot(window, "13-net-panel.png")

    session.shutdown()
    return 0


def _settle(app) -> None:
    """`grab()` 之前把事件队列跑干净：布局与重绘都在这一轮里落地。"""
    for _ in range(6):
        app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())