# 运行配置与历史分析 —— 窗口职责重构设计方案

## 概述

将当前 `MainWindow`（主窗口）和 `HistoryWindow`（历史窗口）的职责重新划分，
使每个窗口只承担单一职责。同时增强历史分析窗口的"厂商 × 协议"对比推荐能力。

## 一、当前状态分析

### 1.1 当前窗口职责

| 窗口 | 当前职责 | 问题 |
|------|---------|------|
| **MainWindow** | DNS 服务器完整表格（启停 + 全选/协议筛选） + 域名摘要 + 上次测量 + 控制栏 + 进度/日志 + 入口按钮 | 承担了"服务器管理"和"运行配置"双重职责，主窗口太重 |
| **HistoryWindow** | 历史列表 + 多维度筛选 + 平铺/分组视图 + 建议生成 + 导出/删除 | 缺少"厂商 × 协议"维度的结构化推荐 |
| **SettingsDialog** | 服务器启停 + 参数调优 + 权重配置 | 功能完备，但服务器管理能力与 MainWindow 重复 |

### 1.2 当前数据模型

`DnsServer` 字段（[models.py](../src/dnspick/models.py#L36-L54)）：

```python
@dataclass(frozen=True)
class DnsServer:
    id: str
    name: str
    address: str
    protocol: Protocol
    region: str
    port: int = 53
    hostname: str | None = None
    tags: tuple[str, ...] = ()
    enabled: bool = True
```

`server_score` 表核心字段（[store.py](../src/dnspick/store.py#L44-L85)）：

```
server_id, server_name, address, protocol, region, tags,
total_score, verdict, advice,
cn_delay_ms, gl_delay_ms, cn_hops, gl_hops,
cn_success_rate, gl_success_rate, cn_reach_rate, gl_reach_rate,
cn_proximity, gl_proximity, cn_score, gl_score,
cn_stability, gl_stability, cn_speed, gl_speed, cn_path, gl_path,
dns_rtt_p50_ms, hijack, ecs_supported, polluted
```

---

## 二、目标架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     MainWindow（运行配置窗口）                     │
│  职责：展示当前生效的 DNS 协议分布 + 运行控制 + 进度反馈            │
│  不负责：服务器管理（→ Settings）、结果分析（→ HistoryWindow）      │
├──────────────────────────────────────────────────────────────────┤
│                     HistoryWindow（分析历史窗口）                   │
│  职责：历史记录浏览 + 多维度筛选 + 厂商×协议对比推荐 + 导出/删除    │
│  不负责：运行控制、服务器管理                                      │
├──────────────────────────────────────────────────────────────────┤
│                     SettingsDialog（配置窗口）                      │
│  职责：完整的服务器管理（启停/排序） + 测量参数 + 评分权重           │
│  不负责：运行控制、结果展示                                        │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、数据模型变更

### 3.1 DnsServer 新增 `vendor` 字段

在 `DnsServer` 中增加 `vendor` 字段，用于标识厂商，替代当前 `_generate_advice` 中靠 `server_id.rsplit("-", 1)[0]` 的隐式推断。

```python
@dataclass(frozen=True)
class DnsServer:
    id: str
    name: str
    address: str
    protocol: Protocol
    vendor: str         # 新增：厂商标识，如 "alibaba", "tencent", "cloudflare"
    region: str = "unknown"
    port: int = 53
    hostname: str | None = None
    tags: tuple[str, ...] = ()
    enabled: bool = True
```

**内置服务器 vendor 映射：**

| vendor       | 服务器 ID 前缀       | 示例服务器名称    |
|-------------|---------------------|------------------|
| `alibaba`   | `alidns-`           | 阿里 AliDNS, 阿里 DoH, 阿里 DoT |
| `tencent`   | `dnspod-`           | 腾讯 DNSPod, 腾讯 DoH, 腾讯 DoT |
| `baidu`     | `baidu`             | 百度 DNS          |
| `cloudflare`| `cf-`               | Cloudflare, Cloudflare DoH, Cloudflare DoT |
| `google`    | `google-`           | Google DNS, Google DoH, Google DoT |
| `quad9`     | `quad9-`            | Quad9, Quad9 DoH, Quad9 DoT |
| `adguard`   | `adguard-`          | AdGuard DNS, AdGuard DoH, AdGuard DoT |
| `opendns`   | `opendns-`          | OpenDNS, OpenDNS DoH |
| `dnssb`     | `dns-sb-`           | DNS.SB, DNS.SB DoH |
| `controld`  | `controld`          | ControlD          |
| `114dns`    | `114-`              | 114DNS            |
| `360dns`    | `360-`              | 360 DNS(电信), 360 DNS(联通) |
| `cnnic`     | `cnnic`             | CNNIC SDNS        |
| `huawei`    | `huawei`            | 华为云 DNS         |
| `onevps`    | `onevps`            | ONE DNS           |

### 3.2 server_score 表新增 `vendor` 列

```sql
ALTER TABLE server_score ADD COLUMN vendor TEXT NOT NULL DEFAULT '';
```

已有数据可通过 `server_id` 前缀回填 vendor，或依赖新字段在下次测量时写入。

---

## 四、MainWindow 改造方案

### 4.1 布局

```
┌──────────────────────────────────────────────────────────────┐
│  [▶ 开始测量]  [■ 停止]  档位:[balanced ▼]  ☐深度traceroute  ☐ECS  │
│                                        预计 3-5 分钟          │
├──────────────────────────────────────────────────────────────┤
│  ████████████░░░░░░ 60%   IP探测 · 142.250.80.1              │
├───────────────────────────┬──────────────────────────────────┤
│  当前 DNS 协议分布         │  域名配置                         │
│                           │                                  │
│  ┌─ UDP ─────────────┐   │  CDN调度 8个 · 国内普通 5个        │
│  │ ● 5 台已启用       │   │  国外 8个 · API发现 3个            │
│  │ 阿里·腾讯·114·百度… │   │  共 24 个域名参与测量              │
│  └───────────────────┘   │  [管理域名…] → DomainManagerDialog │
│  ┌─ DoH ─────────────┐   │                                  │
│  │ ● 3 台已启用       │   │  ── 上次测量 ──                   │
│  │ 阿里·腾讯·Cloudflare│   │  09-18 14:30 · 耗时 45s          │
│  └───────────────────┘   │  DNS 10台 · 域名 24个              │
│  ┌─ DoT ─────────────┐   │  [查看结果 ✅]                    │
│  │ ● 2 台已启用       │   │                                  │
│  │ 腾讯·Cloudflare    │   │                                  │
│  └───────────────────┘   │                                  │
│  ┌─ TCP ─────────────┐   │                                  │
│  │ ○ 未启用           │   │                                  │
│  └───────────────────┘   │                                  │
│                           │                                  │
│  [管理服务器…]             │                                  │
├───────────────────────────┴──────────────────────────────────┤
│  日志区域 (150px 最大高度)                                     │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 协议卡片设计

- **4 张卡片**：UDP、TCP、DoT、DoH，垂直排列在左侧
- 每张卡片显示：协议名、启用服务器数量、启用厂商名称列表（截断显示，完整列表通过点击展开）
- 卡片可**点击交互**：
  - **单击**：展开/折叠该协议下的启用服务器明细列表
  - **双击**：打开 Settings 并定位到对应协议的服务器分组
- 颜色区分：
  - 有启用的协议：绿色/蓝色左边框
  - 无启用的协议：灰色，降低透明度

### 4.3 移除的内容

| 移除项 | 原位置 | 新归属 |
|--------|-------|--------|
| 完整 `QTableWidget` 服务器表格 | MainWindow 左侧 | SettingsDialog |
| "全选/全不选/仅UDP/仅加密" 按钮 | 表格上方工具栏 | SettingsDialog |
| 服务器表格勾选交互 (`_on_server_toggle`) | MainWindow | SettingsDialog |
| 文件菜单"载入自定义服务器清单"（保留，但重定向到 Settings） | 菜单栏 | 保留入口，功能走 Settings |

### 4.4 新增的组件

| 组件 | 说明 |
|------|------|
| `_build_protocol_cards()` | 构建 4 张协议分布卡片的容器 |
| `_refresh_protocol_cards()` | 根据 `config.servers` 刷新卡片内容 |
| 卡片点击展开/折叠逻辑 | 复用现有 `_refresh_server_list` 中的数据，但在卡片内以内联方式展示 |

### 4.5 触发刷新的时机

| 事件 | 刷新内容 |
|------|---------|
| Settings 对话框确认关闭 | 协议卡片 + 域名摘要 + 预估时间 |
| 载入新服务器文件 | 协议卡片 + 域名摘要 + 预估时间 |
| 载入新域名文件 | 域名摘要 + 预估时间 |
| 档位切换 | 预估时间 |

---

## 五、HistoryWindow 增强方案

### 5.1 整体布局（改造后）

```
┌──────────────────────────────────────────────────────────────┐
│  优选历史 —— 结果分析                                          │
├──────────────────────────────────────────────────────────────┤
│  共 12 条历史记录           [导出 JSON] [删除此记录] [关闭]     │
├──────────┬───────────────────────────────────────────────────┤
│ 历史列表  │  ┌── 厂商协议推荐（摘要卡片）──────────────────┐    │
│          │  │ ★ 阿里巴巴 → 推荐 UDP(92分) DoH(88分)       │    │
│ 09-18…   │  │              ⚠ 备用 DoT(65分)               │    │
│ 09-17…   │  │ ★ 腾讯云   → 推荐 UDP(85分) DoH(82分)       │    │
│ 09-16…   │  │              ❌ 不推荐 DoT(42分,劫持)      │    │
│ ...      │  │ ○ Cloudflare → ⚠ 备用 DoH(68分)            │    │
│          │  └──────────────────────────────────────────┘    │
│          │                                                   │
│          │  ┌── 筛选面板 ────────────────────────────────┐   │
│          │  │ 协议: ☑UDP ☑TCP ☑DoT ☑DoH                 │   │
│          │  │ 地区: ☑国内 ☑国外                          │   │
│          │  │ 厂商: ☑阿里巴巴 ☑腾讯云 ☑Cloudflare …      │   │
│          │  │ 结论: ☑推荐 ☑备用 ☑限定 ☑不推荐           │   │
│          │  │ 标签: ☑ecs ☑cdn ☑encrypted …              │   │
│          │  │ 分数: [0 ════════●══ 100]                  │   │
│          │  │ 视图: [平铺列表 ▼]  [应用筛选] [重置]        │   │
│          │  └──────────────────────────────────────────┘    │
│          │                                                   │
│          │  ┌── 结果视图 ────────────────────────────────┐   │
│          │  │ 平铺表格 / 分组树（与当前一致）              │   │
│          │  └──────────────────────────────────────────┘    │
│          │                                                   │
│          │  ┌── 分析建议 ────────────────────────────────┐   │
│          │  │ HTML 格式推荐文本 + 同厂商协议对比表格       │   │
│          │  └──────────────────────────────────────────┘    │
└──────────┴───────────────────────────────────────────────────┘
```

### 5.2 新增：厂商协议推荐摘要卡片

这是本次增强的**核心组件**，位于筛选面板上方，提供"一眼看清"的厂商协议推荐。

#### 5.2.1 数据来源

由 `store.py` 新增方法提供：

```python
def vendor_protocol_summary(
    self,
    measurement_id: str,
    filters: ScoreFilter | None = None,
) -> list[dict[str, Any]]:
    """按厂商聚合，返回每个厂商下的协议推荐摘要。

    返回格式：
    [
        {
            "vendor": "alibaba",
            "display_name": "阿里巴巴",
            "protocols": [
                {"protocol": "udp", "score": 92.3, "verdict": "recommend",
                 "cn_delay_ms": 18.2, "hijack": False, "advice": "..."},
                {"protocol": "doh", "score": 88.1, "verdict": "recommend", ...},
                {"protocol": "dot", "score": 65.0, "verdict": "fallback", ...},
            ],
            "best_protocol": "udp",
            "has_recommend": True,
            "has_avoid": False,
        },
        ...
    ]
    """
```

#### 5.2.2 聚合逻辑（SQL 层）

```sql
SELECT
    vendor,
    protocol,
    MAX(total_score) AS best_score,
    -- 取最高分记录的 verdict/advice
    GROUP_CONCAT(DISTINCT verdict) AS verdicts,
    MIN(cn_delay_ms) AS cn_delay_ms,
    MAX(CASE WHEN hijack = 1 THEN 1 ELSE 0 END) AS has_hijack
FROM server_score
WHERE measurement_id = ?
  [AND protocol IN (...)]
  [AND region IN (...)]
  [AND ...]
GROUP BY vendor, protocol
ORDER BY vendor, best_score DESC
```

#### 5.2.3 卡片 UI 结构

每张卡片对应一个厂商，内部按协议横向排列标签：

```
┌── 阿里巴巴 ──────────────────────────────────────────────┐
│ ✅ UDP  92分  国内 18ms  推荐作为主力上游                    │
│ ✅ DoH  88分  国内 22ms  推荐作为主力上游                    │
│ ⚠ DoT  65分  国内 35ms  可作为备用                          │
└──────────────────────────────────────────────────────────┘

┌── Cloudflare ────────────────────────────────────────────┐
│ ⚠ DoH  68分  国内 120ms  国内延迟偏高，仅适合国外域名         │
│ ❌ UDP  45分  国内 180ms  国内表现不佳，不推荐               │
└──────────────────────────────────────────────────────────┘
```

#### 5.2.4 排序规则

厂商卡片按以下优先级排序：
1. 有"推荐"协议的厂商优先
2. 推荐协议数量多的优先
3. 最高分高的优先
4. 仅有"不推荐"的厂商排最后（折叠显示）

#### 5.2.5 交互

- 点击某个协议的标签 → 在下方结果视图中高亮/筛选该行
- 点击厂商名称 → 展开/折叠该厂商的全部协议

### 5.3 筛选面板增强

#### 5.3.1 新增：厂商筛选

在协议/地区/结论筛选行后，新增一行：

```
厂商: ☑阿里巴巴 ☑腾讯云 ☑Cloudflare ☑Google ☑Quad9 ☑AdGuard ☑OpenDNS …
```

每个复选框对应 `server_score.vendor` 中的一个去重值，初始全部勾选。

#### 5.3.2 新增：分数区间滑块

```
分数区间: [0 ═══════●═══════ 100]  当前: 50 - 100
```

使用两个 `QSlider` 或范围滑块，映射到 `ScoreFilter.min_score` / `ScoreFilter.max_score`。

#### 5.3.3 ScoreFilter 扩展

```python
@dataclass
class ScoreFilter:
    protocols: list[str] | None = None
    regions: list[str] | None = None
    vendors: list[str] | None = None      # 新增
    tags: list[str] | None = None
    verdicts: list[str] | None = None
    server_ids: list[str] | None = None
    min_score: float | None = None
    max_score: float | None = None
```

### 5.4 视图模式增强

当前两种分组模式：
- `server_name`（厂商） → `protocol`
- `protocol` → `region`

新增两种：

| 新模式 | 说明 | SQL 实现 |
|--------|------|---------|
| **厂商 → 结论** (`vendor → verdict`) | 一目了然：每个厂商的推荐/备用/避免分别是什么协议 | `compare(mid, "vendor", "verdict")` |
| **协议 → 厂商** (`protocol → vendor`) | 横向对比：同一 UDP 协议下各厂商排名 | `compare(mid, "protocol", "vendor")` |

`store.py` 的 `compare()` 方法需要更新：
- `group_by` / `sub_group_by` 的有效值增加 `"vendor"`
- SQL 的 `GROUP BY` 和 `ORDER BY` 使用 `vendor` 字段

### 5.5 现有代码的改动

| 文件 | 改动点 |
|------|--------|
| `store.py` | 新增 `vendor_protocol_summary()` 方法；`compare()` 支持 `vendor` 分组；`_apply_filters()` 支持 `vendors` 参数 |
| `history.py` | 在筛选面板上方新增"厂商协议推荐"卡片区；筛选面板新增厂商复选框 + 分数滑块；新增两种视图模式 |

---

## 六、SettingsDialog 改动

### 6.1 服务器管理标签页增强

当前服务器列表仅显示 `{name} — {address} [{protocol}]`。改造后显示：

```
  ☑ 阿里巴巴  UDP  223.5.5.5          阿里 AliDNS
  ☑ 阿里巴巴  UDP  223.6.6.6          阿里 AliDNS
  ☐ 阿里巴巴  DoH  dns.alidns.com     阿里 DoH
  ☐ 阿里巴巴  DoT  223.5.5.5:853      阿里 DoT
  ☑ 腾讯云    UDP  119.29.29.29       腾讯 DNSPod
  ☐ 腾讯云    DoH  doh.pub            腾讯 DoH
  ...
```

改进：
- 按 `vendor` 分组显示，厂商间用分隔线
- 显示 `vendor`、`protocol`、`address`、`name` 四列
- 保留全选/全不选/反选/仅UDP/仅DoH/DoT 按钮

### 6.2 入口

MainWindow 中的 **"管理服务器…"** 按钮 → 直接打开 SettingsDialog 的"DNS 服务器"标签页。

---

## 七、实施步骤

| 阶段 | 步骤 | 内容 | 影响文件 |
|------|------|------|---------|
| **阶段 1** | 1.1 | `DnsServer` 模型新增 `vendor` 字段 | `models.py` |
|          | 1.2 | 更新内置服务器列表，填充 vendor | `builtin.py` |
|          | 1.3 | `server_score` 表新增 `vendor` 列 + `ScoreFilter` 新增 `vendors` | `store.py` |
| **阶段 2** | 2.1 | MainWindow 构建协议分布卡片，替换完整表格 | `main_window.py` |
|          | 2.2 | MainWindow 新增"管理服务器…"按钮 → Settings | `main_window.py` |
|          | 2.3 | Settings 服务器标签页显示优化 | `settings.py` |
| **阶段 3** | 3.1 | `store.py` 新增 `vendor_protocol_summary()` | `store.py` |
|          | 3.2 | `store.py` 的 `compare()` 支持 `vendor` 分组 | `store.py` |
| **阶段 4** | 4.1 | HistoryWindow 新增"厂商协议推荐"摘要卡片 | `history.py` |
|          | 4.2 | HistoryWindow 筛选面板新增厂商 + 分数滑块 | `history.py` |
|          | 4.3 | HistoryWindow 新增两种视图模式 | `history.py` |

---

## 八、不影响的部分

| 组件 | 说明 |
|------|------|
| `scorer.py` | 评分逻辑不变 |
| `runner.py` | 测量运行逻辑不变 |
| `resolver.py` / `ipprobe.py` | DNS 解析 / IP 探测逻辑不变 |
| `config.py` | 配置加载逻辑不变 |
| `worker.py` | 后台测量线程不变 |
| `store.py` 的 `save()` / `list_measurements()` / `get_scores()` / `trend()` / `delete()` | 基础 CRUD 逻辑不变，仅扩展 |
| CLI (`cli.py`) | 命令行接口不变 |