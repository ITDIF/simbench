"""改工程的唯一入口：新建、增删通道 / 设备 / 点位。本文件不得 import PySide6 / pymodbus。

为什么单独一层：界面里"加一个点位"不是一个动作，而是**四件事**——分配一个不与现有点
重叠的地址、改工程对象、按全量判定验一遍、验不过就整体回滚。前两件是数据形状，第三件
住在 `validate`，第四件是这三件凑起来才有的语义；把它留在界面里，下一次换个界面（或
命令行/脚本）就得再抄一遍"验不过就回滚"，而抄漏一处的表现是**工程里留下一个起不来的点、
界面显示的却是另一个**——本项目最防的那类假象。

改动管线与既有的两条编辑路（`gui/channel_panel._commit`、`gui/point_props.apply`）**同一个
形状**：改 → 全量 `validate_project` → 与改前的 error 清单做差集 → 有新增就整体回滚。
差集而不是"看有没有错"：点表**带着 error 也能装载**（spec §8，操作员开属性面板往往就是
为了修那一行），加一个点不该被**别人的**旧错误拦住，只该被**自己引入的**新错误拦住。

判定一条都不在这里：类型搭不搭配、地址越界、重名、重叠，全部由 `validate_project` 说，
本层只负责"把候选答案摆进去、验、不行就撤回"。
"""

from __future__ import annotations

from .model import (AREAS, BOOL, FLOAT32, S7_AREAS, S7_DEFAULTS, S7_MAX_DB,
                    S7_TYPE_BYTES, SERIAL_DEFAULTS, TcpChannel, UINT16, WORD_ABCD,
                    Device, Point, Project, S7Channel, SerialChannel, area_is_bit,
                    channel_transport, parse_s7_area, register_span, s7_span_bytes)
from .validate import error_lines, validate_project

# 能从界面从零建的三种传输。名字与 `channel_transport()` 的返回值一字不差——
# 两处各写一份字符串迟早对不上，界面上"选了 TCP 却去建串口"就这么来的。
TRANSPORTS = ("tcp", "rtu", "s7")
BLANK = "blank"                     # "新建工程"里的空白起步（不是一种传输，别混进 TRANSPORTS）

# TCP 默认端口从 5020 起：502 要管理员权限、首次监听还要放行防火墙（spec §13），
# 而 5020 是本项目现场一直在用的那个。跳过已被占用的，让"再加一条"不会一加就报重复。
DEFAULT_TCP_PORT = 5020
DEFAULT_S7_PORT = S7_DEFAULTS["port"]         # S7 的惯例端口就是 102（ISO-on-TCP）
# 口径：默认端口只对 TCP 是"从 5020 起"，S7 照惯例给 102。**不给 S7 加"1024 以下不行"的
# 下界**：1024 以下是 Unix 的特权端口，Windows 上普通用户也能绑 102（现场就是这么用的），
# 加一条下界会让 S7 通道默认跑到一个非标准端口上去。
# Modbus 的裸协议地址上界（`validate._shape` 的那条）：分配时要连跨度一起算，
# 因为 validate 只看首地址，`65535` 上放一个 4 寄存器的点它不报，真跑起来主站会读到越界。
ADDRESS_MAX = 65535
UNIT_MIN, UNIT_MAX = 1, 247


class EditRefused(ValueError):
    """这次改动被拒。理由逐条摊在 `reasons` 里，界面原样打印——不许只给一句"失败"。"""

    def __init__(self, reasons) -> None:
        self.reasons = [str(reason) for reason in reasons]
        super().__init__("; ".join(self.reasons) if self.reasons else "refused")


# ---------- 分配器：只读现有工程，算一个"不会一加就撞车"的默认值 ----------

def _channel_names(project) -> set[str]:
    return {ch.name for ch in project.channels}


