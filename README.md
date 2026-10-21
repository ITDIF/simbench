# simbench

设备通讯模拟仿真：Modbus 从站模拟器（TCP 与 RTU 两种通道）。点表（CSV / Excel / 工程文件）
驱动，桌面界面 + 无界面两种跑法，带报文监视与预设波形（const / ramp / sine / random / step）。

## 跑起来

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt      # 现场只要这一份

# 桌面界面（点表可省，装载留给界面）
.\.venv\Scripts\python.exe -m simbench --gui examples\covi-tunnel.csv

# 无界面监听（CI / 只想要从站）
.\.venv\Scripts\python.exe -m simbench examples\covi-tunnel.csv --port 5020

# 只校验点表 / 打印点表
.\.venv\Scripts\python.exe -m simbench examples\covi-tunnel.csv --check
.\.venv\Scripts\python.exe -m simbench examples\covi-tunnel.csv --list
```

**串口（RTU）通道**在 CSV 里用通道名写：`通道名@串口名[ 波特率[ 帧格式]]`，例如

```csv
channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
com-485-1@COM3 19200 8E1,COVI-1,11,回路温度,hr,0,uint16,ABCD,25,const,,false
```

一条通道 = 一个串口、多个 unit 挂同一条总线；串口参数（波特率/校验/停止位/超时）在界面的
通道行上改，也可以写进 `project.json`。真机（USB-485）上的冒烟步骤见
`docs/superpowers/plans/2026-10-12-modslave-m5-rtu.md` 的 Task 8.3。

**S7（ISO-on-TCP / S7comm）通道**也在点表里写：

```csv
channel,device,unit_id,name,area,address,datatype,word_order,initial,waveform,wave_param,readonly
s7-1@192.168.0.10:102 0/1,D1,11,温度,db1,0,real,ABCD,12.5,const,,false
s7-1@192.168.0.10:102 0/1,D1,11,启动,db1,6.3,bool,ABCD,true,const,,false
```

语义与 Modbus 那两条不同：`area` 是 S7 的区（`db<n>` / `mk` / `pe` / `pa`）、`address` 是
**字节偏移**（`bool` 写成 `字节.位`，如 `6.3` = `DBX6.3`）、`datatype` 用 S7 的名字
（`bool/byte/char/word/int/dword/dint/real/string`）、**没有字序**（一律大端，填了别的字序只给
一条警告）。通道名语法 `name@host:port[ rack/slot]`；`rack/slot` 省略时是 `0/1`。

S7 支持需要 `snap7.dll`（随 `python-snap7==1.3` 的 wheel 来，LGPL-3.0）：
`pip install "python-snap7==1.3" "setuptools<81"`（后者是因为 1.3 会 import 已被
setuptools>=81 移除的 `pkg_resources`）。**没装也能用 Modbus**：只有起 S7 通道时才会报一句
带安装提示的错。注意 S7 这一路的报文窗只有**主站请求事件行**（原生库不交出原始字节，
见计划 §3 的口径 5）。

**一张点表里可以混装多种协议**（TCP / 串口 / S7，各通道按自己的传输走）。无界面监听会把表里
**所有**通道一起起起来（每路一行 `listening on …` / `serving on …`）；某一路起不来（端口被占、
串口没插）只在 stderr 上留一行带通道名的错，**其余照服务**——一路都没起来才当场退出码 2，部分
起不来的情况在收工（Ctrl+C）时回 2。

系统网络（专用网卡 + 设备 IP）那一层是 `python -m simbench net ...`：`show` / `check` /
`device show` 只读；`up` / `clean` / `device create|remove` 会动系统网络（要提权）；
`init` 从点表和 IP 清单生成声明骨架。

## 一台虚拟设备，5 分钟用 Modbus Poll 读起来

```powershell
.\.venv\Scripts\python.exe -m simbench examples\virtual-device.csv
```

`examples/virtual-device.csv` 是一台**虚拟设备**（"隧道机电 1 号机"，unit id **11**）：
四区都有点、保持寄存器 hr 0..14 **连续**，里面既有会动的（回路温度 sine、风速 ramp、
启动命令 step）也有不动的（状态字、累计电量 uint64、铭牌 string）。

Modbus Poll 里：Connection 选 **Modbus TCP/IP**、IP `127.0.0.1`、Port `5020`、Slave ID `11`；
然后 `F8` 读保持寄存器：地址 1（勾着 "PLC Addresses (Base 1)"）/ 0（勾掉）、数量 10
→ 值就该动起来。完整清单（含 32 位 float 的字序怎么设）在
`docs/superpowers/notes/2026-10-14-modbus-poll.md`。

三条现场最容易被坑的（都是实测形状）：

- **读的范围必须落在点表声明过的格上**：点表里有空洞时，那一条请求会**整条**回异常码 2
  （Modbus 的语义就是"要么整条成功、要么整条失败"），现场看着像模拟器坏了。有空洞就先把
  点表补连续：`python -m simbench 现场点表.csv --fill-gaps --result 现场点表-连续.csv`
  ——补出来的是**看得见的**保留点（`reg16`/`bool`、值 0、名字 `保留<地址>`），不是隐式回 0；
- **Slave ID 不对回的是异常码 4**（不是超时）：扫 unit 时"有应答"就说明那个号被回应了；
- **一次最多读 125 格**，超过回异常码 1（协议上限）。

## 支持哪些传输（点表里怎么写）

| 传输 | 通道名写法 | 说明 | 主站侧怎么连 |
|---|---|---|---|
| Modbus TCP | `tcp-5020@tcp:0.0.0.0:5020` | 最常见的一路 | Modbus Poll / QModMaster 直接连 |
| Modbus UDP | `udp-5020@udp:0.0.0.0:5020` | 现场的网关与老平台 | 主站客户端选 UDP |
| RTU over TCP | `gw-1@rtutcp:192.168.0.9:4001` | 以太网转串口的网关：网络上跑的是 RTU 帧 | `ModbusTcpClient(framer=RTU)` |
| 串口 RTU | `com-485-1@COM3 19200 8E1` | 一条串口总线，多个 unit 挂上面 | 串口主站（参数照抄） |
| 串口 ASCII | `com-485-1@COM3 19200 8E1 ascii` | 少数设备说的是 ASCII | 主站选 ASCII framer |
| S7 | `s7-1@192.168.0.10:102 0/1` | ISO-on-TCP / S7comm（区是 `db<n>`/`mk`/`pe`/`pa`） | 组态软件/S7 驱动连 102 |

一张点表里可以**混装**这六种（各通道按自己的传输走）。`@` 后面那一段把"这台设备在哪儿"
写全，所以**导出再导入逐字一致**；老写法（不带 `@`、端口按名字尾数猜）仍然能读，读的时候
会有一条提示。第四段（`ascii`）只在 ASCII 通道上写——它是帧类型，不是第四个串口参数。

## 从零建一份工程（不用先写 CSV）

界面上点"**新建工程**"：选**空白**起步，或选 TCP / 串口 / S7 的**骨架**（一条通道 + 一个
设备 + 一个点位，参数是该传输的默认值，名字是 `tcp-5020` / `设备1` / `点位1` 这类占位符）。
之后：

- **设备树右键**：空白处 → 添加通道；通道行 → 添加设备 / 删除通道；设备行 → 添加点位 / 删除设备；
- **点位表右键**：添加点位…（加到这一行所属的设备）/ 删除点位…（删这一个）；属性…
  改名字、区、地址、类型、字序、初值、波形、是否只读。
- **点表按设备分类**：第一列是「设备（unit）」，多台设备时一眼看得出哪些点属于哪台；
  设备树上**点某台设备**表就只显示那一台的点，**点通道行**回到这一条通道的全部设备。

几条口径，先知道少踩坑：

- **默认值都避开已有的**：通道名与端口取下一个没用的、unit 取最小空号、点位地址取该设备该区
  "已占末尾之后"，所以点着加不会撞重叠；S7 的默认端口是惯例的 **102**。
- **串口名不替现场猜**：新建串口通道时那一格要自己填 COM 口——本工具不会替你写一个 `COM3`，
  因为它很可能是别人真设备的串口。
- **在跑的通道上可以加/删别的通道**（2026-10-13 起）：新加/删的那一路自己启停，别的不受影响。
  **目标通道自己在跑时**不许改它（加/删它的设备、点位，或删它）：先停它——理由是
  「在跑的从站拿着的是启动那一刻的地址表」，改了点表会与它拆成两份真相；界面会点名说哪一路在跑。
- **删除会先问一次**（带"连同 N 个设备、M 个点位"），删掉不可撤销。
- **换工程前先问未保存改动**：新建 / 打开 / 导入 / 关窗四个动作都会问一次"保存 / 丢弃 / 取消"。
- **保存工程**在还没有路径时会弹另存为（有路径就直接写回），**导出点表**每次都问落到哪
  ——那里有两种点表格式可选，默认名是工程旁边那份 `.csv`（回车即就地导出一份 CSV）。
  点表里「这台设备在哪儿」烘在通道名里（CSV 与 Excel 同一套写法），三种写法：串口
  `名字@COM3 19200 8E1`、S7 `名字@192.168.0.10:102 0/1`、TCP `名字@tcp:0.0.0.0:5020`。
  **导出一律写全**，所以导出再导入逐字一致；老写法（不带 `@`、端口按名字尾数猜）仍然能读，
  读的时候会有一条提示。

## 三种文件格式（点表 CSV / 点表 Excel / 工程 project.json）

打开、导入、另存为、导出都认这三种，**按后缀分派**（后缀不认识就拒绝，消息里列出支持哪些
——猜一个格式等于把 JSON 内容当 CSV 解析，报出来的话离真原因很远）：

| 后缀 | 装什么 | 拿它做什么 | 已知代价 |
|---|---|---|---|
| `.csv` | 12 列点表 | 现场互通、diff 看得见 | 通道参数烘在通道名那一格 |
| `.xlsx` | 同样的 12 列表（表头冻结） | 发给只用 Excel 的同事 | Excel 的数字只有 **15 位有效数字**：17 位 uint64 那种初值存不精确 |
| `.json` | 整份工程（含 `tick_ms`、串口参数、机架/槽位） | 自己存盘、换机器搬 | 字段比点表多，别手改；它不是点表工具能读的格式 |

几条口径，先知道少踩坑：

- **Excel 的表头不必在第一行**：现场的表上面常压着"某某隧道 点表 V1.2"这种标题。读的时候找
  第一行 12 格与表头完全一致的当表头，**报错里的行号与你在 Excel 里看到的行号一致**。
- **导出写哪种由后缀说了算**：导出对话框里选 CSV 就写 CSV，选 Excel 就写 Excel；
  默认名沿用工程旁边那份 `.csv`，直接回车就还是「就地导一份 CSV」这个老习惯。
- **写 Excel 时尽量写真数字格**（右对齐、能直接算），但 `0008` / `12.50` / `1e5` 这类原样留成
  文本——不然 Excel 一存就把初值改掉了。17 位的整数同理，写成文本格。
- 要精确传那种长整数，用 CSV 或 `project.json`；15 位有效数字是 Excel 自己的限制，
  读的时候会有一条提示点明。
- 用不到 Excel 的人不受影响：`openpyxl` 是**用到才加载**的（与 S7 那条 `snap7` 同一个姿态），
  没装也能跑 CSV 与工程文件。

## 保存、备份与日志（现场带得回来的东西）

- **保存是原子的**：点表 / 工程 / Excel 都是"先写临时文件、再原子替换"。写到一半断电、
  磁盘满、进程被杀，盘上那份**还是上一次的完整内容**，不会变成半个文件。
- **覆盖时留一份备份**：`点表.csv` 的上一份在同目录的 **`点表.csv.bak`**（每存一次刷新一次，
  只留一代）。Excel 那份同理叫 `点表.xlsx.bak`——注意 Excel 与 openpyxl 都按**后缀**认格式，
  所以恢复时把 `.bak` 改成 `.xlsx` 再打开。
- **被别的程序占着会说人话**：文件正被 Excel 打开着时保存失败，提示直接点明这一点，
  并说明"这次保存没有改动原文件"。
- **日志有两个落点**：窗口里那份（日志页，可以"**另存日志**"发给同事），以及
  `%LOCALAPPDATA%\simbench\logs\simbench-<日期>.log`（**连崩溃都在**）。
  界面版 exe 没有控制台，出了事只有这两个地方能留下证据——所以别把日志页当成"看看就好"。
- **崩溃也会落盘**：未捕获异常（含引擎线程里的）写完整调用栈；解释器级崩溃（段错误）靠
  `faulthandler` 记原生栈。想换目录就用环境变量 `SIMBENCH_LOG_DIR`。

## 大点表下的刷新（界面永不等引擎）

点位表的取数跑在**后台**，界面那一跳只把最近一份快照搬进表格：

- **界面不会因为引擎忙/卡住而僵住**：取数失败或超时只影响"值什么时候更新"，界面照旧响应
  （失败时保留上一份并把原因写进日志页，绝不把表清空——那会演成"设备没值"）；
- 跳表节奏 10Hz，点表越大每跳越贵。实测（本机，单通道、值不变）：2000 点约 8ms 一跳、
  4000 点约 16ms 一跳；报文窗在总线上有流量时按增量重画（写满 5000 帧也不再整表重画）；
- 现场要点：**一次只轮询当前显示的那一条通道**。要看另一路的实时值就切过去（切换时会立刻
  同步取一次），不看的那几路不花这个成本。

## MQTT 通道（通用模板）

MQTT 与 Modbus/S7 不同：它**连到 broker**，不是监听一个端口。点表里写：

```csv
mqtt-1@mqtt:127.0.0.1:1883,温湿度-1,1,hr00-温度-℃,hr,0,float32,ABCD,23.5,const,,false
```

`host:port` 是 broker；可选在后面再写 topic 前缀和 QoS（默认 `simbench` / `1`），
例如 `mqtt-1@mqtt:10.0.0.5:1883 plant 2`。界面里通道行的「前缀」「帧」「保留」
「原始」四格对应 topic 前缀、QoS、retain、payload 形式，可以直接改：
默认 JSON（`{"value": ...}`）且 retain=True；勾「原始」后发布裸标量，取消「保留」
则发布时不带 retain。勾「遗嘱」后由 broker 在 `{prefix}/{channel}/status` 维护在线状态：
连上发布 `{"online":true}`（或 `online`），异常断开时 broker 发布遗嘱 `{"online":false}`
（或 `offline`），正常停止也会先发离线。CSV 里对应可选的
`PREFIX QOS RETAIN PAYLOAD WILL` 五段，例如
`mqtt-1@mqtt:10.0.0.5:1883 simbench 1 0 raw 1`。

**通用 topic/payload 约定**：

| 用途 | topic | payload |
|---|---|---|
| 值 | `simbench/{channel}/{device}/{area}/{address}` | `{"value": 25.5}`（也接受裸标量 `25.5`） |
| 写入 | 值 topic + `/set` | `{"value": 30}` |

模拟侧连上 broker 后把每个点位的当前值以 retain=True 发布一次，之后值变化才再发；
同时订阅各点的 `/set`，收到写入后把该点标成人工接管并回发新值。核对页也能选 MQTT
通道，按点表读真实设备、做持续监视与异常提醒。示例见 `examples/mqtt-template.csv`。
核对页的 MQTT 主站会同时订阅 `{prefix}/{channel}/status`：设备明确离线时，点位立即
报“设备离线”，不再逐点等超时。

broker 断线会自动重连（1~30 秒退避），重连后自动重新订阅；断开/恢复各在日志里留一行，
通道状态灯同时变红/恢复运行——不会出现"broker 断了但界面还显示运行"。

## 主站模式：拿点表去核对真设备（只读）

这个工具平时**当**从站；主站模式反过来——它拿点表当"要读什么"的清单，去读**别人的**设备。
现场八成的返工来自"点表跟设备对不上"，这一步把那条路缩短成一条命令/一下按钮。

```powershell
# 扫一遍 unit（默认 1..247，只读 hr 0 那一格）
.\.venv\Scripts\python.exe -m simbench examples\covi-tunnel.csv --scan --units 1-20

