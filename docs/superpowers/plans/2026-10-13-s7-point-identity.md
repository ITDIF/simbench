# S7 同字节 bool 点的身份键（彻底修）实现计划

日期：2026-10-13　｜　范围：把"点位身份"从 `(通道, unit, 区, 地址)` 扩成
`(通道, unit, 区, 地址, 位)`，让 S7 在**同一个字节上放两个 bool 点**时不再互相覆盖。
前置：M1–M6、S7、打包、新建工程全部完成；本计划只动身份键与归属键，**不改任何判定**。

> 这条是"新建工程"那一轮全面审查里**唯一明确留着没动**的缺陷（那份计划"审查里没动的"表
> 第一行）。用户口径：全修。

---

## 1. 缺陷是什么（实测，不是推测）

S7 的位是地址的一部分：`DBX6.2` 与 `DBX6.3` 同占一个字节，判定（`validate._s7_overlap`）
**按位**判、明确允许这么写——点表里一个字节塞几个 bool 是 S7 的常规用法。但身份键只到"字节"：

| 位置 | 键 | 后果 |
|---|---|---|
| `Session._points`（写值的落点） | `(通道, unit, 区, 地址)` | 后注册的那个把前一个**覆盖**掉：界面双击 `DBX6.2`，写进去的是 `DBX6.3` 那个点 |
| `Row.key`（界面身份，交给恢复/属性/删点） | 同上（四元组） | 属性面板打开的是"另一个点" |
| `SourceStore._manual`（值归属） | `(通道, unit, 区, 地址)` | 两个点共享一个归属：写一个，两个都显示"已停" |
| `restore_waveform` | 由前两条推出来 | 恢复的也可能不是右键那一个 |

复现（一条命令，不用界面）：一张 `db1` 上 `6.2` 与 `6.3` 两个 bool 的点表，`session.set_point`
其中一个 → 读回来是另一个被改了。这是**主要工作流**（现场手写 CSV）就能撞上的数据错位级缺陷。

---

## 2. 执行前先定的口径

| # | 要定的事 | 结论 | 备选（为什么不） |
|---|---|---|---|
| 1 | 身份键长什么样 | **统一五元组** `(通道, unit, 区, 地址, 位)`，`位` 只有 S7 的 bool 点非 `None`。Modbus 的位区不用它——那里的"位"**就是** `address`（一个位一个地址） | 元组长度按传输变（4 或 5）：调用方得先判传输才能拆包，界面那条 `channel, unit, area, address = key` 立刻碎 |
| 2 | 归属键要不要一起改 | **要**。spec §7 写的是"归属的单位是**点位**"；两个同字节的 bool 是两个点，共享一个归属就是"改一个停两个" | 只改写值不动归属：界面改一个点，另一个显示"已停"且它的波形真的停了——半个修复比不修更难查 |
| 3 | 签名怎么扩 | 新参数一律**加在末尾、带默认 `None`**（`bit: int \| None = None`） | 插在中间：既有 30 多处调用（Modbus 那些）要全体改一遍，改动面白扩大 |
| 4 | S7 事件只给**字节**范围怎么办 | **照旧标整段**（同字节里另一个 bool 也会被标成 manual）：snap7 的事件只到字节，不知道主站动的是哪一位；"宁可多标一个"是既有的 S7 口径，一次"恢复波形"就回来 | 拿缓冲内容去猜动了哪一位：那是另一套判定，且猜错就是"该停的没停" |
| 5 | `edit.find_point`/`remove_point` | 按 `(区, 地址, 位)` **精确匹配**，那条"同地址有多个点、分不出来"的拒绝删掉 | 保留拒绝：那是本缺陷的止痛片，不是修复 |
| 6 | `Row` 新字段放哪 | 放在**最后一位**且带默认值（`bit: int \| None = None`）：`Row` 的字段序是位置参数契约（`FakeEngine` 与一批用例按位置构造） | 插在中间：既有的位置构造会静默错位（本项目最防的那类"看着合法的错值"） |
| 7 | 要不要顺手把"同字节两个 bool"从**警告**提成**拒绝** | **不提**。理由见 §1：那是 S7 的常规用法，`validate` 的按位重叠是**有意**判的（`_s7_overlap` 的注释写着"套 Modbus 那套会报成重叠，现场就得把点位挪到不同字节去，那是假约束"） | 提成拒绝 = 把一条现场用法判死，且要推翻一条已实测的裁决 |

---

## 3. 任务

一段做完，分五步落地（每步之后全量跑一遍）：

### Task 1　归属层（`backend/ownership.py`）
`SourceStore` 的四个方法（`own`/`is_waveform`/`restore`/`cleared` 的键）都加 `bit`，
仍是"地址 + 偏移"的集合，`bit` 只作用在基础地址那一个（bool 的跨度恒是 1 位）。

### Task 2　后端转发（`backend/modbus_base.py`、`backend/s7_server.py`）
`mark_manual` / `restore_waveform` / `source_is_waveform` 都加 `bit=None`；
`_own_intersecting` 把点自己的 `pt.bit` 交下去（Modbus 的点恒是 `None`）。

### Task 3　会话层（`engine/session.py`）
`Row` 加 `bit` 并进 `key`；`_points` 的桶键加 `bit`；`rows()`/`manual_count()` 用 `pt.bit`；
`set_point`/`write`/`restore_waveform` 加 `bit=None`；`_source_of`/`_awrite_s7`/`_arestore` 一路带上。

