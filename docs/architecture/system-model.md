# simbench 现状架构模型

生成日期：**2026-10-16**　｜　取证基线：git HEAD `cbd2322`（已 `git fetch`，与 `origin/main` 一致）
产出流程：`architecture-visualization` 的 `system-modeler`（场景）+ `c4model` / `graphviz`（图源码格式）

配套文件（都在本目录）：

| 文件 | 是什么 | 怎么看 |
|---|---|---|
| `system-model.structurizr.dsl` | 现状架构的**规范来源**：一份模型 + 四张视图（L0 Landscape / L1 Context / L2 Container / L3 Component） | 用 Qoder 的 Structurizr DSL 查看器打开 |
| `module-deps.dot` | 包级依赖图，边由 **AST 实测**得出（不是按目录猜） | 用 Qoder 的 DOT 查看器打开；本机没装 graphviz，没做本地渲染 |
| `system-model.md` | 本文件：叙述 + 证据索引 + 置信度 + 未决项 | 直接读 |

## 这份模型回答什么、不回答什么

**回答**：simbench 这个系统边界在哪、由哪些可独立部署的单元组成、内部八个包各自负责什么、它们之间的依赖方向是怎样的、哪些结论有代码直接证据。

**不回答**（本次明确不在范围内，要问请另起一轮）：风险清单与整改建议、某条业务流程的时序细节、改动爆炸半径、部署与发布拓扑、以及"这些图还新不新鲜"的校验机制。

**阅读顺序**：先看 `L1-Context` 弄清边界与三个方向（谁连进来 / 我们连出去到哪 / 我们会改动什么），再看 `L2-Container` 弄清三种打包形态与那个提权子进程，最后看 `L3-Component` 与 `module-deps.dot` 弄清内部分层。

---

## 一、系统边界：三个方向

simbench 平时**当**从站被上游主站读，必要时反过来**当**主站去读别人的设备，另外它还有一层会**改动现场机器本身**的网络配置能力。三个方向必须分开看，因为它们的信任级别完全不同。

| 方向 | 对端 | 同步性 | 会不会改对方状态 | 证据 | 置信度 |
|---|---|---|---|---|---|
| **连进来** | 上游主站（Modbus Poll / QModMaster / 组态软件 / 隧道机电平台）经六种传输读我们模拟的设备 | 异步，主站发起 | 会——主站的写帧会触发"人工接管"判定 | `README.md:72-84`、`simbench/__main__.py:161-164`、`packaging/smoke.py` | 高 |
| **连出去** | 真实现场设备（PLC / Modbus 从设备），主站核对模式 | 同步，我们发起 | **不会**，只读 | `simbench/master/modbus.py:108-113`；`master/` 全树无任何 `write_*` 调用（已 grep 核对） | 高 |
| **改本机** | Windows 网络栈：网卡、IP、虚拟环回网卡 | 同步 | **会**，且要提权 | 读 `simbench/net/state.py:142-163`；写 `simbench/net/cli.py:277-300`、`simbench/net/win32.py:154-233` | 高 |

第三个方向是全仓唯一会改变现场机器状态的地方，被结构性地圈在 `net/` 一个包里（守卫 G3：`subprocess` 只许出现在 `net/`）。`net/` 自己的安全口径是"现场不许留痕"：只读不提权、增删一律走显式提权路径、删除只针对账本里记过的地址（`simbench/net/__init__.py:16-25`）。

**人**：两个角色（现场调试工程师 / 改代码的人）是从 `README` 的章节划分推出来的（"跑起来"、"现场"对"改代码的人要先装的"），代码里没有任何角色、权限或身份概念 —— 所以这两个 actor 标 **推断，中等置信度**。

## 二、L2 容器：三种打包形态 + 一个提权子进程 + 五个落盘存储

三种形态**共用同一份包树**，区别只在 PyInstaller 的 console 开关与压缩方式（`packaging/simbench.spec:51-107`）：

- `simbench.exe`（控制台）—— `--check` / `--list` / 无头监听 / `net` 子命令
- `simbench-gui.exe`（无控制台）—— 双击开界面
- `simbench-onefile.exe` —— README 明说**不是**发布形态（启动要解压，慢 3–4 倍）

走界面还是走命令行由 `packaging/entry.py:17-39` 判：stdout 为 `None`，或 exe 名以 `-gui` 结尾。

**提权子进程不是另一个程序**：`net` 的写操作（`device create|remove` / `up` / `clean`）用 `Start-Process -Verb RunAs` 把**同一个 exe** 再启一遍，参数经 `%TEMP%` 里一个带 pid 的 JSON 任务文件传递，结果经另一个 JSON 文件回传（`simbench/net/elevate.py:56-93`）。只有这四条命令提权，GUI 与只读命令不提权（`simbench/net/cli.py:256-262,385-394,439-446,553-563`）。

五个落盘存储，写入路径全部经 `project/fsio.py` 的同一套"临时文件 + `os.replace` 原子替换、覆盖前留一份 `.bak`"（`fsio.py:35-91`；调用方 `csv_io.py:146`、`json_io.py:286`、`xlsx_io.py:196`、`net/spec.py:124`、`master/report.py:70,85`）：

