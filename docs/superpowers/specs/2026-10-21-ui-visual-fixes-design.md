# 界面视觉修复（QC 批次 1）设计

日期：2026-10-21　｜　基线：`7422bd3`（v1.1.0 之后）　｜　范围：只修视觉缺陷与主题覆盖，不动布局

## 1. 目标

1. 深色主题下不再出现浅色控件残留。
2. 不可用控件"看得出不可用"（禁用态与可用态有明确差异）。
3. 通道行里非适用槽位有可读提示，不再像坏掉的输入框。
4. 上述两条在 **1366×768@100%** 与 **150% 缩放**两档下都成立。

**非目标**（明确不做）：不引入尺寸令牌体系、不重排任何布局、不改现有配色值、不动行高/列宽/间距、
不修改 `docs/superpowers/notes/2026-10-13-gui-shots/` 里的历史截图。

## 2. 已证实的缺陷（按基线实测）

| # | 缺陷 | 证据 |
|---|---|---|
| 1 | 深色下 `QComboBox` 是白底（核对页"目标"框） | 深色截图 + `theme.py` 的 QSS 无 `QComboBox` 选择器 |
| 2 | 深色下 `QSpinBox` 是白块（"5 秒"间隔） | 同上，无 `QSpinBox` 选择器 |
| 3 | 禁用态不可辨：`QRadioButton, QCheckBox { color: {ink} }` 无条件设色，盖掉 palette；且 `palette()` 未设 `Disabled` 色组，所以 TCP 行上的「保留/原始/遗嘱」看起来仍可点 | `theme.py` 的 QCheckBox 规则与 `palette()` |
| 4 | 通道行两个空槽（帧格式、前缀）只有 tooltip，肉眼看像失效输入框 | `channel_panel.py` 的"置灰不隐藏"注释（几何恒定的有意设计） |

**已撤回的两条误判**（不留待办）：深色表头/树头实测为 `#262f3a`，正确；状态栏"停止"对比度约 6:1，
达标。二者都是看缩略图得出的错误结论，以像素采样为准。

## 3. 方案

### 3.1 主题层（`simbench/gui/theme.py`）

补 QSS 选择器，规则只引用现有 palette 占位符，**不新增颜色字面量、不改现有取值**：

- `QComboBox`：背景 `surface`、边框 `line`、文字 `ink`、`padding` 与现有输入框一致；
  `QComboBox:disabled`、`QComboBox::drop-down`；弹层 `QComboBox QAbstractItemView`
  （背景 `surface`、选中项 `accent`、文字 `accent_text`）。
- `QSpinBox, QDoubleSpinBox`：背景/边框/文字/`padding` 同上；`:disabled`；不做 `::up-button`
  自绘（保留原生按钮，避免箭头丢失）。
- `QProgressBar`：背景 `band`、进度块 `accent`、文字 `ink`（`QProgressDialog` 内会用到）。
- 禁用态：`QCheckBox:disabled`、`QRadioButton:disabled`、`QLabel:disabled` → `ink_disabled`。
  同时把 `QCheckBox` / `QRadioButton` 的无条件 `color` 保持为 `ink`（可用态），禁用态由新规则覆盖。
- `palette()` 增加 `Disabled` 色组：`WindowText` / `Text` / `ButtonText` → `ink_disabled`，
  `Base` → `surface`（原生弹层与文件对话框的禁用项跟着一致）。

### 3.2 通道行（`simbench/gui/channel_panel.py`）

- 非适用槽位用 **`placeholderText`** 给一句短提示（帧格式槽在 TCP 行、前缀槽在非 MQTT 行），
  不写 `setText`：`.text()` 仍是空串，`apply_edits` 的校验路径与数据回写完全不受影响。
- 仍 `setEnabled(False)`、仍 `setFixedWidth(...)`；`geometry_snapshot()` 必须与改动前逐项相同。
- **实现时的判定**：先验证 Qt 在禁用态是否绘制 placeholder。
  - 绘制 → 采用 placeholder 方案（首选）。
  - 不绘制 → 改为写入可见文案，并在该槽读取处跳过非适用字段（校验与回写都按 transport 判定），
    仍然不动宽度与启用状态。
  两条路都不允许隐藏控件或改变列数。

