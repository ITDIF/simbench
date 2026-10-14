# 深度审查那一批：13 条优化 + 2 项扩展（2026-10-14）

日期：2026-10-14　｜　来源：用户要求"深度分析项目，给出 10 条优化 + 5 条扩展"，随后口径是
**"全部优化，扩展 1、7 实现"**。本文件是这一批的**总计划**：每条先写"现象 + 证据 + 口径"，
落地后补"实测数字"与"执行期已裁决的偏离"。

> 这一批东西多、彼此不依赖，所以**分批提交**：每一批自己跑全量串行 `pytest`，绿了才进下一批；
> 每批一个 commit，推送一次。批次顺序按依赖排：先做能被别人复用的底层件
> （原子写、诊断落盘、传输注册表），再做依赖它们的东西（主站模式、界面页）。

## 0. 审查结论的判据（为什么这些算"该优化"）

不是风格偏好，三条硬判据：

1. **会丢东西**（数据、证据、端口）——优先级最高；
2. **随规模退化**（点数、报文速率、通道数涨了就卡或坏）；
3. **改动成本随文件数增长**（同一件事在 N 处各写一遍，加一种协议/格式要改十几处）。

## 1. 优化清单（13 条）

| # | 现象（证据） | 口径 | 批次 |
|---|---|---|---|
| O1 | `csv_io.write_csv_file` / `json_io.save` 都是"截断 + 直接写"（`Path(path).write_text`），写一半断电 = 现场唯一那份点表废掉；而 `net/ledger.py:102-104` 已经有 `tmp + os.replace` 的正确写法 | 抽 `project/fsio.py:write_text_atomic(path, text)`：同目录临时文件 → `os.replace`；覆盖已有文件时先留一份 `<名>.bak`（best-effort，备份失败不拦保存）；被别的程序占着时把 `WinError 5` 翻成一句人话 | B1 |
| O2 | 界面版 exe 无控制台 ⇒ `sys.stdout/stderr is None`（`packaging/entry.py` 自己写着）；全仓没有 `excepthook`/`faulthandler`/`qInstallMessageHandler`；日志只有窗口里那份 `setMaximumBlockCount` | 新增 `simbench/diagnostics.py`：`log_dir()`（`%LOCALAPPDATA%\\simbench\\logs`，可用 `SIMBENCH_LOG_DIR` 覆盖）+ `install_crash_handlers()`（excepthook + faulthandler 落盘）+ `LogFile`（追加写、行缓冲）；GUI 启动时装上，日志页加"另存日志" | B1 |
| O3 | 4000 点的点表，`Session.rows()` 一次 36.6ms（其中引擎整批回读 11.6ms），GUI 每 100ms 调一次、跑在主线程（实测见 §3） | ① `_render` 结果按 `(点位身份, 寄存器元组)` 缓存，值没变不重解码；② GUI 那条路改成**后台线程取快照 + 主线程读缓存**（`engine/rows_pump.py`），界面永不等引擎；③ 加性能回归用例（解码次数为 0 + 宽松时间上限） | B2 |
| O4 | 报文窗在总线忙时每一跳把整表标脏：缓冲满了以后 65.6ms/跳（5000 行逐个 `dataChanged`），没满则每跳 `beginResetModel`（选中行与键盘位置被清） | `PacketEvent` 加单调 `seq`（由 `PacketStore` 发号），模型按 seq 算差集：纯追加走 `rowsInserted`、淘汰走 `rowsRemoved`、只有清空/换过滤器才 reset。选中态不再每 100ms 被清 | B2 |
| O5 | 只有 `pytest.ini`：无 `pyproject.toml`、无 ruff/mypy、无 CI。24k 行 Python 全靠人眼 | 加 `pyproject.toml`（ruff：E4/E7/E9/F/I/B/C4 + 每文件豁免；mypy：先只覆盖 `project/`、`codec/`、`engine/`，`ignore_missing_imports`）+ `.github/workflows/ci.yml`（windows + offscreen 跑全量；tag 时打包冒烟）。ruff 到 `requirements-dev.txt` | B3 |
| O6 | 面向操作员的消息行实测 **91 条中文 / 93 条英文**（`port guessed as 5020 from its name`、`engine write failed: ...`） | 定口径：**界面/日志/对话框看到的文案一律中文**（`LogLine`、`Issue`、状态栏、对话框）；**命令行的 stdout/stderr 保持英文**（脚本与 README 依赖它，冻结冒烟也断言英文），纯开发断言可留英文。加用例把这条口径钉住 | B4 |
| O7 | 全仓无 `QSettings`/`saveGeometry`：主题、窗口几何、上次打开的工程、上次导出目录每次重启都回默认 | `%APPDATA%\\simbench\\settings.json`（可用 `SIMBENCH_SETTINGS_DIR` 覆盖）：主题、几何、最近 5 个工程；文件菜单加"最近打开"子菜单（动作对象与快捷键的用例要跟着改） | B4 |
| O8 | 版本号三处，谁也不是权威：`packaging/version_info.txt` 写死 1.0.0.0、`simbench/__init__.py` 没有 `__version__`、没有 CHANGELOG | `simbench/__about__.py` 单一来源；`--version`；窗口标题带版本；用例比较 `version_info.txt` 与 `__version__`；加 CHANGELOG.md | B3 |
| O9 | `requirements.txt` 只钉了一半（`openpyxl>=3.1`、`pyserial>=3.5`、dev 那几个都是范围），没有 lock | 运行时与 dev 的依赖全部钉到现场/CI 实测版本；生成 `requirements.lock.txt`（`pip freeze`，给"一模一样复现"用）+ `packaging/lock.ps1` 重新生成；用例比较"两边的钉法一致" | B3 |
| O10 | `net/cli.py` 681 行一个文件：argparse 定义、提权编排、4 个子命令、结果 JSON 读写全挤在一起 | 拆 `net/cli_args.py`（解析与帮助）/ `net/cli_ops.py`（子命令实现）/ `net/elevate.py`（UAC 编排，唯一能脱离真 UAC 单测的一层）；`net.cli.main` 保持是入口 | B4 |
| O11 | `point_table._display()` 每次 `data()` 都造整行 10 键 dict | `data()` 按列直算（`_display` 保留为兼容入口，内部改成同样的按列实现） | B2 |
| O12 | 29 处宽异常里有的吞得没痕迹（`PacketStore._notify` 直接 `pass`） | 加 `simbench/swallow.py:swallow(exc, where, sink=None)`（至少计数 + 一条 warning），把裸 `pass` 换成它；加用例"吞掉必须留痕" | B3 |
| O13 | Excel 有多张工作表时静默只读第一张（`xlsx_io.sheet_rows` 用 `worksheets[0]`） | 多于一张时给一条 note（列出被忽略的 sheet 名），与"猜了要说"同一条口径 | B1 |

