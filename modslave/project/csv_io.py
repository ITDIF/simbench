"""CSV 点表读写。列顺序即合同；解析错误带行号，好让厂商点表能当场改对。

本文件不得 import pymodbus / serial / PySide6：它只负责"读不出来就带行号报错"，
点表语义是否合法（重叠、类型与区匹配、波形参数）一律是 validate.py 的活。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .model import Device, Point, Project, TcpChannel, brief

HEADER = ["channel", "device", "unit_id", "name", "area", "address", "datatype",
          "word_order", "initial", "waveform", "wave_param", "readonly"]
# readonly 只认这两张表，其余（含空串、`TURE` 这种拼错的）一律 ValueError：默默当
# False 会把"只读"的保护静默变成"可写"，主站就能改写一个真设备只读的点位。
# 口径与 model.py 里 bool 初值（_BOOL_TRUE/_BOOL_FALSE）对称——宁可报错，绝不静默给错值。
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def read_csv(text: str, ports: dict[str, int] | None = None, address_offset: int = 0,
             with_notes: bool = False):
    """CSV -> Project。`ports` 给通道名定端口；没给就按尾数猜并记 note。

    返回值有两种形态，调用点不要靠猜：默认 `Project`，`with_notes=True` 才是
    `(Project, notes)`。地址一律 `+ address_offset`，所以 40001 那种 1-based 点表
    要传负数整体左移（-40001 -> 0）；挪完是否越界由 validate 落 [address]。
    """
    ports = ports or {}
    notes: list[str] = []
    rows = _rows(text)
    channels: dict[str, TcpChannel] = {}
    devices: dict[tuple[str, int], Device] = {}
    for index, cell in rows:
        try:
            address = _require_int(cell["address"], "address") + address_offset
            unit_id = _require_int(cell["unit_id"], "unit_id")
            readonly = _require_bool(cell["readonly"], "readonly")
        except ValueError as exc:
            # 行号只在这一层加：底层只知道"哪一列的哪个值读不懂"，
            # 现场改点表靠的是"第几行"，两个信息必须同时出现
            raise ValueError(f"line {index}: {exc}") from None
        point = Point(
            name=cell["name"], area=cell["area"].lower(), address=address,
            datatype=cell["datatype"].lower(), initial=cell["initial"],
            word_order=(cell["word_order"] or "ABCD").upper(),
            waveform=(cell["waveform"] or "const").lower(),
            wave_param=parse_wave_param(cell["wave_param"]),
            readonly=readonly, row=index)
        channel = _channel_for(channels, cell["channel"], ports, notes)
        device = _device_for(devices, channel, unit_id, cell["device"], index)
        device.points.append(point)
    project = Project(channels=list(channels.values()))
    return (project, notes) if with_notes else project


def read_csv_file(path: Path, **kwargs):
    """读文件再交给 read_csv，kwargs 原样转发（ports/address_offset/with_notes）。

    utf-8-sig：Excel 导出的 CSV 常带 BOM，不吃掉它第一个列名会变成 `\ufeffchannel`
    从而报出一个看不懂的 header mismatch。文件读不到（不存在、是目录、没权限）
    让 OSError 原样抛出去——那是操作错误，不是点表内容错误。
    """
    return read_csv(Path(path).read_text(encoding="utf-8-sig"), **kwargs)


def write_csv(project: Project) -> str:
    """Project -> CSV 文本。`row` 是 CSV 自己的行号，不写回表里。

    lineterminator 固定 `\n`：csv 模块默认 `\r\n`，而 `write_csv_file` 用
    `newline=""` 原样落盘，同一份工程会因平台/路径不同产出不同字节（读回倒是
    两种都全等，实测过）。行尾钉死才有可比对的导出文件，也才谈得上进 git。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    for ch in project.channels:
        for dev in ch.devices:
            for pt in dev.points:
                writer.writerow([ch.name, dev.name, dev.unit_id, pt.name, pt.area,
                                 pt.address, pt.datatype, pt.word_order, pt.initial,
                                 pt.waveform, format_wave_param(pt.wave_param),
                                 "true" if pt.readonly else "false"])
    return buf.getvalue()


