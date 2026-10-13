"""波形：纯函数 + 注册表。运行态一律不住这里（见 `backend/ownership.py`）。

`t` 是**引擎启动后的单调秒**，由调用方给；模块自己不读 `time`，这样 spec §10 的
"注入固定时间戳"才做得到，系统时间跳变也污染不了曲线。参数缺失/类型不符在
启动前校验（`project/validate._wave`）就拦住了，这里只信任已通过的参数。
"""

from __future__ import annotations

import math
import os
import random as _random

from ..project.model import (
    BOOL, DATATYPES, FLOAT32, FLOAT64, STRING, WAVEFORMS,
    _FLOAT_LIMIT, _INT_BOUNDS, brief,
)


def _num(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"wave param {brief(text)} is not a number") from exc


def _const(params: dict[str, str]):
    value = _num(params["value"]) if params.get("value") not in (None, "") else 0
    return lambda t: value


def _sine(params: dict[str, str]):
    base, amp, period = _num(params["base"]), _num(params["amp"]), _num(params["period"])
    phase = _num(params.get("phase", "0"))
    return lambda t: base + amp * math.sin(2 * math.pi * (t / period) + phase)


def _ramp(params: dict[str, str]):
    """`from → to` 的闭合三角波：上行 / 上停 / 下行 / 下停四段首尾相接。

    位置只由 `t % cycle` 落在哪一段决定，不记"上一次是往上还是往下"，所以同一个
    `t` 永远给同一个值；`speed` 是数值/秒，走完一条单程要 `span/speed` 秒，与引擎
    多久 tick 一次无关（spec §5、裁决 W2）。`from`/`to` 允许写反（10→0），故这里叫
    `start`/`end` 而不是 `low`/`high`，方向由 `span` 的符号自己带。
    """
    start, end = _num(params["from"]), _num(params["to"])
    speed, hold = _num(params["speed"]), _num(params.get("hold", "0"))
    span = end - start
    if span == 0:
        return lambda t: start                # validate 会拦 from==to，纯函数自己不许崩
    leg = abs(span) / speed                   # 单程秒数
    cycle = 2 * leg + 2 * hold                # 上→(hold)→下→(hold)

    def value(t: float) -> float:
        phase = t % cycle
        if phase < leg:                       # 上行：start → end
            ratio = phase / leg
        elif phase < leg + hold:              # 上停：钉在 end（hold=0 时这段不存在）
            ratio = 1.0
        elif phase < 2 * leg + hold:          # 下行：end → start
            ratio = 1.0 - (phase - leg - hold) / leg
        else:                                 # 下停：钉在 start
            ratio = 0.0
        return start + span * ratio

    return value


def _step(params: dict[str, str]):
    raw = [v.strip() for v in str(params["values"]).split(",")]
    values = [_num(v) for v in raw if v != ""]
    interval = _num(params["interval"])
    if not values or interval <= 0:
        raise ValueError(f"step needs values and interval>0, got "
                         f"{brief(params.get('values'))}/{brief(params.get('interval'))}")
    return lambda t: values[int(t / interval) % len(values)]


# `seed` 不填时按 spec §5 的"不填则不重复"：种子要带**每进程**变化的熵，又不能是 `t`
# 的函数（否则同一个 interval 区间里的两个 tick 抖成两个值，裁决 W2 就破了）。所以熵在
# import 时定死一次，本次运行内恒定，两次 pytest 之间才不同。
_RUN_SALT = f"{os.getpid()}:{_random.SystemRandom().getrandbits(64):x}"


def _random_wave(params: dict[str, str]):
    low, high = sorted((_num(params["min"]), _num(params["max"])))
    interval, seed = _num(params.get("interval", "1.0")), params.get("seed", "")

    def value(t: float):
        bucket = int(t / interval)
        # 填了 seed 就完全可复现（同 seed 同 bucket 同值），没填才掺进本次运行的熵
        key = f"{seed}:{bucket}" if seed else f"{_RUN_SALT}:{bucket}"
        # 恒给 float：要不要收成整数由 `clamp_to_datatype` 按 datatype 一处决定，在这里按
        # `min`/`max` 的字面写法猜类型，会让 float32 点位莫名其妙丢掉小数（spec §5 说的是均匀分布）。
        return _random.Random(key).uniform(low, high)

    return value


WAVE_BUILDERS = {"const": _const, "sine": _sine, "ramp": _ramp,
                 "random": _random_wave, "step": _step}
if not set(WAVE_BUILDERS) <= set(WAVEFORMS):
    # 注册表键必须是 model.WAVEFORMS 的子集：加了一种波形只改这里、忘了改 model，
    # 校验层会把它当 unknown waveform 报给用户，界面里永远点不到它。
    raise RuntimeError("WAVE_BUILDERS drifted from model.WAVEFORMS")


def waveform_value(name: str, params: dict[str, str], t: float):
    if name not in WAVE_BUILDERS:
        raise KeyError(f"unknown waveform {brief(name)}")
    return WAVE_BUILDERS[name](params)(t)


_BOUNDS: dict[str, tuple[float, float]] = {
    # 区间只有一个真相源：整型直接复用 model._INT_BOUNDS（`typed_initial` 用的就是它），
    # 浮点从 model._FLOAT_LIMIT 派生。这里再手抄一份数字，model 改上限时必然漏（裁决 H/M）。
    **_INT_BOUNDS,
    FLOAT32: (-_FLOAT_LIMIT[FLOAT32], _FLOAT_LIMIT[FLOAT32]),
    FLOAT64: (-_FLOAT_LIMIT[FLOAT64], _FLOAT_LIMIT[FLOAT64]),
}
if set(_BOUNDS) | {BOOL, STRING} != set(DATATYPES):
    # model 加了类型而这里没登记时，`clamp_to_datatype` 会在引擎的 tick 里抛 KeyError，
    # 一个点打断一整轮写入——宁可导入即失败。
    raise RuntimeError("clamp bounds drifted from model.DATATYPES")


def clamp_to_datatype(value, datatype: str):
    """钳位到 datatype 边界，返回 `(值, 是否被钳过)`。

    `sine` 的 `base+amp` 很容易飘出 uint16，而把 70000 直接 `& 0xFFFF` 写进寄存器，
    主站读到的是一个"合法但完全错误"的遥测量（同 raw_writer 拒绝掩码的理由）。
    """
    if datatype == BOOL:
        return bool(value), False
    if datatype == STRING:
        return value, False                      # 字符串没有区间，交给 codec 限长度
    lo, hi = _BOUNDS[datatype]
    if value != value:                            # NaN：钳到下界并报告，别把 NaN 写进寄存器
        return lo, True
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return (int(value) if datatype in _INT_BOUNDS else value), False
