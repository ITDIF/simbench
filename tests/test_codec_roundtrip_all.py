# tests/test_codec_roundtrip_all.py
"""全 11 种 dtype × 全部允许字序的互逆回归。

计划文件的结构表里没列这个文件，但它兜住的是整个 M1 最不能错的东西：上一轮质量评审
用 42113 组随机 + 边界值实测过 `_permute_words` 正确且互逆，那是脚本跑出来的结论，
不落进 pytest 就守不住后续改动（`_NUMERIC` 改成从 model.DATATYPES 派生之后尤其如此）。

边界值直接读 model 的 `_INT_BOUNDS` / `_FLOAT_LIMIT`：测试里再手抄一份上下限，
就成了第三份真相，model 改宽时这里会假绿。
"""
import pytest

from modslave.codec.point_codec import decode, encode
from modslave.project.model import (
    BOOL, DATATYPES, FLOAT32, FLOAT64, STRING, _FLOAT_LIMIT, _INT_BOUNDS,
    register_span, word_orders_allowed,
)

# 每个 dtype 把自己允许的每一种字序都跑一遍，字序口径同样来自 model
COMBOS = [(dt, wo) for dt in DATATYPES for wo in word_orders_allowed(dt)]

# 多位类型专用的“换个字序就一定看得出来”的值；1 寄存器类型走不到这里
_DISCRIMINATOR = {
    "int32": 0x12345678, "uint32": 0x12345678, "float32": -12.5,
    "int64": 0x0102030405060708, "uint64": 0x0102030405060708,
    "float64": 1.2345678901234,
}


def sample_values(datatype: str):
    """各类型自己的边界值：只测 0/1 测不出符号位、进位与补零。"""
    if datatype == BOOL:
        return (True, False)
    if datatype == STRING:
        return ("A", "abc", "")
    if datatype in (FLOAT32, FLOAT64):
        # 全部是可精确表示的数，所以能断言 ==，不需要 approx
        limit = _FLOAT_LIMIT[datatype]
        return (0.0, 1.0, -1.0, 12.5, -12.5, limit, -limit)
    lo, hi = _INT_BOUNDS[datatype]
    values = [0, 1, lo, hi]
    if lo < 0:
        values.append(-1)
    return tuple(values)


def test_matrix_covers_every_datatype_and_allowed_order():
    """矩阵必须铺满 DATATYPES，且只铺 model 认可的字序。"""
    assert {dt for dt, _ in COMBOS} == set(DATATYPES)
    for dt, wo in COMBOS:
        assert wo in word_orders_allowed(dt)


@pytest.mark.parametrize("datatype,word_order", COMBOS)
def test_decode_is_the_inverse_of_encode(datatype, word_order):
    for value in sample_values(datatype):
        regs = encode(value, datatype, word_order)
        got = decode(regs, datatype, word_order)
        assert got == value, (datatype, word_order, value, regs)


@pytest.mark.parametrize("datatype,word_order", COMBOS)
def test_encode_length_matches_register_span(datatype, word_order):
    """encode 产出的寄存器个数必须等于点表算出的跨度，否则映射到 SimDevice 就错位。"""
    for value in sample_values(datatype):
        span = register_span(datatype, value if datatype == STRING else "0")
        regs = encode(value, datatype, word_order)
        assert len(regs) == span, (datatype, word_order, value, regs)


@pytest.mark.parametrize("datatype,word_order", COMBOS)
def test_encode_output_passes_the_decode_register_guard(datatype, word_order):
    """encode 的产出要能被 decode 入口的脏值校验接受，两侧口径闭环。"""
    for value in sample_values(datatype):
        for reg in encode(value, datatype, word_order):
            assert isinstance(reg, int) and not isinstance(reg, bool)
            assert 0 <= reg <= 0xFFFF


@pytest.mark.parametrize("datatype", [
    dt for dt in DATATYPES if len(word_orders_allowed(dt)) > 1])
def test_each_allowed_word_order_is_a_distinct_permutation(datatype):
    """字序不许悄悄退化成 ABCD：同一值在不同允许字序下必须产出不同序列。"""
    orders = word_orders_allowed(datatype)
    value = _DISCRIMINATOR.get(datatype, sample_values(datatype)[-1])
    produced = [tuple(encode(value, datatype, wo)) for wo in orders]
    assert len(set(produced)) == len(orders), (datatype, value, produced)
