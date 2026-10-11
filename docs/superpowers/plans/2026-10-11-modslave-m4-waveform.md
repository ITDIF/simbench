# M4 波形引擎 + 值归属 + 点位属性面板 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让点表里 `waveform`/`wave_param` 真跑出值（`const/ramp/sine/random/step`），并按 spec §7 落地唯一的值归属规则 `source = waveform | manual`，同时补上点位属性面板（改区/地址/类型/字序/波形参数）。

**Architecture:** 波形是**纯函数**（注入 `t`，无状态、可单测），住在 `codec/`；状态（每点的 `source`、钳位日志的去重）与 tick 循环住在 **`backend/`**——因为无头 CLI 那条路（`python -m modslave x.csv`，M1 的验收入口）直接用 `ModbusTcpBackend`、**不建 `Session`**，把 tick 放在 `Session` 里就等于"命令行起的从站没有波形"。tick 是 backend 在引擎 loop 上的 asyncio 任务，随 `ModbusTcpBackend.start()/stop()` 起停，算出的值经 `codec.encode` → `raw_writer` 直接写进 `SimRuntime` 的寄存器列表，主站读时不等 GUI；`Session` 只做命令转发（接管/恢复）与 `Row.source` 的呈现。

**Tech Stack:** Python 3.13（venv 在 `.venv/`）、pymodbus 3.15.0（只在 `backend/`）、PySide6 6.9.2（只在 `gui/`）、pytest（`pytest.ini` 已带 `-q` 与 `testpaths=tests`）。

**Spec:** `docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md`（§5 波形参数表、§7 值归属、§8 钳位与日志去重、§9 注册表缝、§10 测试策略、§11 M4）。**这份计划与 spec 一起读**，冲突以 spec 为准并把偏离记进本文件末尾的"执行期已裁决的偏离"表（沿用 M1/M2 计划的措辞与做法）。

## Global Constraints

- 依赖方向单向 `gui → engine → backend → project/codec`；`pymodbus`/`serial` 只许出现在 `backend/`，`PySide6` 只许出现在 `gui/`（`tests/test_architecture.py` 守，每条守卫都配防空跑断言）。
- **跨线程纪律**：引擎 loop 线程是可变的唯一归属。凡 `source`、随机数状态、日志去重表这类运行态，**只在 loop 线程读写**；GUI 一律经 `runner.call(...)` 投递命令，读侧走 10Hz 拉模型（`Session.rows()`）。
- **回收（GC）只许发生在 GUI 线程**（spec §12/§13）：`gui/app.py` 已 `gc.disable()`，破环由 `MainWindow._maybe_collect()` 每 5s 至多一次。任何新代码都不许在引擎线程触发回收的工作（例如把界面对象交给引擎线程持有）。
- 运行期写寄存器**只走 `backend/raw_writer.py`**，不走 `SimCore.async_setValues`：实测后者会把 `readonly` 点上的引擎自写一起拒（异常码 2），而 `readonly` 是我们故意给只读点设来挡主站的。
- 地址一律是**裸协议地址（0-based）**，模型里不藏 40001 偏置（spec §5，M2 已定）。
- `t` 一律是**引擎启动后的单调时钟秒**（`time.monotonic()` 减去通道启动时刻），不用系统时间——现场改系统时间会把曲线整段跳变。
- 报错口径：启动前校验一次报全；运行期拒绝**带点位名**；同点同类运行期错误 **60s 内不重复刷屏**（spec §8）。
- 界面口径（spec §7 + M2 已定）：状态变化**不许挪布局**、可枚举少量选项用**平铺标签不用下拉**、槽位装不下**改文案不加宽不截断**、默认视图给"全部"。
- 测试：全量必须无 skip / 无 xfail；**串行跑**（同机并行两份绑端口的套件结果不可信）；基线 **773 passed**。
- 提交者身份：`git -c user.name=gaojie -c user.email=2084220263@qq.com commit`；提交信息中文、`fix:/feat:/test:/docs:` 前缀，讲清"改了什么问题"。

## 执行前必须先定的四条口径（不要带着猜测写代码）

| # | 决定 | 本计划采用的口径 | 为什么（现场场景） |
|---|---|---|---|
| W1 | 主站写入怎么算"被人工接管" | 在 backend 的 **rx 侧**报文钩子里识别 `fc ∈ {5,6,15,16}`，解析出 unit + 起始地址（15/16 按数量展开），把落在点表上的点标成 `manual` | 平台下发"风机启动=1"，若波形还在跑，下一 tick 就把线圈顶回 0——运维读到的就是"下发没生效"。spec §7 唯一那条值归属规则要防的正是这个 |
| W2 | `random`/`step` 的换值节拍 | 由**波形自己按 `interval` 保持**（其余 tick 返回上一个值），不由 tick 频率决定 | `interval=1.0` 在 `tick_ms=100` 下要 1 秒换一个值；跟 tick 绑定会让"改 `tick_ms`"改变曲线含义 |
| W3 | 属性面板改区/地址/类型/字序何时生效 | **需要重启通道**：面板明确标注，且不假装立刻生效（`SimDevice` 是 `start_channel` 时建的） | 现场最常见的误判是"我改了地址，从站还在旧地址应答"。要么真生效，要么说清楚没生效 |
| W4 | `waveform=const` 且 `wave_param` 为空 | 保持现状：`const` 允许不填 `value`（`validate._wave` 已把它当合法），此时**不参与 tick 写入**（等于静态初值） | 示例点表 `examples/covi-tunnel.csv` 里全是 `const,,`，若 M4 把空 `const` 当 error，整张示例表就起不来了 |

---

## 文件结构（M4 新增/改动）

```
modslave/
  codec/
    waveform.py      新增：五种波形的纯函数 + 注册表 + 钳位（无运行态、不 import pymodbus/Qt）
  backend/
    ownership.py     新增：SourceStore（每点 waveform|manual）+ 写报文识别 + 钳位日志去重表
    modbus_tcp.py    改动：tick 循环随 start/stop 起停；算值→encode→raw_writer；rx 写报文标 manual
  engine/
    session.py       改动：Row.source、restore_waveform / manual_count 转发（经 runner.call）
    point_source.py  改动：FakeEngine 跟上 Row 位置契约与 source
  gui/
    point_table.py   改动："波形"列文案（`sine` / `已停`）+ 行右键菜单
    point_props.py   新增：点位属性面板（对话框）
    main_window.py   改动：选中行打开属性面板、右键"恢复波形"、接线
tests/
    test_waveform.py             新增：纯函数 + 每个默认值分支（spec §10）
    test_ownership.py            新增：SourceStore、写报文识别、60s 去重
    test_integration_tcp.py      追加：真主站读波形、下发不被顶（M4 的两条核心验收）
    test_session.py              追加：Row 契约、恢复波形转发
    test_gui_point_table.py      追加："已停"文案与列宽不动
    test_gui_main_window.py      追加：右键菜单项与置灰（不隐藏）
    test_gui_point_props.py      新增：面板回滚、"重启通道"提示、几何恒定
docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md  回写 §4/§7/§11/§12
```

**为什么 tick 不在 `engine/`**：spec §4 把 `Waveform` 画在引擎那一侧，但无头 CLI（`python -m modslave x.csv`，M1 的验收入口，`__main__._serve` 直接用 `ModbusTcpBackend`）**不建 `Session`**——tick 放在 `Session` 就等于"命令行起的从站没有波形"。而 `gui → engine → backend → project/codec` 是单向的（`tests/test_architecture.py` 守），`backend` 反过来 import `engine` 会破方向；所以纯函数落 `codec/`（`backend` 本来就依赖 `codec`），驱动与状态落 `backend/`。§4 那张图在 Task 7 回写时按这条改，并记进末尾的偏离表。

---

## Task 1: 波形纯函数 `codec/waveform.py`

**Files:**
- Create: `modslave/codec/waveform.py`
- Test: `tests/test_waveform.py`

**Interfaces:**
- Consumes: `modslave.project.model.WAVEFORMS`、点位的 `wave_param: dict[str,str]`（`csv_io.parse_wave_param` 已经切好 `k=v;k=v`）
- Produces:
  - `waveform_value(name: str, params: dict[str, str], t: float) -> float | int | bool | str`
  - `WAVE_BUILDERS: dict[str, Callable[[dict[str,str]], Builder]]`，`Builder.next(t) -> value`（spec §9 的"注册表按名字装配"）
  - `clamp_to_datatype(value, datatype: str) -> tuple[value, bool]`（返回钳位后的值与"是否被钳过"）

