# P2/P4/P5：改名、事件契约挪到根上、把 GUI 的测试缝显式化

日期：2026-10-16　｜　范围：上一轮架构审查里剩下的三项（P1/P3 已在
`2026-10-16-type-coverage-and-net-purity.md` 里落地）。三项各自独立，分三个提交。

> 三项的共同点：都在收拾「名字/位置说的和实际做的不一样」。P2 是类名在说谎，P4 是模块位置
> 逼着架构守卫开口子，P5 是测试缝藏在模块全局里、读源码看不出来。

---

## P2　`ModbusBackendBase` 改名 `BackendBase`（`modbus_base.py` → `base.py`）

**事实**：这一层承载的是「与传输无关的那一半」（点位摊平、波形 tick、值归属、报文/日志钩子、
启停顺序），而 **S7 也继承它**——`backend/s7_server.py: class S7Backend(ModbusBackendBase)`。
S7comm 不是 Modbus。名字里带 Modbus 的代价是读者会以为它只管 Modbus 那几条路，于是它自己的
文档串得反过来写一句「与传输无关」来纠正自己；spec 里也一样，每处都要附一句解释。

**改动**：改名 + 更新门面再导出、各后端、`engine/records.py` 的 `ChannelRuntime.backend` 注解、
测试与 spec §4。

**顺带**：`modbus_tcp.py` 那个「老路径再导出」块，2026-10-16 用 AST 核过**从本模块取走的全部
名字**——只有 `_DropDatastoreNoise` / `configure_library_logging` 是从基类模块再导出的
（`tests/test_integration_tcp.py` 用）；`BackendStartError` 与 `BackendBase` 无人从那里取，
已从该块移除。原来的注释写的是「import 它的人不少」——那句话没有任何东西能核对。

**还修了一处既有损坏**：`engine/records.py` 的文档串里有两个控制字符（CR 与换页符）。成因是
早前用 PowerShell 生成文件时被反引号转义吃掉的（PowerShell 的反引号是转义符：`` `r `` → CR、
`` `f `` → FF），于是「读侧（`reads.py`）」成了「读侧（<CR>eads.py）」。副作用是 git 一直把该
文件当**二进制**（`ls-files --eol` 显示 `-text`），它的行尾从不做归一。现在还原文、行尾统一
LF，该文件重新是文本。

## P4　`backend/events.py` → `simbench/events.py`（根上）

**事实**：这几个类（`PacketEvent` / `ChannelState` / `LogLine` / `PointChanged`）的文档串自称
「引擎往 GUI 抛的事件对象」——它们是**引擎与界面之间的契约**，产出方是 backend，消费方是
engine 与 gui。住在 backend 包里的代价直接落在守卫上：「界面不许 import backend」那条线必须为
它开一个口子（`gui → backend.events`）。

**改动**：挪到根上（与 `swallow.py` / `settings.py` 同级），三方都从 `simbench.events` 取；
守卫收紧成两条——engine 只许走门面，**gui 连门面都不许碰**（引用数 0，含植入样本）。

**不能挪进 `engine/`**：backend 是这些事件的**产出方**，那样会变成 backend → engine，把依赖
方向弄反。

**顺带**：`events.py` 挪到根上后不纳入 mypy 就等于**悄悄掉出闸门**（实测从 41 个文件掉到 40 个）。
于是把根下那七个模块一并纳入（现在 47 个），成本 4 处报错全在 `__main__.py`——同样是 `object`
与 `Optional` 的洞：`_serve` 的 `channels`/`started` 补上真实类型，并给「走到这里点表一定装好了」
这条不变量补一句人话守卫。同批给 `project/model.py` 加了 `Channel = TcpChannel | SerialChannel |
S7Channel` 类型别名（这个联合原先手写两处，第三处一写就发现读不动）。

## P5　GUI 的测试缝显式化（`gui/deps.py` 的 `WindowDeps`）

**事实**：用例靠 `monkeypatch.setattr(main_window, "X", ...)` 换掉 7 个**模块全局**：`PORT_PROBE`、
`PointPropertiesDialog`、`IssueDialog`、`save_project_as`、`open_project`、`import_table`、
`choose_offset`。代价有两处：

1. 混在别的模块里的方法（`window_edit` / `window_io`）必须**运行时**去 `main_window` 上现查那个
   名字（上一轮为此写了 `_mw()` / `_probe()`）——一个纯粹为迁就测试写法而存在的间接层，读源码
   看不出「这个方法到底依赖什么」；
2. 补丁改的是模块全局：谁还没还原、两条用例的先后都会互相影响。

**改动**：收成一份显式的 `WindowDeps`（默认就是真件），主窗构造时接收；用例两种写法都落在对象
上——构造时注入（`MainWindow(..., deps=gui_deps(probe=lambda _p: True))`）或拿到窗口后换某一样
（`monkeypatch.setattr(window.deps, "save_project_as", fake)`）。`_mw()` / `_probe()` 删除，
`window_consts.PORT_PROBE` 删除。

共享的假件放 `tests/gui_support.py`（`SilentIssuesDialog` + `gui_deps()`），**不放 conftest**：
conftest 是所有用例都会 import 的，而它要 import PySide6——内核用例不该因为跑一次 pytest 就被
装上 Qt（那是 M1 特意拆开的）。

---

## 证据

```
$ ruff check                    # CI 的 lint 第一步
All checks passed!

$ python -m mypy                # CI 的 lint 第二步（覆盖 47 个源文件）
Success: no issues found in 47 source files

$ python -m pytest -q           # CI 的 test 那一步
1568 passed（比上一轮 +1：架构守卫里那条 gui 不碰 backend）
```

守卫：`test_architecture.py` 新增 `test_gui_does_not_touch_backend_at_all`（gui 对 backend 的
引用数必须是 0，含植入样本）。
