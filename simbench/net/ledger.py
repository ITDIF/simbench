"""net 账本：记下本工程往哪块网卡加过哪些地址、建过哪块网卡。

有了它才谈得上"关闭时收回"——否则删除动作只能靠猜，而猜错的代价是删掉别人手工配的
地址或网卡，在现场等于把设备网打断。账本按机器存放（默认 `%LOCALAPPDATA%/simbench/`），
因为地址与网卡都是机器本地状态，不该跟着工程文件跑到别人机器上。

账本记两类东西：

- **地址**（`entries`）：加到哪块网卡的哪个 IP。回收时只有"账本记过"的才能删。
- **网卡**（`adapters`）：simbench 自己建的适配器（GUID + 设备实例）。这样"关闭时
  连网卡一起收掉"才有依据，也才能保证只认自己建的那块、不碰机器上别人的环回适配器。

**宁可不删，不可误删**：账本读不出来、格式不对、字段缺失，一律当作"什么都没记"，
于是 owned 为空、回收动作一个都不产生；损坏本身只报一条 note。这个方向的错是可以
被发现的（地址/网卡还在机器上），反方向的错在现场很难补救。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

LEDGER_ENV = "SIMBENCH_NET_LEDGER"
_APP_DIR = "simbench"
_FILE = "net-ledger.json"


def default_path() -> Path:
    """账本默认位置：机器本地，不跟着工程文件走。"""
    override = os.environ.get(LEDGER_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if not base:
        return Path.home() / f".{_APP_DIR}" / _FILE
    return Path(base) / _APP_DIR / _FILE


def _key(guid: str) -> str:
    return (guid or "").lower().strip("{}")


@dataclass
class Entry:
    ip: str
    prefix: int
    adapter_guid: str
    added_at: str = ""

    def to_json(self) -> dict:
        return {"ip": self.ip, "prefix": self.prefix,
                "adapter_guid": self.adapter_guid, "added_at": self.added_at}


@dataclass
class AdapterRecord:
    """simbench 自己建的网卡。`device_instance` 是卸载时要用的那条路径。"""

    guid: str
    name: str
    device_instance: str = ""
    created_at: str = ""

    def to_json(self) -> dict:
        return {"guid": self.guid, "name": self.name,
                "device_instance": self.device_instance, "created_at": self.created_at}


@dataclass
class Ledger:
    entries: list[Entry] = field(default_factory=list)
    adapters: list[AdapterRecord] = field(default_factory=list)
    corrupt: str = ""          # 非空表示账本不可信，此时 owned 一律为空

    @classmethod
    def load(cls, path: Path | None = None) -> Ledger:
        target = path or default_path()
        if not target.exists():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            entries = [Entry(ip=str(e["ip"]), prefix=int(e["prefix"]),
                             adapter_guid=str(e["adapter_guid"]),
                             added_at=str(e.get("added_at", "")))
                       for e in payload.get("entries", [])]
            adapters = [AdapterRecord(guid=str(a["guid"]), name=str(a.get("name", "")),
                                      device_instance=str(a.get("device_instance", "")),
                                      created_at=str(a.get("created_at", "")))
                        for a in payload.get("adapters", [])]
        except Exception as exc:               # noqa: BLE001
            # 损坏方向要安全：当作没记
            return cls(corrupt=f"{target}: 账本读不出来（{exc}），本次不回收任何地址或网卡")
            return cls(corrupt=f"{target}: 账本读不出来（{exc}），本次不回收任何地址或网卡")
        return cls(entries=entries, adapters=adapters)

    def save(self, path: Path | None = None) -> Path:
        target = path or default_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        body = {"version": 1,
                "entries": [e.to_json() for e in self.entries],
                "adapters": [a.to_json() for a in self.adapters]}
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)                            # 原子替换，半截文件不会留在地上
        return target

    # ---------- 地址 ----------

    def owned(self, adapter_guid: str) -> set[str]:
        if self.corrupt:
            return set()
        wanted = _key(adapter_guid)
        return {e.ip for e in self.entries if _key(e.adapter_guid) == wanted}

    def record(self, adapter_guid: str, added: list[tuple[str, int]], stamp: str) -> None:
        known = {(e.ip, _key(e.adapter_guid)) for e in self.entries}
        for ip, prefix in added:
            if (ip, _key(adapter_guid)) in known:
                continue
            self.entries.append(Entry(ip=ip, prefix=prefix,
                                      adapter_guid=adapter_guid, added_at=stamp))

    def drop(self, adapter_guid: str, ips: list[str]) -> None:
        wanted, gone = _key(adapter_guid), set(ips)
        self.entries = [e for e in self.entries
                        if not (e.ip in gone and _key(e.adapter_guid) == wanted)]

    # ---------- 网卡 ----------

    def owned_adapter_guids(self) -> set[str]:
        if self.corrupt:
            return set()
        return {a.guid for a in self.adapters}

    def owned_device_instances(self) -> set[str]:
        if self.corrupt:
            return set()
        return {a.device_instance.upper() for a in self.adapters if a.device_instance}

    def record_adapter(self, guid: str, name: str, device_instance: str,
                       stamp: str) -> None:
        wanted = _key(guid)
        for record in self.adapters:
            if _key(record.guid) == wanted:
                record.name, record.device_instance = name, device_instance
                return
        self.adapters.append(AdapterRecord(guid=guid, name=name,
                                           device_instance=device_instance,
                                           created_at=stamp))

    def drop_adapter(self, guid: str) -> None:
        wanted = _key(guid)
        self.adapters = [a for a in self.adapters if _key(a.guid) != wanted]
