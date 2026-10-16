"""系统网络层：读现状、算差异、按需增删网卡地址。

**本包是本工具唯一允许调用 PowerShell / netsh 的地方**（`tests/test_architecture.py` 守
这条线，判据是 `subprocess` 只许出现在本包）。`gui/` 与 `engine/` 都不直接碰系统网络配置：
GUI 只显示差异，命令由这一层执行。

不 import pymodbus，也不 import PySide6 —— 这一层管的是"让 IP 在操作系统里存在"，
与协议、界面无关，无头 CLI 与 GUI 共用。

分工：`spec.py`（声明文件）、`plan.py`（差异判定，纯函数）、`state.py`（只读现状）、
`ledger.py`（我们加过什么）、`device.py` + `win32.py`（专用网卡）、`endpoints.py`（这一路
通道要额外监听哪些地址）、`startup.py`（启动挂钩与网络页的入口）、`cli.py`（命令行）。
`errors.py` 是**叶子**（只放 `NetError`，什么都不 import）：`endpoints.py` 与 `spec.py`
都要用它，而它们不该因此把 `state.py` 那一层（ctypes + subprocess）拖进来。
口径与"为什么"写在 spec §14。

安全口径（现场不许留痕）：

- 只认工程声明的那一块网卡。定位按 **GUID**，中文别名（"以太网 5"）跨机器/跨语言会变，
  只当显示名用。
- **只读不需要管理员**；任何增删都走显式提权路径，校验流程里绝不顺手改系统。
- 删除只针对本工程记过账的地址；网卡上原有的地址一律不动（`PrefixOrigin` 不是 Manual
  的一律视为"不是我们的"）。
"""
