# dnspick

**为家庭网络挑最优 DNS 上游的测量器。** 只做测量与建议，不改任何系统设置——
结果你自己填进 OpenWrt 的 SmartDNS。

## 它到底在测什么

一句话：**不看 DNS 回答得多快，看它给你的 IP 有多好。**

DNS 自身的响应延迟在家庭场景几乎没意义——本地一缓存就被抹平了。真正影响体验的是：
它把淘宝解析到山东联通节点，还是解析到了北京、上海甚至海外。

所以流程是两段式的：

```
阶段0  环境探测       出口公网 IP / ECS 子网
阶段1  解析矩阵       每台 DNS × 每个域名 × N 次 → IP 列表 + RTT
阶段2  IP 链路探测    去重后的 IP 池 → TCP 握手 / 跳数 / ICMP / traceroute
阶段3  归属地         GeoIP（可降级为未知）
阶段4  评分与结论     分组打分 + 可直接抄进 SmartDNS 的建议
```

### 评分口径对齐 SmartDNS 的 fastest-ip

SmartDNS 会把上游返回的全部 IP 汇总、本地测速，只把最快的那个交给客户端。
所以我们对「某 DNS 在某域名上的体验值」取的是
**它返回的 IP 集合里最快的那个可达 IP**，再对同组域名取中位数。
直接算「该 DNS 返回所有 IP 的平均延迟」会低估返回多 IP 的 CDN 型 DNS——那是错的口径。

### 四项判据

| 维度 | 怎么测 | 为什么重要 |
|---|---|---|
| 就近性 | 国内 CDN 域名解析出的 IP，TCP 延迟在同组内的相对排名 | 决定打开淘宝/抖音的实际速度 |
| 可达性 | 对这些 IP 做 TCP 443 握手，取 p50 与成功率 | IP 解析对了但链路绕路，一样慢 |
| 路径 | 跳数（TCP TTL 递增法，免权限） | 看是否绕路 |
| 干净度 | 随机子域必须 NXDOMAIN；国外域名返回境内 IP 即判污染 | 污染比慢更致命 |

国内外**分组评分**（国内权重默认 0.7、国外 0.3）。很多 DNS 是「国内快国外慢」或反之，
混在一起打分会掩盖真相。

## 安装

需要 **Python 3.13.7+**。

```bash
# 桌面端：uv sync 会按 default-groups 一次装好 GUI + 开发工具
uv sync

# 只要命令行（OpenWrt / 无显示环境，不装 PySide6）
uv sync --no-group gui
```

若机器上还没有 3.13.7+，让 uv 自己拉一个：

```bash
uv python install 3.13
uv python pin 3.13      # 写入 .python-version，之后 uv sync 自动用它
```

> `uv sync` 不需要先 `uv venv`，它会自己建 `.venv` 并生成 `uv.lock`。
> PySide6 放在 `dependency-groups` 而不是 `optional-dependencies`：
> 分组不进入包的分发元数据，因此在 OpenWrt 上 `uv sync --no-group gui`
> 不会拉那 600MB 的 GUI 栈。

## 命令行

```bash
# 默认档位跑一次
dnspick run

# 只关心 CDN 调度质量
dnspick run --profile cdn --top 5

# 只测 UDP，自定义域名集，存 JSON
dnspick run --protocol udp --domains resources/domains.toml --json out.json

# 深度模式：对每个 IP 跑完整 traceroute（慢很多）
dnspick run --deep

# 启用 ECS 对比查询（会先探测公网出口 IP）
dnspick run --ecs

# 查看内置清单（--all 显示含默认关闭的加密端点）
dnspick list-servers --all
```

档位：`balanced`（默认）/ `speed` / `stable` / `cdn` / `privacy`。

## 图形界面

```bash
dnspick-gui
```

- 结果表格可点列排序，点行看该 DNS 的分项明细
- 设置里勾选参与测量的 DNS、调权重滑块与超时/并发
- 「深度 traceroute」和「ECS 对比」默认关闭，按需开启
- 结论区直接给出可抄进 SmartDNS 的建议 + 各 DNS 最快 IP 的归属地

## 三个平台

| 平台 | 说明 |
|---|---|
| Windows | `dnspick-gui` 完整可用。TCP 握手与跳数探测免管理员；完整 traceroute 走系统 `tracert`，无需提权 |
| Linux | CLI 完整可用；GUI 装 PySide6 后可用。ICMP 需要 `ping_group_range` 或 root，否则自动跳过 |
| OpenWrt | 跑 CLI（`uv pip install -e .`）。ICMP/traceroute 若镜像里没有会自动跳过，不影响主结论 |

## 并发模型

- **海量 I/O（DNS 查询、TCP 握手、TTL 跳数探测）→ asyncio**。全局 semaphore 限流，
  并单独限制单台 server 内的并发——几十个请求互相排队，测出来的是排队时间不是网络延迟。
- **必须阻塞的系统调用（ICMP、traceroute、GeoIP 批量查询）→ ThreadPoolExecutor**。
  通过 `asyncio.to_thread` / `run_in_executor` 调度，不阻塞事件循环。
