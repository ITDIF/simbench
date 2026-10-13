# 第二轮深度审查的 12 条意见：实现计划

日期：2026-10-13　｜　范围：把第二轮深度审查提出的 12 条全部落地。
前置：M1–M6、S7、打包、新建工程、身份键全部完成；全量 1338 passed。

> 用户口径：**全部做**。三条一批，每批自己可验收、单独提交。

---

## 1. 每条的证据（都在本机跑过，不是推测）

| # | 意见 | 证据 |
|---|---|---|
| 1 | TCP 端口（与 host）在 CSV 往返里会变 | 通道 `网关-1`（15020）导出再导入 → 端口变成 **1**，只有一条 note |
| 2 | 界面建不出 S7 的 bool 点位 | `default_point(s7)` 给 `word`；`PointPropertiesDialog` 没有"位"这一格；改成 bool 被 `validate` 挡回 "bool points need a bit" |
| 3 | 点位表里看不出 `DBX6.2` 与 `DBX6.3` | 两行的"地址"列都是 `6`，整行一模一样 |
| 4 | "保存工程"永远弹另存为 | `_save_project` 无条件调 `save_project_as` |
| 5 | 日志窗无上限 | `_build_log_view` 没有 `setMaximumBlockCount` |
| 6 | 有通道在跑时不许增删 | `Session` 只有 `load()` 一条换工程入口，它第一步 `_stop_all_locked()` |
| 7 | 回读是跨线程直读 | `rows()` 的跨线程调用次数实测 **0**（`_read_back` 直接读后端缓冲），而 `ownership.py` 写着"运行态只在 loop 线程读写" |
| 8 | 文档没有索引 | `docs/superpowers/` 下 6 份计划 + 6 份 note，没有 README |
| 9 | 依赖文件混装 | `requirements.txt` 里同时有运行时 / pytest / 验收主站 `pys7`，而打包要的 `pyinstaller` 没有 |
| 10 | exe 没有图标与版本信息 | spec 里 `icon` 与 `version` 都没给 |
| 11 | 提权路的临时文件用固定名 | `%TEMP%\simbench_net_job.json`、结果默认落 CWD |
| 12 | 默认端口不探系统占用 | `next_port` 只避开**工程内**冲突；5020 被别人占着时第一次启动就是一条"端口被占" |

---

## 2. 执行前要定的口径

| # | 要定的事 | 结论 | 备选（为什么不） |
|---|---|---|---|
| A | TCP 在 CSV 里怎么带上 host/port | **`name@tcp:host:port`**（`@` 段里用 `tcp:` 前缀）。导入侧：`@tcp:` → TCP；没有 `@` → 仍按名字尾数猜（旧点表零影响）；`@` + `://` → 串口；`@` + `:` → S7 | 原提议的 `name@host:port` **不能用**：那个形状已经是 S7 的（`s7-1@192.168.0.10:102`），加进来就是二义（见偏离 D1）。加第 13 列：动了"s！12 列是合同"那条，且 S7/RTU 的信息本来就在名字里，多一列只有 TCP 用得上 |
| B | 导出时**总是**写 `@tcp:` 还是只在必要时写 | **总是写**：那套"名字尾数"的隐式约定正是这条缺陷的根，留着它就等于把脚枪继续上膛（用户改个名就静默换端口）。代价是导出的 TCP 名字变长、几条断言要改——显式换静默，值 | 只在"推不出来"时写：文件干净，但两种形态要各自维护、测试与文档 |
| C | S7 的 bool 怎么在界面里建 | 面板加一格**位**（只在"通道是 S7 且类型是 bool"时启用，其余置灰不隐藏），进 `STRUCTURAL_FIELDS`；"添加点位"仍只给默认类型（`word`/`uint16`），用户接着在面板里把它改成 bool 并填位 | 新建时弹完整表单：那份表单 `point_props` 已经有一份，抄第二遍必然漂移 |
| D | "保存"与"另存为"的家 | **加一个"文件"菜单**（新建 / 打开 / 导入CSV / 保存 / 另存为 / 导出CSV），并给 Ctrl+N/O/S/Shift+S。"保存"在**有路径时直接写回**（状态栏说一句），没路径才弹 | 再加一个工具栏按钮：工具栏已经 9 个动作，且每个都要配图标与定宽槽的断言 |
| E | 回读要不要收成一次跨线程调用 | **收**（口径 7 的 (a)）：`rows()` 在跑着时**一次** `runner.call` 取整批快照（寄存器 + 归属），把"运行态只在 loop 线程读写"这条规矩真正闭合；超时或失败 → 整批标 `stale`（不抛进 GUI 心跳） | 保持直读 + 写进文档：零代价，但"文档说一套、代码做一套"，而且多寄存器值有一帧撕裂 |
| F | 增量加/删通道的语义 | `Session.add_channel(ch)` / `remove_channel(name)`：只动那一路的 runtime 并重建索引；删一个**在跑**的通道先停它（停不掉就拒绝并给理由），**别的通道一律不碰** | 继续用 `load()` + 拒绝：一次点击就能解掉的限制，不值得留 |