# 按点表逐点读回来，与点表初值核对；有对不上的回退出码 2（脚本里能当"需要处理"用）
.\.venv\Scripts\python.exe -m simbench 现场点表.csv --verify --result 核对报告.xlsx
```

界面上是**"核对"页**：先选目标通道，再 `扫描 unit` / `逐点核对` / `开始监视` /
`导出报告`（长活跑在后台线程，界面不卡）。

**持续监视**按设定间隔（1~3600 秒）反复核对同一路设备：异常行按状态标色（对不上=琥珀、
没应答/读出错=红），新增异常写日志并顶到状态栏，"核对"标签上挂 `●N` 角标；同一项一直坏
不会每轮重复刷屏，恢复时会说一句。换工程、改点表结构或关窗都会自动停止监视并清空旧结论。
几条口径：

- **只读**：核对过程一个寄存器都不写（要改值到点位表去改，那里有归属与跨度判定）；
- 结论分四类：**对得上 / 对不上 / 没应答 / 读出错**——"没应答"是设备没装或 unit 填错，
  "读出错"是协议层回异常码，两者的处置完全不同；
- 读的是**最少那几格**（按点位跨度），不顺手多读邻居——多读回来的值会让"对上了"变成假象。

## 打包成 exe（现场不用装 Python）

```powershell
.\packaging\build.ps1
```

打包要 PyInstaller（**只在开发机上**需要）：

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt   # pytest / pys7 / pyinstaller
```

