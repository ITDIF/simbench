"""CSV 点表读写。列顺序即合同；解析错误带行号，好让厂商点表能当场改对。

本文件不得 import pymodbus / serial / PySide6：它只负责"读不出来就带行号报错"，
点表语义是否合法（重叠、类型与区匹配、波形参数）一律是 validate.py 的活。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .model import (
    Device, Point, Project, S7_DEFAULTS, S7_TYPES, SERIAL_DEFAULTS, SERIAL_MARK,
    S7Channel, SerialChannel, TcpChannel, BOOL_TOKENS, brief, channel_transport,
    format_frame, parse_bool, parse_frame, parse_s7_address, parse_s7_area,
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
        # 通道要先认出来：地址格怎么读**取决于传输**（S7 的 bool 写成 `字节.位` 的 `0.3`，
        # Modbus 与 S7 的其它类型是裸整数），而这件事只有通道类型知道。
        channel = _channel_for(channels, cell["channel"], ports, notes, index,
                               row_area=cell["area"], row_datatype=cell["datatype"])
        try:
            address, bit = _address_cell(cell["address"], channel, address_offset, index)
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
            readonly=readonly, row=index, bit=bit)
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


def rows_of(project: Project) -> list[list[str]]:
    """工程 -> **表格形状**：第一行是表头，其余一行一个点位，12 格都是字符串。

    这是**所有"点表格式"的共用形状**（2026-10-14）：CSV 用它拼文本、Excel 用它填单元格——
    两份各写一遍"哪一列写什么"迟早漂（加一列时改了一处忘了另一处，导出的 Excel 就少一列）。
    通道名那点语法（`name@tcp:host:port` 等）也在这里烘好，见 `_csv_channel_name`。
    """
    # 一个点位都没有的工程会写成**只有表头**的一份点表，而读回来是 `csv is empty` 的错——
    # 那等于产出一个谁也读不回来的文件（新建一个空通道、导一次、再想导入就撞上）。点表格式
    # 里没有"空通道"的表示法，所以这里就说清楚，并指到工程文件（`.json`）那条路上。
    if not any(True for _ in project.iter_points()):
        raise ValueError("this project has no point; a point table cannot express an "
                         "empty channel — save it as .json instead")
    rows = [list(HEADER)]
    for ch in project.channels:
        for dev in ch.devices:
            for pt in dev.points:
                rows.append([_csv_channel_name(ch), dev.name, str(dev.unit_id),
                             pt.name, pt.area, _address_cell_text(pt, ch),
                             pt.datatype, pt.word_order, pt.initial,
                             pt.waveform, format_wave_param(pt.wave_param),
                             "true" if pt.readonly else "false"])
    return rows


def write_csv(project: Project) -> str:
    """Project -> CSV 文本。`row` 是 CSV 自己的行号，不写回表里。

    lineterminator 固定 `\n`：csv 模块默认 `\r\n`，而 `write_csv_file` 用
    `newline=""` 原样落盘，同一份工程会因平台/路径不同产出不同字节（读回倒是
    两种都全等，实测过）。行尾钉死才有可比对的导出文件，也才谈得上进 git。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows_of(project):
        writer.writerow(row)
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


def _csv_channel_name(channel) -> str:
    """导出时把"那台设备在哪儿"烘进通道名（导入按同一语法还原）：往返不丢参数，也不新增列。

    三种写法（互不重叠，导入侧按同一个顺序问）：

    * 串口：`com-485-1@COM3 19200 8E1`（全默认只写 `名字@串口名`）
    * S7：`s7-1@192.168.0.10:102 0/1`（全默认的 rack/slot 不写）
    * TCP：`tcp-5020@tcp:0.0.0.0:5020`（2026-10-13 加的；不写的话端口只能靠名字尾数带过去、
      host 则无处可放——自定义名字的工程导出再导入就是**静默改配置**）

    段之间用空格：串口名/主机名本身不许含空格（validate 拦），所以这里不会与 CSV 的引号规则打架。
    """
    if channel_transport(channel) == "s7":
        # `s7-1@192.168.0.10:102 0/1`（全默认的 rack/slot 不写，与串口那条同一个口径）
        spec = f"{channel.host}:{channel.port}"
        if (channel.rack, channel.slot) != (S7_DEFAULTS["rack"], S7_DEFAULTS["slot"]):
            spec = f"{spec} {channel.rack}/{channel.slot}"
        return f"{channel.name}{SERIAL_MARK}{spec}"
    if channel_transport(channel) == "tcp":
        # TCP 也把监听地址写进 `@` 段，但**带一个 `tcp:` 前缀**：`name@tcp:host:port`。
        # 不写成 `name@host:port` —— 那个形状是 S7 的（`s7-1@192.168.0.10:102`），两种传输
        # 撞成同一个写法，导入时只能靠猜，而猜错的后果是静默走错传输（见 `parse_tcp_name`）。
        #
        # 为什么 TCP 也要写：不写的话，端口只能靠"通道名尾数"那条隐式约定带过去（host 则
        # 无处可放）——自定义名字（`网关-1`）导出再导入端口会变成 1、host 会变回 0.0.0.0，
        # 这是**静默改配置**。显式写出来，文件长一点，但往返逐字一致。
        return f"{channel.name}{SERIAL_MARK}tcp:{channel.host}:{channel.port}"
    default_frame = format_frame(SERIAL_DEFAULTS["bytesize"], SERIAL_DEFAULTS["parity"],
                                 SERIAL_DEFAULTS["stopbits"])
    frame = format_frame(channel.bytesize, channel.parity, channel.stopbits)
    fields = [str(channel.port)]
    if channel.baudrate != SERIAL_DEFAULTS["baudrate"] or frame != default_frame:
        fields.append(str(channel.baudrate))
        if frame != default_frame:
            fields.append(frame)
    return f"{channel.name}{SERIAL_MARK}{' '.join(fields)}"