| 存储 | 位置 | 作用 |
|---|---|---|
| 点表 / 工程文件 | 用户指定路径 | `.csv` / `.xlsx` / `.json` 三种，**按后缀分派**，后缀不认识直接拒绝（`project/formats.py:30-87`） |
| 网络声明 | 点表旁边 `x.net.json` | 设备名 ↔ ip/port/prefix（`net/spec.py:37-71`） |
| 界面设置 | `%APPDATA%\simbench\settings.json` | 主题等偏好（`settings.py:36-45`） |
| 网络账本 | `%LOCALAPPDATA%\simbench\net-ledger.json` | 记"我们加过什么"，是 `clean` 只回收自己东西的唯一依据；**账本读不出来就当什么都不拥有，绝不删**（`net/ledger.py:13-15,92-95`） |
| 日志与崩溃栈 | `%LOCALAPPDATA%\simbench\logs\` | 未捕获异常写完整调用栈，解释器级崩溃靠 `faulthandler` 记原生栈（`diagnostics.py:1-45`） |

## 三、L3 组件：八个包，实测是一个无环 DAG

包级依赖边由 AST 实测得出（重跑方法见文末）。实测结果：

```
project  →  （不 import 任何内部包）        ← 最底层的纯核，2760 行
codec    →  project
backend  →  codec, project, events
master   →  project, codec(延迟)
net      →  project, csv_io_entry(延迟)
engine   →  backend(仅门面), codec, project, events, swallow, net.endpoints(延迟)
gui      →  engine, project, master, net, events, settings, csv_io_entry, __about__
__main__ →  gui / master / net / project / crosscut（全部延迟 import）
```

无环。`project/` 实测不 import 任何内部包，也不碰 Qt 与 pymodbus —— 这是"点表层可脱离界面与协议库单测"这句话在结构上成立的原因，而不是一句口号。

各包职责与体量：

| 包 | 文件 / 行数 | 职责 | 关键证据 |
|---|---|---|---|
| `gui/` | 25 / 4971 | PySide6 界面：通道面板 + 设备树 + 点位表 + 报文窗 + 日志页 + 核对页 + 网络页。主窗体拆成三个 mixin（`window_menus` / `window_edit` / `window_io`），拆分动机是打断 mixin→main_window 的循环依赖 | `gui/main_window.py:65`、`gui/window_consts.py:1-4` |
| `engine/` | 12 / 1775 | `Session` 是**唯一**对 GUI 暴露的门面（启停 / 改值 / 拉行 / 订阅事件）；`EngineRunner` 持有那条 asyncio 线程 | `engine/session.py:27-43`、`engine/runner.py:39-87` |
| `backend/` | 11 / 2362 | 六种传输的后端；与传输无关的那一半（波形 tick、值归属、报文钩子、启停顺序）在 `BackendBase`。`__init__.py` 是门面，按 `channel_transport()` 分派 | `backend/__init__.py:36-62`、`backend/base.py:100-617` |
| `project/` | 9 / 2760 | 领域模型 + 三种文件格式读写 + 校验 + 带碰撞回避的编辑 | `project/model.py:247-490` |
| `net/` | 12 / 2180 | 读网卡现状、算差异、按需增删 IP 与虚拟网卡 | `net/__init__.py:1-25` |
| `codec/` | 4 / 623 | 纯换算：值↔寄存器（数据类型 × 字序）、S7 值↔字节、波形发生器 | `codec/point_codec.py:34-60`、`codec/waveform.py:1-6` |
| `master/` | 4 / 382 | 主站核对模式，只读；产出四类结论（对得上 / 对不上 / 没应答 / 读出错）与一份报告 | `master/scan.py:1-48` |
| 根下横切 | 8 / 963 | `events`（事件契约）、`swallow`（吞异常唯一出口）、`settings`、`diagnostics`、`csv_io_entry`、`__main__`、`__about__` | `events.py:1-11` |

### 领域模型与身份键

`Project{channels, tick_ms}` → `Channel`（Tcp / Serial / S7 三种）→ `Device{name, unit_id, points}` → `Point{name, area, address, datatype, initial, word_order, waveform, wave_param, readonly, bit}`（`project/model.py:247-490`）。

三个身份键，各自不同层级：通道按**名字**（`model.py:486-490`）、设备按**同一通道内的 unit_id**（`edit.py:340-344`）、点位按 **(area, address, bit)** 三元组（`edit.py:465-483`）。

### 后端契约：一个门面、五个必填钩子

`make_backend(channel, *, packet_sink, on_connection, tick_ms, log_sink, extra_endpoints)` 是全仓**唯一**按传输选后端的地方（`backend/__init__.py:36-62`），分派键是 `channel_transport(channel)`：`tcp` / `udp` / `rtutcp` / `rtu` / `ascii` / `s7`，其余抛 `UnsupportedTransportError`。

子类必须填五个钩子：`_probe`（探针）、`_create_server`（建 server）、`_await_ready`（等起来）、`_split_frames`（切帧）、`_parse_frame`（解析）（`backend/base.py:189-247`）。另有约十个带默认实现的可覆盖钩子，S7 后端覆盖了其中大半（`backend/s7_server.py:103-270`）。

**可选依赖只有 snap7**：`s7_server.py:47-59` try-import 并在失败时置 `None`，缺依赖只在**起 S7 通道那一刻**报一句带安装提示的 `BackendStartError`（`:111-117`），原生 DLL 加载失败另有一道闸（`:118-135`）。`pymodbus` 与 `pyserial` 是**硬**依赖，没有任何可选处理（`backend/simdev.py:5`、`raw_writer.py:9-10`、`modbus_tcp.py:20-21`、`modbus_rtu.py:27-29`）。`openpyxl` 同样是懒加载（`project/xlsx_io.py:43-52`）。

### 运行时线程结构

理解 `engine/` 必须先看线程，因为它的并发纪律是"运行态只有一条线程碰"：

| 线程 / 任务 | 创建处 | 干什么 | 怎么停 |
|---|---|---|---|
| `simbench-engine`（守护线程） | `engine/runner.py:39-64` | 跑 `loop.run_forever()`，**一个 Session 的所有通道共用这一条** | `call_soon_threadsafe(loop.stop)` + join 5s（`runner.py:66-87`） |
| `simbench-engine-events` | `engine/runner.py:44-45,176-192` | 排空 `queue.Queue`，把事件投给各 sink | `_pump_stop` 事件；退出前把剩余事件排空 |
| 每通道 `serve_forever` 任务 | `backend/base.py:225-231` | 该路的收发 | `stop()` 里等 5s 超时（`:291-293`）；S7 没有此任务（同步 `start_to`，`s7_server.py:148-156`） |
| 每通道波形 tick 任务 | `backend/base.py:183-185`，循环 `:378-397` | 算波形值→钳位→编码→直接改寄存器 | `stop()` 里**最先**取消（`:280-282`）；`tick_ms<=0` 时根本不建 |
| `simbench-rows-pump` | `engine/rows_pump.py:107-113` | 100ms 一跳，只快照**当前显示的那一路** | `_stop` 事件 + join 2s（`:115-119`）；`session.closed` 时自行退出（`:131-134`） |
| snap7 DLL 线程 | 原生库自建 | 回调 `_on_event` | 不受我们控制（`s7_server.py:297-298`） |
| `asyncio.to_thread` 探针 | `modbus_tcp.py:251-259`、`modbus_rtu.py:164-168`、`s7_server.py:98-101` | 把端口/串口探针推出事件循环 | 随调用结束 |

**锁纪律**：`SourceStore`（人工接管地址表）与 `LogDedupe` **没有锁**，靠的是"只在引擎 loop 线程上碰"这条单线程约束（`backend/ownership.py:1-8`、`base.py:137-139`）；GUI 侧的写命令一律经 `runner.call` 投回 loop 线程（`engine/writes.py:174-191`）。`Session._lock`（RLock）守 `_channels/_points/_rows_meta/_render_cache/_sinks/_revision`（`session.py:57`）；`PacketStore._lock` 只守 deque，监听器在锁外调（`packets.py:55-64,86-108`）；`RowsPump._lock` 只守缓存的行列表（`rows_pump.py:42,96-99`）。

**这条约束有一处实证例外**：S7 的 `_on_event` 在 **snap7 DLL 自己的线程上**改 `SourceStore`（`s7_server.py:297-323`），而 `SourceStore` 本身无锁（`ownership.py:23`）；被 DLL 区域锁保护的只是区域缓冲（`s7_server.py:19-21,352-366`）。**已实测结案：竞态确实存在，且不是罕见事件——单次主站写约 1% 会被同一跳波形静默顶掉并冻住（10/1000 实测），限制项是 snap7 的事件送达延迟（中位 0.65ms）而不是 Python 侧那几十微秒；它是逻辑竞态（GIL 下集合不会被写坏），Modbus 那五条路结构上不可能有它。全部证据见第十节。**

### 通道生命周期与部分失败

通道运行态在 `ChannelRuntime{running, last_error}`（`engine/records.py:82-92`），对外发布的状态值是 `"running" | "stopped" | "error"`（`session.py:300,306,311,315,352-354`）。

`start_channel` 先把**整份工程**校验一遍，有任何 error 就拒绝启动（`session.py:271-275`）；建后端并在 `runner.call(..., 10s 超时)` 里启动（`:289-294`）；出异常就记 `last_error`、发布 `error` 态、再抛出，通道保持停止（`:295-301`）。

部分失败的形状：`start_all` 收集每一路的失败字符串而不是遇错就中止（`session.py:318-336`）；无头 CLI 里逐路 `except (BackendStartError, UnsupportedTransportError)` → 往 stderr 打一行带通道名的错 → `continue` 下一路（`__main__.py:281-296`），**一路都没起来**才抛（`:305-307`），有任何一路失败则退出码 2（`:458-460`）。这与 README:53-56 的说法逐条对得上。

### 报文事件流

`events.py` 定义四类纯数据事件：`PacketEvent`（含 `pdu_offset` 与由 `PacketStore.add` 分配的 `seq`）、`ChannelState`、`LogLine`、`PointChanged`。

一帧原始 Modbus 报文变成报文窗一行的路径：pymodbus `trace_packet` → `base._trace_packet`（`base.py:295-313`）→ 各传输自己的 `_parse_frame`（`modbus_tcp.py:202-215,271-273`；`modbus_rtu.py:87-97,217-220`；`modbus_ascii.py:63-73,82-83`）→ sink → `PacketEvent`（`session.py:408-418`）→ 队列 → pump 线程 → `PacketStore`（在这里分配 `seq`，`packets.py:88-89`）→ GUI 按 10Hz 增量取行（`packets.py:102-108`）。

并行的另一条：收包 → `_note_master_writes` → `_split_frames` 切帧 → **只取第一帧** → `write_targets` 解出 (unit, area, addr, count) → `_own_intersecting` 标接管（`base.py:315-371,475-493`）；一个批次里多出来的帧记一条"丢弃"日志（`base.py:350-357`）。

**S7 只有事件行、没有原始字节**，README:50-51 的说法成立：snap7 不交出原始字节（`s7_server.py:11-14`），`_report_master` 发的是 `direction="rx"`、`raw=b""`、`unit=None`、`fc=None`、摘要为"读/写 区 NB @off"（`s7_server.py:324-335`），且**没有任何 tx 事件**。

---

## 四、意图与实测的对账

`docs/superpowers/specs/2026-10-08-...-design.md` §4（第 105-204 行）写明了**意图**架构。把它与 AST 实测逐条对账，绝大部分吻合，以下几处不吻合：

| # | spec / 文档怎么说 | 实测定量 | 判定 |
|---|---|---|---|
| 1 | §4:129 依赖链是 `gui → engine → backend → project/codec`，`net` 是旁路 | 实测吻合，另有三条 spec 未画进这条链的边：`gui → master`（核对页自己的后台线程直调）、`gui → net`（网络页同步跑在 GUI 线程上）、`engine → net.endpoints`（延迟 import） | 前两条是**结构事实**，spec §4 的链没覆盖；第三条 spec §4:184-185 其实描述过，属意图之内 |
| 2 | §4:107-127 的架构块列了 gui/engine/backend/codec/project/net/根下 | **`master/` 完全不在 §4 的分层清单里**，尽管守卫 G1 为它单开了一条豁免（`tests/test_architecture.py:24-26`，注明"主站模式 2026-10-14 E1"） | spec §4 相对代码**已滞后**：master/ 是 2026-10-14 加的，架构块没跟着补 |
| 3 | §4:124 与 `net/__init__.py:3` 都说 net 是"唯一允许跑 PowerShell 与 **netsh** 的地方" | 全仓 grep `netsh`：**只出现在注释与文档串里，从未被调用**。真实的系统调用面是 PowerShell cmdlet（`Get-NetAdapter` / `Get-NetIPAddress` / `Get-PnpDevice` / `New-NetIPAddress` / `Remove-NetIPAddress` / `Rename-NetAdapter`）、`pnputil /remove-device`、以及 SetupAPI/newdev 的 ctypes 调用 | 文档**说过头了**：netsh 是历史口径，实现没走它 |
| 4 | `simbench/__init__.py:1` 包文档串："（M1：点表内核与 TCP 从站）" | 实际已有六种传输（Modbus TCP/UDP/RTU-over-TCP/串口 RTU/串口 ASCII/S7） | 包文档串**停在 M1**，未随里程碑更新 |
| 5 | `gui/bridge.py:6`："这个文件是 Qt 与非 Qt 的唯一接缝" | 该句原文限定在**引擎回调→Qt** 这一个方向（"Session/Runner 的回调只准调 `forward()`"），这个方向实测确实只有 bridge 一条路。GUI→引擎是 `EngineRunner.call` 直接同步调用，spec §4:144-147 另有明文描述 | **不矛盾**。此前一版取证把它当成"文档越界声明"，直接读原文后推翻：两处说的是两个方向 |
| 6 | spec 里三处标注日期为 `2026-10-17`（第 409、416、539 行） | 含这些行的提交 `cbd2322` 的时间是 `2026-10-16 17:59:42 +0800` | 文档日期**比承载它的提交晚一天**，所以文档里的日期不能当严格时序证据用 |

## 五、结构事实：声明了但接不上线的地方

以下是**实测到的现状事实**，不是整改建议 —— 本次没有改动任何一行代码，也没有删除任何东西。列出来是为了让模型完整：这些名字在图上存在，但在运行时没有产出方或没有调用方。

| 事实 | 证据 | 备注 |
|---|---|---|
| `events.PointChanged` 有消费方、**无产出方** | 定义 `events.py:52-59`；消费 `gui/bridge.py:15,22,30`；用例 `tests/test_bridge.py:58`、`tests/test_simdev.py:303-318`。全仓 grep 无任何 emit 点 | **已查清，见第九节**：M1 为 M4 预留的占位，M4 走了自己计划里明写的备选路（`LogLine`），所以它从来没被需要过 |
| `ChannelState` 的 `"starting"` 态**从不发布** | 声明 `events.py:42`；界面渲染 `gui/channel_state.py:16`；引擎实际只发 `running`/`stopped`/`error`（`session.py:300,306,311,315,352-354`） | 四态表里有一态没有来源 |
| `ChannelRuntime.connections` 字段从未被读写 | 声明 `engine/records.py:87`；`simbench/` 全树 grep 无第二处引用 | — |
| `BackendBase` 的 `on_connection` 钩子只有测试传值 | 贯穿所有构造器（`base.py:114,123`、`modbus_tcp.py:237-239`、`s7_server.py:80-83`、`__init__.py:36,49,54,59`），在 `base.py:375-376` 被触发；但 `session.py` 与 `__main__._serve` 都不传，唯一传真值的地方是 `tests/test_integration_tcp.py:446` | 生产路径上是空钩子 |
| `net apply` 子命令有分派、**无 subparser** | 分派 `net/cli.py:723-724`，实现 `_cmd_apply` 在 `:355-419`，提权调用在 `:387`；但 `:629-691` 的 subparser 注册里没有 `apply` | 从命令行不可达；`startup.py:52-67` 复用的是 `up` |
| `net/ledger.py` 有两条完全相同的 `return` | `:94` 与 `:95` 逐字相同 | 第二条不可达 |
| `SERIAL_MARK = "@"` 定义了两次 | `project/model.py:60` 与 `:64`，值相同、注释近似 | — |
| `_cmd_device_write` 两个分支都返回 0 | `net/cli.py:272-274`：`if result.get("already"): return 0` 紧跟 `return 0` | 条件判断无实际作用 |
| 两处模块文档串被 PowerShell 的反引号转义吃掉了一个字符 | `engine/reads.py:1-2`：`取数（rows）` 成了 `取数（` + 换行 + `ows）`；`engine/writes.py:3-4`：`` `runner.call` `` 成了 `经 ` + 换行 + `unner.call` | 成因见 `docs/superpowers/plans/2026-10-16-rename-events-deps.md:26-30`：PowerShell 的反引号是转义符，markdown 行内代码的 `` `r `` 被吃成 CR。同一批已修好 `engine/records.py`（实测其 L3 现在是完整的 ``读侧（`reads.py`）``），这两个文件漏了。现已无裸 CR/FF 字节（实测全仓 `FF=0 裸CR=0`，那个 CR 早被归一成真实换行），`git ls-files --eol` 全仓 82 个文件均为 `i/lf w/crlf` |
| 根下 `modslave/` 是改名前的骨架 | 32 个文件**全部是 `__pycache__/*.pyc`，零个 `.py`**；子目录 backend/codec/engine/gui/project 与现包树同形 | 本项目 2026-10 从 `modslave` 改名为 `simbench`。`README.md:32` 仍引用 `docs/.../2026-10-12-modslave-m5-rtu.md`——那个**文件本身就叫这个名**，引用是对的，只是 docs/ 下留了 8 处 `modslave` 字样 |

