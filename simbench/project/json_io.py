"""project.json 读写。它是运行态唯一真相，与 CSV 双向可导。"""

from __future__ import annotations

import json
from dataclasses import MISSING, fields
from pathlib import Path

from . import fsio
from .model import (
    BOOL,
    S7_DEFAULTS,
    SERIAL_DEFAULTS,
    Device,
    MqttChannel,
    Point,
    Project,
    S7Channel,
    SerialChannel,
    TcpChannel,
    brief,
    channel_transport,
    is_serial,
    parse_value,
)

VERSION = 1
_POINT_FIELDS = ("name", "area", "address", "datatype", "initial", "word_order",
                 "waveform", "wave_param", "readonly")


def _require_bool(value, what: str) -> bool:
    """readonly 只认 Python bool 和 bool 白名单里的字符串，其余一律 ValueError。

    `bool(raw.get("readonly"))` 有两个静默错值的方向：`"false"` 是非空字符串恒真（把
    可写点亮成只读，主站写不进去），`None`/`0`/`[]` 静默 False（把我们故意设的只读保护
    关掉，主站就能改写真设备只读的点位）。判定口径复用 `model.parse_value(BOOL, ...)`，
    白名单只此一份（裁决 E/M）；换消息只为了带上字段名，loader 的 ValueError 会被
    Task 12 的装载口原样打印。
    """
    try:
        return parse_value(BOOL, value)
    except ValueError:
        raise ValueError(f"{what} must be a boolean, got {brief(value)}") from None


def _require_int(value, what: str, loc: str) -> int:
    """整数字段（address/unit_id/port/tick_ms）只认 JSON 整数，禁止 `int()` 静默截断。

    `int(1.9)`=1、`int(11.9)`=11、`int(True)`=1、`int(502.7)`=502：地址被截断就是读写
    错寄存器，unit_id 被截断就是答错从站号（主站扫到一台不存在的设备），port 被截断
    就是监听端口静默改了。三者都会表现成"平台读到的值/连上的设备不对"，现场只会当成
    平台 bug 去调。与裁决 B 对 initial 的口径一致：宁可当场报错，绝不替用户猜一个
    看着合法的整数；`int(400 个 9)` 这种脏值原值走 brief()（裁决 N）。

    `isinstance(value, bool)` 也要挡：bool 是 int 的子类，放行就等于把 `true` 读成 1。
    JSON 字符串 `"502"` 同样拒——那是类型错，不是"差一步就对"，交给 validate 也只会
    多绕一层（裁决 O 的范围比较口径就是非整数不参与判定）。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{loc}: {what} must be an integer, got {brief(value)}")
    return value


def _require_number(value, what: str, loc: str) -> float:
    """小数字段（目前只有串口的 timeout）只认 JSON 数字，理由与 `_require_int` 同一条：
    `"0.3"` 这种字符串进来 `float()` 一下看着也能用，但那是在替用户猜类型。bool 同样挡
    （`true` 会变成 1.0 秒）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{loc}: {what} must be a number, got {brief(value)}")
    return float(value)


def _require_object(value, what: str) -> dict:
    """形状 gate：project.json 的每个层级都必须是 JSON object。

    顶层写成 list → `AttributeError: 'list' object has no attribute 'get'`；channel 写成
    字符串 → `TypeError: string indices must be integers`；`wave_param` 写成 list →
    `dict()` 抛 `TypeError`。而本模块的契约（load/read_csv 的 docstring）是"读出来了但
    读不懂 → ValueError"，Task 12 的装载口只 `except ValueError`——逃逸出去就是当场
    traceback。消息按层级点名（project/channel/device/point/wave_param），一条话说清
    是哪一层，不吞异常也不继续解析。
    """
    if not isinstance(value, dict):
        raise ValueError(f"project.json {what} must be an object, "
                         f"got {type(value).__name__}")
    return value