- [ ] **Step 1: 写失败的测试（`const` / `sine`）**

```python
# tests/test_waveform.py
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_waveform.py -v`
Expected: FAIL —`ModuleNotFoundError: No module named 'modslave.codec.waveform'`

- [ ] **Step 3: 实现 `const` 与 `sine`**

```python
# modslave/codec/waveform.py
"""波形：纯函数 + 注册表。运行态一律不住这里（见 `backend/ownership.py`）。

`t` 是**引擎启动后的单调秒**，由调用方给；模块自己不读 `time`，这样 spec §10 的
"注入固定时间戳"才做得到，系统时间跳变也污染不了曲线。参数缺失/类型不符在
启动前校验（`project/validate._wave`）就拦住了，这里只信任已通过的参数。
"""

from __future__ import annotations

import math

from ..project.model import WAVEFORMS, brief

def _num(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError) as exc:                # noqa: BLE001
        raise ValueError(f"wave param {brief(text)} is not a number") from exc

def _const(params: dict[str, str]):
    value = _num(params["value"]) if params.get("value") not in (None, "") else 0
    return lambda t: value

def _sine(params: dict[str, str]):
    base, amp, period = _num(params["base"]), _num(params["amp"]), _num(params["period"])
    phase = _num(params.get("phase", "0"))
    return lambda t: base + amp * math.sin(2 * math.pi * (t / period) + phase)

WAVE_BUILDERS = {"const": _const, "sine": _sine}
if not set(WAVE_BUILDERS) <= set(WAVEFORMS):
    # 注册表键必须是 model.WAVEFORMS 的子集：加了一种波形只改这里、忘了改 model，
    # 校验层会把它当 unknown waveform 报给用户，界面里永远点不到它。
    raise RuntimeError("WAVE_BUILDERS drifted from model.WAVEFORMS")

def waveform_value(name: str, params: dict[str, str], t: float):
    if name not in WAVE_BUILDERS:
        raise KeyError(f"unknown waveform {brief(name)}")
    return WAVE_BUILDERS[name](params)(t)

def clamp_to_datatype(value, datatype: str):
    """钳位到 datatype 边界，返回 `(值, 是否被钳过)`。

    `sine` 的 `base+amp` 很容易飘出 uint16，而把 70000 直接 `& 0xFFFF` 写进寄存器，
    主站读到的是一个"合法但完全错误"的遥测量（同 raw_writer 拒绝掩码的理由）。
    """
    raise NotImplementedError
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_waveform.py -v`
Expected: 两条 PASS

- [ ] **Step 5: 写 `ramp` 的失败测试（含 `hold` 与"与 tick_ms 无关"）**

```python
def test_ramp_moves_by_speed_and_bounces_at_the_edges():
    p = {"from": "0", "to": "10", "speed": "2"}          # 5 秒走完
    assert waveform_value("ramp", p, 0.0) == pytest.approx(0.0)
    assert waveform_value("ramp", p, 1.0) == pytest.approx(2.0)
    assert waveform_value("ramp", p, 5.0) == pytest.approx(10.0)   # 到边界
    assert waveform_value("ramp", p, 6.0) == pytest.approx(8.0)    # 反向
    assert waveform_value("ramp", p, 10.0) == pytest.approx(0.0)   # 回到下界（周期 10s）
    assert waveform_value("ramp", p, 15.0) == pytest.approx(10.0)  # 第二个上界
# 计划原来写的是 `15.0 → 0.0`：hold=0 时一个来回是 2*leg=10s，t=15 的相位是 5，
# 正落在上边界。Task 1 实测序列 0, 2, 10, 8, 0, 10，已按上两行改正。


def test_ramp_hold_parks_at_the_edge_before_reversing():
    p = {"from": "0", "to": "4", "speed": "4", "hold": "1"}   # 1 秒到顶，停 1 秒
    assert waveform_value("ramp", p, 1.0) == pytest.approx(4.0)
    assert waveform_value("ramp", p, 1.5) == pytest.approx(4.0)   # 还在 hold 里
    assert waveform_value("ramp", p, 2.5) == pytest.approx(2.0)   # hold 结束后反向
```

- [ ] **Step 6: 实现 `ramp`**

把 `_const`/`_sine` 之外的第三行注册补上（`ramp` 是**位置的闭合三角波**，所以可以纯函数化，不靠上一次方向的状态）：

```python
def _ramp(params: dict[str, str]):
    low, high = _num(params["from"]), _num(params["to"])
    speed, hold = _num(params["speed"]), _num(params.get("hold", "0"))
    span = abs(high - low)
    if span == 0:
        return lambda t: low
    leg = span / speed                       # 单程秒数
    cycle = 2 * leg + 2 * hold               # 上→(hold)→下→(hold)
    def value(t: float):
        phase = (t % cycle)
        for start, length in ((0.0, leg), (leg + hold, leg), (2 * leg + 2 * hold, 0.0)):
            if length and start <= phase < start + length:
                ratio = (phase - start) / length
                return low + (high - low) * (ratio if start == 0.0 else 1 - ratio) \
                    if start in (0.0, 2 * leg + 2 * hold) else \
                    high - (high - low) * ratio
        return high if phase < leg + hold else low
    return value
```

- [ ] **Step 7: 跑 `ramp` 测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_waveform.py -v -k ramp`
Expected: 两条 PASS

> **实现者注意**：Step 6 那个 `value()` 写得不干净（三段分支的表达式绕了）。跑通两条测试之后**必须**重写成可读的形状（建议：先算 `phase` 落在 上行/上停/下行/下停 哪一段，再算比例），并保持这两条测试不动——它们是验收，不是脚手架。若重写后测试红了，说明重写改坏了行为，回到上一版。

- [ ] **Step 8: 写 `step` 的失败测试**

```python
def test_step_cycles_through_values_by_interval():
    p = {"values": "10,20,30", "interval": "5"}
    assert waveform_value("step", p, 0.0) == 10
    assert waveform_value("step", p, 4.999) == 10
    assert waveform_value("step", p, 5.0) == 20
    assert waveform_value("step", p, 12.0) == 30
    assert waveform_value("step", p, 15.0) == 10          # 循环回头


def test_step_keeps_spaces_and_empty_slots_out_of_the_sequence():
    assert waveform_value("step", {"values": "1, 2 ,,3", "interval": "1"}, 2.0) == 3
```

- [ ] **Step 9: 实现 `step`**

```python
def _step(params: dict[str, str]):
    raw = [v.strip() for v in str(params["values"]).split(",")]
    values = [_num(v) for v in raw if v != ""]
    interval = _num(params["interval"])
    if not values or interval <= 0:
        raise ValueError(f"step needs values and interval>0, got "
                         f"{brief(params.get('values'))}/{brief(params.get('interval'))}")
    return lambda t: values[int(t / interval) % len(values)]
```

- [ ] **Step 10: 写 `random` 的测试（确定性靠 `seed`，换值节拍靠 `interval`，见 W2）**

```python
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
```

- [ ] **Step 11: 实现 `random`**

`random` 要"按 interval 换值、其余 tick 保持"又要**纯函数**（同 `t` 同值才好测），做法是把区间序号当随机种子的一部分，而不是存状态：

```python
import random as _random

def _random_wave(params: dict[str, str]):
    low, high = sorted((_num(params["min"]), _num(params["max"])))
    interval = _num(params.get("interval", "1.0"))     # §5：interval 是选填，默认 1.0
    seed = params.get("seed", "")

    def value(t: float):
        bucket = int(t / interval)
        # 填了 seed → 同 (seed, bucket) 必须同值，跨进程也要同：种子只由这两者决定。
        # 没填 seed → §5 要求"不填则不重复"，所以掺一份每进程熵（`Random(bucket)` 单独用
        # 是**确定**的，两次运行会给出同一条曲线，那正是 spec 不想要的）。
        key = f"{seed}:{bucket}" if seed else f"{bucket}:{_RUN_SALT}"
        return _random.Random(key).uniform(low, high)   # 取整不在这里做，归 clamp_to_datatype
    return value

