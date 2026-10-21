"""启动前校验：一次报全，不撞第一个就崩。本文件不得 import pymodbus。"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    AREAS,
    BOOL,
    DATATYPES,
    S7_MAX_DB,
    S7_TYPES,
    SERIAL_FRAMINGS,
    SERIAL_MARK,
    STRING,
    WAVEFORMS,
    WORD_ABCD,
    WORD_ORDERS,
    MqttChannel,
    S7Channel,
    SerialChannel,
    area_is_bit,
    brief,
    brief_name,
    channel_transport,
    is_socket,
    parse_s7_area,
    register_span,
    s7_area_name,
    s7_span_bytes,
    word_orders_allowed,
)
from .mqtt_topic import value_topic

# 串口通道的合法集（M5 口径 4）。白名单而不是范围：现场能用的波特率就这几个，写 9500
# 这种"看着也像数"的值进来，真跑起来是收发全乱、报文窗里一片 CRC 错，比当场报错难查得多。
# 与 `model.SERIAL_DEFAULTS` 的分工：默认值在模型层，合法集在判定层，两份各自只此一份。
SERIAL_BAUDRATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
SERIAL_PARITIES = ("N", "E", "O")
SERIAL_BYTESIZES = (7, 8)
SERIAL_STOPBITS = (1, 2)

# 波形参数表（spec §5）。M4 实现波形时必须复用这张表，不要再抄一份。
WAVE_SPEC: dict[str, tuple[set[str], set[str]]] = {
    "const": ({"value"}, set()),
    "ramp": ({"from", "to", "speed"}, {"hold"}),
    "sine": ({"base", "amp", "period"}, {"phase"}),
    "random": ({"min", "max"}, {"interval", "seed"}),
    "step": ({"values", "interval"}, set()),
}
if set(WAVE_SPEC) != set(WAVEFORMS):
    # 波形合法取值有两个来源就会漂移，宁可导入即失败也不要留到运行时（裁决 H）
    raise RuntimeError("WAVE_SPEC keys drifted from model.WAVEFORMS")
_POSITIVE = {"period", "speed", "interval", "hold"}
# “单寄存器类型”= 只允许 ABCD 的那些，由 model 的字序允许集派生，不在这儿手抄类型名
# （同裁决 M 对 codec._NUMERIC 的要求：抄一份就会漂移，model 加类型时这里必然漏）。
# string 是唯一例外：它也只允许 ABCD，但填别的是语义分歧，必须走 error 而不是 warning。
_SINGLE = {d for d in DATATYPES if d != STRING and word_orders_allowed(d) == (WORD_ABCD,)}


@dataclass(frozen=True)
class Issue:
    level: str          # "error" | "warning"
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.where}: {self.message}"


def error_lines(issues: list[Issue]) -> list[str]:
    """拦住启动的那几条，逐行给命令行/GUI 打印（warning 不进清单）。"""
    return [str(i) for i in issues if i.level == "error"]


def validate_project(project) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[str] = set()
    bound: dict[tuple[str, int], str] = {}      # (host, port) -> 最先占用的通道名
    bound_serial: dict[str, str] = {}           # 串口名 -> 最先占用的通道名
    for ch in project.channels:
        # `where` 是清单每行开头的定位串，身份名原样拼进来时一个 400 字符的通道名能把
        # 整屏刷掉（真出现：厂商把整段说明塞进通道名）。判定不动，只截渲染（同裁决 N）。
        name = brief_name(ch.name)
        if ch.name in seen:
            issues.append(Issue("error", "channel_duplicated", name,
                                f"channel name {brief(ch.name)} used twice"))
        seen.add(ch.name)
        # 通道名里不许有 `@`（M5）：CSV 的通道列用 `name@COM3 9600 8N1` 表示串口通道，
        # 名字里再带一个就把那条语法变成猜谜——导出一份再导回来会解析成另一路通道，
        # 而"导入导出丢设备"是现场最难接受的一类。改成别的名字即可，判定不猜。
        if SERIAL_MARK in (ch.name or ""):
            issues.append(Issue("error", "channel_name", name,
                                f"channel name must not contain {SERIAL_MARK!r}; CSV writes "
                                f"a serial channel as name{SERIAL_MARK}COM3 9600 8N1 and an "
                                f"S7 channel as name{SERIAL_MARK}host:port"))
        # 分派：TCP 那几条判定一个字没动，串口走自己那一份，未知 type 仍然报 channel_type。
        # 判据用**类**而不是 `channel_transport()`：这里问的是"这个对象身上有哪些字段"，
        # 而 `TcpChannel(type="rtu")` 那种自相矛盾的对象（只可能来自代码或手拼测试）
        # 只有 TCP 的字段——拿它去判串口参数会变成一串 AttributeError 式的假错，
        # 报一句 channel_type 才是它真正的问题。外部的"这路是什么传输"仍以
        # `channel_transport()` 为准（后端工厂、CSV、界面都走它）。
        transport = channel_transport(ch)
        port_ok = host_ok = False
        serial_key: str | None = None
        if isinstance(ch, SerialChannel):
            serial_key = _serial(ch, issues, name)
        elif isinstance(ch, S7Channel):
            host_ok, port_ok = _s7(ch, issues, name)
        elif isinstance(ch, MqttChannel):
            host_ok, port_ok = _mqtt(ch, issues, name)
        elif is_socket(transport):
            # tcp / udp / rtutcp 共用同一套 host:port 判定（它们就是同一个形状，
            # 只差帧怎么切——那是后端的事，不是点表的事）
            host_ok, port_ok = _tcp(ch, issues, name)
        else:
            issues.append(Issue("error", "channel_type", name,
                                f"channel type {brief(transport)} is not supported"))
        _devices(ch, issues)
        # 同 host+port 的两个通道要等启动第二阶段才炸 bind，Windows 只回一句 10048、
        # 不带通道名，现场看不出是点表配置错：静态查得出的冲突就静态报完（裁决 R）。
        # 脏 port 与脏 host 上面两条已各自说完，键也构造不出来，一起跳过（裁决 O/T）。
        # MQTT 连的是 broker，多个通道共用同一个 host:port 是正常用法，不能按
        # "监听端口重复"报错（那种检查只对真正 bind 的 tcp/udp/rtutcp/s7 有意义）。
        if port_ok and host_ok and not isinstance(ch, MqttChannel):
            key = (ch.host, ch.port)
            if key in bound:
                issues.append(Issue("error", "port_duplicated", name,
                                    f"port {ch.port} on host {brief_name(ch.host)} "
                                    f"already used by channel {brief_name(bound[key])}"))
            else:
                bound[key] = ch.name
        # 串口同理：两个通道抢同一个串口，现象是"一路能收一路全错帧"，而 Windows 只会说
        # Access is denied。静态查得出的冲突就静态报完。
        if serial_key is not None:
            if serial_key in bound_serial:
                issues.append(Issue("error", "serial_duplicated", name,
                                    f"serial port {brief_name(serial_key)} already used by "
                                    f"channel {brief_name(bound_serial[serial_key])}"))
            else:
                bound_serial[serial_key] = ch.name
    if not _is_int(project.tick_ms) or not 10 <= project.tick_ms <= 60000:
        issues.append(Issue("error", "tick_ms", "project",
                            f"tick_ms {brief(project.tick_ms)} must be in 10..60000"))
    return issues


def _tcp(ch, issues: list[Issue], where: str) -> tuple[bool, bool]:
    """TCP 通道的 host/port 判定（顺序与消息原样保留：既有用例逐条比过）。"""
    port_ok = _is_int(ch.port) and 1 <= ch.port <= 65535
    host_ok = isinstance(ch.host, str) and bool(ch.host)
    if not host_ok:
        issues.append(Issue("error", "host", where,
                            f"host must be a non-empty string, got {brief(ch.host)}"))
    # 只判“非空字符串”，不判 IP 字面量语法（裁决 T）：主机名/IPv6/前导零都合法，
    # 这里造第二套判定容易误杀，真绑不上时 bind 的 OSError 已经足够明确。
    if not port_ok:
        # port=0（“让系统随便给一个”）在 M1 也是 error：要从 pymodbus 内部摸
        # transport 对象才能读回实际端口，版本一变就碎
        issues.append(Issue("error", "port", where,
                            f"port {brief(ch.port)} must be in 1..65535"))
    return host_ok, port_ok


S7_RACKS = (0, 1, 2, 3, 4, 5, 6, 7)
S7_SLOTS = tuple(range(1, 32))          # 300/400 常用 2、1200/1500 常用 1，范围给足


def _s7(ch, issues: list[Issue], where: str) -> tuple[bool, bool]:
    """S7 通道的 host/port/rack/slot 判定。返回 `(host_ok, port_ok)` 给"同 host:port 重复"
    那条共用（S7 与 TCP 抢同一个端口同样是冲突）。"""
    port_ok = _is_int(ch.port) and 1 <= ch.port <= 65535
    host_ok = isinstance(ch.host, str) and bool(ch.host)
    if not host_ok:
        issues.append(Issue("error", "host", where,
                            f"host must be a non-empty string, got {brief(ch.host)}"))
    elif SERIAL_MARK in ch.host or ":" in ch.host or ch.host.split() != [ch.host] \
            or "/" in ch.host:
        # 这几样都会把 CSV 那条 `name@host:port` 语法拆坏：`:` 是端口分隔符、`/` 会与
        # pyserial 的 URL（`socket://`）撞车、空格是段分隔符。host 本身不判 IP 语法（同 TCP）。
        host_ok = False
        issues.append(Issue("error", "host", where,
                            f"S7 host {brief(ch.host)} must not contain "
                            f"{SERIAL_MARK!r}, ':', '/' or whitespace (CSV writes it as "
                            f"name{SERIAL_MARK}host:port)"))
    if not port_ok:
        issues.append(Issue("error", "port", where,
                            f"port {brief(ch.port)} must be in 1..65535"))
    if ch.rack not in S7_RACKS:
        issues.append(Issue("error", "s7_rack", where,
                            f"rack {brief(ch.rack)} must be in "
                            f"{S7_RACKS[0]}..{S7_RACKS[-1]}"))
    if ch.slot not in S7_SLOTS:
        issues.append(Issue("error", "s7_slot", where,
                            f"slot {brief(ch.slot)} must be in "
                            f"{S7_SLOTS[0]}..{S7_SLOTS[-1]}"))
    return host_ok, port_ok


def _mqtt(ch, issues: list[Issue], where: str) -> tuple[bool, bool]:
    """MQTT 通道：broker host/port、QoS、topic 前缀，以及"两个点抢同一个 topic"。"""
    port_ok = _is_int(ch.port) and 1 <= ch.port <= 65535
    host_ok = isinstance(ch.host, str) and bool(ch.host.strip())
    if not host_ok:
        issues.append(Issue("error", "host", where,
                            f"host must be a non-empty string, got {brief(ch.host)}"))
    elif "/" in ch.host or any(char.isspace() for char in ch.host):
        # 这两个字符会把 CSV 的 `name@mqtt:host:port` 拆坏；host 本身不判 IP 语法。
        host_ok = False
        issues.append(Issue("error", "host", where,
                            f"MQTT host {brief(ch.host)} must not contain '/' or whitespace"))
    if not port_ok:
        issues.append(Issue("error", "port", where,
                            f"port {brief(ch.port)} must be in 1..65535"))
    if ch.qos not in (0, 1, 2):
        issues.append(Issue("error", "mqtt_qos", where,
                            f"QoS {brief(ch.qos)} must be 0, 1 or 2"))
    if not isinstance(ch.retain, bool):
        issues.append(Issue("error", "mqtt_retain", where,
                            f"retain {brief(ch.retain)} must be true or false"))
    if ch.payload not in ("json", "raw"):
        issues.append(Issue("error", "mqtt_payload", where,
                            f"payload {brief(ch.payload)} must be 'json' or 'raw'"))
    if (not isinstance(ch.topic_prefix, str) or not ch.topic_prefix.strip()
            or any(char in ch.topic_prefix for char in "+#")):
        issues.append(Issue("error", "mqtt_topic_prefix", where,
                            f"topic prefix {brief(ch.topic_prefix)} must be non-empty "
                            f"and must not contain '+' or '#'"))
    seen: dict[str, str] = {}
    for dev in ch.devices:
        for pt in dev.points:
            topic = value_topic(ch.topic_prefix, ch.name, dev.name, pt.area, pt.address)
            if topic in seen:
                issues.append(Issue("error", "mqtt_topic", where,
                                    f"topic {brief(topic)} is used by both "
                                    f"{brief(seen[topic])} and {brief(pt.name)}"))
            else:
                seen[topic] = pt.name
    return host_ok, port_ok


def _serial(ch, issues: list[Issue], where: str) -> str | None:
    """串口通道的参数判定。返回**可用**的串口名（给"两个通道抢同一个口"那条用），
    不可用就回 None——回空串会让两个写坏串口名的通道互相报重复，那是假错。

    为什么白名单而不判范围：见文件头 `SERIAL_BAUDRATES` 的说明。为什么串口名不许含
    `@`/空格：CSV 那条语法用它们当标记与分隔符，允许就等于"导出再导入解析不回来"。
    """
    if getattr(ch, "framing", "rtu") not in SERIAL_FRAMINGS:
        # 第四段（帧类型）写错：串口只有 RTU 与 ASCII 两种帧
        issues.append(Issue("error", "serial_framing", where,
                            f"serial framing {brief(getattr(ch, 'framing', None))} "
                            f"must be one of {SERIAL_FRAMINGS}"))
    ok = isinstance(ch.port, str) and bool(ch.port.strip())
    if not ok:
        issues.append(Issue("error", "serial_port", where,
                            f"serial port must be a non-empty string, got {brief(ch.port)}"))
    elif SERIAL_MARK in ch.port or ch.port.split() != [ch.port]:
        ok = False
        issues.append(Issue("error", "serial_port", where,
                            f"serial port {brief(ch.port)} must not contain "
                            f"{SERIAL_MARK!r} or whitespace (CSV writes it as "
                            f"name{SERIAL_MARK}COM3 9600 8N1)"))
    if ch.baudrate not in SERIAL_BAUDRATES:
        issues.append(Issue("error", "baudrate", where,
                            f"baudrate {brief(ch.baudrate)} must be one of "
                            f"{_joined(SERIAL_BAUDRATES)}"))
    if ch.parity not in SERIAL_PARITIES:
        issues.append(Issue("error", "parity", where,
                            f"parity {brief(ch.parity)} must be one of "
                            f"{_joined(SERIAL_PARITIES)}"))
    if ch.bytesize not in SERIAL_BYTESIZES:
        issues.append(Issue("error", "bytesize", where,
                            f"bytesize {brief(ch.bytesize)} must be one of "
                            f"{_joined(SERIAL_BYTESIZES)}"))
    if ch.stopbits not in SERIAL_STOPBITS:
        issues.append(Issue("error", "stopbits", where,
                            f"stopbits {brief(ch.stopbits)} must be one of "
                            f"{_joined(SERIAL_STOPBITS)}"))
    # 0 秒超时不是"不限时"而是"立刻放弃"：pymodbus 拿它当读超时，主站一帧都收不全
    if not _is_number(ch.timeout) or not ch.timeout > 0:
        issues.append(Issue("error", "timeout", where,
                            f"timeout {brief(ch.timeout)} must be a positive number of "
                            f"seconds"))
    return ch.port if ok else None


def _joined(values) -> str:
    """白名单消息里的取值清单：与 `brief()` 同一套限长思路，但这里是定长的小表。"""
    return "/".join(str(value) for value in values)


def _devices(ch, issues: list[Issue]) -> None:
    units: dict[int, str] = {}
    for dev in ch.devices:
        where = f"{brief_name(ch.name)}/{brief_name(dev.name)}"
        if not _is_int(dev.unit_id) or not 1 <= dev.unit_id <= 247:
            issues.append(Issue("error", "unit_range", where,
                                f"unit_id {brief(dev.unit_id)} outside 1..247"))
        if _is_int(dev.unit_id):
            # 非整数不进 units：[] 连哈希都没有（`in` 直接 TypeError），
            # True 与 1 哈希相同会把合法设备误判成重复。脏值上一条 unit_range 说完
            if dev.unit_id in units:
                issues.append(Issue("error", "unit_duplicated", where,
                                    f"unit_id {brief(dev.unit_id)} already used by "
                                    f"{brief_name(units[dev.unit_id])}"))
            # 跨通道重复 unit_id 不在这里判：那是 Task 8 build_sim_devices 的 ValueError
            units[dev.unit_id] = dev.name
        _points(ch, dev, issues)


def _points(channel, dev, issues: list[Issue]) -> None:
    """点位判定按传输分派：S7 的区/类型/地址（字节+位）与 Modbus 那套完全不同。

    `unit_id` 的 1..247 那条在 `_devices` 里对两种传输都照旧（S7 不上线它、只做设备分组，
    但同一个模型里保持一套规则，免得"哪条通道要填 unit"变成看传输猜的事）。
    """
    if channel_transport(channel) == "s7":
        _points_s7(channel, dev, issues)
        return
    channel_name = channel.name
    used: dict[tuple[str, int], str] = {}
    for pt in dev.points:
        loc = (f"{brief_name(channel_name)}/{brief_name(dev.name)}/"
               f"{brief_name(pt.name)}")
        if pt.row:
            loc = f"{loc} (line {pt.row})"
        _name(pt, dev, loc, issues)
        if pt.datatype not in DATATYPES:
            issues.append(Issue("error", "datatype", loc,
                                f"unknown datatype {brief(pt.datatype)}"))
        if pt.area not in AREAS:
            issues.append(Issue("error", "area", loc, f"unknown area {brief(pt.area)}"))
        if pt.datatype in DATATYPES and pt.area in AREAS:
            _shape(channel_name, dev, pt, loc, used, issues)
        _wave(pt, loc, issues)


def _points_s7(channel, dev, issues: list[Issue]) -> None:
    """S7 点位的判定：区名 / 类型 / 地址（字节偏移与位）/ **按位**的重叠。

    与 Modbus 那套分开写而不是硬套：三处语义都不一样——S7 的地址单位是**字节**（bool 还要
    带一位）、类型是 S7 自己的名字、重叠要按**位**判（`DBX0.0` 与 `DBX0.3` 同占一个字节但
    互不冲突，套 Modbus 那套会把它们报成重叠，现场就得把点位挪到不同字节去，那是假约束）。
    """
    used: dict[tuple[str, int | None], list[tuple[int, int, str]]] = {}
    for pt in dev.points:
        loc = (f"{brief_name(channel.name)}/{brief_name(dev.name)}/"
               f"{brief_name(pt.name)}")
        if pt.row:
            loc = f"{loc} (line {pt.row})"
        _name(pt, dev, loc, issues)
        _s7_point_shape(pt, loc, issues)
        _s7_overlap(pt, loc, used, issues)
        _wave(pt, loc, issues)          # 波形参数与传输无关，复用 Modbus 那一份


def _s7_point_shape(pt, loc, issues) -> None:
    area = parse_s7_area(pt.area)
    if area is None:
        issues.append(Issue("error", "s7_area", loc,
                            f"unknown S7 area {brief(pt.area)} "
                            f"(use mk/pe/pa or db<n>)"))
    elif area[0] == "db" and not 1 <= (area[1] or 0) <= S7_MAX_DB:
        issues.append(Issue("error", "s7_area", loc,
                            f"DB number {brief(area[1])} outside 1..{S7_MAX_DB}"))
    if pt.datatype not in S7_TYPES:
        issues.append(Issue("error", "s7_datatype", loc,
                            f"{brief(pt.datatype)} is not an S7 type "
                            f"({'/'.join(S7_TYPES)})"))
    if not _is_int(pt.address) or not 0 <= pt.address <= 65535:
        issues.append(Issue("error", "address", loc,
                            f"address {brief(pt.address)} outside 0..65535"))
    # 位只有 bool 有，而且必须写出来：`DBX0.3` 的那一位是地址的一部分，缺了它就没法寻址
    # （不像 Modbus 的位区那样"地址即位号"）。
    if pt.datatype == BOOL:
        if pt.bit is None:
            issues.append(Issue("error", "s7_bit", loc,
                                "bool points need a bit: write the address as "
                                "byte.bit (like 0.3)"))
        elif not _is_int(pt.bit) or not 0 <= pt.bit <= 7:
            issues.append(Issue("error", "s7_bit", loc,
                                f"bit {brief(pt.bit)} outside 0..7"))
    elif pt.bit is not None:
        issues.append(Issue("error", "s7_bit", loc,
                            f"bit is only for bool points, got {brief(pt.bit)}"))
    if pt.word_order != WORD_ABCD:
        # S7 一律大端、没有字序这回事：给警告不拦启动（与"单寄存器类型填了字序"同一个姿态）
        issues.append(Issue("warning", "s7_word_order", loc,
                            f"S7 has no word order: {brief(pt.word_order)} is ignored"))


def _s7_overlap(pt, loc, used, issues) -> None:
    """按位判重叠：bool 占 1 位，其余按类型占 `8 * 字节数` 位（string 按初值长度）。

    区/地址/类型本身有错时直接跳过——占位算不出来，硬算会喷出一串假重叠把真错挤出屏幕。
    """
    area = parse_s7_area(pt.area)
    if area is None or not _is_int(pt.address):
        return
    if pt.datatype == BOOL:
        if not _is_int(pt.bit):
            return
        start, length = pt.address * 8 + pt.bit, 1
    elif pt.datatype in S7_TYPES:
        start, length = pt.address * 8, 8 * s7_span_bytes(pt.datatype, pt.initial)
    else:
        return
    end = start + length
    short = loc.split("/")[-1].split(" (")[0]
    for other_start, other_end, other_name in used.get(area, ()):
        if start < other_end and other_start < end:
            issues.append(Issue("error", "s7_overlap", loc,
                                f"overlaps {other_name} in {s7_area_name(*area)}"))
            break
    used.setdefault(area, []).append((start, end, short))


def _name(pt, dev, loc, issues) -> None:
    """点位名是**身份列**，空着不许一路走到运行态：属性面板把 `name_edit.text().strip()`
    原样写回本体，零新增错误就应用成功，`start_channel` 照起这个无名的点，而这份工程导出
    的 CSV 再也导不回来（`csv_io` 认 name 是身份列）。

    家在这里而不是面板：`gui/point_props` 的新增错误差集与 `Session.start_channel` 问的都
    是这一份清单，加一条两处同时生效；只写进面板的话，命令行起的那一路照样服务无名点。
    `csv_io._require_text` 不接管这条也不许删——它判的是**CSV 单元格里的那一格**并说得出
    行号（行号只有 loader 知道），这里判的是**模型里的那个点**，两个来源两种说法，不是
    同一个判定抄两遍。

    只判"空"（strip 之后），不判"难看"：`0`、带空格、400 字符的名字都是别人点表里的合法
    身份，多拦一条就是把别人的点表拦在启动之外（同裁决 T 对 host 的口径）。非字符串不在
    这里说一句"must not be empty"：那要说的是类型不对，说反了比不说更难查。
    """
    if isinstance(pt.name, str) and not pt.name.strip():
        issues.append(Issue(
            "error", "name", loc,
            f"point name must not be empty (device {brief_name(dev.name)}, "
            f"area {brief(pt.area)}, address {brief(pt.address)})"))


def _shape(channel_name, dev, pt, loc, used, issues) -> None:
    if area_is_bit(pt.area) != (pt.datatype == BOOL):
        # 类型与区不匹配也继续查：本点位占的寄存器位别人也要占，在这里 return 会把
        # 别人的重叠一起吞掉，用户就得跑两轮启动才发现第二个错（“一次报全”不打折）
        issues.append(Issue("error", "area_type", loc,
                            f"{pt.datatype} is not allowed on area {pt.area}"))
    if not _is_int(pt.address) or not 0 <= pt.address <= 65535:
        issues.append(Issue("error", "address", loc,
                            f"address {brief(pt.address)} outside 0..65535"))
    if pt.word_order not in WORD_ORDERS:
        issues.append(Issue("error", "word_order", loc,
                            f"unknown word_order {brief(pt.word_order)}"))
    else:
        allowed = word_orders_allowed(pt.datatype)
        if pt.word_order not in allowed:
            # 单寄存器类型填了字序 -> 只是没意义，warning；
            # 64 位/string 填了不支持的字序 -> error（语义有分歧，不能猜）
            is_single = pt.datatype in _SINGLE
            issues.append(Issue(
                "warning" if is_single else "error",
                "word_order_ignored" if is_single else "word_order",
                loc, f"{pt.datatype} only supports {allowed}, got {brief(pt.word_order)}"))
    try:
        pt.typed_initial()
    except ValueError as exc:
        issues.append(Issue("error", "initial", loc, str(exc)))
    _overlap(pt, loc, used, issues)


def _overlap(pt, loc, used, issues) -> None:
    """位区按位判，寄存器区按寄存器判。跨度算不出来就跳过——initial 已经报过错。"""
    short = loc.split("/")[-1].split(" (")[0]
    if not _is_int(pt.address):
        return                                  # 地址不是整数，占位无从算起
    if area_is_bit(pt.area):
        keys = [(pt.area, pt.address)]
    else:
        try:
            span = register_span(pt.datatype, pt.initial)
        except ValueError:
            return                              # 裁决 I：算不出跨度就跳过，不 traceback
        keys = [(pt.area, pt.address + i) for i in range(span)]
    for key in keys:
        if key in used:
            issues.append(Issue("error", "overlap", loc,
                                f"{key[0]} address {key[1]} already used by {used[key]}"))
            return                              # 只报第一次撞上的那个寄存器，不刷屏
        used[key] = short


def _wave(pt, loc, issues) -> None:
    if pt.waveform not in WAVE_SPEC:
        issues.append(Issue("error", "waveform", loc,
                            f"unknown waveform {brief(pt.waveform)}"))
        return                                  # 未知波形的参数无从判起，不许拿猜测充数
    if not isinstance(pt.wave_param, dict):
        # CSV 原文（"base=20;amp=15"）没解析就进来时，逐字符判会喷出几十条假错误，
        # 把真正的错挤出屏幕：形状不对就一条说完
        issues.append(Issue("error", "wave_param", loc,
                            f"wave_param must be a k=v mapping, got "
                            f"{brief(pt.wave_param)}"))
        return
    required, optional = WAVE_SPEC[pt.waveform]
    got = set(pt.wave_param)
    if pt.waveform == "const" and not got:
        return                      # const 不填就当 0，M4 才用 value
    for miss in sorted(required - got):
        issues.append(Issue("error", "wave_param", loc,
                            f"{pt.waveform} missing parameter {miss!r}"))
    for extra in sorted(got - required - optional):
        issues.append(Issue("error", "wave_param", loc,
                            f"{pt.waveform} unknown parameter {brief(extra)}"))
    if got & required == required or (pt.waveform == "const" and got):
        _wave_ranges(pt, loc, issues)


def _wave_ranges(pt, loc, issues) -> None:
    numbers: dict[str, float] = {}
    for key, text in pt.wave_param.items():
        if key == "values" or key == "seed":
            continue
        try:
            numbers[key] = float(text)
        except (TypeError, ValueError):
            # float(None) 抛的是 TypeError：一次报全的路上不许有 traceback（同裁决 D/K）
            issues.append(Issue("error", "wave_param", loc,
                                f"parameter {brief(key)} is not a number: {brief(text)}"))
    for key in sorted(_POSITIVE & set(numbers)):
        if numbers[key] <= 0:
            issues.append(Issue("error", "wave_param", loc,
                                f"{pt.waveform} {key} must be > 0"))
    if {"min", "max"} <= set(numbers) and numbers["min"] > numbers["max"]:
        issues.append(Issue("error", "wave_param", loc, "random min > max"))
    if {"from", "to"} <= set(numbers) and numbers["from"] == numbers["to"]:
        issues.append(Issue("error", "wave_param", loc, "ramp from == to"))
    if pt.waveform == "step":
        raw = pt.wave_param.get("values", "")
        if not isinstance(raw, str):
            issues.append(Issue("error", "wave_param", loc,
                                f"step values must be a comma separated string: "
                                f"{brief(raw)}"))
        elif not [v for v in raw.split(",") if v.strip()]:
            issues.append(Issue("error", "wave_param", loc, "step values list is empty"))


def _is_number(value) -> bool:
    """小数字段（串口 timeout）的比较前先确认是数字：字符串 `"0.3"` 与 `>` 比较会 TypeError，
    一次报全的路上不许有 traceback（同 `_is_int`，裁决 O）。
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value) -> bool:
    """范围比较与字典成员判定前先确认是整数：bool 是 int 的子类，字符串端口（手改
    project.json 的 rtu 残留）比较会直接 TypeError，`[]` 连哈希都没有。校验自己在
    “一次报全”的路上崩掉，比漏一条错误更糟（裁决 O：脏值一条说完，不许 traceback 逃逸）。
    """
    return isinstance(value, int) and not isinstance(value, bool)
