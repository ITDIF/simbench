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


def free_port() -> int:
    """挑一个当前空闲的端口。`port=0` 不支持（Task 5 的 validate 已拦），测试自己占位。

    bind(0) 拿到号就立刻 close，所以理论上存在"被别人抢先占用"的窗口；真撞上了表现为
    `start()` 抛 `RuntimeError: Could not start listen`（见 modbus_tcp 的 start），
    是一条能读懂的失败，不是"端口没起来但测试继续跑"。
    """
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def open_backend():
    """`with open_backend(channel, packet_sink=..., on_connection=...) as backend:`。

    loop 由 `EngineRunner` 持有（生产同一个类），协程一律经 `submit(工厂)` 投回引擎线程。
    `packet_sink`/`on_connection` 仍然直接交给 backend：它们在 **loop 线程**被调用，与 M1
    完全一致——不绕 `runner.publish()` 的 pump 线程，否则用例里"发完报文立刻断言"的时序
    会多一次跨线程交接，M1 那 27 条真主站用例钉的就是 backend 直调的形状。

    `finally` 里先停 backend 再停 runner：顺序反了的话 `shutdown()` 协程没机会跑完，
    监听套接字还挂在 loop 上，下一个用例的 `free_port()` 就会撞车。backend 还没建出来
    （start 自己就炸）时也必须把线程收掉，所以 `runner.stop()` 单独占一层 `finally`。
    runner 起两个线程（`modslave-engine` loop + `-events` pump），`stop()` 两个都 join，
    所以退出 with 之后端口能重新 bind、`threading.enumerate()` 不残留。
    """
    @contextlib.contextmanager
    def _open(channel, packet_sink=None, on_connection=None):
        runner = EngineRunner()
        runner.start()
        backend = None
        try:
            backend = ModbusTcpBackend(channel, packet_sink=packet_sink,
                                       on_connection=on_connection)
            runner.submit(lambda: backend.start()).result(_CALL_TIMEOUT)
            yield backend
        finally:
            try:
                if backend is not None:
                    runner.submit(lambda: backend.stop()).result(_CALL_TIMEOUT)
            finally:
                runner.stop()

    yield _open
