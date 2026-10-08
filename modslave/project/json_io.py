"""project.json 读写。它是运行态唯一真相，与 CSV 双向可导。"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Device, Point, Project, TcpChannel

VERSION = 1
_POINT_FIELDS = ("name", "area", "address", "datatype", "initial", "word_order",
                 "waveform", "wave_param", "readonly")


def _point_to_dict(point: Point) -> dict:
    out = {name: getattr(point, name) for name in _POINT_FIELDS}
    out["wave_param"] = dict(point.wave_param)
    out["readonly"] = bool(point.readonly)
    return out


def _point_from_dict(raw: dict) -> Point:
    # initial 只 str()，不截断也不判类型（裁决 B）：`100.5` 进来变成字符串 "100.5"，
    # 由 validate 的 [initial] 一条说完；这里判就变成 loader 造第二套类型口径。
    return Point(
        name=raw["name"], area=raw["area"], address=int(raw["address"]),
        datatype=raw["datatype"], initial=str(raw.get("initial", "0")),
        word_order=raw.get("word_order", "ABCD"),
        waveform=raw.get("waveform", "const"),
        wave_param=dict(raw.get("wave_param", {})),
        readonly=bool(raw.get("readonly", False)))


def _device_to_dict(device: Device) -> dict:
    return {"name": device.name, "unit_id": device.unit_id,
            "points": [_point_to_dict(p) for p in device.points]}


def _device_from_dict(raw: dict) -> Device:
    return Device(name=raw["name"], unit_id=int(raw["unit_id"]),
                  points=[_point_from_dict(p) for p in raw.get("points", [])])


def _channel_to_dict(channel: TcpChannel) -> dict:
    return {"name": channel.name, "type": channel.type, "host": channel.host,
            "port": channel.port,
            "devices": [_device_to_dict(d) for d in channel.devices]}


def _channel_from_dict(raw: dict) -> TcpChannel:
    return TcpChannel(
        name=raw["name"], type=raw.get("type", "tcp"),
        host=raw.get("host", "0.0.0.0"), port=int(raw.get("port", 502)),
        devices=[_device_from_dict(d) for d in raw.get("devices", [])])


def to_dict(project: Project) -> dict:
    return {"version": VERSION, "tick_ms": project.tick_ms,
            "channels": [_channel_to_dict(ch) for ch in project.channels]}


def to_json(project: Project) -> str:
    return json.dumps(to_dict(project), ensure_ascii=False, indent=2)


def from_dict(data: dict) -> Project:
    version = data.get("version")
    if version != VERSION:
        raise ValueError(f"unsupported project.json version {version!r}, "
                         f"expected {VERSION}")
    return Project(channels=[_channel_from_dict(ch) for ch in data.get("channels", [])],
                   tick_ms=int(data.get("tick_ms", 100)))


def save(project: Project, path: Path) -> None:
    Path(path).write_text(to_json(project) + "\n", encoding="utf-8")


def load(path: Path) -> Project:
    # 文件不存在/是目录/没权限都让 OSError 原样抛出去：那是操作错误不是数据错误，
    # 只有"读出来了但读不懂"才是 ValueError（Task 12 的装载口按这两类分开处理）。
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