- **GUI** 用 `QThread` + `asyncio.run()` 桥接，不合并 Qt 与 asyncio 的事件循环。

跳数用**并发 TTL 探测**而不是逐跳串行：把 ttl=1..30 的连接一次性并发发出，
最小成功 ttl 即跳数，耗时从 O(N×timeout) 降到约 1 个 timeout。
它不收 ICMP、不用 raw socket，因此全平台免管理员权限。

## 域名集：1000+ 域名与自动发现

`resources/domains.toml` 内置 **1040 个分层域名**，覆盖国内外主流站点。

分层是为了解决一个实际问题：**域名不是越多越好**。长尾站点几乎都解析到
固定的几个 IP，对「哪个 DNS 更好」没有区分度，只会稀释信号并把测量时间拉长。

| tier | 数量 | 说明 |
|---|---|---|
| 1 核心 | 45 | CDN 调度最敏感 + 最常访问的站点。**默认只跑这层** |
| 2 常用 | 300 | 主流站点，想看更全面的国内外表现时用 `--tier 2` |
| 3 长尾 | 695 | 自动发现的长尾域名，默认不跑 |

```bash
dnspick run                 # tier1，45 个域名，几分钟出结果
dnspick run --tier 2        # 345 个域名，更全面但更慢
```

### 自动发现

```bash
dnspick discover                          # 浏览器历史 + 内置种子 → domains.toml
dnspick discover --no-browser             # 只要内置种子
dnspick discover --online                 # 额外拉 Tranco/Majestic 榜单（需外网）
dnspick discover --crawl www.taobao.com   # 从入口页抓外链域名
dnspick discover --tier1 100 --tier2 500  # 调整分层规模
```

三个来源按价值排序：

1. **浏览器历史（最有价值）** —— 挖的是你自己真正访问过的域名。
   1000 个别人列表里的域名，不如 200 个你家每天真打开的域名有说服力。
   支持 Chrome / Edge / Firefox，只读历史数据库不上传任何数据。
2. **内置种子库** `resources/seed_domains.txt` —— 1064 个精选真实域名。
3. **在线榜单 / 入口爬虫**（可选）—— 需要外网，失败自动跳过。

种子库按分区注释自动判定国内/国外，域名按 TLD 与分区双重复核归入 `cn` / `global` 组。

## 自定义

- `resources/servers.toml` —— 追加自己的 DNS（家里的 SmartDNS、运营商 DNS）
- `resources/seed_domains.txt` —— 域名种子库，`discover` 的原料
- `--cn-list cn.txt` —— 导入完整中国 IP 段列表，提升境内/境外判定准确率

内置约 29 条上游（国内：阿里/腾讯 DNSPod/114/百度/360/CNNIC/华为云等；
国外：Cloudflare/Google/Quad9/AdGuard/OpenDNS/DNS.SB/ControlD），
并附带各家的 DoT/DoH 端点（默认关闭，需手工开启）。

## 需要知道的限制

1. **GeoIP 是解释性信息，不是评分的必要输入。** 精确的省市/运营商要靠 GeoIP 库，
   离线库未必齐备、在线 API 未必可达。所以真正的就近性由 TCP 延迟与跳数这些**直接观测量**
   表达，归属地拿不到就降级为未知，不影响结论成立。内置网段表只覆盖常见段，命中可信、未命中未知。
2. **透明代理会让测速失去意义。** 如果本机到任意 IP 的 TCP 都被本地网关秒回
   （所有 IP 延迟都 ~0.1ms、跳数全是 1），程序会明确给出环境提示——此时评分只反映解析可用性，
   请在直连家庭网络的环境重测。企业网络/部分虚拟机/容器里常见这种情况。
3. **TCP 443 不可达 ≠ IP 不可用。** 有些 CDN 边缘节点不响应裸握手。程序会依次尝试 443 → 80 → 8443，
   仍不可达才标记 unreachable。
4. **DNS 污染判定需要归属地数据。** 归属地缺失时该项自动跳过，不会误判。
5. 只测解析与链路，**不做**广告拦截、家长控制、系统 DNS 写入、SmartDNS 配置生成。

## 目录结构

```
src/dnspick/
├── models.py      数据模型（纯数据，无 IO）
├── builtin.py     内置 DNS 清单 / 域名集 / 网段表
├── config.py      TOML 覆盖 + 测量参数与评分权重
├── resolver.py    阶段1 解析矩阵（asyncio）
├── ipprobe.py     阶段2 TCP / 跳数 / ICMP / traceroute
├── geoip.py       阶段3 归属地（mmdb → 在线 → 内置）
├── scorer.py      阶段4 评分与结论
├── runner.py      编排 + 环境可信度检测
├── cli.py         typer + rich
└── gui/           PySide6（worker 用 QThread 桥 asyncio）
```

核心引擎零 GUI 依赖，CLI 与 GUI 共用同一套。

## 开发

```bash
uv run pytest
uv run ruff check src tests scripts
uv run python scripts/smoke.py     # 真实网络小样本端到端

# 需要更精确的归属地时，装可选依赖并指定 mmdb
uv sync --extra geoip
dnspick run --geoip-db /path/to/City.mmdb
```
