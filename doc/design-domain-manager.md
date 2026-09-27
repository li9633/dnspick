# 域名管理器设计方案 v2

## 一、现状分析与问题

### 1.1 前端展示能力不足

```python
# main_window.py — 只展示统计数字，用户看不到具体有哪些域名
self.dom_label.setText(
    f"tier1 核心 <b>{t1}</b> 个 · tier2 常用 <b>{t2}</b> 个 · tier3 长尾 <b>{t3}</b> 个\n"
    ...
)
```

"管理域名…" 按钮 → 打开文件选择器载入 TOML，没有任何可视化管理。

### 1.2 tier 设计的意义与局限

`tier`（1=核心/2=常用/3=长尾）最初为 `discover.py` 设计——从浏览器历史批量挖掘 1000+ 域名时，
按访问频次分三层来控制测量规模。但对于 `builtin.py` 中的 22 个内置域名，它们**全部默认 tier=1**，
tier 在这里没有实际区分意义。

tier 的另一个隐含作用是"控制测量规模"——`Tunables.max_tier=1` 让 Runner 只测核心层。
但这本质上应该是**用户按自己的意图选择测哪些域名**，而不是被一个抽象的数字层级绑架。

### 1.3 浏览器历史挖掘未集成

`discover.py::mine_browser_history()` 已经具备从 Chrome/Edge/Firefox 历史中提取域名
（带访问次数）的能力，但它是独立脚本工具，没有集成到 GUI 或 Runner 中。
用户自己真正访问过的域名，远比任何内置列表更能反映个人需求。

### 1.4 痛点总结

| # | 痛点 |
|---|------|
| 1 | 用户看不到具体有哪些域名将被测量 |
| 2 | 不知道 API 发现功能发现了哪些关联域名 |
| 3 | 没有地方查看主域名 → 关联域名的关系 |
| 4 | 想增减域名只能改 TOML 文件 |
| 5 | tier 三层抽象对用户不直观 |
| 6 | 浏览器历史这个最有价值的数据源没有被利用 |

---

## 二、设计目标

| 目标 | 说明 |
|------|------|
| **可视化管理** | 列表展示所有待测域名，可见名称、来源、类别、备注 |
| **多源统一** | 内置域名 + 浏览器历史 + API 发现 + 手动添加，统一管理 |
| **智能去重** | 多来源交集的域名自动去重，按优先级保留 |
| **分类筛选** | 按类别（CDN/国内/国外/浏览器/发现/手动）筛选，支持全选/反选 |
| **关系展示** | 查看主域名与关联域名的映射 |
| **精简模型** | 移除 `tier`，简化 `DomainSpec` |

---

## 三、域名来源架构

### 3.1 四个来源，按优先级排列

```
来源优先级（去重时保留高优先级）：
  1. 内置域名   (builtin)     — 22 个精选强 CDN 域名，始终保留
  2. 用户添加   (manual)      — 用户通过 UI 手动添加
  3. 浏览器历史 (history)     — 从本地 Chrome/Edge/Firefox 挖掘
  4. API 发现   (discovered)  — HTML 抓取 + crt.sh 证书查询
```

### 3.2 去重规则

```
当同一个域名出现在多个来源时：
  - 保留优先级最高的来源标记
  - 被去重项记录到 note 中："来自浏览器历史（已存在 #内置）"
  - 浏览器历史中的访问次数 > 内置的访问次数时，合并统计信息
```

### 3.3 数据流

```
DomainManager 打开
  │
  ├─ 1. 加载 Config.domains        → 内置 + 用户添加
  ├─ 2. 加载 RelationStore         → API 发现（已持久化在 DB 中）
  ├─ 3. [可选] 扫描浏览器历史       → mine_browser_history()
  │
  └─ 4. 合并去重 → 统一 DomainItem 列表 → 展示
```

---

## 四、数据模型重构

### 4.1 简化 DomainSpec

```python
# models.py — 移除 tier，保持 frozen
@dataclass(frozen=True)
class DomainSpec:
    name: str
    group: Group
    note: str = ""
    # tier: int = 1  ← 移除
```

### 4.2 域名来源枚举