TCP_SCHEME = "tcp:"


def parse_tcp_name(raw: str, index: int) -> tuple[str, TcpChannel] | None:
    """`<通道名>@tcp:<host>:<port>` -> `(名字, TcpChannel)`；不是这个写法回 None。

    为什么 TCP 要一个显式前缀：`name@host:port` 这个形状**已经是 S7 的**，两种传输共用同一个
    写法就分不出来了，而"分不出来"意味着导入时要靠区/类型去猜传输——猜错就是静默走错路。
    加上前缀之后四种写法互不重叠：`@tcp:…`（TCP）、`@…://…`（串口 URL）、`@…:…`（S7）、
    `@…`（串口名）。

    只做**转换**：端口不是数、host 为空这种形状错在这里带行号拒掉；host 是不是合法地址、
    端口范围这些判定留给 `validate`。
    """
    if SERIAL_MARK not in raw:
        return None
    name, _, spec = raw.partition(SERIAL_MARK)
    name = name.strip()
    fields = spec.split()
    if not fields or not fields[0].lower().startswith(TCP_SCHEME):
        return None
    if not name:
        raise ValueError(f"line {index}: channel {brief(raw)} has no name before "
                         f"{SERIAL_MARK!r} (write it as name{SERIAL_MARK}tcp:host:port)")
    if len(fields) > 1:
        raise ValueError(f"line {index}: channel {brief(raw)} must be written as "
                         f"name{SERIAL_MARK}tcp:host:port (one part after "
                         f"{SERIAL_MARK!r})")
    body = fields[0][len(TCP_SCHEME):]
    host, sep, port_text = body.rpartition(":")
    if not sep or not host:
        raise ValueError(f"line {index}: channel {brief(raw)}: TCP channels are written "
                         f"as name{SERIAL_MARK}tcp:host:port (no host:port found)")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"line {index}: channel {brief(raw)}: port "
                         f"{brief(port_text)} is not an integer") from None
    return name, TcpChannel(name=name, host=host, port=port)


def parse_serial_name(raw: str, index: int) -> tuple[str, SerialChannel] | None:
    """CSV 的通道列 -> `(通道名, SerialChannel)`；没有 `@` 就回 None（那是 TCP 那一路）。

    语法：`<通道名>@<串口名>[ <波特率>[ <帧格式>]]`（段之间空格）——
    `com-485-1@COM3`、`com-485-1@COM3 19200`、`com-485-1@COM3 19200 8E1`。
    只做**转换**：`9N1` 这种"形状对、值不合法"的解析得出来，留给 validate 报；
    形状就不对的（空的名字/串口名、多于三段、波特率不是数、帧格式不是 `8N1` 的样子）
    在这里就带行号拒掉——那不是一个值，是 CSV 里写坏的一格。
    """
    if SERIAL_MARK not in raw:
        return None
    name, _, spec = raw.partition(SERIAL_MARK)
    name = name.strip()
    if not name:
        raise ValueError(f"line {index}: channel {brief(raw)} has no name before "
                         f"{SERIAL_MARK!r} (write it as name{SERIAL_MARK}COM3)")
    fields = spec.split()
    if not 1 <= len(fields) <= 3:
        raise ValueError(
            f"line {index}: channel {brief(raw)} must be written as "
            f"name{SERIAL_MARK}PORT[ BAUD[ FRAME]] (at most three parts)")
    baudrate = SERIAL_DEFAULTS["baudrate"]
    if len(fields) >= 2:
        try:
            baudrate = int(fields[1])
        except ValueError:
            raise ValueError(f"line {index}: channel {brief(raw)}: baudrate "
                             f"{brief(fields[1])} is not an integer") from None
    frame = {"bytesize": SERIAL_DEFAULTS["bytesize"],
             "parity": SERIAL_DEFAULTS["parity"],
             "stopbits": SERIAL_DEFAULTS["stopbits"]}
    if len(fields) == 3:
        parsed = parse_frame(fields[2])
        if parsed is None:
            raise ValueError(f"line {index}: channel {brief(raw)}: frame "
                             f"{brief(fields[2])} must look like 8N1 "
                             f"(data bits / parity / stop bits)")
        frame = parsed
    return name, SerialChannel(name=name, port=fields[0], baudrate=baudrate, **frame)