## 2. 扩展清单（本轮做 1 与 7）

### E1　主站模式：扫描 + 逐点核对（B6/B7）
工具现在**完全没有**主站能力（grep 无任何 client 用法）。现场八成的返工来自"点表跟设备对不上"。
- `simbench/master/`：`modbus.py`（pymodbus 客户端：tcp/udp/rtutcp/串口 RTU/ASCII）、`s7.py`
  （pyS7）、`scan.py`（扫 unit id + 逐点核对，出 `VerifyReport`）、`report.py`（文本 + 走
  `project/formats.py` 导出 CSV/Excel）。
- 命令行：`simbench scan <点表> [--host H] [--port P] [--units 1-247] [--result x.json]`、
  `simbench verify <点表>`（逐点读回来与点表初值/期望比对）。
- 界面：新增"核对"页（**不用 QComboBox**、定宽槽、几何恒定）：目标来自当前工程的通道参数，
  两个按钮（扫描 / 核对）、进度、结果表、导出报告。
- 验收：拿**我们自己的从站**当真设备（`ModbusTcpBackend` + 真主站 + 真端口）——扫描能列出
  unit、核对能报出"对得上/对不上/读不到"三类，值故意改坏一处必须报 mismatch。

### E7　传输扩展：UDP / RTU-over-TCP / 串口 ASCII / S7 STRING（B5）
"加一种协议要改哪 16 处"那份清单（`2026-10-14-import-export-formats.md` §4）这一轮**真的用上了**：
先把它收成一张**传输注册表**（`project/transports.py`），再照表加四种。
| 传输 | 通道名语法 | 载体 | framer |
|---|---|---|---|
| `tcp` | `name@tcp:host:port` | TCP | MBAP（现状） |
| `udp` | `name@udp:host:port` | UDP | MBAP（socket） |
| `rtutcp` | `name@rtutcp:host:port` | TCP | RTU over TCP |
| `rtu` | `name@COM3 19200 8E1` | 串口 | RTU（现状） |
| `ascii` | `name@COM3 19200 8E1 ascii` | 串口 | ASCII |
| `s7` | `name@host:port 0/1` | ISO-on-TCP | S7comm（现状） |