_RUN_SALT = f"{os.getpid()}:{_random.SystemRandom().random()}"
```

> **Task 1 落地时改的两处**（评审的变异检查逼出来的，实测记录在 `.superpowers` 报告里）：① 原计划的 `round(picked) if _looks_like_int(low) ...` 分支**永不触发**（`low/high` 恒是 `float`，`str(5.0).isdigit()` 为 False），已连 `_looks_like_int` 一起删除；② 原计划那句"同 seed 同值"的断言是自证式（`{**p,"seed":"7"}` 就是 `p`），改成钉 golden 值（`Random("7:2").uniform(0,100) == 25.14189165270887`）。

> `seed` 为空时用 `Random(bucket)`：同一 `bucket` 在同一次运行里稳定，但两次 pytest 之间不重复（spec §5 的"`seed=空`(不填则不重复)"——注意 Python 的 `Random(int)` 是**确定**的，所以"不重复"要靠 `bucket` 之外的熵。**实现时按 spec 的字面意图办**：不填 seed 就每次运行不同 → 用 `Random((bucket, id(value), os.getpid()))` 这类每进程变化的种子，并给这条单独加一条"两次 `waveform_value` 在同 bucket 上仍相等"的断言来保证稳定性，别把稳定性测丢了。）

- [ ] **Step 12: 跑全模块测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_waveform.py -v`
Expected: 全 PASS，无 skip

- [ ] **Step 13: 实现 `clamp_to_datatype`（spec §8 的钳位）**

```python
_BOUNDS = {
    "int16": (-32768, 32767), "uint16": (0, 65535), "reg16": (0, 65535),
    "int32": (-2147483648, 2147483647), "uint32": (0, 4294967295),
    "int64": (-2**63, 2**63 - 1), "uint64": (0, 2**64 - 1),
    "float32": (-3.4028234663852886e38, 3.4028234663852886e38),
    "float64": (-1.7976931348623157e308, 1.7976931348623157e308),
}

def clamp_to_datatype(value, datatype: str):
    if datatype == "bool":
        return bool(value), False
    if datatype == "string":
        return value, False                      # 字符串没有区间，交给 codec 限长度
    lo, hi = _BOUNDS[datatype]
    if value != value:                            # NaN：钳到下界并报告，别把 NaN 写进寄存器
        return lo, True
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return (int(value) if datatype in ("int16", "uint16", "reg16", "int32", "uint32",
                                       "int64", "uint64") else value), False
```

补测试：

```python
def test_clamp_pins_out_of_range_and_nan_to_the_low_edge():
    from modslave.codec.waveform import clamp_to_datatype
    assert clamp_to_datatype(70000, "uint16") == (65535, True)
    assert clamp_to_datatype(-5, "uint16") == (0, True)
    assert clamp_to_datatype(float("nan"), "float32") == (-3.4028234663852886e38, True)
    assert clamp_to_datatype(12.5, "float32") == (12.5, False)
    assert clamp_to_datatype(1, "bool") == (True, False)
    assert clamp_to_datatype(3.7, "uint16") == (3, False)
```

- [ ] **Step 14: 跑测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_waveform.py -v`
Expected: 全 PASS

- [ ] **Step 15: 提交**

```bash
git add modslave/codec/waveform.py tests/test_waveform.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 波形纯函数与注册表（const/ramp/sine/random/step + 钳位）"
```

---

## Task 2: 值归属状态 `backend/ownership.py`

**Files:**
- Create: `modslave/backend/ownership.py`
- Test: `tests/test_ownership.py`

**Interfaces:**
- Consumes: `modslave.backend.events.PacketEvent`（`channel/direction/raw/unit_id/function_code/summary/mono`）、`modslave.project.model.Point`
- Produces:
  - `class SourceStore`：`own(channel, unit_id, area, address, count=1, who="gui")`、`is_waveform(channel, unit_id, area, address) -> bool`、`restore(channel, unit_id, area, address) -> bool`（没接管过就返回 `False`）、`cleared() -> int`
  - `write_targets(packet) -> list[tuple[int, str, int, int]] | None`：从一条 **rx** 报文里解出"主站写了哪些点"，形如 `(unit_id, area, address, count)`

- [ ] **Step 1: 写失败的测试（SourceStore 默认在波形、能接管、能恢复）**

```python
# tests/test_ownership.py
"""值归属是 M4 唯一的"谁说了算"状态：默认 `waveform`，人工一改就转 `manual`。

spec §7 的理由是现场最费时的误判——平台下发命令后值被波形顶回去，人就去查平台。
这个文件只测状态本身；tick 真不真写寄存器由 `tests/test_session.py` 与集成用例钉。
"""

