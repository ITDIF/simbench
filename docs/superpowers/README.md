# 文档索引（`docs/superpowers/`）

这个目录里是**设计总账 + 每个里程碑的落地记录**。找东西从这里进：凡是"当时怎么定的、为什么
这么定、哪条实测推翻了什么"都写在对应的那份里，代码注释只留结论与指针。

| 文档 | 是什么 | 状态 |
|---|---|---|
| `specs/2026-10-08-modbus-slave-simulator-design.md` | **设计总账**：范围、架构、数据模型、CSV 合同、界面口径、错误处理、里程碑总表、风险、系统网络层 | 活文档，改动回写在这里 |
| `plans/2026-10-08-modslave-m1-kernel.md` | M1 地基：模型 / codec / TCP 后端 / 命令行入口 | 已完成（人工那条：第三方主站连一次） |
| `plans/2026-10-08-modslave-m2-gui.md` | M2 界面：设备树 / 点位表 / 报文窗 / 启停 | 已完成（人工那两条还挂着） |
| `plans/2026-10-11-modslave-m4-waveform.md` | M4 波形引擎与值归属 + 点位属性面板 | 已完成（人工那条还挂着） |
| `plans/2026-10-12-modslave-m5-rtu.md` | M5 串口（RTU）通道 | 已完成（真 USB-485 那五条还在人手上） |
| `plans/2026-10-13-s7-server.md` | S7（ISO-on-TCP / S7comm）通道 | 已完成（真上位机那条还在人手上） |
| `plans/2026-10-13-packaging.md` | 打包成 exe（PyInstaller） | 已完成（真 UAC / 真上位机连冻结包留人工） |
| `plans/2026-10-13-new-project.md` | 新建工程与增删（通道 / 设备 / 点位） | 已完成；**注意**它的"口径 6（在跑时拒绝增删）"已被 `2026-10-13-review-round2.md` 改掉 |
| `plans/2026-10-13-s7-point-identity.md` | 点位身份键带"位"（同字节两个 S7 bool 不再互相覆盖） | 已完成 |
| `plans/2026-10-13-review-round2.md` | 第二轮深度审查的 12 条意见（本批） | 已完成（逐条有结论） |
| `plans/2026-10-14-import-export-formats.md` | 多种导入导出格式（CSV / Excel / project.json 收进一张表）+ 协议适配器的结论与「加一种协议要改哪 16 处」 | 已完成 |
| `plans/2026-10-14-deep-review-batch.md` | 深度审查那一批：13 条优化（原子保存、崩溃落盘、刷新性能、静态检查、设置、传输注册…）+ 2 项扩展（主站模式、三种新传输） | 已完成 |
| `plans/2026-10-16-type-coverage-and-net-purity.md` | mypy 覆盖扩到 `backend/`+`master/`（15 处全用真标注修，顺带逮到 `write_targets` 的 None 键隐患）+ `NetError` 挪成叶子模块让 `net` 的纯模块摘掉 ctypes/subprocess | 已完成 |
| `plans/2026-10-16-rename-events-deps.md` | P2/P4/P5：`ModbusBackendBase` → `BackendBase`（S7 也继承它）+ 事件契约挪到根上（gui 从此不碰 backend）+ GUI 的 7 个测试缝收成显式 `WindowDeps` | 已完成 |
| `plans/2026-10-15-architecture-tidy.md` | 结构整理第二轮：CI 两条门禁（ruff / mypy）转绿 + 通道四态收成一处 + 删四个死函数 + 图标从 theme 拆出 | 已完成 |
| `notes/2026-10-12-m5-rtu-probes.md` | M5 的前置实测（`socket://` 当虚拟串口那条路） | 实测记录 |
| `notes/2026-10-13-s7-probes.md` | S7 的前置实测（snap7 能当服务端、事件码布局、两套区代码） | 实测记录 |
| `notes/2026-10-14-modbus-poll.md` | **用 Modbus Poll 读这台模拟器**的 5 分钟清单（连接/显示设置、读不到时先看哪三处、空洞与 `--fill-gaps`） | 参照 |
| `notes/2026-10-13-manual-checklist.md` | **人工验收总表**（8 项，含 S7 与打包）——自动化替不了的那些 | 等人跑 |
| `notes/2026-10-13-gui-shots/` | 界面截图 14 张 + 重新渲染脚本 + 它照出来过哪两处排版缺陷 | 参照（随界面改动重渲染） |

读法建议：先看 `spec` 的 §11（里程碑总表）与 §2（范围），再按上表进对应的计划。
