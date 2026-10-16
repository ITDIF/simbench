"""值归属是 M4 唯一的"谁说了算"状态：默认 `waveform`，人工一改就转 `manual`。

spec §7 的理由是现场最费时的误判——平台下发命令后值被波形顶回去，人就去查平台。
这个文件只测状态本身；tick 真不真写寄存器由 `tests/test_session.py` 与集成用例钉。

`LogDedupe`（spec §8 的"同点同类 60s 一条"）也住在这里，因为它和 `SourceStore` 是同一种
东西：只在引擎 loop 线程读写的运行态表，本身不含任何协议或波形知识。
"""

from simbench.backend.modbus_tcp import parse_packet
from simbench.backend.ownership import LogDedupe, SourceStore, write_targets
from simbench.events import PacketEvent

K = ("tcp-5020", 11, "hr", 4)


def test_points_start_on_the_waveform_and_a_takeover_sticks():
    store = SourceStore()
    assert store.is_waveform(*K)
    store.own(*K, count=1, who="master")
    assert not store.is_waveform(*K)
    store.own(*K, count=1, who="gui")
    assert not store.is_waveform(*K)          # 已经是 manual，再标一次不改变什么


def test_restore_puts_the_point_back_on_the_waveform():
    store = SourceStore()
    store.own(*K, count=1, who="gui")
    assert store.restore(*K) is True
    assert store.is_waveform(*K)
    assert store.restore(*K) is False          # 本来就在波形上：没东西可恢复，别谎报成功


def test_takeover_of_a_multi_register_point_marks_every_register_address():
    store = SourceStore()
    store.own("tcp-5020", 11, "hr", 5, count=4, who="master")   # uint64 占 4 个寄存器
    for address in (5, 6, 7, 8):
        assert not store.is_waveform("tcp-5020", 11, "hr", address)
    assert store.is_waveform("tcp-5020", 11, "hr", 9)


def test_a_point_with_no_waveform_is_still_notified_as_waveform_owned():
    """`const`/空 wave_param 的点（W4）不参与写入，但界面上"已停"这个字只能由状态决定。

    默认值必须是"在波形上"，否则示例点表九个点全显示 `已停`，那是界面在演一出
    没发生的接管。
    """
    assert SourceStore().is_waveform("tcp-5020", 11, "hr", 0)


def evt(fc: int, raw: bytes, unit: int = 11) -> PacketEvent:
    return PacketEvent("tcp-5020", "rx", raw, unit, fc, "", 0.0)


def test_a_frame_missing_unit_or_function_code_is_never_a_write():
    """`unit_id` 与 `function_code` 在 `PacketEvent` 上是**可缺的**（不是每条报文都带着）。

    缺了必须当"不是写"：照标会往归属表里塞一个 `None` 键，而按 (unit_id, 区, 地址) 查的那
    一侧再也命中不了它——现场只表现成"这条曲线不动"，比漏标难查得多。这条钉的是
    `write_targets` 里那个早退（2026-10-16 纳入 mypy 时逮到的：原来只有 `direction` 一道闸）。
    """
    raw = bytes.fromhex("0001000000060b0600040058")      # 一条**合法**的 FC06 写
    assert write_targets(evt(6, raw)) == [(11, "hr", 4, 1)]
    assert write_targets(evt(6, raw, unit=None)) is None          # 不知道是哪个 unit
    assert write_targets(PacketEvent("tcp-5020", "rx", raw, 11, None, "", 0.0)) is None


def test_fc06_write_one_register_is_read_as_that_address():
    # tid=0001 unit=0B fc=06 addr=0004 value=0058
    assert write_targets(evt(6, bytes.fromhex("0001000000060b0600040058"))) \
        == [(11, "hr", 4, 1)]


def test_fc16_write_multiple_expands_over_the_count():
    raw = bytes.fromhex("00010000000b0b100005000408")
    assert write_targets(evt(16, raw)) == [(11, "hr", 5, 4)]


