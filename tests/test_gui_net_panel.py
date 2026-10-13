"""网络页 `gui/net_panel.py`：现状 + 差异 + 一键应用（提权）。

钉住四件事：

1. **刷新只读**：走 `startup.status()`，"点一下看看"不许改系统；
2. **应用走 `net up` 那条路**，结论一句话落状态栏（页没被打开时也得看得见）；
3. **没有工程路径时拒绝**：没有点表就不知道按哪份声明配，拒绝要说清；
4. **切到网络页才读系统**：读一次网卡现状是几次 PowerShell（秒级），"打开点表"不许跟着等。

本文件把 `net/` 那一层的两个入口都换成记录器，所以一条系统调用、一条真网络连接都不做。
"""

from __future__ import annotations

import pytest

from simbench.engine.packets import PacketStore
from simbench.engine.session import Session
from simbench.gui import net_panel as net_panel_module
from simbench.gui.main_window import MainWindow
from simbench.gui.net_panel import NetPanel


@pytest.fixture
def net(monkeypatch):
    """替掉 `net/startup.py` 的两个入口，并记下被谁以什么参数调过。"""
    calls = {"status": [], "apply": []}
    monkeypatch.setattr(net_panel_module.net_startup, "status",
                        lambda source, project: calls["status"].append(source) or ["（桩）"])
    monkeypatch.setattr(net_panel_module.net_startup, "apply_up",
                        lambda source: calls["apply"].append(source) or (0, ["已应用"]))
    return calls


@pytest.fixture
def panel(qapp, net):
    session = Session()
    widget = NetPanel(session)
    yield widget
    widget.deleteLater()
    session.shutdown()


def test_refresh_shows_what_the_net_layer_says(monkeypatch, net, panel):
    monkeypatch.setattr(net_panel_module.net_startup, "status",
                        lambda source, project: ["声明：x.net.json", "网络已就绪：X（2 个地址）"])
    panel.refresh()
    assert panel.view.toPlainText() == "声明：x.net.json\n网络已就绪：X（2 个地址）"


def test_apply_refuses_without_a_project_path(net, panel):
    said = []
    panel.notice.connect(said.append)
    panel.apply()
    assert net["apply"] == [], "没有路径却去应用了"
    assert said and "没有工程路径" in said[0], said


def test_apply_runs_net_up_and_tells_the_status_bar(net, panel):
    net["apply"] = []                      # 上面那个桩返回固定值，这里换成"报一件事"
    panel.session.load(panel.session.project, source="examples/covi-tunnel.csv")
    said = []
    panel.notice.connect(said.append)
    panel.apply()
    assert net["apply"] == ["examples/covi-tunnel.csv"]
    assert said[-1] == "已应用", said
    assert panel.view.toPlainText() == "（桩）", "应用之后没按新现状刷新"


def test_apply_reports_a_quiet_success(monkeypatch, net, panel):
    monkeypatch.setattr(net_panel_module.net_startup, "apply_up", lambda source: (0, []))
    panel.session.load(panel.session.project, source="p.csv")
    said = []
    panel.notice.connect(said.append)
    panel.apply()
    assert said[-1] == "网络已一致，无需改动", said


def test_the_network_tab_reads_the_system_only_when_opened(qapp, engine_runner, net):
    session = Session(runner=engine_runner)
    window = MainWindow(session, PacketStore())
    try:
        assert net["status"] == [], "开窗就把系统读了一遍"
        window.tabs.setCurrentWidget(window.net_panel)
        assert len(net["status"]) == 1
        window.tabs.setCurrentWidget(window.log_view)
        assert len(net["status"]) == 1, "切到别的页也去读了系统"
    finally:
        window.timer.stop()
        session.shutdown()
        window.deleteLater()
