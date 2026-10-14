"""主站侧的连接与读写：按通道参数连过去，读寄存器/位。

这是 `simbench/` 里**第一次**出现 pymodbus 的客户端（之前只有 tests/ 与打包冒烟用）。
边界照旧：pymodbus 的 import 只允许出现在 `backend/` 与 `master/` 这两棵树里
（`tests/test_architecture.py` 那份清单里加了它）。

支持的传输：`tcp` / `udp` / `rtutcp` / `rtu` / `ascii`——就是点表里那五种 Modbus 载体。
S7 不在这一层（它读的是字节 + 位，语义与寄存器不同，走 `master/s7.py` 另说，本轮没做）。

口径两条：

* **连不上不许当成"没有这个 unit"**：`probe()` 把"超时/异常码"与"连不上"分开——前者是
  "这台设备没应答这个 unit"，后者是"这个地址根本连不上"，报错要说清是哪一种；
* **读回来的永远是最少的那几格**：按点位的 `span` 读（uint32 读 2 格、string 按初值长度），
  读多了会把邻居的值混进来（那是"看着像对上了"的假象）。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from ..project.model import area_is_bit, channel_transport, is_serial


@dataclass
class MasterLink:
    """一个主站连接（一个通道一份）。字段就是"连过去要的那几样"。"""

    transport: str
    host: str = ""
    port: int = 0
    serial_port: str = ""
    baudrate: int = 9600
    parity: str = "N"
    bytesize: int = 8
    stopbits: int = 1
    timeout: float = 1.0
    _client: object | None = field(default=None, repr=False)

    @classmethod
    def for_channel(cls, channel, *, host: str | None = None, port: int | None = None,
                    timeout: float = 1.0) -> MasterLink:
        """按通道的参数造一份连接描述；`host`/`port` 给了就覆盖（命令行那两个参数）。"""
        transport = channel_transport(channel)
        if transport == "s7":
            raise ValueError("S7 通道的主站模式还没做（只支持 Modbus 那五种传输）")
        if is_serial(transport):
            return cls(transport=transport, serial_port=channel.port,
                       baudrate=channel.baudrate, parity=channel.parity,
                       bytesize=channel.bytesize, stopbits=channel.stopbits,
                       timeout=timeout)
        return cls(transport=transport, host=host or channel.host,
                   port=int(port or channel.port), timeout=timeout)

    def describe(self) -> str:
        if is_serial(self.transport):
            return (f"{self.serial_port} {self.baudrate} "
                    f"{self.bytesize}{self.parity}{self.stopbits} ({self.transport})")
        return f"{self.host}:{self.port} ({self.transport})"

    # ---------- 连接 ----------

    def open(self) -> None:
        """建客户端并连上。连不上抛 `ConnectionError`（调用方说人话）。"""
        self._client = self._make_client()
        if not self._client.connect():
            raise ConnectionError(f"连不上 {self.describe()}")

    def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _make_client(self):
        from pymodbus import FramerType
        from pymodbus.client import ModbusSerialClient, ModbusTcpClient, ModbusUdpClient

        if self.transport == "tcp":
            return ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
        if self.transport == "udp":
            return ModbusUdpClient(self.host, port=self.port, timeout=self.timeout)
        if self.transport == "rtutcp":
            return ModbusTcpClient(self.host, port=self.port, framer=FramerType.RTU,
                                   timeout=self.timeout)
        framer = FramerType.ASCII if self.transport == "ascii" else FramerType.RTU
        return ModbusSerialClient(port=self.serial_port, framer=framer,
                                  baudrate=self.baudrate, parity=self.parity,
                                  bytesize=self.bytesize, stopbits=self.stopbits,
                                  timeout=self.timeout)

    # ---------- 读 ----------

    def read(self, unit_id: int, area: str, address: int,
             count: int) -> ReadResult:
        """读一个点位的寄存器/位。回 `ReadResult`（`values` 为 `None` = 没读到）。"""
        if self._client is None:
            raise RuntimeError("这个连接还没 open()")
        response = (self._client.read_coils(address, count=count, device_id=unit_id)
                    if area_is_bit(area)
                    else self._client.read_holding_registers(
                        address, count=count, device_id=unit_id)
                    if area == "hr"
                    else self._client.read_input_registers(
                        address, count=count, device_id=unit_id))
        if response.isError():
            return ReadResult(values=None, detail=_error_text(response))
        if area_is_bit(area):
            return ReadResult(values=[1 if bit else 0 for bit in response.bits[:count]],
                             detail="")
        return ReadResult(values=list(response.registers[:count]), detail="")

    def probe(self, unit_id: int) -> ReadResult:
        """扫 unit 用的一次轻读：读 `hr` 的 0 号寄存器 1 格。

        为什么读 `hr 0`：几乎所有设备都有它（组态软件第一件事就是读它）；读不到只说明
        "这个 unit 没应答这一格"，不等于"没有这台设备"——报错措辞照这条写。
        """
        return self.read(unit_id, "hr", 0, 1)


@dataclass(frozen=True)
class ReadResult:
    """一次读的结果：`values is None` = 没读到（`detail` 说为什么）。"""

    values: list[int] | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.values is not None


def _error_text(response) -> str:
    code = getattr(response, "exception_code", None)
    if code is None:
        return "no response (timeout)"
    return f"exception code {int(code)}"