def test_fc05_and_fc15_land_on_the_coil_area():
    assert write_targets(evt(5, bytes.fromhex("0001000000060b050000ff00"))) \
        == [(11, "co", 0, 1)]
    assert write_targets(evt(15, bytes.fromhex("0001000000090b0f0003000501"))) \
        == [(11, "co", 3, 5)]


def test_reads_and_unknown_frames_are_not_writes():
    assert write_targets(evt(3, bytes.fromhex("0001000000050b0300000002"))) is None
    assert write_targets(evt(0, b"\x00")) is None
    assert write_targets(PacketEvent("tcp-5020", "tx", b"", 11, 6, "", 0.0)) is None  # 只认 rx


# ---- 下面几组钉的是接口里已声明但上面没覆盖到的那几处，以及那些字节偏移的出处 ----

def test_takeover_of_a_bit_area_marks_one_key_per_coil():
    """位区一个点 = 一个位，所以 count 在这里是"多少个线圈"而不是"多少字节"。

    FC15 的 qty=5 点亮 5 个线圈；`own` 对位区必须摊成 5 个 key，否则主站只写了
    `风机群` 的第一个线圈，其余四个还在波形手上被顶掉（spec §7 的同一类误判）。
    """
    store = SourceStore()
    store.own("tcp-5020", 11, "co", 3, count=5, who="master")
    for address in range(3, 8):
        assert not store.is_waveform("tcp-5020", 11, "co", address)
    assert store.is_waveform("tcp-5020", 11, "co", 8)
    assert store.is_waveform("tcp-5020", 11, "hr", 3)     # 位区与寄存器区互不相干


def test_cleared_counts_the_addresses_under_takeover():
    """按**地址**的接管计数要数得动，也要恢复得回去（状态栏那句"有 N 处被接管"按点计数，
    用的是 `Session.manual_count`，不是这里）。"""
    store = SourceStore()
    assert store.cleared() == 0
    store.own("tcp-5020", 11, "hr", 4, count=1, who="gui")
    store.own("tcp-5020", 11, "hr", 5, count=4, who="master")
    assert store.cleared() == 5                            # 1 + 4 个地址
    assert store.restore("tcp-5020", 11, "hr", 5) is True
    assert store.cleared() == 4
    store.own("tcp-5020", 11, "hr", 4, count=1, who="gui")  # 重复接管不加数
    assert store.cleared() == 4


def test_a_restore_clears_exactly_the_address_it_was_given():
    """多寄存器点的尾巴**不会**跟着基础地址一起放行——这条把口径钉死，免得各写各的。

    `restore` 的 `count` 默认 1（单寄存器点省事，也是 brief 那条 `restore(*K) is True`
    用的形状），所以"按点整段放行"是调用方的事：Task 3/5 手里有 `pt.span`，必须把它传进来
    （见 `test_restoring_a_multi_register_point_clears_the_whole_span`）。这里断言的是**没传
    跨度时**的行为，不是最省事的行为——真要改默认值，红的第一时间就是这个文件。
    """
    store = SourceStore()
    store.own("tcp-5020", 11, "hr", 5, count=4, who="master")
    assert store.restore("tcp-5020", 11, "hr", 5) is True
    assert store.is_waveform("tcp-5020", 11, "hr", 5)
    assert not store.is_waveform("tcp-5020", 11, "hr", 6)
    assert store.cleared() == 3


def test_restoring_a_multi_register_point_clears_the_whole_span():
    """spec §7 的归属单位是**点位**：4 个寄存器的 uint64 按基础地址 + `count=4` 恢复，
    整段都得回到波形手上，`cleared()` 也要回得去 0。

    留尾巴的代价是看得见的两处：`cleared()` 永远回不了 0（它数的是地址），`is_waveform(6)`
    还会对着波形正在刷的地址说"仍被接管"。本模块没有点表，
    所以跨度由调用方传（`own`/`restore` 同一条要求）。
    """
    store = SourceStore()
    store.own("tcp-5020", 11, "hr", 5, count=4, who="master")
    assert store.cleared() == 4
    assert store.restore("tcp-5020", 11, "hr", 5, count=4) is True
    for address in (5, 6, 7, 8):
        assert store.is_waveform("tcp-5020", 11, "hr", address)
    assert store.cleared() == 0


