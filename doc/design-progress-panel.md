# 进度面板组件化与阶段管理设计方案

## 一、现状分析与问题

### 1.1 当前进度系统架构

Runner 使用显式 `_enter_stage()` / `_skip_stage()` 进行阶段管理，通过 `StageProgress` 数据类 + Qt Signal 将进度快照从 Worker 传递到 GUI：

```
Runner.run()                              Worker.run()
    │                                         │
    ├─ _enter_stage("环境探测") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("域名收集") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("API发现") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("DNS 解析") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("ECS 对比") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("TCP 探测") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("ICMP 探测") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    ├─ _enter_stage("归属地") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
    └─ _enter_stage("评分") ──→ StageProgress → progress Signal → ProgressPanel.on_progress()
```

回调签名：`ProgressCb = Callable[[StageProgress], None]`

### 1.2 问题清单

| # | 问题 | 根因 | 影响 |
|---|------|------|------|
| 1 | **进度条跳跃** | 不同阶段 `total` 语义完全不同（1/1 → 5/250 → 0/100），`QProgressBar` 的 `maximum` 反复突变 | 用户看到进度条先满再空再满，无法感知整体进展 |
| 2 | **无阶段概念** | 回调只有平铺的 `phase` 字符串，"阶段切换" vs "同阶段内进度" 完全靠调用方自行判断 | GUI 和 CLI 各自实现阶段检测，逻辑分散且不可靠 |
| 3 | **无时间线** | 没有"哪些阶段已完成、哪些正在运行、哪些等待中"的视觉呈现 | 跑完之前用户不知道经过了什么步骤 |
| 4 | **时间预估严重失真** | `elapsed / done * remaining` 假设所有阶段速率相同，实际速率差异可达 1000 倍 | "剩余 2s" 实际还要等 5 分钟 |
| 5 | **main_window.py 臃肿** | 进度条构建、状态标签、时间预估、日志输出全部挤在 `main_window.py`（593 行） | 进度区域代码纠缠在主窗口逻辑中，不可单独测试、不可复用 |

### 1.3 各阶段实际耗时特征

| 阶段 | 总任务数 | 单任务耗时 | 总耗时（典型） | 速率特征 |
|------|----------|------------|----------------|----------|
| 环境探测 | 1 | ~3s | ~3s | 固定 |
| 域名收集 | 1 | <1ms | 瞬间 | 固定 |
| API 发现 | 1 | 0~15s | ~8s（有缓存时 0s） | 固定 |
| DNS 解析 | `服务器数 × 域名数` | ~0.5s/对 | ~2min（8×35 时） | 稳定 |
| ECS 对比 | `服务器数`（仅 CN 5 域名） | ~1s/台 | ~45s（8 台时） | 稳定 |
| TCP 探测 | `去重 IP 数` | ~0.5s/IP | ~1min（100 IP 时） | 稳定 |
| ICMP 探测 | `去重 IP 数` | ~1s/IP | ~2min（100 IP 时） | 稳定 |
| 归属地 | 1 | ~3s | ~3s | 固定 |
| 评分 | 1 | ~0.1s | 瞬间 | 固定 |

线性预估算法的致命问题：在"环境探测"阶段（1 个任务，done=0），`rate = 3s / 0 = ∞`，无法预估。在"TCP/ICMP 探测"阶段（100 个任务），前期 `rate ≈ 2s/个`，用它算 DNS 解析剩余完全错误。

---

## 二、Qt 组件化原则

### 2.1 官方推荐模式

Qt 的核心哲学是 **Widget 树 + Signals/Slots 通信**：

```
┌─ 自定义 QWidget 子类 ──────────────────────┐
│  ├─ 内部管理自己的 layout（自包含 UI）        │
│  ├─ 通过 Signal 向外通知状态变化             │
│  ├─ 通过 Slot / 公共方法接收外部指令          │
│  └─ 零耦合：不 import 父级窗口              │
└────────────────────────────────────────────┘
```

### 2.2 分离目标

