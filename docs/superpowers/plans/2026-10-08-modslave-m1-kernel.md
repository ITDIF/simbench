# Modbus 从站模拟器 M1（内核地基）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 命令行起一个 Modbus TCP 从站，让第三方主站（QModMaster 或平台网关）按 CSV 点表读到正确数值——含多字节类型的字序、越界读与写只读点的异常响应。

**Architecture:** 五层单向依赖 `gui → engine → backend → project/codec`。本计划只做 `project / codec / backend` 三层加一个 `__main__` 入口：点表模型 → CSV/project.json → 自研 `PointCodec` 转原始 reg16 → 映射成 pymodbus `SimDevice` 四区 → `ModbusTcpServer` 起监听。pymodbus 的 import 只允许出现在 `backend/`。

**Tech Stack:** Python 3.13.9、pymodbus 3.15.0、pytest；GUI 用的 PySide6 6.9.2 本计划不涉及。

**Spec:** `docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md`
**范围:** spec 里程碑 M1。M2 GUI、M3 报文监视窗、M4 波形、M5 RTU 都不在本计划内。

**M1 完成定义：** `.venv/Scripts/python.exe -m pytest` 全绿（含真主站互测），并且
`.venv/Scripts/python.exe -m modslave examples/covi-tunnel.csv --port 5020` 起来后，
QModMaster 连上能读到浮点值、写到只读点收到异常码 2。

---

## 已实测事实（三轮探针结论，照做，别再猜）

探针脚本与原始输出在 `%TEMP%\pmb-venv\`（`probe_simdevice.py`、`probe2_units.py`、
`probe3_direct_write.py`），依赖只装在临时 venv，没动本机环境。

1. **`SimData.count` 是"重复倍数"，不是跨度。** 实际跨度 = `len(build_registers(values)) × count`。
   写 `SimData(0, 10, [0]*10, REGISTERS)` 会占 100 个寄存器并把 20 号点位判成重叠
   （实测 `TypeError: SimData address 20 is overlapping!`）。→ **本计划一律 `count=1` + 值列表。**
2. **四区独立形态可用**：`SimDevice(id=unit_id, simdata=(co, di, hr, ir))`。tuple 的 0/1 两块
   只允许 `DataType.BITS`（否则 `TypeError: not DataType.BITS, not allowed`），且此形态下
   `use_bit_addressing` 被强制为 True，**主站侧按位号寻址，我们不做 ×16 换算**。
3. **`readonly` 同时挡住主站和我们自己**：主站写只读点回异常码 2；更关键的是
   `SimCore.async_setValues(unit, 4, 0, [999])` 也返回 `2` 且值不变（实测
   `async_setValues fc=4 addr0: 2`），而直接改 `SimRuntime.block["i"][2][offset] = 555`
   主站立刻读到 `[555, 666]`。→ **引擎/运行期写寄存器必须走直接改列表那条路**（Task 9）。
4. **不要建 `id=0` 兜底设备**：没有它时未知 unit id 请求走 `SimCore.__get_device` 回落
   `devices[0]` → `KeyError: 0` → 主站收到异常码 4（服务不崩，实测总线上仍然活着）；
   一旦加了 id=0，总线扫描 1..247 会变成 247 台全应答（实测 `{'ok': 247}`），
   这不是我们想要的假现场。→ 保留异常码 4，但要压掉 pymodbus 打的那 492 行 traceback。
5. **寄存器区的未定义地址回异常码 2**（`__create_block` 把空洞填 `DataType.INVALID` 标志），
   所以点表**不需要**把地址铺满，实测 `read_holding_registers(5,10)` 与 `(100,1)` 都是 2。
6. **位区的空洞行为与寄存器区不对称**：同一 16 位寄存器覆盖的未定义线圈可读（回 False）
   可写（实测 `write_coil(6)` 成功、`read_coils(8,8)` 全 False），超出块才回 2（`read_coils(16,4)`）。
   → **重叠校验对位区必须按"位"判**，不能按寄存器判。
7. **pymodbus 自己会查重叠并抛 `TypeError`**，但错误里没有 CSV 行号和点位名。我们的
   `validate.py` 必须自己先查一遍（要"一次报全 + 带行号"），SimDevice 的异常只当兜底断言。
8. `pyserial` 不在 pymodbus 的默认依赖里（`pip show pymodbus` 的 `Requires:` 是空的，
   METADATA 里是 `extra == "serial"`），M5 才用得上，但 `requirements.txt` 先写上。
9. 异常码数字：`2 = ILLEGAL_ADDRESS`、`4 = DEVICE_FAILURE`（`pymodbus/constants.py:43-55`）。
10. client 侧 `count=` / `device_id=` 都是 **keyword-only**：
    `read_holding_registers(0, count=5, device_id=1)`；位置参数写法会
    `TypeError: takes 2 positional arguments but 3 ...`。
11. `ModbusTcpServer(list[SimDevice], framer=FramerType.SOCKET, address=(host, port),
    ignore_missing_devices=False, trace_packet=fn)`；`await server.serve_forever()` 起服务，
    `await server.shutdown()` 停。`trace_packet(is_outgoing: bool, payload: bytes) -> bytes`
    **必须把字节原样返回**，实测双向都能抓到（`RX 00 01 ... 01 03 00 00 00 05` /
    `TX 00 01 ... 01 03 0a ...`）。

### 相对 spec 的四处修正（写计划时逐条比对发现，按本计划执行）

| # | spec 原口径 | 改成 | 原因 |
|---|---|---|---|
| 1 | string 吃四种字序 | string 只支持 `ABCD` | 4 个字母只描述 4 字节置换，8 字节以上无公认定义（与 §6 收窄 64 位同理） |
| 2 | 运行期用 `SimCore.async_setValues` | 直接改 `SimRuntime.block[..][2]` | 事实 3 |
| 3 | 重叠检测统一按寄存器 | 位区按位、寄存器区按寄存器 | 事实 6 |
| 4 | 地址重叠交给校验 | 校验照做，且**不建 id=0 兜底设备** | 事实 4 |

## 执行期已裁决的偏离（评审后确定，评审者按此核对，不要再当跑偏报）

| # | 偏离 | 定死的口径 | 出处 |
|---|---|---|---|
| A | `parse_value` 对 bool 的宽松白名单会静默返回 False | 未识别 token（含空串、`TURE`、`01`、`-1`）一律 `ValueError`；`bool` 只接受 `str` 或 Python `bool` | Task 2 |
| B | `parse_value` 接受 int/float 并截断 | 数值类型只接受 `str`；收到 `int`/`float`/`None` 抛 `ValueError`，禁止静默截断（`project.json` 里手改成 `100.5` 要报错而不是变成 100） | Task 2 |
| C | 浮点越界消息 | NaN 报 `rejects nan`；超上限报 `out of range, \|value\| must be <= {limit}`；整数分支照旧带 `[{lo}, {hi}]` | Task 2 |
| D | `register_span`/`_encode_string` 的 string 分支 | `value` 必须是 `str`，非 ASCII 与内嵌 `\x00` 都抛领域 `ValueError`（不许裸抛 `UnicodeEncodeError`，否则 `validate` 的"一次报全"会变成 traceback） | Task 2/4 |
| E | codec 自己那份 `_as_bool` | 删掉。bool 的编解码一律复用 `model.parse_value(BOOL, ...)`，不许有第二份白名单 | Task 3 |
| F | 64 位 `DCBA` 的期望值 | `[0x0807, 0x0605, 0x0403, 0x0201]`（8 字节整体反转）。原计划测试写的 `[0x0708, ...]` 是字序反转、等同 CDAB，是笔误 | Task 4 |
| G | `decode([], "bool")` 抛 IndexError | bool 分支也走寄存器数校验，抛 `ValueError`，消息与数值分支同句式 | Task 4 |
| H | `WAVEFORMS` 与 `WAVE_SPEC` 两份真相 | `validate.py` 导入期检查两者键集合一致，漂移即 `RuntimeError` | Task 5 |
| I | `_overlap` 的 string 分支 | 统一走带 `try` 的 `register_span`，算不出跨度就跳过（`initial` 已经报过错） | Task 5 |
| J | `decode` 用 `& 0xFFFF` 静默吞脏寄存器 | 超出 `0..65535`、非 `int`、`bool`、非 `list`/`tuple` 容器一律 `ValueError`；掩码只许用在产出侧，入口截断等于静默给错值（`0x12345` 读成 9029 会被当平台 bug 调） | Task 3/4 整改（第一轮） |
| K | 底层 `TypeError`/`OverflowError`/`UnicodeDecodeError` 从 codec 逃出去 | 全部包成领域 `ValueError`：`encode(None,'float32')`、`encode(10**400,'float32')`、`decode([0xE465],'string')`。`UnicodeDecodeError` 本就是 `ValueError` 子类，包它是为了让消息带上 `string` 与原始字节，不是为了能 catch | Task 3/4 整改（第一轮） |
| L | 入口先判字序再判类型，`encode(1,'wchar','CDAB')` 报 `wchar only supports ('ABCD',)` | 类型判定优先于字序判定：`unknown datatype` 先于 `only supports`，错误消息必须指向真正的错。脏寄存器与长度校验仍排在字序之后，`bool`/`int64` 的字序洞不回退 | Task 3/4 整改（第二轮） |
| M | codec 自己抄一份数值类型清单；全 dtype × 全字序没有回归测试 | `_NUMERIC` 由 `DATATYPES` 派生，不许有第二份真相；新增 `tests/test_codec_roundtrip_all.py`：全 dtype × 全允许字序互逆 + 产出个数与 `register_span` 一致 + 各字序必须是互异置换（最后这条是变异测试逼出来的） | Task 3/4 整改（第一/二轮） |
| N | 错误消息把用户原值整段打出来（`encode(10**400,'float32')` 是 401 位十进制） | `{value!r}` 超过 80 字符截断成前 80 字符加 `...`；数值、string、bool、寄存器列表四类消息统一走 `_brief`，异常类型与消息前缀都不许变——Task 5 一次报全不能被一个脏值刷屏 | Task 3/4 整改（第二轮） |
| O | `validate` 里范围比较与 `in dict` 判定拿到手改 project.json 的脏值（`port="COM3"`、`unit_id=[]`、`address=None`）先抛 `TypeError` | 所有范围比较与字典成员判定前先 `_is_int()` gate（`bool` 是 int 子类，同样挡掉）：一个脏值只落一条 error（`port`/`unit_range`/`address`/`tick_ms`），清单里不许有 traceback 逃逸。非整数 `unit_id` 不进 `units` 字典，重复判定一起 gate——`True` 与 `1` 哈希相同，进了表就把合法设备误判成 `unit_duplicated` | Task 5（本轮补齐 `unit_id`） |
| P | `_shape` 判完 `area_type` 就 `return` | 不早退：类型/区不匹配的点位照样占寄存器位。否则它吞掉别的点位的重叠错误，用户要跑两轮启动才报全（`test_area_type_mismatch_still_reserves_its_address` 钉住） | Task 5 |
| Q | `_SINGLE = {BOOL, INT16, UINT16, REG16}` 手抄类型清单 | 由 `model.word_orders_allowed` 派生（“只允许 ABCD 的类型”），validate 里不维护第二份真相（同裁决 M）。`string` 显式排除：它也只允许 `ABCD`，但填别的是 error 不是 warning——单寄存器类型填字序只是没意义，string 填字序是语义分歧 | Task 5 |
| R | 同 `(host, port)` 的两个通道留到启动第二阶段炸 bind（Windows 只回 10048，不带通道名，现场看不出是点表配置错） | 启动前一次报全：`code="port_duplicated"`，`where` 是后加入那个通道名，message 写明 `port 502 on host 0.0.0.0 already used by channel tcp-502`。不同 host 同 port 各自监听，**不算**冲突；非整数/越界 port 已被 `port` 那条拦下，不重复报；非字符串 host 不参与判定 | Task 5（本轮） |
| S | 计划正文 Task 5 Step 1 用 `_p(), _p(name="q", address=1)` 断言 `overlap` | 笔误：紧邻不是重叠（`register_span("uint16") == 1`，事实 5「点表不需要把地址铺满」）。已改成双向钉住——0 与 0 判重叠、0 与 1 判合法 | Task 5 |
| U | 同一 `(host, port)` 起第二个实例时两边都打印"listening 成功"，主站读的仍是**旧点表**（实测叠 3 个 LISTENING、30 次读全拿旧值，杀掉 A 才变 B） | `ModbusTcpBackend.start()` 先把端口交给 pymodbus 之前用**裸 `socket.bind()` 探针**自检，占用即抛 `PortInUseError`（消息含 `host:port` 与"可能是上一个模拟器实例"）。bind 与 listen 之间有 TOCTOU，所以措辞是"可能已被占用"，不说死。判据换成**可失败**的：起第二个必须红，"同端口再起一次也能成功"不算通过 | Task 10/12 |
| V | 日志 filter 按消息前缀 `Datastore unable to fulfill request` 抑制 | 那条是 pymodbus 的**裸 `except Exception`**，datastore 路径上任何内部 bug 都走它——按前缀抑制会把"模拟器自己崩"一起吞掉，而主站收到的异常码 4 与"unit 不在线"在报文上同形。判据收窄到只挡已知的 `KeyError: 0` unit 回落；**内部异常必须仍在日志里**（有测试分别钉住这两侧） | Task 10 |
| W | `read/write_registers` 允许接 `co/di` | 位区的"地址 0"是**线圈 0**，寄存器路径的"地址 0"是寄存器 0 的第 0 位（实测 `write_registers(11,'co',0,[0x0002])` 点亮的是**线圈 1**）。两边都加类别门控，抛 `ValueError` 并指明该用哪个方法；未知区名仍报 `unknown area` | Task 9 |
| X | 校验清单的 `Issue.where` 与 `port_duplicated` 原样拼通道名/设备名/点位名/host | 厂商把整段说明塞进通道名时单行 1244 字符，会把真正那条错误挤出屏幕。身份名走 `model.brief_name()`（**只截断不加引号**，判定逻辑与已钉住的对外句式不动） | Task 5 |
| Y | `test_backend_does_not_touch_gui` 是**恒过守卫**（`ROOT` 就是 `modslave/`，backend 下每个文件都被 `continue` 掉） | 改成显式声明跳过哪一层；并给每条守卫配"该有的人必须有"式防空跑断言 + 扫描器自测；删掉无人使用的死参 `allow_backend`。变异验证：往 backend 塞 `import PySide6`，架构测试必须红 | Task 10 |

**另**：Task 10/11/12 的参考代码块同样是裁决前的快照（Task 11 里 `regs_to_float(CDAB) == 12.5` 与同块 `[0x0000,0x4148]` 自相矛盾——CDAB 必须**换字**才是 12.5；Task 10 的 `await asyncio.sleep(0)` 会把 bind 失败吞成"已启动"）。以仓库与上述 U–Y 为准，不要照抄那三节。

---

## 文件结构

```
modslave-sim/
├── requirements.txt
├── pytest.ini
├── examples/
│   └── covi-tunnel.csv          # 一张能直接跑的示例点表
├── modslave/
│   ├── __init__.py              # 一行 docstring
│   ├── __main__.py              # M1 验收入口：CSV -> 起 TCP 从站
│   ├── project/
│   │   ├── __init__.py
│   │   ├── model.py             # 类型元数据 + Point/Device/TcpChannel/Project
│   │   ├── validate.py          # 启动前全量校验，一次报所有问题
│   │   ├── json_io.py           # project.json 读写
│   │   └── csv_io.py            # CSV 读写
│   ├── csv_io_entry.py          # CSV/工程装载口（CLI 与 GUI 共用：读文件+端口+校验）
│   ├── codec/
│   │   ├── __init__.py
│   │   └── point_codec.py       # 值 <-> 原始 reg16 数组（字序在这层）
│   ├── engine/
│   │   └── __init__.py          # M4 波形/tick 的包，M1 只占位不实现
│   └── backend/
│       ├── __init__.py
│       ├── events.py            # PacketEvent / ChannelState / LogLine / PointChanged
│       ├── simdev.py            # 点表 -> SimDevice/SimData（空区补 INVALID 占位）
│       ├── raw_writer.py        # 直接读写寄存器列表（绕开 readonly；寄存器/位接口不混用）
│       └── modbus_tcp.py        # pymodbus TCP 封装：启动前 bind 自检 + 报文钩子 + 降噪
└── tests/
    ├── __init__.py              # Task 1 Step 5 建的包标记（空文件）
    ├── conftest.py              # 起停后端的事件循环线程夹具
    ├── test_architecture.py     # 分层守卫（AST，含防空跑断言与扫描器自测）
    ├── test_model.py
    ├── test_codec_single.py
    ├── test_codec_32bit.py
    ├── test_codec_64bit.py
    ├── test_codec_roundtrip_all.py
    ├── test_validate.py
    ├── test_json_io.py
    ├── test_csv_io.py
    ├── test_simdev.py
    ├── test_raw_writer.py
    ├── test_integration_tcp.py   # 真主站互测（M1 门禁）
    └── test_cli.py               # 命令行入口 + 子进程真连一次