def parse_s7_name(raw: str, index: int) -> tuple[str, S7Channel] | None:
    """CSV 的通道列 -> `(通道名, S7Channel)`；不是 S7 写法就回 None。

    语法：`<通道名>@<host>:<port>[ <rack>/<slot>]`，例 `s7-1@192.168.0.10:102 0/1`。

    与串口那条语法共用 `@` 标记，靠**第一段里有没有 `://`、有没有 `:`** 分开：pyserial 的
    URL（`socket://`、`spy://`）一定是串口；`host:port` 这种冒号写法是 S7；既没有 `://`
    又没有 `:` 的（`COM3`、`/dev/ttyUSB0`）是串口。**调用方必须先问 S7 再问串口**：反过来问的话
    `192.168.0.10:102` 会被串口那条路当成一个"串口名"收下（它不含空格、形状合法），
    于是一路 S7 变成了开着名叫 `192.168.0.10:102` 的串口——静默走错路。
    """
    if SERIAL_MARK not in raw:
        return None
    name, _, spec = raw.partition(SERIAL_MARK)
    name = name.strip()
    if not name:
        raise ValueError(f"line {index}: channel {brief(raw)} has no name before "
                         f"{SERIAL_MARK!r} (write it as name{SERIAL_MARK}host:port)")
    fields = spec.split()
    if not fields:
        return None                          # `name@` 这种残缺写法归串口那条路去报（消息更准）
    endpoint = fields[0]
    # **先分类再校验**：不是 S7 写法就立刻交回串口那条路，别拿 S7 的字段数去卡它
    # （`com-485-1@COM3 19200 8E1` 有三段，在 S7 这里"超了两段"，但它根本不是 S7）。
    if "://" in endpoint or ":" not in endpoint:
        return None                          # 那是串口那一路（pyserial URL 或纯端口名）
    if not 1 <= len(fields) <= 2:
        raise ValueError(f"line {index}: channel {brief(raw)} must be written as "
                         f"name{SERIAL_MARK}host:port[ rack/slot] (at most two parts)")
    host, _, raw_port = endpoint.rpartition(":")
    if not host or not raw_port.isdigit():
        raise ValueError(f"line {index}: channel {brief(raw)} must be written as "
                         f"name{SERIAL_MARK}host:port[ rack/slot]")
    rack, slot = S7_DEFAULTS["rack"], S7_DEFAULTS["slot"]
    if len(fields) == 2:
        parts = fields[1].split("/")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError(f"line {index}: channel {brief(raw)}: rack/slot "
                             f"{brief(fields[1])} must look like 0/1")
        rack, slot = int(parts[0]), int(parts[1])
    return name, S7Channel(name=name, host=host, port=int(raw_port), rack=rack, slot=slot)


def _address_cell_text(pt, channel) -> str:
    """写回 CSV 的地址格：S7 的 bool 要还原成 `字节.位`，否则位就丢了（往返不再等价）。"""
    if channel_transport(channel) == "s7" and pt.bit is not None:
        return f"{pt.address}.{pt.bit}"
    return str(pt.address)


def _address_cell(cell: str, channel, address_offset: int, index: int):
    """地址格 -> `(address, bit)`。S7 的 bool 写成 `字节.位`（`0.3`），其余是裸整数。

    只做转换：`0.9` 这种"形状对、位越界"解析得出来（`(0, 9)`），留给 validate 报；
    形状不对（`a.b`、`1.2.3`）在这里带行号拒——那不是一个值，是 CSV 里写坏的一格。
    """
    if channel_transport(channel) == "s7":
        parsed = parse_s7_address(cell)
        if parsed is None:
            raise ValueError(f"address {brief(cell)} is not a byte offset or a "
                             f"byte.bit pair (like 0.3)")
        address, bit = parsed
        return address + address_offset, bit
    return _require_int(cell, "address") + address_offset, None


def _spec_after_mark(raw_name: str) -> str:
    """`name@host` -> `host`（只为报错里能把那一格原样引出来）。"""
    return raw_name.partition(SERIAL_MARK)[2].split()[0] if SERIAL_MARK in raw_name else ""


