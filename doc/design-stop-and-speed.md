# 停止速度与测量速度 —— 最终重构方案

## 1. 问题域

大规模测量场景（启用全部配置后 DNS 解析阶段约 112 万次查询），需要同时满足四个维度：

| 维度 | 含义 | 当前瓶颈 |
|---|---|---|
| 测量速度 | 整批跑完要多久 | 已通过 round-robin 投喂解决；剩余瓶颈在 ICMP 串行等待 |
| 停止速度 | 点停止后多快真正停 | 已优化到 O(1)（取消 24 worker），边际可改进空间小 |
| 安全性 | 内存/句柄/semaphore 不泄漏 | 有界工作池 + `async with` + `_drain_queue`，已安全 |
| 不引入数据竞争 | 阶段间依赖正确，结果可复现 | 阶段间天生串行，阶段内任务独立，无竞争 |

四个维度中，测量速度是唯一仍有余量且影响体验的关键指标。

## 2. 核心架构：有界工作池（已实现）

当前 `DnsResolver._run_pool()` 已将架构从"每 job 一个 task"切换为"N 个 worker + 1 个队列"：

```
producer ──put──→ Queue(maxsize=N×4) ──→ worker[0..N-1] ──→ query_once()
   ↑                                              ↑
   │ 取消时 cancel                                │ 取消时 cancel
   │ producer_task                                │ N 个 worker
```

**相比旧方案的优势**：

| | 旧（as_completed + 全量 task） | 新（有界工作池） |
|---|---|---|
| task 数量 | 112 万 | **N**（24） |
| task 对象内存 | ~2 GB | ~2 MB |
| 取消耗时 | cancel 112 万次 | cancel 24 次 |
| 取消失败概率 | 中（112 万次操作中任一异常） | 极低（24 次操作） |

## 3. 已实现的优化

### 3.1 Round-Robin 投喂 ✓（已实现）

**解决办法**：`_iter_jobs()` 和 `run_probes` 的迭代器均使用 **domain 优先（round-robin）** 投喂顺序：

```
[server0, dom0], [server1, dom0], ..., [server49, dom0]
[server0, dom1], [server1, dom1], ...
```

24 个 worker 获取 24 个不同服务器的 job，全部并行。总耗时 ≈ 单台服务器完成时间。