def _require_array(value, what: str) -> list:
    """形状 gate：channels/devices/points 三个容器必须是 JSON array（见 `_require_object`）。"""
    if not isinstance(value, list):
        raise ValueError(f"project.json {what} must be a list, "
                         f"got {type(value).__name__}")
    return value


def _required_keys(model_cls) -> tuple[str, ...]:
    """dataclass 里没有默认值的字段就是 project.json 的必填键，清单由字段定义派生。

    不在这里抄第二份键名列表（同裁决 M 对 codec `_NUMERIC` 的要求）：`Point` 加一个必填
    字段时，手抄的清单必然漏，缺键又退回 `KeyError` 逃逸。`row`（CSV 行号，不进 json）
    有默认值，自然不会被算成必填键。
    """
    return tuple(f.name for f in fields(model_cls)
                 if f.default is MISSING and f.default_factory is MISSING)


def _require_keys(raw: dict, model_cls, loc: str) -> None:
    """缺必填键 → 领域 ValueError，不是 `KeyError`。

    `raw["area"]` 直接下标在缺键时抛 `KeyError: 'area'`，而本模块的契约是"文件读不到 →
    OSError / 读出来了但读不懂 → ValueError"（`load` 的 docstring、Task 12 的装载口只
    `except ValueError`）：手改 project.json 删掉一行是纯粹的"读不懂"，逃逸成 KeyError
    就是当场 traceback。而且 `KeyError: 'area'` 既不说是哪个点位也不说是哪个设备，现场
    对着几千行的工程文件找不到该补哪一条，所以消息按层级点名，序号从 1 数（与 CSV 行号
    同向）。形状 gate（`_require_object`）必须排在它前面：`key not in "p"` 在字符串上是
    子串判定，会把"point 不是 object"说成"缺键"。
    """
    for key in _required_keys(model_cls):
        if key not in raw:
            raise ValueError(f"project.json {loc} is missing required key '{key}'")


def _point_to_dict(point: Point) -> dict:
    out = {name: getattr(point, name) for name in _POINT_FIELDS}
    out["wave_param"] = dict(point.wave_param)
    out["readonly"] = bool(point.readonly)
    if point.bit is not None:
        # 只有 S7 的位点位多这一个键（Modbus 点恒为 None）：**不写 null**，否则既有
        # project.json 的逐字节往返（那条用例拿 spec §5 的样例做基准）会全体变样。
        out["bit"] = point.bit
    return out


def _point_from_dict(raw: dict, index: int, device_name: str) -> Point:
    _require_object(raw, "point")
    _require_keys(raw, Point, f"point #{index} of device {brief(device_name)}")
    loc = f"point {brief(raw['name'])}"
    # initial 只 str()，不截断也不判类型（裁决 B）：`100.5` 进来变成字符串 "100.5"，
    # 由 validate 的 [initial] 一条说完；这里判就变成 loader 造第二套类型口径。
    return Point(
        name=raw["name"], area=raw["area"],
        address=_require_int(raw["address"], "address", loc),
        datatype=raw["datatype"], initial=str(raw.get("initial", "0")),
        word_order=raw.get("word_order", "ABCD"),
        waveform=raw.get("waveform", "const"),
        wave_param=dict(_require_object(raw.get("wave_param", {}), "wave_param")),
        readonly=_require_bool(raw.get("readonly", False), "readonly"),
        bit=(_require_int(raw["bit"], "bit", loc) if "bit" in raw else None))


def _device_to_dict(device: Device) -> dict:
    return {"name": device.name, "unit_id": device.unit_id,
            "points": [_point_to_dict(p) for p in device.points]}


def _device_from_dict(raw: dict, index: int, channel_name: str) -> Device:
    _require_object(raw, "device")
    _require_keys(raw, Device, f"device #{index} of channel {brief(channel_name)}")
    name = raw["name"]
    return Device(name=name,
                  unit_id=_require_int(raw["unit_id"], "unit_id",
                                       f"device {brief(name)}"),
                  points=[_point_from_dict(p, i, name)
                          for i, p in enumerate(
                              _require_array(raw.get("points", []), "points"), 1)])


