"""运行期直接读写 SimRuntime 的寄存器列表。

不走 SimCore.async_setValues：那条路会检查 readonly 标志，而 readonly 是我们故意
给只读点设的（用来挡主站写），引擎自己刷新数值也会被它挡掉（"已实测事实"3）。
"""

from __future__ import annotations

from pymodbus.simulator import DataType
from pymodbus.simulator.simutils import SimUtils

_BLOCK = {"co": "c", "di": "d", "hr": "h", "ir": "i"}


def _defined(flags: list[int], offset: int, count: int) -> bool:
    """`[offset, offset+count)` 这段是不是点表真定义过的地址。

    SimRuntime 把一个区的多个 SimData 合并成一块，空洞也在同一个列表里（填 0），所以
    只按 `[start, start+length)` 判边界会把未定义地址当成"可读写"——主站对同一个地址
    收异常码 2（"已实测事实"5 与实测 `read hr 5..5: 2`），引擎却读到 0 或写成功，两侧
    不一致就是静默错值。判据直接用 pymodbus 自己打的标志位（`__create_block` 把空洞填
    `DataType.INVALID`），与 `SimRuntime.__check_block` 同一口径。
    """
    if offset < 0 or count <= 0 or offset + count > len(flags):
        return False
    return all(flag & SimUtils.RunTimeFlag_TYPE != DataType.INVALID
               for flag in flags[offset:offset + count])


class RawRegisterWriter:
    def __init__(self, core) -> None:
        """core: ModbusBaseServer.context，即 pymodbus 的 SimCore。"""
        self._core = core

    def write_registers(self, unit_id: int, area: str, address: int,
                        values: list[int]) -> bool:
        block = self._block(unit_id, area)
        if block is None:
            return False
        start, _length, registers, flags = block
        offset = address - start
        if not _defined(flags, offset, len(values)):
            return False
        for i, value in enumerate(values):
            registers[offset + i] = int(value) & 0xFFFF
        return True

    def read_registers(self, unit_id: int, area: str, address: int,
                       count: int) -> list[int] | None:
        block = self._block(unit_id, area)
        if block is None:
            return None
        start, _length, registers, flags = block
        offset = address - start
        if not _defined(flags, offset, count):
            return None
        return list(registers[offset:offset + count])

    def write_bits(self, unit_id: int, area: str, bit_address: int, value: bool) -> bool:
        """写一个线圈/离散输入位，同寄存器里其余 15 位保持不动。

        四区独立形态下 `use_bit_addressing` 恒为 True（"已实测事实"2），元组里的 start
        已经是**寄存器下标**（`__create_block_bits` 返回 `int(start_address / 16)`），
        所以先 `// 16` 换寄存器、再 `% 16` 取位偏移，与 `SimRuntime.get_bit_block` 同式。
        """
        block = self._block(unit_id, area)
        if block is None:
            return False
        start, _length, registers, flags = block
        index = bit_address // 16 - start
        if not _defined(flags, index, 1):
            return False
        mask = 1 << (bit_address % 16)
        registers[index] = (registers[index] & ~mask | mask) if value \
            else registers[index] & ~mask
        return True

    def read_bits(self, unit_id: int, area: str, bit_address: int,
                  count: int) -> list[bool] | None:
        block = self._block(unit_id, area)
        if block is None:
            return None
        start, _length, registers, flags = block
        first = bit_address // 16 - start
        last = (bit_address + count - 1) // 16 - start
        if not _defined(flags, first, last - first + 1):
            return None
        bits = []
        for position in range(bit_address, bit_address + count):
            index = position // 16 - start
            bits.append(bool(registers[index] >> (position % 16) & 1))
        return bits

    def _block(self, unit_id: int, area: str):
        if area not in _BLOCK:
            raise ValueError(f"unknown area {area!r}")
        device = self._core.devices.get(unit_id)
        if device is None:
            return None
        return device.block.get(_BLOCK[area])
