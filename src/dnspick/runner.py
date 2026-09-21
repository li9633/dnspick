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
from dnspick.log import log
from dnspick.models import (
    Group,
    IpProbeResult,
    ResolveRecord,
    ResolveStatus,
    RunResult,
    RunSummary,
    StageProgress,
)
from dnspick.relation_store import RelationStore
from dnspick.resolver import DnsResolver, parse_ecs_subnet
from dnspick.scorer import Scorer, hijack_from_probes

_STAGES = [
    "环境探测",
    "域名收集",
    "API发现",
    "DNS 解析",
    "ECS 对比",
    "TCP 探测",
    "ICMP 探测",
    "归属地",
    "评分",
]

_STAGE_INDEX: dict[str, int] = {n: i for i, n in enumerate(_STAGES)}

ProgressCb = Callable[[StageProgress], None]

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
        self._stage_idx: int = -1
        self._stage_started: float = 0.0

    def _emit(self, name: str, done: int, total: int, msg: str, status: str = "running") -> None:
        if not self.on_progress:
            return
        idx = _STAGE_INDEX.get(name, 0)
        elapsed = time.time() - self._stage_started if self._stage_started else 0.0
        self.on_progress(StageProgress(
            stage=idx + 1, total_stages=len(_STAGES),
            stage_name=name, done=done, total=total,
            msg=msg, status=status, elapsed_s=elapsed,
        ))

    def _enter_stage(self, name: str) -> None:
        if self._stage_idx >= 0:
            # 发上一阶段的 done
            prev_name = _STAGES[self._stage_idx]
            duration = time.time() - self._stage_started if self._stage_started else 0.0
            self._emit(prev_name, 1, 1, f"完成 · 耗时 {duration:.1f}s", "done")
        self._stage_idx = _STAGE_INDEX.get(name, 0)
        self._stage_started = time.time()

    def _skip_stage(self, name: str, reason: str = "") -> None:
        self._enter_stage(name)
        self._emit(name, 0, 0, reason or "已跳过", "skipped")

    def _check_cancel(self) -> None:
        """在阶段之间检查取消标记，及时终止避免浪费计算。"""
        if self._cancel_pending:
            if self._task and not self._task.done():
                self._task.cancel()
            raise asyncio.CancelledError()

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
        self._enter_stage("环境探测")
        self._emit("环境探测", 0, 1, "获取出口公网 IP")
        public_ip = await asyncio.to_thread(_detect_public_ip)
        summary.public_ip = public_ip
        ecs_subnet = _to_subnet(public_ip) if (public_ip and self.cfg.use_ecs) else None
        if ecs_subnet and parse_ecs_subnet(ecs_subnet) is None:
            ecs_subnet = None
        summary.ecs_subnet = ecs_subnet
        self._emit("环境探测", 1, 1, f"出口 IP: {public_ip or '未知'}")
        log.info(f"出口公网 IP: {public_ip or '未知'}")

        # A2. 域名收集：从数据库加载启用的域名
        self._check_cancel()
        self._enter_stage("域名收集")
        self.dom_store.init()
        domains = self.dom_store.get_enabled()
        servers = self.cfg.active_servers()
        self._emit("域名收集", len(domains), len(domains),
                   f"共 {len(domains)} 个域名，{len(servers)} 台服务器")
        log.info(f"域名收集完成: {len(domains)} 个域名, {len(servers)} 台服务器")

        # A3. API 域名自动发现（可选）
        if t.api_discovery_enabled:
            self._enter_stage("API发现")
            self._emit("API发现", 0, 1, "从主站首页 + 证书透明日志发现关联域名")
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
                self._emit("API发现", 1, 1,
                           f"发现 {len(extra)} 个关联域名，总域名 {len(domains)} 个")
            else:
                self._emit("API发现", 1, 1, "未发现新关联域名")
        else:
            self._skip_stage("API发现")

        self._check_cancel()
        summary.servers = len(servers)
        summary.domains = len(domains)

        # === 阶段B: DNS 测量 ============================================

        # B1. DNS 解析
        self._enter_stage("DNS 解析")
        resolver = DnsResolver(
            servers=servers,
            timeout=t.resolve_timeout,
            global_concurrency=t.resolve_concurrency,
            per_server_concurrency=t.per_server_concurrency,
            repeat=t.resolve_repeat,
            warmup=t.warmup,
            ecs_subnet=ecs_subnet,
            on_progress=lambda d, n, m: self._emit("DNS 解析", d, n, m),
        )
        records: list[ResolveRecord] = []
        records.extend(await resolver.run(domains))

        probes = builtin.probe_domains()
        records.extend(await resolver.run_probes(probes))
        self._check_cancel()
        ok_count = sum(1 for r in records if r.ok)
        timeout_count = sum(1 for r in records if r.status == ResolveStatus.TIMEOUT)
        error_count = sum(1 for r in records if r.status == ResolveStatus.ERROR)
        log.info(
            f"DNS 解析完成: {len(records)} 条, {ok_count} 成功"
            + (f", {timeout_count} 超时" if timeout_count else "")
            + (f", {error_count} 错误" if error_count else "")
        )

        # B2. ECS 对比（可选）
        ecs_support: dict[str, bool | None] = {s.id: None for s in servers}
        if ecs_subnet:
            self._enter_stage("ECS 对比")
            cn_doms = [d for d in domains if d.group is Group.CN][:5]
            ecs_total = len(servers)
            for ecs_done, server in enumerate(servers, 1):
                pairs = await resolver.ecs_compare(server, cn_doms)
                self._emit("ECS 对比", ecs_done, ecs_total, server.name)
                diffs = [a != b for a, b in pairs.values() if a and b]
                if diffs:
                    ecs_support[server.id] = sum(diffs) / len(diffs) > 0.3
        else:
            self._skip_stage("ECS 对比", "ECS 未启用或无子网")

        self._check_cancel()
        # B3. IP 探测
        unique_ips = sorted({ip for r in records if r.ok for ip in r.answers if is_ip(ip)})
        summary.unique_ips = len(unique_ips)
        self._enter_stage("TCP 探测")
        self._emit("TCP 探测", 0, len(unique_ips), f"去重后 {len(unique_ips)} 个 IP")

        last_probe_phase = "TCP 探测"

        def on_probe_progress(phase: str, done: int, total: int, msg: str) -> None:
            nonlocal last_probe_phase
            if phase != last_probe_phase:
                self._enter_stage(phase)
                last_probe_phase = phase
            self._emit(phase, done, total, msg)

        prober = IpProber(
            ports=t.tcp_ports,
            tcp_repeat=t.tcp_repeat,
            tcp_timeout=t.tcp_timeout,
            tcp_concurrency=t.tcp_concurrency,
            hop_max=t.hop_max,
            hop_timeout=t.hop_timeout,
            icmp_enabled=t.icmp_enabled,
            icmp_count=t.icmp_count,
            icmp_timeout=t.icmp_timeout_s,
            icmp_concurrency=t.icmp_concurrency,
            deep_traceroute=t.deep_traceroute,
            on_progress=on_probe_progress,
        )
        ip_results = await prober.run(unique_ips)
        self._check_cancel()

        unreachable = sum(1 for r in ip_results.values() if r.unreachable)
        log.info(f"IP 探测完成: {len(ip_results)} 个 IP, {unreachable} 个不可达")

        Runner._check_environment(ip_results, summary)
        if summary.warning:
            self._emit("ICMP 探测", len(unique_ips), len(unique_ips), summary.warning)
            log.warn(summary.warning)

        # B4. 归属地
        self._enter_stage("归属地")
        geo = GeoIpResolver(
            enabled=t.geoip_enabled,
            mmdb_path=self.cfg.geoip_db,
            extra_cn_prefixes=self.cfg.extra_cn_prefixes,
        )
        try:
            self._emit("归属地", 0, 1, "查询 GeoIP 归属地")
            geos = await geo.resolve(unique_ips)
            for ip, g in geos.items():
                if ip in ip_results:
                    ip_results[ip].geo = g
            self._emit("归属地", 1, 1, f"完成 · {len(geos)} 个 IP")
        finally:
            geo.close()

        # B5. 评分
        self._enter_stage("评分")
        self._emit("评分", 0, 1, "综合评分中")
        hijacks = hijack_from_probes(records)
        scorer = Scorer(t)
        reports = scorer.build(servers, records, ip_results, hijacks, ecs_support)

        # 末阶段 done
        duration = time.time() - self._stage_started
        self._emit("评分", 1, 1, f"完成 · 耗时 {duration:.1f}s", "done")

        summary.duration_s = round(time.time() - started, 1)

        recommend = sum(1 for r in reports if r.verdict and r.verdict.value == "recommend")
        log.info(
            f"测量完成 · 耗时 {summary.duration_s}s · "
            f"{summary.servers} 台服务器 · {len(reports)} 份报告 · {recommend} 台推荐"
        )

        return RunResult(
            reports=reports, ips=ip_results, records=records, summary=summary
        )