def test_restoring_with_too_small_a_count_leaves_the_tail_taken_over():
    """`count` 传小了不会报错，只会留下尾巴——这条把"跨度必须传对"写成断言，别让调用方猜。

    接口没法自己知道 `hr 5` 那个点占几格（点表在调用方手里），只能照给的撤；剩下的那一截
    还在 manual 上，直到有人按地址把它也放回去。
    """
    store = SourceStore()
    store.own("tcp-5020", 11, "hr", 5, count=4, who="master")
    assert store.restore("tcp-5020", 11, "hr", 5, count=2) is True
    assert store.cleared() == 2
    assert store.is_waveform("tcp-5020", 11, "hr", 6)
    assert not store.is_waveform("tcp-5020", 11, "hr", 7)
    assert store.restore("tcp-5020", 11, "hr", 7, count=2) is True
    assert store.cleared() == 0


def test_a_restore_only_touches_the_span_it_was_handed():
    """段外的接管一个都不许跟着撤：`[address, address+count)` 是闭左开右的那一段。

    "任一命中就整区放行"这种省事写法会把别人正在 manual 的点一起交回波形——那是反向的
    spec §7 事故（人刚改的值被波形顶回去）。
    """
    store = SourceStore()
    store.own("tcp-5020", 11, "hr", 5, count=1, who="gui")
    assert store.restore("tcp-5020", 11, "hr", 6, count=3) is False
    assert not store.is_waveform("tcp-5020", 11, "hr", 5)
    assert store.cleared() == 1


def test_a_non_positive_count_is_clamped_to_the_base_address():
    """`count <= 0` 在 `own` 与 `restore` 都钳到 1（`max(1, int(count))`）——这是**声明**。

    选钳到 1 而不是"什么都不标"：漏标才是 spec §7 那条"下发被顶回去"的事故，多标一个地址
    一次 `restore` 就回得来。报文那一路轮不到它兜（qty=0 的坏帧已被 `write_targets` 挡掉，
    见 `test_a_quantity_of_zero_is_not_a_write`），它兜的是 `count=len(registers)` 这类
    调用方算出 0 的手滑。
    """
    store = SourceStore()
    store.own(*K, count=0, who="gui")
    assert store.cleared() == 1
    assert not store.is_waveform(*K)
    assert store.restore(*K, count=0) is True        # restore 的钳位与 own 同一条规则
    assert store.cleared() == 0

    store.own(*K, count=-3, who="master")
    assert store.cleared() == 1                      # 负数同样只标基础地址
    assert store.restore(*K, count=-1) is True
    assert store.cleared() == 0


def test_a_write_that_starts_mid_point_leaves_the_base_address_on_the_waveform():
    """从点中间截一段写进来时，只标被写的那两个地址，基础地址仍在波形手上。

    本模块不认识"点"：`is_waveform` 的签名里没有跨度（Task 3 的 tick 就按基础地址判），
    所以"按点整段标"只能由手里有点表的那一层做。这条把这个缺口写成断言，别让它第一次
    显形的地方是集成用例。
    """
    store = SourceStore()
    store.own("tcp-5020", 11, "hr", 6, count=2, who="master")    # 打的是 5..8 那个 uint64
    assert store.is_waveform("tcp-5020", 11, "hr", 5)
    assert not store.is_waveform("tcp-5020", 11, "hr", 6)
    assert not store.is_waveform("tcp-5020", 11, "hr", 7)
    assert store.is_waveform("tcp-5020", 11, "hr", 8)


def test_the_takeover_source_does_not_add_a_third_state():
    """主站接管与 GUI 接管在状态上必须等价：恢复一次就回到波形上。

    `who` 只给日志看（"被主站接管"vs"被界面接管"），一旦它参与判定，"GUI 先接管、主站
    后接管"就得谈谁覆盖谁，而 spec §7 的规则是谁都不覆盖。
    """
    store = SourceStore()
    store.own(*K, count=1, who="gui")
    store.own(*K, count=1, who="master")
    assert store.cleared() == 1
    assert store.restore(*K) is True
    assert store.is_waveform(*K)


