"""CSV/工程装载：把"读文件 + 端口覆盖 + 校验"收成一次调用。

CLI（`python -m simbench`）与后面的 GUI 都只走这一个口。解析错误在这里就转成
Issue 返回，不抛异常——"一次报全"这件事如果靠每个调用点各自 try/except，迟早会漏掉
一处，漏掉的那处表现成"改一个错、报一个错"的挤牙膏。
"""

from __future__ import annotations

from pathlib import Path

from .project.csv_io import read_csv_file
from .project.json_io import load as load_project
from .project.model import Project, channel_transport
from .project.validate import Issue, validate_project


def load_point_table(path: str, port_overrides: dict[str, int] | None = None):
    """返回 `(Project, notes, issues)`；CSV 与 project.json 两种入口都支持。

    `port_overrides` 是"通道名 -> 端口"，值为 0/None 视为没填（留给通道名尾数猜，
    并记一条 note 让人确认）。

    点表**读不懂**（表头不符、地址不是数字、列数不对、json 缺键、不是 utf-8）一律转成
    `Issue("error", "parse", 文件名, 消息)` 返回：CSV 与 project.json 的 loader 对
    "读出来了但读不懂"都只抛 `ValueError`（见两个模块的 docstring），这里兜住的正是
    那一类。文件本身读不到（不存在、是目录、没权限）仍然把 `OSError` 原样抛出去——
    那是操作错误不是点表内容错误，由调用方（`__main__`）单独说一句人话。
    """
    source = Path(path)
    overrides = dict(port_overrides or {})
    # 0/None 不当地作端口传给 read_csv：那条路径会跳过"从通道名猜端口"并少记一条 note，
    # 而 0 本来就是 validate 拦下的非法端口（见裁决：M1 不支持 port=0）
    ports = {name: port for name, port in overrides.items() if port}
    notes: list[str] = []
    try:
        if source.suffix.lower() == ".json":
            project = load_project(source)
        else:
            project, notes = read_csv_file(source, ports=ports, with_notes=True)
    except ValueError as exc:
        return project_placeholder(), [], [
            Issue("error", "parse", source.name, str(exc))]
    skipped: list[str] = []
    for channel in project.channels:
        override = overrides.get(channel.name)
        if not override:
            continue
        # 串口通道的 `port` 是串口名，`--port 5020` 那种覆盖对它没有意义。静默跳过等于
        # 让命令行以为自己改了点什么（现场表现是"参数不生效"），所以照"猜了要说"同一条
        # 口径，记一条 note 一起打出来。
        if channel_transport(channel) != "tcp":
            skipped.append(channel.name)
            continue
        channel.port = override
    if skipped:
        notes.append(f"port override ignored for serial channel(s) "
                     f"{', '.join(skipped)}: they have a serial port name, not a TCP port")
    return project, notes, validate_project(project)


def project_placeholder() -> Project:
    """解析失败时给调用方一个空工程，免得下游判 None。"""
    return Project(channels=[])
