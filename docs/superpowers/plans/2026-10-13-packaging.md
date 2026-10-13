# 打包（PyInstaller）实现计划

日期：2026-10-13　｜　范围：把已经做完的 v1（TCP/RTU/S7 三路通道 + GUI）打成**免装 Python 的
Windows 可执行包**，并在冻结环境下把三路通道与命令行一起跑一遍。
前置：M1–M6 与 S7 里程碑全部完成（spec §11）；本计划**只加打包外壳**，不改任何既有行为。

> 这是 spec §13 记的那条尾巴（"打包推迟到 v1 功能稳定后，v1 不验证打包会留一个尾巴，
> 届时单列里程碑"）。技术前提是**读代码 + 实测**两路交叉出来的，不是推测：
> 侦察脚本与命令见 §2，产物实测数据见 §4。

---

## 1. 目标与边界

做：

- 一份 `packaging/simbench.spec`，产出两个可执行文件：`simbench.exe`（控制台：`--check` /
  `--list` / 无头监听 / `net` 子命令）与 `simbench-gui.exe`（无控制台：双击即开界面）。
- 冻结后**行为与源码运行一致**：三路通道照常起、点表照样校验、`net` 子命令照样提权。
- 修掉冻结后唯一确定会碎的点：提权重启自己那条路（`-m simbench` 在冻结包里不成立，见 §3）。
- 打包说明写进 README；spec §13 那条尾巴清零。

不做（见文末"不做"表）：安装器（Inno Setup / MSI）、代码签名、自动更新、单文件（onefile）
作为**发布形态**、把 `examples/`/`tests/`/`docs/`/验收主站 `pys7` 打进包、非 Windows 平台。

---

## 2. 技术前提（本机已实测，非推测）

明细命令见本节末尾；结论四条：

1. **冻结会碎的只有一处**：`simbench/net/cli.py::_run_elevated` 用
   `-FilePath '{sys.executable}' -ArgumentList @('-m','simbench','net',…)` 重跑自己。
   源码运行时 `sys.executable` 是 `python.exe`、`-m simbench` 成立；冻结后 `sys.executable`
   就是 exe 本身，`-m` 没有任何意义（`simbench.exe -m simbench net …` 会被 argparse 拒掉）。
   → 按 `sys.frozen` 分派命令行前缀（§3 口径 2）。
2. **没有动态 import、没有 `__file__` 资源路径**：`simbench/` 下 grep `importlib` / `__import__`
   / `__file__` **零命中**（命中的全是注释里提到 `pkg_resources` 那两句）。原生依赖的 import
   形态都是静态的：`from pymodbus.server import ModbusTcpServer`、`import serial`、
   `from snap7 import server`（延迟 import 在函数体里，PyInstaller 的 AST 扫描照样看得见）。
   → 不需要为自家代码写 `hiddenimports`。
3. **`snap7.dll` 的寻找方式对打包友好**：`snap7.common.find_in_package()` 是
   `pathlib.Path(__file__).parent / 'lib' / 'snap7.dll'`（实测源码），不读注册表、不读
   `PATH`。→ 只要把 `snap7/lib/snap7.dll` 当**数据文件**收进包（PyInstaller 的
   `--collect-all snap7` 一次收齐数据 + 子模块 + 动态库），冻结后 `__file__` 落在
   `_MEIPASS/snap7/common.py`，这条路自然指向 `_MEIPASS/snap7/lib/snap7.dll`。
   另：`snap7/__init__.py` 第 4 行 `import pkg_resources`（1.3 的既有行为），所以
   **`pkg_resources`（setuptools）也必须进包**，否则 import snap7 就崩。
4. **`pymodbus` 没有动态 import**：12 个包文件里只有
   `server/simulator/http_server.py` 一处 `importlib`（我们不用的那条路），帧解析器
   （`FramerType`）是模块顶层的静态映射。→ 静态分析能收齐，**不用**手工列 framer 子模块。

侦察命令（可重跑）：

- `python -c "import sys; print(sys.version)"` → 3.13.9（本机 venv，Anaconda 基座）
- `pip list` grep → `python-snap7 1.3` / `pymodbus 3.15.0` / `pyserial 3.5` / `PySide6 6.9.2` /
  `pys7 3.1.1`（验收主站）/ `setuptools 80.10.2`