def through_the_sink(raw: bytes, direction: str = "rx") -> PacketEvent:
    """按 backend 的 sink 真给 Task 3 的形状造事件：unit/fc 由 `parse_packet` 从字节里解。

    手写 `fc=` 的用例证明不了那条真实风险——异常响应里的 `0x80` 已经被 `parse_packet`
    脱掉了，剩下的数字和一个写功能码一模一样。
    """
    unit, function, summary = parse_packet(raw)
    return PacketEvent("tcp-5020", direction, raw, unit, function, summary, 0.0)


def test_offsets_match_the_frames_a_real_pymodbus_master_puts_on_the_wire():
    """这几帧是从 pymodbus 3.15.0 的 client 打到真 `ModbusTcpBackend` 上抓下来的。

    偏移不靠文档靠实测：MBAP 7 字节，之后 fc(1) 地址(2) 数量(2)。其中 qty=2 的那条
    FC16 专门留着顶一句"别把 byte count 当数量"——两条的 byte count 分别是 `08`/`04`，
    都在 `body[5]`，而 count 取的是 `body[3:5]`。
    """
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000060B050000FF00"))) == [(11, "co", 0, 1)]
    assert write_targets(through_the_sink(
        bytes.fromhex("0002000000060B0600040058"))) == [(11, "hr", 4, 1)]
    assert write_targets(through_the_sink(
        bytes.fromhex("0003000000080B0F00030005011F"))) == [(11, "co", 3, 5)]
    fc16_four = bytes.fromhex("00040000000F0B1000050004080001000200030004")
    assert write_targets(through_the_sink(fc16_four)) == [(11, "hr", 5, 4)]
    fc16_two = bytes.fromhex("00050000000B0B10000000020400010002")
    assert write_targets(through_the_sink(fc16_two)) == [(11, "hr", 0, 2)]
    assert write_targets(through_the_sink(
        bytes.fromhex("0006000000060B0300000002"))) is None       # 读报文不是写


def test_the_slaves_own_write_response_is_not_a_write():
    """05/06 的响应与请求**逐字节相同**（连 transaction id 都一样），只有方向能分开它们。

    实测（字节见 `test_offsets_match_the_frames_a_real_pymodbus_master_puts_on_the_wire`
    那组抓包）：把从站自己回的那条 FC16 响应标成 rx，
    `write_targets` 一样会解出 `[(11, "hr", 5, 4)]`。也就是说 Task 3 若在 tx 上报文上调
    `own()`，从站每答一次写就把刚被写的点标成"主站写过"——响应里没有任何字段能分辨，
    所以 `direction == "rx"` 不是防御性检查，是唯一的判据。
    """
    coil_response = bytes.fromhex("0001000000060B050000FF00")
    register_response = bytes.fromhex("0002000000060B0600040058")
    fc16_response = bytes.fromhex("00AA000000060B1000050004")
    for raw in (coil_response, register_response, fc16_response):
        assert write_targets(through_the_sink(raw, direction="tx")) is None
    assert write_targets(through_the_sink(fc16_response)) == [(11, "hr", 5, 4)]


def test_an_exception_response_is_never_a_write_target():
    """异常响应 `0B 90 02` 经 `parse_packet` 之后 `function_code` 是 16 —— 两个守卫都得拦住。

    钉这一条是因为 M1 已把"脱 0x80"定成 `parse_packet` 对外契约（报文窗要一眼看出异常码），
    那就等于把"功能码看起来像写"这件事交给了调用方；这里是调用方，所以两个方向都断言：
    tx 走方向守卫，而假装的 rx 走长度守卫（异常响应的 PDU 只有 fc+码 2 字节）。
    """
    exception = bytes.fromhex("0004000000030B9002")
    assert write_targets(through_the_sink(exception, direction="tx")) is None
    assert write_targets(through_the_sink(exception)) is None


