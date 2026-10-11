"""值归属：每个点是 `waveform` 还是 `manual`。

**只在引擎 loop 线程读写**（`Session` 负责把 GUI 的接管/恢复命令用 `runner.call`
投回来）。这不是洁癖：这条仓库刚被"跨线程共享可变状态"咬过一次（spec §13 那条
GC 死锁），运行态只有一条线程碰，才谈得上"界面卡住也不会把状态写歪"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import PacketEvent


@dataclass
class SourceStore:
    _manual: set[tuple[str, int, str, int]] = field(default_factory=set)

    def own(self, channel: str, unit_id: int, area: str, address: int,
            count: int = 1, who: str = "gui") -> None:
        """把 `[address, address+count)` 这一段标成 manual。

        key 存的是**寄存器/位地址**而不是点位：本模块没有点表，`write_targets` 给出的也
        只有裸协议地址+数量，能稳定落进集合的只有地址（位区一个位一个 key，与
        `raw_writer` 的位地址同口径）。
        代价要写明白：主站从某个 uint64 点的**中间**截一段写（FC16 addr=6 qty=2 打到
        占 5..8 的那个点）时，被标的只有 6 与 7，基础地址 5 仍在波形手上——按点整段标是
        调用方的事，它手里才有点表跨度。
        `who` 不进 key，也不另存一份：spec §7 的 v1 只有 waveform|manual 两态，第三维一旦
        参与判定，"GUI 接管过、主站又接管过"就要谈谁覆盖谁，而现在的规则是谁都不覆盖。
        它留着只给调用方的日志说"这次是谁动的"，所以两个来源在状态上必须等价。
        `count <= 0` 钳到 1（只标基础地址那一个）：宁可多标一个也不要一个都不标——漏标就是
        "平台下发被波形顶回去"那次事故，多标顶多多一次"已停"，一次 `restore` 就回得来。
        报文那一路轮不到它兜（qty=0 的坏帧已被 `write_targets` 判掉，见
        `test_a_quantity_of_zero_is_not_a_write`），它兜的是调用方自己把跨度算成 0 的手滑。
        """
        for offset in range(max(1, int(count))):
            self._manual.add((channel, unit_id, area, address + offset))

    def is_waveform(self, channel: str, unit_id: int, area: str, address: int) -> bool:
        return (channel, unit_id, area, address) not in self._manual

    def restore(self, channel: str, unit_id: int, area: str, address: int,
                count: int = 1) -> bool:
        """把 `[address, address+count)` 这一段还给波形；这一段里原本有 manual 就返回 True。

        `count` 要与当初 `own` 给的那一段一致（默认 1，单寄存器点照旧一个参数就够）。
        跨度必须由调用方传：spec §7 的归属单位是**点位**，而本模块只认地址（点表在调用方
        手里），所以"整点放行"只能靠它把 `pt.span` 递进来。少了这一层就会留尾巴——占 4 个
        寄存器的 uint64 只恢复基础地址时，6/7/8 永远标着 manual：`cleared()`（状态栏那句
        "有 N 处被接管"）只增不减，`is_waveform(6)` 也会对着波形正在刷的地址说"仍被接管"。
        一段里一个都没接管过时返回 False，调用方才好在日志里说"没恢复过什么"。
        `count <= 0` 与 `own` 同样钳到 1。
        """
        span = {(channel, unit_id, area, address + offset)
                for offset in range(max(1, int(count)))}
        handed_back = span & self._manual
        self._manual -= handed_back
        return bool(handed_back)

    def cleared(self) -> int:
        """当前被接管的**地址**数（给状态栏/日志一句人话用）。

        按地址计数而不是按点位：一个 uint64 点被接管会算 4。这里不折算成点数，是因为
        折算需要点表跨度，而本模块不持有点位对象（见 `write_targets` 只给裸协议地址）。
        """
        return len(self._manual)


_WRITE_FC_AREA = {5: "co", 15: "co", 6: "hr", 16: "hr"}


def write_targets(packet: PacketEvent) -> list[tuple[int, str, int, int]] | None:
    """主站写报文 → `[(unit_id, area, address, count)]`；不是写就返回 None。

    地址是**裸协议地址**（报文里本来就没有 40001 偏置），count 对 05/06 是 1，
    对 15/16 取报文里的数量字段。16 的功能码 `0x10` 与"写 16 个寄存器"的字节 16 同值，
    所以功能码必须从固定偏移取，不能靠值猜。

    偏移与真实主站对过：`tests/test_ownership.py` 里那组字节是从 pymodbus 3.15.0 的
    client 打到真 backend 上抓下来的，MBAP 占 7 字节，`body[0]` 是功能码、`body[1:3]`
    是地址、`body[3:5]` 是数量。

    `direction == "rx"` 是唯一可靠的"这是主站的命令"判据，两个理由：05/06 的**响应**与请求
    逐字节相同（连 transaction id 都一样，实测见
    `test_the_slaves_own_write_response_is_not_a_write`），15/16 的响应回的是同样的
    fc+地址+数量，按内容分辨不出来；而异常响应被 `parse_packet` 脱掉了 `0x80`（M1 钉的
    契约），单看 `function_code` 会把"从站回了异常码 2"当成一次写。

    count 直接采信报文，不核对"数量是否真有那么些数据字节"：sink 收到的是**到达的字节块**
    而不是组好的帧（实测半个 FC16 帧也会进 sink），够长就按数量标。宁可早标一次也不要漏标——
    重复标在 set 里是同一个 key，无害；漏标才是 spec §7 那条"值被波形顶回去"的事故。
    所以这里只有**一处**长度判定 `len(body) < 5`：5 正好是这几个功能码的固定 PDU
    （05/06 = fc+地址+值，15/16 = fc+地址+数量），够长才认为字段读得出来。它**不是**在验帧
    完整——MBAP 的 length、byte count、数量 × 2 都不参与判定，本函数也不管一个字节块里挤了
    几条报文（切帧是调用方的事）。
    """
    if packet.direction != "rx":
        return None
    area = _WRITE_FC_AREA.get(packet.function_code)
    if area is None:
        return None
    body = packet.raw[7:]                # MBAP(7B) 之后从功能码开始
    if len(body) < 5:
        return None                     # 固定 PDU 还没到齐：这一条先不当写，下一片到了再认
    address = int.from_bytes(body[1:3], "big")
    count = 1
    if packet.function_code in (15, 16):
        count = int.from_bytes(body[3:5], "big")
    if count <= 0:
        return None
    return [(packet.unit_id, area, address, count)]
