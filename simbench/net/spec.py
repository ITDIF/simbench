"""网络声明文件：把"哪台设备用哪个 IP"落成可版本化的一份东西。

为什么不塞进 `project.json`：IP 是**环境相关的**（现场、同事机器、我的机器各不相同），
而点表是设备定义，两份东西的生命周期不一样。所以网络声明单独一份 `.net.json`，
跟点表放一起 —— 从使用者角度看还是一个目录、一条命令，但它不会让"拷贝点表给同事"
顺手把地址也带过去。

格式（故意的扁平，现场拿记事本也能改）：

    {
      "adapter_name": "simbench 模拟网卡",
      "port": 502,
      "devices": [
        {"name": "风机-1", "ip": "10.136.107.3"},
        {"name": "照明回路-1", "ip": "10.136.107.5", "port": 5020}
      ]
    }

`addresses()` 出的前缀长度默认 /24（现场口径，与 `net apply` 的默认一致），
逐条可以覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..project import fsio
from .state import NetError

DEFAULT_PREFIX = 24
DEFAULT_PORT = 502
SUFFIX = ".net.json"


@dataclass
class DeviceNet:
    name: str
    ip: str
    port: int = DEFAULT_PORT
    prefix: int = DEFAULT_PREFIX

    def to_json(self) -> dict:
        body = {"name": self.name, "ip": self.ip}
        if self.port != DEFAULT_PORT:
            body["port"] = self.port
        if self.prefix != DEFAULT_PREFIX:
            body["prefix"] = self.prefix
        return body


@dataclass
class NetworkSpec:
    adapter_name: str
    devices: list[DeviceNet] = field(default_factory=list)

    def addresses(self) -> list[tuple[str, int]]:
        return [(d.ip, d.prefix) for d in self.devices]

    def owner_of(self, ip: str) -> str:
        for device in self.devices:
            if device.ip == ip:
                return device.name
        return ""


def companion_path(point_table: str | Path) -> Path:
    """点表旁边的网络声明路径：`x.json` / `x.csv` -> `x.net.json`。"""
    target = Path(point_table)
    return target.with_name(target.stem + SUFFIX)


def loads(text: str, origin: str = "<内存>") -> NetworkSpec:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise NetError(f"{origin} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise NetError(f"{origin} 顶层应该是一个对象")
    raw = payload.get("devices")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise NetError(f"{origin} 的 devices 应该是数组")

    devices = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise NetError(f"{origin} 第 {index} 个设备不是对象")
        ip = str(item.get("ip") or "").strip()
        if not ip:
            raise NetError(f"{origin} 第 {index} 个设备没有 ip")
        try:
            port = int(item.get("port", DEFAULT_PORT))
            prefix = int(item.get("prefix", DEFAULT_PREFIX))
        except (TypeError, ValueError) as exc:
            raise NetError(f"{origin} 第 {index} 个设备的 port/prefix 不是整数") from exc
        devices.append(DeviceNet(name=str(item.get("name") or ip), ip=ip,
                                 port=port, prefix=prefix))
    return NetworkSpec(adapter_name=str(payload.get("adapter_name") or ""),
                       devices=devices)


def load(path: str | Path) -> NetworkSpec:
    target = Path(path)
    if not target.is_file():
        raise NetError(f"读不到网络声明 {target}")
    return loads(target.read_text(encoding="utf-8"), str(target))


def load_companion(point_table: str | Path) -> NetworkSpec | None:
    """有点表旁边那份就用，没有回 None（不是错误：网络可以还没声明）。"""
    target = companion_path(point_table)
    return load(target) if target.is_file() else None


def save(spec: NetworkSpec, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {"adapter_name": spec.adapter_name or None,
            "devices": [d.to_json() for d in spec.devices]}
    # 与点表/工程同一条路：临时文件 + 原子替换 + 一份 `.bak`
    fsio.write_text_atomic(target, json.dumps(body, ensure_ascii=False, indent=1))
    return target


def unique_addresses(spec: NetworkSpec) -> list[tuple[str, int]]:
    """地址去重，并按 IP 排序。同一个 IP 声明两次是配置错误，直接报出来。"""
    seen: dict[str, int] = {}
    for device in spec.devices:
        if device.ip in seen:
            if seen[device.ip] != device.prefix:
                raise NetError(f"{device.ip} 被声明了两种掩码："
                               f"/{seen[device.ip]} 与 /{device.prefix}")
            raise NetError(f"{device.ip} 被声明了两次（{spec.owner_of(device.ip)} 等）")
        seen[device.ip] = device.prefix
    return sorted(seen.items())
