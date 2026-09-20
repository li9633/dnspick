"""编排：把四个阶段串起来，并向调用方（CLI / GUI）汇报进度。

阶段A 域名收集（环境探测 + 域名加载 + API自动发现 + 去重）
阶段B DNS 测量（解析矩阵 + IP链路探测 + 归属地 + 评分与结论）
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import statistics
import time
import urllib.request
from collections.abc import Callable

from dnspick import builtin
from dnspick.api_discovery import ApiDomainDiscovery
from dnspick.config import Config
from dnspick.domain_store import DomainStore
from dnspick.geoip import GeoIpResolver
from dnspick.ipprobe import IpProber, is_ip
from dnspick.models import Group, IpProbeResult, ResolveRecord, RunResult, RunSummary
from dnspick.relation_store import RelationStore
from dnspick.resolver import DnsResolver, parse_ecs_subnet
from dnspick.scorer import Scorer, hijack_from_probes

# phase, done, total, message
ProgressCb = Callable[[str, int, int, str], None]

_PUBLIC_IP_SOURCES = (
    # 国内服务优先 —— VPN 分流场景下国内服务走直连，能拿到真实出口 IP
    "https://myip.ipip.net",
    "http://ip.3322.net",
    "https://ip.useragentinfo.com/json",
    # 国际服务兜底
    "http://ip-api.com/json/?fields=query",
    "https://api.ipify.org?format=json",
)


def _detect_public_ip(timeout: float = 3.0) -> str | None:
    """尽力而为地拿到出口公网 IP，拿不到就返回 None（ECS 对比会自动关闭）。"""
    for url in _PUBLIC_IP_SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        # ipip.net：文本中包含 IP 地址，按 token 提取
        if "ipip.net" in url:
            for token in text.replace("：", ":").split():
                if is_ip(token):
                    return token
            continue
        # 纯文本 IP（如 ip.3322.net 直接返回 "1.2.3.4"）
        stripped = text.strip()
        if is_ip(stripped):
            return stripped
        # JSON 格式
        try:
            data = json.loads(text)
        except ValueError:
            continue
        for key in ("query", "ip"):
            val = data.get(key)
            if isinstance(val, str) and is_ip(val):
                return val
    return None


def _to_subnet(ip: str) -> str | None:
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    except ValueError:
        return None
    return str(net)


class Runner:
    """测量编排器。GUI 通过 request_cancel() 中断。"""

    def __init__(self, config: Config, domain_store: DomainStore | None = None,
                 on_progress: ProgressCb | None = None) -> None:
        self.cfg = config
        self.dom_store = domain_store or DomainStore()
        self.on_progress = on_progress
        self._task: asyncio.Task | None = None
        self._cancel_pending = False

    def emit(self, phase: str, done: int, total: int, msg: str) -> None:
        if self.on_progress:
            self.on_progress(phase, done, total, msg)

    @staticmethod
    def _check_environment(ips: dict[str, IpProbeResult], summary: RunSummary) -> None:
        """识别「测了个寂寞」的环境。

        最典型的是透明代理：本机到任意 IP 的 TCP 连接都被本地网关秒回，
        于是所有 IP 延迟都在 0.1ms、跳数全是 1，各 DNS 之间的差异被彻底抹平。
        这种情况下评分仍然可用（反映的是解析可用性），但延迟/跳数不具区分度，
        必须明确告诉用户，否则他会拿着一张没有信息量的榜单去改 SmartDNS。
        """
        probes = [p for p in ips.values() if p.tcp_p50_ms is not None]
        if len(probes) < 5:
            return
        med = statistics.median(p.tcp_p50_ms for p in probes)  # type: ignore[misc]
        hops = [p.hop_count for p in probes if p.hop_count]
        one_hop_ratio = (sum(1 for h in hops if h <= 1) / len(hops)) if hops else 0.0
        if one_hop_ratio > 0.9 or med < 1.5:
            summary.warning = (
                "疑似透明代理/本地拦截：几乎所有 TCP 连接都在 1 跳内即时建立"
                f"（中位 {med:.2f}ms，1 跳占比 {one_hop_ratio:.0%}）。"
                "此时延迟与跳数不具区分度，评分主要反映解析可用性——"
                "请在直连家庭网络的环境（Windows 本机 / OpenWrt）重测。"
            )
        elif med > 150:
            summary.warning = (
                f"整体链路延迟偏高（中位 {med:.0f}ms），可能是网络拥塞或出口链路异常，建议换个时段重测。"
            )

    def request_cancel(self) -> None:
        self._cancel_pending = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def run(self) -> RunResult:
        self._task = asyncio.current_task()
        if self._cancel_pending:
            self._task.cancel()
            self._task = None
            raise asyncio.CancelledError()
        started = time.time()
        t = self.cfg.tunables

        # === 阶段A: 域名收集 ============================================
        summary = RunSummary(started_at=started)

        # A1. 环境探测
        self.emit("环境探测", 0, 1, "获取出口公网 IP")
        public_ip = await asyncio.to_thread(_detect_public_ip)
        summary.public_ip = public_ip
        ecs_subnet = _to_subnet(public_ip) if (public_ip and self.cfg.use_ecs) else None
        if ecs_subnet and parse_ecs_subnet(ecs_subnet) is None:
            ecs_subnet = None
        summary.ecs_subnet = ecs_subnet
        self.emit("环境探测", 1, 1, f"出口 IP: {public_ip or '未知'}")

        # A2. 从数据库加载启用的域名
        self.dom_store.init()
        domains = self.dom_store.get_enabled()

        servers = self.cfg.active_servers()
        summary.servers = len(servers)
        summary.domains = len(domains)

        # A3. API 域名自动发现（可选）
        if t.api_discovery_enabled:
            self.emit("API发现", 0, 1, "从主站首页 + 证书透明日志发现关联域名")
            store = RelationStore()
            discovery = ApiDomainDiscovery(
                store=store,
                cache_days=t.api_discovery_cache_days,
                html_timeout=t.api_discovery_html_timeout,
                crt_timeout=t.api_discovery_crt_timeout,
                max_per_source=t.api_discovery_max_per_source,
                min_cache=t.api_discovery_min_cache,
            )
            results = await discovery.discover_all(domains)
            extra = ApiDomainDiscovery.to_domain_specs(results, domains)
            if extra:
                domains = domains + extra
                summary.api_discovered = len(extra)
                self.emit(
                    "API发现", 1, 1,
                    f"发现 {len(extra)} 个关联域名，总域名 {len(domains)} 个",
                )
            else:
                self.emit("API发现", 1, 1, "未发现新关联域名")

        # A4. 阶段A 完成
        self.emit("域名收集", len(domains), len(domains),
                  f"共 {len(domains)} 个域名，开始 DNS 测量")

        # === 阶段B: DNS 测量 ============================================

        # B1. 解析矩阵
        resolver = DnsResolver(
            servers=servers,
            timeout=t.resolve_timeout,
            global_concurrency=t.resolve_concurrency,
            per_server_concurrency=t.per_server_concurrency,
            repeat=t.resolve_repeat,
            warmup=t.warmup,
            ecs_subnet=ecs_subnet,
            on_progress=lambda d, n, m: self.emit("DNS 解析", d, n, m),
        )
        records: list[ResolveRecord] = []
        records.extend(await resolver.run(domains))

        probes = builtin.probe_domains()
        records.extend(await resolver.run_probes(probes))

        # ECS 对比（可选，只对国内 CDN 组做，省时间）
        ecs_support: dict[str, bool | None] = {s.id: None for s in servers}
        if ecs_subnet:
            cn_doms = [d for d in domains if d.group is Group.CN][:5]
            ecs_total = len(servers)
            for ecs_done, server in enumerate(servers, 1):
                pairs = await resolver.ecs_compare(server, cn_doms)
                self.emit("ECS 对比", ecs_done, ecs_total, server.name)
                diffs = [a != b for a, b in pairs.values() if a and b]
                if diffs:
                    ecs_support[server.id] = sum(diffs) / len(diffs) > 0.3

        # B2. IP 探测
        unique_ips = sorted({ip for r in records if r.ok for ip in r.answers if is_ip(ip)})
        summary.unique_ips = len(unique_ips)
        self.emit("IP 探测", 0, len(unique_ips), f"去重后 {len(unique_ips)} 个 IP")

        prober = IpProber(
            ports=t.tcp_ports,
            tcp_repeat=t.tcp_repeat,
            tcp_timeout=t.tcp_timeout,
            tcp_concurrency=t.tcp_concurrency,
            hop_max=t.hop_max,
            hop_timeout=t.hop_timeout,
            icmp_enabled=t.icmp_enabled,
            icmp_count=t.icmp_count,
            icmp_concurrency=t.icmp_concurrency,
            deep_traceroute=t.deep_traceroute,
            on_progress=lambda d, n, m: self.emit("IP 探测", d, n, m),
        )
        ip_results = await prober.run(unique_ips)

        Runner._check_environment(ip_results, summary)
        if summary.warning:
            self.emit("环境检测", 1, 1, summary.warning)

        # B3. 归属地
        geo = GeoIpResolver(
            enabled=t.geoip_enabled,
            mmdb_path=self.cfg.geoip_db,
            extra_cn_prefixes=self.cfg.extra_cn_prefixes,
        )
        try:
            geos = await geo.resolve(unique_ips)
            for ip, g in geos.items():
                if ip in ip_results:
                    ip_results[ip].geo = g
        finally:
            geo.close()

        # B4. 评分
        self.emit("评分", 0, 1, "综合评分中")
        hijacks = hijack_from_probes(records)
        scorer = Scorer(t)
        reports = scorer.build(servers, records, ip_results, hijacks, ecs_support)
        self.emit("评分", 1, 1, "完成")

        summary.duration_s = round(time.time() - started, 1)
        return RunResult(
            reports=reports, ips=ip_results, records=records, summary=summary
        )