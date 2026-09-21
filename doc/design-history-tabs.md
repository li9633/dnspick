# HistoryWindow Tab 化重构 —— 实现文档

## 概述

`HistoryWindow` 采用 Tab 多页布局（结果总览 / 分组对比 / 分析建议），筛选栏精简为紧凑单行多选下拉，左侧历史列表使用自定义卡片。

源码：[src/dnspick/gui/history.py](../src/dnspick/gui/history.py)

## 整体布局

```
┌──────────────────────────────────────────────────────────────────┐
│ 工具栏                                                           │
│ [共 N 条记录]  ────────  [导出JSON] [SmartDNS] [删除] [✕ 关闭]  │
├──────────┬───────────────────────────────────────────────────────┤
│          │ [协议▾] [地区▾] [结论▾] [标签▾] [厂商▾] 分数 0━100 [重置]│
│ 历史列表  ├───────────────────────────────────────────────────────┤
│ (200px)  │ ┌─ 结果总览 ─┬─ 分组对比 ─┬─ 分析建议 ─┐            │
│ ┌──────┐ │ │ ★推荐3 · ○备用5 · △限定1 · ✗不推荐2      │            │
│ │09-20 │ │ │────────────────────────────────────────│            │
│ │default│ │ │ # │名称│地址│协议│综合分│结论│...       │            │
│ │21台   │ │ │ 1 │Ali │1.1 │UDP │95.3 │推荐│...      │            │
│ │1149域 │ │ │ 2 │114 │2.2 │DoH │92.1 │推荐│...      │            │
│ │▸AliDNS│ │ │                                      │            │
│ │────── │ │ │            (右键: 复制地址/复制SD行)    │            │
│ │09-19  │ │ │                                      │            │
│ │gaming │ │ │                                      │            │
│ └──────┘ │ └──────────────────────────────────────┘            │
└──────────┴───────────────────────────────────────────────────────┘
```

## 组件映射

| 旧组件 | 新实现 | 说明 |
|--------|--------|------|
| `_build_filter_panel` (3行 GroupBox + QCheckBox) | `_MultiSelectCombo` × 5 + QSlider | 紧凑单行，QPushButton+QMenu 多选 |
| `_build_vendor_cards` | **删除** | 内容合并到 Tab 3 分析建议 |
| `_build_result_view` (QStackWidget) | Tab 1 + Tab 2 | 分别承载表格和树，不再切换 |
| `_build_advice_panel` (QTextEdit fixed 200px) | Tab 3 QScrollArea + QFrame 卡片 | 不再固定高度 |
| `_view_mode` QComboBox | **删除** | Tab 本身就是视图切换 |
| 历史列表纯文本 | `_HistoryCard` 自定义卡片 | 3行结构化 + 底部分隔线 |

## 新增内部组件

### `_MultiSelectCombo`

紧凑多选下拉，QPushButton + QMenu with checkable QAction。

- 按钮文字："全部" / "无" / "A,B" / "N项"
- `get_checked()` → `list[str]` 获取选中值
- `set_all_checked()` 全选
- `rebuild(options)` 动态重建选项（厂商下拉用）
- `filter_changed` 信号 → 连锁触发筛选

### `_HistoryCard`

QFrame 子类，作为 `QListWidget.setItemWidget()` 的自定义条目。

- 固定高度 56px
- 3 行：日期+profile / 统计 / 最快服务器
- 底部 `border-bottom: 1px solid #3a3a3a` 分隔
- hover 效果 `background: #2d2d2d`
- QListWidget 选中态 `background: #2d4a6d`

## Tab 详细设计

### Tab 1: 结果总览

```
┌─────────────────────────────────────────────────┐
│  ★ 推荐 3 台 · ○ 备用 5 台 · △ 限定 1 台 · ✗ 不推荐 2 台 │  ← stats QLabel (HTML)
│─────────────────────────────────────────────────│
│  # │名称│ 地址  │协议│综合分│国内延迟│结论│...   │
│  1 │Ali │1.2.3 │UDP │ 95.3 │12.5ms │推荐│...   │  ← QTableWidget (可排序)
│                                      │
│                   右键菜单:                      │
│                   ├ 复制地址                     │
│                   └ 复制 SmartDNS 行             │
└─────────────────────────────────────────────────┘
```

- **QLabel 统计条**：HTML 格式，彩色 verdict 计数
- **QTableWidget**：16 列，可排序，最后一列 stretch
- **右键菜单**：复制地址 / 复制 SmartDNS 行（`server=1.2.3.4#udp`）

### Tab 2: 分组对比

```
┌─────────────────────────────────────────────────┐
│  分组方式: [厂商→协议 ▾]    [展开全部] [折叠全部] │
│─────────────────────────────────────────────────│
│  ▼ AliDNS                                        │
│    ├ UDP ── 推荐  95.3分  12.5ms  3跳            │
│    ├ DoH ── 推荐  92.1分  15.2ms  4跳            │
│    └ DoT ── 备用  88.7分  18.1ms  5跳            │
│  ▼ 114DNS                                        │
│    └ DoH ── 推荐  92.1分  15.2ms  4跳            │
└─────────────────────────────────────────────────┘
```