def next_channel_name(project, transport: str) -> str:
    """`tcp-5020` / `rtu-1` / `s7-1` 这类默认名，重名就递增。

    TCP 的名字里带端口是**既有约定**（CSV 没有端口列，导入时按名字尾数认：
    `csv_io._guessed_port`，示例点表里的 `tcp-5020` 就是它）。所以 TCP 的名字由端口算，
    不在这里给序号；串口/S7 的参数在名字**后面的 `@` 段**里，名字本身只当标签。
    """
    used = _channel_names(project)
    if transport == "tcp":
        port = next_port(project, DEFAULT_TCP_PORT)
        return f"tcp-{port}"
    stem = "rtu" if transport == "rtu" else "s7"
    serial = 1
    while f"{stem}-{serial}" in used:
        serial += 1
    return f"{stem}-{serial}"


def next_port(project, start: int = DEFAULT_TCP_PORT) -> int:
    """`start` 起第一个没被**任何**通道占用的端口。

    为什么不按 host 分开算：`0.0.0.0:5020` 与 `127.0.0.1:5020` 在静态校验里是两个不同的
    键（`validate` 判的是 (host, port)），但绑起来第二个必失败——`0.0.0.0` 已经把所有
    网卡上的这个端口吃掉了。分配器宁可给一个偏保守的答案：凡是这个工程里用掉的端口，
    下一个通道就绕开。
    """
    used = {ch.port for ch in project.channels}
    port = int(start)
    while port in used:
        port += 1
    if port > ADDRESS_MAX:
        raise EditRefused([f"no free port from {start} up"])
    return port


def next_device_name(channel) -> str:
    """从 1 起找**最小的空号**（与 `next_unit_id` 同一条口径：补空档而不是一路加下去）。

    "一路加下去"（拿设备个数 + 1 当起点）在删过设备的工程上会跳号：删掉 `设备1` 之后
    再加一个，得到的是 `设备4` 而界面里只剩两个设备——操作员对着这个号没法判断是不是
    重复了。
    """
    used = {dev.name for dev in channel.devices}
    serial = 1
    while f"设备{serial}" in used:
        serial += 1
    return f"设备{serial}"


def next_unit_id(channel) -> int:
    """该通道里没被占用的最小 unit。**同一通道内**唯一是校验拦下的错（`unit_duplicated`）。"""
    used = {dev.unit_id for dev in channel.devices}
    for unit in range(UNIT_MIN, UNIT_MAX + 1):
        if unit not in used:
            return unit
    raise EditRefused([f"channel {channel.name}: all unit ids {UNIT_MIN}..{UNIT_MAX} are used"])


def next_point_name(device) -> str:
    """同上：取最小的空号，删过点位之后不跳号。"""
    used = {pt.name for pt in device.points}
    serial = 1
    while f"点位{serial}" in used:
        serial += 1
    return f"点位{serial}"


def _occupied_end(device, area: str, s7: bool) -> int:
    """这个设备在这个区里已经占到的**末尾**（开区间）。位置算不出来（脏地址/脏类型）的点跳过。

    跳过而不是抛：`edit` 经常在**带着错**的工程上工作（那份点表本来就是被开出来修的），
    一个脏点不该让"再加一个"变成不可能。真正该报的错由 validate 在改完之后报。
    """
    end = 0
    for pt in device.points:
        if pt.area != area or not isinstance(pt.address, int) or pt.address < 0:
            continue
        if s7:
            if pt.datatype == BOOL and isinstance(pt.bit, int):
                start = pt.address * 8 + pt.bit
                end = max(end, start + 1)
            else:
                # 类型不认识时 `s7_span_bytes` 给 1（它自己那条口径），脏点照样占一格，
                # 不会让"再加一个"直接落到它头上
                end = max(end, pt.address * 8 + 8 * s7_span_bytes(pt.datatype, pt.initial))
            continue
        if area_is_bit(area):
            end = max(end, pt.address + 1)
        else:
            try:
                end = max(end, pt.address + register_span(pt.datatype, pt.initial))
            except ValueError:
                continue
    return end


