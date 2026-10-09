"""运行期直接读写 SimRuntime 的寄存器列表。

不走 SimCore.async_setValues：那条路会检查 readonly 标志，而 readonly 是我们故意
给只读点设的（用来挡主站写），引擎自己刷新数值也会被它挡掉（"已实测事实"3）。
"""

from __future__ import annotations

from pymodbus.simulator import DataType
from pymodbus.simulator.simutils import SimUtils

from ..project.model import brief

_BLOCK = {"co": "c", "di": "d", "hr": "h", "ir": "i"}


def _check_values(area: str, values: list[int]) -> None:
    """裁决 2/5：入口拒绝脏容器、空值表与越界值，`& 0xFFFF` 只许用在产出侧。

    判据与 codec 的 `_check_registers` 同口径同句式（不是 list/tuple 就拒、消息同一句），
    但谓词不跨层复用：`backend → codec` 的依赖会让"引擎只用 raw_writer 写寄存器"这条
    边界变模糊，宁可留两句一样的文案。

    - 脏容器：`write_registers(11,'hr',0,5)` 实测抛 `TypeError`（逃逸），`set()`/`{}`/`()`
      实测静默返回 False。整数迭代到 str/int 上才会炸，所以容器判定必须排在前面。
    - 空值表：以前同样静默 False，语义不明——"一个都没写"和"地址未定义写不进"是两件
      事，Task 11 的批量写会把成功当失败。现在单独抛 `empty value list`。
    - 越界/脏元素：掩码会把 70000 静默截成 4464 写进寄存器，主站读到的是一个"合法但
      错误"的遥测量，现场只会当成平台 bug 去调。引擎的写入值都来自 codec，天然在
      0..65535 内，出现越界值（或 bool/float/str）只可能是引擎或调用方的 bug。

    先整表校验再落地，绝不半写：`[1, 70000]` 不能把地址 0 改了一半。
    """
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{area} registers must be ints in 0..65535, got {brief(values)}")
    if not values:
        raise ValueError("empty value list")
    if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFFFF
           for v in values):
        raise ValueError(f"{area} registers must be ints in 0..65535, got {brief(values)}")


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
        # 两个编程错误先炸，再谈运行期数据错误：值列表不对，unit 存不存在都不重要。
        block = self._block(unit_id, area)   # 未知 area -> ValueError
        _check_values(area, values)          # 脏容器/空表/越界/非 int -> ValueError（不再掩码）
        if block is None:                    # 未知 unit -> 运行期数据错误
            return False
        start, _length, registers, flags = block
        offset = address - start
        if not _defined(flags, offset, len(values)):
            return False
        for i, value in enumerate(values):
            registers[offset + i] = value
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

        值只收 Python bool（裁决 4，与 `write_registers` 的严格口径对称）：实测
        `write_bits(11,'co',0,"0")` 把线圈置成 True，`"no"` 同样置 True，`None`/`[]`/`""`
        静默 False。`bool(value)` 对非空字符串恒真，正是裁决 A 禁的"未识别值静默给
        bool"——引擎想关风机、位被写成开，主站读回一个合法但相反的开关量。
        """
        if not isinstance(value, bool):
            raise ValueError(f"{area} bit value must be a bool, got {brief(value)}")
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