另外两处是**有意为之、且写了理由**的，不算断线：

- `SourceStore.cleared()`（`ownership.py:72-85`）注释里明说生产无调用方，故意留着；
- `backend/modbus_ascii.py:19` 的 `channel_transport` 带 `# noqa: F401` 与一句"本模块只做后端，判定在别处"，是为兼容旧 import 路径的再导出；
- `engine/ports.py`（30 行）整个模块存在的理由就是维持 `gui → engine → backend`，让 gui 不必碰 backend（`ports.py:2-4`），是守卫 G5 的配套；
- `backend/raw_writer.py:24-27` 刻意复制了 codec 里 `_check_registers` 的报错文案，理由是避免 backend→codec 的一条谓词依赖 —— 注意这与 `base.py:28`、`simdev.py:7` 已经在 import codec 的事实相比，**声明的边界比实际更窄**。

## 六、分层守卫：这套结构是被机器守着的

`tests/test_architecture.py`（541 行）把分层规则钉成自动化用例。**2026-10-16 实测：24 条全绿**（`.venv\Scripts\python.exe -m pytest tests/test_architecture.py -q`）。

| 守卫 | 规则 | 判据 |
|---|---|---|
| G1 | `pymodbus` / `serial` / `snap7` 只许出现在 `backend/` 与 `master/` | AST 扫 import 顶层包名 |
| G2 | `PySide6` / `PyQt5` 只许出现在 `gui/` | 同上 |
| G3 | `subprocess` 只许出现在 `net/` | 同上 |
| G4 | `engine/` 只许经 `backend/__init__.py` 门面取后端，不许 import `base`/`modbus_tcp` 等实现文件 | 解析 `backend.` 之后那一段，只有空串（门面）放行 |
| G5 | `gui/` 对 `backend` 的引用数必须是 **0**，连门面也不许 | 同上，但空串也算违规 |
| G6 | `net/` 的五个纯模块（`errors` / `spec` / `ledger` / `plan` / `endpoints`）运行时不许 import `ctypes` / `subprocess` / `state` | 排除 `if TYPE_CHECKING:` 分支里的 import |

