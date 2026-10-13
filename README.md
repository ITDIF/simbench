# simbench

设备通讯模拟仿真：Modbus 从站模拟器（TCP 与 RTU 两种通道）。点表（CSV）驱动，
桌面界面 + 无界面两种跑法，带报文监视与预设波形（const / ramp / sine / random / step）。

## 跑起来

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

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

系统网络（专用网卡 + 设备 IP）那一层是 `python -m simbench net ...`：`show` / `check` /
`device show` 只读；`up` / `clean` / `device create|remove` 会动系统网络（要提权）；
`init` 从点表和 IP 清单生成声明骨架。

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
`docs/superpowers/`（spec 一份 + M1/M2/M4/M5 四份实现计划 + `notes/` 下的实测脚本与输出）。