- `python -c "import snap7, os; print(os.path.dirname(snap7.__file__))"` 后遍历 → 唯一二进制是
  `snap7/lib/snap7.dll`
- `inspect.getsource(snap7.common.find_in_package)` / `Snap7Library` → 定位口径见上第 3 条
- 对 `simbench/` 的全包 grep：`importlib|__import__|__file__`、`sys.executable`
- 对 `pymodbus` 安装目录的 grep：`importlib|__import__`

---

## 3. 执行前必须先定的口径（不要带着猜测写代码）

| # | 要定的事 | 结论（含理由） | 备选 |
|---|---|---|---|
| 1 | **发布形态：onedir 还是 onefile** | **两者都出，onedir 是发布形态**：onedir 启动快（不每次解压）、缺 DLL 时看得见、现场排查只要看目录；onefile 作为"单文件便于拷给同事"的附加产物，但 PySide6 打进单文件每次启动都要解压上百 MB，启动时间与临时目录占用都不划算 | 只做 onefile：拷贝方便但启动慢、排障难 |
| 2 | **冻结后提权怎么重跑自己** | **按 `sys.frozen` 分派**：冻结 → `[sys.executable, 'net', *parts, …]`；源码 → `[sys.executable, '-m', 'simbench', 'net', *parts, …]`。抽成 `net/cli.py::_self_command_prefix()` 一个函数，单独可测（monkeypatch `sys.frozen`），**真 UAC 仍留人工** | 冻结时改写成 `[sys.executable, *parts]`（把 net 塞进 argv[0]）：与 `__main__` 的分流口径不一致，容易两边打架 |
| 3 | **两个 exe 怎么分工** | **`simbench.exe` = console=True**（`--check/--list/监听/net` 全走它，报错能看）；**`simbench-gui.exe` = console=False**（双击开界面）。GUI 那个仍认显式命令行动作（`--check/--list/net` 优先于自动开界面），免得"想跑校验却弹出一个空窗口" | 只出一个 exe：双击会挂一个黑框，且 GUI 崩溃信息无处可看 |
| 4 | **哪些东西进包** | 进：`simbench` 全包、`pymodbus`、`serial`、`snap7`（含 dll）、`pkg_resources`、PySide6（官方 hook）。**不进**：`tests/`、`docs/`、`examples/`、`pys7`（只在测试里当主站用）。`examples/covi-tunnel.csv` 单独当**数据文件**放进去一份，好让"第一次拿到 exe"的人有个能点的点表 | 把 examples 整个目录打进去：多带一串演示点表，且与源码目录不同步时容易混淆 |
| 5 | **onefile 的"自身命令"要不要特殊处理** | **不要**：onefile 冻结后 `sys.executable` 仍是那个 exe，提权重跑它就是重新解压再跑一遍，行为正确（只是慢一点）。那条路本来就只走 `net device/up/clean` 这几条命令 | 特意为 onefile 复制一份 exe 出来再提权：多余 |

---

## 4. 任务

四段，每段跑完都能单独验收（Task 1/2 是代码，Task 3 是产物，Task 4 是文档）。

### Task 1　冻结入口与提权命令构造（代码）

- `simbench/__main__.py`：加 `gui_main(argv=None)` —— GUI exe 的入口。规则：显式命令行动作
  （`net` / `--check` / `--list`）优先，其余情况自动补 `--gui`（已经带 `--gui` 就不重复补）。
  `main()` 本体不动，两个入口共用它。
- `simbench/net/cli.py`：加 `_self_command_prefix()`，`_run_elevated` 用它拼 passthrough。
- `tests/test_frozen_entry.py`（新）：
  - 源码口径：`_self_command_prefix() == [sys.executable, '-m', 'simbench', 'net']`；
  - 冻结口径：monkeypatch `sys.frozen=True` + `sys.executable='C:/x/simbench.exe'` →
    `['C:/x/simbench.exe', 'net']`，且 `_run_elevated` 拼出的 `Start-Process` 脚本里
    **不含** `-m`、包含 `'net'` 与 `--job`（用假的 subprocess.run 抓脚本字符串）；
  - `gui_main`：空 argv → `--gui`；`['x.csv']` → `['--gui','x.csv']`；`['--check','x.csv']`
    与 `['net','show']` **不加** `--gui`（也不开窗口）。
- 这一段的验收：三条新用例全绿 + 既有 `tests/test_cli.py` / `tests/test_net_*.py` 一个断言不改。

