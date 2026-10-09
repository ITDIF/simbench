"""CSV 点表读写。列顺序即合同；解析错误带行号，好让厂商点表能当场改对。

本文件不得 import pymodbus / serial / PySide6：它只负责"读不出来就带行号报错"，
点表语义是否合法（重叠、类型与区匹配、波形参数）一律是 validate.py 的活。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .model import (
    Device, Point, Project, TcpChannel, BOOL_TOKENS, brief, parse_bool,
)

HEADER = ["channel", "device", "unit_id", "name", "area", "address", "datatype",
          "word_order", "initial", "waveform", "wave_param", "readonly"]
# 三个身份列：空的 channel 会建出一个名为 '' 的通道（两行还会并进同一个无名通道），
# 空的 device/name 把空串原样带进模型，validate 一条 error 都不出、从站照起。Excel
# 少拖一格就会这样，后果是点位挂到无名通道上，GUI、报文和日志三边都对不上号。
_IDENTITY_COLUMNS = ("channel", "device", "name")
# readonly 认哪些写法不在这里定：判定住在 model.parse_bool，本文件只负责把"认不出来"
# 翻译成点表这一列的说法并补列名（裁决 E——两份白名单会让同一张 CSV 的 readonly 列和
# initial 列对同一个词给相反答案）。


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
        # 身份列判空停在 try 之外：`_require_text` 的消息自带行号，再套一层前缀就变成
        # "line 2: line 2: ..."。行号信息只有 loader 有，所以这条不许挪进 validate。
        for col in _IDENTITY_COLUMNS:
            _require_text(cell[col], col, index)
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
        # 两个表头都要打出来：只说"表头不对"，现场无法判断是模板换了还是列名写错。
        # 实际表头走 brief()（裁决 N）：200 列的厂商表实测把消息撑到 1020 个字符，
        # 期望表头是我们自己的常量、原样打印才有比对价值。
        raise ValueError(f"header mismatch: expected {','.join(HEADER)}, "
                         f"got {brief(','.join(header))}")
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


def _require_text(value, what: str, index: int) -> str:
    """身份列（channel/device/name）不许为空：空串原样进模型 = 建出一个无名通道/设备。

    判据只看 strip 之后的长度——`_rows` 已经 strip 过，所以 `"   "` 这一格在 Excel 里
    和留空完全等价，同样要拦。非 `str` 走 brief() 打出来（裁决 N），虽然 csv 模块给
    我们的永远是 str，这一支留着是为了 `read_csv` 被别的调用方复用时不至于抛
    `TypeError` 逃逸（json_io 同一条契约）。
    """
    if not isinstance(value, str):
        raise ValueError(f"line {index}: {what} must be a string, got {brief(value)}")
    if not value:
        raise ValueError(f"line {index}: {what} must not be empty")
    return value


def _require_bool(text: str, what: str) -> bool:
    """白名单判布尔：认不出来就报错，绝不替用户猜一个 False（行号由 read_csv 补）。

    判定不在这里做（裁决 E）：`model.parse_bool` 是全仓库唯一那张 bool 白名单，
    `initial` 列（走 `parse_value(BOOL, ...)`）与 `readonly` 列共用它。这里只把
    "认不出来"换成本文件已经钉住的句式，并保留列名——`{what} 'TURE' is not a boolean,
    use one of ...` 是点表编辑者看的，带列名比带类型名有用。
    """
    try:
        return parse_bool(text)
    except ValueError:
        raise ValueError(f"{what} {brief(text)} is not a boolean, "
                         f"use one of {BOOL_TOKENS}") from None


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
        # 另一台设备的值——两边都没法从报文看出来，只能当场停下问人。
        # 两个名字都走 brief()（裁决 N）：实测脏名字能把这条消息撑到 654 个字符。
        raise ValueError(f"line {index}: unit_id {unit_id} is already used by device "
                         f"{brief(existing)}, got {brief(device_name)}")
    return devices[key]
