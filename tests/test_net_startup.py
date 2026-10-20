"""启动挂钩：起监听/开界面之前，按点表旁边那份网络声明把网卡对一遍。

四条口径各配一个"反过来会出事"的用例：

1. **没有声明文件就一声不吭** —— IP 是环境相关的，"没声明"不是错误；
2. **`SIMBENCH_NET_AUTO=0` 是总闸** —— 用例装载的是现场点表，闸门一松，跑一次测试就真会
   去改机器上的网卡地址（conftest 默认关掉它，所以本文件每条都要显式打开）；
3. **已经一致时不提权** —— `net up` 只在该动系统的时候才跑；每次启动弹一次 UAC，
   用户很快就会把整个功能关掉；
4. **网络失败绝不放倒启动** —— 只回一行话，异常不往外抛。

判定全部打在纯数据上（网卡/地址/账本都是假的），**本文件一条真系统调用都不做**。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from simbench.net import startup
from simbench.net.device import MANAGED_ADAPTER_NAME
from simbench.net.errors import NetError
from simbench.net.state import MANUAL, Adapter, Address
from simbench.project.model import Project, TcpChannel

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")

OURS_GUID = "{AAAA1111-0000-0000-0000-000000000001}"
OURS = Adapter(name=MANAGED_ADAPTER_NAME, description="Microsoft KM-TEST 环回适配器",
               if_index=20, guid=OURS_GUID, status="Up", mac="",
               pnp_device_id=r"ROOT\NET\0001")
WLAN = Adapter(name="WLAN", description="Realtek RTL8852BE WiFi 6", if_index=4,
               guid="{1111}", status="Up", mac="")


@pytest.fixture(autouse=True)
def _hook_on(monkeypatch):
    """本文件测的是挂钩本身，所以把 conftest 那个总闸打开；每条用例结束自动关回去。"""
    monkeypatch.setenv(startup.AUTO_ENV, "1")
    startup.forget_verified()


def _declare(tmp_path, devices=({"name": "风机-1", "ip": "10.0.0.1"},),
             name="t.csv") -> pathlib.Path:
    """点表旁边放一份网络声明（点表内容与挂钩无关，只当路径用）。"""
    table = tmp_path / name
    table.write_text("point table, 内容与挂钩无关\n", encoding="utf-8")
    (tmp_path / f"{table.stem}.net.json").write_text(
        json.dumps({"adapter_name": MANAGED_ADAPTER_NAME, "devices": list(devices)}),
        encoding="utf-8")
    return table


def _install(monkeypatch, *, adapters=(OURS,), addresses=(), elsewhere=(),
             ledger=(), owned=(), inf=True, apply=None, apply_ips=None,
             bindable=False):
    """把系统那几处读口换成假数据，并记下 `net up` 被怎么调的。

    两种写法各记一处：`.table` 是 `--point-table`（声明那条），`.ips` 是 `--ip`（没有声明、
    地址来自通道 host 那条）。返回命名对象而不是 list：两条路的记录要能分开断言。
    """
    monkeypatch.setattr(startup, "read_adapters", lambda: list(adapters))
    monkeypatch.setattr(startup, "read_all_addresses",
                        lambda: list(addresses) + list(elsewhere))
    monkeypatch.setattr(startup, "Ledger", SimpleNamespace(load=lambda path=None: SimpleNamespace(
        owned_adapter_guids=lambda: set(ledger), owned=lambda guid: set(owned))))
    monkeypatch.setattr(startup, "_binds_locally", lambda _host: bindable)
    monkeypatch.setattr(startup, "inf_available", lambda: inf)
    monkeypatch.setattr(startup, "loopback_inf_path",
                        lambda: pathlib.Path(r"C:\Windows\INF\netloop.inf"))
    calls = SimpleNamespace(table=[], ips=[], extra=[])

    def record(table, extra=()):
        calls.table.append(table)
        calls.extra.append(list(extra))
        if apply is not None:
            return apply(table)
        return 0, ([f"已配 {len(ledger)} 个地址"] if ledger else ["已一致，无需改动"])

    def record_ips(values):
        calls.ips.append(list(values))
        if apply_ips is not None:
            return apply_ips(values)
        return 0, [f"已配 {len(values)} 个地址"]

    monkeypatch.setattr(startup, "apply_up", record)
    monkeypatch.setattr(startup, "apply_up_ips", record_ips)
    return calls


def _forbid_system(monkeypatch):
    """任何一次系统读都当场炸：用来证明"这条路真的没碰机器"。"""
    def boom(*args, **kwargs):
        raise AssertionError("这一步不许碰系统")
    for name in ("read_adapters", "read_all_addresses"):
        monkeypatch.setattr(startup, name, boom)


# ---------- 什么时候闭嘴 ----------

def test_no_declaration_is_silent(tmp_path, monkeypatch):
    """没有 `.net.json` 就是"这个工程不关心网络"，不是错误，也不该在每次启动时念一遍。"""
    _forbid_system(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    assert startup.prepare(table) == []


def _project(host: str, *, name: str = "tcp-5020", port: int = 5020) -> Project:
    return Project(channels=[TcpChannel(name=name, host=host, port=port)])


def test_a_verified_address_is_checked_without_reading_the_system(
        tmp_path, monkeypatch):
    """完整核对过专用网卡之后，重复启动只做 bind 探针，不再跑 PowerShell。"""
    _install(monkeypatch, bindable=True,
             addresses=[Address("10.99.99.99", 24, 20, MANUAL)],
             ledger={OURS_GUID}, owned={"10.99.99.99"})
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    first = startup.prepare(table, project=_project("10.99.99.99"))
    assert any("网络已就绪" in line for line in first), first

    _forbid_system(monkeypatch)                       # 任何一次系统读都当场炸

    def refuse(*_args):
        raise AssertionError("地址已核对过，不该去提权/跑 net up")

    monkeypatch.setattr(startup, "apply_up_ips", refuse)
    second = startup.prepare(table, project=_project("10.99.99.99"))
    assert any("网络已就绪" in line for line in second), second


def test_an_address_on_another_adapter_is_not_reported_as_ready(
        tmp_path, monkeypatch):
    """地址在 WLAN 上也能 bind，但那不等于"专用网卡已就绪"。"""
    called = _install(monkeypatch, adapters=[OURS, WLAN], bindable=True,
                      elsewhere=[Address("10.99.99.99", 24, 4, MANUAL)])
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    lines = startup.prepare(table, project=_project("10.99.99.99"))
    assert any("WLAN" in line for line in lines), lines
    assert not any("已在本机网卡上" in line for line in lines), lines
    assert called.ips == [], called.ips


def test_a_concrete_host_without_a_declaration_is_put_on_the_dedicated_nic(
        tmp_path, monkeypatch):
    """没有声明、但通道 host 是具体 IP：启动时**把它配到专用网卡上**（2026-10-19 现场反馈）。

    原先这条路什么都没做（"没有声明"被当成"不关心网络"），于是 bind 必然失败——现场看到的就是
    "0.0.0.0/127.0.0.1 能起、换个 IP 就不行，像没用虚拟网卡"。专用网卡本来就是干这件事的：
    网络声明只是**地址的来源之一**，通道 host 是另一个来源（一路一个地址）。
    """
    called = _install(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    lines = startup.prepare(table, project=_project("10.99.99.99"))
    assert called.ips == [["10.99.99.99"]], called.ips
    assert lines[0].startswith("通道 tcp-5020 的 host 是具体地址 10.99.99.99:5020"), lines
    assert lines[1] == "已配 1 个地址", lines


def test_a_concrete_host_never_takes_other_addresses_away(tmp_path, monkeypatch):
    """通道 host 那条路**只加不删**：声明才表达"这个工程要的完整集合"。

    否则"换一个 IP 启动"会把之前配的地址收回——两个实例/两份点表同时用不同 IP 时，第二个会把
    第一个正在用的地址拔掉（实测预览里真的出现过"待收回 10.136.107.240"）。
    """
    called = _install(monkeypatch,
                      addresses=[Address("10.136.107.240", 24, 20, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.136.107.240"})
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    lines = startup.prepare(table, project=_project("10.99.99.99"), apply=False)
    joined = "\n".join(lines)
    assert called.ips == [] and called.table == []
    assert "待添加 1 个：10.99.99.99" in joined, lines
    assert "待收回" not in joined, f"通道 host 那条路不许收回别人的地址: {lines}"


def test_a_concrete_host_preview_names_the_command(tmp_path, monkeypatch):
    """只看不跑（`apply=False`）：一次提权都不发，但要说清该跑哪条命令（`--ip` 那条）。"""
    called = _install(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    lines = startup.prepare(table, project=_project("10.99.99.99"), apply=False)
    assert called.ips == [] and called.table == [], (called.ips, called.table)
    assert any("net up --ip 10.99.99.99" in line for line in lines), lines


def test_a_concrete_host_held_by_another_adapter_is_reported_not_added(tmp_path, monkeypatch):
    """地址已经挂在**别的**网卡上：只报是谁占着，绝不去抢（与声明那条同一条边界）。"""
    called = _install(monkeypatch, adapters=[OURS, WLAN],
                      elsewhere=[Address("10.99.99.99", 24, 4, MANUAL)])
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    lines = startup.prepare(table, project=_project("10.99.99.99"))
    assert called.ips == [], "明知加不上还去提权"
    assert any("10.99.99.99" in line and "WLAN" in line for line in lines), lines


def test_local_hosts_without_a_declaration_stay_silent(tmp_path, monkeypatch):
    """`0.0.0.0` / `127.0.0.1` 这类本机地址：没有声明仍然一声不吭（口径 1 原样），
    而且**一个系统读都不发**——通配/回环本来就只听本机，不需要往网卡上加任何地址。
    """
    _forbid_system(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    for host in ("0.0.0.0", "127.0.0.1", "localhost", "::", ""):
        assert startup.prepare(table, project=_project(host)) == [], host


def test_apply_waits_until_the_new_address_is_bindable(monkeypatch):
    """`net up` 返回 ≠ 地址立刻能 bind（2026-10-19 现场：新 IP 第一次点启动失败、第二次才好）。

    这条钉住"应用之后要再等它变成可 bind"：前两次探针说不在、第三次才在，函数必须等满三次才回。
    """
    from simbench.net import cli as net_cli

    monkeypatch.setattr(net_cli, "main", lambda argv: 0)
    calls = {"n": 0}

    def flips(_host: str) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(startup, "_binds_locally", flips)
    code, lines = startup.apply_up_ips(["10.0.0.9"])
    assert code == 0
    assert calls["n"] >= 3, calls
    assert not any("还没生效" in line for line in lines), lines


def test_apply_says_so_when_the_address_never_becomes_bindable(monkeypatch):
    """一直等不到（驱动/系统不认）：**说出来**，别让操作员以为已经好了。"""
    from simbench.net import cli as net_cli

    monkeypatch.setattr(net_cli, "main", lambda argv: 0)
    monkeypatch.setattr(startup, "_binds_locally", lambda _host: False)
    code, lines = startup.apply_up_ips(["10.0.0.9"], wait=0.15)
    assert code == 0
    assert any("还没生效" in line for line in lines), lines


def test_the_switch_off_is_a_full_stop(tmp_path, monkeypatch):
    """总闸关掉时连系统状态都不读：这是"跑测试绝不动真网卡"那条保证的落点。"""
    monkeypatch.setenv(startup.AUTO_ENV, "0")
    _forbid_system(monkeypatch)
    assert startup.prepare(_declare(tmp_path)) == []


def test_an_empty_declaration_is_not_a_task(tmp_path, monkeypatch):
    _forbid_system(monkeypatch)
    assert startup.prepare(_declare(tmp_path, devices=())) == []


def test_a_broken_declaration_is_one_line_not_a_crash(tmp_path, monkeypatch):
    _forbid_system(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    (tmp_path / "t.net.json").write_text("{半截文件", encoding="utf-8")
    lines = startup.prepare(table)
    assert len(lines) == 1 and "网络声明有问题" in lines[0], lines


# ---------- 已经一致 ----------

def test_ready_network_is_reported_and_nothing_is_applied(tmp_path, monkeypatch):
    """机器上别的网卡有一堆地址是常态，不能因此判成"对不上"、更不能去提权。"""
    called = _install(monkeypatch, bindable=True,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      elsewhere=[Address("192.168.1.5", 24, 4, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"})
    lines = startup.prepare(_declare(tmp_path))
    assert lines == [f"网络已就绪：{OURS.label}（1 个地址）"], lines
    assert called.table == [], "已经一致还去动了系统"


def test_a_ready_network_can_be_previewed_without_touching_anything(tmp_path, monkeypatch):
    called = _install(monkeypatch, bindable=True,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"})
    assert startup.prepare(_declare(tmp_path), apply=False)[0].startswith("网络已就绪")
    assert called.table == []


# ---------- 该动系统的时候 ----------

def test_missing_addresses_go_through_net_up(tmp_path, monkeypatch):
    table = _declare(tmp_path)
    seen = {}
    _install(monkeypatch, ledger={OURS_GUID},
             apply=lambda given: seen.update(given=given)
             or (0, [f"已配 1 个地址到 {OURS.name}"]))
    lines = startup.prepare(table)
    assert seen["given"] == table, seen
    assert lines == [f"已配 1 个地址到 {OURS.name}"], lines


def test_missing_adapter_is_built_through_net_up(tmp_path, monkeypatch):
    called = _install(monkeypatch, adapters=[WLAN], apply=lambda table: (0, []))
    lines = startup.prepare(_declare(tmp_path))
    assert called.table, "没有专用网卡却没去建"
    assert lines == [f"专用网卡 {MANAGED_ADAPTER_NAME} 已就绪"], lines


def test_missing_loopback_driver_is_reported_without_elevating(tmp_path, monkeypatch):
    """没有 netloop.inf 就造不出网卡，先别弹 UAC：弹了也只是白弹一次。"""
    called = _install(monkeypatch, adapters=[WLAN], inf=False)
    lines = startup.prepare(_declare(tmp_path))
    assert called.table == [] and "netloop.inf" in lines[0], lines


def test_preview_says_exactly_what_to_run(tmp_path, monkeypatch):
    called = _install(monkeypatch, adapters=[WLAN])
    table = _declare(tmp_path)
    lines = startup.prepare(table, apply=False)
    assert called.table == []
    assert any("net up --point-table" in line and str(table) in line for line in lines), lines


def test_an_address_held_by_another_adapter_is_reported_not_fought_over(tmp_path, monkeypatch):
    """同一个 IP 不能挂两块网卡：加必然失败，弹 UAC 只换来一句英文错误。"""
    called = _install(monkeypatch, adapters=[OURS, WLAN],
                      elsewhere=[Address("10.0.0.1", 24, 4, MANUAL)])
    lines = startup.prepare(_declare(tmp_path))
    assert called.table == [], "明知加不上还去提权"
    assert any("10.0.0.1" in line and "WLAN" in line for line in lines), lines


def test_partly_blocked_still_applies_the_rest(tmp_path, monkeypatch):
    """.1 被别人占着，.2 还能加：不该因为一个冲突就整批放弃。"""
    seen = {}
    _install(monkeypatch, elsewhere=[Address("10.0.0.1", 24, 4, MANUAL)],
             apply=lambda table: seen.update(table=table) or (0, ["已配 1 个"]))
    table = _declare(tmp_path, devices=[{"name": "a", "ip": "10.0.0.1"},
                                        {"name": "b", "ip": "10.0.0.2"}])
    assert startup.prepare(table) == ["已配 1 个"]
    assert seen["table"] == table, "给 net up 的不是点表路径"


def test_a_pure_serial_project_is_never_given_interface_addresses(tmp_path, monkeypatch):
    """纯串口工程不按声明配设备 IP（2026-10-13 全面审查定的口径）。

    `endpoints.for_channel` 早就对 RTU 回空、不额外挂监听；但**自动配网**这一侧原先只看
    声明文件，于是"一份纯串口的点表 + 旁边躺着一份旧 `.net.json`"会把地址配到网卡上——
    那些地址 ping 得通、上面却没有任何从站，正是本项目最防的"看着在、其实没有"。
    """
    from simbench.project.model import Device, Point, Project, SerialChannel, TcpChannel

    seen = {}
    _install(monkeypatch, adapters=[WLAN],
             apply=lambda table: seen.update(table=table) or (0, ["已配 1 个"]))
    table = _declare(tmp_path)

    serial = Project(channels=[SerialChannel("com-485-1", port="COM3", devices=[
        Device("风机-1", 11, [Point("x", "hr", 0, "uint16", initial="1")])])])
    assert startup.prepare(table, project=serial) == []
    assert seen == {}, "纯串口工程去动网卡了"

    # 对照：同一个设备名挂到 TCP 通道上时照旧要配——证明上面那条不是"什么都没测到"
    tcp = Project(channels=[TcpChannel("tcp-1", host="0.0.0.0", port=5020, devices=[
        Device("风机-1", 11, [Point("x", "hr", 0, "uint16", initial="1")])])])
    assert startup.prepare(table, project=tcp) == ["已配 1 个"]


# ---------- 失败口径 ----------

def test_a_failed_apply_is_one_line_and_never_raises(tmp_path, monkeypatch):
    def boom(table):
        raise NetError("提权被取消（可能是 UAC 被关掉了）")
    _install(monkeypatch, adapters=[WLAN], apply=boom)
    lines = startup.prepare(_declare(tmp_path))
    assert len(lines) == 1 and "提权被取消" in lines[0] and "照常启动" in lines[0], lines


def test_an_unexpected_failure_is_swallowed_with_a_reason(tmp_path, monkeypatch):
    """挂钩是全局唯一一处兜到 Exception 的地方：一个没预料到的异常不许放倒整个工具。"""
    def boom(table):
        raise RuntimeError("powershell 被策略挡掉了")
    _install(monkeypatch, adapters=[WLAN], apply=boom)
    lines = startup.prepare(_declare(tmp_path))
    assert len(lines) == 1 and "出了意外" in lines[0] and "照常启动" in lines[0], lines


def test_a_partial_apply_points_at_the_next_command(tmp_path, monkeypatch):
    _install(monkeypatch, adapters=[WLAN],
             apply=lambda table: (2, ["已配 1 个地址到 X",
                                      "失败 1 个：10.0.0.1（已存在）"]))
    lines = startup.prepare(_declare(tmp_path))
    assert "失败" in "".join(lines) and any("net show" in line for line in lines), lines


# ---------- 同一条命令：`net up` 的调用形状 ----------

def test_apply_up_reuses_the_command_line(monkeypatch, tmp_path):
    """挂钩不许自己再写一份"建网卡 + 加地址"：进程内调同一条子命令。"""
    from simbench.net import cli as net_cli
    seen = {}
    monkeypatch.setattr(net_cli, "main",
                        lambda argv: seen.update(argv=argv) or 0)
    code, lines = startup.apply_up(tmp_path / "t.csv")
    assert code == 0
    assert seen["argv"] == ["up", "--point-table", str(tmp_path / "t.csv")], seen
    assert lines == [], lines


def test_apply_up_strips_the_command_prefix(monkeypatch):
    """回传的行不再带 `[net] `：调用方自己打前缀，否则现场看到两层。"""
    from simbench.net import cli as net_cli
    monkeypatch.setattr(net_cli, "main",
                        lambda argv: print("[net] 已一致，无需改动") or 0)
    assert startup.apply_up("t.csv")[1] == ["已一致，无需改动"]


# ---------- 两条启动路径的接线 ----------

def test_serve_path_prepares_the_network_before_listening(monkeypatch, capsys):
    import simbench.__main__ as cli
    calls = []
    monkeypatch.setattr(startup, "prepare",
                        lambda table, **kw: calls.append(table) or ["已就绪"])

    def fake_run(coro):
        coro.close()                                  # 不真起监听，也不留"没 await"的警告
    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    assert cli.main([CSV, "--quiet"]) == 0
    assert calls == [CSV], calls
    assert "[net] 已就绪" in capsys.readouterr().err


def test_read_only_commands_never_touch_the_network(monkeypatch):
    """`--check`/`--list` 是只读命令：它们的语义就是"绝不改系统"，网络这一步也一样。"""
    import simbench.__main__ as cli
    calls = []
    monkeypatch.setattr(startup, "prepare",
                        lambda table, **kw: calls.append(table) or [])
    assert cli.main([CSV, "--check"]) == 0
    assert cli.main([CSV, "--list"]) == 0
    assert calls == []


def test_no_net_flag_turns_the_hook_off(monkeypatch, capsys):
    """`--no-net` 落在总闸上而不是参数往后传：GUI 那条路不经过 argparse，两边读同一个变量。

    这里**不换** `prepare`：闸门关掉之后真的调它，也一条系统读都不该发生。
    """
    import simbench.__main__ as cli

    def fake_run(coro):
        coro.close()
    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    assert startup.auto_enabled(), "前置条件：总闸本来是开的"
    assert cli.main([CSV, "--quiet", "--no-net"]) == 0
    assert not startup.auto_enabled(), "`--no-net` 没落到总闸上"
    assert "[net]" not in capsys.readouterr().err


def test_gui_entry_shows_the_network_line_in_the_status_bar(qapp, monkeypatch):
    from simbench.gui.app import build_app
    monkeypatch.setattr(startup, "prepare",
                        lambda table, **kw: [f"网络已就绪：{MANAGED_ADAPTER_NAME}（2 个地址）"])
    _app, window, session = build_app([CSV])
    try:
        assert window.status.currentMessage() == \
            f"网络已就绪：{MANAGED_ADAPTER_NAME}（2 个地址）"
    finally:
        window.timer.stop()
        window.deleteLater()
        session.shutdown()



# ---------- 2026-10-19 第二批修复 ----------

def test_a_declaration_does_not_hide_a_changed_concrete_host(tmp_path, monkeypatch):
    """有 `.net.json` 时，通道 host 改了也必须参与配网。

    声明是设备地址的来源，但通道 host 是模拟器真正的监听地址；两条来源取并集，
    否则界面上改了 host、声明还是旧 IP，启动必然 bind 失败。
    """
    from simbench.project.model import Device, Point

    table = _declare(tmp_path)
    project = Project(channels=[TcpChannel(
        "tcp-1", host="10.0.0.2", port=5020,
        devices=[Device("风机-1", 11, [Point("x", "hr", 0, "uint16", initial="1")])])])
    called = _install(monkeypatch, ledger={OURS_GUID},
                      apply=lambda table: (0, ["已配 2 个地址"]))
    lines = startup.prepare(table, project=project)
    assert called.table == [table], called.table
    assert called.extra == [["10.0.0.2"]], called.extra
    assert any("通道 host 另需" in line for line in lines), lines


def test_udp_and_s7_concrete_hosts_are_not_ignored():
    """所有有 host:port 的传输都要收集，不只是 TCP。"""
    from simbench.project.model import S7Channel

    project = Project(channels=[
        TcpChannel("udp-1", host="10.0.0.10", port=5020, type="udp"),
        S7Channel("s7-1", host="10.0.0.11", port=102),
        TcpChannel("tcp-1", host="0.0.0.0", port=5020),
    ])
    hosts = {(name, host) for name, host, _port, _transport in startup._concrete_hosts(project)}
    assert hosts == {("udp-1", "10.0.0.10"), ("s7-1", "10.0.0.11")}, hosts
    assert startup.required_addresses(None, project) == ["10.0.0.10", "10.0.0.11"]


def test_a_hostname_is_reported_not_sent_to_net_up(tmp_path, monkeypatch):
    """validate 允许主机名，但自动配网只认 IPv4；不能拿主机名去弹 UAC。"""
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    called = _install(monkeypatch)
    lines = startup.prepare(table, project=_project("plc.local"))
    assert called.table == [] and called.ips == [], (called.table, called.ips)
    assert any("不是 IPv4 字面量" in line for line in lines), lines


def test_repeated_prepare_uses_the_verified_fast_path(tmp_path, monkeypatch):
    """一次完整核对成功后，重复点启动不再跑 PowerShell（只留便宜的 bind 探针）。"""
    called = _install(monkeypatch, bindable=True,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"})
    table = _declare(tmp_path)
    first = startup.prepare(table)
    assert first == [f"网络已就绪：{OURS.label}（1 个地址）"], first

    def boom():
        raise AssertionError("已核对过的地址不该再读系统")
    monkeypatch.setattr(startup, "read_adapters", boom)
    monkeypatch.setattr(startup, "read_all_addresses", boom)
    second = startup.prepare(table)
    assert second == [f"网络已就绪：{OURS.label}（1 个地址）"], second
    assert called.table == []


def test_clean_but_unbindable_is_not_reported_ready(tmp_path, monkeypatch):
    """地址在 Get-NetIPAddress 里、但还不能 bind：不许报"已就绪"然后让启动失败。"""
    called = _install(monkeypatch, bindable=False,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"})
    lines = startup.prepare(_declare(tmp_path))
    joined = "\n".join(lines)
    assert "网络尚未就绪" in joined and "当前不能监听" in joined, lines
    assert called.table == [], "地址已经齐了，不该再提权"


def test_required_addresses_is_the_union_of_declaration_and_hosts(tmp_path):
    from simbench.project.model import Device, Point

    table = _declare(tmp_path)
    project = Project(channels=[TcpChannel(
        "tcp-1", host="10.0.0.2", port=5020,
        devices=[Device("风机-1", 11, [Point("x", "hr", 0, "uint16", initial="1")])])])
    assert startup.required_addresses(table, project) == ["10.0.0.1", "10.0.0.2"]


def test_host_only_apply_keeps_stale_addresses(monkeypatch):
    """`apply_up_ips` 必须带 `--keep-stale`：通道 host 不能收回别人的地址。"""
    from simbench.net import cli as net_cli

    seen = {}
    monkeypatch.setattr(net_cli, "main", lambda argv: seen.update(argv=argv) or 0)
    startup.apply_up_ips(["10.0.0.9"], wait=0)
    assert seen["argv"] == ["up", "--ip", "10.0.0.9", "--keep-stale"], seen

# ---------- `net device create` 的提权：两段式子命令 ----------

def test_device_create_relaunches_with_both_subcommand_words(monkeypatch, tmp_path):
    """提权子进程的命令行必须是 `net device create …`。

    只带 `device` 会让子进程 argparse 认不出动作；提权重跑的是一句话拼出来的命令行，
    所以这条必须原样带过去。回归的那次形状是"`_run_elevated` 少传参数"——直接 TypeError。
    """
    from simbench.net import cli as net_cli
    result = tmp_path / "result.json"
    launched = {}

    def fake_powershell(argv, capture_output, timeout, **kwargs):
        launched["argv"] = argv
        launched["kwargs"] = kwargs
        result.write_text(json.dumps({"ok": True, "message": "已创建"}),
                          encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(net_cli, "is_elevated", lambda: False)
    # 提权编排住在 `net/elevate.py`（O10）：拦的是**它**的 subprocess
    from simbench.net import elevate
    monkeypatch.setattr(elevate.subprocess, "run", fake_powershell)
    code = net_cli.main(["device", "create", "--ledger", str(tmp_path / "none.json"),
                         "--result", str(result)])
    assert code == 0
    script = launched["argv"][-1]
    assert "'device','create'" in script, script
    assert "'--elevated'" in script, script


def test_device_create_replays_its_job_file(monkeypatch, tmp_path):
    """提权子进程只拿一个任务文件路径，参数全从文件里读回来（绕开命令行里的空格/引号）。"""
    from simbench.net import cli as net_cli
    job = tmp_path / "job.json"
    result = tmp_path / "result.json"
    job.write_text(json.dumps({"cmd": "device", "action": "create",
                               "adapter_name": MANAGED_ADAPTER_NAME,
                               "device_instance": "",
                               "ledger": str(tmp_path / "none.json"),
                               "result": str(result), "log": None}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(net_cli, "_create_and_record",
                        lambda name, ledger, path: seen.update(name=name)
                        or {"ok": True, "message": f"已创建 {name}"})
    assert net_cli.main(["device", "create", "--elevated", "--job", str(job)]) == 0
    assert seen["name"] == MANAGED_ADAPTER_NAME, seen
    assert json.loads(result.read_text(encoding="utf-8"))["message"] == \
        f"已创建 {MANAGED_ADAPTER_NAME}"


# ---------- `check` 也能吃声明文件 ----------

def test_check_reads_the_companion_declaration(tmp_path, monkeypatch, capsys):
    """`check` 与 `up` 共用一个地址来源：校验的那份清单必须就是将要应用的那份。"""
    from simbench.net import cli as net_cli
    monkeypatch.setattr(net_cli, "read_adapters", lambda: [OURS])
    monkeypatch.setattr(net_cli, "read_addresses", lambda if_index: [])
    table = _declare(tmp_path, devices=[{"name": "风机-1", "ip": "10.0.0.9"}])
    code = net_cli.main(["check", "--adapter", OURS_GUID, "--point-table", str(table),
                         "--ledger", str(tmp_path / "none.json")])
    assert code == 2, "有差异时 check 该回 2（脚本里当「需要处理」用）"
    assert "10.0.0.9" in capsys.readouterr().out

def test_a_disabled_managed_adapter_is_still_sent_through_net_up(tmp_path, monkeypatch):
    """地址齐了但网卡是 Disabled：仍要跑一次 net up（它会先启用专用网卡）。"""
    disabled = Adapter(name=MANAGED_ADAPTER_NAME, description="Microsoft KM-TEST 环回适配器",
                       if_index=20, guid=OURS_GUID, status="Disabled", mac="",
                       pnp_device_id=r"ROOT\NET\0001")
    called = _install(monkeypatch, adapters=[disabled], bindable=False,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"},
                      apply=lambda table: (0, ["已启用专用网卡"]))
    lines = startup.prepare(_declare(tmp_path))
    assert called.table, "网卡禁用时没有走 net up"
    assert any("已启用专用网卡" in line for line in lines), lines

def test_status_previews_a_host_only_project(tmp_path, monkeypatch):
    """没有声明但通道 host 是具体 IP：网络页也要能看到差异，而不是只说"没有声明"。"""
    _install(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    lines = startup.status(table, project=_project("10.99.99.99"))
    joined = "\n".join(lines)
    assert "按通道 host" in joined and "10.99.99.99" in joined, lines

def test_a_declaration_without_matching_devices_does_not_hide_the_host(tmp_path, monkeypatch):
    """声明在、但其中没有挂到 socket 通道的设备：只配通道 host，不把整份声明再灌一遍。"""
    called = _install(monkeypatch)
    project = Project(channels=[TcpChannel("tcp-1", host="10.0.0.9", port=5020)])
    lines = startup.prepare(_declare(tmp_path), project=project)
    assert called.ips == [["10.0.0.9"]], called.ips
    assert called.table == [], called.table
    assert any("10.0.0.9" in line for line in lines), lines

def test_a_partial_apply_still_waits_for_the_addresses(monkeypatch):
    """`net up` 回 2（部分成功）时也不能跳过 bind 等待：已经加上的地址同样要等它生效。"""
    from simbench.net import cli as net_cli

    monkeypatch.setattr(net_cli, "main", lambda argv: print("[net] 已配 1 个") or 2)
    calls = {"n": 0}

    def flips(_host):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(startup, "_binds_locally", flips)
    code, lines = startup.apply_up_ips(["10.0.0.9"], wait=0.5)
    assert code == 2
    assert calls["n"] >= 2, calls
    assert not any("还没生效" in line for line in lines), lines


def test_gui_entry_shows_the_window_before_network_prepare(qapp, monkeypatch):
    """双击带点表的工程时先把窗口显示出来，再跑 UAC/PowerShell。"""
    from simbench.gui import app as app_module

    order = []
    real_show = app_module.MainWindow.show

    def show(self):
        order.append("show")
        return real_show(self)

    monkeypatch.setattr(app_module.MainWindow, "show", show)
    monkeypatch.setattr(startup, "prepare", lambda *a, **k: order.append("net") or [])
    _app, window, session = app_module.build_app([CSV])
    try:
        assert order[:2] == ["show", "net"], order
    finally:
        window.timer.stop()
        window.deleteLater()
        session.shutdown()

def test_net_run_does_not_swallow_other_threads_output(monkeypatch, capsys):
    """回归：`_run_net` 只能截获本线程的输出，不能像 redirect_stdout 那样吞掉别的线程。"""
    import threading

    from simbench.net import cli

    other_done = threading.Event()
    release = threading.Event()

    def fake_main(argv):
        print("[net] from-worker")
        # 在 net 子命令还没结束时，另一个线程照常往 stdout 写
        def other():
            print("OTHER-THREAD-LINE", flush=True)
            other_done.set()
        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        release.set()
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    code, lines = startup._run_net(["show"])
    assert code == 0
    assert lines == ["from-worker"]
    assert other_done.is_set()
    # 另一个线程的输出必须落到真正的 stdout，而不是被吞进 net 的缓冲
    assert "OTHER-THREAD-LINE" in capsys.readouterr().out
    # 结束之后本线程也恢复直通
    print("AFTER")
    assert "AFTER" in capsys.readouterr().out