def _channel_to_dict(channel) -> dict:
    """按 `channel_transport()` 分派：串口写 spec §5 那几个键，TCP 写 host/port。

    串口那一支的键序就是 spec §5 的示例顺序（name/type/port/baudrate/parity/bytesize/
    stopbits/timeout/devices）——同一个工程反复存盘要产出同样的字节，键序跟着 JSON 的
    插入序走，所以这里手写而不是 dict 推导。
    """
    devices = [_device_to_dict(d) for d in channel.devices]
    transport = channel_transport(channel)
    if transport == "s7":
        return {"name": channel.name, "type": "s7", "host": channel.host,
                "port": channel.port, "rack": channel.rack, "slot": channel.slot,
                "devices": devices}
    if transport == "mqtt":
        return {"name": channel.name, "type": "mqtt", "host": channel.host,
                "port": channel.port, "topic_prefix": channel.topic_prefix,
                "qos": channel.qos, "retain": channel.retain,
                "payload": channel.payload, "will": channel.will,
                "devices": devices}
    if is_serial(transport):
        # `type` 就是传输名（`rtu` / `ascii`），**不另写 `framing` 键**：一个事实一个位置。
        # 好处是老工程与 spec §5 的示例逐字节不变（它们没有这个键）。
        return {"name": channel.name, "type": transport, "port": channel.port,
                "baudrate": channel.baudrate, "parity": channel.parity,
                "bytesize": channel.bytesize, "stopbits": channel.stopbits,
                "timeout": channel.timeout, "devices": devices}
    # socket 那三种（tcp / udp / rtutcp）：`type` 写的就是传输名
    return {"name": channel.name, "type": transport, "host": channel.host,
            "port": channel.port, "devices": devices}


def _channel_from_dict(raw: dict, index: int):
    _require_object(raw, "channel")
    # 没写 `type` 就是 TCP（老工程文件里 TCP 那一支本来就不写它）；写 `rtu`/`ascii` 才是串口
    if is_serial(str(raw.get("type", "tcp"))):
        return _serial_channel_from_dict(raw, index)
    if raw.get("type") == "s7":
        return _s7_channel_from_dict(raw, index)
    if raw.get("type") == "mqtt":
        # MQTT 支路原先直接 `raw.get("name")`，缺键时把 None 带进 MqttChannel，
        # 到下游才炸成看不懂的 TypeError；按本模块口径先过必填键闸。
        _require_keys(raw, MqttChannel, f"channel #{index}")
        name = raw["name"]
        return MqttChannel(
            name=name,
            host=raw.get("host", "127.0.0.1"),
            port=_require_int(raw.get("port", 1883), "port", f"channel {brief(name)}"),
            topic_prefix=str(raw.get("topic_prefix", "simbench")),
            qos=_require_int(raw.get("qos", 1), "qos", f"channel {brief(name)}"),
            retain=bool(raw.get("retain", True)),
            payload=str(raw.get("payload", "json")),
            will=bool(raw.get("will", False)),
            devices=_devices_from_dict(raw, name))
    _require_keys(raw, TcpChannel, f"channel #{index}")
    name = raw["name"]
    return TcpChannel(
        name=name, type=raw.get("type", "tcp"),
        host=raw.get("host", "0.0.0.0"),
        port=_require_int(raw.get("port", 502), "port",
                          f"channel {brief(name)}"),
        devices=_devices_from_dict(raw, name))


def _devices_from_dict(raw: dict, channel_name: str) -> list[Device]:
    return [_device_from_dict(d, i, channel_name)
            for i, d in enumerate(
                _require_array(raw.get("devices", []), "devices"), 1)]