S7 侧另加一种点位类型 `s7string`（真 S7 STRING：前两字节 最大/当前 长度），与现有的
`string`（裸 ASCII，无长度头）并存，点表里写哪个就是哪个。

## 3. 实测数字（本机，2026-10-14）

* `Session.rows()` 一次（合成点表，单通道，值不变）：200 点 1.81ms / 1000 点 8.94ms /
  2000 点 17.98ms / 4000 点 36.57ms；其中**引擎侧整批回读** 0.64 / 2.83 / 5.65 / 11.64ms
  ——3/4 的时间花在"解码 + 造 Row + 造文案"，而不是回读本身。
* 报文窗（offscreen + 真 `QTableView`，每跳新增 10 帧）：缓冲未满 14.81ms/跳；
  缓冲写满（5000）**65.60ms/跳**（逐行 `dataChanged`）。
* 优化之后（同一台机器、同一份脚本，2026-10-14 复测）：
  **4000 点一跳 36.57 → 16.34ms**（其中引擎整批回读 12.98ms；GUI 侧从 25ms 降到 3.4ms）；
  报文窗写满之后 **65.60 → 14.41ms/跳**（与"未写满"的 14.68ms 持平，那个"写满就掉悬崖"
  的形状没有了）。

## 3b. 分批执行记录

### B1　数据安全与证据（O1 / O2 / O13）· 已完成

* `project/fsio.py`：`write_atomic` / `write_text_atomic` / `write_bytes_atomic`（临时文件 +
  `os.replace` + best-effort `.bak`，失败时目标文件保持旧内容并清掉临时文件）。CSV / Excel /
  JSON / 网络声明四条落盘路径全部改走它（`net/ledger.py` 本来就是原子写，没动）。
* `diagnostics.py`：`log_dir()` / `session_log_path()` / `LogFile` / `install_crash_handlers()`；
  GUI 入口装钩子，`_log` 双写（窗 + 文件），日志页加"另存日志"，关窗写收尾行并收句柄。
* `xlsx_io`：多于一张工作表时给一条 note（列出被忽略的 sheet 名）。
* 新增用例：`tests/test_fsio.py`（11 条）、`tests/test_diagnostics.py`（10 条）、
  `tests/test_gui_log.py`（5 条）、`tests/test_formats.py` 补 2 条多工作表。
* 顺带：`simbench/__about__.py`（版本单一来源的第一块砖，O8 在 B3 收尾）。

### B2　界面刷新（O3 / O4 / O11）· 已完成

* `Session._row_cached`：值没变就把上一跳那个 `Row` **原样还回去**（键里带一行的全部输入：
  容器名/身份/名字/类型/字序/初值/只读/波形/传输）。`_rebuild_index_locked` 换工程或改结构时
  清空；`Session.revision` 给出"结构变过没有"。
  **踩过的坑**：第一版键只带身份与类型，于是"改名字"这类 live 改动读到旧 `Row`
  （被既有用例 `test_a_live_only_apply_from_the_menu_says_next_tick_not_restart` 逮住），
  现在有 `test_performance.test_renaming_a_point_rebuilds_that_row` 单独钉住。
