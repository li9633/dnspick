# dnspick

[English](../README.md)

用于家庭网络环境的 DNS 上游基准测试工具。评估 DNS 解析器返回 IP 地址的质量——而非仅测量响应延迟。
输出可直接用于 SmartDNS 的配置；不修改任何系统设置。

> [!IMPORTANT]
> 本工具评估的是 **DNS 解析器返回的 IP 地址质量**，而非应答速度。在家庭网络的本地缓存场景下，
> DNS 响应延迟（RTT）几乎无意义——决定体验的是解析器将你路由到了最近的 CDN 节点还是远距离区域。

---

## 设计动机

传统 DNS 基准测试关注响应延迟（RTT）。在家庭网络的本地缓存场景下，首次查询之后不同解析器的 RTT 差异几乎消失。

对实际浏览体验而言，决定性的因素是 CDN 节点的地理分布：解析淘宝、京东等国内站点时，
DNS 返回的 IP 究竟是就近的省内 CDN 节点，还是绕至跨省甚至海外节点的差异，远大于 RTT 差异本身。

dnspick 采用两阶段测量：

```
DNS 解析矩阵  →  每台服务器 × 每个域名 × N 次重复，收集返回的 IP 集合
IP 链路探测   →  对所有去重 IP 执行 TCP 握手、跳数统计、ICMP、归属地查询
分组独立评分  →  国内/国外分别打分，产出四档 verdict
```

核心原则：衡量 DNS 返回的 IP 质量，而非应答速度。

---

## 测量流水线

| 阶段 | 描述 |
|---|---|
| 环境探测 | 公网出口 IP / ECS 子网自动检测 |
| 域名收集 | 从 SQLite 加载已启用的域名（内置 + 浏览器历史 + 手动添加 + API 发现） |
| API 发现（可选） | HTML 抓取 + crt.sh 证书透明度日志发现关联域名 |
| DNS 解析 | 服务器 × 域名 × 重复次数矩阵，支持 UDP / TCP / DoT / DoH |
| ECS 对比（可选） | 对比带/不带 ECS 子网的解析结果 |
| TCP 探测 | 多端口握手（443/80/8443）× 多次采样，取 p50 延迟 |
| ICMP 探测 | 线程池系统 ping + 可选 TCP TTL 递增 traceroute |
| 归属地查询 | 离线 CN 网段表；无法判断时回退为 unknown |
| 综合评分 | 加权多维评分，按组产出 verdict |

---

## 评分维度

| 维度 | 方法 | 权重（balanced） |
|---|---|---|
| **就近性** | 同组国内域名解析 IP 的 TCP 延迟相对排名 | 30 |
| **速度** | 每域名最快 IP 的组内 TCP 握手延迟中位数 | 30 |
| **稳定性** | 加权综合：解析成功率(3/4) + IP 可达率(1/4) + TCP 成功率 | 25 |
| **路径** | TCP TTL 递增法跳数统计（不需要 raw socket） | 15 |

### 附加检测

- **劫持检测**：对随机不存在的子域发起查询，有返回结果即判定劫持。
- **污染检测**：国际域名解析出境内 IP 即判定 DNS 污染。
- **ECS 支持**：对比带/不带 ECS 子网的解析结果差异。
- **环境诊断**：识别透明代理或本地拦截导致的延迟虚低，避免测量结果失去区分度。

> [!WARNING]
> 在存在透明代理或本地拦截的网络环境（常见于企业 VPN 和部分运营商），所有 IP 的 TCP 握手可能
> 显示近乎零延迟和单跳路径。此时延迟和跳数评分完全失去意义。请在直连的家庭网络环境下运行测量。

---

## 评分引擎

### 分组独立评分

国内和国际域名分别评分（默认权重：国内 0.7，国际 0.3）。
DNS 解析器在不同分组上通常表现不对称，合并评分会掩盖这一差异。

### 两阶段流水线

1. **分组子项计算**：分别对国内/国际组计算速度、路径、稳定性、就近性四项子分（归一化至 0–100）。
2. **综合 verdict**：
   ```
   总分 = cn_weight × 国内组得分 + global_weight × 国际组得分
          - 劫持扣分(60) - 污染扣分(25)
   ```
   verdict 四档：**推荐 / 备用 / 限定分组 / 避免**

### 预设评分档位

通过 `--profile` 参数或 GUI 下拉框切换：

| 档位 | 速度 | 路径 | 稳定 | 就近 | 适用场景 |
|---|---|---|---|---|---|
| `balanced` | 30 | 15 | 25 | 30 | 日常综合 |
| `speed` | 55 | 10 | 15 | 20 | 游戏/实时通信 |
| `stable` | 20 | 10 | 50 | 20 | 下载/串流 |
| `cdn` | 20 | 10 | 15 | 55 | CDN 密集型 |
| `privacy` | 25 | 10 | 20 | 20 | 隐私优先 |

GUI 设置面板支持滑块自定义权重。