```python
from enum import Enum, auto

class DomainSource(Enum):
    BUILTIN = auto()     # builtin.py 内置
    MANUAL = auto()      # 用户手动添加
    HISTORY = auto()     # 浏览器历史挖掘
    HTML = auto()        # API 发现 - HTML 抓取
    CRT_SH = auto()      # API 发现 - crt.sh
```

### 4.3 域名类别（UI 维度，运行时推导）

```python
from enum import Enum

class DomainCategory(Enum):
    CDN = "CDN调度域名"       # group=CN + note 含 "CDN" 或内置 CDN 名单
    CN = "国内普通"           # group=CN 且非 CDN
    GLOBAL = "国外"           # group=GLOBAL
    HISTORY = "浏览器历史"     # domain.source == HISTORY
    DISCOVERED = "API发现"    # domain.source in (HTML, CRT_SH)
    MANUAL = "手动添加"       # domain.source == MANUAL
```

> 类别不存储在 `DomainSpec` 中，而是由 `DomainItem` 在构建时根据来源和 group 推导。
> 一个域名可能属于多个类别（如内置 CDN 域名同时属于 CDN + BUILTIN）。

### 4.4 整合的域名视图

```python
@dataclass
class DomainItem:
    name: str
    group: Group
    source: DomainSource
    categories: list[DomainCategory]  # 所属类别（可多个）
    note: str = ""
    enabled: bool = True
    visit_count: int = 0  # 浏览器访问次数（仅 HISTORY 来源有值）
    main_domain: str = ""  # 关联域名的主域名（仅 API 发现有关联域名有值）
    related_count: int = 0  # 此主域名的关联域名数（仅主域名有值）
```

### 4.5 配置持久化

```python
# config.py — Tunables 移除 max_tier
@dataclass
class Tunables:
    # ... 其他字段不变 ...
    # max_tier: int = 1  ← 移除

# config.py — Config 新增字段
@dataclass
class Config:
    tunables: Tunables
    servers: list[DnsServer]
    domains: list[DomainSpec]          # 所有已知域名
    disabled_domains: set[str]         # 本次禁用的域名名
    extra_cn_prefixes: list[str]       # 不变
    geoip_db: str | None = None        # 不变
    use_ecs: bool = False              # 不变
```

`active_domains()` 简化为只需过滤 disabled：

```python
def active_domains(self) -> list[DomainSpec]:
    return [d for d in self.domains if d.name not in self.disabled_domains]
```

---

## 五、移除 tier 的改动范围

| 文件 | 改动 | 说明 |
|------|------|------|
| `models.py` | 移除 `tier: int = 1` | DomainSpec 字段 |
| `config.py` | 移除 `max_tier: int = 1` | Tunables 字段 |
| `config.py` | 移除 `max_tier` 参数 | `active_domains()` 方法 |
| `config.py` | 移除 `max_tier` 持久化 | `save()` / `load_saved()` |
| `cli.py` | 移除 `--tier` 参数 | CLI `run` 命令 |
| `cli.py` | 移除 tier 显示 | 运行结果输出 |
| `runner.py` | 移除 `max_tier=t.max_tier` | `active_domains()` 调用 |
| `main_window.py` | 移除 tier 统计 | `_refresh_domain_summary()` |
| `api_discovery.py` | 移除 `tier=1` | `to_domain_specs()` |
| `discover.py` | 不移除 tier | `Discovered` 类内部仍用 tier 排序，但不写入 DomainSpec |
| `discover.py` | `write_toml()` 不输出 tier | 生成的 TOML 文件去掉 `tier = 1` |
| `builtin.py` | 无影响 | 原本就没显式设 tier |

---

## 六、UI 布局

### 6.1 入口

MainWindow "域名配置" 区域 → "管理域名…" 按钮 → 打开 `DomainManagerDialog`

### 6.2 对话框布局

