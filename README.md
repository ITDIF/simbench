# simbench

设备通讯模拟仿真：Modbus 从站模拟器（TCP 与 RTU 两种通道）。点表（CSV）驱动，
桌面界面 + 无界面两种跑法，带报文监视与预设波形（const / ramp / sine / random / step）。

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

系统网络（专用网卡 + 设备 IP）那一层是 `python -m simbench net ...`：`show` / `check` /
`device show` 只读；`up` / `clean` / `device create|remove` 会动系统网络（要提权）；
`init` 从点表和 IP 清单生成声明骨架。

## 从零建一份工程（不用先写 CSV）

界面上点"**新建工程**"：选**空白**起步，或选 TCP / 串口 / S7 的**骨架**（一条通道 + 一个
设备 + 一个点位，参数是该传输的默认值，名字是 `tcp-5020` / `设备1` / `点位1` 这类占位符）。
之后：

- **设备树右键**：空白处 → 添加通道；通道行 → 添加设备 / 删除通道；设备行 → 添加点位 / 删除设备；
- **点位表右键**：添加点位…（加到这一行所属的设备）/ 删除点位…（删这一个）；属性…
  改名字、区、地址、类型、字序、初值、波形、是否只读。

几条口径，先知道少踩坑：

- **默认值都避开已有的**：通道名与端口取下一个没用的、unit 取最小空号、点位地址取该设备该区
  "已占末尾之后"，所以点着加不会撞重叠；S7 的默认端口是惯例的 **102**。
- **串口名不替现场猜**：新建串口通道时那一格要自己填 COM 口——本工具不会替你写一个 `COM3`，
  因为它很可能是别人真设备的串口。
- **有通道在跑时不许增删**：先"全部停止"再改点表（加/删通道要走一次整体重装载，会停掉正在
  服务的那一路；加/删点位也会与在跑的从站对不上）。界面会点名说"哪一路在运行"。
- **删除会先问一次**（带"连同 N 个设备、M 个点位"），删掉不可撤销。
- **换工程前先问未保存改动**：新建 / 打开 / 导入 / 关窗四个动作都会问一次"保存 / 丢弃 / 取消"。
- 新建的工程还没有文件路径：**保存工程**会弹另存为，**导出 CSV** 也会先问落到哪。
  注意 TCP 的端口在 CSV 里是靠**通道名尾数**带过去的（默认名 `tcp-5020` 就是它），
  改了通道名之后导出会有一条提示告诉你 CSV 读回来会变成哪个端口。

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