---

## 安装

需要 **Python 3.13.7+**。使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

> [!NOTE]
> 在 OpenWrt 等无头环境下，使用 `--no-group gui --no-group dev` 跳过 PySide6，
> 可避免拉取约 600 MB 的 GUI 依赖。

```bash
# 桌面端（GUI + 开发工具）
uv sync

# 无头模式（仅 CLI，不安装 PySide6）
uv sync --no-group gui --no-group dev

# 可选 GeoIP 支持（需要 MaxMind mmdb）
uv sync
pip install maxminddb
```

### 平台支持

| 平台 | CLI | GUI |
|---|---|---|
| Windows | ✓ | ✓ |
| Linux | ✓ | ✓ |
| OpenWrt | ✓ | — |
| macOS | ✓ | ✓ |

---

## 使用

### GUI

```bash
uv run dnspick-gui
```

功能：
- 协议分布仪表盘，一键启动/停止
- 设置面板：服务器启停、测量参数、自定义评分权重
- 实时阶段进度面板
- 流式日志输出，可调节日志等级
- 历史分析 Tab 页（结果总览 / 分组对比 / 分析建议）
- SmartDNS 配置导出（仅推荐级服务器，遵循当前筛选和排序）

### CLI

```bash
# 基础测量
uv run dnspick run

# 指定评分档位
uv run dnspick run --profile speed

# 限制输出条目
uv run dnspick run --top 5

# 按协议筛选
uv run dnspick run --protocol dot,doh

# 导出结果
uv run dnspick run --json result.json --csv result.csv

# 深度 traceroute 模式
uv run dnspick run --deep

# 自定义服务器清单
uv run dnspick run --servers my-servers.toml

# 启用 API 域名发现
uv run dnspick run --api-discovery

# 查看完整选项
uv run dnspick run --help
```

### 域名发现

从浏览器历史和内置种子收集域名：

```bash
uv run dnspick discover

# 从指定 URL 抓取外链域名
uv run dnspick discover --crawl https://example.com

# 拉取在线热门站点排名
uv run dnspick discover --online

# 仅浏览器历史，跳过内置种子
uv run dnspick discover --no-seed

# 限制输出数量
uv run dnspick discover --max 500
```

---

## 内置资源

- **30+ DNS 上游**：涵盖阿里、腾讯、百度、114DNS、Cloudflare、Google、Quad9、AdGuard 等。
  各厂商提供 UDP 端点，可选 DoT/DoH。默认仅启用各厂商主要 UDP 节点。
- **370+ 种子域名**：主要国内外站点及 CDN 端点。存入 SQLite 数据库，支持按类别筛选。
- **离线 GeoIP**：内置 CN 网段表，无需网络即可判定国内外归属。

所有内置资源均可通过外部 TOML 文件覆盖或追加。

---

## 项目结构

```
dnspick/
├── src/dnspick/
│   ├── gui/               # PySide6 GUI 层
│   │   ├── app.py         # 入口
│   │   ├── main_window.py # 运行配置仪表盘
│   │   ├── settings.py    # 设置对话框（服务器/权重/参数）
│   │   ├── history.py     # 历史分析窗口（Tab 页）
│   │   ├── progress_panel.py  # 阶段进度面板
│   │   ├── worker.py      # 测量 Worker（QThread）
│   │   └── runner_config.py   # 配置 Signal 桩（UI 绑定）
│   ├── cli.py             # CLI 入口（Typer + Rich）
│   ├── runner.py          # 测量编排器（阶段调度）
│   ├── resolver.py        # DNS 解析矩阵（有界工作池，多协议）
│   ├── ipprobe.py         # IP 链路探测（TCP/ICMP/跳数/traceroute）
│   ├── scorer.py          # 评分引擎（分组打分 + verdict）
│   ├── discover.py        # 域名发现（浏览器历史/种子/在线）
│   ├── api_discovery.py   # API 域名自动发现（HTML + crt.sh）
│   ├── geoip.py           # GeoIP 归属地解析
│   ├── builtin.py         # 内置 DNS 服务器 + 域名
│   ├── config.py          # 配置加载/持久化
│   ├── models.py          # 数据模型（纯数据容器）
│   ├── store.py           # SQLite 结果存储
│   ├── domain_store.py    # 域名数据库
│   ├── relation_store.py  # 域名关联关系存储
│   └── log.py             # 全局日志（RotatingFile + Qt Signal）
├── resources/
│   ├── servers.toml       # 内置 DNS 服务器注册表
│   └── seed_domains.txt   # 种子域名列表（首次运行写入 SQLite）
├── tests/                 # 单元测试（pytest）
├── doc/                   # 设计文档
├── scripts/               # 辅助脚本
└── pyproject.toml         # 项目元数据与依赖
```

---

## 开发

```bash
# 运行测试
uv run pytest tests/ -v

# 代码检查
uv run ruff check
uv run ruff check --fix
```

---

## 许可证

MIT