### 3.3 防复发用例（`tests/test_gui_theme.py`）

1. `test_the_dark_theme_styles_every_field_control_the_gui_uses`：
   深色下实例化 `QLineEdit` / `QComboBox` / `QSpinBox`，`grab()` 后统计控件内部区域的平均亮度，
   必须低于阈值（浅色残留会显著拉高）；同时断言 `theme.qss("dark")` 含这三类的选择器，
   防止"颜色恰好对但规则被删"。
2. `test_disabled_controls_are_visibly_dimmer_than_enabled_ones`：
   同一文案的 `QCheckBox` 分别在启用/禁用下渲染，取"与背景亮度差 > 0.1"的像素求平均亮度，
   禁用必须明显更低；浅色与深色各跑一次。**样本像素少于 20 个直接判失败**——防止控件没画出来
   时用例变成空跑。
3. 通道行：TCP 行的帧格式/前缀槽存在非空提示且 `not isEnabled()`；
   现有 `geometry_snapshot()` 用例保持全绿即视为几何未变（不新增重复断言）。

阈值写成常量并在注释里写明"这是渲染判据，不是配色口径"，避免被当成设计规范。

### 3.4 双分辨率截图验收

给 `docs/superpowers/notes/2026-10-13-gui-shots/regenerate.py` 增加可选参数：

```
python .../regenerate.py --size 1366x768 --scale 1.0 --out D:\tmp\shots-1366
python .../regenerate.py --size 1280x720 --scale 1.5 --out D:\tmp\shots-150
```

- 默认（不带参数）行为完全不变，仍写入原目录——历史截图不被覆盖。
- `--scale` 通过 `QT_SCALE_FACTOR` 在创建 `QApplication` 之前设置；`--size` 只改 `window.resize`。
- 两组各出：主窗浅色、主窗深色、核对页深色、通道行局部。

## 4. 验收判据

1. 两组截图里：核对页的"目标"框与"5 秒"框在深色下为深底；TCP 行两个空槽有提示文字；
   非 MQTT 行的三个勾选框明显置灰；主窗本身不被裁切、报文页仍可见。
   点位表在 1366 宽下本来就需要横向滚动（列宽是按最长内容定的），这是既有布局，
   **本批不改列宽、也不把"无横向滚动"当验收项**。
2. 新增两条主题用例 + 一条通道行断言；现有 `test_gui_theme.py` / `test_gui_channel_panel.py` /
   `test_gui_layout_invariant.py` 全绿。
3. 门禁：`ruff check`、`python -m mypy`（50 文件）、全量 `pytest`（当前基线 1654 条）全绿。
4. 几何零变化：`geometry_snapshot()` 与 `test_gui_layout_invariant.py` 的断言不改、不放宽。

## 5. 风险与已知坑

- **QComboBox 箭头**：QSS 接管 `QComboBox` 后原生下拉箭头可能消失。首轮截图为准；若消失，
  只补 `::drop-down` 的尺寸与边框，不绘制自定义图片。
- **QSpinBox 按钮**：同理，不覆盖 `::up-button/::down-button`，保留原生。
- **placeholder 在禁用态是否绘制**：见 3.2 的判定，二选一在实现时定，不悬空。
- **渲染判据的稳定性**：亮度阈值在 Fusion + offscreen 下是确定的；若首轮实测波动超过阈值余量，
  把采样区域固定到控件中心 8×8 像素，而不是整块区域。

## 6. 交付物

- `simbench/gui/theme.py`、`simbench/gui/channel_panel.py`、`tests/test_gui_theme.py`、
  `docs/superpowers/notes/2026-10-13-gui-shots/regenerate.py` 四处改动。
- 两组截图在仓库外（`D:\tmp\`），不新增仓库文件。
- 不新增依赖、不改打包脚本、不改 CHANGELOG（属于视觉修复，用户可见变化记为"深色主题下控件不再漏白"，
  发版时再写）。

## 7. 待定项

无。两处"实现时二选一"（placeholder 是否绘制、箭头是否保留）都有明确判据与退路，不构成阻塞。
