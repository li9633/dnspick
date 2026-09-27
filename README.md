# dnspick

**为家庭网络挑选最优 DNS 上游的测量器。** 只做测量与建议，不改任何系统设置——结果你自己填进 OpenWrt 的 SmartDNS 或其他分流代理。

---

## 它和传统 DNS 优选有什么不同？

市面上多数工具只看一件事：**DNS 响应有多快。** 这在家庭场景里意义不大——本地缓存一上，RTT 差异就被抹平了。

真正影响体验的是：**DNS 把淘宝解析到了哪个 CDN 节点？** 是离你最近的省节点，还是绕到了北京、上海甚至海外？

所以 dnspick 的流程是两段式的：

```
DNS 解析矩阵          →  每台 DNS × 每个域名 × N 次，收集它返回的 IP 集合
IP 链路探测           →  对去重后的所有 IP 做 TCP 握手、跳数、ICMP、归属地
分组独立评分           →  国内/国外分开打分，最终产出四档结论
```

一句话：**不看 DNS 回答得多快，看它给你的 IP 有多好。**

---

## 测量流程

| 阶段 | 内容 | 说明 |
|---|---|---|
| 环境探测 | 出口公网 IP / ECS 子网 | 自动检测，仅用于信息展示和 ECS 对比 |
| 域名收集 | 从 SQLite 数据库加载启用域名 | 内置 + 浏览器历史 + 手动添加 + API 发现，统一管理 |
| API 发现（可选） | HTML 首页 + crt.sh 证书日志 | 自动发现主站的关联域名 |
| DNS 解析 | 服务器 × 域名 × 重复 N 次矩阵 | 支持 UDP / TCP / DoT / DoH |
| ECS 对比（可选） | 带/不带 ECS 两次查询结果对比 | 检测 DNS 是否真正支持 ECS |
| TCP 探测 | 多端口（443/80/8443）× 多次握手 | 取 p50 延迟与成功率——最贴近真实访问体验 |
| ICMP 探测 | 系统 ping（线程池）+ 可选跳数探测 | 辅助指标；跳数通过 TCP TTL 递增法（免 raw socket）实现 |
| 归属地 | GeoIP（可降级为未知） | 用于就近性评分和污染判定 |
| 综合评分 | 四维加权 + 两阶段流水线 | 产出结论、建议和可直接填进 SmartDNS 的配置 |

---

## 四项判据

| 维度 | 怎么测 | 为什么重要 |
|---|---|---|
| **就近性** | 国内 CDN 域名解析出的 IP，TCP 延迟在同组内的相对排名 | 决定打开淘宝/抖音的实际速度 |
| **速度** | 对该 DNS 返回的 IP 做 TCP 握手，取每域名最快 IP 的组中位延迟 | 对齐 SmartDNS 的 `fastest-ip` 语义 |
| **稳定性** | 解析成功率（3/4）+ IP 可达率（1/4）+ TCP 握手成功率的加权 | 解析频繁失败或 IP 不可达比慢更致命 |
| **路径** | TCP TTL 递增法数跳数 | 看运营商是否绕路到外省或海外 |

此外还会额外检测：

- **劫持**：对随机不存在子域做查询，返回结果即判劫持
- **污染**：国外域名解析出境内 IP 即判污染
- **ECS 支持**：对比带/不带 ECS 子网的返回结果
- **环境诊断**：识别透明代理/本地拦截，提示用户测了个寂寞

---

## 评分机制

### 分组独立评分

国内域名和国外域名 **分开打分**（国内权重默认 0.7、国外 0.3）。很多 DNS 是「国内快国外慢」或反之，混在一起打分会掩盖真相。

### 两阶段流水线

1. **分组子分计算**：对每台 DNS 的国内/国外组分别计算速度分、路径分、稳定性分、就近分（均为 0~100）
2. **总分合成与结论判定**：
   ```
   总分 = cn_weight × 国内组得分 + global_weight × 国外组得分
         - 劫持扣分（60 分）- 污染扣分（25 分）
   ```
   根据总分和各分项情况，给出四档结论：**推荐 / 备用 / 限定分组 / 避免**

### 预设评分档位

通过 `--profile` / GUI 下拉框切换，调整四项权重：

| 档位 | 速度 | 路径 | 稳定 | 就近 | 适合场景 |
|---|---|---|---|---|---|
| `balanced` | 30 | 15 | 25 | 30 | 日常综合 |
| `speed` | 55 | 10 | 15 | 20 | 游戏/实时通信 |
| `stable` | 20 | 10 | 50 | 20 | 下载/串流 |
| `cdn` | 20 | 10 | 15 | 55 | 网站/CDN |
| `privacy` | 25 | 10 | 20 | 20 | 隐私优先 |

GUI 中可在设置面板拖动滑块自定义权重。

---

## 安装

需要 **Python 3.13.7+**。使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
# 桌面端：uv sync 会按 default-groups 一次装好 GUI + 开发工具
uv sync

# 只要命令行（OpenWrt / 无显示环境，不装 PySide6）
uv sync --no-group gui --no-group dev

