# 结构整理（第二轮）：两条门禁 + 重复表 + 死代码

日期：2026-10-15　｜　范围：把上一轮拆分（`09a58be`）留下的三件事收干净——**CI 的两条门禁**
（`ruff check` / `mypy`）、**四处各写一遍的通道四态表**、**四个全仓无引用的函数**；顺带把图标
从 `theme.py` 拆出去。

> 上一轮的验收只跑了用例（1559 passed），**没有跑 CI 真正跑的那两条静态检查命令**。所以那次
> 提交在本地是绿的、在 CI 上会红。这份记录的第一条就是补这个洞。

---

## 1. 事实（实测）

### 1.1 上一轮提交会让 CI 红（两条门禁都红）

* `ruff check`：**8 条**。原因是自查时只用了 `--select F,I`，而 `pyproject.toml` 里选中的规则
  还包含 `RUF`（当时没人在本地跑过不带 `--select` 的那条）。具体是：
  * `RUF100` 7 条——`session.py` / `writes.py` / `main_window.py` 里我写的 `# noqa: F401`
    其实没抑制任何东西（名字都在 `__all__` 里，或本来就有人用）；
  * `RUF022` 1 条——`main_window.py` 的 `__all__` 没排序。
* `mypy`：**32 条** `attr-defined`。`pyproject.toml` 的 `files` 含 `simbench/engine`，而
  `session.py` 拆成 mixin 之后，mypy 单独检查 `reads.py` / `writes.py` 时看不见 `Session` 上那些
  名字（`_lock` / `_channels` / `publish_log` / `_transport` / `_require` ...），于是逐行报
  `"ReadMixin" has no attribute "_lock"`。

### 1.2 通道四态在四处各写了一遍

| 位置 | 内容 |
|---|---|
| `channel_panel.STATUS_LABELS` | `{"stopped": "○ 停止", "starting": "◐ 启动中", ...}` |
| `device_tree._STATUS_MARK` | `{"stopped": "○", "starting": "◐", ...}` |
| `window_consts._STATUS_WORD` | `{"stopped": "停止", "starting": "启动中", ...}` |
| `theme._STATUS_COLORS` | 颜色（按深浅模式两套） |

前三张表里，`STATUS_LABELS` **恰好等于**"`STATUS_MARK` + 空格 + `STATUS_WORD`"（逐条核对过）。
加第五个状态要同时改三处，漏一处的表现是"某个通道的状态格空着"——界面上分不出那是没实现
还是没状态。

### 1.3 四个函数全仓无引用

用 AST 扫全仓（`simbench/` + `tests/` + `packaging/` + `examples/`）的标识符出现次数，逐个比对
定义处：`device_tree.menu_actions`、`rows_pump.Pump.age`、`ledger.owned_device_instances`、
`fsio.write_bytes_atomic`。其中 `menu_actions` 的文档串写着"`contextMenuEvent` 与用例走**同一个**
构造口"——实际上真身是 `_menu_for`，用例（`test_gui_main_window.py` / `test_gui_point_table.py`）
走的是 `contextMenuEvent` 与 `point_table.row_menu_actions`。也就是说它不只是死代码，注释还在
**指错地方**。

---

## 2. 改了什么

1. **两条门禁转绿**
   * `ruff`：删掉那两个没人用的 import（`_READ_TIMEOUT` / `_WRITE_TIMEOUT` 在 `session.py` 里
     本来就不用了）；把再导出换成项目**原有**的写法 `from .x import y as y`（`backend/__init__.py`
     一直这么写，见它同一条注释）；`__all__` 交给 `ruff --fix` 排序。
   * `mypy`：新增 `engine/session_host.py`，把两个 mixin 用到的宿主名字**声明一次**（属性写注解，
     三个方法体写 `raise NotImplementedError`）。`Session(ReadMixin, WriteMixin)` 里 `Session`
     自己的实现在 MRO 上排在声明之前，所以这些声明运行时不加任何行为；真被调到说明继承顺序
     被改坏了，那时候**当场喊出来**比静默回一个 `None` 强。
2. **四态收成一处**：新增 `gui/channel_state.py`（`STATUS_MARK` / `STATUS_WORD` / `STATUS_LABELS`
   / `STATUS_ORDER`）。`channel_panel` / `device_tree` / `main_window` 改为引用它；`STATUS_LABELS`
   由两张表合成，逐条核对过与原值、原键序**完全一致**。
3. **删死代码四个**（含那条指错地方的 `menu_actions` 文档串）。
4. **图标从 `theme.py` 拆出** → `gui/icons.py`（641 → 453 + 197 行）。图标只与 theme 相交一处
   （按模式取一组亮一档的颜色），依赖方向 `icons → theme`，没有反向依赖。相应地，图标那三条
   断言从 `tests/test_gui_theme.py` 搬到新的 `tests/test_gui_icons.py`，并补了两条：每个图标
   **真画一遍**（现画的图标不进仓库、不参与打包校验，这是"装完按钮空白"唯一能提前抓到的地方）、
   窗口图标按 16/32/48/64/128/256 六档**各自画**而不是拉伸。

---

## 3. 口径与代价

* **`STATUS_LABELS` 这个名字留在 `channel_panel`**：用例与文档都按 `channel_panel.STATUS_LABELS`
  写（4 个测试文件 import 它），所以名字不动、值改为从 `channel_state` 取——去重不该顺手改调用方
  的名字。
* **`session_host` 用 `raise` 不用 `...`**：`...` 在继承顺序被改坏时会静默返回 `None`，那正是本
  项目最防的"演一出没发生的事"。
* **本轮不动 `theme.` 这个名字的用法**（`theme.qss` / `theme.set_mode` ...）：只有图标挪了家，
  颜色与字体仍归 `theme`。
* 代价：`gui/` 多了两个小模块（`channel_state.py` / `icons.py`），`engine/` 多了一个只给类型
  检查器看的 `session_host.py`。换来的是"四态只有一处定义"和"mypy 能单独检查两个 mixin"。

---

## 4. 证据

```
$ ruff check                    # CI 的 lint 第一步（全仓、不带 --select）
All checks passed!

$ python -m mypy                # CI 的 lint 第二步
Success: no issues found in 25 source files

$ python -m pytest -q           # CI 的 test 那一步
1563 passed（比上一轮多 4 条：新增 `tests/test_gui_icons.py`）
```