from modslave.backend.ownership import SourceStore

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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ownership.py -v`
Expected: FAIL —模块不存在

- [ ] **Step 3: 实现 `SourceStore`**

```python
# modslave/backend/ownership.py
"""值归属：每个点是 `waveform` 还是 `manual`。

**只在引擎 loop 线程读写**（`Session` 负责把 GUI 的接管/恢复命令用 `runner.call`
投回来）。这不是洁癖：这条仓库刚被"跨线程共享可变状态"咬过一次（spec §13 那条
GC 死锁），运行态只有一条线程碰，才谈得上"界面卡住也不会把状态写歪"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class SourceStore:
    _manual: set[tuple[str, int, str, int]] = field(default_factory=set)

    def own(self, channel: str, unit_id: int, area: str, address: int,
            count: int = 1, who: str = "gui") -> None:
        for offset in range(max(1, int(count))):
            self._manual.add((channel, unit_id, area, address + offset))

    def is_waveform(self, channel: str, unit_id: int, area: str, address: int) -> bool:
        return (channel, unit_id, area, address) not in self._manual

    def restore(self, channel: str, unit_id: int, area: str, address: int) -> bool:
        key = (channel, unit_id, area, address)
        if key not in self._manual:
            return False
        self._manual.discard(key)
        return True

    def cleared(self) -> int:
        """当前被人工接管的点数（给状态栏/日志一句人话用）。"""
        return len(self._manual)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ownership.py -v`
Expected: 四条 PASS

- [ ] **Step 5: 写主站写报文识别的失败测试**

```python
from modslave.backend.events import PacketEvent
from modslave.backend.ownership import write_targets

def evt(fc: int, raw: bytes, unit: int = 11) -> PacketEvent:
    return PacketEvent("tcp-5020", "rx", raw, unit, fc, "", 0.0)

def test_fc06_write_one_register_is_read_as_that_address():
    # tid=0001 unit=0B fc=06 addr=0004 value=0058
    assert write_targets(evt(6, bytes.fromhex("000100000006" "0b" "06" "0004" "0058"))) \
        == [(11, "hr", 4, 1)]

def test_fc16_write_multiple_expands_over_the_count():
    raw = bytes.fromhex("00010000000b" "0b" "10" "0005" "0004" "08")
    assert write_targets(evt(16, raw)) == [(11, "hr", 5, 4)]

def test_fc05_and_fc15_land_on_the_coil_area():
    assert write_targets(evt(5, bytes.fromhex("000100000006" "0b" "05" "0000" "ff00"))) \
        == [(11, "co", 0, 1)]
    assert write_targets(evt(15, bytes.fromhex("000100000009" "0b" "0f" "0003" "0005" "01"))) \
        == [(11, "co", 3, 5)]

def test_reads_and_unknown_frames_are_not_writes():
    assert write_targets(evt(3, bytes.fromhex("000100000005" "0b" "03" "0000" "0002"))) is None
    assert write_targets(evt(0, b"\x00")) is None
    assert write_targets(PacketEvent("tcp-5020", "tx", b"", 11, 6, "", 0.0)) is None  # 只认 rx
```

- [ ] **Step 6: 实现 `write_targets`**

`parse_packet` 会把异常响应的 `0x80` 脱掉（M1 已钉的契约），所以这里**不能只看 `function_code`**，还要判"这条是请求还是响应"——判据用 `direction == "rx"`（收进来的才是主站的命令）：

```python
_WRITE_FC_AREA = {5: "co", 15: "co", 6: "hr", 16: "hr"}

def write_targets(packet):
    """主站写报文 → `[(unit_id, area, address, count)]`；不是写就返回 None。

    地址是**裸协议地址**（报文里本来就没有 40001 偏置），count 对 05/06 是 1，
    对 15/16 取报文里的数量字段。16 的功能码 `0x10` 与"写 16 个寄存器"的字节 16 同值，
    所以功能码必须从固定偏移取，不能靠值猜。
    """
    if packet.direction != "rx":
        return None
    raw = packet.raw or b""
    area = _WRITE_FC_AREA.get(packet.function_code)
    if area is None or len(raw) < 8:
        return None
    body = raw[7:]                      # MBAP(7B) 之后从功能码开始
    if len(body) < 5:
        return None
    address = int.from_bytes(body[1:3], "big")
    count = 1
    if packet.function_code in (15, 16) and len(body) >= 5:
        count = int.from_bytes(body[3:5], "big")
    if count <= 0:
        return None
    return [(packet.unit_id, area, address, count)]
```

- [ ] **Step 7: 跑全模块测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ownership.py -v`
Expected: 全 PASS

- [ ] **Step 8: 提交**

```bash
git add modslave/backend/ownership.py tests/test_ownership.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 值归属状态与主站写报文识别"
```

---

## Task 3: backend 的 tick 循环，`Row` 带上 `source`

**Files:**
- Modify: `modslave/backend/modbus_tcp.py`（构造参数 `tick_ms`、`_tick_once`、tick 任务起停、`mark_manual`/`manual_keys`、`_packet_sink` 侧的写识别）
- Modify: `modslave/engine/session.py`（`Row` 加 `source`、`start_channel` 把 `tick_ms` 交给 backend、`set_point` 成功后标接管、`restore_waveform`）
- Modify: `modslave/engine/point_source.py`（替身跟上 `Row` 位置契约）
- Modify: `modslave/gui/point_table.py`（"波形"列文案）
- Test: `tests/test_integration_tcp.py`、`tests/test_session.py`、`tests/test_gui_point_table.py`

**Interfaces:**
- Consumes: Task 1 的 `waveform_value(name, params, t)`、`clamp_to_datatype(value, datatype)`；Task 2 的 `SourceStore`、`write_targets(packet)`
- Produces:
  - `ModbusTcpBackend(channel, packet_sink=None, on_connection=None, tick_ms=0)`；`tick_ms=0` 表示**不起 tick**（M1 那条 CLI 冒烟与现有用例的形状不变）
  - `ModbusTcpBackend._tick_once(now: float) -> int`（返回写掉的点数）、`.mark_manual(unit_id, area, address, count, who)`、`.restore_waveform(unit_id, area, address, count) -> bool`、`.source_is_waveform(unit_id, area, address) -> bool`（给 `rows()` 用；**不外泄 `manual_keys()` 这种整表快照**，Task 2 复审的口径：调用方要什么就给什么）
  - `Row` 尾部新增 `source: str = "waveform"`（有默认值，位置契约不变——见 Step 1 的理由）
  - `Session.restore_waveform(channel, unit_id, area, address) -> bool`、`Session.manual_count(channel) -> int`

- [ ] **Step 1: 先钉住"为什么不加位置参数"**

`tests/test_session.py` 里有一条按顺序断言 `Row` 的用例（`point_source.py:52` 的注释也写着"位置参数顺序就是 Row 的契约"）。所以 `source` **必须带默认值并放在最后一位**，不要插进 `waveform` 前后：插了会把 `readonly`/`waveform` 串成一列看着合法的错值，而那种错**测试全绿、现场读错**。

```python
def test_row_source_is_a_trailing_default_not_an_inserted_field():
    """`source` 加在最后且有默认值：老的位置构造不会把它接到 `waveform` 上。"""
    fields = list(dataclasses.fields(Row))
    assert fields[-1].name in ("source",)
    assert fields[-1].default == "waveform"
```

`tests/test_session.py` 顶部补 `import dataclasses`（现有文件里没有）。注意 `Row` 现在以 `stale: bool = False` 收尾，`source` 加在 `stale` **之后**，两者都带默认值。

- [ ] **Step 2: 写 backend 一步 tick 的失败测试（注入时钟，不 `sleep`）**

放进 `tests/test_integration_tcp.py`——因为"寄存器块真的存在"这件事只有在真建起 server 之后才成立，单元测试里造不出这个前提。

```python
def test_a_tick_writes_the_waveform_value_into_the_registers(open_backend):
    """一步 tick 之后，**第三方主站**读到的就是算出来的值。

    选 `CO浓度` 是故意的：它是 `readonly=true`。波形必须能写进只读点（走 raw_writer
    直接改列表），否则"只读的模拟量"这一整类点位（隧道里 CO/VI/照度几乎全是只读）
    就永远冻在初值上。主站写同一个地址仍回异常码 2，那是另一条用例（M1 已钉）。
    时钟由 `_tick_once(now=...)` 注入，不靠 `sleep`：这条要证的是"值算对写对"，
    "定时器会自己响"由下一条钉。
    """
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[0]           # CO浓度 hr0 float32 CDAB
    point.waveform, point.wave_param = "const", {"value": "44.5"}
    with open_backend(project.channels[0]) as backend:
        backend._tick_once(now=0.0)
        client = connect(project.channels[0].port)
        try:
            got = client.read_holding_registers(0, count=2, device_id=11).registers
            assert cdab_to_float(got) == pytest.approx(44.5), got
        finally:
            client.close()


def test_the_tick_task_dies_with_the_backend(engine_runner):
    """stop 之后不许留一个还在写寄存器的任务。

    "通道已经停了、值还在自己动"是这个工具最难查的一类假象：主站读到的是已经不该
    再存在的模拟量。这里断言任务已取消，而不是断言"过一会儿值不再变"——后者要 sleep
    且慢测试会假绿。
    """
    project = tunnel_project(free_port())
    project.channels[0].devices[0].points[0].waveform = "sine"
    project.channels[0].devices[0].points[0].wave_param = {
        "base": "20", "amp": "15", "period": "60"}
    backend = ModbusTcpBackend(project.channels[0], tick_ms=20)
    engine_runner.call(lambda: backend.start(), 10, "test open")
    try:
        assert backend._tick_task is not None and not backend._tick_task.done()
    finally:
        engine_runner.call(lambda: backend.stop(), 10, "test close")
    assert backend._tick_task is None, "stop 没把 tick 任务收掉"
```

- [ ] **Step 3: 实现 `_tick_once`（在 `ModbusTcpBackend` 上）**

`__init__` 里加三个字段：`self.tick_ms = tick_ms`、`self._sources = SourceStore()`、`self._tick_task: asyncio.Task | None = None`、`self._tick_started = 0.0`；`start()` 成功尾部把点位摊平成一份**只读的工作表**（每次 tick 不再遍历 project 对象树，与 `Session.load()` 一次建索引同一个理由）：

```python
        self._wave_points = [(dev.unit_id, pt) for dev in self.channel.devices
                             for pt in dev.points]
```

一步 tick：

```python
    def _tick_once(self, now: float) -> int:
        """把"还在波形手上"的点算一遍值写进寄存器，返回写掉的点数。

        `t = now - self._tick_started`：重新起通道曲线就从 0 相位开始，改系统时间
        影响不到它（spec §5）。`now` 由调用方给，测试才注得进固定时间戳。
        """
        written = 0
        t = now - self._tick_started
        for unit_id, pt in self._wave_points:
            if not self._sources.is_waveform(self.channel_name, unit_id, pt.area,
                                             pt.address):
                continue                                  # spec §7：被接管就一个字都不写
            if pt.waveform in ("", "const") and not pt.wave_param:
                continue                                  # W4：空 const 不参与
            value = waveform_value(pt.waveform, pt.wave_param, t)
            value, clamped = clamp_to_datatype(value, pt.datatype)
            if clamped:
                self._report_clamp(unit_id, pt, value, now)
            registers = encode(value, pt.datatype, pt.word_order)
            if area_is_bit(pt.area):
                self._writer.write_bits(unit_id, pt.area, pt.address,
                                        bool(registers[0]))
            else:
                self._writer.write_registers(unit_id, pt.area, pt.address,
                                             list(registers))
            written += 1
        return written
```

要点，逐条都要实现到位（都是会被现场咬的那种）：

1. **两个写入口不许混用**：`hr/ir` 走 `write_registers`，`co/di` 走 `write_bits`。`raw_writer._check_area_kind` 的实测说明写着——拿 `write_registers` 写 `co` 会返回 True 而点亮的却是**线圈 1**（寄存器 0 的第 0 位），"引擎想关风机、写成开"就是这里来的。`_tick_once` 里必须按 `area_is_bit(pt.area)` 分支。
2. `_writer` 在 `stop()` 之后是 `None`（`_require_writer` 会抛"通道没在跑"），所以 tick 任务**必须在 `stop()` 里先取消**，否则取消与 stop 的顺序一反就变成"停通道时崩一次"。
3. `write_registers` 返回 `False` 表示地址不在块内（点表与 SimDevice 不一致的兜底情况），**静默跳过继续下一个点**：一个点写不到不许把整轮 tick 打断，那会让同通道其余点位一起冻住。
4. `encode` 抛的 `ValueError`（比如 `string` 超长）**只跳过该点并留一条去重后的日志**，别让它冒到 asyncio 任务里——任务里未捕获异常会让 tick 静默死掉，表现正是"波形停了但通道还在跑"。
5. `random` 的保持性由 `t` 落在哪个 `bucket` 决定，纯函数天然成立，不需要额外状态。

- [ ] **Step 4: 起停 tick 任务**

```python
    async def _tick_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._tick_once(time.monotonic())
```

`start()` 在 `_await_listener` 成功、`self._writer` 就位之后：`self._tick_started = time.monotonic()`，且只有 `self.tick_ms > 0` 才 `self._tick_task = asyncio.create_task(self._tick_loop(self.tick_ms / 1000.0))`。`stop()` 里第一件事就是 `if self._tick_task: self._tick_task.cancel(); self._tick_task = None`（在 `server.shutdown()` 之前，见要点 2）。`tick_ms=0` 默认不起任务——**M1 的 CLI 冒烟与现有全部用例的形状因此不变**。

- [ ] **Step 5: `Session` 把 `tick_ms` 交下去，并把接管记进 backend**

- `Session.start_channel`：`ModbusTcpBackend(channel, packet_sink=..., tick_ms=self._project.tick_ms)`。
- `Session.set_point` 写成功后：把这次写当成"人工接管"——`runtime.backend.mark_manual(unit_id, area, address, count=len(registers), who="gui")`。**写法**：`set_point` 的寄存器写是 `self._runner.call(lambda: _awrite(...))` 投回 loop 线程执行的，`mark_manual` 必须在**同一个 lambda 里之后**做（不要在主线程直接调），否则就是往 loop 线程的状态里插一脚。
- `ModbusTcpBackend._packet_sink` 的回调里（跑在 loop 线程，与 `SourceStore` 同线程）：收到 rx 写报文后**不许**把解出的区间原样 `own()` 进去，必须**按点位跨度落到点上**——spec §7 说的是"那个点"切 `manual`，不是"那几格寄存器"：
  ```python
  for unit, area, address, count in write_targets(event) or ():
      for point_unit, pt in self._wave_points:
          if point_unit != unit or pt.area != area or pt.readonly:
              continue
          if address <= pt.address < address + count:      # 写落在点的跨度内（含起于中间）
              self._sources.own(self.channel_name, unit, pt.area, pt.address,
                                count=pt.span, who="master")
  ```
  三条判据各钉一件事：① **`pt.span` 展开**——主站从 4 个字节的 `uint64` 中间那一格写起时，按裸地址记会让点的基础格仍留在波形手上，下一 tick 就把整点顶掉（评审实测）；② **`pt.readonly` 跳过**——主站写只读点会被协议拒绝（异常码 2），但 rx 报文照样解码成"写"，不过滤就会出现"一次没成功的写把波形停了"，而引擎侧 `raw_writer` 是能写只读点的，症状更难查；③ 落在点跨度外的地址（点表没有的格子）直接忽略，不往状态里塞键。
- `_packet_sink` 一次拿到的可能是**半个帧或两个帧粘在一起**（pymodbus 在组帧前就 trace）。`write_targets` 只解**一个**帧，所以这里要按 MBAP 长度字段自己切：`while len(buf) >= 8 and len(buf) >= 6 + buf[4]` 那样先切帧再逐帧解；切不出来的余量留给下一包。**这条不做的话"接管"会漏判**，且漏得没有声音。
- `Session.restore_waveform(channel, unit_id, area, address)`：**签名只有基础地址，跨度要在这里自己算**——`Session` 有摊平的点表索引，取出该点的 `pt.span` 再 `self._runner.call(lambda: runtime.backend.restore_waveform(..., count=pt.span), _WRITE_TIMEOUT, f"restore waveform {area} {address} (unit {unit_id})")`。Task 2 的 `SourceStore.restore(..., count=1)` 与 `own` 对称：少传跨度就会让一个 `uint64` 恢复后仍留 3 格是 `manual`，状态栏的接管计数永远回不了零。把结果发成一条 `LogLine`（恢复了什么要点名，别静默把值交还波形）。
- `runtime.running is False` 时调用这三条，一律按现有"通道没在跑"的拒绝口径返回，不许抛 `AttributeError`（`_require_writer` 那条已钉过同形问题）。

- [ ] **Step 6: `rows()` 把 `source` 带出去**

`Row(..., waveform=pt.waveform, source="manual" if not backend.source_is_waveform(unit_id, area, address) else "waveform")`，通道没起时一律 `"waveform"`（ζ：没起时显示的是点表初值，标"已停"是界面在演一出没发生的接管）。

读侧口径：`rows()` 从主线程调，这里**直接读** backend 的集合成员判定，与既有的 `_read_back`（主线程直接 `runtime.backend.read_bits(...)`）同一个口径——两者都是无 `await` 的纯内存读；**写侧**（`mark_manual` / `restore_waveform` / tick）全部只在 loop 线程发生。

- [ ] **Step 7: 界面"波形"列文案**

`gui/point_table.py`：该列显示 `sine`；当 `row.source == "manual"` 时显示 `已停`。宽度已定 `Column("波形", 96)`，`sine`/`已停` 都装得下（**不许为它加宽、不许截成省略号**）。

- [ ] **Step 8: 跑受影响模块**

Run: `.venv/Scripts/python.exe -m pytest tests/test_integration_tcp.py tests/test_session.py tests/test_gui_point_table.py tests/test_packets.py -q`
Expected: 全 PASS

- [ ] **Step 9: 提交**

```bash
git add modslave/backend/modbus_tcp.py modslave/engine/session.py modslave/engine/point_source.py modslave/gui/point_table.py tests/test_integration_tcp.py tests/test_session.py tests/test_gui_point_table.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: backend tick 驱动波形写寄存器，接管状态上到界面"
```

---

## Task 4: 钳位与运行期日志去重

**Files:**
- Modify: `modslave/backend/ownership.py`（新增 `LogDedupe`）
- Modify: `modslave/backend/modbus_tcp.py`（`log_sink` 参数 + `_report_clamp`）
- Modify: `modslave/engine/session.py`（把 `log_sink` 接到 `publish_log("warning", ...)`）
- Modify: `tests/conftest.py`（`open_backend` 透传 `tick_ms`/`log_sink`，Task 7 要用）
- Test: `tests/test_ownership.py`、`tests/test_integration_tcp.py`

**Interfaces:**
- Consumes: `clamp_to_datatype`（Task 1）、`LogLine(level, text)` / `Session.publish_log`
- Produces: `LogDedupe.report(kind: str, key: tuple, text: str, now: float) -> str | None`（该发就返回消息，被抑制就返回 `None`，窗口 60s）；`ModbusTcpBackend(..., log_sink=None)`

- [ ] **Step 1: 写 `LogDedupe` 的失败测试**

```python
def test_the_same_point_and_kind_is_reported_once_per_sixty_seconds():
    """飘出边界的曲线每 100ms 一条，日志窗两分钟就被同一条刷屏刷死（spec §8）。"""
    from modslave.backend.ownership import LogDedupe

    dedupe = LogDedupe()
    key = (11, "hr", 0)
    assert dedupe.report("clamp", key, "CO浓度 clamped", 0.0) == "CO浓度 clamped"
    assert dedupe.report("clamp", key, "CO浓度 clamped", 1.0) is None
    assert dedupe.report("clamp", key, "CO浓度 clamped", 59.0) is None
    assert dedupe.report("clamp", key, "CO浓度 clamped", 61.0) == "CO浓度 clamped"
    # 另一个点是另一件事：风机与照明不能互相把对方的报错吃掉
    assert dedupe.report("clamp", (12, "hr", 0), "回路 clamped", 61.5)


def test_different_kinds_on_the_same_point_do_not_share_a_window():
    from modslave.backend.ownership import LogDedupe

    dedupe = LogDedupe()
    assert dedupe.report("clamp", (11, "hr", 0), "a", 0.0)
    assert dedupe.report("encode", (11, "hr", 0), "b", 0.5)      # 不同类，各报各的
```

- [ ] **Step 2: 实现 `LogDedupe`**

```python
@dataclass
class LogDedupe:
    """同点同类的运行期错误 60s 只留一条；`window` 是给测试压成 0 用的。"""

    window: float = 60.0
    _last: dict[tuple[str, tuple], float] = field(default_factory=dict)

    def report(self, kind: str, key: tuple, text: str, now: float):
        stamp = self._last.get((kind, key))
        if stamp is not None and now - stamp < self.window:
            return None
        self._last[(kind, key)] = now
        return text

    def reset(self) -> None:
        self._last.clear()
```

- [ ] **Step 3: 写"钳位真的报了、且走 `log_sink`"的失败测试**

```python
def test_a_clamped_waveform_logs_one_line_naming_the_point(open_backend):
    """`base=20;amp=1e6` 压在 float32 上合法，但 uint16 点这么配就天天飘出边界。

    两条都钉：值必须落在边界上（不许写个 70000 掩码成 4464 出去），且日志里点名
    是哪个点钳的——只写"clamped"的操作员等于没看日志。
    """
    seen: list[tuple[str, str]] = []
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[2]        # 状态字 hr4 uint16
    point.waveform, point.wave_param = "const", {"value": "70000"}
    with open_backend(project.channels[0], tick_ms=0,
                      log_sink=lambda level, text: seen.append((level, text))) as backend:
        backend._tick_once(now=0.0)
        client = connect(project.channels[0].port)
        try:
            assert client.read_holding_registers(4, count=1, device_id=11).registers == [65535]
        finally:
            client.close()
    assert seen and seen[0][0] == "warning" and "状态字" in seen[0][1], seen
```

- [ ] **Step 4: 在 backend 里接上**

```python
    def _report_clamp(self, unit_id, pt, value, now) -> None:
        if self._log_sink is None:
            return
        text = (f"{pt.name}: waveform value outside {pt.datatype}, "
                f"clamped to {value}")
        if (line := self._dedupe.report("clamp", (unit_id, pt.area, pt.address),
                                        text, now)) is not None:
            self._log_sink("warning", line)
```

`Session.start_channel` 建 backend 时传 `log_sink=lambda level, text: self.publish_log(level, text)`（`publish_log` 内部就是 `_publish(LogLine(...))`，已经吞订阅者异常，安全）。`encode` 抛错的那条走同一张表，`kind="encode"`。

- [ ] **Step 5: 跑测试并提交**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ownership.py tests/test_integration_tcp.py tests/test_session.py -q`
Expected: 全 PASS

```bash
git add modslave/backend/ownership.py modslave/backend/modbus_tcp.py modslave/engine/session.py tests/conftest.py tests/test_ownership.py tests/test_integration_tcp.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 波形越界钳位并同点同类 60s 不重复刷屏"
```

---

## Task 5: 恢复波形（右键一条命令）

**Files:**
- Modify: `modslave/engine/session.py`（`restore_waveform`）
- Modify: `modslave/gui/point_table.py`（右键菜单）
- Modify: `modslave/gui/main_window.py`（接线：菜单动作 → `runner.call` 投递）
- Test: `tests/test_session.py`、`tests/test_gui_main_window.py`

**Interfaces:**
- Produces: `Session.restore_waveform(channel, unit_id, area, address) -> bool`（True=真的把一次接管还回去了）；`point_table` 暴露 `row_key(index) -> tuple | None` 与 `contextMenuEvent`

- [ ] **Step 1: 引擎侧测试**

```python
def test_restoring_puts_the_point_back_under_the_waveform(session):
    """接管 → 波形不顶 → 恢复 → 波形又拿回去，整条路要能在一个用例里走完。

    tick 是 backend 的事、住在 loop 线程，所以这里用 `runner.call` 走一步；`now` 固定
    给 0.0，`const` 波形在任何 `t` 上都是同一个值，断言与真实时钟无关。
    """
    project = loaded(free_port())
    p = next(pt for pt in project.channels[0].devices[0].points if pt.name == "风机状态")
    p.waveform, p.wave_param = "const", {"value": "77"}
    session.load(project)
    session.start_channel("tcp-5020")
    backend = session.channel("tcp-5020").backend
    tick = lambda: session._runner.call(lambda: backend._tick_once(0.0), 10, "test tick")
    try:
        assert session.set_point("tcp-5020", 11, "hr", 4, "5").ok
        tick()
        assert value_of(session, "风机状态") == "5"          # 被接管，波形一个字都不写
        assert session.restore_waveform("tcp-5020", 11, "hr", 4) is True
        tick()
        assert value_of(session, "风机状态") == "77"         # 还给波形就重新动
        assert session.restore_waveform("tcp-5020", 11, "hr", 4) is False
    finally:
        session.stop_channel("tcp-5020")


def test_restore_on_a_stopped_channel_is_a_named_refusal_not_an_attribute_error(session):
    """没起的通道上点"恢复波形"：给一句带点位名的拒绝，别从 Qt 槽里抛 traceback。

    与 β（拒绝要有名字）同口径：`backend` 这时是 None，裸调就是 `AttributeError`，
    界面表现为"点了没反应"。
    """
    session.load(loaded(free_port()))
    lines: list[LogLine] = []
    session.subscribe(lambda e: isinstance(e, LogLine) and lines.append(e))
    assert session.restore_waveform("tcp-5020", 11, "hr", 4) is False
    assert any("not running" in l.text and "风机状态" in l.text for l in lines), lines
```

- [ ] **Step 2: 实现（跨线程只走 `runner.call`）**

```python
    def restore_waveform(self, channel: str, unit_id: int, area: str, address: int) -> bool:
        """把一次人工接管还给波形。命令投回 loop 线程执行，读改写都在那一条线程上。"""
        self._require(channel)
        return bool(self._runner.call(
            lambda: self._sources.restore(channel, unit_id, area, address),
            _WRITE_TIMEOUT, f"restore waveform {area} {address} (unit {unit_id})"))
```

恢复成功后立刻发一条 `PointChanged`（或至少 `LogLine`），让 10Hz 的下一次心跳能解释那一格为什么又变成波形值——**别静默把值改掉**，操作员会以为是自己点错了。

- [ ] **Step 3: 界面右键**

`point_table` 上给一行加右键菜单，只两项：**`恢复波形`**（`source == "manual"` 时 enabled，否则置灰但**不隐藏**——隐藏会让菜单高度变来变去）与 **`属性…`**（Task 6）。菜单文字里带上点位名（`恢复波形：风机状态`），因为一次只可能恢复右键那一条。

- [ ] **Step 4: 写界面用例（几何不许动 + 拒绝要有名字）**

```python
def test_the_row_menu_offers_restore_only_for_taken_over_rows(make_window, qapp):
    """置灰而不是删掉：菜单项增删会让弹框高度变，两次右键两种样子。"""
    window = make_window()
    relaunch(window, window.session.project, qapp)
    window.point_model.setData(window.point_model.index(row_of(window, "风机状态"),
                                                        VALUE_COLUMN), "42")
    qapp.processEvents()
    actions = window.point_table.row_menu_actions(row_of(window, "风机状态"))
    assert [a.text() for a in actions] == ["恢复波形：风机状态", "属性…"]
    assert actions[0].isEnabled() is True
    other = window.point_table.row_menu_actions(row_of(window, "CO浓度"))
    assert other[0].isEnabled() is False         # 没被接管的行：看得见，点不动
```

- [ ] **Step 5: 跑测试并提交**

Run: `.venv/Scripts/python.exe -m pytest tests/test_session.py tests/test_gui_main_window.py -q`
Expected: 全 PASS

```bash
git add modslave/engine/session.py modslave/gui/point_table.py modslave/gui/main_window.py tests/test_session.py tests/test_gui_main_window.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 右键恢复波形，接管过的点能还给波形且界面给得出名字"
```

---

## Task 6: 点位属性面板

**Files:**
- Create: `modslave/gui/point_props.py`
- Modify: `modslave/gui/main_window.py`（`属性…` 动作）
- Test: `tests/test_gui_point_props.py`

**Interfaces:**
- Consumes: `project.model.Point/Device/Project`、`project.validate.validate_project`（**判定不许在面板里抄一份**，与通道面板 `apply_edits` 同一分工）、`project.model.word_orders_allowed(datatype)`、`project.model.parse_value(datatype, text)`、`gui/point_table.conventional(area, address)`
- Produces:
  - `PointPropertiesDialog(parent, project, key)`，`exec()` 后 `dialog.applied` 为改动字典或 `None`
  - 面板自己的成员（测试要用的就是这些，别另造名字）：`address_edit: QLineEdit`、`name_edit`、`initial_edit`、`readonly_check`、`wave_param_edit`、`notice: QLabel`（"重启通道后生效"那句写这里）、`point -> Point`（当前编辑中的副本）、`error_lines() -> list[str]`、`pick_area(area: str) -> None`、`apply() -> bool`、`geometry_snapshot() -> tuple`（沿用 `channel_panel.geometry_snapshot()` 的写法：只含矩形，不含可见性变化）
  - `key` 的形如 `(channel, unit_id, area, address)`，与 `Row.key` 同源（`session.rows()` 给的就是它）

**面板字段**：名称 / 区(`co|di|hr|ir` 四个平铺单选，**不用下拉**) / 地址 / 惯例地址（4x 只读显示） / 类型（11 项，`DATATYPES`）/ 字序（按 `word_orders_allowed` 收窄）/ 初值 / 波形（五项平铺）/ 波形参数（一行 `k=v;k=v`，用 `format_wave_param`/`parse_wave_param`）/ 只读。

- [ ] **Step 1: 写回滚口径的失败测试**

```python
# tests/test_gui_point_props.py
"""属性面板改的是点表本体：验不过就整体回滚，工程里不许留下一个"看着改了"的半状态。