def write_csv_file(project: Project, path: Path) -> None:
    Path(path).write_text(write_csv(project), encoding="utf-8", newline="")


def parse_wave_param(text: str) -> dict[str, str]:
    """`base=20;amp=15` -> {"base": "20", "amp": "15"}；键值两边的空格都吃掉。"""
    out: dict[str, str] = {}
    for chunk in (c for c in (text or "").split(";") if c.strip()):
        key, _, value = chunk.partition("=")
        out[key.strip()] = value.strip()
    return out


def format_wave_param(params: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in params.items())


def _rows(text: str) -> list[tuple[int, dict[str, str]]]:
    reader = csv.reader(io.StringIO(text))
    lines = [(i, row) for i, row in enumerate(reader, start=1)
             if row and not row[0].lstrip().startswith("#")]
    if not lines:
        raise ValueError("csv is empty")
    header = [c.strip().lower() for c in lines[0][1]]
    if header != HEADER:
        # 两个表头都要打出来：只说"表头不对"，现场无法判断是模板换了还是列名写错
        raise ValueError(f"header mismatch: expected {','.join(HEADER)}, "
                         f"got '{','.join(header)}'")
    if len(lines) == 1:
        # 只有表头（或表头外加注释）不能当成"读出一个零点位工程"：那样从站会
        # 起成功却什么都不应答，用户只会怀疑平台。这里就停下，话说明白。
        raise ValueError("csv is empty: header matched but no data rows")
    out = []
    for index, row in lines[1:]:
        if len(row) != len(HEADER):
            raise ValueError(f"line {index}: expected {len(HEADER)} columns, got {len(row)}")
        out.append((index, dict(zip(HEADER, (c.strip() for c in row)))))
    return out


def _require_int(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError:
        # brief() 截断（裁决 N）：厂商表里一格粘了几百个字符时，报错还是那一句，
        # 但原值不再能把真正的线索挤出屏幕
        raise ValueError(f"{what} {brief(text)} is not an integer") from None


def _require_bool(text: str, what: str) -> bool:
    """白名单判布尔：认不出来就报错，绝不替用户猜一个 False（行号由 read_csv 补）。"""
    key = text.strip().lower()
    if key in _TRUE:
        return True
    if key in _FALSE:
        return False
    raise ValueError(f"{what} {brief(text)} is not a boolean, "
                     f"use one of 1/0/true/false/yes/no/on/off")


def _channel_for(channels: dict[str, TcpChannel], name: str, ports: dict[str, int],
                 notes: list[str]) -> TcpChannel:
    if name in channels:
        return channels[name]
    if name in ports:
        port = ports[name]
    else:
        port = _guessed_port(name)
        notes.append(f"channel {name!r}: port guessed as {port} from its name, "
                     f"confirm it in the project settings")
    channels[name] = TcpChannel(name=name, host="0.0.0.0", port=port)
    return channels[name]


def _guessed_port(name: str) -> int:
    tail = name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() and 1 <= int(tail) <= 65535 else 502


def _device_for(devices: dict[tuple[str, int], Device], channel: TcpChannel,
                unit_id: int, device_name: str, index: int) -> Device:
    key = (channel.name, unit_id)
    if key not in devices:
        devices[key] = Device(name=device_name, unit_id=unit_id)
        channel.devices.append(devices[key])
    existing = devices[key].name
    if existing != device_name:
        # 同 (channel, unit_id) 就是一个设备；同一个 unit 号带出两个名字，要么是
        # unit 号抄串了行、要么是设备名打错。静默并进其中任何一个，主站读到的都是
        # 另一台设备的值——两边都没法从报文看出来，只能当场停下问人
        raise ValueError(f"line {index}: unit_id {unit_id} is already used by device "
                         f"{existing!r}, got {device_name!r}")
    return devices[key]