* `engine/rows_pump.py`：后台线程按 100ms 取快照，界面 `rows()` 只读缓存；换通道/工程结构
  变了/写成功这三条**同步取一次**（不许先空一拍）。失败保留上一份 + 报一次；
  `Session.closed` 之后自己退场。窗口多了 `refresh_rows_now()`（改完属性、用例要"此刻的真值"）。
* `packet_view.PacketModel.sync`：按 `PacketEvent.seq`（由 `PacketStore` 发号）做差集，
  只发 `rowsInserted` / `rowsRemoved`；换过滤器或缓冲被清才 reset。选中行跟着序号走。
* `point_table._display` 改成**按列直算**（原来每问一格都构造整行 10 键 dict）。
* 新增用例：`tests/test_rows_pump.py` 9 条、`tests/test_performance.py` 4 条、
  `test_gui_packet_view.py` 补 5 条（差集通知 / 选中跟序号 / 换过滤器仍 reset）。

### B3　工程化（O5 / O8 / O9 / O12）· 已完成

* `pyproject.toml`（新）：ruff 只开"有便宜收益"的规则（E4/E7/E9、F、I、B、BLE、C4、SIM、UP、
  RUF、PLW、RET、ISC），行宽/全角标点/三元这些"风格偏好"一律关（中文注释密集的仓库里那些
  只会制造噪音）；mypy 只覆盖 `project/`、`codec/`、`engine/` 三棵子树，`follow_imports=silent`
  把 backend/net 留在闸门外（理由写在配置里）。
* 125 条 ruff 发现全部处理完（`ruff check` 现在是 **All checks passed**）：193 条是机械修复
  （import 顺序、没用的 import、`noqa` 用错），剩下的是真东西——**`ModbusTcpServer` 这个
  未定义的名字**（`modbus_base` 的注解）、`net/cli` 里两个解包错位的变量、`validate._points`
  少一个显式 return、`session.manual_count` 的返回类型漏了 `| None`、`row cache` 的键、
  以及 S7 专用的 `read_point_bytes` 在基类上补成显式钩子（原来只有 S7 后端有，静态检查看得见）。
* mypy 从 42 条降到 **0 条**（三棵子树）：默认值表标 `dict[str, Any]`、`_points` 的内层键改
  `(区, 地址, 位)`、`runner` 的 `thread.ident` 可能为 `None`、`csv_io` 里 `wanted` 标成三种
  通道的联合类型。
* `simbench/swallow.py`（新）+ 两处投递/通知路径改用它记账（`runner.swallowed` /
  `session.swallowed`），窗口心跳把第一笔报一次（`_report_swallowed`）。
* 版本：`simbench/__about__.py` 是唯一来源，`--version` 报它，`packaging/version_info.txt`
  由 `tests/test_version.py` 钉着与它一致，新增 `CHANGELOG.md`（含 1.0.0 这一版的内容）。
* 依赖：运行时与开发依赖**全部钉到实测版本**（含 `ruff==0.17.0` / `mypy==2.4.0`），新增
  `requirements.lock.txt`（`pip freeze` 原文，连传递依赖）+ `packaging/lock.ps1` 重新生成，
  `tests/test_requirements.py` 补两条（`==` 到底 / 锁文件与它不打架）。
* `.github/workflows/ci.yml`（新）：lint（ruff + mypy）→ test（windows + offscreen 全量）→
  打包（只在打 tag 时）。冻结冒烟留在人工清单里（要桌面会话）。
* 新增用例：`tests/test_version.py` 4 条、`tests/test_swallow.py` 4 条、`test_gui_log.py` 补 1 条。

### B6/B7　主站模式（E1）· 已完成

- `simbench/master/`（新）：`modbus.py`（五种 Modbus 传输的连接与读写）、`scan.py`
  （扫 unit + 逐点核对，四类结论：`ok` / `mismatch` / `no_unit` / `error`）、
  `report.py`（文本 + CSV/Excel 导出，**不**走点表那张格式表——报告不该被当点表导回去）。
- 命令行：`--scan [--units 1-20]` / `--verify [--result 报告.xlsx]`。两条都**只读**，
  有对不上的回退出码 2（与 `net check` 同一条口径）。挑通道的规则：点名优先，否则
  **第一条 Modbus 通道**（混装工程里第一条可能是 S7，静默挑错等于"扫出来什么都没有"）。
