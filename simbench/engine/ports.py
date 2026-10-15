"""端口探针：界面用的一层薄封装。

方向是 `gui → engine → backend`（`tests/test_architecture.py` 守的那条线）：界面不许直接
import `backend`，所以"这个端口现在有没有人占着"这件事由这一层转出去，界面只认这个函数。

用来干什么：新建工程 / 添加通道时给默认端口（TCP 从 5020 起、S7 是 102）。`edit.next_port`
只避开**工程内**的冲突，不探系统——这台机器上 5020 被别人占着时，第一次"全部启动"就是一条
"端口被占"，而那本来可以在选端口的时候就知道。
"""

from __future__ import annotations

from ..backend import check_port_is_free


def is_free(port: int, host: str = "0.0.0.0") -> bool:
    """这个地址+端口现在绑得上吗（绑得上 = 空闲）。

    参数顺序是"端口在前"：调用方（`project.edit.next_port` 的 `probe`）只关心端口，主机由
    它固定给 `0.0.0.0`（两条传输的默认监听地址都是它）。

    探针自己出错（主机名解析不了之类）当**被占**处理：这个方向的误判只是让默认端口顺延一位，
    另一个方向的误判是"默认就选了一个起不来的端口"——那正是要避免的。探针是只读的
    （bind + connect 两级，见 `check_port_is_free`），不改任何系统状态。
    """
    try:
        check_port_is_free(host, port, "port probe")
    except Exception:                                # noqa: BLE001
        return False
    return True