### Task 4　界面与编辑层
`point_table` 写值时带 `row.bit`；`main_window` 拆五元组（恢复/删点/加完属性面板）；
`point_props` 的身份键带位；`edit.find_point`/`remove_point` 精确到位。

### Task 5　用例与文档
新增"同字节两个 bool 互不干扰"的一组用例（写值、归属、恢复、属性面板、删点）；
既有那些把身份写成四元组的地方逐条改（机械改动，逐字记进偏离表）；spec §7/§12 与
那份计划"审查里没动的"表回写；重新打包 + 冻结冒烟。

---

## 4. 验收表

| # | 场景 | 期望 | 归属用例 |
|---|---|---|---|
| 1 | 同字节两个 bool 点，`set_point` 其中一个 | 只有那一个变，另一个一个字不动 | `test_s7_session.py` |
| 2 | 同上，主站写 `DBX6.2` | 该点的 `source` 变 `manual`；另一个也标 manual（口径 4，字节粒度），但**值**不变 | 同上 |
| 3 | 同上，`restore_waveform(…, bit=3)` | 只交还那一个；另一个仍 manual | 同上 |
| 4 | 界面双击其中一行 | 写的是**那一行**（按 `Row.key` 的五元组定位） | `test_gui_point_props.py` / `test_gui_point_table.py` |
| 5 | 属性面板打开其中一行 | 改的是那一行（不是同位邻居） | `test_gui_point_props.py` |
| 6 | 右键"删除点位" | 删的是那一行；不再出现"分不出来"的拒绝 | `test_gui_new_project.py` / `test_edit.py` |
| 7 | Modbus 的既有行为 | 一条断言不改（四元组的地方只多一个 `None`，其余全绿） | 全量 |

## 5. 执行结果（实测，2026-10-13）

- **全量串行 pytest：1329 → 1334 passed**（新增 3 条真主站用例 + 1 条面板用例；另有 1 条
  旧用例按新语义重写、2 条字段序契约用例按新尾巴改写）。无 skip / xfail。
- **真上位机那一条是硬证据**：`test_two_bools_in_one_byte_are_two_identities` 让 `pys7`
  读 `DB1,X6.2` 与 `DB1,X6.3`，断言 `[False, True]`——即界面改的是**那一位**。
- 动的文件：`backend/ownership.py`、`backend/modbus_base.py`、`engine/session.py`、
  `engine/point_source.py`、`gui/point_table.py`、`gui/main_window.py`、`gui/point_props.py`、
  `project/edit.py`（生产 8 个）+ 6 个测试文件。
- 重新打包后 `packaging/smoke.py` **11/11** 不回归。

## 完成检查表

- [x] Task 1–5 全部落地；新增用例覆盖上面七行
- [x] Modbus 那 30 多处**调用点**（`set_point`/`write`/`restore_waveform`）一个字符没改
      （新参数在末尾带默认值）——改的只有测试里的**身份字面量**（见偏离 D2）
- [x] 全量串行 `.venv\Scripts\python.exe -m pytest` 全绿：**1329 → 1334**，只增不减
- [x] spec §7 的"归属单位是点位"补一句"键里带位"，§12 记一条实测（含真上位机那条）；
      "审查里没动的"表把第一行划掉（改成"→ 已修"）
- [x] 重新打包 + `packaging/smoke.py` 11 项不回归
- [x] `git status --short` 为空

## 执行期已裁决的偏离

| # | 原计划写的 | 实际做的 | 为什么 |
|---|---|---|---|
| D1 | 没写 | `Row` 的字段序两条契约用例改名并改写：`test_row_field_order_…` 的期望元组补 `bit`；`test_row_source_is_a_trailing_default_…` 改成 `test_the_fields_added_later_all_sit_at_the_tail_with_defaults` | 契约本身没变松（**新字段只许往后加、且必须带默认值**），变的是"尾巴是哪三个"。不许为了让旧断言过而把 `bit` 插到中间——那正是它要防的静默串列 |
| D2 | "Modbus 那些调用一个字符不改"（只提了生产代码） | 测试里的**身份字面量**约 20 处补了 `None`，替身 `FakeEngine.writes` 从五元组变六元组 | 身份契约变了，断言必须跟着变——这是显式改动，不是放松断言（新加的 `bit` 让替身也能验"写错那一行"）。替身的 `values` 注入表**仍按四元组**：它只伺候 Modbus 的行，动它没有收益 |
| D3 | 没写 | 生产代码里漏改的一处五元组拆包（`main_window._add_point`）在跑全量时被两条用例照出来 | 记在这里是为了说明"先跑再交"这条纪律的价值：那一处漏改的表现是 Qt 槽里抛 `ValueError`（界面"点了没反应"），靠断言抓不住，靠**跑**才看得见 |

## 不做

| 内容 | 为什么 |
|---|---|
| 按"主站动的是哪一位"精确标接管 | snap7 的事件只给字节范围（实测），要精确只能拿缓冲内容反推，那是另一套判定且猜错方向更糟 |
| 把"同字节两个 bool"提成校验错误 | 那是 S7 的常规用法，推翻它要连 `_s7_overlap` 的按位裁决一起推翻 |
| 顺带把 Modbus 的 `Row.key` 里那个 `unit_id` 换成设备名 | 与本次缺陷无关，动它要改一批既有用例 |