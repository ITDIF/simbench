"""波形必须是**纯函数**：给同样的 `t` 就给同样的值，不读系统时间、不藏状态。

spec §10 要求"波形注入固定时间戳做纯函数断言（含 §5 表中每个默认值分支）"，所以
这里每条都直接喂 `t`，不启引擎、不占端口、不开 Qt。
"""

import math

import pytest

from modslave.codec.waveform import waveform_value


def test_const_is_flat_and_defaults_to_zero_when_value_missing():
    assert waveform_value("const", {"value": "20.5"}, 0.0) == 20.5
    assert waveform_value("const", {"value": "20.5"}, 99.0) == 20.5
    # W4：空 const 不参与写入，但纯函数本身要能给个不炸的答案
    assert waveform_value("const", {}, 0.0) == 0


def test_sine_formula_and_phase_default():
    # base + amp*sin(2π(t/period)+phase)，spec §5 原式
    got = waveform_value("sine", {"base": "20", "amp": "15", "period": "60"}, 15.0)
    assert got == pytest.approx(35.0)                    # sin(π/2)=1
    assert waveform_value("sine", {"base": "20", "amp": "15", "period": "60"}, 45.0) \
        == pytest.approx(5.0)                            # sin(3π/2)=-1
    with_phase = waveform_value("sine", {"base": "0", "amp": "1", "period": "4",
                                         "phase": str(math.pi / 2)}, 0.0)
    assert with_phase == pytest.approx(1.0)              # phase 默认 0，填了就用


def test_ramp_moves_by_speed_and_bounces_at_the_edges():
    p = {"from": "0", "to": "10", "speed": "2"}          # 5 秒走完
    assert waveform_value("ramp", p, 0.0) == pytest.approx(0.0)
    assert waveform_value("ramp", p, 1.0) == pytest.approx(2.0)
    assert waveform_value("ramp", p, 5.0) == pytest.approx(10.0)   # 到边界
    assert waveform_value("ramp", p, 6.0) == pytest.approx(8.0)    # 反向
    assert waveform_value("ramp", p, 10.0) == pytest.approx(0.0)   # 回到下界
    # 一个完整周期是 2×(span/speed)=10 秒，所以 15 秒是第二个周期的上界顶点
    assert waveform_value("ramp", p, 15.0) == pytest.approx(10.0)


def test_ramp_hold_parks_at_the_edge_before_reversing():
    p = {"from": "0", "to": "4", "speed": "4", "hold": "1"}   # 1 秒到顶，停 1 秒
    assert waveform_value("ramp", p, 1.0) == pytest.approx(4.0)
    assert waveform_value("ramp", p, 1.5) == pytest.approx(4.0)   # 还在 hold 里
    assert waveform_value("ramp", p, 2.5) == pytest.approx(2.0)   # hold 结束后反向


def test_step_cycles_through_values_by_interval():
    p = {"values": "10,20,30", "interval": "5"}
    assert waveform_value("step", p, 0.0) == 10
    assert waveform_value("step", p, 4.999) == 10
    assert waveform_value("step", p, 5.0) == 20
    assert waveform_value("step", p, 12.0) == 30
    assert waveform_value("step", p, 15.0) == 10          # 循环回头


def test_step_keeps_spaces_and_empty_slots_out_of_the_sequence():
    assert waveform_value("step", {"values": "1, 2 ,,3", "interval": "1"}, 2.0) == 3


def test_random_is_stable_within_one_interval_and_seeded_is_reproducible():
    p = {"min": "0", "max": "100", "interval": "10", "seed": "7"}
    a = waveform_value("random", p, 3.0)
    assert 0 <= a <= 100
    assert waveform_value("random", p, 9.9) == a          # 同一区间内不许抖
    assert waveform_value("random", p, 20.3) == waveform_value(
        "random", {**p, "seed": "7"}, 20.3)                # 同 seed 同 t 同值


def test_random_without_seed_still_respects_the_bucket_boundary():
    p = {"min": "5", "max": "6", "interval": "2"}
    assert waveform_value("random", p, 4.0) == waveform_value("random", p, 5.5)


def test_random_flips_min_max_when_written_backwards():
    # validate 会拦 min>max，但纯函数不能因此给出区间外的值
    assert 1 <= waveform_value("random", {"min": "5", "max": "1", "interval": "1"}, 0.5) <= 5


def test_random_defaults_to_one_second_between_values_when_interval_is_omitted():
    # spec §5 的默认值分支：`interval` 选填、默认 1.0 秒，validate 认它合法，纯函数不许崩
    p = {"min": "0", "max": "1", "seed": "3"}
    assert waveform_value("random", p, 2.0) == waveform_value("random", p, 2.9)
    assert waveform_value("random", p, 2.0) != waveform_value("random", p, 3.0)


def test_clamp_pins_out_of_range_and_nan_to_the_low_edge():
    from modslave.codec.waveform import clamp_to_datatype
    assert clamp_to_datatype(70000, "uint16") == (65535, True)
    assert clamp_to_datatype(-5, "uint16") == (0, True)
    assert clamp_to_datatype(float("nan"), "float32") == (-3.4028234663852886e38, True)
    assert clamp_to_datatype(12.5, "float32") == (12.5, False)
    assert clamp_to_datatype(1, "bool") == (True, False)
    assert clamp_to_datatype(3.7, "uint16") == (3, False)