def _s7_channel_from_dict(raw: dict, index: int) -> S7Channel:
    """S7 通道：`host` 原样收下（是不是合法主机名由 validate 判，与 TCP 的 host 同口径），
    `port`/`rack`/`slot` 必须是 JSON 整数——`int(102.7)` 那种静默截断会让现场连去另一个端口。
    """
    _require_keys(raw, S7Channel, f"channel #{index}")
    name = raw["name"]
    loc = f"channel {brief(name)}"
    return S7Channel(
        name=name,
        host=raw.get("host", S7_DEFAULTS["host"]),
        port=_require_int(raw.get("port", S7_DEFAULTS["port"]), "port", loc),
        rack=_require_int(raw.get("rack", S7_DEFAULTS["rack"]), "rack", loc),
        slot=_require_int(raw.get("slot", S7_DEFAULTS["slot"]), "slot", loc),
        devices=_devices_from_dict(raw, name))


def _serial_channel_from_dict(raw: dict, index: int) -> SerialChannel:
    """串口通道：`port`/`parity` 原样收下（是不是合法串口名、白名单里有没有，是 validate 的活，
    与 TCP 的 host 同一条口径——loader 只在"类型上根本不可能"时拦，例如把小数当整数）。
    `baudrate`/`bytesize`/`stopbits` 必须是 JSON 整数，`timeout` 必须是数字：这三种一旦被
    `int()`/`float()` 静默截断，现场看到的是"波特率变成 9600 了/停止位变成 1 了"，
    而工程文件里写的明明是别的。
    """
    _require_keys(raw, SerialChannel, f"channel #{index}")
    name = raw["name"]
    loc = f"channel {brief(name)}"
    return SerialChannel(
        name=name,
        port=raw.get("port", SERIAL_DEFAULTS["port"]),
        # `type` 就是传输名（`rtu` / `ascii`）。`framing` 那个键只在**手写的**工程文件里
        # 出现（老版本没有它），照读，缺省 rtu。
        framing=("ascii" if str(raw.get("type")) == "ascii"
                 else str(raw.get("framing", "rtu"))),
        baudrate=_require_int(raw.get("baudrate", SERIAL_DEFAULTS["baudrate"]),
                              "baudrate", loc),
        parity=raw.get("parity", SERIAL_DEFAULTS["parity"]),
        bytesize=_require_int(raw.get("bytesize", SERIAL_DEFAULTS["bytesize"]),
                              "bytesize", loc),
        stopbits=_require_int(raw.get("stopbits", SERIAL_DEFAULTS["stopbits"]),
                              "stopbits", loc),
        timeout=_require_number(raw.get("timeout", SERIAL_DEFAULTS["timeout"]),
                                "timeout", loc),
        devices=_devices_from_dict(raw, name))


def to_dict(project: Project) -> dict:
    return {"version": VERSION, "tick_ms": project.tick_ms,
            "channels": [_channel_to_dict(ch) for ch in project.channels]}


def to_json(project: Project) -> str:
    return json.dumps(to_dict(project), ensure_ascii=False, indent=2)


def from_dict(data: dict) -> Project:
    _require_object(data, "project")
    version = data.get("version")
    if version != VERSION:
        raise ValueError(f"unsupported project.json version {brief(version)}, "
                         f"expected {VERSION}")
    channels = _require_array(data.get("channels", []), "channels")
    return Project(channels=[_channel_from_dict(ch, i)
                             for i, ch in enumerate(channels, 1)],
                   tick_ms=_require_int(data.get("tick_ms", 100), "tick_ms", "project"))


def save(project: Project, path: str | Path) -> None:
    """工程落盘：走 `fsio` 的原子替换（写一半断电不许把工程废掉）。"""
    fsio.write_text_atomic(path, to_json(project) + "\n")


def load(path: Path) -> Project:
    # 文件不存在/是目录/没权限都让 OSError 原样抛出去：那是操作错误不是数据错误，
    # 只有"读出来了但读不懂"才是 ValueError（Task 12 的装载口按这两类分开处理）。
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
