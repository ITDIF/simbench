# 打包产物的冒烟记录（2026-10-13 首轮，2026-10-14 补一轮）

这一份是**冻结形态**（`dist/` 里的 exe）的验收记录：`tests/` 那 1387 条用例跑的是源码，
永远测不到"打包漏了一个 DLL / 漏了一个纯 Python 包"这类只有产物才有的问题。

## 怎么跑

```powershell
.\packaging\build.ps1                                   # 打全套（约 2 分钟）
.\.venv\Scripts\python.exe packaging\smoke.py            # 用真主站打产物（约 90 秒）
```

`smoke.py` 用仓库 venv 里的 `pymodbus` / `pys7` 当**主站**，被验的是 `dist/` 里的 exe，
十三条逐条打印 `[OK]/[FAIL]`，全通过退回码 0。`--skip-gui` 可跳过要桌面的那一条，
`--dist` / `--onefile` 可指向别处的产物。

## 最近一轮实测结果（13/13，2026-10-14）

```
[OK  ] --check 说点表合法  — rc=0
[OK  ] --list 打出中文点位名  — rc=0
[OK  ] 读不到文件报一行错且退出码 2  — rc=2
[OK  ] net show（只读那一条）  — rc=0
[OK  ] Excel 点表：冻结包读得进（openpyxl 真进包 + 表头在标题行之下也认）  — rc=0
[OK  ] TCP：读初值 + 写进去能读回来（含 float32 CDAB 的字节序）  — hr2=[11] float=[0, 16712] back=[999]
[OK  ] RTU（socket:// 虚拟串口）：读初值 + 写进去能读回来  — read=[7] back=[4242]
[OK  ] S7（snap7.dll 真进包）：读初值 + 写进去能读回来  — |Δ|=0.0 words=[11, True] back=[1234]
[OK  ] 主站模式：--verify 在冻结包里核对并写出报告  — rc=0 核对完成：对得上 2
[OK  ] simbench-gui.exe 不带参数就开界面（分派正确 + Qt 起得来）
[OK  ] simbench-gui.exe 收点表参数
[OK  ] onefile：--check 通过（2.7s 含解压）  — rc=0
[OK  ] onefile：S7 读得到（DLL 从临时目录解压出来）

13/13 通过
```

（首轮 11 条的输出与结论见本文件的 git 历史；这一轮加的是 **Excel 点表**与**主站模式**两条。）
两条 GUI 里，"**不带参数**"那条才是真的在验分派：`entry.py` 靠"没有控制台"认出自己是界面
那一个，而如果它被当成命令行程序，会立刻以 `point table is required` 退 2 —— 所以"它没退"
同时钉住了"两个 exe 分派正确"与"Qt 在冻结包里起得来"，不是一句"能启动就算过"。
（这条判据本身也是实测出来的：把 `simbench-gui.exe` **改名**成 `tmp-probe.exe` 再不带参数跑，
它照样活着、照样开界面——说明生效的是"没有控制台"，不是文件名那一条副判据。探针脚本用完删了。）

新增的 Excel 那条为什么非有不可：`openpyxl` 是**纯 Python** 包，进的是 PYZ 归档、不在
`_internal/` 里落成文件——所以"包里有没有它"在文件系统上看不出来，只有**拿冻结包去读一个真
`.xlsx`** 才算数。那张表故意在表头上面压一行标题：顺手把"表头不在第一行"也走一遍
（`[note] 表头在第 2 行，前面 1 行按标题跳过`），一条检查钉两个判据。

`float=[0, 16712]` 就是 `12.5` 在 CDAB 字序下的寄存器序列（`0x0000, 0x4148`，与
`tests/test_cli.py` 里钉的字面量同一个值）——它证明**冻结之后编解码那条路没变**。

## 缺 DLL 的形态（把 `snap7/lib/snap7.dll` 挪走再跑 S7）

```
[error] [port] channel s7-1: snap7.dll cannot be loaded (can't find snap7 library. If
installed, try running ldconfig); install S7 support with `pip install "python-snap7==1.3"
"setuptools<81"` (the 1.3 wheel carries snap7.dll)
```

退出码 2，一行，没有 traceback。这是本轮**改出来**的一条：原来这里是一段
`RuntimeError` traceback（`Server.__init__` 里 `load_library()` 失败），因为 snap7 是
"用到才加载 DLL"，`import` 阶段的可用性闸门拦不到它。

## 冷启动与体积（本机，SSD，含进程创建）

| 形态 | `--check` 冷启动 | 体积 |
|---|---|---|
| onedir（`dist/simbench/`） | 0.42–0.47 s | 125.1 MB（其中 `_internal/` 114.3 MB） |
| onefile（`dist/simbench-onefile.exe`） | 1.7–2.1 s | 49.2 MB |

（2026-10-14 复测：加进 `openpyxl` 之后 onedir 涨了约 1 MB。）

onedir 是**发布形态**：启动快 3–4 倍，缺文件时看得见（`_internal/snap7/lib/snap7.dll`
就在那儿），现场排查不用等解压。onefile 是"拷一个文件给同事"的方便形态，慢是它的固有代价。

## 本轮踩到并修掉的两个坑

1. **onefile 会留下孤儿进程**：bootloader 父进程再起一个真身，只 `terminate()` 父进程的话
   真身会活着占住 exe 文件——下一次 `build.ps1` 直接
   `PermissionError: [WinError 5] ... dist\simbench-onefile.exe`，而且临时端口也一直被占。
   修法在 `smoke.py` 的 `close()`：`taskkill /F /T /PID`（连子进程一起收）。
2. **`snap7.dll` 不在时的 traceback 尾巴**：修法见上一节（先单独
   `snap7.common.load_library()` 探一次，就不建那个半成品 `Server` 对象了，也就不会再触发
   它 `__del__` 里那行 `Exception ignored ... has no attribute 'library'`）。

## 还留在人手上的（自动化替不了）

1. **真 UAC 提权重跑**：`simbench.exe net device create`（会弹一次 UAC）。本轮只把"提权重跑
   自己"那条命令行**用假 `sys.frozen` 单测钉住**（`tests/test_frozen_entry.py`，4 条），
   真弹窗留给人在虚拟机/真机上点一次——不许用脚本去点 UAC。
2. **真上位机连冻结包**：按 `本机IP:102` 用组态王/力控一类驱动读一遍（防火墙放行那步同样是
   人工），口径与 S7 计划里的同一条。
3. **双击 `simbench-gui.exe` 的观感**：本轮只验了"起得来、活着、能收掉"，窗口布局/高 DPI
   那几条仍在 M2 的人工清单里。