def S7_MARK_LOOKS_WRONG(raw_name: str, row_area: str, row_datatype: str) -> bool:
    """`name@host`（有 `@`、没冒号）却配上 S7 的区/类型：几乎肯定是漏了端口。

    为什么要有这条：`name@host` 与"串口名叫 host"在语法上**一模一样**，只问通道名是分不出来的。
    而同一行的区/类型是现成的证据——`db1`/`real` 这些不是 Modbus 的写法，配上它就该带行号
    说清"你多半是把 S7 的 :port 漏了"，而不是把它当成一路开不起来的串口收下去。
    （同一张表里 Modbus 区只有 co/di/hr/ir、类型只有那 11 个，所以这条不会误伤。）
    """
    spec = _spec_after_mark(raw_name)
    if not spec or ":" in spec or "://" in spec:
        return False
    return parse_s7_area(row_area) is not None or row_datatype in S7_TYPES


def _channel_for(channels: dict, raw_name: str, ports: dict[str, int],
                 notes: list[str], index: int, row_area: str = "",
                 row_datatype: str = ""):
    """这一行属于哪个通道。串口写法（带 `@`）与 TCP（按名字猜端口）在这里分派。

    同一个通道名第二次出现时只做一致性检查：参数写法与第一次冲突就带行号拒掉——
    静默取第一个等于让 CSV 里一半的行在描述另一台设备。
    """
    # 顺序要紧：先问 `@tcp:`（它长得像 S7 的 host:port，不问在前会被 S7 那条路收下），
    # 再问 S7，最后问串口。
    wanted_tcp = parse_tcp_name(raw_name, index)
    wanted_s7 = None if wanted_tcp is not None else parse_s7_name(raw_name, index)
    parsed = (None if wanted_tcp is not None or wanted_s7 is not None
              else parse_serial_name(raw_name, index))
    if wanted_tcp is not None:
        name, wanted = wanted_tcp
    elif wanted_s7 is not None:
        name, wanted = wanted_s7
    elif parsed is not None:
        name, wanted = parsed
    else:
        name, wanted = raw_name, None
    if parsed is not None and S7_MARK_LOOKS_WRONG(raw_name, row_area, row_datatype):
        raise ValueError(
            f"line {index}: channel {brief(raw_name)} has no ':' before the port — "
            f"S7 channels are written as name{SERIAL_MARK}host:port (the point on this "
            f"row uses S7 area/datatype, so this looks like a missing port; a serial "
            f"channel on a port literally named {brief(_spec_after_mark(raw_name))} "
            f"would be written the same way)")
    if name in channels:
        existing = channels[name]
        if wanted is not None and channel_transport(existing) != channel_transport(wanted):
            raise ValueError(f"line {index}: channel {brief(name)} already appeared as a "
                             f"{channel_transport(existing)} channel, now with "
                             f"{_csv_channel_name(wanted)}")
        if wanted is not None and _channel_params(existing) != _channel_params(wanted):
            label = {"rtu": "serial", "s7": "S7"}.get(channel_transport(wanted), "TCP")
            raise ValueError(
                f"line {index}: channel {brief(name)} appears with two different "
                f"{label} settings ({_csv_channel_name(existing)} vs "
                f"{_csv_channel_name(wanted)})")
        return existing
    if wanted is not None:
        if name in ports and channel_transport(wanted) == "tcp":
            # `ports`（调用方给的端口）比文件里写的更优先——与原来"调用方最优先"同一条契约，
            # 只是现在文件里也可能带端口了。
            wanted.port = ports[name]
        channels[name] = wanted
        return wanted
    if name in ports:
        port = ports[name]
    else:
        port = _guessed_port(name)
        notes.append(f"channel {name!r}: port guessed as {port} from its name, "
                     f"confirm it in the project settings")
    channels[name] = TcpChannel(name=name, host="0.0.0.0", port=port)
    return channels[name]


def _channel_params(channel) -> tuple:
    """通道参数的可比形式（同一个名字第二次出现时比"说的是不是同一台设备"）。"""
    transport = channel_transport(channel)
    if transport == "s7":
        return (channel.host, channel.port, channel.rack, channel.slot)
    if transport == "tcp":
        return (channel.host, channel.port)
    return _serial_params(channel)


def _serial_params(channel) -> tuple:
    """串口参数的可比形式：只用来比"两次写法说的是不是同一台设备"。"""
    return (channel.port, channel.baudrate, channel.parity, channel.bytesize,
            channel.stopbits, channel.timeout)


def port_from_name(name: str) -> int:
    """CSV 里 TCP 通道的端口从**名字尾数**读（读不出回 502）。

    与导入用的是同一个函数（`read_csv` 那句"猜了要说"的 note 就是它）：导出那边要检查
    "端口是否真的写在名字里"，两边必须是同一套读法，否则导出侧会报一个导入侧不认的结论。
    """
    return _guessed_port(name)


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
