"""起停后端的事件循环线程夹具。

调用形态与后面 GUI 计划一致：asyncio loop 跑在专用线程，测试主体（真主站 client）在主线程，
所以 backend 的 start/stop 必须经 `run_coroutine_threadsafe` 投回 loop 线程执行。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading

import pytest

from modslave.backend.modbus_tcp import ModbusTcpBackend


def free_port() -> int:
    """挑一个当前空闲的端口。`port=0` 不支持（Task 5 的 validate 已拦），测试自己占位。

    bind(0) 拿到号就立刻 close，所以理论上存在"被别人抢先占用"的窗口；真撞上了表现为
    `start()` 抛 `RuntimeError: Could not start listen`（见 modbus_tcp 的 start），
    是一条能读懂的失败，不是"端口没起来但测试继续跑"。
    """
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _LoopThread:
    """把 loop 起在专用线程里，`call()` 投协程并同步等结果。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        """关停必须是"三步都有"：停 loop、join、close。

        少一步就会留下活着的线程或没释放的 socket：本文件的 `free_port()` 靠端口空闲来
        挑端口，泄漏一个监听套接字会让后面的用例偶发红（同端口撞车）。
        """
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


@pytest.fixture
def open_backend():
    """`with open_backend(channel, packet_sink=..., on_connection=...) as backend:`。

    `finally` 里先停 backend 再停 loop：顺序反了的话 `shutdown()` 协程没机会跑完，
    监听套接字还挂在 loop 上。backend 还没建出来（start 自己就炸）时也必须把线程收掉，
    所以 `worker.stop()` 单独占一层 `finally`。
    """
    @contextlib.contextmanager
    def _open(channel, packet_sink=None, on_connection=None):
        worker = _LoopThread()
        worker.start()
        backend = None
        try:
            backend = ModbusTcpBackend(channel, packet_sink=packet_sink,
                                       on_connection=on_connection)
            worker.call(backend.start())
            yield backend
        finally:
            try:
                if backend is not None:
                    worker.call(backend.stop())
            finally:
                worker.stop()

    yield _open