def test_a_quantity_of_zero_is_not_a_write():
    """`qty=0` 的 15/16 是坏帧：按数量标就会白占一个地址，那个点从此不再被波形刷新。

    FC16 的合法数量是 1..123、FC15 是 1..1968，0 只能来自半坏的 485 桥或写错了的从站固件。
    `own(count=0)` 会被 `max(1, ...)` 兜成"标一个地址"，等于凭空停掉一个点——所以这一条
    必须在报文识别这层就挡掉，不许留给状态去猜。
    """
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000070B100005000000"))) is None
    assert write_targets(through_the_sink(
        bytes.fromhex("0002000000070B0F0003000000"))) is None
    # 对照：06 的那个 0 是**值**不是数量，寄存器照样被写
    assert write_targets(through_the_sink(
        bytes.fromhex("0003000000060B0600040000"))) == [(11, "hr", 4, 1)]


def test_a_quantity_beyond_the_protocol_maximum_is_not_a_write():
    """MBAP 长度合规、数量字段撒谎（FC16 `qty=65535`）：这一帧协议压根收不下，一个寄存器
    都没落下去，照数量标就等于让一帧坏报文静默停掉整条通道的曲线——实测点表 10 个地址一次
    全成 manual、日志一个字没有。上限因此必须在这里判死：FC16 123 寄存器、FC15 1968 位。

    两头都钉，因为差一个在这里看不见：上限本身照收，+1 才拒。不给数据字节是故意的——
    本函数**不**核对数量与数据字节数（理由见 docstring 与
    `test_a_split_rx_chunk_is_read_never_later_than_the_full_frame`）。
    """
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000060B100005007C"))) is None      # qty=124
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000060B100005FFFF"))) is None      # qty=65535
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000060B0F000307B1"))) is None      # qty=1969
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000060B100005007B"))) == [(11, "hr", 5, 123)]
    assert write_targets(through_the_sink(
        bytes.fromhex("0001000000060B0F000307B0"))) == [(11, "co", 3, 1968)]


def test_a_split_rx_chunk_is_read_never_later_than_the_full_frame():
    """sink 收到的是**到达的字节块**而不是组好的帧：TCP 分片时这里会先进来半个帧。

    实测过一片只到地址字段的半帧（10 字节）与一片到数量字段的半帧（12 字节）。判据按
    "能不能确定性地读出 fc+地址+数量"给：读不出就返回 None，读得出就照数量标——后者会
    在同一帧的下一片再到时重复标一次，`_manual` 是 set，同一个 key 加两次无害；反过来
    若为了等完整帧而漏标，就是"平台下发被波形顶回去"那次事故。
    """
    without_quantity = bytes.fromhex("00AA0000000F0B100005")
    with_quantity = bytes.fromhex("00AA0000000F0B1000050004")
    assert write_targets(through_the_sink(without_quantity)) is None
    assert write_targets(through_the_sink(with_quantity)) == [(11, "hr", 5, 4)]


def test_a_chunk_that_stops_inside_the_fixed_pdu_is_not_a_write():
    """唯一的长度判定 `len(body) < 5` 得两头钉住：短了不认，齐了必须认。

    上面原本还有一道 `len(raw) < 8`，它被这一道整个吞掉（`body = raw[7:]` 之后 8 与 5 判的
    是同一件事），删掉它没人少测一条；所以这一道现在是最后一道闸，而 05/06 的 count 是写死
    的 1、`count <= 0` 那道闸对它们**永远不亮**：半个 FC06（只到地址字段）没有这道闸就会被
    读成"写了 hr 4"，于是凭空停掉一个点——那半帧可能永远不会补全（半坏的 485 桥、缓冲里的
    残包）。FC16 只到数量**高字节**的那片同理，剩下那半个字节会被读成一个像模像样的小数量。
    最后一条对照是给"别把这道闸改严"用的：补上值字段那 2 字节就是合法帧，照旧得认。
    """
    half_fc06 = bytes.fromhex("00AA000000060B060004")          # 10B：值字段还没到
    partial_fc16 = bytes.fromhex("00AA0000000F0B10000502")     # 11B：qty 只到了高字节
    assert write_targets(through_the_sink(half_fc06)) is None
    assert write_targets(through_the_sink(partial_fc16)) is None
    assert write_targets(through_the_sink(half_fc06 + b"\x00\x58")) == [(11, "hr", 4, 1)]