这套守卫有两点值得单独指出，因为它们是**这套架构可信度的来源**：

1. **每条守卫都配一条"防空跑"用例**。只断言"违规者为 0"的话，把豁免口径写错（比如按目录深度判而不是按顶层包名判）会让扫描清单变成空表、断言变成 `[] == []` 恒真。所以每条都另配一个"在仓库外造一棵同形假树、植入违规、必须被抓出来"的用例（`:193-268`、`:323-335`、`:414-429`、`:517-541`）。`test_architecture.py:8-10` 记录了原先按 `len(file.parts)` 判层级时**确实**恒过的那次事故。
2. **G5 那条"0"是最近才成立的**。事件的纯数据类原先住在 `backend/events.py`，产出方是 backend、消费方是 engine 与 gui，于是"界面不许碰后端"必须为它开一个口子；2026-10-16 把它挪到根上的 `simbench/events.py` 之后口子才封掉（`events.py:1-11` 写了完整的搬迁理由，包括"为什么不能挪进 `engine/`"——那会变成 backend→engine，把依赖方向弄反）。

类型检查的覆盖范围（`pyproject.toml:50-79`）：mypy 覆盖 `project` / `codec` / `engine` / `backend` / `master` 五棵子树 + 根下七个模块。**仍在闸门外的是 `gui/`（Qt 的动态用法）与 `net/`（ctypes + Windows 结构体）** —— 也就是说，恰好是体量最大的一层（gui 4971 行）与碰系统状态最重的一层（net 2180 行）没有类型闸门。

**三条门禁在 HEAD `cbd2322` 上的实测结果**（本机 `.venv`，版本与 `requirements.lock.txt` 逐个一致；过程见第九节）：

| 门禁 | 命令 | 结果 |
|---|---|---|
| lint | `ruff 0.17.0` → `ruff check` | `All checks passed!`，rc=0 |
| type | `mypy 2.4.0` → `python -m mypy` | `Success: no issues found in 47 source files`，rc=0 |
| test | `pytest 9.1.1` → `python -m pytest`（串行） | `1573 passed in 260.83s`，0 failed / 0 skipped，rc=0 |

## 七、证据强度与未决项

**高置信度**（有代码/配置直接证据）：包级依赖图的全部边（AST 实测）、六条分层守卫（实跑 24 条全绿）、后端契约与分派键、线程清单与锁纪律、通道生命周期与部分失败形状、报文事件流路径、五个落盘存储与原子写、提权路径、net 层的真实系统调用面、第五节列出的全部断线事实（逐条 grep 复核过）。

**中等置信度**：两个人类 actor 的划分（依据 README 章节结构，代码无角色概念）；`spec §4` 滞后于 `master/` 这个判断（依据是 §4 清单里没有它 + 守卫为它开了豁免 + 守卫注释注明加入日期，三处信号一致但没有一条直接说"spec 忘了更新"）。

**未决 / 待验证**（这些是本轮**没有**回答的问题，需要另起一轮或直接实测）：

1. **`net apply` 是被 `up` 取代后忘了删，还是准备再暴露出来**。有分派（`net/cli.py:723-724`）、有实现（`:355-419`）、有提权调用（`:387`），但没注册 subparser（`:629-691`），从命令行不可达；`startup.py:52-67` 复用的是 `up`。要查历史或问作者。
2. **`gui/` 与 `net/` 在 mypy 闸门外**这件事，是"暂不划算"还是"有具体阻碍"。`pyproject.toml:60-61` 给的理由是"注解要补到能过检查是另一档工作量，等有人真正去碰那两层时再逐个拉进来"——这是意图陈述，不是实测结论。

原先列在这里的三项（`PointChanged` 的来历、全量套件是否全绿、**S7 跨线程改 `SourceStore` 是否构成竞态**）**已分别在第 9.1、9.2、第十节查清并结案**——第三项的答案是"确实构成竞态，但只是逻辑竞态且窗口很窄"。

## 八、怎么重新生成

**依赖图（`module-deps.dot`）**：遍历 `simbench/**/*.py`，用 `ast` 取每个文件的 import；**相对 import 必须按 `level` 解析回绝对模块名**（本项目几乎全用 `from ..x`，只扫 `level == 0` 会得到一张**空图**——这是本轮第一次生成时实际踩到的坑）；按"相对包根的第一层目录名"聚合成包级边；落在函数体内的 import 单独标成 lazy。豁免口径同样按顶层包名判，理由见 `tests/test_architecture.py:8-10` 与 `:57-64`。

**DSL**：模型是手写的，但每个元素的 description 末尾都带 `[证据: 路径:行]`。代码改动后需要人工核对这些行号；本轮没有建立自动校验（那属于 `architecture-health` 的范围）。

**这条已经应验过一次**：第十一节的修复让 `s7_server.py` 加了约 60 行，文档里 13 处 `s7_server.py:行` 引用中 9 处当场漂号（`_on_event` 240→297）。核对方法（不需要工具，一条 python 就够）：把文档里所有 `path:行` 引用抽出来，对每个被引用的符号在源码里重找一次定义行，再与引用比对；替换时用"旧串→新串→期望命中次数"的表，命中数对不上就跳过并报出来，避免把一个已经改对的引用二次替换。第十一节的 9 处就是按这个办法修的，改后 13 条引用逐条与真实行号对得上。

**注意**：`.dsl` 与 `.dot` 是规范来源，渲染出来的图是派生产物。要改就改源文件，不要手改导出图。

---

## 九、追查结案

第七节原先挂着两个"本轮没回答"的问题，2026-10-16 追查后结案；追查过程中又撞出第三件事（9.3）。

### 9.1 `PointChanged` 是"从来没接上"，不是"接过又拆了"

**查法**：`git log -S "PointChanged" --all` 找到 8 个提交；再对 `git rev-list --all` 的**每一个 revision** 跑 `git grep -n "PointChanged(" <rev> -- '*.py'` 并排除 `tests/`。

**结论**：全历史所有 revision 中，非测试文件里**从未**出现过一次 `PointChanged(` 的构造。它不是被拆掉的，是从来没接上。

时间线（都有出处）：

| 时间 | 提交 / 文件 | 发生了什么 |
|---|---|---|
| 2026-10-08 | `plans/2026-10-08-modslave-m1-kernel.md:125` | M1 计划把它列进 `backend/events.py` 的四个类之一 |
| 2026-10-08 | 同上 `:2951-2955`「后续计划的边界」 | **明写这是预留占位**："`Project.tick_ms`、`Point.waveform/wave_param`、`events.PointChanged` 已经在本计划里占好字段，M4 只需实现 `engine/` 层，不必回头改点表模型" |
| 2026-10-09 | `5c294d4` | 随 `modslave/backend/events.py` + `simdev.py` 落地（该提交只加了 3 个文件，全新增，无产出方） |
| 2026-10-09 | `plans/2026-10-08-modslave-m2-gui.md:211-231` | M2 计划在 `bridge.py` 里给它开了 `point_changed` signal 与 `isinstance` 分支——**消费方就是这时装上的** |
| 2026-10-11 | `plans/2026-10-11-modslave-m4-waveform.md:910` | M4 计划要求："恢复成功后立刻发一条 `PointChanged`（**或至少 `LogLine`**）……别静默把值改掉，操作员会以为是自己点错了" |
| — | `simbench/engine/writes.py:101-110` | **实现走了括号里那条备选路**：`restore_waveform` 成功时 `publish_log("info", ... handed back to the waveform; N register(s) released)`，"本来就在波形上"时另发一条，两条拒绝路径（`:76-86`）与异常路径（`:97-99`）也各有一条日志 |
| 2026-10-16 | `plans/2026-10-16-rename-events-deps.md:34-40` | 四个事件类整体从 `backend/events.py` 挪到根上 `simbench/events.py`，`PointChanged` 作为契约的一部分被一起搬走，计划里未单独提它有没有产出方 |