口径沿用 `gui/channel_panel.apply_edits`：面板只做**转换**（文字→类型），判定全在
`validate_project` 那一份里；先写回工程再验，验出新增错误就整体还原。

窗口夹具在这里自建（`make_window` 住在 `tests/test_gui_main_window.py`，跨文件借夹具
就是把两个文件的变更绑死，M2 里那种"改一个连累一片"就是这么来的）。
"""

import pathlib

import pytest
from PySide6 import QtWidgets

from modslave.csv_io_entry import load_point_table
from modslave.engine.packets import PacketStore
from modslave.engine.session import Session
from modslave.gui.point_props import PointPropertiesDialog
from modslave.gui.main_window import MainWindow

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = str(ROOT / "examples" / "covi-tunnel.csv")
KEY = ("tcp-5020", 11, "hr", 4)          # 状态字：可写、uint16、邻居是 5 上的 uint64


@pytest.fixture
def window(qapp, engine_runner):
    project, _notes, issues = load_point_table(CSV)
    assert not [i for i in issues if i.level == "error"], [str(i) for i in issues]
    store = PacketStore()
    session = Session(runner=engine_runner, packet_store=store)
    win = MainWindow(session, store)
    win.use_project(project, CSV)
    win.resize(1200, 700)
    win.show()
    qapp.processEvents()
    yield win
    win.timer.stop()
    win.session.shutdown()
    win.deleteLater()
    qapp.processEvents()


def test_applying_an_illegal_address_change_is_rolled_back_wholesale(qapp, window):
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    before = dialog.snapshot_point()
    dialog.address_edit.setText("0")            # CO浓度 是 float32，在 0 占两格 → 撞上
    dialog.apply()
    assert dialog.snapshot_point() == before, "被拒的改动留在工程里了"
    assert dialog.error_lines(), "回滚了却没给任何理由"
    assert not window.session.dirty, "被拒的改动不该留下未保存标记"
```

- [ ] **Step 2: 写"需重启通道"的失败测试**

```python
def test_the_panel_says_the_change_needs_a_channel_restart(qapp, window):
    """SimDevice 是 start_channel 时建的：改区/地址/类型/字序不重启就不生效。

    这条标注是给现场看的——最坏的形状是"改了地址、从站还在旧地址应答、界面不说"。
    """
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.address_edit.setText("20")
    dialog.apply()
    assert "重启通道" in dialog.notice.text(), dialog.notice.text()
```

- [ ] **Step 3: 写几何与可见性的失败测试**

```python
def test_the_panel_geometry_is_identical_across_every_state(qapp, window):
    """切区/切类型会让字序、波形参数那些控件的可用性变，但一个矩形都不许挪。"""
    dialog = PointPropertiesDialog(window, window.session.project, KEY)
    dialog.resize(460, 520)
    dialog.show()
    qapp.processEvents()
    baseline = dialog.geometry_snapshot()
    for area in ("co", "di", "hr", "ir"):
        dialog.pick_area(area)
        qapp.processEvents()
        assert dialog.geometry_snapshot() == baseline, area
```

- [ ] **Step 4: 实现面板**

必须做到的几点（逐条都有历史事故）：

1. 类型改成 `bool` 时初值框里的文字要能被 `parse_value("bool", ...)` 吃下，否则在 `apply()` 里给一条带点位名的拒绝，不静默清成 `false`。
2. 字序控件按 `word_orders_allowed(datatype)` 收窄到合法集合：`float64/int64/uint64` 只给 `ABCD/DCBA`、`string` 只给 `ABCD`。收窄的是**合法选项**，不是靠隐藏控件（隐藏即改布局）；不可选的项置灰。
3. `区` 改了之后，地址含义要跟着换区，`4x` 那列的显示同步重算（`conventional(area, address)`）。
4. 波形参数的校验**复用** `validate_project`：面板把改动写回工程副本 → 全量校验 → 与改前的 error 清单做差集，只报"新增的那几条"（与 `channel_panel.apply_edits` 同一写法，照它的 `_errors()` 口径走，不许抄第二份判定）。
5. 只有整体应用成功才 `session.mark_dirty()`；被拒不改工程、不留脏标记。
6. 面板关掉后不持有 `Point` 引用（避免跨线程与 GC 上的意外；spec §12/§13 那条 GC 口径在界面上同样成立：面板控件只在主线程被创建和销毁）。

- [ ] **Step 5: 接线到主窗**

`main_window` 里 `属性…` → `PointPropertiesDialog(...).exec()` → 返回非 `None` 则 `session.load(project)` 重新装载（这样树/表/面板三处一次对上）+ 状态栏一句"已改 N 处，重启通道后生效"。`IssueDialog` 的模态与 offscreen 的关系沿用既有做法（用例里打桩，不真弹）。

- [ ] **Step 6: 跑测试并提交**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gui_point_props.py tests/test_gui_main_window.py -q`
Expected: 全 PASS

```bash
git add modslave/gui/point_props.py modslave/gui/main_window.py tests/test_gui_point_props.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 点位属性面板，改动过校验才落地并写明需重启通道"
```

---

## Task 7: 真主站验收 + 文档回写

**Files:**
- Modify: `tests/test_integration_tcp.py`
- Modify: `docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md`（§7/§11/§12）
- Modify: `docs/superpowers/plans/2026-10-11-modslave-m4-waveform.md`（勾选与偏离表）

- [ ] **Step 0: 夹具先把 `tick_ms` 透出来**

`tests/conftest.py` 的 `open_backend` 已经是 `_open(channel, packet_sink=None, on_connection=None)`；Task 4 加了 `tick_ms`/`log_sink` 之后签名变成 `_open(channel, packet_sink=None, on_connection=None, tick_ms=0, log_sink=None)`，内部原样交给 `ModbusTcpBackend(...)`。**这一步在 Task 4 做过，这里只是确认它存在**——Task 7 三条用例都靠 `tick_ms` 让真 tick 跑起来。

- [ ] **Step 1: 真主站读一条动起来的 `sine`**

```python
def test_a_real_master_reads_a_running_sine_wave(open_backend):
    """曲线必须是**主站侧**看得见：界面自己刷是一回事，网关读到的是另一回事。

    周期 1.2s、`tick_ms=50`，采 4 次每次间隔 0.4s：区间要守住，而且不能是一串常数
    （常数就是"tick 没在跑"或"值被谁顶住了"的形状）。`CO浓度` 是 CDAB 浮点，正好把
    字序一起验了。
    """
    project = tunnel_project(free_port())
    point = project.channels[0].devices[0].points[0]
    point.waveform, point.wave_param = "sine", {"base": "100", "amp": "20",
                                                "period": "1.2"}
    with open_backend(project.channels[0], tick_ms=50) as backend:
        client = connect(project.channels[0].port)
        seen = []
        try:
            for _ in range(4):
                seen.append(client.read_holding_registers(0, count=2,
                                                          device_id=11).registers)
                time.sleep(0.4)
        finally:
            client.close()
    values = [cdab_to_float(pair) for pair in seen]
    assert all(80.0 <= v <= 120.0 for v in values), values
    assert len({round(v, 1) for v in values}) >= 3, f"曲线没在动：{values}"
```

- [ ] **Step 2: 主站下发不被波形顶回去（spec §7 的验收，也是 W1 的验收）**

```python
def test_a_master_write_survives_the_waveform_tick(open_backend):
    """给 `hr4` 配一条 0.4 秒一圈的 `sine`，主站写 333 进去，0.5 秒后必须还是 333。

    这条红了就是 M4 的核心承诺破了：平台下发成功、值被模拟器顶掉，运维会去查平台
    而不是查这里。0.5s 在 `tick_ms=50` 下跨 10 个 tick、1.25 个周期，够证"没被覆盖"。
    """
    project = tunnel_project(free_port())
    point = next(pt for pt in project.channels[0].devices[0].points if pt.name == "状态字")
    point.waveform, point.wave_param = "sine", {"base": "0", "amp": "10",
                                                "period": "0.4"}
    with open_backend(project.channels[0], tick_ms=50):
        client = connect(project.channels[0].port)
        try:
            client.write_register(4, 333, device_id=11)
            time.sleep(0.5)
            assert client.read_holding_registers(4, count=1,
                                                 device_id=11).registers == [333]
        finally:
            client.close()
```

- [ ] **Step 3: `co` 上的下发同测（走 `write_bits`，不是 `write_registers`）**

```python
def test_a_master_write_of_a_coil_survives_the_waveform_tick(open_backend):
    """位区单独走一条：`write_registers` 写 `co` 会写错一位（raw_writer 的实测说明）。

    `启动` 线圈配 `step;1,0;interval=0.2`——不接管的话它每 0.2 秒翻一次，主站读到的是
    一直在抖的启动指令。写 1 之后 0.5 秒再读，必须仍是 1。
    """
    project = tunnel_project(free_port())
    coil = next(pt for pt in project.channels[0].devices[0].points if pt.area == "co")
    coil.waveform, coil.wave_param = "step", {"values": "1,0", "interval": "0.2"}
    with open_backend(project.channels[0], tick_ms=50):
        client = connect(project.channels[0].port)
        try:
            client.write_coil(coil.address, True, device_id=11)
            time.sleep(0.5)
            assert client.read_coils(coil.address, count=1, device_id=11).bits[0] is True
        finally:
            client.close()
```

- [ ] **Step 4: 全量与变异检查**

Run: `.venv/Scripts/python.exe -m pytest`（**串行，一次只跑一份**）
Expected: 全 PASS（基线 773，只增不减），无 skip / xfail

变异检查（做完再撤，逐条记下"改坏它时是否真变红"）：
1. `_tick_once` 里去掉 `is_waveform` 判定 → Step 2/3 两条必须红。
2. `write_targets` 里把 `direction != "rx"` 那条判掉 → 主站写不进 manual（Step 2 红或响应帧把点误标）。
3. `clamp_to_datatype` 直接返回原值 → Task 4 的钳位用例红。
4. `ramp` 的 `hold` 改成恒 0 → Step 6 的 hold 用例红。

任何一条"改坏了还全绿"，就是那条用例是假的——把它写结实再继续（M2 有过同样一轮：`readonly` 恒 True 时全量 468 条全绿）。

- [ ] **Step 5: 人工冒烟清单（要他亲手做，自动化替不了）**

```
.venv\Scripts\python.exe -m modslave examples\covi-tunnel.csv --gui
```

1. 把 `CO浓度` 配成 `sine;base=20;amp=15;period=60`，看那一格每 `tick_ms` 一跳、数值按曲线走；
2. 双击改 `CO浓度` 的值 → 波形列那一格变成 `已停`，且**不再自己动**；
3. 右键该点 `恢复波形：<点名>` → 又动起来，日志里有一条说得清的接管/恢复记录；
4. 另开主站进程 `write_register(4, 88)`，界面上 `风机状态` 变 88 且**不被波形顶回去**（该点配了波形时也要成立）；
5. 属性面板把 `VI风速` 的地址从 2 改到 0（与 `CO浓度` 撞）→ 面板给出一条带点名的重叠错误、整体回滚、标题不带 `*`；
6. 属性面板改一个合法地址 → 面板写着"重启通道后生效"，此时从站**仍在旧地址应答**（这是预期），停止再启动后新地址生效；
7. 全流程里窗口布局不因为状态变化挪动过（`●/▲`、`已停`、菜单置灰都不许推挤控件）。

- [ ] **Step 6: 文档回写并提交**

spec 里三处：
- §7 把"`source` 规则"从设计描述改成已实现口径（含"主站写入也切 manual，识别在 rx 报文侧"）；
- §11 的 M4 标已完成，写用例数与 commit；
- §12 补 M4 已实测条目：`Random(seed=int)` 是确定的（所以"不填 seed 就不重复"必须靠每进程熵，不能只 `Random(bucket)`）、`SimRuntime` 写位/写寄存器两条接口混用会写错位（引用 raw_writer 的实测说明）、`hold` 的三角波周期公式。

```bash
git add tests/test_integration_tcp.py docs
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "test: 真主站验收波形与下发不被顶，回写 M4 实测结论"
```

---

## M4 完成检查表

- [ ] `.venv\Scripts\python.exe -m pytest` 全绿，无 skip / xfail（基线 773，只增不减；**串行跑**）
- [ ] `tests/test_architecture.py` 仍全绿：`waveform.py`/`ownership.py` 里**没有** `PySide6`，`point_props.py` 里**没有** `pymodbus`
- [ ] 五种波形每条都有"注入固定 `t`"的纯函数断言，且 §5 表的每个默认值分支（`phase/hold/interval/seed`）都有用例
- [ ] 主站写 `hr`（06/16）与 `co`（05/15）两条路都有"两个 tick 后值仍是主站写的"集成用例
- [ ] 钳位有日志、且同点同类 60s 内只一条
- [ ] 属性面板：验不过整体回滚、不留脏标记、写明"重启通道后生效"、几何与可见性恒定
- [ ] Task 7 Step 4 的四条变异检查逐条确认"改坏它就变红"
- [ ] Task 7 Step 5 的人工清单由用户当面跑过
- [ ] `git status --short` 为空

## M4 不做（留给后续）

| 内容 | 归属 | 为什么不在这里 |
|---|---|---|
| RTU 串口通道 | M5 | 需要 USB-485 人工冒烟 |
| 波形参数的**逐参数表单**（每个波形自己的输入框） | v1.1 | 现在 `k=v;k=v` 一行够用且校验齐全；逐参数表单要重做一套控件几何不变量，收益不值 |
| 曲线图（点位历史画成折线） | v2 | 明确不做，接口留在 `PacketStore` 之外另建再说 |
| 坏 CRC / 延迟 / 异常码注入 | v2 的缝 | spec §9 已留（`SimDevice.action` / `custom_pdu`），混进 M4 会让"全绿"无从判定 |

---

## 自审（写完这份计划后按 spec 逐条对过）

**1. spec 覆盖**：§5 波形参数表 → Task 1（五种 + 默认值分支）；§7 值归属 → Task 2/3/5；§8 钳位 + 60s 去重 → Task 4；§9 注册表缝 → Task 1 的 `WAVE_BUILDERS`；§10 纯函数注入时间戳 + 真主站集成 → Task 1 / Task 7；§11 M4 含"点位属性面板" → Task 6。**没有漏项**。
**2. 占位符**：Task 1 Step 6 的 `_ramp.value()` 是**故意留了"能跑通但不干净"的实现 + 一条必须重写的指令**（不是 TBD，代码是全的），Step 7 之后有验收。Task 3 的 `_tick_once` 给的是完整代码，但 **Step 5/6 关于 `Session` 的那几处给的是"改哪里 + 判据 + 为什么"而不是整段抄写**——因为 `Session` 已被 M2 撑到 450 行、`set_point` 的 `runner.call` 那段有它自己的异常口径，逐行抄一份"参考实现"会与本仓库实际形状分叉（M1/M2 计划里那些"裁决前快照"代码块就是这么失效的）。执行时以"测试先红后绿 + 要点逐条落到"为准，形状对不上就按仓库现状改，别照抄。
**3. 类型一致性**：`waveform_value(name, params, t)`、`clamp_to_datatype(value, datatype) -> (value, bool)`、`SourceStore.own(channel, unit_id, area, address, count, who)`、`is_waveform(channel, unit_id, area, address)`、`restore(...) -> bool`、`write_targets(packet) -> list[(unit_id, area, address, count)] | None`、`Session.restore_waveform(...) -> bool`、`Row.source: str`（默认 `"waveform"`，位置在最后）——Task 1/2/3/5 之间用名与签名一致。`_tick_once(now)` 全程只用传入的 `now`，不在函数里读 `time`，这样 Task 3/4/7 的确定性测试才成立。
