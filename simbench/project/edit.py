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

from collections import Counter

from .model import (
    BOOL,
    S7_DEFAULTS,
    SERIAL_DEFAULTS,
    UINT16,
    WORD_ABCD,
    Device,
    Point,
    Project,
    S7Channel,
    SerialChannel,
    TcpChannel,
    area_is_bit,
    is_serial,
    is_socket,
    register_span,
    s7_span_bytes,
)
from .validate import error_lines, validate_project

# 能从界面从零建的三种传输。名字与 `channel_transport()` 的返回值一字不差——
# 两处各写一份字符串迟早对不上，界面上"选了 TCP 却去建串口"就这么来的。
# 能"从零建一条"的传输（顺序就是界面上按钮的顺序）：socket 三种、串口两种、S7。
# `MODBUS_TRANSPORTS` 那份清单在 `model.py`（它同时给 validate/后端工厂用）。
TRANSPORTS = ("tcp", "udp", "rtutcp", "rtu", "ascii", "s7")
BLANK = "blank"                     # "新建工程"里的空白起步（不是一种传输，别混进 TRANSPORTS）

# TCP 默认端口从 5020 起：502 要管理员权限、首次监听还要放行防火墙（spec §13），
# 而 5020 是本项目现场一直在用的那个。跳过已被占用的，让"再加一条"不会一加就报重复。
DEFAULT_TCP_PORT = 5020
DEFAULT_S7_PORT = S7_DEFAULTS["port"]         # S7 的惯例端口就是 102（ISO-on-TCP）
# 口径：默认端口只对 TCP 是"从 5020 起"，S7 照惯例给 102。**不给 S7 加"1024 以下不行"的
# 下界**：1024 以下是 Unix 的特权端口，Windows 上普通用户也能绑 102（现场就是这么用的），
# 加一条下界会让 S7 通道默认跑到一个非标准端口上去。
# Modbus 的裸协议地址上界（`validate._shape` 的那条）：分配时要连**跨度**一起算，
# 因为 validate 只看首地址——`65535` 上放一个 4 寄存器的点它不报，真跑起来主站会读到越界。
# 所以这里的判据是"新点的**末格**也要在范围内"，不是"首地址在范围内"。
ADDRESS_MAX = 65535
# 端口的上界与地址上界恰好都是 65535，但它们是两件事：合成一个常量，改端口范围时
# 会顺手把地址范围也改掉。分开写，各自只有一处。
PORT_MAX = 65535
UNIT_MIN, UNIT_MAX = 1, 247


class EditRefused(ValueError):
    """这次改动被拒。理由逐条摊在 `reasons` 里，界面原样打印——不许只给一句"失败"。"""

    def __init__(self, reasons) -> None:
        self.reasons = [str(reason) for reason in reasons]
        super().__init__("; ".join(self.reasons) if self.reasons else "refused")


# ---------- 分配器：只读现有工程，算一个"不会一加就撞车"的默认值 ----------

def _channel_names(project) -> set[str]:
    return {ch.name for ch in project.channels}


def next_channel_name(project, transport: str, probe=None) -> str:
    """`tcp-5020` / `rtu-1` / `s7-1` 这类默认名，重名就递增。

    TCP 的名字里带端口是**既有约定**（CSV 没有端口列，导入时按名字尾数认：
    `csv_io._guessed_port`，示例点表里的 `tcp-5020` 就是它）。所以 TCP 的名字由端口算，
    不在这里给序号；串口/S7 的参数在名字**后面的 `@` 段**里，名字本身只当标签。
    """
    used = _channel_names(project)
    if transport == "tcp":
        # 名字里的端口要跟**真正会用**的那个一致（探针一起传下去）：名字是 `tcp-5020` 而
        # 端口是 5021 的话，导出的 CSV 与人对名字的期待都会对不上。
        port = next_port(project, DEFAULT_TCP_PORT, probe)
        return f"tcp-{port}"
    stem = "rtu" if transport == "rtu" else "s7"
    serial = 1
    while f"{stem}-{serial}" in used:
        serial += 1
    return f"{stem}-{serial}"


def next_port(project, start: int = DEFAULT_TCP_PORT, probe=None) -> int:
    """`start` 起第一个没被**任何**通道占用、（给了 `probe` 时）系统上也真空着的端口。

    `probe(port) -> bool` 由调用方注入（界面给的是 `engine.ports.is_free`）：这一层不许
    import backend，而"这个端口现在有没有人占"要真去绑一次才知道。不给 probe 就只避开
    工程内的冲突（既有行为不变）。

    为什么不按 host 分开算：`0.0.0.0:5020` 与 `127.0.0.1:5020` 在静态校验里是两个不同的
    键（`validate` 判的是 (host, port)），但绑起来第二个必失败——`0.0.0.0` 已经把所有
    网卡上的这个端口吃掉了。分配器宁可给一个偏保守的答案：凡是这个工程里用掉的端口，
    下一个通道就绕开。
    """
    used = {ch.port for ch in project.channels}
    port = int(start)
    while port in used or (probe is not None and not probe(port)):
        port += 1
    if port > PORT_MAX:
        raise EditRefused([f"no free port from {start} up (past {PORT_MAX})"])
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


