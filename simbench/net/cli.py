"""`python -m simbench net ...` 的子命令实现。

分两类，界线是"会不会改系统"：

- **只读**：`show` 看网卡现状、`check` 把工程要的地址和现状对一遍、`device show` 判断
  专用网卡有没有/能不能造。这三条不需要管理员，也永远不会改动系统。
- **改动**：`device create` / `device remove`。建/删网卡是设备级动作，必须提权。
  本命令自己处理提权：当前不是管理员时用 `Start-Process -Verb RunAs` 重跑自己
  （弹一次 UAC），子进程把结果写成 JSON 回传——提权进程的 stdout 抓不到，只能用文件。

提权只覆盖这一条子命令，**GUI 与其余命令始终保持普通权限**。

退出码沿用本项目的口径：`check` 发现差异回 2（脚本里能当"需要处理"用）；读/改系统
失败回 2 并给一句人话；成功回 0。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from ..project.validate import error_lines
from . import spec as netspec
from .device import (
    MANAGED_ADAPTER_NAME,
    find_managed_adapter,
    loopback_inf_path,
    prepare_report,
    read_root_net_devices,
)

# 提权编排搬去了 `elevate.py`（O10）：这里**再导出**（`as` 成同一个名字是显式声明
# 「给外面用」），用例与 `startup.py` 按老路径 import 那两个名字。
from .elevate import _run_elevated as _run_elevated
from .elevate import _self_command_prefix as _self_command_prefix
from .ledger import Ledger
from .ledger import default_path as default_ledger_path
from .plan import foreign_owners, plan_addresses
from .state import (
    NetError,
    is_elevated,
    read_adapters,
    read_addresses,
    read_all_addresses,
    resolve_adapter,
    run_powershell,
)
from .win32 import create_loopback_device, remove_device

_RENAME_TIMEOUT_S = 30.0


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 地址：只读 ----------

def _parse_ip_spec(text: str) -> tuple[str, int]:
    """`10.136.107.3/24` -> ("10.136.107.3", 24)。不给掩码按 /24 算（现场口径）。"""
    raw = text.strip()
    if not raw:
        raise NetError("空的地址")
    if "/" not in raw:
        raw = f"{raw}/24"
    try:
        net = ipaddress.ip_interface(raw)
    except ValueError as exc:
        raise NetError(f"地址 {text!r} 不合法：{exc}") from exc
    if net.version != 4:
        raise NetError(f"只支持 IPv4，收到 {text!r}")
    return str(net.ip), net.network.prefixlen


def _read_ip_file(path: str) -> list[tuple[str, int]]:
    target = Path(path)
    if not target.exists():
        raise NetError(f"读不到地址清单 {path}")
    specs = []
    for lineno, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        try:
            specs.append(_parse_ip_spec(body))
        except NetError as exc:
            raise NetError(f"{path} 第 {lineno} 行：{exc}") from exc
    if not specs:
        raise NetError(f"{path} 里没有有效地址")
    return specs


def _resolve(adapters, key: str):
    adapter = resolve_adapter(adapters, key)
    if not adapter.up:
        print(f"[net] 注意：{adapter.label} 当前状态是 {adapter.status}，"
              "地址加上去也未必通", file=sys.stderr)
    return adapter


def _load_ledger(args) -> Ledger:
    ledger = Ledger.load(Path(args.ledger) if args.ledger else None)
    if ledger.corrupt:
        print(f"[net] {ledger.corrupt}", file=sys.stderr)
    return ledger


def _cmd_show(args) -> int:
    adapters = read_adapters()
    if args.adapter:
        adapters = [_resolve(adapters, args.adapter)]
    for adapter in adapters:
        print(f"{adapter.label}  ifIndex={adapter.if_index}  GUID={adapter.guid}  "
              f"status={adapter.status}")
        if not adapter.up and args.adapter is None:
            continue
        try:
            addresses = read_addresses(adapter.if_index)
        except NetError as exc:
            print(f"    （读地址失败：{exc}）")
            continue
        manual = [a for a in addresses if a.manual]
        auto = len(addresses) - len(manual)
        print(f"    手工地址 {len(manual)} 个，系统自动/DHCP {auto} 个")
        for address in sorted(manual, key=lambda a: ipaddress.ip_address(a.ip)):
            print(f"      {address.cidr}")
    return 0


def _cmd_check(args) -> int:
    # 地址来源与 `up`/`apply` 共用一套（`_desired_addresses`）：校验的那份清单必须就是将要
    # 应用的那份。两处各解析一遍，早晚分叉成"check 说没问题、up 报的却是别的地址"，
    # 而这种分叉在现场最难查——两个命令都"没报错"。
    desired = _desired_addresses(args)

    adapters = read_adapters()
    adapter = _resolve(adapters, args.adapter)
    present = read_addresses(adapter.if_index)
    result = plan_addresses(adapter.label, desired, present,
                            _load_ledger(args).owned(adapter.guid))
    print(f"[net] {adapter.label}（{adapter.guid}）")
    for line in result.summary():
        print(f"[net] {line}")
    return 0 if result.clean else 2


# ---------- 网卡：show 只读，create/remove 提权 ----------

def _device_state(ledger: Ledger):
    adapters = read_adapters()
    return adapters, find_managed_adapter(adapters, ledger.owned_adapter_guids())


def _cmd_device_show(args) -> int:
    ledger = _load_ledger(args)
    adapters, _managed = _device_state(ledger)
    devices = read_root_net_devices()
    print(f"[net] ROOT\\NET 设备实例 {len(devices)} 个")
    for device in devices:
        print(f"[net]   {device.instance_id}  {device.friendly_name}  [{device.status}]")
    for line in prepare_report(adapters, is_elevated(), owned_guids=ledger.owned_adapter_guids()):
        print(f"[net] {line}")
    return 0


def _wait_for_adapter(instance_id: str, timeout: float = _RENAME_TIMEOUT_S):
    r"""等**我们刚建的那个实例**变成一块网卡。

    按实例 ID 精确匹配，不按命名空间猜：`SetupDiCreateDeviceInfo` 会把传进去的设备名
    用作实例路径（实测建出来是 `ROOT\*MSLOOP\NNNN`，不是 `ROOT\NET\NNNN`），
    照着 ROOT\NET 找会永远找不到、每次都误报失败——这个坑已经踩过一次。
    """
    wanted = instance_id.upper()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for adapter in read_adapters():
            if adapter.pnp_device_id.upper() == wanted:
                return adapter
        time.sleep(0.5)
    return None


def _create_and_record(adapter_name: str, ledger: Ledger, ledger_path: Path | None) -> dict:
    _adapters, managed = _device_state(ledger)
    if managed is not None:
        return {"ok": True, "already": True, "name": managed.name,
                "if_index": managed.if_index, "guid": managed.guid,
                "message": f"{managed.name} 已经存在，没有重复创建"}

    inf = loopback_inf_path()
    instance_id, reboot = create_loopback_device(inf)
    adapter = _wait_for_adapter(instance_id)
    if adapter is None:
        raise NetError(f"设备 {instance_id} 已注册，但网卡在 30 秒内没有出现；"
                       "它可能留在设备管理器里，请用 `net device remove "
                       f"--device-instance {instance_id}` 清掉")

    run_powershell(f"Get-NetAdapter -InterfaceIndex {adapter.if_index} | "
                   f"Rename-NetAdapter -NewName '{adapter_name}'")
    final = None
    for candidate in read_adapters():
        if candidate.guid == adapter.guid:
            final = candidate
            break
    final = final or adapter

    ledger.record_adapter(final.guid, final.name, final.pnp_device_id, _stamp())
    ledger.save(ledger_path)
    return {"ok": True, "already": False, "name": final.name,
            "if_index": final.if_index, "guid": final.guid,
            "device_instance": final.pnp_device_id, "reboot_required": reboot,
            "message": f"已创建 {final.name}（ifIndex={final.if_index}）"}


def _remove_and_record(instance_id: str, ledger: Ledger, ledger_path: Path | None) -> dict:
    if not instance_id:
        records = ledger.adapters
        if not records:
            raise NetError("账本里没有 simbench 建的网卡；请用 --device-instance 指定")
        instance_id = records[0].device_instance
    output = remove_device(instance_id)
    for record in list(ledger.adapters):
        if record.device_instance.upper() == instance_id.upper():
            ledger.drop_adapter(record.guid)
    ledger.save(ledger_path)
    return {"ok": True, "device_instance": instance_id,
            "message": f"已移除 {instance_id}", "detail": output}


def _cmd_device_write(args) -> int:
    ledger_path = Path(args.ledger) if args.ledger else default_ledger_path()
    ledger = _load_ledger(args)

    if args.dry_run:
        adapters, managed = _device_state(ledger)
        # 预演也要说实话：网卡已经在那儿了还说"创建"，人就会以为这下会多出一块
        verb = ("移除网卡" if args.device_cmd == "remove"
                else "复用已存在的网卡" if managed else "创建网卡")
        print(f"[net] 将要执行：{verb}")
        if args.device_cmd == "create":
            print(f"[net]   名字：{args.adapter_name}")
            print(f"[net]   驱动：{loopback_inf_path()}（系统自带）")
            print(f"[net]   当前状态：{prepare_report(adapters, is_elevated(), owned_guids=ledger.owned_adapter_guids())[0]}")
            print(f"[net]   账本：{ledger_path}")
        else:
            print(f"[net]   设备实例：{args.device_instance or '（取账本第一条）'}")
        need = "需要管理员权限（会弹一次 UAC）" if not is_elevated() else "当前已是管理员，无需再提权"
        print(f"[net]   {need}；去掉 --dry-run 才会真的执行")
        return 0

    if not is_elevated() and not args.elevated:
        print("[net] 需要管理员权限，正在请求提权（请确认 UAC）…", file=sys.stderr)
        result = _run_elevated(args, ["device", args.device_cmd],
                               {"action": args.device_cmd,
                                "adapter_name": args.adapter_name,
                                "device_instance": args.device_instance,
                                "ledger": args.ledger})
    else:
        if args.device_cmd == "create":
            result = _create_and_record(args.adapter_name, ledger, ledger_path)
        else:
            result = _remove_and_record(args.device_instance, ledger, ledger_path)

    if args.result:
        Path(args.result).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"[net] {result['message']}")
    if result.get("already"):
        return 0
    return 0


def _mutate_addresses(if_index: int, items: list[tuple[str, int]],
                      action: str) -> list[dict]:
    """批量增/删地址，逐条回结果。

    逐条 try/catch 而不是一把梭：52 个地址里错一个不该让整批回滚——现场要的是
    "加上了哪些、哪几个没加上、为什么"，而不是一句笼统的失败。
    """
    verb = "New-NetIPAddress" if action == "add" else "Remove-NetIPAddress"
    lines = ["$out = @()"]
    for ip, prefix in items:
        extra = "" if action == "add" else " -Confirm:$false"
        lines.append(
            f"try {{ {verb} -InterfaceIndex {if_index} -IPAddress '{ip}' "
            f"-PrefixLength {prefix}{extra} -ErrorAction Stop | Out-Null; "
            f"$out += [pscustomobject]@{{ip='{ip}';ok=$true;err=''}} }} "
            f"catch {{ $out += [pscustomobject]@{{ip='{ip}';ok=$false;"
            f"err=$_.Exception.Message}} }}")
    lines.append("@($out) | ConvertTo-Json -Compress")
    raw = run_powershell("\n".join(lines))
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise NetError(f"解析地址操作结果失败：{exc}") from exc
    return payload if isinstance(payload, list) else [payload]


def _target_adapter(args, ledger: Ledger):
    """定目标网卡：显式给就用给的，否则用 simbench 自己建的那块。

    默认**不会**落到机器上别的网卡上——没有 `--adapter` 又找不到我们的网卡时直接报错，
    绝不退而求其次去改现场的真实网卡。
    """
    adapters = read_adapters()
    if args.adapter:
        return _resolve(adapters, args.adapter)
    managed = find_managed_adapter(adapters, ledger.owned_adapter_guids())
    if managed is None:
        raise NetError(f"找不到专用网卡 {MANAGED_ADAPTER_NAME}；"
                       "先跑 `net device create`，或用 --adapter 显式指定")
    return managed


def _desired_addresses(args) -> list[tuple[str, int]]:
    """地址来源三选多：命令行 --ip、清单文件 --ip-file、网络声明 --net-file/--point-table。

    网络声明优先被认作"这个工程的权威清单"——它旁边（`x.csv` -> `x.net.json`）有就用，
    也可以显式 `--net-file` 指过去。
    """
    desired = [_parse_ip_spec(s) for s in getattr(args, "ip", [])]
    if getattr(args, "ip_file", None):
        desired.extend(_read_ip_file(args.ip_file))

    net_file = getattr(args, "net_file", None)
    point_table = getattr(args, "point_table", None)
    if net_file or point_table:
        spec = (netspec.load(net_file) if net_file
                else netspec.load_companion(point_table))
        if spec is None:
            raise NetError(f"点表旁边没有网络声明（{netspec.companion_path(point_table)}）；"
                           "先用 --net-file 指一份，或先用 --ip 显式给地址")
        desired.extend(netspec.unique_addresses(spec))

    if not desired:
        raise NetError("没有给地址：用 --ip、--ip-file 或 --net-file/--point-table")
    seen: dict[str, int] = {}
    for ip, prefix in desired:
        if ip in seen and seen[ip] != prefix:
            raise NetError(f"同一个地址给了两种掩码：{ip}/{seen[ip]} 与 {ip}/{prefix}")
        seen[ip] = prefix
    return sorted(seen.items())


def _report_plan(adapter, plan) -> None:
    print(f"[net] {adapter.label}（{adapter.guid}）")
    for line in plan.summary():
        print(f"[net] {line}")


def _cmd_apply(args) -> int:
    ledger_path = Path(args.ledger) if args.ledger else default_ledger_path()
    ledger = _load_ledger(args)
    adapter = _target_adapter(args, ledger)
    desired = _desired_addresses(args)
    present = read_addresses(adapter.if_index)

    # 同一 IP 不能同时挂在两块网卡上。先查"是不是已经被别的网卡占了"——直接加只会得到
    # 一句英文的 `The object already exists.`，现场看不出是撞了哪块网卡（实测踩过）。
    names = {a.if_index: a.name for a in read_adapters()}
    owners = {addr.ip: names.get(addr.if_index, f"ifIndex {addr.if_index}")
              for addr in read_all_addresses() if addr.if_index != adapter.if_index}
    blocked = sorted(foreign_owners(desired, owners))

    plan = plan_addresses(adapter.label, desired, present, ledger.owned(adapter.guid))

    if args.dry_run or not plan.to_add:
        _report_plan(adapter, plan)
        if not plan.to_add:
            return 0 if plan.clean else 2
        if blocked:
            print(f"[net] 有 {len(blocked)} 个地址已经配在别的网卡上了，直接加会失败：")
            for ip in blocked[:5]:
                print(f"[net]   {ip} 现在在 {owners[ip]} 上")
            if len(blocked) > 5:
                print(f"[net]   …还有 {len(blocked) - 5} 个")
            print("[net]   要迁到新网卡，得先从旧网卡上把地址撤掉（或换用没被占用的 IP）")
        print("[net]   需要管理员权限；去掉 --dry-run 才会真的执行")
        return 2 if blocked else 0

    if not is_elevated() and not args.elevated:
        print("[net] 需要管理员权限，正在请求提权（请确认 UAC）…", file=sys.stderr)
        payload = _run_elevated(args, "apply",
                                {"adapter": adapter.guid, "ip": args.ip,
                                 "ip_file": args.ip_file, "net_file": args.net_file,
                                 "point_table": getattr(args, "point_table", None),
                                 "ledger": args.ledger})
        for line in payload.get("lines", []):
            print(f"[net] {line}")
        return payload.get("code", 0)

    wanted = dict(desired)
    results = _mutate_addresses(adapter.if_index,
                                [(ip, wanted[ip]) for ip in plan.to_add], "add")
    added = sorted(r["ip"] for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    if added:
        ledger.record(adapter.guid, [(ip, wanted[ip]) for ip in added], _stamp())
        ledger.save(ledger_path)

    lines = [f"已添加 {len(added)} 个地址到 {adapter.name}"
             + (f"：{'、'.join(added)}" if added else "")]
    if failed:
        lines.append(f"失败 {len(failed)} 个：" +
                     "；".join(f"{r['ip']}（{r['err']}）" for r in failed))
    if plan.prefix_conflicts:
        lines.append(f"掩码不一致、未改动 {len(plan.prefix_conflicts)} 个："
                     + "、".join(plan.prefix_conflicts))
    for line in lines:
        print(f"[net] {line}")
    if args.result:
        Path(args.result).write_text(json.dumps(
            {"ok": True, "lines": lines, "code": 2 if failed else 0},
            ensure_ascii=False), encoding="utf-8")
    return 2 if failed else 0


def _cmd_clean(args) -> int:
    ledger_path = Path(args.ledger) if args.ledger else default_ledger_path()
    ledger = _load_ledger(args)
    adapter = _target_adapter(args, ledger)
    owned = ledger.owned(adapter.guid)
    present = {a.ip for a in read_addresses(adapter.if_index) if a.manual}
    targets = sorted(owned & present)

    if args.dry_run or not targets:
        print(f"[net] {adapter.label}（{adapter.guid}）")
        if not targets:
            print("[net] 账本里没有需要收回的地址")
            return 0
        print(f"[net] 待收回 {len(targets)} 个（本工程加的）：" + "、".join(targets))
        print("[net]   需要管理员权限；去掉 --dry-run 才会真的执行")
        return 0

    if not is_elevated() and not args.elevated:
        print("[net] 需要管理员权限，正在请求提权（请确认 UAC）…", file=sys.stderr)
        payload = _run_elevated(args, "clean", {"adapter": adapter.guid,
                                                "ip": [], "ip_file": None,
                                                "ledger": args.ledger})
        for line in payload.get("lines", []):
            print(f"[net] {line}")
        return payload.get("code", 0)

    results = _mutate_addresses(adapter.if_index, [(ip, 24) for ip in targets], "remove")
    removed = sorted(r["ip"] for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    if removed:
        ledger.drop(adapter.guid, removed)
        ledger.save(ledger_path)
    lines = [f"已收回 {len(removed)} 个地址" + (f"：{'、'.join(removed)}" if removed else "")]
    if failed:
        lines.append("失败 " + "；".join(f"{r['ip']}（{r['err']}）" for r in failed))
    for line in lines:
        print(f"[net] {line}")
    if args.result:
        Path(args.result).write_text(json.dumps(
            {"ok": True, "lines": lines, "code": 2 if failed else 0},
            ensure_ascii=False), encoding="utf-8")
    return 2 if failed else 0


def _cmd_init(args) -> int:
    """生成网络声明骨架：把点表里的设备名和一份 IP 清单配起来。

    设备名从点表读（工程里已经写好的那批），IP 从 `--ip` / `--ip-file` 给——IP 是
    环境相关的，工具猜不出来，也不该猜。数量对不上就报出来，不静默截断：现场少配一台
    设备的 IP，表现是"那台设备连不上"，查起来很费时间。
    """
    # 这里**不能**用 _desired_addresses：它会因为 --point-table 而去读点表旁边的网络声明，
    # 而 init 的语义恰恰相反——点表在这里只提供设备名，声明正是要被生成的东西。
    raw = [_parse_ip_spec(s) for s in getattr(args, "ip", [])]
    if getattr(args, "ip_file", None):
        raw.extend(_read_ip_file(args.ip_file))
    if not raw:
        raise NetError("没有给 IP：用 --ip 或 --ip-file")
    deduped: dict[str, int] = {}
    for ip, prefix in raw:
        deduped.setdefault(ip, prefix)
    addresses = sorted(deduped.items())

    names: list[str] = []
    if getattr(args, "point_table", None):
        from ..csv_io_entry import load_point_table

        project, _notes, issues = load_point_table(args.point_table)
        if error_lines(issues):
            raise NetError(f"点表有 {len(error_lines(issues))} 处错误，先修好再生成网络声明")
        names = [device.name for channel in project.channels for device in channel.devices]

    if names and len(names) > len(addresses):
        raise NetError(f"点表里有 {len(names)} 台设备，只给了 {len(addresses)} 个 IP；"
                       "IP 不够分配")

    devices = []
    for index, (ip, prefix) in enumerate(addresses):
        name = names[index] if index < len(names) else ip
        devices.append(netspec.DeviceNet(name=name, ip=ip, prefix=prefix))

    spec = netspec.NetworkSpec(
        adapter_name=getattr(args, "adapter_name", MANAGED_ADAPTER_NAME) or MANAGED_ADAPTER_NAME,
        devices=devices)

    target = Path(args.out) if args.out else (
        netspec.companion_path(args.point_table) if getattr(args, "point_table", None)
        else Path.cwd() / "network.net.json")
    if target.exists() and not args.force:
        raise NetError(f"{target} 已存在；加 --force 覆盖")

    netspec.save(spec, target)
    print(f"[net] 已写出 {target}")
    print(f"[net] {len(devices)} 台设备，适配网卡 {spec.adapter_name}")
    if names and len(names) < len(devices):
        print(f"[net] 注意：点表只有 {len(names)} 台设备，多出的 IP 用了地址本身当名字")
    return 0


def _cmd_up(args) -> int:
    """一条命令把网络准备好：没有专用网卡就建，缺的地址就配。

    这是"启动模拟就该自动完成"的那两件事的合体。做成一条命令而不是让调用方连着敲
    `device create` + `apply`，是因为两件事都要提权——合成一次只弹一次 UAC，
    现场体验差别很大。默认网卡就是 simbench 自己那块，不会落到现场真实网卡上。
    """
    ledger_path = Path(args.ledger) if args.ledger else default_ledger_path()
    ledger = _load_ledger(args)
    desired = _desired_addresses(args)

    if args.dry_run:
        adapters = read_adapters()
        managed = find_managed_adapter(adapters, ledger.owned_adapter_guids())
        print(f"[net] 将要执行：{'复用' if managed else '创建'}专用网卡"
              + (f" {managed.name}（ifIndex={managed.if_index}）" if managed else
                 f" {getattr(args, "adapter_name", MANAGED_ADAPTER_NAME)}（系统自带 {loopback_inf_path().name}）"))
        print(f"[net] 然后配 {len(desired)} 个地址到它上面")
        if managed is not None:
            names = {a.if_index: a.name for a in read_adapters()}
            owners = {addr.ip: names.get(addr.if_index, f"ifIndex {addr.if_index}")
                      for addr in read_all_addresses()
                      if addr.if_index != managed.if_index}
            blocked = sorted(foreign_owners(desired, owners))
            if blocked:
                print(f"[net] 其中 {len(blocked)} 个已经配在别的网卡上，直接加会失败：")
                for ip in blocked[:5]:
                    print(f"[net]   {ip} 现在在 {owners[ip]} 上")
                print("[net]   要迁过来得先从旧网卡撤掉，或换用没被占用的 IP")
        print("[net]   需要管理员权限；去掉 --dry-run 才会真的执行")
        return 0

    if not is_elevated() and not args.elevated:
        print("[net] 需要管理员权限，正在请求提权（请确认 UAC）…", file=sys.stderr)
        payload = _run_elevated(args, "up", {"adapter": None, "ip": args.ip,
                                             "ip_file": args.ip_file,
                                             "net_file": args.net_file,
                                             "point_table": getattr(args, "point_table", None),
                                             "adapter_name": getattr(args, "adapter_name", MANAGED_ADAPTER_NAME),
                                             "ledger": args.ledger})
        for line in payload.get("lines", []):
            print(f"[net] {line}")
        return payload.get("code", 0)

    lines = []
    adapters = read_adapters()
    adapter = find_managed_adapter(adapters, ledger.owned_adapter_guids())
    if adapter is None:
        created = _create_and_record(getattr(args, "adapter_name", MANAGED_ADAPTER_NAME), ledger, ledger_path)
        lines.append(created["message"])
        adapter = find_managed_adapter(read_adapters(), ledger.owned_adapter_guids())
        if adapter is None:
            raise NetError("网卡创建后没有找到它；请用 `net device show` 看现场")

    wanted = dict(desired)
    names = {a.if_index: a.name for a in read_adapters()}
    owners = {addr.ip: names.get(addr.if_index, f"ifIndex {addr.if_index}")
              for addr in read_all_addresses() if addr.if_index != adapter.if_index}
    blocked = sorted(foreign_owners(desired, owners))

    plan = plan_addresses(adapter.label, desired,
                          read_addresses(adapter.if_index), ledger.owned(adapter.guid))
    # 已被别的网卡占用的不许去试：试了必然失败，还会报出两句互相矛盾的话
    to_add = [ip for ip in plan.to_add if ip not in owners]
    if to_add:
        results = _mutate_addresses(adapter.if_index,
                                    [(ip, wanted[ip]) for ip in to_add], "add")
        added = sorted(r["ip"] for r in results if r.get("ok"))
        failed = [r for r in results if not r.get("ok")]
        if added:
            ledger.record(adapter.guid, [(ip, wanted[ip]) for ip in added], _stamp())
            ledger.save(ledger_path)
        lines.append(f"已配 {len(added)} 个地址到 {adapter.name}"
                     + (f"：{'、'.join(added[:5])}" + ("…" if len(added) > 5 else "")
                        if added else ""))
        if failed:
            lines.append("失败 " + "；".join(f"{r['ip']}（{r['err'].strip()}）"
                                            for r in failed[:5]))
    else:
        lines.append("地址已经齐了，没有要加的")

    for ip in blocked[:5]:
        lines.append(f"{ip} 已被 {owners[ip]} 占用，未处理")
    if len(blocked) > 5:
        lines.append(f"…另有 {len(blocked) - 5} 个地址被别的网卡占用")

    for line in lines:
        print(f"[net] {line}")
    code = 2 if (blocked or [r for r in locals().get("results", []) if not r.get("ok")]) else 0
    if args.result:
        Path(args.result).write_text(json.dumps(
            {"ok": True, "lines": lines, "code": code}, ensure_ascii=False),
            encoding="utf-8")
    return code


def _cmd_device(args) -> int:
    if args.device_cmd in (None, "show"):
        return _cmd_device_show(args)
    return _cmd_device_write(args)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simbench net",
        description="系统网络：查看现状、对账地址、准备专用网卡")
    sub = parser.add_subparsers(dest="cmd", required=True)

    show = sub.add_parser("show", help="列出网卡与已配地址（只读）")
    show.add_argument("--adapter", default=None, help="只看这一块（GUID / ifIndex / 名称）")

    check = sub.add_parser("check", help="把指定的地址清单和网卡现状对一遍（只读）")
    check.add_argument("--adapter", required=True, help="目标网卡（GUID / ifIndex / 名称）")
    check.add_argument("--ip", action="append", default=[],
                       help="要校验的地址，可重复；不给掩码按 /24 算")
    check.add_argument("--ip-file", default=None, help="地址清单文件，一行一个，# 起注释")
    check.add_argument("--net-file", default=None,
                       help="网络声明文件（IP ↔ 设备）")
    check.add_argument("--point-table", default=None,
                       help="点表；用旁边的 x.net.json 作为地址来源")
    check.add_argument("--ledger", default=None, help="账本路径（默认按机器存放）")

    device = sub.add_parser("device", help="专用网卡：查看 / 创建 / 移除")
    dsub = device.add_subparsers(dest="device_cmd")
    for name, helptext in (("show", "查看专用网卡状态（只读）"),
                           ("create", "创建 simbench 专用网卡（提权）"),
                           ("remove", "移除 simbench 专用网卡（提权）")):
        child = dsub.add_parser(name, help=helptext)
        child.add_argument("--adapter-name", default=MANAGED_ADAPTER_NAME)
        child.add_argument("--ledger", default=None)
        child.add_argument("--device-instance", default="",
                           help="移除时指定设备实例（默认取账本第一条）")
        child.add_argument("--dry-run", action="store_true",
                           help="只说要做什么，不改系统")
        child.add_argument("--result", default=None,
                           help=argparse.SUPPRESS)      # 提权子进程回传结果用
        child.add_argument("--elevated", action="store_true",
                           help=argparse.SUPPRESS)      # 已经在提权进程里了
        child.add_argument("--log", default=None,
                           help=argparse.SUPPRESS)      # 提权子进程的输出去向
        child.add_argument("--job", default=None,
                           help=argparse.SUPPRESS)      # 提权任务文件（绕开命令行引号）

    init = sub.add_parser("init", help="从点表和 IP 清单生成网络声明骨架")
    init.add_argument("--out", default=None, help="输出路径（默认点表旁的 x.net.json）")
    init.add_argument("--point-table", default=None, help="从这里读设备名")
    init.add_argument("--ip", action="append", default=[])
    init.add_argument("--ip-file", default=None)
    init.add_argument("--net-file", default=None)
    init.add_argument("--adapter-name", default=MANAGED_ADAPTER_NAME)
    init.add_argument("--force", action="store_true", help="覆盖已存在的文件")

    for name, helptext in (("up", "一步到位：建网卡 + 配地址（提权）"),
                           ("clean", "收回本工程加过的地址（提权）")):
        child = sub.add_parser(name, help=helptext)
        child.add_argument("--adapter", default=None,
                           help="目标网卡（默认用 simbench 专用网卡）")
        child.add_argument("--ip", action="append", default=[],
                           help="要配的地址，可重复；不给掩码按 /24 算")
        child.add_argument("--ip-file", default=None,
                           help="地址清单文件，一行一个，# 起注释")
        child.add_argument("--ledger", default=None)
        child.add_argument("--net-file", default=None,
                           help="网络声明文件（IP ↔ 设备）")
        child.add_argument("--point-table", default=None,
                           help="点表；用旁边的 x.net.json 作为地址来源")
        child.add_argument("--dry-run", action="store_true",
                           help="只说要做什么，不改系统")
        child.add_argument("--result", default=None, help=argparse.SUPPRESS)
        child.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
        child.add_argument("--job", default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if getattr(args, "job", None):
        # 提权子进程：所有参数从任务文件读回来，命令行只带一个路径。
        # 用 getattr 兜底：不同子命令的参数集不一样（device 有 device_cmd，apply 没有）。
        job = json.loads(Path(args.job).read_text(encoding="utf-8"))
        args.cmd = job.get("cmd", args.cmd)
        if hasattr(args, "device_cmd"):
            args.device_cmd = job.get("action", args.device_cmd)
        args.adapter = job.get("adapter", getattr(args, "adapter", None))
        args.ip = job.get("ip", getattr(args, "ip", []))
        args.ip_file = job.get("ip_file", getattr(args, "ip_file", None))
        args.net_file = job.get("net_file", getattr(args, "net_file", None))
        args.point_table = job.get("point_table", getattr(args, "point_table", None))
        args.adapter_name = job.get("adapter_name", getattr(args, "adapter_name", ""))
        args.device_instance = job.get("device_instance",
                                       getattr(args, "device_instance", ""))
        args.ledger = job.get("ledger") or None
        args.result = job.get("result")
        args.log = job.get("log")
    if getattr(args, "log", None):
        # 提权子进程没有可见控制台，起手先把输出接到文件，失败原因才不会丢；
        # 这个句柄要活到进程结束（sys.stdout 就是它），所以不能用 with
        handle = open(args.log, "w", encoding="utf-8",  # noqa: SIM115
                      buffering=1)
        sys.stdout = sys.stderr = handle
    try:
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd == "check":
            return _cmd_check(args)
        if args.cmd == "apply":
            return _cmd_apply(args)
        if args.cmd == "init":
            return _cmd_init(args)
        if args.cmd == "up":
            return _cmd_up(args)
        if args.cmd == "clean":
            return _cmd_clean(args)
        return _cmd_device(args)
    except NetError as exc:
        # 提权进程把失败写进结果文件，父进程才报得出原因（隐藏窗口里什么都看不见）
        result_path = getattr(args, "result", None)
        if result_path:
            Path(result_path).write_text(
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
                encoding="utf-8")
        print(f"[error] [net] {exc}", file=sys.stderr)
        return 2