```
┌──────────────────────────────────────────────────────────────────────────┐
│  域名管理器                                                     [×]     │
├───────────────┬──────────────────────────────────────────────────────────┤
│  域名类别      │  域名列表 (已选 28 / 共 35 个)                          │
│               │  ┌────┬──────────────────────┬──────┬──────────┬─────┐  │
│  ☑ CDN调度    │  │ ☑  │ www.bilibili.com     │ CN   │ CDN调度   │ 内置│  │
│  ☑ 国内普通   │  │ ☑  │ www.baidu.com        │ CN   │ 国内普通  │ 内置│  │
│  ☑ 国外       │  │ ☑  │ www.google.com       │ GLOBAL│ 国外     │ 内置│  │
│  ☑ 浏览器历史 │  │ ☑  │ www.taobao.com       │ CN   │ CDN调度   │ 历史│  │
│  ☑ API发现    │  │ ☑  │ mail.163.com         │ CN   │ 浏览器历史│ 历史│  │
│  ☐ 手动添加   │  │ ☑  │ api.bilibili.com     │ CN   │ API发现   │ 发现│  │
│               │  │ ☐  │ www.zhihu.com        │ CN   │ 国内普通  │ 内置│  │
│               │  │    │ …                    │ …    │ …        │ …   │  │
│               │  └────┴──────────────────────┴──────┴──────────┴─────┘  │
│               │                                                          │
│               │  [全选] [全不选] [反选]                                  │
│               │                                                          │
│  [扫描浏览器] │  [+ 添加域名] [− 删除选中] [查看关联] [导入 TOML…]       │
├───────────────┴──────────────────────────────────────────────────────────┤
│  选中: www.bilibili.com  [CDN调度 · 内置]  访问 0 次                    │
│  ├─ 关联域名 (16 个): api.bilibili.com, i0.hdslb.com, s1.hdslb.com,     │
│  │   chat.bilibili.com, passport.bilibili.com ...                       │
│  │   来源: HTML抓取(3) + crt.sh(13)                                     │
│  └─ 数据类型: 主域名 (内置)                                              │
├──────────────────────────────────────────────────────────────────────────┤
│  [确定]  [取消]                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.3 布局说明

| 区域 | 组件 | 说明 |
|------|------|------|
| **域名类别** | `QGroupBox` + 6 个 CheckBox | 勾选类别 → 表格显示该类别域名。一个域名多个类别时，任意匹配即显示 |
| **域名列表** | `QTableWidget` | 列: `☑` `域名` `分组` `类别` `来源` `备注`。`类别` 列可用颜色标签区分 |
| **操作按钮** | `QPushButton` × 8 | 全选/全不选/反选（操作当前可见行）、扫描浏览器、添加、删除、查看关联、导入TOML |
| **关联预览** | `QGroupBox` | 选中主域名或关联域名时，显示其关联关系 |
| **底栏** | `QDialogButtonBox` | 确定/取消 |

### 6.4 类别颜色编码

| 类别 | 颜色 | 
|------|------|
| CDN调度 | `#e74c3c` 红 |
| 国内普通 | `#3498db` 蓝 |
| 国外 | `#2ecc71` 绿 |
| 浏览器历史 | `#f39c12` 橙 |
| API发现 | `#9b59b6` 紫 |
| 手动添加 | `#95a5a6` 灰 |

---

## 七、交互流程

### 7.1 打开管理器

```
MainWindow."管理域名…" 
  → DomainManagerDialog(config, relation_store)
    → _load_all_sources()
        ├─ 1. config.domains → DomainItem(source=BUILTIN|MANUAL)
        ├─ 2. RelationStore.get_relations() → DomainItem(source=HTML|CRT_SH)
        └─ 3. → _dedup_merge() 合并去重
    → _refresh_table() 渲染
```

### 7.2 扫描浏览器历史

```
[扫描浏览器] 按钮
  → 显示进度: "正在扫描 Chrome/Edge/Firefox 历史…"
  → mine_browser_history(min_hits=2)   # 复用 discover.py 现有函数
  → 过滤掉噪声域名（analytics、CDN 资源、localhost 等）
  → 返回 {域名: 访问次数}
  → 与现有域名去重: 已存在的 → 合并 visit_count + 标记来源
  → 新增的 → DomainItem(source=HISTORY, visit_count=N)
  → 自动勾选"浏览器历史"类别，刷新表格
  → 显示提示: "发现 156 个域名（新增 87 个，已存在 69 个）"
```