- 界面：**"核对"页**（`gui/verify_panel.py`）：三个定宽按钮 + 定宽状态槽 + 结果表；
  长活跑在**后台线程**（扫 247 个 unit 是几分钟的事，跑在界面线程上就是窗口卡死），
  跑着的时候按钮**置灰不隐藏**（几何不变），结果经 Qt 信号投回主线程。
- 分层守卫（`tests/test_architecture.py`）放行的是 `master` 这个包（它 import 的是
  pymodbus 的**客户端**），不是"谁都能 import pymodbus"。
- 验收：`tests/test_master.py` 18 条（含"真从站 + 真主站扫 unit / 逐点核对 / 点表写错一位
  必须报对不上"三条端到端与一条命令行用例）、`tests/test_gui_verify.py` 7 条
  （含真从站上的界面核对与导出）。全量串行 **1510 → 1535 passed**。

## 4. 验收口径

* 每一批：全量串行 `.venv\\Scripts\\python.exe -m pytest`（`QT_QPA_PLATFORM=offscreen`、
  `SIMBENCH_NET_AUTO=0`）全绿，只增不减；
* 涉及打包/依赖的批次（B3、B5、B7）：重新打包 + `packaging/smoke.py` 不回归（**12 条**）；
* 界面有变化的批次：`docs/superpowers/notes/2026-10-13-gui-shots/regenerate.py` 重渲染；
* 收工：spec §11 补一条、README 回写、`docs/superpowers/README.md` 索引补一行。

## 4b. 执行期已裁决的偏离

| # | 计划里写的 | 实际做的 | 为什么 |
|---|---|---|---|
| D1 | O6："面向操作员的文案统一成中文" | 改成**界面层补一句中文说明**（`gui/messages.py`：14 条已知失败形状 → 中文说明；**原文一个字不动**，认不出来就原样显示） | 引擎/校验那两层同时也是命令行、README 与冻结冒烟断言的**对外合同**（`point table is valid` 这类），改文案就是改合同；而英文原话现场取证时要贴给上游。代价：新增一种失败形状时中文说明要跟着补——`tests/test_gui_messages.py` 按**真失败**钉住常见的那几种 |
| D2 | O6 里"校验 / 编解码那些文案也改中文" | 本轮**没做**，记在 §5 | 它们出现在"点表编辑期"，同一句被几十条用例断言着；机械但量大，单独一批 |

## 5. 不做（写明理由）

| 内容 | 为什么 |
|---|---|
| 第 5 条里"mypy 全仓严格" | 首轮噪音不可控；先把三棵子树钉住，其余留给后续按文件收 |
| 第 9 条里的 pip hash 校验 | 需要把 wheel 全部下载归档（现场没有 PyPI 镜像时用不了）；先做"钉死 + lock 文件" |
| 扩展 7 的 S7 定时器/计数器区（`dt`/`ct`） | 用 `pys7` 主站读这两类区的行为要单独实测（值语义不是字节） |
| 扩展 7 里的 `s7string`（真 S7 STRING：前两字节是最大/当前长度） | 本轮三种 Modbus 传输已经占满这一批；`s7string` 要动 `s7_codec` 与 S7 的跨度计算，单独一批 |
| UDP 的「声明里额外监听地址」 | TCP 那一支靠 `loop.create_server` 再挂几个监听，UDP 要走 `create_datagram_endpoint`（每个地址一个协议对象）；本轮**不静默**：启动时日志里说一句「这一路只监听通道自己那个地址」 |
| 扩展 2/3/4/5/6/8/9 | 用户本轮明确只要扩展 1 与 7 |
| 校验（`validate.py`）与编解码（`codec/`）的英文文案改中文 | 见 D2：同一句被几十条用例断言着，单独一批做（口径与做法已定） |
| mypy 覆盖 `backend/` 与 `net/` | 那两层的注解要先补完（pymodbus 与 ctypes 的动态用法），否则 `# type: ignore` 一片 |
