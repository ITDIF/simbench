"""project.json 读写。它是运行态唯一真相，与 CSV 双向可导。"""

from __future__ import annotations

import json
from pathlib import Path

from .model import BOOL, Device, Point, Project, TcpChannel, brief, parse_value

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


def _point_to_dict(point: Point) -> dict:
    out = {name: getattr(point, name) for name in _POINT_FIELDS}
    out["wave_param"] = dict(point.wave_param)
    out["readonly"] = bool(point.readonly)
    return out


def _point_from_dict(raw: dict) -> Point:
    _require_object(raw, "point")
    loc = f"point {brief(raw.get('name'))}"
    # initial 只 str()，不截断也不判类型（裁决 B）：`100.5` 进来变成字符串 "100.5"，
    # 由 validate 的 [initial] 一条说完；这里判就变成 loader 造第二套类型口径。
    return Point(
        name=raw["name"], area=raw["area"],
        address=_require_int(raw["address"], "address", loc),
        datatype=raw["datatype"], initial=str(raw.get("initial", "0")),
        word_order=raw.get("word_order", "ABCD"),
        waveform=raw.get("waveform", "const"),
        wave_param=dict(_require_object(raw.get("wave_param", {}), "wave_param")),
        readonly=_require_bool(raw.get("readonly", False), "readonly"))


def _device_to_dict(device: Device) -> dict:
    return {"name": device.name, "unit_id": device.unit_id,
            "points": [_point_to_dict(p) for p in device.points]}


def _device_from_dict(raw: dict) -> Device:
    _require_object(raw, "device")
    return Device(name=raw["name"],
                  unit_id=_require_int(raw["unit_id"], "unit_id",
                                       f"device {brief(raw.get('name'))}"),
                  points=[_point_from_dict(p)
                          for p in _require_array(raw.get("points", []), "points")])


def _channel_to_dict(channel: TcpChannel) -> dict:
    return {"name": channel.name, "type": channel.type, "host": channel.host,
            "port": channel.port,
            "devices": [_device_to_dict(d) for d in channel.devices]}


def _channel_from_dict(raw: dict) -> TcpChannel:
    _require_object(raw, "channel")
    return TcpChannel(
        name=raw["name"], type=raw.get("type", "tcp"),
        host=raw.get("host", "0.0.0.0"),
        port=_require_int(raw.get("port", 502), "port",
                          f"channel {brief(raw.get('name'))}"),
        devices=[_device_from_dict(d)
                 for d in _require_array(raw.get("devices", []), "devices")])


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
    return Project(channels=[_channel_from_dict(ch) for ch in channels],
                   tick_ms=_require_int(data.get("tick_ms", 100), "tick_ms", "project"))


def save(project: Project, path: Path) -> None:
    Path(path).write_text(to_json(project) + "\n", encoding="utf-8")


def load(path: Path) -> Project:
    # 文件不存在/是目录/没权限都让 OSError 原样抛出去：那是操作错误不是数据错误，
    # 只有"读出来了但读不懂"才是 ValueError（Task 12 的装载口按这两类分开处理）。
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