```

依赖方向由 `tests/test_architecture.py` 守住：`test_only_backend_touches_pymodbus`（pymodbus/serial 只许在 `backend/`）、`test_backend_actually_ownes_the_library_imports`（防空跑：backend 里必须真有人 import）、`test_project_and_codec_do_not_touch_gui` 与 `test_backend_does_not_touch_gui`（下层不碰 Qt）、外加扫描器自测。扫描用 AST 而不是行前缀，所以函数体内的延迟 import 与 `from pymodbus.simulator.simcore import SimCore` 这种完整路径写法都跑不掉。

---

## Task 1: 环境与骨架

**Files:**
- Create: `requirements.txt`、`pytest.ini`
- Create: `modslave/__init__.py`、`modslave/project/__init__.py`、`modslave/codec/__init__.py`、`modslave/backend/__init__.py`、`tests/__init__.py`

- [ ] **Step 1: 建 venv 并装依赖**

```bash
cd /d/project/PythonProject/modslave-sim
python -m venv .venv
.venv/Scripts/python.exe -m pip install -q pymodbus==3.15.0 pyserial pytest
```
Expected: 只有 pip 版本升级提示，无 ERROR。

- [ ] **Step 2: 确认同步 client 可导入**（Task 10 的测试要用它当主站）

```bash
.venv/Scripts/python.exe -c "from pymodbus.client import ModbusTcpClient; from pymodbus.simulator import SimData, SimDevice, DataType; print('ok')"
```
Expected: `ok`。若失败，停下报告——本计划后续任务都以这两个导入为前提。

- [ ] **Step 3: 写 `requirements.txt`**

```
pymodbus==3.15.0
pyserial>=3.5
pytest>=8.0
```

- [ ] **Step 4: 写 `pytest.ini`**

```ini
[pytest]
pythonpath = .
testpaths = tests
addopts = -q
```

- [ ] **Step 5: 建包目录**

```bash
mkdir -p modslave/project modslave/codec modslave/backend tests examples
for f in modslave/__init__.py modslave/project/__init__.py modslave/codec/__init__.py \
         modslave/backend/__init__.py tests/__init__.py; do : > "$f"; done
```

- [ ] **Step 6: 跑收集**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: `no tests ran`，退出码 5（pytest 的"没收集到测试"码）。若退出码是 1 并带
`ImportError`/`ModuleNotFoundError`，说明骨架有问题，先修再往下走。

- [ ] **Step 7: Commit**

```bash
git add requirements.txt pytest.ini modslave tests
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "chore: 建 venv 依赖与包骨架"
```

---

## Task 2: 类型元数据与点表模型

**Files:**
- Create: `modslave/project/model.py`
- Test: `tests/test_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model.py
import struct

import pytest
from modslave.project.model import (
    BOOL, DATATYPES, FLOAT32, FLOAT64, INT16, INT64, REG16, STRING, UINT16,
    UINT64, Device, Point, Project, TcpChannel, area_is_bit, parse_value,
    register_span, struct_format, word_orders_allowed,
)

def test_areas_and_bit_areas():
    assert area_is_bit("co") and area_is_bit("di")
    assert not area_is_bit("hr") and not area_is_bit("ir")

def test_register_span():
    assert register_span(INT16) == 1
    assert register_span(FLOAT32) == 2
    assert register_span(UINT64) == 4

def test_string_span_counts_pairs_rounded_up():
    assert register_span(STRING, "") == 1         # 空串也至少占一个寄存器
    assert register_span(STRING, "a") == 1
    assert register_span(STRING, "ab") == 1
    assert register_span(STRING, "abc") == 2      # 3 字节补到 4
    assert register_span(STRING, "aaaaa") == 3    # 5 字节补到 6

