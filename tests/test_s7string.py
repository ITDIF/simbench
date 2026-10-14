"""真 S7 STRING（`s7string`）：长度头 + 字符，主站按 STRING 读得到。

为什么要这一种类型（2026-10-14）：点表里原来的 `string` 是**裸 ASCII 定长**（没有长度头），
而组态软件读 STRING 时是**按长度头**读的（`DB1,S0.10` = 最大 10）。两边对不上时现场看到的
是"字符串读出来是空的/多了一串乱码"，却查不出为什么。两种并存：点表里写 `string` 就是裸的，
写 `s7string` 就是真 STRING。

三条判据（都在这里钉住）：

1. 编码形状：`[最大长度, 当前长度] + 字符 + 补零`；
2. 真主站 `pyS7` 用 `DB1,S0.2` 读得到、写回来也生效（我们这一侧的归属跟着切成 manual）；
3. 长度头撒谎（当前长度 > 缓冲里真有的字节）时报错，**不猜一个长度截断**。
"""

from __future__ import annotations

import socket
import time

import pytest
from pyS7 import S7Client

from simbench.backend import make_backend
from simbench.backend.s7_server import S7Backend
from simbench.codec import s7_codec
from simbench.project.model import S7_TYPES, Device, Point, Project, S7Channel, s7_span_bytes
from simbench.project.validate import error_lines, validate_project

_CALL_TIMEOUT = 10.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ---------- 形状与跨度 ----------

def test_the_type_is_registered_as_an_s7_type():
    assert "s7string" in S7_TYPES


def test_the_span_is_the_header_plus_the_max_length_from_the_initial():
    """跨度 = 2 + 最大长度，最大长度**取点表初值的长度**（与裸 string 同一条口径）。"""
    assert s7_span_bytes("s7string", "AB") == 4
    assert s7_span_bytes("s7string", "ABCDEFGHIJ") == 12


def test_the_encoding_carries_both_lengths():
    raw = s7_codec.encode("AB", "s7string", 12)
    assert raw == bytes([10, 2]) + b"AB" + b"\x00" * 8
    assert s7_codec.decode(raw, "s7string", 12) == "AB"


def test_a_value_longer_than_the_field_is_refused():
    with pytest.raises(ValueError, match="at most"):
        s7_codec.encode("ABCD", "s7string", 4)          # 最大长度只有 2


def test_a_lying_header_is_refused_instead_of_guessed():
    """当前长度比缓冲里真有的字节多：报错，不截断（截断会把邻居读成字符串的一部分）。"""
    raw = bytes([10, 9]) + b"AB"
    with pytest.raises(ValueError, match="header says"):
        s7_codec.decode(raw, "s7string")      # 不给 length：让长度头自己说话


def test_a_max_length_over_254_is_refused():
    with pytest.raises(ValueError, match="over 254"):
        s7_codec.encode("A", "s7string", 2 + 255)


def test_validate_accepts_an_s7string_point():
    project = Project(channels=[S7Channel("s7-1", host="127.0.0.1", port=102, devices=[
        Device("D1", 11, [Point("铭牌", "db1", 0, "s7string", initial="AB")])])])
    assert error_lines(validate_project(project)) == []


# ---------- 端到端：真主站按 STRING 读 ----------

def channel(port: int) -> S7Channel:
    return S7Channel("s7-1", host="127.0.0.1", port=port, devices=[
        Device("D1", 11, [
            Point("铭牌", "db1", 0, "s7string", initial="AB"),
            Point("温度", "db1", 4, "real", initial="12.5",
                  waveform="const", wave_param={"value": "20"}),
        ])])


def master(port: int) -> S7Client:
    return S7Client("127.0.0.1", rack=0, slot=1, port=port, timeout=3.0)


def wait_for(predicate, timeout: float = 3.0, what: str = "") -> None:
    """等一个条件成立（与 `test_s7_backend.py` 同一个理由）：主站的读/写在服务端是**事件**，
    snap7 在它自己的线程上回调，`client.write()` 返回只说明它收到了响应，回调可能还差一拍。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what or predicate}")


def test_a_real_master_reads_it_as_an_s7_string(engine_runner):
    """`DB1,S0.2` = 最大 2 的 STRING：读回来是点表初值，写回去也生效。"""
    port = free_port()
    backend = make_backend(channel(port))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "s7string start")
    client = master(port)
    try:
        if not client.is_connected:
            client.connect()
        assert client.read(["DB1,S0.2"]) == ["AB"]

        client.write(["DB1,S0.2"], ["CD"])
        assert client.read(["DB1,S0.2"]) == ["CD"]
        # 归属：主站写过的那一格切成 manual（S7 那套事件记账照旧管这一种类型）。
        # 两个细节：**定位键是 0**（S7 按区/DB/字节寻址，没有 unit 这一维）；
        # 事件是 DLL 线程回调过来的，所以等一下，别用裸 sleep 赌时间。
        wait_for(lambda: backend.source_is_waveform(0, "db1", 0) is False,
                 what="主站写事件把铭牌那一格标成 manual")
        # 写进去的是"当前长度 + 字符"：长度头也跟着变了
        raw = backend.read_point_bytes(Point("铭牌", "db1", 0, "s7string", initial="AB"))
        assert raw[:4] == bytes([2, 2]) + b"CD"
    finally:
        client.disconnect()
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7string stop")


def test_the_seeded_bytes_carry_the_header(engine_runner):
    """播种（起从站那一刻）写进去的就得是带长度头的形状：主站一上来读到的是"AB"。"""
    port = free_port()
    backend = make_backend(channel(port))
    engine_runner.call(lambda: backend.start(), _CALL_TIMEOUT, "s7string seed")
    try:
        raw = backend.read_point_bytes(Point("铭牌", "db1", 0, "s7string", initial="AB"))
        assert raw == bytes([2, 2]) + b"AB"
        assert isinstance(backend, S7Backend)
    finally:
        engine_runner.call(lambda: backend.stop(), _CALL_TIMEOUT, "s7string stop")
