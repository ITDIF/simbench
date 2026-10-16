# 类型检查覆盖到边界 + net 的纯模块摘掉系统层

日期：2026-10-16　｜　范围：上一轮架构审查里排在最前两项的可执行部分——**P1 让 mypy 覆盖
`backend/` 与 `master/`**、**P3 把 `NetError` 挪成叶子模块**。P2（`ModbusBackendBase` 改名）
按当时的建议留到单独一轮。

> 这两项的共同点是：都不是"功能"，而是"让已有的说法变成可检查的"。P1 让 `engine → backend`
> 这条边界的两侧都进闸门；P3 让 `net` 里那句"纯函数，不读系统"变成真的。

---

## 1. 事实（实测）

### 1.1 engine 被类型检查，它调用的 backend 不被检查

`pyproject.toml` 的 `files` 原先只有 `simbench/project` / `codec` / `engine`。engine 里每一处
`make_backend(...)` 的返回值、每个 `ModbusBackendBase` 钩子的签名，都来自 `backend/`——而那棵树
不在闸门内，所以这条边界只有一半可信。上一轮那 32 条 mypy 报错全部出在 `engine/`，同一条继承链
在 `backend/` 里没有任何人看，正是这个洞的表现。

实测纳入成本：**15 处报错 / 5 个文件**（不是当初担心的"几百条"）。

### 1.2 `NetError` 住在 `state.py`，于是"纯函数"这句话是假的

`net/state.py` 顶层 `import ctypes` / `import subprocess`。而 `NetError` 定义在那里，被 7 个模块
引用：

* `endpoints.py` 的文档串写着"纯函数，不读系统"，只为 `except NetError` 而 import `state`；
* `spec.py` 只是个文件格式模块，同样如此；
* `plan.py` 更别扭：它的文档串写着"**这是"要不要动系统"的唯一判据，所以它必须能脱离真实网卡
  单测**"，却为了一个 `Address` 而 import `state`。

实测（改之前）：`import simbench.net.endpoints` 之后，`ctypes`、`ctypes.wintypes`、`subprocess`
都在 `sys.modules` 里。

---

## 2. 改了什么

### 2.1 mypy 覆盖 `backend/` 与 `master/`（15 处，全部用真标注修）

| 位置 | 原来 | 改成 |
|---|---|---|
| `master/modbus.py`（5 处） | `_client: object \| None` | `ModbusBaseSyncClient \| None`（`TYPE_CHECKING` 下取，保住"pymodbus 没装也能 import 本模块"） |
| `s7_server.py`（3 处） | 可选 snap7 模块由 `except` 分支推类型 | 显式声明 `ModuleType \| None`，并把 `load_library()` 前的判空补全（原来只判了 server） |
| `s7_server.py`（2 处） | `_flatten_points -&gt; list[tuple[int, object]]`；`_build_areas -&gt; 4 元组` | 与基类对齐 `list[tuple[int, Point]]`；补成实现一直在回的 6 元组 |
| `modbus_udp.py` / `modbus_tcp.py`（1 处） | `SERVER_CLASS = ModbusTcpServer`（被推成 `type[ModbusTcpServer]`） | `SERVER_CLASS: type[ModbusBaseServer]`（两个服务端类没有继承关系，公共基类才是对的宽窄） |
| `simdev.py`（1 处） | `simdata=tuple(blocks)` | `simdata=blocks`（`blocks` 本来就是 4 元组，多套一层反而降级成不定长） |

**顺手逮到一处真隐患**：`ownership.write_targets` 只挡了 `direction != "rx"`，而
`packet.unit_id` 与 `packet.function_code` 在 `PacketEvent` 上是 Optional——缺了照样会返回
`(None, 区, 地址, 数量)`，往归属表里塞一个 `None` 键。那一侧按 `(unit_id, 区, 地址)` 查，
永远命中不了它，现场只表现成"这条曲线不动"。现在早退，并有用例钉住。

### 2.2 `NetError` 挪成叶子 `net/errors.py`

* 新建 `net/errors.py`（只放 `NetError`，除 `__future__` 外什么都不 import）；
* 7 个模块 + 2 个用例文件改为从 `errors` 取；
* `plan.py` 的 `Address` 收进 `if TYPE_CHECKING:`——它只出现在注解里，而
  `from __future__ import annotations` 让注解不被求值，所以运行时那一份依赖本来就是多余的。

实测（改之后）：`import simbench.net.endpoints` + `spec` + `plan` + `ledger` 之后，
`ctypes` 与 `subprocess` **都不在** `sys.modules` 里，而 `plan_addresses` 照常可调用、mypy 照常过。

---

## 3. 口径

* **不加 `# type: ignore`**：15 处全部找到正确的标注。pymodbus 的两个公共基类
  （`ModbusBaseSyncClient` / `ModbusBaseServer`）是现成的，snap7 那三个可选模块用
  `ModuleType | None` 就够了。撒 ignore 会把闸门关掉，那与"扩覆盖"的初衷相反。
* **`TYPE_CHECKING` 是"类型依赖"的正规写法**，不是掩盖：`plan.py` 与 `Address` 的关系本来
  就是"操作这种数据"，运行时不需要那个类对象（注解不求值）。同一写法在 `master/modbus.py`
  里用来保住 pymodbus 的延迟加载。
* **`gui/` 与 `net/` 仍在闸门外**：Qt 的动态用法与 ctypes/Win32 结构体要补到能过检查是另一档
  工作量。挂在 `pyproject.toml` 的注释里，等有人真正去碰那两层时再拉进来。

---

## 4. 证据

```
$ ruff check                    # CI 的 lint 第一步
All checks passed!

$ python -m mypy                # CI 的 lint 第二步（覆盖从 25 个源文件到 41 个）
Success: no issues found in 41 source files

$ python -m pytest -q           # CI 的 test 那一步
1567 passed（比上一轮 +4：`test_ownership` 一条 + `test_architecture` 三条）
```

守卫：`tests/test_architecture.py` 新增两条——`net` 的纯模块（`errors` / `spec` / `ledger` /
`plan` / `endpoints`）运行时不许 import `state` / `ctypes` / `subprocess`（`TYPE_CHECKING`
里的不算），外加一条"守卫不是空跑"的对照（`state.py` 里确实有人 import 这两个库）与一株植入样本。
