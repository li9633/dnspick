"""阶段一：DNS 解析矩阵。

对「每台 DNS × 每个域名 × 重复 N 次」做查询，产出解析记录。

并发模型：有界工作池 + 任务队列。
  - 只创建 N 个 worker task（N = global_concurrency），内存固定
  - 生产者把 job tuple 喂入 asyncio.Queue
  - 消费者从队列拉任务执行，用 Semaphore 控制并发
  - 取消时：即刻清空队列 + cancel N 个 worker → O(1) 完成
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import struct
import time
from collections.abc import Callable, Iterator

import dns.asyncquery
import dns.edns
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdatatype

from dnspick.models import DnsServer, DomainSpec, Protocol, ResolveRecord, ResolveStatus

ProgressCb = Callable[[int, int, str], None]


def parse_ecs_subnet(subnet: str | None) -> tuple[str, int] | None:
    """把 "1.2.3.0/24" 拆成 (裸地址, 前缀长度)。

    dnspython 的 ECSOption(address, srclen) 只接受裸 IP —— 直接传 CIDR 字符串
    会让它在 af_for_address() 里解析失败并抛 ValueError，进而中断整轮测量。
    """
    if not subnet:
        return None
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None
    return str(net.network_address), net.prefixlen


def _make_query(name: str, rdtype: str, ecs_subnet: str | None = None) -> dns.message.Message:
    options = None
    parsed = parse_ecs_subnet(ecs_subnet)
    if parsed:
        address, srclen = parsed
        options = [dns.edns.ECSOption(address, srclen=srclen)]
    return dns.message.make_query(name, rdtype, use_edns=0, options=options)


def _ssl_context(server: DnsServer) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = bool(server.hostname)
    ctx.verify_mode = ssl.CERT_REQUIRED if server.hostname else ssl.CERT_NONE
    return ctx


async def _dispatch(
    server: DnsServer, q: dns.message.Message, timeout: float
) -> dns.message.Message:
    """按协议把查询发出去。"""
    if server.protocol is Protocol.UDP:
        return await dns.asyncquery.udp(q, server.address, timeout=timeout, port=server.port)
    if server.protocol is Protocol.TCP:
        return await dns.asyncquery.tcp(q, server.address, timeout=timeout, port=server.port)
    if server.protocol is Protocol.DOT:
        return await dns.asyncquery.tls(
            q,
            server.address,
            timeout=timeout,
            port=server.port,
            server_hostname=server.hostname,
            ssl_context=_ssl_context(server),
        )
    if server.protocol is Protocol.DOH:
        return await dns.asyncquery.https(q, server.address, timeout=timeout)
    raise ValueError(f"unsupported protocol: {server.protocol}")


def _status_of(resp: dns.message.Message) -> ResolveStatus:
    rc = resp.rcode()
    if rc == dns.rcode.NXDOMAIN:
        return ResolveStatus.NXDOMAIN
    if rc == dns.rcode.SERVFAIL:
        return ResolveStatus.SERVFAIL
    if rc == dns.rcode.REFUSED:
        return ResolveStatus.REFUSED
    if rc == dns.rcode.NOERROR:
        return ResolveStatus.OK
    return ResolveStatus.ERROR


def _extract(resp: dns.message.Message, rdtype: str) -> tuple[list[str], int | None]:
    ips: list[str] = []
    ttl: int | None = None
    want = dns.rdatatype.from_text(rdtype)
    for rrset in resp.answer:
        if rrset.rdtype != want:
            continue
        if ttl is None:
            ttl = rrset.ttl
        for item in rrset:
            ips.append(item.address if hasattr(item, "address") else item.to_text())
    return ips, ttl


def _drain_queue(queue: asyncio.Queue) -> None:
    """清空任务队列，释放 job tuple 占用的内存。"""
    while not queue.empty():
        try:
            queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            break


class DnsResolver:
    """解析矩阵的执行器。

    内部使用有界工作池：只创建 N = global_concurrency 个 worker task，
    通过 asyncio.Queue 分发任务。取消时只需 cancel N 个 worker + 清空队列。
    """

    def __init__(
        self,
        servers: list[DnsServer],
        timeout: float = 2.0,
        global_concurrency: int = 24,
        per_server_concurrency: int = 2,
        repeat: int = 3,
        warmup: bool = True,
        ecs_subnet: str | None = None,
        on_progress: ProgressCb | None = None,
    ) -> None:
        self.servers = servers
        self.timeout = timeout
        self.global_concurrency = global_concurrency
        self.per_server_concurrency = per_server_concurrency
        self.repeat = repeat
        self.warmup = warmup
        self.ecs_subnet = ecs_subnet
        self.on_progress = on_progress
        self._global_sem = asyncio.Semaphore(global_concurrency)
        self._server_sems = {
            s.id: asyncio.Semaphore(per_server_concurrency) for s in servers
        }
        self._done = 0

    # -- 单次查询 ---------------------------------------------------------
    async def query_once(
        self, server: DnsServer, domain: DomainSpec, rdtype: str = "A", ecs: bool = False
    ) -> ResolveRecord:
        subnet = self.ecs_subnet if ecs else None
        try:
            q = _make_query(domain.name, rdtype, subnet)
        except (ValueError, dns.exception.DNSException) as exc:
            return ResolveRecord(
                server.id,
                domain.name,
                domain.group,
                rdtype,
                ResolveStatus.ERROR,
                None,
                error=f"make_query: {type(exc).__name__}",
            )
        started = time.perf_counter()
        try:
            resp = await _dispatch(server, q, self.timeout)
            rtt = (time.perf_counter() - started) * 1000.0
            status = _status_of(resp)
            ips, ttl = _extract(resp, rdtype) if status is ResolveStatus.OK else ([], None)
            return ResolveRecord(
                server_id=server.id,
                domain=domain.name,
                group=domain.group,
                rdtype=rdtype,
                status=status,
                rtt_ms=round(rtt, 2),
                answers=ips,
                ttl=ttl,
                ecs=ecs,
            )
        except dns.exception.Timeout:
            return ResolveRecord(
                server.id, domain.name, domain.group, rdtype, ResolveStatus.TIMEOUT, None
            )
        except (dns.exception.DNSException, OSError, ssl.SSLError, ValueError, struct.error) as exc:
            return ResolveRecord(
                server.id,
                domain.name,
                domain.group,
                rdtype,
                ResolveStatus.ERROR,
                None,
                error=type(exc).__name__,
            )
        except Exception as exc:
            return ResolveRecord(
                server.id,
                domain.name,
                domain.group,
                rdtype,
                ResolveStatus.ERROR,
                None,
                error=type(exc).__name__,
            )

    def _tick(self, total: int, msg: str) -> None:
        self._done += 1
        if self.on_progress:
            self.on_progress(self._done, total, msg)

    # -- 通用工作池 -------------------------------------------------------
    async def _run_pool(
        self,
        job_iter: Iterator[tuple[DnsServer, DomainSpec, str, bool]],
        total: int,
        label_fn: Callable[[DnsServer, DomainSpec], str] | None = None,
    ) -> list[ResolveRecord]:
        """有界工作池：N 个 worker 从队列取任务，取消时即停。

        核心设计：
          - 只创建 global_concurrency 个 worker task（而非每个 job 一个 task）
          - 生产者把 job 喂入有界 Queue（默认容量 = concurrency × 4）
          - 取消时：cancel N 个 worker + 清空队列，不遍历任务列表
        """
        records: list[ResolveRecord] = []
        queue: asyncio.Queue[tuple[DnsServer, DomainSpec, str, bool]] = asyncio.Queue(
            maxsize=self.global_concurrency * 4,
        )

        async def worker() -> None:
            while True:
                try:
                    server, dom, rdtype, ecs = await queue.get()
                except asyncio.CancelledError:
                    break

                try:
                    server_sem = self._server_sems.get(server.id)
                    async with self._global_sem, (server_sem or _NULL_SEM):
                        rec = await self.query_once(server, dom, rdtype, ecs=ecs)
                    records.append(rec)
                    msg = label_fn(server, dom) if label_fn else f"{server.name} → {dom.name}"
                    self._tick(total, msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self.global_concurrency)]

        async def producer() -> None:
            for job in job_iter:
                await queue.put(job)

        producer_task = asyncio.create_task(producer())

        try:
            await producer_task
            await queue.join()
        except asyncio.CancelledError:
            producer_task.cancel()
            for w in workers:
                w.cancel()
            _drain_queue(queue)
            raise
        finally:
            for w in workers:
                if not w.done():
                    w.cancel()

        return records

    # -- 预热 -------------------------------------------------------------
    async def warmup_server(self, server: DnsServer, domain: DomainSpec) -> None:
        """先查一次丢弃：消除冷启动 / ARP / TLS 握手带来的首包污染。"""
        await self.query_once(server, domain, "A", ecs=False)

    # -- 主流程 -----------------------------------------------------------
    async def run(self, domains: list[DomainSpec]) -> list[ResolveRecord]:
        total = len(self.servers) * len(domains) * self.repeat
        if self.warmup:
            total += len(self.servers)
        self._done = 0

        if self.warmup and domains:
            warm_domain = domains[0]
            await asyncio.gather(
                *(self._warm(server, warm_domain, total) for server in self.servers)
            )

        return await self._run_pool(
            job_iter=self._iter_jobs(domains),
            total=total,
            label_fn=lambda s, d: f"{s.name} → {d.name}",
        )

    def _iter_jobs(
        self, domains: list[DomainSpec]
    ) -> Iterator[tuple[DnsServer, DomainSpec, str, bool]]:
        """生成器：惰性产出所有 job tuple，避免全量内存分配。

        投喂顺序：domain 优先（round-robin）。同一轮 repeat 内，
        所有 domain × server 交替产出，保证 worker pool 中每个 server
        都能同时被处理，避免 server 串行排队。
        """
        for i in range(self.repeat):
            ecs = bool(self.ecs_subnet) and i == 0
            for dom in domains:
                for server in self.servers:
                    yield (server, dom, "A", ecs)

    async def _warm(self, server: DnsServer, dom: DomainSpec, total: int) -> None:
        async with self._global_sem:
            await self.warmup_server(server, dom)
            self._tick(total, f"{server.name} 预热")

    # -- 劫持探针 ---------------------------------------------------------
    async def run_probes(self, probes: list[DomainSpec]) -> list[ResolveRecord]:
        """随机子域必须返回 NXDOMAIN；返回 A 记录即为劫持。"""
        total = len(self.servers) * len(probes)
        self._done = 0

        def _iter() -> Iterator[tuple[DnsServer, DomainSpec, str, bool]]:
            for dom in probes:
                for server in self.servers:
                    yield (server, dom, "A", False)

        return await self._run_pool(
            job_iter=_iter(),
            total=total,
            label_fn=lambda s, _d: f"{s.name} 劫持探测",
        )

    # -- ECS 对比 ---------------------------------------------------------
    async def ecs_compare(
        self, server: DnsServer, domains: list[DomainSpec]
    ) -> dict[str, tuple[list[str], list[str]]]:
        """同一域名，带 ECS 与不带 ECS 各查一次，看返回 IP 是否不同。"""
        out: dict[str, tuple[list[str], list[str]]] = {}
        if not self.ecs_subnet:
            return out

        async def one(dom: DomainSpec) -> None:
            async with self._global_sem, self._server_sems[server.id]:
                plain = await self.query_once(server, dom, "A", ecs=False)
                ecs = await self.query_once(server, dom, "A", ecs=True)
            if plain.ok or ecs.ok:
                out[dom.name] = (plain.answers, ecs.answers)

        tasks = [asyncio.create_task(one(d)) for d in domains]
        try:
            for coro in asyncio.as_completed(tasks):
                await coro
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise
        return out


_NULL_SEM = asyncio.Semaphore(99999)