def _occupied_end(device, area: str, transport: str) -> int:
    """这个设备在这个区里已经占到的**末尾**（开区间）。位置算不出来（脏地址/脏类型）的点跳过。

    跳过而不是抛：`edit` 经常在**带着错**的工程上工作（那份点表本来就是被开出来修的），
    一个脏点不该让"再加一个"变成不可能。真正该报的错由 validate 在改完之后报。
    """
    end = 0
    for pt in device.points:
        if pt.area != area or not isinstance(pt.address, int) or pt.address < 0:
            continue
        if transport == "s7":
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


def next_point_address(device, area: str, transport: str, bit_point: bool = False,
                       span: int = 1) -> tuple[int, int | None]:
    """这个区里第一个不与现有点重叠的位置，回 `(address, bit)`。

    规则只有一条：取"已占末尾"当新点的起点。Modbus 的位区与寄存器区、S7 的字节与位
    都走同一个末尾规则，只是末尾的单位不同（位 / 寄存器 / 位）。重叠判定本身住在
    `validate`（Modbus 按区、S7 按**位**），这里只保证默认值不会撞上去。

    `bit_point` 区分 S7 的两种落点：**bool 占一位**，可以落在任意位上；其余类型占整字节，
    必须落在字节起点（`validate` 的 `s7_bit` 明写"bit 只有 bool 点有"，给一个非 bool 点
    带上 bit 会被它拒掉——所以这里对非 bool 一律回 `bit=None`，并向上取整到下一个字节）。

    `span` 是**新点要占几格**（与地址同一个单位：Modbus 是寄存器/位、S7 是字节）。判据是
    "末格也要在范围内"：validate 只看首地址，`65535` 上放一个占 4 格的点它不报，真跑起来
    主站会读到越界——分配这一层必须替它把住（今天界面只用跨度 1 的类型，但这是公开 API）。
    """
    end = _occupied_end(device, area, transport)
    if transport == "s7":
        address, bit = divmod(end, 8) if bit_point else ((end + 7) // 8, None)
    else:
        address, bit = end, None
    last = address + max(int(span), 1) - 1
    if last > ADDRESS_MAX:
        raise EditRefused([f"device {device.name}: area {area} is full "
                           f"(a {span}-wide point would reach {last}, past {ADDRESS_MAX})"])
    return address, bit


def _point_span(datatype: str, area: str, transport: str) -> int:
    """新点要占几格（单位与地址一致）。类型不认识时回 1：那份点会被 validate 拦下，
    这里只要不算错格子、不把邻居顶掉就行。"""
    try:
        if transport == "s7":
            return s7_span_bytes(datatype, "0")
        if area_is_bit(area):
            return 1
        return register_span(datatype, "0")
    except ValueError:
        return 1


def default_point(device, area: str, transport: str, datatype: str | None = None) -> Point:
    """一个新点的默认值：名字唯一、地址不重叠、类型取该区/该传输最普通的那一格。

    类型只给"最普通"的一种（Modbus `uint16`、S7 `word`），别的留给属性面板改：
    那 10 个字段的表单 `point_props` 已经有一份，在这里再摆一份必然漂移。
    位区（Modbus 的 `co`/`di`）与显式要 bool 的调用，按位点分配。
    """
    if datatype is None:
        datatype = (BOOL if (transport != "s7" and area_is_bit(area))
                    else ("word" if transport == "s7" else UINT16))
    bit_point = datatype == BOOL
    address, bit = next_point_address(device, area, transport, bit_point=bit_point,
                                      span=_point_span(datatype, area, transport))
    return Point(name=next_point_name(device), area=area,
                 address=address, datatype=datatype,
                 initial="0", word_order=WORD_ABCD, bit=bit)


def default_area(transport: str) -> str:
    """新点落在哪个区：Modbus 用 `hr`（最常用），S7 用 `db1`。"""
    return "db1" if transport == "s7" else "hr"


def default_channel(project, transport: str, name: str | None = None, probe=None,
                    **overrides):
    """建一条默认通道。`overrides` 原样盖到字段上（界面表单交过来的就是这些）。

    `probe` 只在**没给端口**时用得上（给了就照给的来）：它是"这个端口系统上现在空着吗"，
    由界面从 `engine.ports` 注进来（见 `next_port`）。
    """
    name = name if name else next_channel_name(project, transport, probe)
    if is_socket(transport):
        # tcp / udp / rtutcp：只差 `type`（默认端口同一套，现场都是 5020 起）
        return TcpChannel(name=name, type=transport,
                          host=overrides.get("host", "0.0.0.0"),
                          port=overrides.get("port",
                                             next_port(project, DEFAULT_TCP_PORT, probe)))
    if is_serial(transport):
        # rtu / ascii：同一组串口参数，只差帧类型
        return SerialChannel(name=name,
                             framing="ascii" if transport == "ascii" else "rtu",
                             port=overrides.get("port", SERIAL_DEFAULTS["port"]),
                             baudrate=overrides.get("baudrate", SERIAL_DEFAULTS["baudrate"]),
                             parity=overrides.get("parity", SERIAL_DEFAULTS["parity"]),
                             bytesize=overrides.get("bytesize", SERIAL_DEFAULTS["bytesize"]),
                             stopbits=overrides.get("stopbits", SERIAL_DEFAULTS["stopbits"]),
                             timeout=overrides.get("timeout", SERIAL_DEFAULTS["timeout"]))
    if transport == "s7":
        return S7Channel(name=name,
                         host=overrides.get("host", S7_DEFAULTS["host"]),
                         port=overrides.get("port",
                                            next_port(project, DEFAULT_S7_PORT, probe)),
                         rack=overrides.get("rack", S7_DEFAULTS["rack"]),
                         slot=overrides.get("slot", S7_DEFAULTS["slot"]))
    raise EditRefused([f"unknown transport {transport!r}"])


def new_project(transport: str, probe=None, **values) -> Project:
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
    channel = default_channel(project, transport, probe=probe, **values)
    add_channel(project, channel)
    device = Device(name=next_device_name(channel), unit_id=next_unit_id(channel))
    add_device(project, channel.name, device)
    add_point(project, channel.name, device.unit_id,
              default_point(device, default_area(transport), transport))
    return project


# ---------- 改动：改 → 验 → 差集 → 不行就回滚 ----------

def _apply(project, mutate, undo) -> None:
    """本文件唯一的管线（见模块头）。`mutate()` 就地改，`undo()` 把它撤回。

    `undo()` 在两条路上都会走：**验出新增错误**（正常的拒绝），以及**验的过程自己炸了**
    （对象根本不是个通道之类，那是调用方的 bug）。后一条也要撤回——"抛出去的时候工程里
    留着半成品"比抛本身更难查。

    差集按**条数**比（`Counter`）而不是"有没有出现过"（集合）：校验的错误行是字符串，
    同一句话可能出现两次（工程里本来就有两条同名通道、再加第三条），用集合比会把新来的
    那一条当成旧的吞掉——那正是"加了不该加的"。`channel_panel`/`point_props` 那两处比的是
    **字段**改动前后的清单，语义上不会出现"同一条错多一份"，所以它们仍用集合：不是两种口径，
    是同一条口径在两个形状下的正确算法。
    """
    before = Counter(error_lines(validate_project(project)))
    mutate()
    try:
        after = Counter(error_lines(validate_project(project)))
    except Exception:
        undo()
        raise
    new = list((after - before).elements())
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


def find_point(device, area: str, address: int, bit: int | None = None) -> Point:
    """按 区 + 地址 + **位** 找点位。身份里带位，所以同字节上的两个 S7 bool 分得开。

    `bit=None` 时若同地址上不止一个点（S7 的 `DBX6.2` 与 `DBX6.3`），仍然拒——
    调用方没给位就说明它不知道自己要点哪一个，猜着删一个、现场丢的是另一个点。
    这条拒绝不是"这一层做不到"（现在做得到），是"你没告诉我哪个"。
    """
    found = [pt for pt in device.points
             if pt.area == area and pt.address == address and pt.bit == bit]
    if not found:
        if bit is None:
            same_address = [pt for pt in device.points
                            if pt.area == area and pt.address == address]
            if same_address:
                names = "/".join(pt.name for pt in same_address)
                raise EditRefused([f"{area} {address}: {len(same_address)} points share "
                                   f"this address ({names}); pass the bit to pick one"])
        raise KeyError((area, address, bit))
    return found[0]


def remove_point(project, channel_name: str, unit_id: int, area: str, address: int,
                 bit: int | None = None) -> None:
    device = _find_device(_find_channel(project, channel_name), unit_id)
    point = find_point(device, area, address, bit)
    index = [device.points.index(point)]
    _apply(project, lambda: device.points.remove(point),
           lambda: device.points.insert(index[0], point))