将 `main_window.py` 中的进度相关代码（~110 行 UI 构建 + ~30 行逻辑处理）提取为独立的 `ProgressPanel(QWidget)`：

```
main_window.py (主窗口)               progress_panel.py (独立组件)
┌──────────────────────────┐        ┌──────────────────────────────┐
│ _build_ui() {             │        │ class ProgressPanel(QWidget): │
│   self.progress_panel =  │──实例化→│   _build_stage_header()       │
│     ProgressPanel()      │        │   _build_progress_bar()       │
│   ...                    │        │   _build_timeline()           │
│ }                        │        │   _build_eta()                │
│                          │        │                              │
│ start_run() {            │        │ @Slot(StageProgress)          │
│   progress_panel.start() │──调用→ │ on_progress(sp)               │
│   worker.start()         │        │ }                             │
│ }                        │        │                              │
│                          │        │ reset()                       │
│ worker.progress.connect→ │─Signal→│ start()                       │
│   panel.on_progress      │        │ set_stages([...])             │
└──────────────────────────┘        └──────────────────────────────┘
```

**增益：**

| | 分离前 | 分离后 |
|------|--------|--------|
| `main_window.py` 行数 | ~593 | ~480（-113 行） |
| 进度逻辑可测试性 | 需启动完整 GUI | `ProgressPanel()` 独立实例化即可 |
| 复用性 | 与 main_window 强绑定 | 可在其他地方独立使用 |
| 职责清晰度 | 主窗口管一切 | 主窗口只管编排，Panel 管进度 |
| 扩展性 | 改进度要改 main_window | 只改 progress_panel.py |

---

## 三、`StageProgress` 数据类

### 3.1 定义

```python
from dataclasses import dataclass

@dataclass
class StageProgress:
    """单个阶段进度快照，通过 Signal/Slot 从 Worker 传递到 GUI。"""
    stage: int          # 当前阶段序号（1-based）
    total_stages: int   # 总阶段数
    stage_name: str     # 阶段名，如 "DNS 解析"
    done: int           # 当前阶段内已完成数
    total: int          # 当前阶段内总数
    msg: str            # 描述文本
    status: str         # "running" | "done" | "skipped"
    elapsed_s: float    # 本阶段已耗时（秒）
```

### 3.2 相比旧回调的改进

| | 旧 `(phase, done, total, msg)` | 新 `StageProgress` |
|------|------|------|
| 阶段序号 | 无，靠字符串判断 | `stage` 明确是第几个阶段 |
| 总阶段数 | 无，调用方不知道 | `total_stages` 让 GUI 显示 "阶段 4/9" |
| 阶段切换 | 调用方自己检测 phase 变化 | Runner 自动发 `status="done"` 再发 `status="running"` |
| 阶段耗时 | 无法知道单阶段耗时 | `elapsed_s` 记录本阶段已用时间 |

---

## 四、`ProgressPanel` 组件设计

### 4.1 布局（紧凑模式）

实际实现为紧凑水平布局，总高约 55px，两行结构：

