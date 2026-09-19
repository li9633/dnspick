"""阶段一：DNS 解析矩阵。

对「每台 DNS × 每个域名 × 重复 N 次」做查询，产出解析记录。

并发模型：asyncio。全局 semaphore 限流，同时限制单台 server 内的并发——
否则几十个请求互相排队，测出来的是排队时间而不是网络延迟。
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import struct
import time
from collections.abc import Callable

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


class DnsResolver:
    """解析矩阵的执行器。"""

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
            # 构造失败（域名不合法、ECS 参数异常等）只记一条 ERROR，不能中断整轮测量
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
            # 兜底：httpx.RemoteProtocolError 等传输层异常不应中断整轮测量
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

    # -- 预热 -------------------------------------------------------------
    async def warmup_server(self, server: DnsServer, domain: DomainSpec) -> None:
        """先查一次丢弃：消除冷启动 / ARP / TLS 握手带来的首包污染。"""
        await self.query_once(server, domain, "A", ecs=False)

    # -- 主流程 -----------------------------------------------------------
    async def run(self, domains: list[DomainSpec]) -> list[ResolveRecord]:
        records: list[ResolveRecord] = []
        jobs: list[tuple[DnsServer, DomainSpec, int, bool]] = []
        for server in self.servers:
            for i in range(self.repeat):
                for dom in domains:
                    jobs.append((server, dom, i, bool(self.ecs_subnet) and i == 0))

        total = len(jobs) + (len(self.servers) if self.warmup else 0)
        self._done = 0

        async def do(server: DnsServer, dom: DomainSpec, ecs: bool) -> None:
            async with self._global_sem, self._server_sems[server.id]:
                rec = await self.query_once(server, dom, "A", ecs=ecs)
                records.append(rec)
                self._tick(total, f"{server.name} → {dom.name}")

        if self.warmup and domains:
            warm_domain = domains[0]
            await asyncio.gather(
                *(self._warm(server, warm_domain, total) for server in self.servers)
            )

        await asyncio.gather(*(do(s, d, e) for s, d, _i, e in jobs))
        return records

    async def _warm(self, server: DnsServer, dom: DomainSpec, total: int) -> None:
        async with self._global_sem:
            await self.warmup_server(server, dom)
            self._tick(total, f"{server.name} 预热")

    # -- 劫持探针 ---------------------------------------------------------
    async def run_probes(self, probes: list[DomainSpec]) -> list[ResolveRecord]:
        """随机子域必须返回 NXDOMAIN；返回 A 记录即为劫持。"""
        records: list[ResolveRecord] = []
        total = len(self.servers) * len(probes)
        self._done = 0

        async def do(server: DnsServer, dom: DomainSpec) -> None:
            async with self._global_sem, self._server_sems[server.id]:
                rec = await self.query_once(server, dom, "A")
                records.append(rec)
                self._tick(total, f"{server.name} 劫持探测")

        await asyncio.gather(*(do(s, d) for s in self.servers for d in probes))
        return records

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

        await asyncio.gather(*(one(d) for d in domains))
        return out