def test_word_orders_allowed_by_width():
    assert word_orders_allowed(FLOAT32) == ("ABCD", "CDAB", "BADC", "DCBA")
    assert word_orders_allowed(UINT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(INT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(FLOAT64) == ("ABCD", "DCBA")
    assert word_orders_allowed(INT16) == ("ABCD",)
    assert word_orders_allowed(STRING) == ("ABCD",)

def test_struct_format_is_big_endian():
    assert struct_format(INT16) == ">h"
    assert struct_format(UINT16) == ">H"
    assert struct_format(REG16) == ">H"
    assert struct_format(FLOAT32) == ">f"
    assert struct_format(FLOAT64) == ">d"
    with pytest.raises(ValueError):               # bool 没有 struct 码
        struct_format(BOOL)

def test_unknown_datatype_rejected():
    with pytest.raises(ValueError):
        register_span("wchar")
    with pytest.raises(ValueError):
        struct_format("wchar")

def test_bool_parses_explicit_whitelists():
    for text in ("1", "true", "yes", "y", "on"):
        assert parse_value(BOOL, text) is True
    for text in ("0", "false", "no", "n", "off"):
        assert parse_value(BOOL, text) is False
    assert parse_value(BOOL, "TRUE") is True      # 大小写与空格吃掉
    assert parse_value(BOOL, " No ") is False

def test_bool_rejects_anything_outside_whitelists():
    # 拼错/多字符/空值一律报错，不许当成 False（静默错值会被当成平台 bug）
    for text in ("TURE", "01", "-1", "maybe", "", "   ", "2", "oneline"):
        with pytest.raises(ValueError):
            parse_value(BOOL, text)

def test_bool_accepts_python_bool_but_not_int():
    assert parse_value(BOOL, True) is True
    assert parse_value(BOOL, False) is False
    for value in (0, 1, None):
        with pytest.raises(ValueError):
            parse_value(BOOL, value)

def test_numeric_types_reject_non_string_input():
    # 以前 parse_value('int16', 42.9) 截断成 42、parse_value('string', 123) 变成 '123'
    for datatype, value in ((INT16, 42.9), (INT16, 42), (UINT64, 1),
                            (FLOAT32, 1.5), (STRING, 123), (REG16, None)):
        with pytest.raises(ValueError):
            parse_value(datatype, value)
    with pytest.raises(ValueError, match="needs a string value"):
        parse_value(INT16, 42.9)
    assert parse_value(INT16, "42") == 42         # 字符串照常工作

def test_float_parses_normal_values():
    assert parse_value(FLOAT32, "12.5") == 12.5
    assert parse_value(FLOAT64, "-2.25") == -2.25

def test_float_rejects_nan_with_nan_message():
    for datatype in (FLOAT32, FLOAT64):
        with pytest.raises(ValueError) as exc:
            parse_value(datatype, "nan")
        # 填的是 nan 才能说 nan；不能把溢出的 inf 也混进这条消息
        assert str(exc.value) == f"{datatype} rejects nan: 'nan'"

def test_float_out_of_range_message_shows_the_limit():
    # 1e309 不是 nan/inf 的拼写问题，是 float64 溢出，消息必须说越界并给上限
    with pytest.raises(ValueError) as exc:
        parse_value(FLOAT64, "1e309")
    assert str(exc.value).startswith(
        "float64 out of range, |value| must be <= 1.7976931348623157e+308:")
    with pytest.raises(ValueError) as exc:
        parse_value(FLOAT32, "3.5e38")
    assert str(exc.value).startswith(
        "float32 out of range, |value| must be <= 3.4028234663852886e+38:")
    with pytest.raises(ValueError, match="out of range"):
        parse_value(FLOAT32, "inf")               # inf 自然落在越界这条

def test_float32_exact_max_still_packable():
    value = parse_value(FLOAT32, "3.4028234663852886e38")
    assert struct.pack(struct_format(FLOAT32), value) != b""

def test_int_out_of_range_message_shows_bounds():
    with pytest.raises(ValueError, match=r"\[-32768, 32767\]"):
        parse_value(INT16, "32768")

def test_string_requires_ascii():
    assert parse_value(STRING, "Fan1") == "Fan1"
    with pytest.raises(ValueError) as exc:
        parse_value(STRING, "风机")
    assert str(exc.value) == "string must be ascii: '风机'"
    # span 在算重叠前先编码长度，中文初值不许抛裸 UnicodeEncodeError
    # （UnicodeEncodeError 是 ValueError 的子类，所以必须显式排除它）
    with pytest.raises(ValueError) as exc:
        register_span(STRING, "风机")
    assert not isinstance(exc.value, UnicodeEncodeError)
    assert str(exc.value) == "string must be ascii: '风机'"

def test_point_defaults():
    p = Point(name="x", area="hr", address=0, datatype=INT16)
    assert p.initial == "0" and p.word_order == "ABCD"
    assert p.waveform == "const" and p.readonly is False and p.row is None
    assert p.wave_param == {}
    # default_factory 唯一真正在防的坑：两个实例不能共享同一个 dict
    q = Point(name="y", area="hr", address=1, datatype=INT16)
    p.wave_param["amplitude"] = "10"
    assert q.wave_param == {}

def test_point_span_follows_datatype_and_area():
    # 位区固定 1：co 上放 FLOAT32（寄存器区是 2）才能证明走的是 area 分支
    assert Point("a", "co", 0, "bool").span == 1
    assert Point("a", "di", 0, FLOAT32).span == 1
    # 寄存器区按类型
    assert Point("a", "hr", 0, "bool").span == 1
    assert Point("a", "hr", 0, FLOAT32).span == 2
    assert Point("a", "hr", 0, UINT64).span == 4

def test_typed_initial_returns_native_values():
    assert Point("a", "hr", 0, INT16, "42").typed_initial() == 42
    assert Point("a", "hr", 0, FLOAT32, "12.5").typed_initial() == 12.5
    assert Point("a", "hr", 0, STRING, "Fan1").typed_initial() == "Fan1"
    assert Point("a", "co", 0, "bool", "on").typed_initial() is True
    assert Point("a", "co", 0, "bool").typed_initial() is False   # 默认 initial="0"

def test_typed_initial_raises_instead_of_guessing():
    with pytest.raises(ValueError):
        Point("a", "co", 0, "bool", "TURE").typed_initial()
    with pytest.raises(ValueError):
        Point("a", "hr", 0, FLOAT32, "3.5e38").typed_initial()
    with pytest.raises(ValueError):
        Point("a", "hr", 0, INT16, "42.9").typed_initial()
    with pytest.raises(ValueError) as exc:
        Point("a", "hr", 0, STRING, "风机").span
    assert not isinstance(exc.value, UnicodeEncodeError)

def test_project_helpers():
    pt = Point("a", "hr", 0, INT16)
    proj = Project(channels=[TcpChannel(name="c1", host="127.0.0.1", port=502,
                                        devices=[Device(name="D1", unit_id=11,
                                                        points=[pt])])])
    assert [p.name for _, _, p in proj.iter_points()] == ["a"]
    assert proj.find_channel("c1").port == 502
    with pytest.raises(KeyError):
        proj.find_channel("nope")

def test_datatypes_are_plain_strings():
    assert isinstance(DATATYPES, tuple) and "bool" in DATATYPES
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_model.py`
Expected: FAIL — `ModuleNotFoundError: modslave.project.model`

- [ ] **Step 3: Write model.py**

类型用**字符串常量**而不是 Enum：它们要直接进出 CSV 和 project.json，字符串省掉一层转换，
也避开"扩展枚举后旧 asset 字段丢值"那类坑。

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/project/model.py` 为准，不要照抄本节。**

```python
"""点表数据模型。本文件不得 import pymodbus 或 PySide6。"""

from __future__ import annotations

from dataclasses import dataclass, field

BOOL, INT16, UINT16, REG16 = "bool", "int16", "uint16", "reg16"
INT32, UINT32, INT64, UINT64 = "int32", "uint32", "int64", "uint64"
FLOAT32, FLOAT64, STRING = "float32", "float64", "string"

DATATYPES = (BOOL, INT16, UINT16, INT32, UINT32, INT64, UINT64,
             FLOAT32, FLOAT64, STRING, REG16)
AREAS = ("co", "di", "hr", "ir")
BIT_AREAS = ("co", "di")
WAVEFORMS = ("const", "ramp", "sine", "random", "step")

WORD_ABCD, WORD_CDAB, WORD_BADC, WORD_DCBA = "ABCD", "CDAB", "BADC", "DCBA"
WORD_ORDERS = (WORD_ABCD, WORD_CDAB, WORD_BADC, WORD_DCBA)

# struct 大端码 -> 占用寄存器数（bool/string 另外处理）
_STRUCT: dict[str, str] = {
    INT16: "h", UINT16: "H", REG16: "H",
    INT32: "i", UINT32: "I", FLOAT32: "f",
    INT64: "q", UINT64: "Q", FLOAT64: "d",
}
_REG_COUNT: dict[str, int] = {
    INT16: 1, UINT16: 1, REG16: 1,
    INT32: 2, UINT32: 2, FLOAT32: 2,
    INT64: 4, UINT64: 4, FLOAT64: 4,
}
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    INT16: (-32768, 32767), UINT16: (0, 65535), REG16: (0, 65535),
    INT32: (-2**31, 2**31 - 1), UINT32: (0, 2**32 - 1),
    INT64: (-2**63, 2**63 - 1), UINT64: (0, 2**64 - 1),
}
_FLOAT_LIMIT = {FLOAT32: 3.4028234663852886e38, FLOAT64: 1.7976931348623157e308}
# bool 初值只认白名单里的写法，其余（含空串）一律 ValueError：默默当 False 的错值
# 会被当成平台 bug 去调，宁可启动时报错也不许猜
_BOOL_TRUE: set[str] = {"1", "true", "yes", "y", "on"}
_BOOL_FALSE: set[str] = {"0", "false", "no", "n", "off"}


def area_is_bit(area: str) -> bool:
    """co/di 是位区：一个点位 = 一个线圈/离散输入。"""
    return area in BIT_AREAS


def _ascii_bytes(value: str) -> bytes:
    """string 初值的编码口径：非 ascii 抛 ValueError，别让 UnicodeEncodeError 逃出去。

    validate.py 算重叠那段没有 try，中文初值必须是一个能被收集成校验错误的
    ValueError，而不是一路 traceback 到启动命令。
    """
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string must be ascii: {value!r}") from exc


def register_span(datatype: str, value: str = "0") -> int:
    if datatype == BOOL:
        return 1
    if datatype == STRING:
        return max(1, -(-len(_ascii_bytes(value)) // 2))
    if datatype not in _REG_COUNT:
        raise ValueError(f"unknown datatype {datatype!r}")
    return _REG_COUNT[datatype]


def struct_format(datatype: str) -> str:
    """大端 struct 串，codec 直接用。"""
    if datatype not in _STRUCT:
        raise ValueError(f"unknown datatype {datatype!r}")
    return ">" + _STRUCT[datatype]


def word_orders_allowed(datatype: str) -> tuple[str, ...]:
    if datatype in (INT32, UINT32, FLOAT32):
        return WORD_ORDERS
    if datatype in (INT64, UINT64, FLOAT64):
        return (WORD_ABCD, WORD_DCBA)
    return (WORD_ABCD,)


def parse_value(datatype: str, text: str):
    """字符串 -> 原生 Python 值；类型不对或越界抛 ValueError，消息里带类型名。

    只收字符串（BOOL 额外允许直接传 Python bool）：CSV 和 project.json 里的初值一律是
    字符串，收到 int/float/None 说明调用方传错了类型；截断或 str() 出来的值看着合法，
    静默错值会被当成平台 bug 去调。未知与越界同样报错，不替用户猜。
    """
    if datatype == BOOL and isinstance(text, bool):
        return text
    if not isinstance(text, str):
        raise ValueError(
            f"{datatype} needs a string value, got {type(text).__name__}: {text!r}")
    if datatype == BOOL:
        key = text.strip().lower()
        if key in _BOOL_TRUE:
            return True
        if key in _BOOL_FALSE:
            return False
        raise ValueError(
            f"bool needs one of 1/0/true/false/yes/no/on/off, got {text!r}")
    if datatype == STRING:
        _ascii_bytes(text)                        # 只为校验，口径与 register_span 一致
        return text
    fmt = struct_format(datatype)
    if fmt.endswith(("f", "d")):
        try:
            num = float(text)
        except ValueError:
            raise ValueError(f"{datatype} needs a number, got {text!r}") from None
        if num != num:
            raise ValueError(f"{datatype} rejects nan: {text!r}")
        limit = _FLOAT_LIMIT[datatype]
        if abs(num) > limit:                      # inf 溢出后自然落到这条
            raise ValueError(
                f"{datatype} out of range, |value| must be <= {limit}: {text!r}")
        return num
    try:
        num = int(text)
    except ValueError:
        raise ValueError(f"{datatype} needs an integer, got {text!r}") from None
    lo, hi = _INT_BOUNDS[datatype]
    if not lo <= num <= hi:
        raise ValueError(f"{datatype} out of range [{lo}, {hi}]: {text!r}")
    return num


@dataclass
class Point:
    name: str
    area: str
    address: int
    datatype: str
    initial: str = "0"
    word_order: str = WORD_ABCD
    waveform: str = "const"
    wave_param: dict[str, str] = field(default_factory=dict)
    readonly: bool = False
    row: int | None = None       # CSV 行号，只在校验报错时用，不进 project.json

    @property
    def span(self) -> int:
        return 1 if area_is_bit(self.area) else register_span(self.datatype, self.initial)

    def typed_initial(self):
        return parse_value(self.datatype, self.initial)


@dataclass
class Device:
    name: str
    unit_id: int
    points: list[Point] = field(default_factory=list)


@dataclass
class TcpChannel:
    name: str
    host: str = "0.0.0.0"
    port: int = 502
    devices: list[Device] = field(default_factory=list)
    type: str = "tcp"


@dataclass
class Project:
    channels: list[TcpChannel] = field(default_factory=list)
    tick_ms: int = 100

    def iter_points(self):
        for ch in self.channels:
            for dev in ch.devices:
                for pt in dev.points:
                    yield ch, dev, pt

    def find_channel(self, name: str) -> TcpChannel:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(name)
```

- [ ] **Step 4: Run it, verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_model.py`
Expected: 22 passed

- [ ] **Step 5: Commit**

```bash
git add modslave/project/model.py tests/test_model.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 点表数据模型与类型元数据"
```

---

## Task 3: PointCodec —— 单寄存器 + 32 位四端序

**Files:**
- Create: `modslave/codec/point_codec.py`
- Test: `tests/test_codec_single.py`、`tests/test_codec_32bit.py`

字序三步法（spec §6）：`struct.pack(">x")` 得大端字节串 `A B C D` → 按字序置换字节串 →
相邻两字节拼 reg。表里的 `BA DC` 意思是 `reg0 == 0x((B<<8)|A)`，与"协议固定 reg 内高字节
在前"不冲突。

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codec_single.py
import pytest
from modslave.codec.point_codec import decode, encode

@pytest.mark.parametrize("dt,value,regs", [
    ("int16", -2, [0xFFFE]),
    ("int16", -32768, [0x8000]),
    ("uint16", 65535, [0xFFFF]),
    ("reg16", 4000, [4000]),
])
def test_encode_single(dt, value, regs):
    assert encode(value, dt, "ABCD") == regs

@pytest.mark.parametrize("dt,value,regs", [
    ("int16", -2, [0xFFFE]),
    ("uint16", 65535, [0xFFFF]),
    ("reg16", 4000, [4000]),
])
def test_decode_single(dt, value, regs):
    assert decode(regs, dt, "ABCD") == value

@pytest.mark.parametrize("dt", ["int16", "uint16", "reg16"])
def test_single_roundtrip(dt):
    for value in (0, 1, 2**15 - 1):
        if dt == "int16" or value <= 32767:
            assert decode(encode(value, dt, "ABCD"), dt, "ABCD") == value

def test_out_of_range_single_register_is_rejected():
    with pytest.raises(ValueError, match="reg16"):
        encode(-1, "reg16", "ABCD")
    with pytest.raises(ValueError, match="int16"):
        encode(32768, "int16", "ABCD")

def test_bool_uses_bit_path():
    assert encode(True, "bool", "ABCD") == [1]
    assert encode(False, "bool", "ABCD") == [0]
    assert decode([1], "bool", "ABCD") is True
    assert decode([0], "bool", "ABCD") is False
```

```python
# tests/test_codec_32bit.py
import pytest
from modslave.codec.point_codec import decode, encode

V = 0x12345678          # A=0x12 B=0x34 C=0x56 D=0x78
EXPECT = {
    "ABCD": [0x1234, 0x5678],
    "CDAB": [0x5678, 0x1234],
    "BADC": [0x3412, 0x7856],
    "DCBA": [0x7856, 0x3412],
}

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_encode_int32_matches_hand_calculated_table(order):
    assert encode(V, "int32", order) == EXPECT[order]

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_decode_int32_matches_hand_calculated_table(order):
    assert decode(EXPECT[order], "int32", order) == V

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_roundtrip_int32_and_uint32(order):
    for value in (0, 1, V, 2**31 - 1):
        assert decode(encode(value, "int32", order), "int32", order) == value
    assert decode(encode(2**32 - 1, "uint32", order), "uint32", order) == 2**32 - 1

@pytest.mark.parametrize("order", ["ABCD", "CDAB", "BADC", "DCBA"])
def test_roundtrip_float32(order):
    got = decode(encode(-12.5, "float32", order), "float32", order)
    assert got == pytest.approx(-12.5, abs=1e-6)

def test_float32_cdab_is_word_swapped():
    """现场最常见的坑：浮点读出来是天价。CDAB 必须能识别。"""
    assert encode(1.0, "float32", "CDAB") == [0x0000, 0x3F80]
    assert encode(1.0, "float32", "ABCD") == [0x3F80, 0x0000]

def test_int32_out_of_range():
    with pytest.raises(ValueError, match="int32"):
        encode(2**31, "int32", "ABCD")

def test_float32_rejects_nan_and_inf():
    import math
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="float32"):
            encode(bad, "float32", "ABCD")

def test_decode_checks_register_count():
    with pytest.raises(ValueError, match="needs 2 registers"):
        decode([1], "int32", "ABCD")

def test_unknown_datatype():
    with pytest.raises(ValueError, match="wchar"):
        encode(1, "wchar", "ABCD")
```

- [ ] **Step 2: Run them, verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codec_single.py tests/test_codec_32bit.py`
Expected: FAIL — module missing

- [ ] **Step 3: Write point_codec.py**

置换只写一份 `_permute_words`，编码和解码共用它。四种字序都是对合（做两次回原值），
所以共用即互逆；`test_roundtrip_*` 就是钉这一点的。

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/codec/point_codec.py` 为准，不要照抄本节。**

```python
"""点位值 <-> 原始 reg16 数组。字序置换在这一层，pymodbus 只见到 reg16。"""

from __future__ import annotations

import struct

from ..project.model import (
    BOOL, FLOAT32, FLOAT64, STRING, WORD_ABCD, WORD_ORDERS, parse_value,
    register_span, struct_format, word_orders_allowed,
)

_NUMERIC = ("int16", "uint16", "reg16", "int32", "uint32", "int64", "uint64",
            "float32", "float64")
_FLOATS = (FLOAT32, FLOAT64)


def encode(value, datatype: str, word_order: str = WORD_ABCD) -> list[int]:
    """原生值 -> reg16 列表。位区（bool）走独立路径，不参与字序。"""
    if datatype == BOOL:
        return [1 if parse_value(BOOL, value) else 0]
    if datatype == STRING:
        raise ValueError("string datatype is added in a later step")
    if datatype not in _NUMERIC:
        raise ValueError(f"unknown datatype {datatype!r}")
    raw = struct.pack(struct_format(datatype), _coerce(value, datatype))
    return _bytes_to_regs(raw, datatype, word_order)


def decode(registers: list[int], datatype: str, word_order: str = WORD_ABCD):
    """reg16 列表 -> 原生值。"""
    if datatype == BOOL:
        return bool(registers[0])
    if datatype == STRING:
        raise ValueError("string datatype is added in a later step")
    if datatype not in _NUMERIC:
        raise ValueError(f"unknown datatype {datatype!r}")
    need = register_span(datatype)
    if len(registers) != need:
        raise ValueError(f"{datatype} needs {need} registers, got {len(registers)}")
    raw = _regs_to_bytes(list(registers), datatype, word_order)
    value = struct.unpack(struct_format(datatype), raw)[0]
    return float(value) if datatype in _FLOATS else value


def _coerce(value, datatype: str):
    """字符串或数字统一成 struct.pack 收得的类型，顺带做范围检查。"""
    if datatype in _FLOATS:
        text = value if isinstance(value, str) else repr(float(value))
        return parse_value(datatype, text)
    text = value if isinstance(value, str) else str(value)
    return parse_value(datatype, text)


def _permute_words(chunks: list[bytes], word_order: str) -> list[bytes]:
    if word_order == "ABCD":
        return chunks
    if word_order == "CDAB":
        return chunks[::-1]                      # 寄存器顺序交换
    if word_order == "BADC":
        return [c[::-1] for c in chunks]          # 寄存器内字节交换
    return [c[::-1] for c in chunks][::-1]        # DCBA：两种交换都做


def _bytes_to_regs(raw: bytes, datatype: str, word_order: str) -> list[int]:
    _check_word_order(datatype, word_order)
    chunks = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    return [int.from_bytes(c, "big") for c in _permute_words(chunks, word_order)]


def _regs_to_bytes(registers: list[int], datatype: str, word_order: str) -> bytes:
    _check_word_order(datatype, word_order)
    chunks = [(int(r) & 0xFFFF).to_bytes(2, "big") for r in registers]
    return b"".join(_permute_words(chunks, word_order))


def _check_word_order(datatype: str, word_order: str) -> None:
    if word_order not in WORD_ORDERS:
        raise ValueError(f"unknown word_order {word_order!r}")
    allowed = word_orders_allowed(datatype)
    if word_order not in allowed:
        raise ValueError(
            f"{datatype} only supports {allowed}, got {word_order!r}")
```

- [ ] **Step 4: Run them, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codec_single.py tests/test_codec_32bit.py`
Expected: 全通过。`test_encode_int32_matches_hand_calculated_table` 失败说明
`_permute_words` 与手算表不符——**按手算表改实现**；手算表是从 `A B C D` 的定义直接推出来的，
不会因为实现而变。`test_decode_checks_register_count` 靠 `register_span` 判长度。

- [ ] **Step 5: Commit**

```bash
git add modslave/codec/point_codec.py tests/test_codec_single.py tests/test_codec_32bit.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: codec 单寄存器与 32 位四端序"
```

---

## Task 4: PointCodec —— 64 位两端序与 string

**Files:**
- Modify: `modslave/codec/point_codec.py`
- Test: `tests/test_codec_64bit.py`

- [ ] **Step 1: Write the failing test**

`ABCD` 是 8 字节大端原样切 reg，`DCBA` 是 8 字节整体反转后切 reg。期望值手算写死。

```python
# tests/test_codec_64bit.py
import pytest
from modslave.codec.point_codec import decode, encode

V64 = 0x0102030405060708

def test_uint64_abcd_is_plain_big_endian():
    assert encode(V64, "uint64", "ABCD") == [0x0102, 0x0304, 0x0506, 0x0708]

def test_uint64_dcba_is_full_byte_reverse():
    """DCBA = 8 字节整体反转后切 reg，不是字序反转（那等同 CDAB）。
    实现只走 _permute_words，64 位没有特殊分支。"""
    assert encode(V64, "uint64", "DCBA") == [0x0807, 0x0605, 0x0403, 0x0201]

@pytest.mark.parametrize("order", ["ABCD", "DCBA"])
def test_uint64_roundtrip(order):
    for value in (0, 1, V64, 2**64 - 1):
        assert decode(encode(value, "uint64", order), "uint64", order) == value

@pytest.mark.parametrize("order", ["ABCD", "DCBA"])
def test_int64_roundtrip_including_negative_min(order):
    for value in (0, -1, 2**63 - 1, -(2**63)):
        assert decode(encode(value, "int64", order), "int64", order) == value

@pytest.mark.parametrize("order", ["ABCD", "DCBA"])
def test_float64_roundtrip(order):
    assert decode(encode(1.2345678901234, "float64", order), "float64", order) \
        == pytest.approx(1.2345678901234)

def test_64bit_rejects_cdab_and_badc():
    """业界对 64 位的 CDAB/BADC 没有公认定义，我们不自发明。"""
    for bad in ("CDAB", "BADC"):
        with pytest.raises(ValueError, match="ABCD.*DCBA"):
            encode(1, "int64", bad)
        with pytest.raises(ValueError, match="ABCD.*DCBA"):
            decode([0, 0, 0, 0], "int64", bad)

def test_64bit_needs_four_registers():
    with pytest.raises(ValueError, match="needs 4 registers"):
        decode([1, 2], "uint64", "ABCD")


# ---- string ----

def test_string_abcd_pads_to_even_bytes():
    assert encode("abc", "string") == [0x6162, 0x6300]

def test_string_two_bytes_per_register_big_endian():
    assert encode("AB", "string") == [0x4142]

@pytest.mark.parametrize("text", ["A", "AB", "ABC", "ABCDE", "FAN-01", ""])
def test_string_roundtrip_strips_padding(text):
    regs = encode(text, "string")
    assert len(regs) == max(1, -(-len(text) // 2))
    assert decode(regs, "string") == text

def test_string_rejects_non_ascii():
    with pytest.raises(ValueError, match="ascii"):
        encode("中", "string")

def test_string_only_supports_abcd():
    with pytest.raises(ValueError, match="ABCD"):
        encode("ab", "string", "CDAB")

def test_string_rejects_embedded_nul():
    with pytest.raises(ValueError, match="nul"):
        encode("a\x00b", "string")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_codec_64bit.py`
Expected: FAIL — string 分支抛 `string datatype is added in a later step`；
64 位的 `needs 4 registers` 已可用（Task 3 的 `register_span` 覆盖），其余断言按实现进度部分通过。

- [ ] **Step 3: 实现 string，并把 string 从早退分支里摘掉**

`encode`/`decode` 里删掉两处 `if datatype == STRING: raise ...`，改成走下面两个函数。
64 位不需要新代码——`_permute_words` 对 4 个 chunk 同样成立（`DCBA` 反转 4 个 chunk 再各自
反转字节，等价于 8 字节整体反转；`CDAB`/`BADC` 由 `word_orders_allowed` 挡掉）。

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/codec/point_codec.py` 为准，不要照抄本节。**

```python
def _encode_string(value) -> list[int]:
    text = value if isinstance(value, str) else str(value)
    if "\x00" in text:
        raise ValueError("string must not contain nul: it would be truncated on read")
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string must be ascii: {text!r}") from exc
    if len(raw) % 2:
        raw += b"\x00"
    if not raw:                       # 空串也占一个寄存器，与 register_span 一致
        raw = b"\x00\x00"
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def _decode_string(registers: list[int]) -> str:
    raw = b"".join((int(r) & 0xFFFF).to_bytes(2, "big") for r in registers)
    return raw.split(b"\x00", 1)[0].decode("ascii")
```

`encode` 顶部变成：

```python
    if datatype == BOOL:
        return [1 if parse_value(BOOL, value) else 0]
    if datatype == STRING:
        _check_word_order(datatype, word_order)
        return _encode_string(value)
```

`decode` 顶部变成：

```python
    if datatype == BOOL:
        return bool(registers[0])
    if datatype == STRING:
        _check_word_order(datatype, word_order)
        return _decode_string(registers)
```

- [ ] **Step 4: Run the whole codec suite**

Run: `.venv/Scripts/python.exe -m pytest tests -k codec`
Expected: 全通过；Task 3 的用例不受影响。

- [ ] **Step 5: Commit**

```bash
git add modslave/codec/point_codec.py tests/test_codec_64bit.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: codec 补 64 位两端序与 ascii 字符串"
```

---

## Task 5: 启动前全量校验

**Files:**
- Create: `modslave/project/validate.py`
- Test: `tests/test_validate.py`

规则（spec §8 + 本计划修正 3、4）：一次返回全部问题；错误级 (`error`) 拦住启动，
警告级 (`warning`) 不拦；每条带 `where = 通道/设备/点位` 和 CSV 行号（有就带）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validate.py
from modslave.project.model import Device, Point, Project, TcpChannel
from modslave.project.validate import validate_project, error_lines

def _p(**kw):
    base = dict(name="p", area="hr", address=0, datatype="uint16", initial="0")
    base.update(kw)
    return Point(**base)

def _proj(*points, port=502, unit=11, name="D1", channel="c1"):
    dev = Device(name=name, unit_id=unit, points=list(points))
    ch = TcpChannel(name=channel, host="127.0.0.1", port=port, devices=[dev])
    return Project(channels=[ch])

def codes(project):
    return {(i.code, i.where) for i in validate_project(project) if i.level == "error"}

def test_clean_project_has_no_issues():
    assert validate_project(_proj(_p(address=0), _p(name="q", address=5))) == []

def test_overlap_in_same_area():
    assert ("overlap", "c1/D1/q") in codes(_proj(_p(), _p(name="q", address=0)))
    assert codes(_proj(_p(), _p(name="q", address=1))) == set()

def test_same_address_in_different_area_is_ok():
    assert codes(_proj(_p(area="hr"), _p(name="q", area="ir"))) == set()

def test_float32_span_overlaps_next_point():
    got = codes(_proj(_p(datatype="float32", initial="1.0"), _p(name="q", address=1)))
    assert ("overlap", "c1/D1/q") in got

def test_bit_area_overlap_is_checked_per_bit_not_per_register():
    """两个线圈号相同才算重叠；16 个线圈共用一个寄存器是正常的。"""
    assert codes(_proj(_p(area="co", datatype="bool", address=3),
                       _p(name="q", area="co", datatype="bool", address=3))) == \
        {("overlap", "c1/D1/q")}
    assert codes(_proj(_p(area="co", datatype="bool", address=0),
                       _p(name="q", area="co", datatype="bool", address=1))) == set()

def test_type_area_mismatch():
    got = codes(_proj(_p(datatype="bool", area="hr"),
                      _p(name="q", area="co", datatype="uint16")))
    assert ("area_type", "c1/D1/p") in got
    assert ("area_type", "c1/D1/q") in got

def test_unknown_datatype_and_area():
    got = codes(_proj(_p(datatype="wchar"), _p(name="q", address=5, area="xr")))
    assert ("datatype", "c1/D1/p") in got
    assert ("area", "c1/D1/q") in got

def test_initial_out_of_range_is_error_with_row_number():
    pt = _p(datatype="int16", initial="40000")
    pt.row = 7
    issue = [i for i in validate_project(_proj(pt)) if i.code == "initial"][0]
    assert "int16" in issue.message and "line 7" in issue.where

def test_word_order_on_64bit_is_error_on_16bit_is_warning():
    assert ("word_order", "c1/D1/p") in codes(
        _proj(_p(datatype="int64", word_order="CDAB", initial="1")))
    issues = validate_project(_proj(_p(datatype="int16", word_order="CDAB")))
    assert [i for i in issues if i.level == "error"] == []
    assert any(i.code == "word_order_ignored" for i in issues)

def test_address_out_of_range():
    assert ("address", "c1/D1/p") in codes(_proj(_p(address=65536)))
    assert ("address", "c1/D1/p") in codes(_proj(_p(address=-1)))

def test_unit_id_range_and_duplicate():
    ch = TcpChannel(name="c1", host="127.0.0.1", port=502, devices=[
        Device(name="A", unit_id=300, points=[_p()]),
        Device(name="B", unit_id=247, points=[_p()]),
        Device(name="C", unit_id=247, points=[_p()])])
    got = {i.code for i in validate_project(Project(channels=[ch]))
           if i.level == "error"}
    assert {"unit_range", "unit_duplicated"} <= got

def test_port_zero_and_out_of_range_are_errors():
    assert ("port", "c1") in codes(_proj(_p(), port=0))
    assert ("port", "c1") in codes(_proj(_p(), port=70000))

def test_duplicate_channel_names():
    ch = TcpChannel(name="c1", host="127.0.0.1", port=502, devices=[])
    ch2 = TcpChannel(name="c1", host="127.0.0.1", port=503, devices=[])
    got = {i.code for i in validate_project(Project(channels=[ch, ch2]))
           if i.level == "error"}
    assert "channel_duplicated" in got

def test_wave_param_missing_extra_and_bad_range():
    a = _p(waveform="sine", wave_param={"base": "0", "amp": "1"})
    b = _p(name="b", address=5, waveform="sine",
           wave_param={"base": "0", "amp": "1", "period": "0"})
    c = _p(name="c", address=8, waveform="random",
           wave_param={"min": "20", "max": "10"})
    d = _p(name="d", address=12, waveform="sine",
           wave_param={"base": "0", "amp": "1", "period": "4", "freq": "1"})
    got = {i.code for i in validate_project(_proj(a, b, c, d)) if i.level == "error"}
    assert "wave_param" in got

def test_unknown_waveform():
    assert ("waveform", "c1/D1/p") in codes(_proj(_p(waveform="triangle")))

def test_error_lines_format():
    lines = error_lines(validate_project(_proj(_p(waveform="triangle"))))
    assert lines == ["[waveform] c1/D1/p: unknown waveform 'triangle'"]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_validate.py`
Expected: FAIL — module missing

- [ ] **Step 3: Write validate.py**

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/project/validate.py` 为准，不要照抄本节。**

```python
"""启动前校验：一次报全，不撞第一个就崩。本文件不得 import pymodbus。"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    AREAS, BOOL, DATATYPES, INT16, REG16, UINT16, WAVEFORMS, WORD_ORDERS,
    area_is_bit, register_span, word_orders_allowed,
)

# 波形参数表（spec §5）。M4 实现波形时必须复用这张表，不要再抄一份。
WAVE_SPEC: dict[str, tuple[set[str], set[str]]] = {
    "const": ({"value"}, set()),
    "ramp": ({"from", "to", "speed"}, {"hold"}),
    "sine": ({"base", "amp", "period"}, {"phase"}),
    "random": ({"min", "max"}, {"interval", "seed"}),
    "step": ({"values", "interval"}, set()),
}
if set(WAVE_SPEC) != set(WAVEFORMS):
    # 波形合法取值有两个来源就会漂移，宁可导入即失败也不要留到运行时
    raise RuntimeError("WAVE_SPEC keys drifted from model.WAVEFORMS")
_POSITIVE = {"period", "speed", "interval", "hold"}
_SINGLE = {BOOL, INT16, UINT16, REG16}


@dataclass(frozen=True)
class Issue:
    level: str          # "error" | "warning"
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.where}: {self.message}"


def error_lines(issues: list[Issue]) -> list[str]:
    return [str(i) for i in issues if i.level == "error"]


def validate_project(project) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[str] = set()
    for ch in project.channels:
        if ch.name in seen:
            issues.append(Issue("error", "channel_duplicated", ch.name,
                                f"channel name {ch.name!r} used twice"))
        seen.add(ch.name)
        if ch.type != "tcp":
            issues.append(Issue("error", "channel_type", ch.name,
                                f"channel type {ch.type!r} is not supported in M1"))
        if not 1 <= ch.port <= 65535:
            issues.append(Issue("error", "port", ch.name,
                                f"port {ch.port} must be in 1..65535"))
        _devices(ch, issues)
    if not 10 <= project.tick_ms <= 60000:
        issues.append(Issue("error", "tick_ms", "project",
                            f"tick_ms {project.tick_ms} must be in 10..60000"))
    return issues


def _devices(ch, issues: list[Issue]) -> None:
    units: dict[int, str] = {}
    for dev in ch.devices:
        where = f"{ch.name}/{dev.name}"
        if not 1 <= dev.unit_id <= 247:
            issues.append(Issue("error", "unit_range", where,
                                f"unit_id {dev.unit_id} outside 1..247"))
        if dev.unit_id in units:
            issues.append(Issue("error", "unit_duplicated", where,
                                f"unit_id {dev.unit_id} already used by {units[dev.unit_id]}"))
        units[dev.unit_id] = dev.name
        _points(ch.name, dev, issues)


def _points(channel_name: str, dev, issues: list[Issue]) -> None:
    used: dict[tuple[str, int], str] = {}
    for pt in dev.points:
        loc = f"{channel_name}/{dev.name}/{pt.name}"
        if pt.row:
            loc = f"{loc} (line {pt.row})"
        if pt.datatype not in DATATYPES:
            issues.append(Issue("error", "datatype", loc,
                                f"unknown datatype {pt.datatype!r}"))
        if pt.area not in AREAS:
            issues.append(Issue("error", "area", loc, f"unknown area {pt.area!r}"))
        if pt.datatype in DATATYPES and pt.area in AREAS:
            _shape(channel_name, dev, pt, loc, used, issues)
        _wave(pt, loc, issues)


def _shape(channel_name, dev, pt, loc, used, issues) -> None:
    if area_is_bit(pt.area) != (pt.datatype == BOOL):
        issues.append(Issue("error", "area_type", loc,
                            f"{pt.datatype} is not allowed on area {pt.area}"))
        return
    if not 0 <= pt.address <= 65535:
        issues.append(Issue("error", "address", loc,
                            f"address {pt.address} outside 0..65535"))
    if pt.word_order not in WORD_ORDERS:
        issues.append(Issue("error", "word_order", loc,
                            f"unknown word_order {pt.word_order!r}"))
    else:
        allowed = word_orders_allowed(pt.datatype)
        if pt.word_order not in allowed:
            # 单寄存器类型填了字序 -> 只是没意义，warning；
            # 64 位/string 填了不支持的字序 -> error（语义有分歧，不能猜）
            is_single = pt.datatype in _SINGLE
            issues.append(Issue(
                "warning" if is_single else "error",
                "word_order_ignored" if is_single else "word_order",
                loc, f"{pt.datatype} only supports {allowed}, got {pt.word_order!r}"))
    try:
        pt.typed_initial()
    except ValueError as exc:
        issues.append(Issue("error", "initial", loc, str(exc)))
    _overlap(pt, loc, used, issues)


def _overlap(pt, loc, used, issues) -> None:
    """位区按位判，寄存器区按寄存器判。跨度算不出来就跳过——initial 已经报过错。"""
    short = loc.split("/")[-1].split(" (")[0]
    if area_is_bit(pt.area):
        keys = [(pt.area, pt.address)]
    else:
        try:
            span = register_span(pt.datatype, pt.initial)
        except ValueError:
            return
        keys = [(pt.area, pt.address + i) for i in range(span)]
    for key in keys:
        if key in used:
            issues.append(Issue("error", "overlap", loc,
                                f"{key[0]} address {key[1]} already used by {used[key]}"))
            return
        used[key] = short


def _wave(pt, loc, issues) -> None:
    if pt.waveform not in WAVE_SPEC:
        issues.append(Issue("error", "waveform", loc,
                            f"unknown waveform {pt.waveform!r}"))
        return
    required, optional = WAVE_SPEC[pt.waveform]
    got = set(pt.wave_param)
    if pt.waveform == "const" and not got:
        return                      # const 不填就当 0，M4 才用 value
    for miss in sorted(required - got):
        issues.append(Issue("error", "wave_param", loc,
                            f"{pt.waveform} missing parameter {miss!r}"))
    for extra in sorted(got - required - optional):
        issues.append(Issue("error", "wave_param", loc,
                            f"{pt.waveform} unknown parameter {extra!r}"))
    if got & required == required or (pt.waveform == "const" and got):
        _wave_ranges(pt, loc, issues)


def _wave_ranges(pt, loc, issues) -> None:
    numbers: dict[str, float] = {}
    for key, text in pt.wave_param.items():
        if key == "values" or key == "seed":
            continue
        try:
            numbers[key] = float(text)
        except ValueError:
            issues.append(Issue("error", "wave_param", loc,
                                f"parameter {key!r} is not a number: {text!r}"))
    for key in _POSITIVE & set(numbers):
        if numbers[key] <= 0:
            issues.append(Issue("error", "wave_param", loc,
                                f"{pt.waveform} {key} must be > 0"))
    if {"min", "max"} <= set(numbers) and numbers["min"] > numbers["max"]:
        issues.append(Issue("error", "wave_param", loc, "random min > max"))
    if {"from", "to"} <= set(numbers) and numbers["from"] == numbers["to"]:
        issues.append(Issue("error", "wave_param", loc, "ramp from == to"))
    if pt.waveform == "step":
        raw = pt.wave_param.get("values", "")
        if not [v for v in raw.split(",") if v.strip()]:
            issues.append(Issue("error", "wave_param", loc, "step values list is empty"))
```

- [ ] **Step 4: Run it, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_validate.py`
Expected: 40 passed（计划原写 36，裁决 T 的 host 非空字符串规则补了 4 条：三个脏 host、
正常 host、不判 IP 语法、脏 host 不进冲突判定）

- [ ] **Step 5: Commit**

```bash
git add modslave/project/validate.py tests/test_validate.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 启动前全量校验，一次报所有问题"
```

---

## Task 6: project.json 读写

**Files:**
- Create: `modslave/project/json_io.py`
- Test: `tests/test_json_io.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_json_io.py
import json
import pytest
from modslave.project.json_io import from_dict, load, save, to_dict, to_json
from modslave.project.model import Device, Point, Project, TcpChannel

def sample():
    pt = Point(name="CO", area="hr", address=0, datatype="float32", initial="12.5",
               word_order="CDAB", waveform="sine",
               wave_param={"base": "20", "amp": "15", "period": "60"}, readonly=True)
    dev = Device(name="COVI-1", unit_id=11, points=[pt])
    ch = TcpChannel(name="tcp-502", host="0.0.0.0", port=502, devices=[dev])
    return Project(channels=[ch], tick_ms=200)

def test_roundtrip_is_identical():
    proj = sample()
    assert from_dict(to_dict(proj)) == proj

def test_json_text_roundtrip_keeps_every_field():
    pt = from_dict(json.loads(to_json(sample()))).channels[0].devices[0].points[0]
    assert (pt.name, pt.area, pt.address, pt.datatype, pt.initial) == \
        ("CO", "hr", 0, "float32", "12.5")
    assert (pt.word_order, pt.waveform, pt.wave_param, pt.readonly) == \
        ("CDAB", "sine", {"base": "20", "amp": "15", "period": "60"}, True)

def test_tick_ms_and_channel_defaults_survive():
    back = from_dict(to_dict(sample()))
    assert back.tick_ms == 200
    assert (back.channels[0].host, back.channels[0].port) == ("0.0.0.0", 502)

def test_row_number_is_not_serialized():
    proj = sample()
    proj.channels[0].devices[0].points[0].row = 9
    data = to_dict(proj)
    assert "row" not in data["channels"][0]["devices"][0]["points"][0]
    assert from_dict(data).channels[0].devices[0].points[0].row is None

def test_file_roundtrip(tmp_path):
    path = tmp_path / "project.json"
    save(sample(), path)
    assert load(path) == sample()

def test_wrong_version_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 99, "channels": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load(path)

def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.json")

def test_channel_without_type_defaults_to_tcp():
    data = to_dict(sample())
    data["channels"][0].pop("type")
    assert from_dict(data).channels[0].type == "tcp"

def test_point_without_optional_fields_uses_defaults():
    data = {"version": 1, "tick_ms": 100, "channels": [{"name": "c", "port": 502,
            "devices": [{"name": "D", "unit_id": 1,
                         "points": [{"name": "p", "area": "hr", "address": 0,
                                     "datatype": "uint16"}]}]}]}
    pt = from_dict(data).channels[0].devices[0].points[0]
    assert (pt.initial, pt.word_order, pt.waveform, pt.readonly) == ("0", "ABCD", "const", False)
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_json_io.py`
Expected: FAIL — module missing

- [ ] **Step 3: Write json_io.py**

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/project/json_io.py` 为准，不要照抄本节。**

```python
"""project.json 读写。它是运行态唯一真相，与 CSV 双向可导。"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Device, Point, Project, TcpChannel

VERSION = 1
_POINT_FIELDS = ("name", "area", "address", "datatype", "initial", "word_order",
                 "waveform", "wave_param", "readonly")


def _point_to_dict(point: Point) -> dict:
    out = {name: getattr(point, name) for name in _POINT_FIELDS}
    out["wave_param"] = dict(point.wave_param)
    out["readonly"] = bool(point.readonly)
    return out


def _point_from_dict(raw: dict) -> Point:
    return Point(
        name=raw["name"], area=raw["area"], address=int(raw["address"]),
        datatype=raw["datatype"], initial=str(raw.get("initial", "0")),
        word_order=raw.get("word_order", "ABCD"),
        waveform=raw.get("waveform", "const"),
        wave_param=dict(raw.get("wave_param", {})),
        readonly=bool(raw.get("readonly", False)))


def _device_to_dict(device: Device) -> dict:
    return {"name": device.name, "unit_id": device.unit_id,
            "points": [_point_to_dict(p) for p in device.points]}


def _device_from_dict(raw: dict) -> Device:
    return Device(name=raw["name"], unit_id=int(raw["unit_id"]),
                  points=[_point_from_dict(p) for p in raw.get("points", [])])


def _channel_to_dict(channel: TcpChannel) -> dict:
    return {"name": channel.name, "type": channel.type, "host": channel.host,
            "port": channel.port,
            "devices": [_device_to_dict(d) for d in channel.devices]}


def _channel_from_dict(raw: dict) -> TcpChannel:
    return TcpChannel(
        name=raw["name"], type=raw.get("type", "tcp"),
        host=raw.get("host", "0.0.0.0"), port=int(raw.get("port", 502)),
        devices=[_device_from_dict(d) for d in raw.get("devices", [])])


def to_dict(project: Project) -> dict:
    return {"version": VERSION, "tick_ms": project.tick_ms,
            "channels": [_channel_to_dict(ch) for ch in project.channels]}


def to_json(project: Project) -> str:
    return json.dumps(to_dict(project), ensure_ascii=False, indent=2)


def from_dict(data: dict) -> Project:
    version = data.get("version")
    if version != VERSION:
        raise ValueError(f"unsupported project.json version {version!r}, "
                         f"expected {VERSION}")
    return Project(channels=[_channel_from_dict(ch) for ch in data.get("channels", [])],
                   tick_ms=int(data.get("tick_ms", 100)))


def save(project: Project, path: Path) -> None:
    Path(path).write_text(to_json(project) + "\n", encoding="utf-8")


def load(path: Path) -> Project:
    return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
```

- [ ] **Step 4: Run it, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_json_io.py`
Expected: 91 passed

- [ ] **Step 5: Commit**

```bash
git add modslave/project/json_io.py tests/test_json_io.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: project.json 读写与版本检查"
```

---

## Task 7: CSV 读写

**Files:**
- Create: `modslave/project/csv_io.py`
- Test: `tests/test_csv_io.py`

表头顺序就是与厂商点表之间的合同，改列必须同步改模板：

```
channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
```

CSV 只带通道**名**，不带 host/port——端口属于工程不属于点表，所以 `read_csv` 接受
`ports: dict[str,int]`，缺省按通道名尾数猜并回一条 note 交给人确认。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_csv_io.py
import pytest
from modslave.project.csv_io import (
    HEADER, format_wave_param, parse_wave_param, read_csv, read_csv_file,
    write_csv, write_csv_file,
)

ROWS = """channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
tcp-502,COVI-1,11,CO浓度,hr,0,float32,CDAB,12.5,sine,base=20;amp=15;period=60,true
tcp-502,COVI-1,11,风速,hr,2,uint16,ABCD,0,const,,false
tcp-502,风机-1,12,启动,co,3,bool,ABCD,false,const,,false
tcp-502,风机-1,12,运行状态,di,0,bool,ABCD,true,const,,true
"""

def test_groups_by_channel_then_device():
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    assert len(proj.channels) == 1
    ch = proj.channels[0]
    assert (ch.name, ch.host, ch.port, ch.type) == ("tcp-502", "0.0.0.0", 502, "tcp")
    assert [d.name for d in ch.devices] == ["COVI-1", "风机-1"]
    assert [d.unit_id for d in ch.devices] == [11, 12]
    assert [p.name for p in ch.devices[0].points] == ["CO浓度", "风速"]

def test_parses_wave_param_and_flags():
    pt = read_csv(ROWS, ports={"tcp-502": 502}).channels[0].devices[0].points[0]
    assert (pt.word_order, pt.waveform, pt.readonly) == ("CDAB", "sine", True)
    assert pt.wave_param == {"base": "20", "amp": "15", "period": "60"}
    assert pt.row == 2

def test_row_numbers_follow_csv_lines():
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    rows = [p.row for ch in proj.channels for d in ch.devices for p in d.points]
    assert rows == [2, 3, 4, 5]

def test_values_are_trimmed_and_case_normalised():
    text = ROWS.replace("tcp-502,COVI-1,11,CO浓度,hr,0,float32,CDAB",
                        "tcp-502,COVI-1,11,CO浓度, HR ,0, Float32 , cdab ")
    pt = read_csv(text, ports={"tcp-502": 502}).channels[0].devices[0].points[0]
    assert (pt.area, pt.datatype, pt.word_order) == ("hr", "float32", "CDAB")

def test_bad_address_reports_line_number():
    bad = ROWS + "tcp-502,风机-1,12,X,hr,notanumber,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError, match="line 6"):
        read_csv(bad, ports={"tcp-502": 502})

def test_bad_unit_id_reports_line_number():
    bad = ROWS + "tcp-502,风机-1,x,X,hr,9,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError, match="line 6"):
        read_csv(bad, ports={"tcp-502": 502})

def test_wrong_column_count_reports_line_number():
    bad = ROWS + "tcp-502,风机-1,12,X,hr,9,uint16\n"
    with pytest.raises(ValueError, match="line 6"):
        read_csv(bad, ports={"tcp-502": 502})

def test_wrong_header_is_rejected():
    with pytest.raises(ValueError, match="header"):
        read_csv("channel,device\nx,y\n", ports={})

def test_empty_file_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        read_csv("", ports={})

def test_blank_lines_and_comment_lines_are_skipped():
    text = ROWS.replace("tcp-502,COVI-1,11,风速", "# note\n\ntcp-502,COVI-1,11,风速")
    proj = read_csv(text, ports={"tcp-502": 502})
    assert len(proj.channels[0].devices[0].points) == 2

def test_device_name_conflict_on_same_unit_is_reported():
    text = ROWS + "tcp-502,别的名,11,电压,hr,4,uint16,ABCD,0,const,,false\n"
    with pytest.raises(ValueError, match="unit_id 11"):
        read_csv(text, ports={"tcp-502": 502})

def test_csv_roundtrip_is_identical():
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    assert read_csv(write_csv(proj), ports={"tcp-502": 502}) == proj

def test_read_csv_file_accepts_bom(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text("\ufeff" + ROWS, encoding="utf-8")
    proj = read_csv_file(path, ports={"tcp-502": 502})
    assert proj.channels[0].devices[0].points[0].name == "CO浓度"

def test_write_csv_file_then_read_back(tmp_path):
    proj = read_csv(ROWS, ports={"tcp-502": 502})
    path = tmp_path / "out.csv"
    write_csv_file(proj, path)
    assert read_csv_file(path, ports={"tcp-502": 502}) == proj

def test_wave_param_helpers():
    assert parse_wave_param("base=20;amp=15") == {"base": "20", "amp": "15"}
    assert parse_wave_param("") == {}
    assert format_wave_param({"base": "20", "amp": "15"}) == "base=20;amp=15"

def test_channel_without_port_falls_back_and_notes_it():
    proj, notes = read_csv(ROWS, ports={}, with_notes=True)
    assert proj.channels[0].port == 502        # 从 tcp-502 尾数猜出来
    assert any("port" in n for n in notes)

def test_header_is_exactly_the_contract():
    assert HEADER == ["channel", "device", "unit_id", "name", "area", "address",
                      "datatype", "word_order", "initial", "waveform", "wave_param",
                      "readonly"]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_csv_io.py`
Expected: FAIL — module missing

- [ ] **Step 3: Write csv_io.py**

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/project/csv_io.py` 为准，不要照抄本节。**

```python
"""CSV 点表读写。列顺序即合同；解析错误带行号，好让厂商点表能当场改对。"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .model import Device, Point, Project, TcpChannel

HEADER = ["channel", "device", "unit_id", "name", "area", "address", "datatype",
          "word_order", "initial", "waveform", "wave_param", "readonly"]
_TRUE = {"1", "true", "yes", "y", "on"}


def read_csv(text: str, ports: dict[str, int] | None = None, address_offset: int = 0,
             with_notes: bool = False):
    """CSV -> Project。`ports` 给通道名定端口；没给就按尾数猜并记 note。"""
    ports = ports or {}
    notes: list[str] = []
    rows = _rows(text)
    channels: dict[str, TcpChannel] = {}
    devices: dict[tuple[str, int], Device] = {}
    for index, cell in rows:
        try:
            address = _require_int(cell["address"], "address", index) + address_offset
            unit_id = _require_int(cell["unit_id"], "unit_id", index)
        except ValueError as exc:
            raise ValueError(f"line {index}: {exc}") from None
        point = Point(
            name=cell["name"], area=cell["area"].lower(), address=address,
            datatype=cell["datatype"].lower(), initial=cell["initial"],
            word_order=(cell["word_order"] or "ABCD").upper(),
            waveform=(cell["waveform"] or "const").lower(),
            wave_param=parse_wave_param(cell["wave_param"]),
            readonly=cell["readonly"].lower() in _TRUE, row=index)
        channel = _channel_for(channels, cell["channel"], ports, notes)
        device = _device_for(devices, channel, unit_id, cell["device"], index)
        device.points.append(point)
    project = Project(channels=list(channels.values()))
    return (project, notes) if with_notes else project


def read_csv_file(path: Path, **kwargs) -> Project:
    return read_csv(Path(path).read_text(encoding="utf-8-sig"), **kwargs)


def write_csv(project: Project) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    for ch in project.channels:
        for dev in ch.devices:
            for pt in dev.points:
                writer.writerow([ch.name, dev.name, dev.unit_id, pt.name, pt.area,
                                 pt.address, pt.datatype, pt.word_order, pt.initial,
                                 pt.waveform, format_wave_param(pt.wave_param),
                                 "true" if pt.readonly else "false"])
    return buf.getvalue()


def write_csv_file(project: Project, path: Path) -> None:
    Path(path).write_text(write_csv(project), encoding="utf-8", newline="")


def parse_wave_param(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (c for c in (text or "").split(";") if c.strip()):
        key, _, value = chunk.partition("=")
        out[key.strip()] = value.strip()
    return out


def format_wave_param(params: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in params.items())


def _rows(text: str) -> list[tuple[int, dict[str, str]]]:
    reader = csv.reader(io.StringIO(text))
    lines = [(i, row) for i, row in enumerate(reader, start=1)
             if row and not row[0].lstrip().startswith("#")]
    if not lines:
        raise ValueError("csv is empty")
    header = [c.strip().lower() for c in lines[0][1]]
    if header != HEADER:
        raise ValueError(f"header mismatch: expected {','.join(HEADER)}, "
                         f"got {','.join(header)}")
    out = []
    for index, row in lines[1:]:
        if len(row) != len(HEADER):
            raise ValueError(f"line {index}: expected {len(HEADER)} columns, got {len(row)}")
        out.append((index, dict(zip(HEADER, (c.strip() for c in row)))))
    return out


def _require_int(text: str, what: str, index: int) -> int:
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{what} {text!r} is not an integer") from None


def _channel_for(channels: dict[str, TcpChannel], name: str, ports: dict[str, int],
                 notes: list[str]) -> TcpChannel:
    if name in channels:
        return channels[name]
    if name in ports:
        port = ports[name]
    else:
        port = _guessed_port(name)
        notes.append(f"channel {name!r}: port guessed as {port} from its name, "
                     f"confirm it in the project settings")
    channels[name] = TcpChannel(name=name, host="0.0.0.0", port=port)
    return channels[name]


def _guessed_port(name: str) -> int:
    tail = name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() and 1 <= int(tail) <= 65535 else 502


def _device_for(devices: dict[tuple[str, int], Device], channel: TcpChannel,
                unit_id: int, device_name: str, index: int) -> Device:
    key = (channel.name, unit_id)
    if key not in devices:
        devices[key] = Device(name=device_name, unit_id=unit_id)
        channel.devices.append(devices[key])
    existing = devices[key].name
    if existing != device_name:
        raise ValueError(f"line {index}: unit_id {unit_id} is already used by device "
                         f"{existing!r}, got {device_name!r}")
    return devices[key]
```

`read_csv` 的默认返回类型是 `Project`，只有 `with_notes=True` 才返回 `(Project, notes)` 元组——
两条路径都已有测试覆盖，不要在调用点靠猜。

- [ ] **Step 4: Run it, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_csv_io.py`
Expected: 69 passed

- [ ] **Step 5: Commit**

```bash
git add modslave/project/csv_io.py tests/test_csv_io.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: CSV 点表读写，解析错误带行号"
```

---

## Task 8: 点表 → SimDevice/SimData

**Files:**
- Create: `modslave/backend/simdev.py`、`modslave/backend/events.py`
- Test: `tests/test_simdev.py`

口径全部来自探针（见"已实测事实"1、2、5）：四区独立 tuple、位区必须 `DataType.BITS`、
`count=1`、寄存器区用 `DataType.REGISTERS` + 原始 reg16（字序已在 codec 做完），
初始值直接编码进 `values`，主站一连就能读到。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_simdev.py
import pytest
from pymodbus.simulator import DataType, SimDevice
from modslave.backend.simdev import area_to_index, build_sim_devices, simdata_for_point
from modslave.codec.point_codec import encode
from modslave.project.model import Device, Point, Project, TcpChannel

def _proj(*points, unit=11, name="c1"):
    dev = Device(name="D1", unit_id=unit, points=list(points))
    return Project(channels=[TcpChannel(name=name, host="127.0.0.1", port=502,
                                        devices=[dev])])

def test_area_index_is_tuple_order():
    assert (area_to_index("co"), area_to_index("di"),
            area_to_index("hr"), area_to_index("ir")) == (0, 1, 2, 3)

def test_hr_simdata_carries_encoded_initial_and_count_is_one():
    pt = Point("CO", "hr", 0, "float32", initial="12.5", word_order="CDAB")
    entry = simdata_for_point(pt)
    assert entry.count == 1
    assert entry.datatype is DataType.REGISTERS
    assert entry.values == encode(12.5, "float32", "CDAB") == [0x0000, 0x4148]

def test_bit_point_uses_bits_datatype_and_one_value():
    pt = Point("启动", "co", 3, "bool", initial="true")
    entry = simdata_for_point(pt)
    assert entry.datatype is DataType.BITS
    assert entry.values == [True] and entry.address == 3

def test_readonly_flag_is_passed_through():
    pt = Point("版本", "ir", 0, "uint16", initial="7", readonly=True)
    assert simdata_for_point(pt).readonly is True

def test_device_is_built_as_four_blocks():
    proj = _proj(Point("h", "hr", 0, "uint16", initial="1"),
                 Point("i", "ir", 0, "uint16", initial="2"),
                 Point("c", "co", 0, "bool", initial="false"),
                 Point("d", "di", 0, "bool", initial="true"))
    device = build_sim_devices(proj)[0]
    assert isinstance(device, SimDevice) and device.id == 11
    assert isinstance(device.simdata, tuple) and len(device.simdata) == 4
    assert device.simdata[0][0].datatype is DataType.BITS
    assert device.simdata[2][0].values == [1]
    assert device.simdata[3][0].values == [2]

def test_same_block_entries_are_sorted_by_address():
    proj = _proj(Point("b", "hr", 10, "uint16", initial="1"),
                 Point("a", "hr", 0, "uint16", initial="2"))
    entries = build_sim_devices(proj)[0].simdata[2]
    assert [e.address for e in entries] == [0, 10]

def test_empty_areas_get_empty_lists():
    proj = _proj(Point("h", "hr", 0, "uint16"))
    blocks = build_sim_devices(proj)[0].simdata
    assert blocks[0] == [] and blocks[1] == [] and blocks[3] == []

def test_multiple_units_and_channels():
    p1 = _proj(Point("a", "hr", 0, "uint16", initial="1"), unit=11)
    p2 = _proj(Point("b", "hr", 0, "uint16", initial="2"), unit=12, name="c2")
    proj = Project(channels=[p1.channels[0], p2.channels[0]])
    devices = build_sim_devices(proj)
    assert [d.id for d in devices] == [11, 12]

def test_no_id_zero_device():
    """未定义的 unit id 必须不应答，所以绝不建 id=0 兜底设备。"""
    devices = build_sim_devices(_proj(Point("a", "hr", 0, "uint16")))
    assert 0 not in [d.id for d in devices]

def test_string_point_span_is_respected():
    """FAN-01 的 ASCII 是 46 41 4e 2d 30 31，两字节大端拼 reg。"""
    pt = Point("铭牌", "hr", 20, "string", initial="FAN-01")
    assert simdata_for_point(pt).values == [0x4641, 0x4e2d, 0x3031]

def test_same_unit_id_across_channels_is_rejected():
    p1 = _proj(Point("a", "hr", 0, "uint16"), unit=11, name="c1")
    p2 = _proj(Point("b", "hr", 0, "uint16"), unit=11, name="c2")
    proj = Project(channels=[p1.channels[0], p2.channels[0]])
    with pytest.raises(ValueError, match="unit_id 11"):
        build_sim_devices(proj)
```

- [ ] **Step 2: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_simdev.py`
Expected: FAIL — module missing

- [ ] **Step 3: Write simdev.py 与 events.py**

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/backend/simdev.py` 为准，不要照抄本节。**

```python
# modslave/backend/simdev.py
"""点表 -> pymodbus SimDevice。这是本工具唯一允许 import pymodbus 的地方之一。"""

from __future__ import annotations

from pymodbus.simulator import DataType, SimData, SimDevice

from ..codec.point_codec import encode
from ..project.model import Point, Project, area_is_bit

_INDEX = {"co": 0, "di": 1, "hr": 2, "ir": 3}


def area_to_index(area: str) -> int:
    return _INDEX[area]


def simdata_for_point(point: Point) -> SimData:
    registers = encode(point.typed_initial(), point.datatype, point.word_order)
    if area_is_bit(point.area):
        return SimData(point.address, 1, [bool(registers[0])], DataType.BITS,
                       readonly=point.readonly)
    return SimData(point.address, 1, [int(r) for r in registers], DataType.REGISTERS,
                   readonly=point.readonly)


def build_sim_devices(project: Project) -> list[SimDevice]:
    """一个 unit id 只能属于一个通道；同 unit 的点位合并进同一个 SimDevice。"""
    owner: dict[int, str] = {}
    by_unit: dict[int, tuple[list, list, list, list]] = {}
    for ch in project.channels:
        for dev in ch.devices:
            unit = dev.unit_id
            if unit in owner and owner[unit] != ch.name:
                raise ValueError(
                    f"unit_id {unit} used by more than one channel "
                    f"({owner[unit]} and {ch.name})")
            owner.setdefault(unit, ch.name)
            blocks = by_unit.setdefault(unit, ([], [], [], []))
            for pt in dev.points:
                blocks[area_to_index(pt.area)].append(simdata_for_point(pt))
    devices = []
    for unit_id in sorted(by_unit):
        co, di, hr, ir = by_unit[unit_id]
        for block in (co, di, hr, ir):
            block.sort(key=lambda e: e.address)
        devices.append(SimDevice(id=unit_id, simdata=(co, di, hr, ir)))
    return devices
```

```python
# modslave/backend/events.py
"""引擎往 GUI 抛的事件对象。字段是契约：加字段就加默认值，不要改名。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PacketEvent:
    channel: str
    direction: str          # "rx" | "tx"
    raw: bytes
    unit_id: int | None = None
    function_code: int | None = None
    parsed: str = ""
    mono: float = 0.0


@dataclass
class ChannelState:
    channel: str
    state: str              # "starting" | "running" | "stopped" | "error"
    detail: str = ""


@dataclass
class LogLine:
    level: str
    text: str


@dataclass
class PointChanged:
    channel: str
    unit_id: int
    area: str
    address: int
    value: object
    registers: list[int] = field(default_factory=list)
```

- [ ] **Step 4: Run it, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_simdev.py`
Expected: 28 passed

- [ ] **Step 5: Commit**

```bash
git add modslave/backend/simdev.py modslave/backend/events.py tests/test_simdev.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 点表映射成 SimDevice 四区，初始值编码进 values"
```

---

## Task 9: raw_writer —— 运行期读写寄存器

**Files:**
- Create: `modslave/backend/raw_writer.py`
- Test: `tests/test_raw_writer.py`

存在理由（"已实测事实"3）：`SimCore.async_setValues` 会把 `readonly` 点连引擎一起拒掉，
直接改 `SimRuntime.block[..][2]` 才写动，而主站侧的拒绝行为照旧生效。

- [ ] **Step 1: 先确认 SimCore 可以从模块路径导入**

Run:
```bash
.venv/Scripts/python.exe -c "from pymodbus.simulator.simcore import SimCore; print('ok')"
```
Expected: `ok`。这个符号不在 `pymodbus.simulator.__all__` 里，测试要用它得走完整模块路径。

- [ ] **Step 2: Write the failing test**

```python
# tests/test_raw_writer.py
import asyncio

import pytest
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simcore import SimCore
from modslave.backend.raw_writer import RawRegisterWriter

def core():
    hr = [SimData(0, 1, [1, 2], DataType.REGISTERS),
          SimData(20, 1, [9], DataType.REGISTERS, readonly=True)]
    ir = [SimData(0, 1, [7], DataType.REGISTERS, readonly=True)]
    co = [SimData(0, 1, [False] * 8, DataType.BITS)]
    di = [SimData(0, 1, [True] * 4, DataType.BITS)]
    return SimCore([SimDevice(id=11, simdata=(co, di, hr, ir))])

def test_read_defined_registers():
    assert RawRegisterWriter(core()).read_registers(11, "hr", 0, 2) == [1, 2]

def test_read_returns_none_outside_defined_range():
    writer = RawRegisterWriter(core())
    assert writer.read_registers(11, "hr", 5, 1) is None
    assert writer.read_registers(11, "hr", 0, 3) is None      # 跨过块尾

def test_unknown_unit_returns_none_and_bad_area_is_a_programming_error():
    """area 是代码写的，不是数据写的：拼错要炸出来，不能静默返回 None。"""
    writer = RawRegisterWriter(core())
    assert writer.read_registers(77, "hr", 0, 1) is None
    with pytest.raises(ValueError, match="xr"):
        writer.read_registers(11, "xr", 0, 1)

def test_write_visible_to_protocol_read_path():
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_registers(11, "hr", 1, [77]) is True
    assert asyncio.run(store.async_getValues(11, 3, 1, 1)) == [77]

def test_write_into_readonly_block_succeeds_for_engine_but_master_is_refused():
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_registers(11, "ir", 0, [555]) is True
    assert asyncio.run(store.async_getValues(11, 4, 0, 1)) == [555]
    assert asyncio.run(store.async_setValues(11, 4, 0, [999])) == 2   # 主站路径仍是 2
    assert asyncio.run(store.async_getValues(11, 4, 0, 1)) == [555]

def test_write_outside_defined_range_returns_false():
    writer = RawRegisterWriter(core())
    assert writer.write_registers(11, "hr", 9, [1]) is False
    assert writer.write_registers(11, "hr", 1, [1, 2, 3]) is False

def test_values_are_masked_to_16_bits():
    store = core()
    RawRegisterWriter(store).write_registers(11, "hr", 0, [0x1FFFF])
    assert asyncio.run(store.async_getValues(11, 3, 0, 1)) == [0xFFFF]

def test_bit_write_and_read():
    store = core()
    writer = RawRegisterWriter(store)
    assert writer.write_bits(11, "co", 3, True) is True
    assert writer.read_bits(11, "co", 0, 8) == [False, False, False, True,
                                                False, False, False, False]

def test_bit_write_keeps_siblings():
    store = core()
    writer = RawRegisterWriter(store)
    writer.write_bits(11, "co", 0, True)
    writer.write_bits(11, "co", 3, True)
    writer.write_bits(11, "co", 0, False)
    assert writer.read_bits(11, "co", 0, 8) == [False, False, False, True,
                                                False, False, False, False]

def test_discrete_inputs_are_writable_by_engine():
    writer = RawRegisterWriter(core())
    assert writer.write_bits(11, "di", 1, True) is True
    assert writer.read_bits(11, "di", 0, 4)[1] is True

def test_bit_write_outside_block_returns_false():
    writer = RawRegisterWriter(core())
    assert writer.write_bits(11, "co", 100, True) is False
    assert writer.read_bits(11, "co", 100, 1) is None
```

- [ ] **Step 3: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_raw_writer.py`
Expected: FAIL — `ModuleNotFoundError: modslave.backend.raw_writer`

- [ ] **Step 4: Write raw_writer.py**

> **注意：本节代码是裁决前的快照，与仓库实际实现有差异（见上方「执行期已裁决的偏离」表）；以 `modslave/backend/raw_writer.py` 为准，不要照抄本节。**

```python
"""运行期直接读写 SimRuntime 的寄存器列表。

不走 SimCore.async_setValues：那条路会检查 readonly 标志，而 readonly 是我们故意
给只读点设的（用来挡主站写），引擎自己刷新数值也会被它挡掉。
"""

from __future__ import annotations

_BLOCK = {"co": "c", "di": "d", "hr": "h", "ir": "i"}


class RawRegisterWriter:
    def __init__(self, core) -> None:
        """core: ModbusBaseServer.context，即 pymodbus 的 SimCore。"""
        self._core = core

    def write_registers(self, unit_id: int, area: str, address: int,
                        values: list[int]) -> bool:
        block = self._block(unit_id, area)
        if block is None:
            return False
        start, length, registers, _flags = block
        offset = address - start
        if offset < 0 or offset + len(values) > length:
            return False
        for i, value in enumerate(values):
            registers[offset + i] = int(value) & 0xFFFF
        return True

    def read_registers(self, unit_id: int, area: str, address: int,
                       count: int) -> list[int] | None:
        block = self._block(unit_id, area)
        if block is None:
            return None
        start, length, registers, _flags = block
        offset = address - start
        if offset < 0 or offset + count > length:
            return None
        return list(registers[offset:offset + count])

    def write_bits(self, unit_id: int, area: str, bit_address: int, value: bool) -> bool:
        block = self._block(unit_id, area)
        if block is None:
            return False
        start, _length, registers, _flags = block
        index = bit_address // 16 - start
        if not 0 <= index < len(registers):
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
        start, _length, registers, _flags = block
        first = bit_address // 16 - start
        last = (bit_address + count - 1) // 16 - start
        if first < 0 or last >= len(registers):
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
```

- [ ] **Step 5: Run it, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_raw_writer.py`
Expected: 67 passed

- [ ] **Step 6: Commit**

```bash
git add modslave/backend/raw_writer.py tests/test_raw_writer.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 直写寄存器列表，readonly 只挡主站"
```

---

## Task 10: TCP backend、日志降噪、依赖方向守卫

**Files:**
- Create: `modslave/backend/modbus_tcp.py`、`tests/conftest.py`
- Test: `tests/test_architecture.py`

`port=0` 不支持：拿系统分配端口要从 `ModbusTcpServer` 里摸 transport 对象，版本一变就碎，
而 Task 5 已把 `port=0` 判成校验错误，测试自己用 `free_port()` 挑端口。

- [ ] **Step 1: Write conftest.py**

夹具负责把 asyncio loop 起在专用线程里——这也是后面 GUI 计划的调用形态。

```python
# tests/conftest.py
from __future__ import annotations

import asyncio
import contextlib
import socket
import threading

import pytest

from modslave.backend.modbus_tcp import ModbusTcpBackend


def free_port() -> int:
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


@pytest.fixture
def open_backend():
    @contextlib.contextmanager
    def _open(channel, packet_sink=None):
        worker = _LoopThread()
        worker.start()
        backend = ModbusTcpBackend(channel, packet_sink=packet_sink)
        worker.call(backend.start())
        try:
            yield backend
        finally:
            worker.call(backend.stop())
            worker.stop()

    yield _open
```

- [ ] **Step 2: Write the failing architecture test**

```python
# tests/test_architecture.py
import pathlib

LIBRARY_ONLY = ("pymodbus", "serial")
GUI_ONLY = ("PySide6", "PyQt5")


def _imports_of(path: pathlib.Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            found.append(stripped)
    return found


def test_only_backend_touches_pymodbus():
    root = pathlib.Path("modslave")
    offenders = []
    for path in root.rglob("*.py"):
        if path.parts[1] == "backend":
            continue
        for stmt in _imports_of(path):
            if any(needle in stmt for needle in LIBRARY_ONLY):
                offenders.append(f"{path}: {stmt}")
    assert offenders == [], f"pymodbus leaked outside backend/: {offenders}"


def test_project_and_codec_do_not_touch_gui():
    offenders = []
    for folder in ("project", "codec"):
        for path in pathlib.Path("modslave", folder).rglob("*.py"):
            for stmt in _imports_of(path):
                if any(needle in stmt for needle in GUI_ONLY):
                    offenders.append(f"{path}: {stmt}")
    assert offenders == []


def test_backend_does_not_touch_gui():
    offenders = []
    for path in pathlib.Path("modslave", "backend").rglob("*.py"):
        for stmt in _imports_of(path):
            if any(needle in stmt for needle in GUI_ONLY):
                offenders.append(f"{path}: {stmt}")
    assert offenders == []
```

- [ ] **Step 3: Run them, verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_architecture.py`
Expected: FAIL — `modslave.backend.modbus_tcp` 不存在，conftest 导入它也在收集阶段炸

- [ ] **Step 4: Write modbus_tcp.py**

```python
# modslave/backend/modbus_tcp.py
"""pymodbus Modbus TCP 从站的封装：起停、报文钩子、日志降噪。"""

from __future__ import annotations

import asyncio
import logging
import time

from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer

from .raw_writer import RawRegisterWriter
from .simdev import build_sim_devices

_NOISE = "Datastore unable to fulfill request"


class _DropDatastoreNoise(logging.Filter):
    """未定义 unit id 会走 SimCore 的 devices[0] 回落抛 KeyError，pymodbus 把整段栈
    打进日志。协议行为是对的（主站收到异常码 4），噪音要压掉。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return _NOISE not in str(record.getMessage())


def configure_library_logging(level: int = logging.ERROR) -> None:
    for name in ("pymodbus", "pymodbus.logging", "pymodbus_internal"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not any(isinstance(f, _DropDatastoreNoise) for f in logger.filters):
            logger.addFilter(_DropDatastoreNoise())


def parse_packet(raw: bytes) -> tuple[int | None, int | None, str]:
    """MBAP 头 + 功能码，给人看的摘要。只解析，不参与协议。"""
    if len(raw) < 8:
        return None, None, f"short ({len(raw)} bytes): {raw.hex(' ')}"
    unit, function = raw[6], raw[7]
    body = raw[8:]
    if function & 0x80:
        code = body[0] if body else "?"
        return unit, function & 0x7F, f"unit {unit} fc {function & 0x7F} EXCEPTION {code}"
    return unit, function, f"unit {unit} fc {function} {len(body)}B {body[:8].hex(' ')}"


class ModbusTcpBackend:
    def __init__(self, channel, packet_sink=None, on_connection=None) -> None:
        self.channel = channel
        self.channel_name = channel.name
        self.port = channel.port
        self.started_at = 0.0
        self._packet_sink = packet_sink
        self._on_connection = on_connection
        self._server: ModbusTcpServer | None = None
        self._writer: RawRegisterWriter | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        configure_library_logging()
        from ..project.model import Project
        devices = build_sim_devices(Project(channels=[self.channel]))
        self._server = ModbusTcpServer(
            devices,
            framer=FramerType.SOCKET,
            address=(self.channel.host, self.channel.port),
            ignore_missing_devices=False,
            trace_packet=self._trace_packet,
            trace_connect=self._trace_connect,
        )
        self._writer = RawRegisterWriter(self._server.context)
        self._task = asyncio.create_task(self._server.serve_forever())
        await asyncio.sleep(0)
        self.started_at = time.monotonic()

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.shutdown()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._server = None
        self._writer = None
        self._task = None

    def _trace_packet(self, is_outgoing: bool, payload: bytes) -> bytes:
        if self._packet_sink is not None:
            raw = bytes(payload)
            unit, function, summary = parse_packet(raw)
            self._packet_sink(
                "tx" if is_outgoing else "rx", raw, unit, function, summary,
                time.monotonic() - self.started_at)
        return payload

    def _trace_connect(self, is_new: bool) -> None:
        if self._on_connection is not None:
            self._on_connection("open" if is_new else "close")

    def write_registers(self, unit_id, area, address, values):
        return self._writer.write_registers(unit_id, area, address, values)

    def read_registers(self, unit_id, area, address, count):
        return self._writer.read_registers(unit_id, area, address, count)

    def write_bits(self, unit_id, area, bit_address, value):
        return self._writer.write_bits(unit_id, area, bit_address, value)

    def read_bits(self, unit_id, area, bit_address, count):
        return self._writer.read_bits(unit_id, area, bit_address, count)
```

`stop()` 用了 `contextlib`，文件顶部补 `import contextlib`。`start()` 里 `await asyncio.sleep(0)`
只是让 `serve_forever` 有机会跑起来，不是"等端口就绪"——真正的就绪由集成测试里的
client 重试循环保证（Step 5）。

- [ ] **Step 5: Run, verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_architecture.py`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add modslave/backend/modbus_tcp.py tests/conftest.py tests/test_architecture.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: TCP backend、报文钩子与 pymodbus 降噪"
```

---

## Task 11: 真主站集成测试（M1 的门禁）

**Files:**
- Create: `tests/test_integration_tcp.py`

用 pymodbus 的**同步** client 当真实主站，从主线程连到 loop 线程里的从站。这条测试就是
"模拟器的 bug 不许伪装成平台 bug"的落地：把八个功能码、异常路径、字序、多 unit 路由、
总线扫描全部钉成自动回归。

- [ ] **Step 1: Write the test**

```python
# tests/test_integration_tcp.py
"""拿真实主站打这个从站。断言里的期望值全部来自 spec §5/§6 与探针实测，不是从实现倒推。"""

import logging
import struct
import time

import pytest
from pymodbus.client import ModbusTcpClient

from modslave.codec.point_codec import encode
from modslave.project.model import Device, Point, Project, TcpChannel
from modslave.project.validate import validate_project
from tests.conftest import free_port

# 一张典型隧道设备点表
POINTS = [
    Point("CO浓度", "hr", 0, "float32", initial="12.5", word_order="CDAB", readonly=True),
    Point("VI风速", "hr", 2, "float32", initial="3.25", word_order="ABCD"),
    Point("状态字", "hr", 4, "uint16", initial="11"),
    Point("累计电量", "hr", 5, "uint64", initial="72623859790382856", word_order="ABCD"),
    Point("铭牌", "hr", 9, "string", initial="FAN-01"),
    Point("启动", "co", 0, "bool", initial="false"),
    Point("故障", "di", 0, "bool", initial="true"),
    Point("版本", "ir", 0, "uint16", initial="7", readonly=True),
]


def regs_to_float(registers) -> float:
    return struct.unpack(">f", struct.pack(">HH", *registers))[0]


def tunnel_project(port: int, unit_id: int = 11) -> Project:
    dev = Device(name="风机-1", unit_id=unit_id, points=[
        Point(p.name, p.area, p.address, p.datatype, initial=p.initial,
              word_order=p.word_order, readonly=p.readonly) for p in POINTS])
    other = Device(name="COVI-2", unit_id=unit_id + 1, points=[
        Point("CO", "hr", 0, "uint16", initial="99")])
    return Project(channels=[TcpChannel(name="tcp", host="127.0.0.1", port=port,
                                        devices=[dev, other])])


def connect(port: int) -> ModbusTcpClient:
    """端口就绪由 loop 线程异步完成，重试到连上为止（最多 5 秒）。"""
    deadline = time.monotonic() + 5.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        client = ModbusTcpClient("127.0.0.1", port=port)
        try:
            if client.connect():
                return client
        except Exception as exc:            # noqa: BLE001
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"could not connect to :{port} ({last})")


@pytest.fixture
def master(open_backend):
    port = free_port()
    with open_backend(tunnel_project(port).channels[0]) as backend:
        client = connect(port)
        yield client, backend
        client.close()


def test_project_under_test_is_valid():
    assert validate_project(tunnel_project(5020)) == []


def test_float32_cdab_initial_value_reaches_master(master):
    """CDAB 读成天价是现场最常见的坑，这条是核心断言。"""
    client, _ = master
    got = client.read_holding_registers(0, count=2, device_id=11)
    assert got.registers == [0x0000, 0x4148]
    assert regs_to_float(got.registers) == pytest.approx(12.5)


def test_big_endian_float32(master):
    client, _ = master
    got = client.read_holding_registers(2, count=2, device_id=11)
    assert regs_to_float(got.registers) == pytest.approx(3.25)


def test_uint64_big_endian(master):
    client, _ = master
    got = client.read_holding_registers(5, count=4, device_id=11)
    assert got.registers == [0x0102, 0x0304, 0x0506, 0x0708]


def test_string_point(master):
    client, _ = master
    got = client.read_holding_registers(9, count=3, device_id=11)
    text = b"".join(r.to_bytes(2, "big") for r in got.registers)
    assert text.split(b"\x00")[0] == b"FAN-01"


def test_function_code_matrix(master):
    client, _ = master
    unit = 11
    assert client.read_coils(0, count=1, device_id=unit).bits[0] is False
    assert client.read_discrete_inputs(0, count=1, device_id=unit).bits[0] is True
    assert client.read_holding_registers(4, count=1, device_id=unit).registers == [11]
    assert client.read_input_registers(0, count=1, device_id=unit).registers == [7]

    assert client.write_coil(0, True, device_id=unit).bits[0] is True
    assert client.read_coils(0, count=1, device_id=unit).bits[0] is True
    assert client.write_coils(0, [False], device_id=unit) is not None
    assert client.read_coils(0, count=1, device_id=unit).bits[0] is False

    assert client.write_register(4, 777, device_id=unit).registers == [777]
    assert client.read_holding_registers(4, count=1, device_id=unit).registers == [777]
    assert client.write_registers(4, [1, 2], device_id=unit) is not None
    assert client.read_holding_registers(4, count=2, device_id=unit).registers == [1, 2]


def test_engine_side_write_is_visible_to_master(master):
    client, backend = master
    assert backend.write_registers(11, "hr", 4, [999]) is True
    assert client.read_holding_registers(4, count=1, device_id=11).registers == [999]


def test_readonly_point_blocks_master_but_not_engine(master):
    """CO浓度 标了 readonly：主站写它必须收到异常码 2，引擎自己照样能刷值。"""
    client, backend = master
    rejected = client.write_registers(0, [1, 2], device_id=11)
    assert rejected.isError() and rejected.exception_code == 2
    assert backend.write_registers(11, "hr", 0, encode(44.0, "float32", "CDAB")) is True
    assert regs_to_float(
        client.read_holding_registers(0, count=2, device_id=11).registers) == \
        pytest.approx(44.0)


def test_undefined_register_address_returns_exception_2(master):
    client, _ = master
    gap = client.read_holding_registers(30, count=1, device_id=11)
    assert gap.isError() and gap.exception_code == 2


def test_bit_area_gap_is_readable_and_writable(master):
    """实测的不对称：位区空洞读回 False 且可写，寄存器区空洞回异常码 2。
    这条把差异钉住，免得将来当 bug 改回去。"""
    client, _ = master
    gap = client.read_coils(4, count=4, device_id=11)
    assert not gap.isError() and gap.bits[:4] == [False] * 4
    assert client.write_coil(4, True, device_id=11).bits[0] is True
    assert client.read_coils(4, count=1, device_id=11).bits[0] is True


def test_unknown_unit_id_answers_exception_4_and_server_survives(master):
    client, _ = master
    miss = client.read_holding_registers(0, count=1, device_id=77)
    assert miss.isError() and miss.exception_code == 4
    alive = client.read_holding_registers(0, count=1, device_id=11)
    assert not alive.isError()


def test_bus_scan_does_not_flood_the_log(master, caplog):
    client, _ = master
    with caplog.at_level(logging.DEBUG, logger="pymodbus.logging"):
        for unit in range(1, 50):
            client.read_holding_registers(0, count=1, device_id=unit)
    texts = [str(r.getMessage()) for r in caplog.records]
    assert not any("Traceback" in t for t in texts)
    assert not any("Datastore unable to fulfill request" in t for t in texts)


def test_multi_unit_routing_is_isolated(master):
    client, _ = master
    assert client.read_holding_registers(0, count=1, device_id=12).registers == [99]
    client.write_register(0, 1, device_id=12)
    assert client.read_holding_registers(0, count=1, device_id=12).registers == [1]
    # unit 11 的同名地址不受影响
    assert regs_to_float(
        client.read_holding_registers(0, count=2, device_id=11).registers) == \
        pytest.approx(12.5)


def test_packet_sink_sees_both_directions(open_backend):
    port = free_port()
    seen: list[tuple] = []
    with open_backend(tunnel_project(port).channels[0],
                      packet_sink=lambda *args: seen.append(args)) as backend:
        client = connect(port)
        client.read_holding_registers(0, count=1, device_id=11)
        client.close()
    directions = [row[0] for row in seen]
    assert "rx" in directions and "tx" in directions
    rx = next(row for row in seen if row[0] == "rx")
    assert rx[2] == 11 and rx[3] == 3            # unit_id / function_code
    assert backend.port == port
```

`test_multi_unit_routing_is_isolated` 里 unit 12 的 CO 是 uint16、unit 11 的 CO浓度是
float32@CDAB，两者地址都是 0 但互不干扰——这条同时钉住"同一监听端口下按 unit id 分流"。

- [ ] **Step 2: Run it**

Run: `.venv/Scripts/python.exe -m pytest tests/test_integration_tcp.py -v`
Expected: 全通过。**如果有失败，先分清是"实现错"还是"期望值来自旧假设"**——本文件里每个
期望值都能对上"已实测事实"的某一条；对不上就停下报告，不要改期望值让它过。

- [ ] **Step 3: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: 全绿，无 skip、无 xfail、无恒真断言。

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_tcp.py
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "test: 真主站互测（功能码矩阵/异常码/字序/多unit/日志）"
```

---

## Task 12: 命令行入口与人工验收

**Files:**
- Create: `modslave/__main__.py`、`examples/covi-tunnel.csv`

- [ ] **Step 1: 写示例点表**

```csv
channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
tcp-5020,COVI-1,11,CO浓度,hr,0,float32,CDAB,12.5,const,,true
tcp-5020,COVI-1,11,VI风速,hr,2,float32,ABCD,3.25,const,,true
tcp-5020,COVI-1,11,风机状态,hr,4,uint16,ABCD,11,const,,false
tcp-5020,COVI-1,11,累计电量,hr,5,uint64,ABCD,72623859790382856,const,,false
tcp-5020,COVI-1,11,铭牌,hr,9,string,ABCD,FAN-01,const,,true
tcp-5020,COVI-1,11,启动命令,co,0,bool,ABCD,false,const,,false
tcp-5020,COVI-1,11,运行反馈,di,0,bool,ABCD,true,const,,true
tcp-5020,COVI-1,11,固件版本,ir,0,uint16,ABCD,7,const,,true
tcp-5020,照明回路-1,12,回路状态,hr,0,uint16,ABCD,0,const,,false
```

- [ ] **Step 2: Write the failing test for the CLI plumbing**

```python
# tests/test_cli.py
import pathlib
import subprocess
import sys

from modslave.csv_io_entry import load_point_table
from modslave.project.validate import error_lines

CSV = str(pathlib.Path("examples/covi-tunnel.csv").resolve())


def test_load_point_table_from_example_csv():
    project, notes, issues = load_point_table(CSV, port_overrides={"tcp-5020": 0})
    assert project.channels[0].devices[0].unit_id == 11
    assert len(project.channels[0].devices) == 2
    assert error_lines(issues) == []
    assert any("port" in n for n in notes)      # 端口是从通道名猜出来的，必须提醒


def test_bad_csv_is_reported_with_line_number(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "channel,device,unit_id,name,area,address,datatype,word_order,initial,"
        "waveform,wave_param,readonly\n"
        "tcp-1,D,11,x,hr,notanumber,uint16,ABCD,0,const,,false\n", encoding="utf-8")
    _proj, _notes, issues = load_point_table(str(path), port_overrides={"tcp-1": 5020})
    assert any("line 2" in str(i) for i in issues)


def test_module_runs_and_reports_validation_errors(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "channel,device,unit_id,name,area,address,datatype,word_order,initial,"
        "waveform,wave_param,readonly\n"
        "tcp-1,D,300,x,hr,0,uint16,ABCD,0,const,,false\n", encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "modslave", str(path), "--check"],
                          capture_output=True, text=True)
    assert done.returncode == 2
    assert "unit_range" in done.stdout or "unit_range" in done.stderr
```

- [ ] **Step 3: Run it, verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py`
Expected: FAIL — `ModuleNotFoundError: modslave.csv_io_entry`

- [ ] **Step 4: Write `modslave/csv_io_entry.py`（CLI 与 GUI 共用的装载口）**

```python
"""CSV/工程装载：把"读文件 + 端口覆盖 + 校验"收成一次调用。"""

from __future__ import annotations

from pathlib import Path

from .project.csv_io import read_csv_file
from .project.json_io import load as load_project
from .project.validate import Issue, validate_project


def load_point_table(path: str, port_overrides: dict[str, int] | None = None):
    """返回 (Project, notes, issues)。

    解析错误（表头不符、地址不是数字、列数不对）转成 `parse` 级 Issue 返回，
    不抛异常——CLI 与 GUI 都要"一次报全"，靠 try/except 分散在各处迟早漏掉。
    文件本身读不到（不存在、不是 utf-8）仍然抛，那是操作问题不是点表问题。
    """
    source = Path(path)
    overrides = port_overrides or {}
    notes: list[str] = []
    if source.suffix.lower() == ".json":
        project = load_project(source)
    else:
        ports = {k: v for k, v in overrides.items() if v}
        try:
            project, notes = read_csv_file(source, ports=ports, with_notes=True)
        except ValueError as exc:
            return (project_placeholder(), [],
                    [Issue("error", "parse", source.name, str(exc))])
    for channel in project.channels:
        override = overrides.get(channel.name)
        if override:
            channel.port = override
    return project, notes, validate_project(project)


def project_placeholder():
    """解析失败时给调用方一个空工程，免得下游判 None。"""
    from .project.model import Project
    return Project(channels=[])
```

- [ ] **Step 5: Write `__main__.py`**

```python
"""命令行起从站：python -m modslave examples/covi-tunnel.csv --port 5020

这是 M1 的验收入口，也是"没有 GUI 也能在 CI 里跑一遍"的最小外壳。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from .backend.modbus_tcp import ModbusTcpBackend
from .csv_io_entry import load_point_table
from .project.validate import error_lines


def _line(direction: str, raw: bytes, unit, function, summary: str, mono: float) -> None:
    print(f"{mono:8.3f} {direction.upper()} {summary} | {raw.hex(' ')}", flush=True)


async def _run(channel, quiet: bool) -> None:
    backend = ModbusTcpBackend(channel, packet_sink=None if quiet else _line)
    await backend.start()
    print(f"[modslave] listening on {channel.host}:{backend.port} "
          f"unit ids {_units(channel)}", flush=True)
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        # Windows 上 add_signal_handler 不可用，Ctrl+C 走 KeyboardInterrupt
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    await stop.wait()


def _units(channel) -> str:
    return ", ".join(str(dev.unit_id) for dev in channel.devices)


def _print_table(channel) -> None:
    for dev in channel.devices:
        print(f"  device {dev.name} (unit {dev.unit_id})")
        for pt in dev.points:
            print(f"    {pt.area:<3} {pt.address:<5} {pt.datatype:<8} "
                  f"{pt.word_order:<5} {str(pt.initial):<12} "
                  f"{'RO' if pt.readonly else '  '} {pt.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m modslave")
    parser.add_argument("point_table", help="CSV 或 project.json")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check", action="store_true", help="只校验，不监听")
    parser.add_argument("--quiet", action="store_true", help="不打印报文")
    parser.add_argument("--list", action="store_true", help="打印点表后退出")
    args = parser.parse_args(argv)

    project, notes, issues = load_point_table(args.point_table)
    for note in notes:
        print(f"[note] {note}", file=sys.stderr)
    for issue in issues:
        print(f"[{issue.level}] {issue}", file=sys.stderr)
    errors = error_lines(issues)
    if errors:
        print(f"[modslave] {len(errors)} error(s); refusing to start", file=sys.stderr)
        return 2
    if not project.channels:
        print("[modslave] the point table has no channel; check its first column",
              file=sys.stderr)
        return 2

    for channel in project.channels:
        if args.host:
            channel.host = args.host
        if args.port:
            channel.port = args.port

    if args.list:
        for channel in project.channels:
            _print_table(channel)
        return 0
    if args.check:
        print("[modslave] point table is valid")
        return 0
    if len(project.channels) > 1:
        print("[modslave] M1 serves one channel at a time; using "
              f"{project.channels[0].name}", file=sys.stderr)

    try:
        asyncio.run(_run(project.channels[0], args.quiet))
    except KeyboardInterrupt:
        print("\n[modslave] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Windows 的 Ctrl+C 走 `KeyboardInterrupt`（`add_signal_handler` 在 Windows 上抛
`NotImplementedError`，已 suppress），所以 `main()` 的 try/except 是主退出路径。

- [ ] **Step 6: Run the CLI tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py`
Expected: 3 passed

- [ ] **Step 7: 命令行冒烟（自动化之外的一次真实检查）**

```bash
.venv/Scripts/python.exe -m modslave examples/covi-tunnel.csv --port 5020 --list
.venv/Scripts/python.exe -m modslave examples/covi-tunnel.csv --port 5020
```
Expected: 第一条打印点表；第二条打印 `listening on 0.0.0.0:5020 unit ids 11, 12`，
并在收到请求时逐行打印 `RX unit 11 fc 3 2B 00 00 | ...`。Ctrl+C 干净退出。

- [ ] **Step 8: 人工验收（M1 收尾，需要你亲手做一次）**

1. 装 QModMaster（或用平台网关）连 `127.0.0.1:5020`，unit 11；
2. 读 `hr 0..1` → 应显示 12.5（CDAB）。若显示 -1.6E+13 之类，是字序错，回来改 codec；
3. 读 `hr 5..8` → uint64 = 72623859790382856；
4. 读 `hr 9..11` → "FAN-01"；
5. 写 `hr 4` = 1 → 读回 1；写 `hr 0` → **必须被拒**（只读点）；
6. 写 `co 0` = ON → 报文行里能看到 fc 5；读回 ON；
7. Windows 首次监听会弹防火墙提示：本机测试点"取消"即可（不走外部网卡），现场要放行时才点"允许"。

- [ ] **Step 9: 回写 spec（把 §12 的待实测项标记为已解决）**

编辑 `docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md`：

- §12 四条 M1 待实测项，逐条改成"已实测：<结论>"，并写明结论来自本计划 Task 8/9/11；
- §5 string 的字序口径改成只支持 `ABCD`；
- §6 补一条：`SimData.count` 是重复倍数，本项目一律 `count=1`；
- §8 补一条：运行期写寄存器走直接改列表，`readonly` 只用于挡主站；
- §11 M1 状态标"已完成"，并把 M4 波形对调进 §11 顺序已生效这件事写成一句变更说明。

- [ ] **Step 10: Commit**

```bash
git add modslave/__main__.py modslave/csv_io_entry.py examples tests/test_cli.py \
        docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md
git -c user.name=gaojie -c user.email=2084220263@qq.com commit -m "feat: 命令行起从站（M1 验收入口）"
```

---

## M1 完成检查表

全部满足才算 M1 结束。

- [ ] `.venv/Scripts/python.exe -m pytest` 全绿，无 skip/xfail
- [ ] `tests/test_architecture.py` 通过：pymodbus 只在 `backend/`，PySide6 一处都没有
- [ ] `test_function_code_matrix` 覆盖 01/02/03/04/05/06/15/16 八个功能码
- [ ] `test_undefined_register_address_returns_exception_2`、`test_readonly_point_blocks_master_but_not_engine`、`test_unknown_unit_id_answers_exception_4_and_server_survives` 三条异常路径都在
- [ ] `test_float32_cdab_initial_value_reaches_master` 用真实主站读出 12.5
- [ ] `test_bus_scan_does_not_flood_the_log` 通过（492 行 traceback 那件事不会在 GUI 里复现）
- [ ] 命令行起从站，QModMaster 手连一遍 Task 12 Step 8 的 6 项
- [ ] spec §12 的 M1 待实测项全部改成已实测结论
- [ ] 工作区干净（`git status --short` 无输出）

## 后续计划的边界

本计划**不做**：GUI（M2）、报文监视窗与多通道独立启停（M3 的界面部分）、波形引擎（M4）、
RTU（M5）。`Project.tick_ms`、`Point.waveform/wave_param`、`events.PointChanged` 已经在本
计划里占好字段，M4 只需实现 `engine/` 层，不必回头改点表模型。