### 7.3 类别筛选

```
类别 CheckBox 变化
  → _apply_filter()
    - 未勾选的类别 → 隐藏属于该类别且不属于其他已勾选类别的域名
    - 域名属于多个类别时，只要其中一个类别被勾选就显示
    - 示例: api.bilibili.com 同时属于 "CDN调度" 和 "API发现"
            → 只要勾选任意一个就显示
```

### 7.4 表格操作

| 按钮 | 行为 |
|------|------|
| **全选** | 勾选当前**可见**的所有域名 |
| **全不选** | 取消当前**可见**的所有域名勾选 |
| **反选** | 反转当前**可见**的所有域名勾选状态 |
| **+ 添加域名** | 弹出输入框 → 输入域名 → 自动检测 group → source=MANUAL |
| **− 删除选中** | 确认后移除。内置域名标记为 disabled；自定义域名物理移除 |
| **查看关联** | 选中主域名 → 切换聚焦到关联预览区域 |
| **导入 TOML** | 打开文件选择器 → 合并到 config.domains → 去重 → 刷新 |

### 7.5 查看关联域名

```
选中某域名行
  → 如果该域名是"主域名"（在 RelationStore 中有 as main_domain 的记录）
    → 底部显示其所有关联域名列表 + 来源统计
  → 如果该域名是"关联域名"（有 main_domain 字段）
    → 底部显示 "← 来自 www.bilibili.com 的关联域名 (crt.sh)"
  → 如果两者都不是
    → 底部显示 "无关联域名数据"
```

### 7.6 确定关闭

```
[确定] 按钮
  → 收集勾选状态
    - enabled (勾选的)    → 加入 active 集合
    - disabled (未勾选)   → 加入 config.disabled_domains
  → 新增的 MANUAL 域名 → config.domains
  → 浏览器历史中勾选的域名 → config.domains（作为新 DomainSpec，note="浏览器历史 · 访问 N 次"）
  → 保存 config
  → 关闭对话框
  → MainWindow._refresh_domain_summary() 刷新统计
```

---

## 八、全部域名迁移到数据库

### 8.1 统一数据库：`history/dnspick.db`

不再使用 TOML 文件、`config.json` 存储域名。`history/dnspick.db` 已是 `store.py`
和 `relation_store.py` 共用的数据库，新增一张 `domain` 表即可。

```
history/dnspick.db
├── measurement        ← 已有 (store.py)
├── server_score       ← 已有 (store.py)
├── domain_relation    ← 已有 (relation_store.py)
└── domain             ← 新增 (domain_store.py)
```

### 8.2 `domain` 表结构

```sql
CREATE TABLE IF NOT EXISTS domain (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,          -- 域名，唯一
    group_name      TEXT NOT NULL DEFAULT 'cn',    -- 'cn' / 'global'
    source          TEXT NOT NULL,                  -- 'builtin' / 'manual' / 'history' / 'html' / 'crt_sh'
    note            TEXT DEFAULT '',                -- 备注
    enabled         INTEGER NOT NULL DEFAULT 1,     -- 1=启用, 0=禁用
    visit_count     INTEGER NOT NULL DEFAULT 0,    -- 浏览器访问次数
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_domain_source ON domain(source);
CREATE INDEX IF NOT EXISTS idx_domain_enabled ON domain(enabled);
CREATE INDEX IF NOT EXISTS idx_domain_group ON domain(group_name);
```

### 8.3 DomainStore 类

```python
# domain_store.py — 新增文件，管理 domain 表
class DomainStore:
    DB_PATH = Path("history") / "dnspick.db"

    def init(self) -> None: ...
    def seed_builtin(self) -> int: ...
    def get_all(self) -> list[DomainSpec]: ...
    def get_enabled(self) -> list[DomainSpec]: ...
    def upsert(self, name, group, source, note, visit_count) -> None: ...
    def set_enabled(self, name: str, enabled: bool) -> None: ...
    def set_enabled_batch(self, names: set[str], enabled: bool) -> None: ...
    def delete(self, name: str) -> None: ...
    def count_by_source(self) -> dict[str, int]: ...
    def count_by_group(self) -> dict[str, int]: ...
```