def next_point_address(device, area: str, s7: bool,
                       bit_point: bool = False) -> tuple[int, int | None]:
    """这个区里第一个不与现有点重叠的位置，回 `(address, bit)`。

    规则只有一条：取"已占末尾"当新点的起点。Modbus 的位区与寄存器区、S7 的字节与位
    都走同一个末尾规则，只是末尾的单位不同（位 / 寄存器 / 位）。重叠判定本身住在
    `validate`（Modbus 按区、S7 按**位**），这里只保证默认值不会撞上去。

    `bit_point` 区分 S7 的两种落点：**bool 占一位**，可以落在任意位上；其余类型占整字节，
    必须落在字节起点（`validate` 的 `s7_bit` 明写"bit 只有 bool 点有"，给一个非 bool 点
    带上 bit 会被它拒掉——所以这里对非 bool 一律回 `bit=None`，并向上取整到下一个字节）。
    """
    end = _occupied_end(device, area, s7)
    if s7:
        address, bit = divmod(end, 8) if bit_point else ((end + 7) // 8, None)
        if address > ADDRESS_MAX:
            raise EditRefused([f"device {device.name}: area {area} is full "
                               f"(next byte {address} is past {ADDRESS_MAX})"])
        return address, bit
    if end > ADDRESS_MAX:
        raise EditRefused([f"device {device.name}: area {area} is full "
                           f"(next address {end} is past {ADDRESS_MAX})"])
    return end, None


def default_point(device, area: str, s7: bool, datatype: str | None = None) -> Point:
    """一个新点的默认值：名字唯一、地址不重叠、类型取该区/该传输最普通的那一格。

    类型只给"最普通"的一种（Modbus `uint16`、S7 `word`），别的留给属性面板改：
    那 10 个字段的表单 `point_props` 已经有一份，在这里再摆一份必然漂移。
    位区（Modbus 的 `co`/`di`）与显式要 bool 的调用，按位点分配。
    """
    if datatype is None:
        datatype = BOOL if (not s7 and area_is_bit(area)) else ("word" if s7 else UINT16)
    bit_point = datatype == BOOL
    address, bit = next_point_address(device, area, s7, bit_point=bit_point)
    return Point(name=next_point_name(device), area=area,
                 address=address, datatype=datatype,
                 initial="0", word_order=WORD_ABCD, bit=bit)


def default_area(transport: str) -> str:
    """新点落在哪个区：Modbus 用 `hr`（最常用），S7 用 `db1`。"""
    return "db1" if transport == "s7" else "hr"


def default_channel(project, transport: str, name: str | None = None, **overrides):
    """建一条默认通道。`overrides` 原样盖到字段上（界面表单交过来的就是这些）。"""
    name = name if name else next_channel_name(project, transport)
    if transport == "tcp":
        return TcpChannel(name=name,
                          host=overrides.get("host", "0.0.0.0"),
                          port=overrides.get("port", next_port(project, DEFAULT_TCP_PORT)))
    if transport == "rtu":
        return SerialChannel(name=name,
                             port=overrides.get("port", SERIAL_DEFAULTS["port"]),
                             baudrate=overrides.get("baudrate", SERIAL_DEFAULTS["baudrate"]),
                             parity=overrides.get("parity", SERIAL_DEFAULTS["parity"]),
                             bytesize=overrides.get("bytesize", SERIAL_DEFAULTS["bytesize"]),
                             stopbits=overrides.get("stopbits", SERIAL_DEFAULTS["stopbits"]),
                             timeout=overrides.get("timeout", SERIAL_DEFAULTS["timeout"]))
    if transport == "s7":
        return S7Channel(name=name,
                         host=overrides.get("host", S7_DEFAULTS["host"]),
                         port=overrides.get("port", next_port(project, DEFAULT_S7_PORT)),
                         rack=overrides.get("rack", S7_DEFAULTS["rack"]),
                         slot=overrides.get("slot", S7_DEFAULTS["slot"]))
    raise EditRefused([f"unknown transport {transport!r}"])


def new_project(transport: str, **values) -> Project:
    """从零建一份工程：`BLANK` 回空工程，否则一条通道 + 一个设备 + 一个点位的骨架。

    整份**必须零 error**（走的还是同一条"改 → 验 → 不行就抛"的管线，只是一步一步来）：
    新工程没有"别人的旧错"可以豁免，带着错的起点不如不建——所以串口名留空（那是
    `default_channel` 的默认，不替现场猜 COM 口）时这里会带着 `validate` 的原话拒绝，
    由对话框把话摆到屏幕上让操作员自己填。

    `values` 原样转给 `default_channel`（界面表单交回来的名字与参数就是它）。
    """
    project = Project()
    if transport == BLANK:
        return project
    channel = default_channel(project, transport, **values)
    add_channel(project, channel)
    device = Device(name=next_device_name(channel), unit_id=next_unit_id(channel))
    add_device(project, channel.name, device)
    add_point(project, channel.name, device.unit_id,
              default_point(device, default_area(transport), transport == "s7"))
    return project


# ---------- 改动：改 → 验 → 差集 → 不行就回滚 ----------

def _apply(project, mutate, undo) -> None:
    """本文件唯一的管线（见模块头）。`mutate()` 就地改，`undo()` 把它撤回。"""
    before = set(error_lines(validate_project(project)))
    mutate()
    new = [line for line in error_lines(validate_project(project)) if line not in before]
    if new:
        undo()
        raise EditRefused(new)


def _find_channel(project, name: str):
    found = next((ch for ch in project.channels if ch.name == name), None)
    if found is None:
        raise KeyError(name)
    return found


def _find_device(channel, unit_id: int):
    found = next((dev for dev in channel.devices if dev.unit_id == unit_id), None)
    if found is None:
        raise KeyError(unit_id)
    return found


def add_channel(project, channel) -> None:
    _apply(project, lambda: project.channels.append(channel),
           lambda: project.channels.remove(channel))


def remove_channel(project, name: str) -> None:
    channel = _find_channel(project, name)

    def undo() -> None:
        # 插回原来的位置：通道顺序就是界面上的行序，也是 CSV 的行序
        project.channels.insert(index[0], channel)

    index = [project.channels.index(channel)]
    _apply(project, lambda: project.channels.remove(channel), undo)


def add_device(project, channel_name: str, device) -> None:
    channel = _find_channel(project, channel_name)
    _apply(project, lambda: channel.devices.append(device),
           lambda: channel.devices.remove(device))


def remove_device(project, channel_name: str, unit_id: int) -> None:
    channel = _find_channel(project, channel_name)
    device = _find_device(channel, unit_id)
    index = [channel.devices.index(device)]
    _apply(project, lambda: channel.devices.remove(device),
           lambda: channel.devices.insert(index[0], device))


def find_device(project, channel_name: str, unit_id: int):
    """按通道 + unit 找设备本体（界面要它来算新点位的默认区/地址）。找不到抛 KeyError。"""
    return _find_device(_find_channel(project, channel_name), unit_id)


def add_point(project, channel_name: str, unit_id: int, point) -> None:
    device = _find_device(_find_channel(project, channel_name), unit_id)
    _apply(project, lambda: device.points.append(point),
           lambda: device.points.remove(point))


def find_point(device, area: str, address: int) -> Point:
    """按区+地址找点位。**同一地址上有多个就直接拒**，不猜是哪一个。

    这一条只在 S7 上会真发生（`DBX6.2` 与 `DBX6.3` 同占一个字节是合法的，判定按位），
    而界面这一层的身份键里还没有"位"（见本计划的 §2 第 8 条）。猜着删一个，现场丢的是
    另一个点——那比"删不掉"难查得多，所以这里明说。
    """
    found = [pt for pt in device.points if pt.area == area and pt.address == address]
    if not found:
        raise KeyError((area, address))
    if len(found) > 1:
        names = "/".join(pt.name for pt in found)
        raise EditRefused([f"{area} {address}: {len(found)} points share this address "
                           f"({names}); this build cannot tell them apart"])
    return found[0]


def remove_point(project, channel_name: str, unit_id: int, area: str, address: int) -> None:
    device = _find_device(_find_channel(project, channel_name), unit_id)
    point = find_point(device, area, address)
    index = [device.points.index(point)]
    _apply(project, lambda: device.points.remove(point),
           lambda: device.points.insert(index[0], point))