产物在 `dist\` 下（同一个目录、共用 `_internal\`，演示点表在 `dist\simbench\examples\`）：

| 文件 | 用途 |
|---|---|
| `dist\simbench\simbench.exe` | 控制台：`--check` / `--list` / 无头监听 / `net` 子命令 |
| `dist\simbench\simbench-gui.exe` | 无控制台：双击开界面，点表路径可以跟在后面 |
| `dist\simbench-onefile.exe` | 单文件形态（拷给同事方便；启动要解压，慢 3–4 倍，**不是**发布形态） |

用法与源码运行一一对应，只是把 `python -m simbench` 换成 exe：

```powershell
.\dist\simbench\simbench.exe examples\covi-tunnel.csv --check
.\dist\simbench\simbench.exe examples\covi-tunnel.csv --port 5020
.\dist\simbench\simbench.exe net show
.\dist\simbench\simbench-gui.exe
```

`simbench-gui.exe` 那一个**没有控制台**，所以文字结果（校验清单、报文日志、报错）在它里面
是看不见的——要看那些就用 `simbench.exe`。冻结产物的十条冒烟记录（含 S7 通道走真
`snap7.dll`、缺 DLL 时的报错形态、冷启动与体积）在 `packaging\SMOKE.md`。

## 改代码的人要先装的（现场不需要）

```powershell
.\.venv\Scripts\pip install -r requirements-dev.txt   # pytest / pys7 / pyinstaller / ruff / mypy
.\.venv\Scripts\ruff check                            # 静态检查（配置在 pyproject.toml）
.\.venv\Scripts\mypy                                  # 类型检查（只覆盖 project / codec / engine）
.\.venv\Scripts\python.exe -m pytest                   # 全量（**串行**跑，绑真端口）
```

依赖都钉到实测版本；想连传递依赖都一样就用 `requirements.lock.txt`（`packaging/lock.ps1` 重新生成）。
版本号只有一处来源：`simbench/__about__.py`（`python -m simbench --version` 能问它），
变更记录在 `CHANGELOG.md`。

## 两个环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `SIMBENCH_THEME` | 界面起手配色：`light` / `dark`。界面上也能一键切（工具栏最右那个"深色主题"，快捷键 `Ctrl+Shift+D`） | `light` |
| `SIMBENCH_NET_AUTO` | 起从站/开界面时是否按点表旁边那份声明自动配网卡；`0` = 只看不动。命令行上对应 `--no-net` | 开着 |

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

串行跑：这套件绑真实临时端口，两份并行结果不可信。设计口径、里程碑与各轮偏离记录在
`docs/superpowers/`（spec 一份 + M1/M2/M4/M5/S7 五份实现计划 + `notes/` 下的实测脚本与输出）。