**API 设计原则**：对外返回 `DomainSpec`（保持与现有代码兼容），内部存储到 SQLite。

### 8.4 首次运行：种子数据

两套种子，一套侧重质量，一套侧重广度：

```
种子来源                          数量      特征
──────────────────────────────────────────────────────────
builtin.py::_DOMAINS              ~22      精选 CDN 强调度域名，带优质备注
resources/seed_domains.txt       ~1000     按阿里系/腾讯系/字节系/国外等分区
```

**写入顺序**：`builtin.py` 先写入（带备注），`seed_domains.txt` 后写入。
已存在的域名（按 `name` UNIQUE）自动跳过，不覆盖前者的 `note`。

`seed_domains.txt` 的解析复用 `discover.py::load_seeds()` 的逻辑：

```
# ===== 国内 · 阿里系 =====    →  group=cn,  section="国内 · 阿里系"
www.taobao.com
www.tmall.com
...

# ===== 国外 · Google系 =====   →  group=global, section="国外 · Google系"
www.google.com
...
```

```python
# domain_store.py
class DomainStore:
    def seed_from_file(self, path: Path) -> int:
        """将 seed_domains.txt 写入 domain 表。已存在的跳过。返回写入数。"""
        seeds = load_seeds(path)  # 复用 discover.py
        count = 0
        for name, group, section in seeds:
            if self.exists(name):
                continue
            self.upsert(
                name=name,
                group=Group(group),
                source='builtin',
                note=section,
            )
            count += 1
        return count

    def seed_all(self) -> tuple[int, int]:
        """首次运行：写入全部种子数据。返回 (builtin数, file数)。"""
        if self._has_builtin():
            return (0, 0)  # 已初始化，跳过

        # 1. 精选 CDN 域名（先写，带备注）
        n1 = 0
        for d in builtin.seed_domains():
            if not self.exists(d.name):
                self.upsert(name=d.name, group=d.group, source='builtin', note=d.note)
                n1 += 1

        # 2. 种子文件（后写，不覆盖）
        n2 = self.seed_from_file(SEED_FILE)

        return (n1, n2)
```

```
应用启动
  → DomainStore.init()
    → 检查 domain 表是否有 source='builtin' 的行
    → 若无 → seed_all()
        → 写入 22 个精选域名（带 "阿里系 CDN" "百度" 等备注）
        → 写入 ~1000 个种子域名（用分区名如 "国内 · 阿里系" 作备注）
        → 自动去重：builtin.py 已有的跳过
    → 若有 → 跳过（不覆盖用户修改）
```

### 8.5 简化后的 Config

```python
# config.py — 不再持有域名列表
@dataclass
class Config:
    tunables: Tunables
    servers: list[DnsServer]
    extra_cn_prefixes: list[str]
    geoip_db: str | None = None
    use_ecs: bool = False
    # 移除: domains: list[DomainSpec]
    # 移除: disabled_domains: set[str]

    def active_servers(self) -> list[DnsServer]: ...

    # active_domains() 移除，改为外部调用 DomainStore.get_enabled()
```

### 8.6 简化后的 config.json

```json
{
  "tunables": { ... },              // 不变，只是没有 max_tier
  "enabled_servers": [...],         // 不变
  // 移除: custom_domains
  // 移除: disabled_domains
}
```

### 8.7 域名生命周期（数据库版）

```
应用启动
  │  DomainStore.init() → seed_builtin() 首次写入内置域名
  ▼
DomainManager 打开
  ├─ DomainStore.get_all()            → 所有已知域名
  ├─ RelationStore.get_relations()    → 关联关系
  ├─ [可选] mine_browser_history()    → 扫描结果临时在内存
  └─ 合并 → 展示
  ▼
用户操作
  ├─ 勾选/取消    → 仅修改 UI 内存状态，不立即写 DB
  ├─ 添加域名     → 立即写入 domain 表 (source='manual')
  ├─ 删除域名     → 内置: SET enabled=0; 自定义: DELETE
  ├─ 扫描浏览器   → 内存中展示，勾选后写入 domain 表
  └─ 点 [确定]    → DomainStore.set_enabled_batch() 批量更新
  ▼
Runner.run()
  ├─ DomainStore.get_enabled()        → 取所有 enabled=1 的域名
  ├─ [可选] API 发现                  → 写入 domain_relation 表
  └─ 去重 → 开始 DNS 测量
```