**判定**：M4 的计划**自己就给了 `LogLine` 这个退路**，实现选了退路并满足了那条设计意图（不静默改值、日志点名恢复了什么、跨度也报出来）。所以 `PointChanged` 是一个**意图已被别的方式满足、因而空转至今的预留占位**——不是漏接的线，也不是拆剩的尾巴。它的字段形状还被 `tests/test_simdev.py:303-318` 与 `tests/test_bridge.py:58` 守着，`bridge.py:30` 那个分支运行时永远走不到。

### 9.2 全量套件当前全绿

```
$ .venv\Scripts\python.exe -m pytest -p no:cacheprovider --tb=short -rf
1573 passed in 260.83s (0:04:20)          # 退出码 0
```

- **1573 passed，0 failed，0 skipped，0 xfail**（`-q` 下若有 skip 会显示成 `N passed, M skipped`，输出里没有）。
- 跑之前先确认了没有别的 python 进程在跑（`Get-CimInstance Win32_Process`），因为 README:259 明说这套件绑真实临时端口、**必须串行**、两份并行结果不可信。整轮只起了这一份。
- **没有复现挂死**：4 分 20 秒跑完，`simbench-suite-stall` 里记的那个"自动 GC 在非 GUI 线程销毁 QWidget 互锁"的形态没有出现。
- 与文档基线对得上并略有增长：`plans/2026-10-16-rename-events-deps.md:83` 记的是 1568 passed，`plans/2026-10-15-architecture-tidy.md:96` 记的是 1563 passed；现在是 1573，多的 5 条与 `cbd2322` 那批改动一致。
- 附带复核：`tests/test_architecture.py` 单独跑是 **24 passed**（`--collect-only` 也确认 24 条），与第六节的说法一致。

**这条改变了第七节的一处措辞**：原先写"要拿全量结论需单独一轮串行执行"，现在结论有了——**当前 HEAD `cbd2322` 上三条门禁里的 test 那一条是全绿的**。

### 9.3 另两条门禁：一开始跑不了，装上钉子版本后实测全绿

顺手去跑 `ruff` 与 `mypy` 时发现工具根本不在 venv 里（实测，非推断）：

```
$ .venv\Scripts\ruff.exe check
bash: .venv/Scripts/ruff.exe: No such file or directory
$ .venv\Scripts\python.exe -m mypy
No module named mypy
```

`.venv` 原先装了 `pytest 9.1.1`、`pymodbus 3.15.0`、`pyserial 3.5`、`PySide6 6.9.2`、`python-snap7 1.3`、`pys7 3.1.1`、`openpyxl 3.1.5`、`setuptools 80.10.2`（均与钉子一致）；**没装** `ruff`、`mypy`，也没装 `pyinstaller`——而 `requirements-dev.txt` 这三样都列了。全局（anaconda）有 `ruff 0.12.0` 与 `mypy 1.17.1`，但钉子是 **`ruff==0.17.0`** 与 **`mypy==2.4.0`**，且 `requirements-dev.txt` 那行注释明写"CI 与本地跑的是**同一对**版本"——版本对不上，所以**没有拿全局版充数**。

按钉子装进 `.venv` 之后重跑：

```
$ .venv\Scripts\ruff.exe --version ; .venv\Scripts\python.exe -m mypy --version
ruff 0.17.0
mypy 2.4.0 (compiled: yes)

$ .venv\Scripts\ruff.exe check          # CI lint 第一步
All checks passed!                       rc=0

$ .venv\Scripts\python.exe -m mypy      # CI lint 第二步
Success: no issues found in 47 source files      rc=0
```

**`47 source files` 与 `plans/2026-10-16-rename-events-deps.md:80` 记的数字逐字一致**——那份文档的门禁证据至此在本机复现成功，三条门禁全部有实测证据。

安装带来的 7 个包（`ruff 0.17.0`、`mypy 2.4.0`、`typing_extensions 4.16.0`、`mypy_extensions 1.1.0`、`pathspec 1.1.1`、`librt 0.16.0`、`ast_serialize 0.13.0`）与 `requirements.lock.txt` 里的钉子**逐个一致**，所以这次是把 venv 拉**回**锁文件的口径，没有越过钉子升级任何既有包。因为动了环境，全量套件重跑了一遍，结果见 9.4。

**唯一仍缺的开发依赖是 `pyinstaller`**（钉子 `pyinstaller==6.22.3` + `pyinstaller-hooks-contrib==2026.8`，两者都在 `requirements.lock.txt` 里，但 `pip list` 里没有）。后果是 `packaging/build.ps1` 在本机跑不了，也就是**三种 exe 形态本轮无法在本机复现**——第二节对容器的描述全部来自 `packaging/simbench.spec` 与 `entry.py` 的源码，加上 `packaging/SMOKE.md` 里记录的十条冒烟结果，不是本机实跑冻结产物得到的。

顺带把"全量套件为什么 0 skipped"这件事坐实：`pys7 3.1.1` **是装着的**（`from pyS7 import S7Client` 在 `tests/test_s7_backend.py:15` 与 `tests/test_s7_session.py:11` 是模块级硬 import，缺了会直接 collection error 而不是 skip），所以那三个 S7 用例文件（`test_s7_backend.py` 254 行 / `test_s7_session.py` 237 行 / `test_s7_model.py` 204 行）是**真跑**的，`python-snap7 1.3` 也在。套件里没有任何 `skip` / `importorskip` 逻辑（`tests/conftest.py` 与三个 S7 用例文件里 grep 均为 0 命中），0 skipped 是"没有一条被跳过"，不是"跳过的没显示"。

### 9.4 装了包之后重跑套件：两轮一致

因为 9.3 往 `.venv` 里装了 7 个包（其中 `typing_extensions` 落在既有依赖链上），环境被改过，所以全量套件重跑一遍对照：

| 轮次 | 环境 | 结果 | 用时 | rc |
|---|---|---|---|---|
| 第一轮（9.2） | 装 ruff/mypy **之前** | 1573 passed，0 failed / 0 skipped | 260.83s | 0 |
| 第二轮（本节） | 装 ruff/mypy **之后** | 1573 passed，0 failed / 0 skipped | 247.13s | 0 |

两轮**用例数与结论完全一致**，所以 9.3 那次安装没有影响运行时行为（`typing_extensions 4.16.0` 与锁文件一致，本来就该是这个版本）。两轮都串行、都先确认过没有别的 python 进程在跑、都没有复现 `simbench-suite-stall` 记的那个挂死形态。

### 9.5 一处我自己取证出错、已当场更正的记录

留在这里是因为它影响第五节那一行的写法，也影响后续读这份文档的人。

我先前断言过两件错事：

1. **"`reads.py`/`writes.py` 的文档串里有真 CR 字节，`cat -A` 看到了 `^M`"** —— 错。`git ls-files --eol` 显示全仓 `i/lf w/crlf`，本地检出的**每一行**都以 `^M` 结尾，我把正常行尾当成了损坏。按字节重扫（`0x0D` 不跟 `0x0A`、以及 `0x0C`）的结果是**全仓裸 CR 与 FF 均为 0**。损坏是真的，但形态是"多一个换行 + 少一个 r"，第五节那行已按此改写。
2. **"`pys7` 没装，所以 S7 用例可能没真跑"** —— 错，而且错得典型：我当时用 `pip list | grep -iE "ruff|mypy|pytest|pyinstaller|pymodbus|snap7|serial|PySide6|openpyxl"` 看了一眼就下结论，**而那个过滤模式里根本没有 `pys7`**。直接看 `pip list` 全量并 `python -c "import pyS7"` → 装着、能 import、版本与钉子一致（3.1.1）。

