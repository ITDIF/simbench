"""起停后端的事件循环线程夹具 + offscreen Qt 夹具。

调用形态与后面 GUI 计划一致：asyncio loop 跑在专用线程，测试主体（真主站 client）在主线程，
所以 backend 的 start/stop 必须投回 loop 线程执行。M2 起这个线程由生产件
`modslave.engine.runner.EngineRunner` 持有——测试不再养一份私有实现，跑的就是生产那条路。

Qt 这边只需要一个会话级的 `offscreen` QApplication：实测该平台下 `show()` +
`processEvents()` 之后控件几何是真的（900 宽、工具栏 27 高），所以"状态变化不许挪布局"
能写成断言，不需要真显示器，也不需要 pytest-qt。
"""

from __future__ import annotations

import contextlib
import os
import socket

import pytest

# 必须在任何 PySide6 import 之前落定：Qt 平台插件在第一次 import 时就绑死了，
# 之后再改这个变量只会留下一个已经起在 windows 平台上的 QApplication（CI/无头机器上直接崩）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from modslave.backend.modbus_tcp import ModbusTcpBackend
from modslave.engine.runner import EngineRunner

# 与 M1 的 _LoopThread.call 同一个上限：起停 backend 是毫秒级的事，10 秒不回就是坏了
_CALL_TIMEOUT = 10.0


@pytest.fixture(scope="session")
def qapp():
    """offscreen 模式的 QApplication。实测 show()+processEvents() 后几何是真的。"""
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    # 不 quit：QApplication 退出后无法再建，同一次会话里还要给后面的用例用


_handed_out: set[int] = set()


def free_port() -> int:
    """挑一个本会话内**没发过**的空闲端口。

    只 bind(0) 再 close 是不够的：Windows 会把刚释放的临时端口立刻再发一次，于是
    A 用例的从站还在监听 P，B 用例又拿到 P —— 第二个 backend 的 connect 探针如实报
    "有人在服务"，测试随机红（本机实测三条不同用例轮着失败过）。生产上端口是写死的
    5020 之类，不会撞上这个形状，所以修在测试侧，不动产品判定。
    """
    for _ in range(500):
        with contextlib.closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _handed_out:
            _handed_out.add(port)
            return port
    raise RuntimeError("could not find a fresh free port for this session")


_leaked = []


@pytest.fixture(autouse=True)
def _no_listener_leak(request):
    """每个用例结束就检查：它拿过的端口不许还在应答。

    上一个用例的后端没关干净，下一个用例的 client 就会连到它身上 —— 表现是"跨用例串扰"
    而不是"泄漏"，最难查。这里直接把泄漏者点名。
    """
    before = set(_handed_out)
    del _leaked[:]                    # 只报本条用例造成的泄漏，否则第一个之后条条都红
    yield
    for port in sorted(set(_handed_out) - before):
        with contextlib.closing(socket.socket()) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                _leaked.append((request.node.nodeid, port))
    assert not _leaked, f"监听没关干净: {_leaked}"



@pytest.fixture(scope="session")
def engine_runner():
    """整个测试会话共用一个引擎线程（loop + pump）。

    每用例各起一个 asyncio loop 时，同进程能攒到 24 个：它们互相饿死，表现为偶发的
    `.result(10)` 超时与 `stop()` join 超时（不是 backend 坏了）。共用之后全量从 45s
    降到 21s 且连跑不红。被停掉的 backend 会把它的 listener 协程留在共享 loop 上，
    这正是我们要测的东西留在内存里，不影响端口。
    """
    runner = EngineRunner(name="modslave-engine")
    runner.start()
    try:
        yield runner
    finally:
        runner.stop()


@pytest.fixture
def open_backend(engine_runner):
    """`with open_backend(channel, packet_sink=..., on_connection=...) as backend:`。

    loop 由 `EngineRunner` 持有（生产同一个类），协程一律经 `submit(工厂)` 投回引擎线程。
    `packet_sink`/`on_connection` 仍然直接交给 backend：它们在 **loop 线程**被调用，与 M1
    完全一致——不绕 `runner.publish()` 的 pump 线程，否则用例里"发完报文立刻断言"的时序
    会多一次跨线程交接，M1 那批真主站用例钉的就是 backend 直调的形状。

    `finally` 里只停 backend，不停 runner（runner 是会话级的）；backend 没建出来也要把
    已经起到的 listener 收干净，所以 `backend.stop()` 单独包一层。
    """
    @contextlib.contextmanager
    def _open(channel, packet_sink=None, on_connection=None):
        backend = ModbusTcpBackend(channel, packet_sink=packet_sink,
                                   on_connection=on_connection)
        try:
            engine_runner.submit(lambda: backend.start()).result(_CALL_TIMEOUT)
            yield backend
        finally:
            engine_runner.submit(lambda: backend.stop()).result(_CALL_TIMEOUT)

    yield _open
