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
             ledger=(), owned=(), inf=True, apply=None):
    """把系统那几处读口换成假数据，并记下 `net up` 被怎么调的。"""
    monkeypatch.setattr(startup, "read_adapters", lambda: list(adapters))
    monkeypatch.setattr(startup, "read_addresses",
                        lambda if_index: [a for a in addresses if a.if_index == if_index])
    monkeypatch.setattr(startup, "read_all_addresses",
                        lambda: list(addresses) + list(elsewhere))
    monkeypatch.setattr(startup, "Ledger", SimpleNamespace(load=lambda path=None: SimpleNamespace(
        owned_adapter_guids=lambda: set(ledger), owned=lambda guid: set(owned))))
    monkeypatch.setattr(startup, "inf_available", lambda: inf)
    monkeypatch.setattr(startup, "loopback_inf_path",
                        lambda: pathlib.Path(r"C:\Windows\INF\netloop.inf"))
    calls = []

    def record(table):
        calls.append(table)
        if apply is not None:
            return apply(table)
        return 0, ([f"已配 {len(ledger)} 个地址"] if ledger else ["已一致，无需改动"])

    monkeypatch.setattr(startup, "apply_up", record)
    return calls


def _forbid_system(monkeypatch):
    """任何一次系统读都当场炸：用来证明"这条路真的没碰机器"。"""
    def boom(*args, **kwargs):
        raise AssertionError("这一步不许碰系统")
    for name in ("read_adapters", "read_addresses", "read_all_addresses"):
        monkeypatch.setattr(startup, name, boom)


# ---------- 什么时候闭嘴 ----------

def test_no_declaration_is_silent(tmp_path, monkeypatch):
    """没有 `.net.json` 就是"这个工程不关心网络"，不是错误，也不该在每次启动时念一遍。"""
    _forbid_system(monkeypatch)
    table = tmp_path / "t.csv"
    table.write_text("x", encoding="utf-8")
    assert startup.prepare(table) == []


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
    called = _install(monkeypatch,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      elsewhere=[Address("192.168.1.5", 24, 4, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"})
    lines = startup.prepare(_declare(tmp_path))
    assert lines == [f"网络已就绪：{OURS.label}（1 个地址）"], lines
    assert called == [], "已经一致还去动了系统"


def test_a_ready_network_can_be_previewed_without_touching_anything(tmp_path, monkeypatch):
    called = _install(monkeypatch,
                      addresses=[Address("10.0.0.1", 24, 20, MANUAL)],
                      ledger={OURS_GUID}, owned={"10.0.0.1"})
    assert startup.prepare(_declare(tmp_path), apply=False)[0].startswith("网络已就绪")
    assert called == []


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
    assert called, "没有专用网卡却没去建"
    assert lines == [f"专用网卡 {MANAGED_ADAPTER_NAME} 已就绪"], lines


def test_missing_loopback_driver_is_reported_without_elevating(tmp_path, monkeypatch):
    """没有 netloop.inf 就造不出网卡，先别弹 UAC：弹了也只是白弹一次。"""
    called = _install(monkeypatch, adapters=[WLAN], inf=False)
    lines = startup.prepare(_declare(tmp_path))
    assert called == [] and "netloop.inf" in lines[0], lines


def test_preview_says_exactly_what_to_run(tmp_path, monkeypatch):
    called = _install(monkeypatch, adapters=[WLAN])
    table = _declare(tmp_path)
    lines = startup.prepare(table, apply=False)
    assert called == []
    assert any("net up --point-table" in line and str(table) in line for line in lines), lines


def test_an_address_held_by_another_adapter_is_reported_not_fought_over(tmp_path, monkeypatch):
    """同一个 IP 不能挂两块网卡：加必然失败，弹 UAC 只换来一句英文错误。"""
    called = _install(monkeypatch, adapters=[OURS, WLAN],
                      elsewhere=[Address("10.0.0.1", 24, 4, MANUAL)])
    lines = startup.prepare(_declare(tmp_path))
    assert called == [], "明知加不上还去提权"
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


# ---------- `net device create` 的提权：两段式子命令 ----------

def test_device_create_relaunches_with_both_subcommand_words(monkeypatch, tmp_path):
    """提权子进程的命令行必须是 `net device create …`。

    只带 `device` 会让子进程 argparse 认不出动作；提权重跑的是一句话拼出来的命令行，
    所以这条必须原样带过去。回归的那次形状是"`_run_elevated` 少传参数"——直接 TypeError。
    """
    from simbench.net import cli as net_cli
    result = tmp_path / "result.json"
    launched = {}

    def fake_powershell(argv, capture_output, timeout):
        launched["argv"] = argv
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