两条的可复用判据：**用过滤后的清单证明"某样东西不存在"是无效的**（过滤器本身就是嫌疑对象），要么看全量枚举、要么对目标单独做一次存在性测试；**解释 `cat -A` / `xxd` 输出前先确认这次检出的行尾**，否则 `^M` 分不清正常与损坏。

---

## 十、追查结案：S7 跨线程归属竞态**确实存在，实测发生率约 1%**（修复见第十一节）

第七节未决项 1 的结论。三段实测 + 一段对照，分"已证实 / 推断 / 未验证"三级标注。

### 10.1 已证实：回调确实在 snap7 的原生线程上，归属记账就发生在那里

起一个真 S7 后端（`tick_ms=50`）、用真 `pys7` 主站写一次，在 `_on_event` 外面套一层探针记录**每个事件的线程名与 EvtCode**：

```
EvtCode=0x0001   线程=simbench-engine      ← 服务端启动事件（我们自己的调用路径）
EvtCode=0x0008   线程=Dummy-1
EvtCode=0x80000  线程=Dummy-2
EvtCode=0x40000  线程=Dummy-3              ← 这就是写事件（s7_server.py:69 _EVENT_WRITE=0x40000）
写完后该点归属 = manual
```

`_EVENT_WRITE` 那一行落在 **`Dummy-3`**——ctypes 给原生线程起的名字，既不是引擎 loop 线程（`simbench-engine`）也不是主线程。而 `s7_server.py:318-322` 正是在这个回调里调 `_own_intersecting` → `base.py:484-493` → `ownership.py:45-47` 的 `self._manual.add(...)`。所以 `ownership.py:3-8` 那条"**只在引擎 loop 线程读写**"的纪律，在 S7 这一路上**被结构性地破掉了**，`s7_server.py:297-298` 的文档串说的是实话。

### 10.2 已证实：窗口存在，强制交错就能复现失效

`_tick_once` 里"查归属"与"写值"不是相邻两句：

```
base.py:411   if not self._sources.is_waveform(...): continue     ← 查
base.py:418   value = waveform_value(...)                          ← 算
base.py:426   value, clamped = self._clamp(value, pt)              ← 钳位
base.py:432   wrote = self._write_point(key, pt, value)            ← 写
```

把"主站的值落进缓冲 + 标接管"这两件事**注入到 `_clamp` 里**（即 :426，正落在查与写之间），一次就复现：

```
交还波形后缓冲值 = 20.0（波形值）
[注入] 主站值 99.0 已落盘；该点归属 = manual
注入之后缓冲值   = 20.0        ← 主站的 99.0 被这一跳的波形写覆盖了
该点归属         = manual      ← 于是后续每一跳都跳过它
```

**后果是永久且静默的**：点被标成 manual，后面的 tick 一律跳过（`base.py:411-413`），所以缓冲就**冻在波形值上**，主站那次写彻底丢失；界面上这一格显示"已接管"，值却是谁都没写过的波形值。这正是 `ownership.py:38-39` 与 `base.py:303-304` 反复提防的那个事故（"平台下发被波形顶回去"）。

窗口宽度实测（`s7_tick_window_probe.py` 包住 `is_waveform` 与 `_write_point`，量同一点上两者的时间差）。**每种配置各跑三次**——微秒级计时在 Windows 上单次不可信，第一次只跑一遍时我拿到的 15.8µs 与复跑的 8.9µs 差了快一倍：

| 配置 | 单次样本 | 均值 (µs) 三次 | 中位 | 最大 | 单次主站写撞上概率 ≈ 窗口/tick |
|---|---|---|---|---|---|
| `tick=100ms`，200 点 | 3800 | 8.9 / 9.4 / 10.1 | 6.4–6.7 | 217–390 | 8.9e-5 ~ 1.0e-4 |
| `tick=50ms`，200 点 | 7400–7600 | 9.7 / 8.3 / 9.8 | 6.4–6.7 | 189–317 | 1.7e-4 ~ 2.0e-4 |
| `tick=100ms`，1 点 | 19 | 85.4 / 93.9 / 78.7 | 67–85 | 132–232 | 7.9e-4 ~ 9.4e-4 |
| `tick=50ms`，1 点 | 39 | 89.3 / 93.9 / 84.8 | 71–87 | 139–194 | 1.7e-3 ~ 1.9e-3 |

两点要说清：

1. **窗口随点位数反常地缩小**：200 点时约 9µs，1 点时约 85µs，差近 10 倍，且两种配置各自三次都稳定复现，不是噪声。最合理的解释是每跳只跑一个点时，被测的那一对调用正好紧跟在 `asyncio.sleep` 之后、缓存与频率都是冷的；点多了循环体被跑热。**这个成因没有验证**，只是记账时不能把"点越少窗口越宽"当规律用——它是这台机的观测形状。
2. **概率口径**：易感区间**不是** Python 侧"查归属→写值"那一段（见下面 10.2b，实测决定它的是事件送达延迟）。

**⇒ 但这一组的绝对数字不能用来算概率**，它只证明了两件事：窗口确实存在、撞上之后的形状是什么。真正决定撞击频率的量在 10.2b。

（我最初把这组数字当成频率估算写 here，得出"约万分之一"——那个结论已被 10.6 的实测推翻，保留在此只为让读到旧版本的人知道错在哪。）

### 10.2b 已证实：易感区间由 snap7 的**事件送达延迟**决定，不是 Python 侧那 85µs

按 `client.write()` 返回之后轮询到"归属翻转"的间隔量事件延迟（15 次）：

```
15 次 (ms)：中位 0.651   p90 1.165   最大 1.346
对照：tick 周期 50ms，Python 侧查-写窗口约 85µs = 0.085ms
```

**事件中位延迟 0.651ms，是 Python 侧窗口的 7.6 倍。** 原生层写完缓冲之后，要过这么久 `own()` 才落地；这段时间里波形 tick 完全有机会把值写回去。所以易感区间 ≈ `事件延迟`（而不是 `查-写窗口`），单次主站写撞上的概率 ≈

```
0.651 ms / 50 ms ≈ 1.3 %
```

与 10.6 实测的 **10/1000 = 1.0%** 吻合（同一量级，模型略偏保守）。这条也解释了为什么概率**与点表规模基本无关**：点数多会让 Python 侧那一段变短（200 点时 9µs vs 1 点时 85µs），但 9µs 和 85µs 都远小于 0.65ms，不是限制项。**限制项是 tick 周期**：tick 越快越容易撞，`tick=10ms` 时按同一延迟约 6.5%。

### 10.3 已证实：Modbus 那五条路结构上不可能有这个竞态

同一段 `base.py` 代码，Modbus 与 S7 的差别只在**谁在什么线程上调 `own()`**：

- Modbus 的归属标记走 `_trace_packet`（`base.py:295-313`）→ `_note_master_writes`（`:315-357`）→ `_own_write_frame`（`:359-371`），而 pymodbus 的 server 跑在**同一条 asyncio loop 线程**上；
- `_tick_once`（`base.py:399-444`）**通篇没有 `await`**（已逐行核对），所以它作为一个 loop 回调一旦开始就会跑完，中间不可能插进 trace 回调。

两者被事件循环**天然串行化**，窗口宽度是 0。这也解释了为什么 `ownership.py` 敢不加锁——那条纪律对 Modbus 是成立的，S7 是唯一一个例外，而例外的来源是原生库自带线程，不是本项目的设计选择。

### 10.4 已证实：`SourceStore` 不会被写坏，所以这是逻辑竞态而不是数据竞态

