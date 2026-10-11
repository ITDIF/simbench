"""启动前校验：一次报全，不撞第一个就崩。本文件不得 import pymodbus。"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    AREAS, BOOL, DATATYPES, STRING, WAVEFORMS, WORD_ABCD, WORD_ORDERS,
    area_is_bit, brief, brief_name, register_span, word_orders_allowed,
)

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
    for ch in project.channels:
        # `where` 是清单每行开头的定位串，身份名原样拼进来时一个 400 字符的通道名能把
        # 整屏刷掉（真出现：厂商把整段说明塞进通道名）。判定不动，只截渲染（同裁决 N）。
        name = brief_name(ch.name)
        if ch.name in seen:
            issues.append(Issue("error", "channel_duplicated", name,
                                f"channel name {brief(ch.name)} used twice"))
        seen.add(ch.name)
        if ch.type != "tcp":
            issues.append(Issue("error", "channel_type", name,
                                f"channel type {brief(ch.type)} is not supported in M1"))
        port_ok = _is_int(ch.port) and 1 <= ch.port <= 65535
        host_ok = isinstance(ch.host, str) and bool(ch.host)
        if not host_ok:
            issues.append(Issue("error", "host", name,
                                f"host must be a non-empty string, got {brief(ch.host)}"))
        # 只判“非空字符串”，不判 IP 字面量语法（裁决 T）：主机名/IPv6/前导零都合法，
        # 这里造第二套判定容易误杀，真绑不上时 bind 的 OSError 已经足够明确。
        if not port_ok:
            # port=0（“让系统随便给一个”）在 M1 也是 error：要从 pymodbus 内部摸
            # transport 对象才能读回实际端口，版本一变就碎
            issues.append(Issue("error", "port", name,
                                f"port {brief(ch.port)} must be in 1..65535"))
        _devices(ch, issues)
        # 同 host+port 的两个通道要等启动第二阶段才炸 bind，Windows 只回一句 10048、
        # 不带通道名，现场看不出是点表配置错：静态查得出的冲突就静态报完（裁决 R）。
        # 脏 port 与脏 host 上面两条已各自说完，键也构造不出来，一起跳过（裁决 O/T）。
        # 不按 ch.type == "tcp" 抑制：两个 rtu 通道配成同一监听参数本身就是错的，
        # 多报一条不算噪音，M5 落地串口后这条判定照样成立。
        if port_ok and host_ok:
            key = (ch.host, ch.port)
            if key in bound:
                issues.append(Issue("error", "port_duplicated", name,
                                    f"port {ch.port} on host {brief_name(ch.host)} "
                                    f"already used by channel {brief_name(bound[key])}"))
            else:
                bound[key] = ch.name
    if not _is_int(project.tick_ms) or not 10 <= project.tick_ms <= 60000:
        issues.append(Issue("error", "tick_ms", "project",
                            f"tick_ms {brief(project.tick_ms)} must be in 10..60000"))
    return issues


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
        _points(ch.name, dev, issues)


def _points(channel_name: str, dev, issues: list[Issue]) -> None:
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


def _is_int(value) -> bool:
    """范围比较与字典成员判定前先确认是整数：bool 是 int 的子类，字符串端口（手改
    project.json 的 rtu 残留）比较会直接 TypeError，`[]` 连哈希都没有。校验自己在
    “一次报全”的路上崩掉，比漏一条错误更糟（裁决 O：脏值一条说完，不许 traceback 逃逸）。
    """
    return isinstance(value, int) and not isinstance(value, bool)