### Task 2　打包 spec 与入口脚本（产物定义）

- `packaging/entry_cli.py`：`from simbench.__main__ import main; raise SystemExit(main())`
- `packaging/entry_gui.py`：`from simbench.__main__ import gui_main; raise SystemExit(gui_main())`
- `packaging/simbench.spec`：一次 `Analysis` 复用（两个 EXE 共用 `PYZ`/`COLLECT`），
  `hiddenimports=['pkg_resources']`、`collect_all('snap7')`、`collect_data_files` 取
  `examples/covi-tunnel.csv`；`EXE(console=True)` + `EXE(console=False)`。
- `packaging/build.ps1`（可选）：一条命令跑 `pyinstaller`，产物落 `dist/`。
- `.gitignore` 加 `build/`、`dist/`、`*.spec` 的生成物（`packaging/simbench.spec` 要留）。

### Task 3　冻结冒烟（在 dist 上真跑）

| # | 场景 | 命令（dist 内） | 期望 |
|---|---|---|---|
| 1 | 校验点表 | `simbench.exe examples\covi-tunnel.csv --check` | 打印 valid，退出码 0 |
| 2 | 打印点表 | `… --list` | 逐通道逐点一行，中文不乱码 |
| 3 | 读不到文件 | `… nope.csv --check` | 一行 `[error] [file] …`，退出码 2，无 traceback |
| 4 | TCP 从站 | `… 临时点表 --port <随机>` + pymodbus 主站读 | 值正确 |
| 5 | RTU 从站 | `… 串口点表`（`socket://` 那一套） | 值正确、CRC 正常 |
| 6 | S7 从站 | `… s7 点表` + pys7 主站读 | 值正确（验证 `snap7.dll` 真进包） |
| 7 | `net` 只读 | `simbench.exe net show` | 网卡表，退出码 0 |
| 8 | `net` 提权命令构造 | 假 `sys.frozen` 单测（Task 1） | 命令前缀正确（真 UAC 留人工） |
| 9 | GUI | `simbench-gui.exe examples\covi-tunnel.csv` 起、关 | 窗口起来、无控制台、能启停通道 |
| 10 | 缺 DLL 形态 | 把 `snap7/lib` 挪走后跑 S7 | 一句带安装提示的错，不崩 |

记录：包大小、冷启动时间（onedir vs onefile）、缺 DLL 的报错原文。

### Task 4　文档回写

- README 加"打包成 exe"一节（怎么打、产物在哪、两个 exe 分别干什么）。
- spec §13 那条尾巴清掉，§11 加第 8 条（打包里程碑）。

---

## 5. 执行结果（实测，2026-10-13）

产物在 `dist/`：

| 产物 | 大小 | 冷启动（`--check`） |
|---|---|---|
| `dist/simbench/simbench.exe`（控制台） | 4.89 MB | 0.42–0.57 s |
| `dist/simbench/simbench-gui.exe`（无控制台） | 4.89 MB | — |
| `dist/simbench/_internal/`（两个 exe 共用） | 114.2 MB | — |
| `dist/simbench-onefile.exe`（单文件） | 48.7 MB | 1.7–1.9 s |

`packaging/smoke.py`（拿真主站打产物）**11/11 通过**：

| # | 场景 | 结果 |
|---|---|---|
| 1 | `--check` 说点表合法 | OK（rc=0） |
| 2 | `--list` 打出中文点位名 | OK（rc=0，中文不乱码） |
| 3 | 读不到文件报一行错 | OK（`[error] [file] …`，rc=2，无 traceback） |
| 4 | TCP 从站 + pymodbus 真主站 | OK（hr2=[11]、float32 CDAB=`[0,16712]`、写回 999） |
| 5 | RTU 从站 + pymodbus 真串口主站 | OK（初值 7、写回 4242） |
| 6 | S7 从站 + `pys7` 真主站 | OK（real/word/位全对，写回 1234） |
| 7 | `net show` | OK（rc=0） |
| 8 | 提权命令构造（假 `sys.frozen`） | OK（前缀 2 条 + 真被拼进 `Start-Process` 的 argv 2 条） |
| 9 | `simbench-gui.exe` 不带参数就开界面（分派正确 + Qt 起得来） | OK |
| 9b | `simbench-gui.exe` 收点表参数 | OK |
| 10 | 缺 `snap7.dll` | OK（一行带安装提示的错，rc=2） |
| + | onefile：`--check` 与 S7 各一条 | OK（DLL 从临时目录解压出来可用） |