- 解释器是 **CPython 3.13.9、GIL 启用**（`sys._is_gil_enabled()` → True，`Py_GIL_DISABLED` → 0，`SOABI` → `cp313-win_amd64`，不是自由线程构建）。
- `_manual` 是 `set[tuple[str,int,str,int,int|None]]`，键全是 C 层可哈希类型，没有 Python 侧 `__hash__` 回调，所以 `set.add`（`ownership.py:46`）与 `in`（`:51`）在 GIL 下各自**原子**。
- 唯一读改写整个集合的 `restore()`（`:65-70`）用的是 `&` 与 `-=`，都是 C 层集合运算；**没有任何地方在 Python 层迭代 `_manual`**，所以也不会出 `Set changed size during iteration`。

⇒ 不会崩、不会撕裂、不会脏读；症状只有"某一次主站写被静默顶掉并冻住"这一种。**若将来换到自由线程（no-GIL）构建，这一条立刻升级成真正的数据竞争**，`_manual` 需要加锁。

### 10.5 已证实：现有那条守卫用例抓不到它

`tests/test_s7_backend.py:123-144`（`test_the_tick_writes_into_the_area_and_respects_ownership`）的断言消息正是"曲线把主站写的顶回去了"，看起来就是守这件事的。但它**在设计上把窗口排除了**：

1. `open_s7(..., tick_ms=0)` → `base.py:183-185` 只在 `tick_ms > 0` 时建 tick 任务，所以**根本没有并发的 tick**；
2. tick 是测试自己**手工同步**调的（`backend._tick_once(now=2.0)`）；
3. 手工调之前先 `wait_for(lambda: not backend.source_is_waveform(0, "db1", 4), ...)`——**显式等归属标记落地**。

所以它验的是"标记落地之后 tick 会不会尊重它"（答案是不会写，正确），而不是"标记落地**之前**那一跳会不会顶掉主站的值"。这也解释了为什么 1573 全绿与这个竞态并存。

### 10.6 实测频率：**1.0%**（10/1000）——我先前那句"0/200、概率很低"是错的

不注入、真主站连续写、**单份前台**跑 1000 轮（`s7_ownership_race_probe.py`，配置为 1 点 / `tick_ms=50`）：

```
1000 轮：保住 990 次；真竞态 10 次；归属没标上 0 次；其它 0 次
第 165/216/274/324/440/505/533/860/909/982 轮：写 1xxx.0 → 读回 20.0｜归属=manual｜归类=race
```

⇒ **每次主站写约 1% 被波形顶掉**，与 10.2b 由事件延迟算出的 1.3% 同量级。全部 10 次都归类 `race`（归属已标 manual、缓冲停在波形值），**没有一次**是"归属压根没标上"——说明事件送达率本身没问题，坏的是**送达太慢，来不及拦住这一跳**。

两处口径事故必须记下来（都是做的过程中自己抓到、并当场作废了数据）：

1. **"200 轮 0 命中、与估算一致"那个说法整个方向错了。** 按 1% 算，0/200 的概率是 0.99²⁰⁰ ≈ **13%**，那只是一次偏低的抽样，我却把它当成"确认了低频"；而用来解释它的模型（窗口/周期）又漏掉了主导项。真实暴露比我上一轮报的高约**两个数量级**。
2. **第一次 1500 轮的运行数据作废**：这个环境的 `run_in_background` 会把同一条命令**起两个进程**（`Get-CimInstance` 查到两条一模一样的命令行，且都往同一个日志追加）。两份各跑一个 snap7 服务端 + 引擎线程、互相抢 CPU，而 CPU 抢占恰恰改变本实验要量的时间——正是本仓库 README:259"两份并行结果不可信"的同一类。发现后 kill + `TaskStop` 停掉调度任务，再**前台单份**重跑，才得到上面这组数字。
   → 凡是时间敏感的测量：**前台单份跑，并在起跑那一刻核对系统里只有一个进程**（上面那行 `起跑前 python 进程数=0` 就是这么来的）。

### 10.7 推断（由实测概率外推，本身不是观测）

- 平台**周期性**下发时几乎一定会撞上：期望命中 ≈ 写次数 × 1%。**1 次/秒写 8 小时（28800 次）期望约 288 次**；哪怕 1 次/分钟写 1 小时，也期望 0.6 次。上一版我按"万分之一"算出"一次性下发基本碰不到"，那个结论随 10.6 一起作废。
- **概率与点表规模基本无关，与 tick 周期强相关**（10.2b）：`tick=100ms` 时约减半到 0.5% 左右，`tick=10ms` 时约 6.5%。默认 `tick_ms` 取自工程文件，所以这一项是可调的。
- **主站若回读并重写，第二次一定成功**（点已是 manual，tick 从此跳过它），所以带回读的平台能自愈；只写不读的 fire-and-forget 下发会真丢，直到有人在界面上"恢复波形"再重下。**这条没有实测**。

### 10.8 未验证

- snap7 原生侧是否在某些路径上用自己的锁把事件回调与区域写串行化。10.2b 量到的是"写返回 → 归属落地"这段端到端延迟（中位 0.65ms），但没拆开里面各段的占比。
- **2000/4000 点这一档真实现场形状下的频率**。10.6 是 1 点；10.2b 的论证说明限制项是事件延迟与 tick 周期、不是点数，所以外推应当接近 1%，但这是推断，没有实测。
- 多协议混装（同表既有 S7 又有 Modbus 通道）时，一条通道的事件延迟会不会被另一条通道的负载推高。

### 10.9 取证脚本（已入库）

两份探针按本仓库 `docs/superpowers/notes/` 的既有惯例入库（那里已有 `rtu_socket_probe.py`、`s7_probe_server.py`、`rtu_open_failure_probe.py`）：

| 文件 | 产出哪一节 | 跑法 |
|---|---|---|
| `docs/superpowers/notes/s7_ownership_race_probe.py` | §10.1 线程身份、§10.2 的交错复现、§10.6 的自然频率 | `.venv\Scripts\python.exe -u docs/superpowers/notes/s7_ownership_race_probe.py [轮数]` |
| `docs/superpowers/notes/s7_tick_window_probe.py` | §10.2 的窗口宽度表 | `.venv\Scripts\python.exe -u docs/superpowers/notes/s7_tick_window_probe.py [tick_ms] [点位数]` |

两份都从新位置**实跑复现过**（不是搬运即完）。入库时踩到并修掉一处：窗口探针原来用 `µs`，而 `µ`(U+00B5) **不在 GBK 里**，在这台机的控制台会 `UnicodeEncodeError` 打断输出——第一次挪进仓库后不带 `PYTHONIOENCODING` 实跑就当场崩了，现已统一写成 `us`，并在 docstring 里留了"别写回 `µs`"的提醒。`≈ → ｜ · ×` 都在 GBK 内，安全。

它们**不影响三条门禁**：`ruff` 的 `extend-exclude` 含 `docs`（`pyproject.toml:12`），`pytest` 的 `testpaths = tests`（`pytest.ini`），所以既不被 lint 也不被收集。

没有按 notes/ 的另一半惯例（探针 .py + 同目录日期 .md）再开一份 .md——结论已经成文在本文件第十节，重复一份只会两处不同步。

---

## 十一、本会话内的修复

§11.1–§11.5 是那一次修复的记录，已作为 `69864e1` 提交并推送；§11.6 是随后把它留下的剩余缝隙
也关掉的那一步。

第 10.6 的频率实测（1%）出来后，作者同意按推荐方案实施。这一节记录改了什么、代价、以及还剩多少。

### 11.1 改法：把判据从"会迟到的事件"换成"写入时刻就能同步看到的缓冲内容"

只动 `simbench/backend/s7_server.py`，两处：

1. `S7Backend._write_point`（`:183` 起）—— 写之前先看缓冲**还是不是上一跳自己写的那串字节**。
   不是，就说明主站已经改过而事件还没到：于是一个字都不写、当场按与事件路径**同一个键与同一个
   跨度**把该点标成接管、发一条去重警告、并忘掉这份记忆。新增状态只有
   `self._tick_written: dict[(area, address, bit), bytes]`（`__init__`，`:90` 附近）。
2. `S7Backend.restore_waveform`（新增覆盖）—— 交还波形时必须把那段地址的记录一起忘掉。

bool 点天然按位隔离：`read_point_bytes` 对 bool 只回那一位（`b"\x00"`/`b"\x01"`），而
`encode(v, "bool", …)` 回同一种形状，所以同字节的邻居位不会互相误判。

