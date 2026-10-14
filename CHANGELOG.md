# 变更记录

只记**用户看得见的变化**（功能、行为、已知代价）与"哪个版本定下过什么"。设计细节不在这里：
每个里程碑的完整决策与实测记录在 `docs/superpowers/plans/` 与
`docs/superpowers/specs/2026-10-08-modbus-slave-simulator-design.md`。

版本号只有一处来源：`simbench/__about__.py`（exe 属性页那份 `packaging/version_info.txt`
由 `tests/test_version.py` 钉着与它一致）。

## 1.0.0

第一次给出**版本号**。到这一版为止的能力（都在 `main` 上）：

**协议与点表**
- Modbus TCP / Modbus RTU（串口）从站；一张点表里可以混装多种协议与多路同协议通道。
- S7（ISO-on-TCP / S7comm）从站（`python-snap7==1.3`，可选装）。
- 点表格式三种：CSV、Excel（`.xlsx`）、工程文件 `project.json`；三者共用一张格式表，
  加格式只改一处。Excel 的表头可以不在第一行，报错行号与 Excel 里看到的一致。
- 波形：`const / ramp / sine / random / step`；被人工或主站写过的点会在表里显示 `已停`，
  可以右键"恢复波形"交还。

**界面**
- 设备树 + 点位表（十列，含"原始寄存器"与"惯例地址"）+ 报文监视 + 日志 + 网络页。
- 多通道独立启停；在跑的通道上可以增删别的通道。
- 从零建工程：新建 TCP / 串口 / S7 骨架，右键增删通道 / 设备 / 点位，属性面板改点。
- 状态变化只改文字 / 颜色 / 勾选，几何恒定；不用下拉框（≤10 项用平铺标签）。

**证据与数据安全**（1.0.0 这一轮补的）
- 保存（点表 / Excel / 工程 / 网络声明）都是**原子替换**，并留一份 `.bak`。
- 崩溃与日志落盘：`%LOCALAPPDATA%\\simbench\\logs\\simbench-<日期>.log`（含未捕获异常、
  线程异常、`faulthandler` 的原生栈）；日志页有"另存日志"。

**打包**
- `packaging/build.ps1` 出 onedir（`simbench.exe` / `simbench-gui.exe`）与 onefile 三个产物；
  `packaging/smoke.py` 用真主站打冻结包（12 条，含 S7 的 `snap7.dll` 与 Excel 的 `openpyxl`）。