原始输出（含缺 DLL 的报错原文、踩到的两个坑、冷启动与体积）留档在 `packaging/SMOKE.md`。
真 UAC 弹窗与真上位机连冻结包仍在人手上（见那份文档最后一节）。

## 执行期已裁决的偏离

| # | 原计划写的 | 实际做的 | 为什么 |
|---|---|---|---|
| D1 | 演示点表当**数据文件**打进包（落 `_internal/examples/`） | 由 `build.ps1` 在构建后拷到 `dist/simbench/examples/`，不进 `_internal/` | 实测发现 `_internal\` 是个下划线开头的目录，第一次拿到 exe 的人不会去翻它；演示点表是给**人**的，不是运行时要读的数据。"数据 vs 给人看的文件"这条界线也更好讲 |
| D2 | Task 3 行 10 期望"一句带安装提示的错，不崩" | 这条**原来做不到**（`snap7.dll` 缺失时是一段 `RuntimeError` traceback），改 `s7_server._create_server`：先单独 `snap7.common.load_library()` 探一次 | snap7 是"用到才加载 DLL"，import 期的可用性闸门拦不到；顺带消掉了它的 `Server.__del__` 在解释器退出时多打的 `Exception ignored … has no attribute 'library'`（半构造对象的问题） |
| D3 | 冒烟脚本只写了"收掉子进程" | `close()` 改成 `taskkill /F /T /PID`（连子进程一起收） | onefile 的 bootloader 会再起一个真身，只 `terminate()` 父进程会留下孤儿**锁住 exe 文件**：下一次 `build.ps1` 直接 `PermissionError: WinError 5`，而且临时端口一直被占（实测攒了 3 个） |
| D4 | Task 3 的行 4–6 只针对 onedir | onefile 另加 `--check` 与 S7 两条冒烟；GUI 那一行只验"起得来且活着" | onefile 的 DLL 要从临时目录解压出来，这条不验等于没验单文件形态；窗口内容（布局/高 DPI）本来就在 M2 的人工清单里，不重复造 |
| D5 | `build.ps1` 写了 `-SkipOnefile` / `-SkipOnedir` 两个开关 | 去掉：spec 一次就把三样都打出来 | 开关没有实际作用（一个 spec 一次构建出全部产物），留着是骗人的参数 |

## 完成检查表

- [x] Task 1 的三条新用例全绿（实际落成 `tests/test_frozen_entry.py` 9 条：提权前缀 2 条、
      提权重跑 argv 2 条、`gui_main` 5 条；另在 `tests/test_s7_backend.py` 加 1 条钉 D2），且既有用例一个断言不改
- [x] `dist/simbench/simbench.exe` 与 `dist/simbench/simbench-gui.exe` 都在（同一个目录共用
      `_internal/`），另有 `dist/simbench-onefile.exe`
- [x] Task 3 表 11 行（含 9b）逐行有结论，原始输出留档在 `packaging/SMOKE.md`（另加 onefile 两条）
- [x] 全量串行 `.venv\Scripts\python.exe -m pytest` 全绿：**1249 → 1259**（+9 条打包入口用例、
      +1 条缺 DLL 那条），只增不减
- [x] README 打包一节 + spec §11 第 8 条 + §13 尾巴清零 + §2/§3 两处口径回写
- [x] `git status --short` 为空
- [ ] 人工：真 UAC 提权重跑一次（冻结包）+ 真上位机连冻结包 + 双击 GUI 的观感

## 不做（留给后续）

| 内容 | 为什么不在这里 |
|---|---|
| 安装器 / 开始菜单 / 卸载 | 现场是"拷目录就能用"，装一次反而多一层要维护的东西；真要装再说 |
| 代码签名 | 要证书；未签名 exe 在别人机器上可能被 SmartScreen 拦一次，先把功能验完 |
| 自动更新 / 版本号展示 | 没有分发渠道，v1 靠人拷 |
| 非 Windows 打包 | `net/` 那层本来就是 Windows 专用（netsh/PowerShell），跨平台打包没有目标场景 |
| 把 `pys7` 打进包 | 它是**验收主站**（测试用），不是产品的一部分；进包只会让现场多一个看不懂的入口 |