**改动位置**：[`resolver.py`](file:///c:/Programer/dnspick/src/dnspick/resolver.py) 中的 `_iter_jobs()` 和 `run_probes` 本地迭代器：

**收益估计**：50 台服务器场景下测量速度提升约 10 倍。

**风险评估**：零风险。job 之间的独立性不变，只改变执行顺序。

### 3.2 DNS 解析投喂顺序对 DNS 服务器的负载影响

**注意**：Round-robin 投喂会让 50 台 DNS 服务器同时被查询。如果被测 DNS 服务器有速率限制（如公共 DNS 限制每秒查询数），可能触发限流导致超时增多。

**缓解**：per_server_sem 已经限制每台服务器同时最多 2 个连接，不会瞬间打满。如果实际测试中出现超时增多，可以保持 round-robin 但降低 per_server_sem 或 global_sem。

## 4. 未实现的优化

### 4.1 ECS 对比阶段并行化（收益极小）

当前 B2 阶段逐个 server 串行执行：

```python
for server in servers:          # 50 台 server 串行
    await resolver.ecs_compare(server, cn_doms)   # 每台只有 5 域名 × 2 查询
```

每台 server 只需 5 域名 × 2（ECS/非ECS）= 10 次 DNS 查询，耗时约 2~4 秒。50 台串行耗时约 100~200 秒，不急但没必要。改为并行：

```python
ecs_tasks = [
    resolver.ecs_compare(server, cn_doms)
    for server in servers
]
results_list = await asyncio.gather(*ecs_tasks)
for server, pairs in zip(servers, results_list):
    # 合并结果
    ...
```

**收益**：ECS 对比耗时从 100~200 秒降到 2~4 秒。但因为 ECS 对比本身只占整轮测量的 5%~10%，整体收益有限。

## 5. 最终架构全景

```
Runner.run()
│
├─ A1  环境探测        串行 ─ 1 次 HTTP 请求
├─ A2  域名收集        串行 ─ 1 条 SQL 查询
├─ A3  API发现         并行 ─ asyncio.gather(每个主域名独立发现)
│
├─ B1  DNS解析 ─────── 并行 ─ 有界工作池
│   │   producer: round-robin 投喂
│   │   workers: N 个，共享 Queue
│   │   cancel: cancel producer + N worker + drain queue
│   │
│   ├─ B1.1  主域名解析 ── 同上
│   └─ B1.2  劫持探针   ── 同上
│
├─ B2  ECS对比        串行 ─ 逐个 server 对比（可并行优化）
│
├─ B3   IP探测
│   ├─ B3a TCP探测     并行 ─ as_completed / worker pool
│   └─ B3b ICMP探测    并行 ─ as_completed / worker pool（线程池）
│
├─ B4   归属地         串行 ─ 单次 GeoIP 批量查询
└─ B5   评分           串行 ─ 纯 CPU 计算
```

### 阶段间依赖

```
A1 → A2 → A3 → B1 → B2 → B3a → B3b → B4 → B5
        ↑     │     │     │
        │     │     │     └── B2 需要 B1 的结果
        │     │     └── B1 需要 A3 的完整域名列表
        │     └── A3 需要 A2 的主域名列表
        └── A2 不依赖 A1（但 A1 先跑是为了 ECS 子网推断）
```

所有阶段间都是严格串行的——这是数据依赖决定的，不可改变。

## 6. 停止路径

```
用户点击停止
  │
  ├─ main_window.on_stop()
  │     └─ worker.stop()
  │           ├─ requestInterruption()              # 通知 QThread 退出
  │           ├─ runner.request_cancel()            # cancel Runner 的 asyncio Task
  │           │     ├─ _task.cancel()               # cancel 当前运行的协程
  │           │     └─ _discovery.cancel()          # cancel API 发现（线程池阻塞 I/O）
  │           └─ _schedule_force_stop()             # 3s 安全网
  │
  ├─ Runner 的 Task 被 cancel
  │     ├─ 如果正在 _run_pool() 中：
  │     │     ├─ producer_task.cancel()            # 停止投喂
  │     │     ├─ for w in workers: w.cancel()      # cancel 24 worker
  │     │     ├─ _drain_queue(queue)                # 释放队列内存
  │     │     └─ raise CancelledError
  │     │
  │     ├─ 如果正在其他阶段（gather / IP probe）：
  │     │     └─ CancelledError 沿调用栈正常传播
  │     │
  │     └─ 如果正在 _check_cancel() 检查点：
  │           └─ 抛出 CancelledError
  │
  └─ worker.run() 捕获 CancelledError
        ├─ finally: cancel 安全网 TimerHandle
        ├─ finally: 跳过 shutdown_asyncgens()（force_stopped=True 时）
        ├─ finally: loop.close()
        └─ 发 failed 信号 → main_window.on_failed()
```

### 安全网

3 秒超时后若 worker 线程仍未结束，执行 `loop.stop()` 强制中断事件循环。此时：
- `run_until_complete()` 抛出 `RuntimeError`
- `finally` 跳过 `shutdown_asyncgens()`（避免异步生成器清理卡住）
- `loop.close()` 会取消所有 pending task，释放 transport 资源

此路径仅在实际代码有 bug（如无限等待的锁）时触发，正常路径下 cancel 24 worker 远快于 3 秒。

## 7. 不做的优化（及原因）

| 方案 | 原因 |
|---|---|
| 协议分离 pool（UDP/TCP 各一个池） | 当前大部分 DNS 是 UDP，24 并发够用。分离增加维护成本，收益不明确。 |
| TCP/ICMP 并行跑 | 两个阶段写同一个 `IpProbeResult` 对象，需要引入锁。当前串行更安全，且 IP 量远小于 DNS 量，不是瓶颈。 |
| C 扩展 / Rust 重写 DNS 查询 | dnspython 的 asyncquery 已经用 C 扩展做 DNS 封包。网络 I/O 是瓶颈，语言层面优化无效。 |
| 多进程 | 进程间共享 DNS 查询结果需要序列化/反序列化，开销高于单进程并发。 |

## 8. 阶段跳过统一 ✓（已实现）

### 8.1 现状（已修复）

当前可选阶段有两种跳过方式：

| 阶段 | 跳过方式 | 进度面板表现 |
|---|---|---|
| A3 API发现 | `if/else → _skip_stage("API发现")` | 显示 "API发现 · 已跳过" |
| B2 ECS对比 | `if/else → _skip_stage("ECS 对比")` | 显示 "ECS 对比 · 已跳过" |
| B3b ICMP探测 | `_skip_stage("ICMP 探测", ...)` 在 `prober.run()` 后显式调用 | ✓ "ICMP 与 traceroute 均未启用" |
| B3b 深 traceroute | `_run_blocking()` 内 `if` 判 | **静默消失**（不显示） |

ICMP 阶段已修复为显式跳过。深 traceroute 作为 ICMP 阶段内的子步骤，不启用时不会单独产生阶段提示（合理行为，因为 traceroute 不是一个独立阶段）。

### 8.2 实现结果

所有可选阶段统一使用显式 `_skip_stage()`，进度面板能展示完整的阶段时间线：

```
[环境探测] ✓ 0.5s
[域名收集] ✓ 0.1s
[API发现]   — 已跳过
[DNS 解析]  ✓ 120.3s
[ECS 对比]  — ECS 未启用
[TCP 探测]  ✓ 45.2s
[ICMP 探测] — ICMP 未启用
[归属地]    ✓ 2.1s
[评分]      ✓ 0.3s
```

每一行都有明确状态，用户一目了然。

### 8.3 实现方案（已在 `runner.py` 中完成）

改动集中在 `runner.py` 的 B3 阶段调度逻辑，在 `prober.run()` 之后判断是否需要显式跳过 ICMP 阶段：

```python
# 在 prober.run() 完成后
if not t.icmp_enabled and not t.deep_traceroute:
    self._skip_stage("ICMP 探测", "ICMP 与 traceroute 均未启用")
```

### 8.4 进度回调解耦（保持现状）

当前 `IpProber` 通过 `on_probe_progress` 回调向 Runner 报告进度并触发阶段切换（`_enter_stage`）。这意味着阶段变更逻辑分散在两处：

| 位置 | 职责 |
|---|---|
| `runner.py` 的 B3 代码块 | 调用 `prober.run()`，处理 IP 探测前/后的阶段切换 |
| `ipprobe.py` 的 `on_probe_progress` | 在探测过程中触发 "TCP 探测" → "ICMP 探测" 的切换 |

更好的做法是将阶段切换职责完全收归 Runner：

1. Runner 在 `prober.run()` 前后自行调用 `_enter_stage()`
2. `on_probe_progress` 只用于更新当前阶段的进度数值（done/total/msg），不再触发阶段切换
3. Runner 在 TCP 探测完成后调用 `_enter_stage("ICMP 探测")`（或 `_skip_stage`），再继续等待 `prober.run()` 完成 ICMP 部分

但考虑到 `prober.run()` 内部顺序执行 TCP → ICMP，且两者合并在一个 `await` 中，拆分会引入额外的复杂性。**P0 改动只需在 `prober.run()` 前后加显式 skip 判断**，不重构进度回调机制。

## 9. 改动清单

| 文件 | 改动 | 状态 |
|---|---|---|
| `resolver.py#_iter_jobs` | for 循环层级交换（server优先 → domain优先） | ✓ 已实现 |
| `resolver.py#run_probes` | 同上 | ✓ 已实现 |
| `runner.py#run` (B3 后) | ICMP/深 traceroute 禁用时显式 `_skip_stage()` | ✓ 已实现 |
| `runner.py#run` (B2) | ECS 对比从串行改为 asyncio.gather | 待实现 |
| `ipprobe.py` | 专用 ThreadPoolExecutor 替代默认线程池，worker 数与并发对齐 | ✓ 已实现 |

## 10. 测试验证

现有 104 个测试全部通过。Round-robin 投喂不改变 job 的集合（只是顺序不同），`run()` 和 `run_probes()` 的结果正确性不受影响：

- DnsResolver 的单元测试使用少数 server/domain 组合，投喂顺序对结果无影响
- 需要手工验证的是：大规模场景下停止按钮的响应延迟应 < 1 秒