"""用户那点"上次是怎么用的"：主题、窗口几何、最近打开的工程、上次用的目录。

为什么要它（2026-10-14 深度审查 O7）：全仓没有 `QSettings`、也没有 `saveGeometry`——主题、
窗口大小、上次打开的工程、上次导出到哪，每次重启都回到默认。对一天开十次的人，这是最烦的
那类小事；而"上次那份点表在哪"在换工程时还得靠资源管理器去找。

三条口径：

* **存纯 JSON**，`%APPDATA%\\simbench\\settings.json`（`SIMBENCH_SETTINGS_DIR` 可覆盖：
  用例、以及"把设置放共享盘"都用这一条）。不用 `QSettings` 是因为那样这一层就得 import
  PySide6，而 `project/`、`engine/` 那两棵树是 Qt-free 的——设置文件本身就够简单。
* **坏文件不许拦启动**：读不动（半截 JSON、被别的东西写坏、权限）就**当空的用**，并且
  把坏文件改名留档（`settings.json.bad`）——下一次保存会写出一份干净的，用户不会永远卡在
  一个读不动的文件上。
* **写不动就静默**：设置丢了是小事，"因为存不了设置而打不开软件"是事故。

不存的东西（刻意）：通道的启停状态、**任何点表内容**。前者会让"打开软件"变成"设备自己
跑起来了"（本项目最防的那类演出），后者本来就是点表文件的职责。

"最近打开"是一张**按后缀分组**的清单（工程 .json / 点表 .csv、.xlsx）：菜单那边用
project.formats.kind_of() 现分，这里只存路径，不抄格式表。导入点表时问出来的地址偏移
跟着那条记录一起存（recent_offsets）——否则从历史里再打开一份 40001 风格的表会按 0
偏移读，地址静默变掉，正是这个项目最防的那类"界面演了没发生的事"。
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field

SETTINGS_DIR_ENV = "SIMBENCH_SETTINGS_DIR"
FILE_NAME = "settings.json"
MAX_RECENT = 10


def settings_path() -> pathlib.Path:
    """设置文件放哪：`SIMBENCH_SETTINGS_DIR` > `%APPDATA%\\simbench`（POSIX 用 `~/.config`）。"""
    explicit = os.environ.get(SETTINGS_DIR_ENV)
    if explicit:
        return pathlib.Path(explicit) / FILE_NAME
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = pathlib.Path(base) if base else pathlib.Path.home()
    else:
        root = pathlib.Path.home() / ".config"
    return root / "simbench" / FILE_NAME


@dataclass
class Settings:
    """一份设置。字段名就是 JSON 里的键（加字段给默认值，老文件照样读得动）。"""

    theme: str = ""                      # "" = 没存过（按默认浅色）
    geometry: str = ""                   # QMainWindow.saveGeometry() 的十六进制
    window_state: str = ""               # saveState()（分隔条位置）
    last_dir: str = ""                   # 上次导入/导出用的目录
    recent: list[str] = field(default_factory=list)
    #: 路径 -> 导入时用过的地址偏移；只为"点表"记录，工程文件用不上（默认 0）
    recent_offsets: dict[str, int] = field(default_factory=dict)
    path: pathlib.Path | None = None     # 这份是从哪个文件读的（None = 默认值）

    @classmethod
    def load(cls, path=None) -> Settings:
        target = pathlib.Path(path) if path is not None else settings_path()
        try:
            body = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path=target)
        except (OSError, ValueError):
            # 读不动（半截 JSON / 权限）：当空的用，但把那份坏的留档——直接覆盖掉的话，
            # "为什么我的设置突然没了"就永远查不出来。
            with contextlib.suppress(OSError):
                target.replace(target.with_name(target.name + ".bad"))
            return cls(path=target)
        if not isinstance(body, dict):
            return cls(path=target)
        recent = body.get("recent")
        offsets = body.get("recent_offsets")
        # 老文件没有 recent_offsets；坏值逐条丢掉，不因为一条脏数据整份设置作废
        recent_offsets = {}
        if isinstance(offsets, dict):
            for key, value in offsets.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    recent_offsets[str(key)] = value
        return cls(
            theme=str(body.get("theme") or ""),
            geometry=str(body.get("geometry") or ""),
            window_state=str(body.get("window_state") or ""),
            last_dir=str(body.get("last_dir") or ""),
            recent=[str(p) for p in recent] if isinstance(recent, list) else [],
            recent_offsets=recent_offsets,
            path=target,
        )

    def to_dict(self) -> dict:
        return {"theme": self.theme, "geometry": self.geometry,
                "window_state": self.window_state, "last_dir": self.last_dir,
                "recent": list(self.recent),
                "recent_offsets": dict(self.recent_offsets)}

    def save(self, path=None) -> bool:
        """写回。**任何失败都咽下**（设置丢了是小事），回是否写成。"""
        target = pathlib.Path(path) if path is not None else (self.path or settings_path())
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(target)                     # 原子替换：半截文件不会被读到
            self.path = target
            return True
        except OSError:
            return False

    def add_recent(self, path, offset: int = 0) -> None:
        """记一个刚打开过的文件：**去重 + 置顶 + 截断**（菜单里只留最近几个）。

        offset 是导入点表时问出来的整体地址偏移；打开工程（.json）传 0 即可。
        同一条路径再次打开会覆盖旧偏移——"最近一次是怎么读的"才是从历史里再打开时
        该复现的那一次。
        """
        text = str(path or "")
        if not text:
            return
        self.recent = [text, *(p for p in self.recent if p != text)][:MAX_RECENT]
        self.recent_offsets[text] = int(offset or 0)
        # 被挤出清单的路径，偏移也一起删：否则设置文件会随使用时长越积越大
        kept = set(self.recent)
        self.recent_offsets = {p: value for p, value in self.recent_offsets.items()
                               if p in kept}

    def recent_offset(self, path) -> int:
        """这条历史记录导入时用过的地址偏移；没记过就是 0（0-based 裸地址）。"""
        return self.recent_offsets.get(str(path), 0)

    def clear_recent(self) -> None:
        """清空历史（菜单里的"清空历史"）：路径与偏移一起清。"""
        self.recent = []
        self.recent_offsets = {}

    def remember_dir(self, path) -> None:
        """记一个用过的目录（导入/导出对话框下次从这里开）。"""
        text = str(path or "")
        if not text:
            return
        self.last_dir = str(pathlib.Path(text).parent if pathlib.Path(text).suffix else text)

    def existing_recent(self) -> list[str]:
        """菜单里要显示的那些：**只留下真还在的文件**。

        已经删掉的条目留在菜单里，点一下弹一句"文件不存在"——比不显示更糟（用户会以为
        是自己记错了）。
        """
        return [p for p in self.recent if pathlib.Path(p).is_file()]