---

## 九、Runner 管道：扁平 9 阶段

### 9.1 实际阶段序列

```
Runner.run()
  │
  ├── 1. 环境探测（公网IP / ECS子网）
  ├── 2. 域名收集（DomainStore.get_enabled()）
  ├── 3. API 域名发现（可选，asyncio.gather 并发）
  │     ├─ discover_all()
  │     ├─ 写入 domain_relation 表
  │     └─ 与已有域名去重
  ├── 4. DNS 解析矩阵
  ├── 5. ECS 对比
  ├── 6. TCP 探测
  ├── 7. ICMP 探测
  ├── 8. GeoIP 归属地
  └── 9. 评分与结论
```

### 9.2 Runner 阶段管理

```python
class Runner:
    def __init__(self, config: Config, domain_store: DomainStore, ...):
        self.cfg = config
        self.dom_store = domain_store

    async def run(self) -> RunResult:
        self._enter_stage("环境探测")
        public_ip = await self._detect_public_ip()
        ecs = _to_subnet(public_ip) if public_ip and self.cfg.use_ecs else None

        self._enter_stage("域名收集")
        domains = self.dom_store.get_enabled()

        if self.cfg.tunables.api_discovery_enabled:
            self._enter_stage("API发现")
            extra = await self._discover_api_domains(domains)
            domains.extend(extra)
        else:
            self._skip_stage("API发现")

        self._enter_stage("DNS 解析")
        records = await self._resolve(domains, servers, ecs)
        # ... 后续阶段类似 ...
```

---

## 十、文件改动清单

| 文件 | 改动量 | 说明 | 状态 |
|------|--------|------|------|
| `src/dnspick/models.py` | **已修改** | 移除 `DomainSpec.tier` | ✓ 已实现 |
| `src/dnspick/domain_store.py` | **新增 ~200 行** | `DomainStore` 类，管理 `domain` 表 | ✓ 已实现 |
| `src/dnspick/builtin.py` | **已修改** | 新增 `seed_domains()` 函数 | ✓ 已实现 |
| `src/dnspick/config.py` | **已修改** | 移除 `max_tier`、`domains`、`disabled_domains` | ✓ 已实现 |
| `src/dnspick/runner.py` | **已修改** | 扁平9阶段序列；接入 `DomainStore` | ✓ 已实现 |
| `src/dnspick/cli.py` | **已修改** | 移除 `--tier`/`--domains` 参数 | ✓ 已实现 |
| `src/dnspick/gui/main_window.py` | **已修改** | 移除 tier 统计；"管理域名…" 打开 DomainManagerDialog | ✓ 已实现 |
| `src/dnspick/gui/domain_manager.py` | **新增 ~450 行** | 完整对话框实现 | ✓ 已实现 |

---

## 十一、实现优先级

| 优先级 | 功能 | 理由 |
|--------|------|------|
| **P0** | `domain` 表 + `DomainStore` 类 | 基础设施，一切依赖它 |
| **P0** | 移除 `tier`（models/config/cli/runner/discover） | 清理无效抽象 |
| **P0** | Config 精简：移除 domains 字段 | 与 DomainStore 对接 |
| **P1** | Runner 管道重构（收集→测量） | 架构优化 |
| **P1** | DomainManagerDialog 基础框架 + 表格展示 | 最小可用的 UI |
| **P1** | 类别过滤 + 全选/反选 | 核心交互 |
| **P2** | 关联域名预览（底部面板） | 解决核心痛点 |
| **P2** | 扫描浏览器历史 + 写入 DB | 差异化功能 |
| **P2** | 添加/删除域名（通过 DomainStore） | 灵活性 |
| **P3** | 导入 TOML → 写入 DB | 兼容旧流程 |
| **P3** | 颜色标签 + 样式优化 | 体验打磨 |