```
┌──────────────────────────────────────────────────────────────────┐
│  ProgressPanel                                                   │
│                                                                  │
│  阶段 4/9  DNS 解析  [████████████░░░░░░░░░░░░] 57% 144/250  ~6min│  ← 第1行：header + 进度条 + ETA
│  环境  域名   API   DNS   ECS   TCP   ICMP   归属  ⏱评分          │  ← 第2行：横排阶段徽章
│  [✓]   [✓]   [✓]  [●]   [ ]   [ ]   [  ]   [  ]   [  ]          │     绿色=完成 蓝色=运行中 灰色=等待
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 子组件

| 子组件 | 类型 | 职责 |
|--------|------|------|
| `_stage_header` | `QLabel` | 显示 "阶段 4/9 DNS 解析"，运行中加粗蓝色 |
| `_progress_bar` | `QProgressBar` | 当前阶段进度；`total<=1` 时显示不确定进度动画（循环滚动） |
| `_eta_label` | `QLabel` | 时间预估信息，如 "~6min" 或 "总耗时 2.1min" |
| `_badge_row` | `QHBoxLayout` | 横排阶段徽章，每个阶段一个 QLabel 圆角标签 |

### 4.3 阶段徽章状态

横排排列的阶段徽章（QLabel），每种状态有不同颜色：

| 状态 | 样式 | 示例 |
|------|------|------|
| 完成 (done) | 绿底绿字 | `环境` 绿色边框 |
| 运行中 (running) | 蓝底蓝字 | `DNS` 蓝色边框 |
| 等待中 (pending) | 灰色 | `TCP` 灰色文字 |
| 已跳过 (skipped) | 灰底删除线 | `API` 灰色+删除线 |

徽章缩写映射：环境探测→环境, 域名收集→域名, API发现→API, DNS解析→DNS, ECS对比→ECS, TCP探测→TCP, ICMP探测→ICMP, 归属地→归属, 评分→⏱评分

### 4.4 公开接口

```python
class ProgressPanel(QWidget):
    """进度与阶段时间线面板（紧凑模式）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        ...

    # ── 生命周期 ──
    def start(self) -> None:
        """显示面板，构建阶段徽章，开始计时。"""

    def reset(self) -> None:
        """恢复到初始状态并隐藏。"""

    # ── 阶段配置 ──
    def set_stages(self, stages: list[str], weights: dict[str, float] | None = None) -> None:
        """预设阶段列表和权重。weights 用于 ETA 计算，不传则用默认值。"""

    # ── 进度更新（Slot） ──
    @Slot(StageProgress)
    def on_progress(self, sp: StageProgress) -> None:
        """接收 Runner 发来的阶段进度快照。"""

    # ── 完成 ──
    def finish(self, success: bool = True) -> None:
        """标记所有阶段完成。"""
```

### 4.5 ETA 算法

**核心思路：不再对当前阶段内做不可靠的线性外推，而是用已完成阶段的平均速率来估算剩余阶段。**

```
已完成阶段总耗时 = Σ(已完成阶段的 elapsed_s)
已完成阶段总权重 = Σ(已完成阶段的 weight)

剩余阶段总权重 = Σ(剩余阶段的 weight)

预估剩余时间 = (已完成阶段总耗时 / 已完成阶段总权重) × 剩余阶段总权重
```

**各阶段权重配置**（基于实际测量经验，与代码 `_DEFAULT_WEIGHTS` 一致）：

| 阶段 | 权重 | 理由 |
|------|------|------|
| 环境探测 | 1 | 固定 ~3s |
| 域名收集 | 0 | <1ms，忽略 |
| API 发现 | 2 | 可缓存，0~15s |
| DNS 解析 | 8 | 主要瓶颈 |
| ECS 对比 | 3 | ≈8 台时 ~45s |
| TCP 探测 | 5 | 取决于去重 IP 数量 |
| ICMP 探测 | 7 | 取决于去重 IP 数量，线程池并发 |
| 归属地 | 1 | 固定 ~3s |
| 评分 | 0 | <0.1s，忽略 |

**冷启动问题**：前几个阶段还未完成时，无可参考速率。此时根据配置参数做静态估算：

```
静态预估 = 环境探测(3s) + API发现(10s) + DNS解析(2min) + ECS对比(1min) + IP探测(5min) + 归属地(3s) + 评分(0s)
         = 约 8min（默认配置下）
```

---

## 五、Runner 阶段追踪

### 5.1 有序阶段定义

```python
# runner.py —— 类级常量
_STAGES = [
    "环境探测",
    "域名收集",
    "API发现",     # 如果 api_discovery_enabled=False，标记为 skipped
    "DNS 解析",
    "ECS 对比",    # 如果 ecs_subnet=None，标记为 skipped
    "TCP 探测",
    "ICMP 探测",   # 如果 icmp_enabled=False 且 deep_traceroute=False，标记为 skipped
    "归属地",
    "评分",
]
```

### 5.2 阶段切换：显式调用

Runner 不使用自动检测，而是通过显式 `_enter_stage()` / `_skip_stage()` 管理阶段切换：

```python
class Runner:
    async def run(self) -> RunResult:
        self._enter_stage("环境探测")
        # ... 环境探测逻辑 ...

        self._enter_stage("域名收集")
        # ... 域名收集逻辑 ...

        if t.api_discovery_enabled:
            self._enter_stage("API发现")
            # ... API 发现逻辑 ...
        else:
            self._skip_stage("API发现")

        self._enter_stage("DNS 解析")
        # ... DNS 解析逻辑 ...

        # ... 后续阶段类似 ...
```

### 5.3 _enter_stage 实现

```python
def _enter_stage(self, name: str) -> None:
    if self._stage_idx >= 0:
        prev_name = _STAGES[self._stage_idx]
        if prev_name not in self._stage_skipped:
            duration = time.time() - self._stage_started
            self._emit(prev_name, 1, 1, f"完成 · 耗时 {duration:.1f}s", "done")
    self._stage_idx = _STAGE_INDEX.get(name, 0)
    self._stage_started = time.time()
```

`_skip_stage()` 内部调用 `_enter_stage()` 后立即发 `status="skipped"`，标记跳过原因。

### 5.4 回调签名（已实现）

使用 `StageProgress` 数据类通过 Qt Signal 传递：

```python
# worker.py
progress = Signal(object)  # StageProgress
```

---

## 六、影响文件与改动范围

### 6.1 文件变更清单

| 文件 | 操作 | 改动量 | 说明 |
|------|------|--------|------|
| `src/dnspick/gui/progress_panel.py` | **已实现** | ~250 行 | ProgressPanel 组件（紧凑横排徽章模式） |
| `src/dnspick/runner.py` | **已实现** | +50 / −20 | 定义 `_STAGES`（9阶段）、显式 `_enter_stage`/`_skip_stage` |
| `src/dnspick/gui/main_window.py` | **已实现** | −110 / +20 | 删除进度 UI 构建代码，替换为 `ProgressPanel` 实例 |
| `src/dnspick/gui/worker.py` | **已实现** | +5 / −5 | Signal 签名改为 `Signal(object)` |
| `src/dnspick/models.py` | **已实现** | +15 | 新增 `StageProgress` 数据类 |

### 6.2 不改动的部分

| 组件 | 说明 |
|------|------|
| `config.py` | 与进度显示无关 |
| `domain_store.py` | 与进度显示无关 |
| `resolver.py` / `ipprobe.py` | 内部回调签名不变（仍是 `(done, total, msg)`），Runner 内部适配 |
| `scorer.py` / `geoip.py` | 与进度显示无关 |
| `history.py` / `settings.py` / `domain_manager.py` | 与进度显示无关 |
| `store.py` | 与进度显示无关 |

### 6.3 测试影响

| 影响 | 说明 |
|------|------|
| 现有测试 | `runner.py` 的 `emit` 方法变化，但测试不直接验证进度回调（纯 UI 逻辑），现有 105 个测试不受影响 |
| 新增测试 | `ProgressPanel` 可独立实例化测试：设置假阶段、发假 `StageProgress`、验证时间线更新和 ETA 计算 |

---

## 七、实施状态

所有步骤已完成实现。

| 步骤 | 内容 | 状态 |
|------|------|------|
| **1** | `models.py` 新增 `StageProgress` 数据类 | ✓ 已实现 |
| **2** | `runner.py` 定义 `_STAGES`（9阶段），显式 `_enter_stage`/`_skip_stage` | ✓ 已实现 |
| **3** | `worker.py` Signal 签名改为 `Signal(object)` | ✓ 已实现 |
| **4** | 新增 `progress_panel.py`，实现紧凑模式 `ProgressPanel` | ✓ 已实现 |
| **5** | `main_window.py` 删除旧进度代码，接入 `ProgressPanel` | ✓ 已实现 |