**为什么恢复必须清记录**（这是实现过程中撞出来的，不是我事先想到的）：接管期间每一跳都在
`is_waveform` 那一步被挡住，`_write_point` 压根没跑，记录就停在接管**之前**的波形值上；恢复后
第一跳一看"缓冲是主站的值、记录是我的旧值"，就判定又被接管了，于是把刚恢复的点重新标成接管
——操作员按了"恢复波形"，曲线永远不接手。**第一版就是这样弄坏了既有用例的**（见 11.2）。

### 11.2 先红后绿（TDD）

在 `tests/test_s7_backend.py` 加两条用例，**先看它们失败**：

```
test_a_master_write_whose_event_has_not_arrived_survives_the_next_tick
  AssertionError: assert b'\x00*' == b'\x03\t'      ← 缓冲里 0x002A=42（波形顶掉了）
test_the_takeover_detected_from_the_buffer_says_so_instead_of_going_silent
  AssertionError: 接管了却一个字没说：[]
```

造中间态的办法是只动缓冲、不动归属（`write_point_bytes` 不碰归属表），这正是事件到达前的世界。
实现后两条转绿；`test_the_tick_writes_into_the_area_and_respects_ownership` 中途被我弄坏过一次
（`AssertionError: 交还之后曲线没接手`，`777 != 42`），它现在就是 11.1 第 2 条的钉子。

顺带得到一个有用的对照：**改之前那条既有用例一直是绿的**，坐实了 10.5 的判断——它结构性地
看不见这个形状。

### 11.3 修复前后的实测对照

| 指标 | 改前 | 改后 |
|---|---|---|
| 自然频率（1000 轮，单份前台） | **10/1000 = 1.0%** | **0/1000**（95% 置信上界 ≈ 0.3%，rule of three） |
| 强制交错那一条 | 缓冲停在 20.0（主站值被顶掉并冻住） | 缓冲 99.0，**主站值保住** |
| Python 侧"查→写"窗口（200 点/100ms） | 8.9 / 9.4 / 10.1 µs | **19.5 / 19.5 / 17.4 µs**（约 2 倍） |

窗口翻倍是**这次改动付出的代价**——每个点每跳多一次 `read_point_bytes`（一回区域锁 + 一次切片）。
它不影响保护是否成立：保护来自换证据源（0.65ms 的事件延迟项被整项消掉），不是来自缩窗口。
若"改后 0/1000"里改动其实无效，观测到 0 次的概率是 0.99¹⁰⁰⁰ ≈ **0.43%**。

### 11.4 剩余缝隙（§11.1 第一版的形状，已由 §11.6 关闭）

第一版把"读回比对"与"写"留在**两次独立的区域锁**里，中间那段（亚微秒级）主站的写仍会被随后的写
覆盖，所以当时只能说"0/1000 给的是 ≤0.3% 的 95% 上界，**不等于 0**"。这条已被 §11.6 关掉；保留原文
是为了让"改之前到底剩什么"有据可查。

### 11.5 门禁与状态

```
ruff check                    All checks passed!                              rc=0
python -m mypy                Success: no issues found in 47 source files     rc=0
pytest tests/test_architecture.py   24 passed                                 rc=0
pytest（全量，串行）           1577 passed in 252.43s                          rc=0
```

1577 = 原 1573 + 4 条新用例（§11.1 的两条行为 + §11.6 的两条锁形状）。

**跨轮次的绝对秒数不能当结论用——这条被自己的数据推翻过一次。** §11.1 落地后连着两次全量是
450s / 468s，我当时据此写了"套件耗时涨到 450–468s"；而 §11.6 关掉缝隙、用例还**多了两条**的那一次
是 **252s**。所以 247–468s 这个区间里**机器状态是主导项**，那个"涨了一倍"的说法站不住。

仍然成立、且可复现的只有**顺序效应**：同样两个文件，`test_gui_theme.py` 放前面是 18.44s（那两条慢
用例 0.73s / 0.57s），放到 `test_gui_main_window.py` 之后是 46.55s（同样两条 5.36s / 5.25s）——
**差 9 倍，唯一变量是前面累积了多少**。当时还做过一组排除法：去掉全部 144 条 S7 用例仍要 441s，
`test_gui_theme.py` 对 `s7`/`backend`/`tick` 的引用是 0 行，所以那次 450s 与 S7 改动无关。

机制在 `tests/conftest.py`：`:42-44` 导入期 `gc.collect(); gc.freeze(); gc.disable()`，而 autouse 的
`_collect_on_main_thread`（`:47-51`）每条用例结束时做一次全量 `gc.collect()`。自动回收被整会话关掉
之后这次 collect 成了唯一回收点，而存活对象图单调增长（`engine_runner` 自己的注释写着"被停掉的
backend 会把它的 listener 协程留在共享 loop 上"），于是 collect 与图上的 Qt 操作都越来越贵，排在
后面的文件付得最多。**这是一条真实的结构性成本，但它解释不了跨轮次那个幅度。**

**为什么不动 gc 策略**：`gc.disable()` 是承重的——`conftest.py:41` 记着实测"`packet_view +
integration_tcp` 这一对在回收开着时 3/3 挂死，`gc.disable()` 后 3/3 跑完"，那是跨线程销毁 `QWidget`
死锁的规避手段。降频会让对象活得更久、并把回收攒成一次大爆发，正好把那条挂死风险买回来；而它换来的
收益是一条**被机器状态主导**的秒数曲线，收益没有被证明存在。真要动必须拿那对挂死组合做回归，属单独一轮。

提交状态：§11.1–§11.5 那一轮修复连同模型与探针已作为 `69864e1` / `f5e3b73` 推到 `origin/main`；
**§11.6（关闭剩余缝隙）是之后的改动，尚未提交。**

### 11.6 关闭剩余缝隙：读回比对与写收进同一次区域锁

动手前先测两件前提（探针在仓库外临时目录跑的，本机 snap7 1.3）：

1. **`lock_area` 不可重入** —— 同一线程第二次取同一个区的锁**直接阻塞**（>2s 未返回）。所以已经
   持锁时绝不能再去调 `read_point_bytes` / `write_point_bytes` 那两个各自加锁的公开方法：那是
   自死锁，而症状恰好是本仓库 2026-10-11 那次"引擎不回话 + 连片超时"的形状。
2. **主站的服务线程确实取这把锁** —— 我们持有 DB1 区域锁 3s 期间，pys7 主站的 `write`
   **一个字节都落不进去**（缓冲仍是基线的 `3f800000`），最后以 `S7TimeoutError` 超时返回。
   所以"收进一次锁"买到的是**真原子性**，不是自我安慰。

据此把两个公开方法拆成"加锁外壳 + 裸操作体（`_read_naked` / `_write_naked`）"，`_write_point` 用
**一次** `_area_lock` 包住读回与写。`own()`、日志、编码全部留在锁外——持锁期间主站是阻塞的，把慢
东西放进锁里等于放大它的超时。

判据为什么用"取锁次数"而不是多线程抢时序：见
`test_the_takeover_check_and_the_write_share_one_lock`，它先红在 `assert 2 == 1`（读回与写各锁一次
= 中间有缝）。抢时序的写法要靠 sleep 赌运气，那种用例本身不诚实（同文件 `wait_for` 的注释也持这个
口径）。另一条 `test_the_public_area_read_and_write_still_take_the_lock` 是**给重构上的保险**：
它改前改后都通过，用途是防止拆分时把锁悄悄丢掉——那种破损是静默的，多字节 real/uint64 会让主站读到
半个值。我没有把它冒充成红用例。

验证：S7 六个文件 **123 passed**（原 121 + 新 2）；全量 **1577 passed，EXIT=0**；`ruff` 与 `mypy`
47 files 通过；自然频率再跑 1000 轮仍 **0 命中**，强制交错那条仍是"主站值保住"，事件中位延迟
0.601ms（与 §10.2b 的 0.651ms 同量级，仍然远大于 Python 侧窗口——但现在已经与正确性无关，因为
拦截不再依赖事件什么时候到）。