# 启用 GeoIP（可选，需要 MaxMind mmdb）
uv sync
pip install maxminddb
```

### 平台兼容性

| 平台 | 命令行 | GUI | 备注 |
|---|---|---|---|
| Windows | ✅ | ✅ | 完整支持 |
| Linux | ✅ | ✅ | 完整支持 |
| OpenWrt | ✅（推荐） | ❌ | 不装 PySide6 即可，免拉 600MB GUI 栈 |
| macOS | ✅ | ✅ | 需自行处理 PySide6 安装 |

---

## 使用方法

### 桌面 GUI（推荐）

```bash
uv run dnspick-gui
# 或
dnspick-gui
```

GUI 提供：
- **运行配置仪表盘**：协议分布、域名配置摘要、一键启动/停止
- **设置面板**：DNS 服务器勾选、测量参数调整、评分权重自定义
- **进度面板**：实时显示各阶段进度
- **实时日志**：串流显示，日志等级可在设置中调整
- **优选历史**：Tab 化结果分析中心（结果总览 / 分组对比 / 分析建议）
- **SmartDNS 导出**：仅导出推荐服务器，受筛选条件和表格排序影响

### 命令行

```bash
# 基础测量
uv run dnspick run

# 指定评分档位
uv run dnspick run --profile speed

# 只看前 5 名
uv run dnspick run --top 5

# 只测加密协议
uv run dnspick run --protocol dot,doh

# 输出 JSON / CSV
uv run dnspick run --json result.json --csv result.csv

# 深度模式（完整 traceroute，慢）
uv run dnspick run --deep

# 自定义服务器清单
uv run dnspick run --servers my-servers.toml

# 启用 API 域名发现
uv run dnspick run --api-discovery

# 查看所有选项
uv run dnspick run --help
```

### 域名发现

自动收集你真正访问的域名（浏览器历史 + 内置种子库）：

```bash
uv run dnspick discover

# 从指定入口抓取外链域名
uv run dnspick discover --crawl https://example.com

# 尝试拉取在线 Top 榜单
uv run dnspick discover --online

# 只看浏览器历史
uv run dnspick discover --no-seed

# 限制输出数量
uv run dnspick discover --max 500
```

---

## 内置资源

- **30+ 条 DNS 上游**：覆盖阿里、腾讯、百度、114DNS、Cloudflare、Google、Quad9、AdGuard 等，每个厂商含 UDP + 可选 DoT/DoH 端点。默认只启用各厂商的 UDP 主力节点。
- **370+ 精选种子域名**：覆盖国内外主流网站/AxP/CDN，写入 SQLite 数据库统一管理，支持按类别筛选。
- **内置 GeoIP 判断**：离线 CN 网段表，不联网也能判断 IP 是否国内。

所有内置资源都可被外部 TOML 文件覆盖或追加。

---

## 项目结构

```
dnspick/
├── src/dnspick/
│   ├── gui/               # PySide6 GUI 层
│   │   ├── app.py         # GUI 入口
│   │   ├── main_window.py # 主窗口（运行配置仪表盘）
│   │   ├── settings.py    # 设置对话框（服务器/权重/参数）
│   │   ├── history.py     # 优选历史窗口（Tab 化分析中心）
│   │   ├── progress_panel.py  # 进度面板
│   │   ├── worker.py      # 测量 Worker（QThread 异步执行）
│   │   └── runner_config.py   # 配置 Signal 桩（UI 双向绑定）
│   ├── cli.py             # CLI 入口（Typer + Rich）
│   ├── runner.py          # 测量编排器（阶段调度）
│   ├── resolver.py        # DNS 解析矩阵（有界工作池 + 多协议）
│   ├── ipprobe.py         # IP 链路探测（TCP/ICMP/跳数/traceroute）
│   ├── scorer.py          # 评分引擎（分组打分 + 结论判定）
│   ├── discover.py        # 域名发现器（浏览器历史/种子库/在线）
│   ├── api_discovery.py   # API 域名自动发现（HTML + crt.sh）
│   ├── geoip.py           # GeoIP 归属地解析
│   ├── builtin.py         # 内置 DNS 服务器 + 探测域名
│   ├── config.py          # 配置加载/持久化
│   ├── models.py          # 数据模型（纯数据容器）
│   ├── store.py           # SQLite 结果存储
│   ├── domain_store.py    # 域名数据库
│   ├── relation_store.py  # 域名关联关系存储
│   └── log.py             # 全局日志管理（RotatingFile + Qt Signal）
├── resources/
│   ├── servers.toml       # 内置 DNS 服务器清单
│   └── seed_domains.txt   # 种子域名列表（首次运行写入 SQLite）
├── tests/                 # 单元测试（pytest）
├── doc/                   # 设计文档
├── scripts/               # 辅助脚本
└── pyproject.toml         # 项目元数据与依赖
```

---

## 运行测试

```bash
uv run pytest tests/ -v
```

## 代码检查

```bash
uv run ruff check
uv run ruff check --fix
```

---

## 许可证

MIT