- **QComboBox 分组维度**：厂商→协议 / 协议→地区 / 厂商→结论 / 协议→厂商
- **展开全部 / 折叠全部** 按钮
- **QTreeWidget** 7 列：名称、协议、综合分、国内延迟、国外延迟、跳数、结论
- 一级节点 bold，二级节点 italic
- 综合分和结论列有颜色标记
- 分组维度切换时自动触发 `_apply_filter()`

### Tab 3: 分析建议

```
┌─────────────────────────────────────────────────┐
│  ┌──────────────────────────────────────────┐   │
│  │ ★ 推荐 (3)                                │   │
│  │ AliDNS [UDP] 综合分 95.3 · 国内 12.5ms   │   │
│  │ 114DNS [DoH] 综合分 92.1 · 国内 15.2ms   │   │
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │ ○ 备用 (5)                                │   │
│  │ ...                                       │   │
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │ 📊 同厂商协议对比                          │   │
│  │ AliDNS: UDP 95.3分 · DoH 92.1分 · ...     │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

- **QScrollArea** 包裹，内容超出可滚动
- **分类卡片**：推荐/备用/限定/不推荐，每类一个 QFrame（`background: #252525; border-radius: 6px`）
- **同厂商对比卡片**：仅当有 ≥2 个协议的厂商时显示
- 无数据时显示占位文字

## 数据流

```
                    ┌─────────────────────┐
                    │   筛选栏 (全局)       │
                    │  _MultiSelectCombo×5 │
                    │  分数区间 QSlider     │
                    └──────┬──────────────┘
                           │ _build_filter() → ScoreFilter
                           │ 信号 → _on_filter_changed → _apply_filter
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │  Tab 1   │    │  Tab 2   │    │  Tab 3   │
    │ 结果总览  │    │ 分组对比  │    │ 分析建议  │
    │_show_flat│    │_show_grd │    │_show_adv │
    └──────────┘    └──────────┘    └──────────┘
          │                │                │
          └────────────────┴────────────────┘
                           │
            共用 self._scores (缓存)
            self._current_mid
```

- 筛选条件变化（多选下拉 toggle）→ `_on_filter_changed()` → `_apply_filter()`
- 分组维度变化 → `_on_group_changed()` → `_apply_filter()`
- 重置按钮 → `_reset_filter()` → 全选 + `_apply_filter()`
- 分数滑块仅更新标签文字，不自动触发筛选
- Tab 切换时不做任何操作
- `_apply_filter()` 同时刷新所有三个 Tab（共用 `self._scores`）

## 状态持久化

`QSettings` 保存/恢复：

| Key | 类型 | 说明 |
|-----|------|------|
| `history_window/geometry` | QByteArray | 窗口几何 |
| `history_window/current_tab` | int | 当前 Tab 索引 |
| `history_window/group_mode` | tuple | 分组维度 (group_by, sub_by) |

注：筛选条件不再持久化（原方案中筛选条件的保存逻辑已删除，每次打开窗口筛选全部重置为全选）。

## 导出功能

### 导出 JSON

当前选中测量的所有得分（`self._scores`）→ `dnspick-result.json`。

### 导出 SmartDNS

遍历 `self._scores`，生成 `smartdns-servers.conf`：
- 按 `地址+协议` 去重
- 协议映射：`udp→udp, tcp→tcp, dot→tls, doh→https`
- 命名规则：`{vendor}_{proto}` 或 `{server_name}`

## 文件结构

```
history.py
├── 常量: _VERDICT_COLOR, _VERDICT_TEXT, _ALL_*, _FLAT_COLS
├── _MultiSelectCombo(QWidget)     # 紧凑多选下拉
├── _HistoryCard(QFrame)           # 自定义列表卡片
└── HistoryWindow(QDialog)
    ├── __init__                   # 主布局
    ├── _build_left_panel          # 左侧历史列表
    ├── _build_filter_bar          # 筛选栏（单行）
    ├── _build_tabs                # QTabWidget 创建
    │   ├── _build_overview_tab    # Tab 1: 统计条 + 表格 + 右键菜单
    │   ├── _build_compare_tab     # Tab 2: 分组维度 + 展开/折叠 + 树
    │   └── _build_advice_tab      # Tab 3: QScrollArea + 卡片
    ├── 状态: _restore_state, _save_state, closeEvent
    ├── 筛选: _on_filter_changed, _on_group_changed, _on_score_changed
    │         _build_filter, _reset_filter, _refresh_vendor_filter
    ├── 列表: _refresh_list, _select_id, _on_select_entry
    ├── 刷新: _apply_filter
    │   ├── _show_flat             # Tab 1 数据填充
    │   ├── _on_table_context_menu # 右键菜单
    │   ├── _copy_to_clipboard     # 剪贴板
    │   ├── _show_grouped          # Tab 2 数据填充
    │   ├── _expand_all / _collapse_all
    │   ├── _clear_advice / _add_advice_card / _show_advice  # Tab 3
    └── 管理: _export_json, _export_smartdns, _delete_entry
```