def test_a_chunk_that_carries_two_frames_reads_as_the_first_one_only():
    """两条写请求挤进同一个 TCP 段时，本函数只认得出第一条——契约就是"一条报文"。

    实测那 24 个字节（两条 FC06，写 `hr 4` 与 `hr 5`）：pymodbus 把两段都写进了 datastore，
    而 sink 只收到一条记录。要条条被接管，切帧是调用方（Task 3 的 sink）按 MBAP 的 length
    做的事；在这里扫整个 buffer 等于把"这是几条报文"的判断从后端抢过来，两头都测不动。
    """
    two_frames = bytes.fromhex("00B1000000060B0600040099"
                               "00B2000000060B0600050088")
    assert write_targets(through_the_sink(two_frames)) == [(11, "hr", 4, 1)]


# ---- 运行期报错的去重表（spec §8：同点同类 60s 内不重复刷屏）----

def test_the_same_point_and_kind_is_reported_once_per_sixty_seconds():
    """飘出边界的曲线每 100ms 一条，日志窗两分钟就被同一条刷屏刷死（spec §8）。"""
    dedupe = LogDedupe()
    key = (11, "hr", 0)
    assert dedupe.report("clamp", key, "CO浓度 clamped", 0.0) == "CO浓度 clamped"
    assert dedupe.report("clamp", key, "CO浓度 clamped", 1.0) is None
    assert dedupe.report("clamp", key, "CO浓度 clamped", 59.0) is None
    assert dedupe.report("clamp", key, "CO浓度 clamped", 61.0) == "CO浓度 clamped"
    # 另一个点是另一件事：风机与照明不能互相把对方的报错吃掉
    assert dedupe.report("clamp", (12, "hr", 0), "回路 clamped", 61.5)


def test_different_kinds_on_the_same_point_do_not_share_a_window():
    dedupe = LogDedupe()
    assert dedupe.report("clamp", (11, "hr", 0), "a", 0.0)
    assert dedupe.report("encode", (11, "hr", 0), "b", 0.5)      # 不同类，各报各的


def test_the_window_is_a_knob_and_not_a_hardcoded_sixty_seconds():
    """`window` 必须真参与判定：把 60 写死在比较里时，上面那两条照样全绿。传非默认值的只有
    本用例——backend 恒用默认 60s，集成用例靠 `_tick_once(now=...)` 灌固定时间戳来验。

    两头都钉：`0` 一条都不压（窗口内没有"上一条"），`3600` 把 61s 那条也按住。只钉一头时
    把常数换成另一头的值还能过。
    """
    off = LogDedupe(window=0.0)
    assert off.report("clamp", (11, "hr", 0), "a", 0.0) == "a"
    assert off.report("clamp", (11, "hr", 0), "a", 0.0) == "a"

    wide = LogDedupe(window=3600.0)
    assert wide.report("clamp", (11, "hr", 0), "a", 0.0) == "a"
    assert wide.report("clamp", (11, "hr", 0), "a", 61.0) is None


def test_reset_lets_the_next_run_report_the_same_point_again():
    """`reset()` 是"这是一次新的运行"那句话：通道重新起来时 backend 调它（集成用例
    `test_restarting_the_channel_reports_the_same_clamped_point_again` 钉那个调用点）。

    这里钉表本身：不清零时 0.5s 的那一次还在窗口内（`now` 没往前走够），清了就重新开口。
    """
    dedupe = LogDedupe()
    assert dedupe.report("clamp", (11, "hr", 0), "a", 0.0) == "a"
    assert dedupe.report("clamp", (11, "hr", 0), "a", 0.5) is None
    dedupe.reset()
    assert dedupe.report("clamp", (11, "hr", 0), "a", 0.5) == "a"