---

## 3. 三批任务

### 第一批（界面与 CSV 层，P1：1–5）

1. **CSV 的 TCP 通道带上 host:port**：`csv_io` 的导出/导入两条路 + `validate` 的 `@` 判定按传输分开 +
   `examples/covi-tunnel.csv` 换新写法；往返用例（含旧 12 列写法仍能读、且仍给"猜了要说"的 note）。
2. **面板的"位"格**：`point_props` 加一格 + 地址格接受 `6.3` + `STRUCTURAL_FIELDS` 加 `bit`。
3. **点位表显示 `6.3`**：`_display` 的"地址"列按 CSV 写法打。
4. **文件菜单 + 保存/另存为**：`dialogs.save_project_to()`（按后缀分派）+ 主窗两个动作 + 快捷键。
5. **日志窗上限**：`setMaximumBlockCount(5000)` + 一条断言。

### 第二批（工程规范，P2：7、9、10、11）

7. **回读快照**：`Session._snapshot(...)` 在 loop 线程上取整批（寄存器 + 归属），`rows()`/`manual_count()` 走它。
9. **依赖拆分**：`requirements.txt`（运行时）/ `requirements-dev.txt`（pytest、pys7、pyinstaller），README 与 `build.ps1` 指后者。
10. **exe 图标与版本**：`packaging/make_icon.py`（用 `theme.app_icon()` 的画法生成多尺寸 `.ico`）+ spec 的 `icon=`/`version=`。
11. **提权临时文件唯一化**：job/结果文件带 pid 与 token，结果默认落临时目录。

### 第三批（结构与文档，P2/P3：6、8、12）

6. **增量加/删通道**：`Session.add_channel` / `remove_channel`；主窗的加/删通道改走它（在跑也能加）。
8. **文档索引**：`docs/superpowers/README.md`（13 份文档一张表）。
12. **建通道时探一次端口占用**：探测放在 `engine` 层（GUI 不许直接 import `backend`，方向是 `gui → engine → backend`），
    被占就顺延并在对话框的提示行说明。

---

## 4. 验收表

| # | 场景 | 期望 | 归属 |
|---|---|---|---|
| 1 | 自定义名字的 TCP 通道导出再导入 | 名字、host、port 三者逐字一致；没有"猜端口"的 note | `test_csv_io.py` |
| 2 | 旧写法（12 列、名字尾数）导入 | 与今天完全一样（含 note） | 同上 |
| 3 | 界面把 S7 点位改成 bool 并填位 | 落地成功；导出 CSV 是 `6.3` | `test_gui_point_props.py` |
| 4 | 点位表看同字节两个 bool | 地址列分别是 `6.2` / `6.3` | `test_gui_s7.py` |
| 5 | 有路径时点"保存" | 直接写回、状态栏一句"已保存"，不再弹对话框 | `test_gui_main_window.py` |
| 6 | 文件菜单 | 六个动作在、快捷键对、"另存为"总能弹 | 同上 |
| 7 | 日志窗 | 超过 5000 行后仍只保留最近 5000 行 | 同上 |
| 8 | `rows()` 的跨线程调用 | 跑着时**一次**；没跑时 0 次；超时 → 整批 stale | `test_session.py` |
| 9 | 依赖 | `pip install -r requirements-dev.txt` 能装齐 pyinstaller/pys7 | README（人工一步） |
| 10 | 打包产物 | exe 有自定义图标与版本资源 | `packaging/SMOKE.md` |
| 11 | 提权 | job 文件唯一；结果默认在临时目录 | `test_net_state.py` / `tests/test_frozen_entry.py` |
| 12 | 在跑的通道上加/删**另一路** | 允许；在跑的那一路不受影响 | `test_gui_new_project.py` |
| 13 | 文档索引 | 13 份文档都在表里 | 人工 |
| 14 | 端口被占时新建工程 | 自动顺延 + 提示里说明 | `test_gui_new_project.py` |

## 完成检查表

- [ ] 三批全部落地，验收表逐行有结论
- [ ] 既有行为里**必须变的**（CSV 的 TCP 写法、保存按钮语义）逐条进偏离表
- [ ] 全量串行 `.venv\Scripts\python.exe -m pytest` 全绿，只增不减
- [ ] README / spec / 人工清单按需回写；截图组重渲染
- [ ] 重新打包 + `packaging/smoke.py` 不回归
- [ ] `git status --short` 为空

## 不做

| 内容 | 为什么 |
|---|---|
| 撤销/重做 | 与"关窗问一次"是两套记忆模型，单独一个里程碑 |
| 报文导出/录制 | v2 之外（spec §2 已写明），接口留 `PacketStore` |
| 通道/设备重命名 | 要动会话的身份键，删了重建更省（见新建工程计划的"不做"表） |
| 线程化回读做成异步 | 10Hz 一次往返、实测 200 点整轮 2.2ms，同步足够；异步要把 `rows()` 改成"上一帧快照"，那是另一个形状 |