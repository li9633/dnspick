"""阶段四：评分与结论。

核心口径 —— 对齐 SmartDNS 的 fastest-ip 语义：
它把上游返回的全部 IP 汇总、本地测速，只把最快的那个交给客户端。
所以我们对「某个 DNS 在某个域名上的体验值」取的是
**它返回的 IP 集合里最快的那个可达 IP**，再对同组域名取中位数。
直接用「该 DNS 返回所有 IP 的平均延迟」会低估返回多 IP 的 CDN 型 DNS。
"""

from __future__ import annotations

import statistics

from dnspick.config import Tunables
from dnspick.models import (
    DnsServer,
    Group,
    GroupScore,
    IpProbeResult,
    ResolveRecord,
    ResolveStatus,
    ServerReport,
    Verdict,
)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


def _speed_score(ms: float | None) -> float:
    """绝对延迟 → 0~100。<10ms 满分，>200ms 归零，中间对数衰减。"""
    if ms is None:
        return 0.0
    if ms <= 10:
        return 100.0
    if ms >= 200:
        return 0.0
    import math

    return _clamp(100.0 * (1.0 - math.log(ms / 10.0) / math.log(20.0)))


def _path_score(hops: float | None) -> float:
    if hops is None:
        return 0.0
    if hops <= 6:
        return 100.0
    if hops >= 20:
        return 0.0
    return _clamp(100.0 * (20.0 - hops) / 14.0)


def _relative_scores(
    raw: dict[str, float | None], min_span: float = 5.0
) -> dict[str, float]:
    """把一组原始值（越小越好）映射成 0~100 的相对分。

    用 p10/p90 作锚点而不是 min/max，避免单个离群值把其他人压成同一档。
    当所有值挤在 min_span 以内时说明区分度不足（例如中间有透明代理，
    所有连接都被本地网关秒回），此时退化为绝对延迟分，否则会出现
    「3.42ms 得 100 分、3.50ms 得 0 分」这种噪声放大。
    """
    vals = [v for v in raw.values() if v is not None]
    if len(vals) < 2:
        return {k: (100.0 if v is not None else 0.0) for k, v in raw.items()}
    lo = _pct(vals, 0.1)
    hi = _pct(vals, 0.9)
    span = hi - lo
    if span < min_span:
        return {k: (_speed_score(v) if v is not None else 0.0) for k, v in raw.items()}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if v is None:
            out[k] = 0.0
        else:
            out[k] = _clamp(100.0 * (hi - v) / span)
    return out


class Scorer:
    def __init__(self, tunables: Tunables) -> None:
        self.t = tunables
        self.t.normalize()

    # -- 单组打分 ---------------------------------------------------------
    def score_group(
        self,
        server: DnsServer,
        group: Group,
        records: list[ResolveRecord],
        ips: dict[str, IpProbeResult],
    ) -> GroupScore:
        gs = GroupScore(group=group)
        mine = [r for r in records if r.server_id == server.id and r.group is group]
        if not mine:
            return gs

        ok = [r for r in mine if r.ok]
        gs.resolve_success_rate = round(len(ok) / len(mine), 3)

        # 每个域名：取该 DNS 返回 IP 中最快的可达 IP（fastest-ip 口径）
        best_per_domain: list[float] = []
        hop_values: list[int] = []
        seen_ips: set[str] = set()
        reachable_ips: set[str] = set()
        for rec in ok:
            cand = [p for p in rec.answers if p in ips]
            if not cand:
                continue
            for ip in cand:
                seen_ips.add(ip)
            reach = [ips[ip] for ip in cand if not ips[ip].unreachable and ips[ip].tcp_p50_ms is not None]
            if reach:
                for r in reach:
                    reachable_ips.add(r.ip)
                fastest = min(r.tcp_p50_ms for r in reach if r.tcp_p50_ms is not None)
                best_per_domain.append(fastest)
                for r in reach:
                    if r.hop_count:
                        hop_values.append(r.hop_count)
                        break

        # 口径：可达 IP 数 / 该 DNS 返回过的 IP 数（IP 维度，别和域名数混着算）
        if seen_ips:
            gs.ip_reachable_rate = round(len(reachable_ips & seen_ips) / len(seen_ips), 3)
        if best_per_domain:
            gs.tcp_p50_ms = round(statistics.median(best_per_domain), 2)
        if hop_values:
            gs.hop_avg = round(statistics.fmean(hop_values), 1)

        tcp_rates = [
            ips[ip].tcp_success_rate for ip in seen_ips if ip in ips and not ips[ip].unreachable
        ]
        avg_tcp_rate = statistics.fmean(tcp_rates) if tcp_rates else 0.0
        gs.stability = round(
            _clamp(100.0 * (0.5 * gs.resolve_success_rate + 0.3 * gs.ip_reachable_rate + 0.2 * avg_tcp_rate)),
            1,
        )
        gs.speed = round(_speed_score(gs.tcp_p50_ms), 1)
        gs.path = round(_path_score(gs.hop_avg), 1)
        return gs

    # -- 污染判定 ---------------------------------------------------------
    @staticmethod
    def detect_pollution(
        records: list[ResolveRecord], ips: dict[str, IpProbeResult]
    ) -> bool:
        """国外域名解析出境内 IP —— 高度疑似污染。"""
        foreign = [r for r in records if r.group is Group.GLOBAL and r.ok]
        checked = 0
        domestic = 0
        for rec in foreign:
            for ip in rec.answers:
                probe = ips.get(ip)
                if probe is None or probe.geo is None or probe.geo.is_domestic is None:
                    continue
                checked += 1
                if probe.geo.is_domestic:
                    domestic += 1
        if checked < 5:
            return False
        return domestic / checked > 0.5

    # -- 汇总 -------------------------------------------------------------
    def build(
        self,
        servers: list[DnsServer],
        records: list[ResolveRecord],
        ips: dict[str, IpProbeResult],
        hijacks: dict[str, bool],
        ecs_support: dict[str, bool | None],
    ) -> list[ServerReport]:
        reports: list[ServerReport] = []
        cn_raw: dict[str, float | None] = {}
        gl_raw: dict[str, float | None] = {}
        by_server: dict[str, list[ResolveRecord]] = {}
        for rec in records:
            by_server.setdefault(rec.server_id, []).append(rec)

        for server in servers:
            cn = self.score_group(server, Group.CN, records, ips)
            glob = self.score_group(server, Group.GLOBAL, records, ips)
            cn_raw[server.id] = cn.tcp_p50_ms
            gl_raw[server.id] = glob.tcp_p50_ms
            rtts = [
                r.rtt_ms for r in records if r.server_id == server.id and r.rtt_ms is not None
            ]
            reports.append(
                ServerReport(
                    server=server,
                    cn=cn,
                    glob=glob,
                    hijack=hijacks.get(server.id, False),
                    ecs_supported=ecs_support.get(server.id),
                    dns_rtt_p50_ms=round(statistics.median(rtts), 2) if rtts else None,
                )
            )

        # 就近性用「同组内相对排名」表达：谁把用户调度到了更近的节点
        cn_prox = _relative_scores(cn_raw)
        gl_prox = _relative_scores(gl_raw)

        for rep in reports:
            rep.cn.proximity = round(cn_prox.get(rep.server.id, 0.0), 1)
            rep.glob.proximity = round(gl_prox.get(rep.server.id, 0.0), 1)
            # 国内 DNS 返回国内 CDN IP 给国外域名是正常的就近调度，
            # 不是污染。仅对境外 DNS 做此项检测。
            rep.cn.polluted = (
                self.detect_pollution(
                    [r for r in records if r.server_id == rep.server.id], ips
                )
                if rep.server.region != "cn"
                else False
            )
            for gs in (rep.cn, rep.glob):
                gs.score = round(
                    (
                        self.t.w_speed * gs.speed
                        + self.t.w_path * gs.path
                        + self.t.w_stability * gs.stability
                        + self.t.w_proximity * gs.proximity
                    )
                    / 100.0,
                    1,
                )
            self._finalize(rep, ips, by_server)

        return sorted(reports, key=lambda r: r.total, reverse=True)

    def _finalize(
        self,
        rep: ServerReport,
        ips: dict[str, IpProbeResult],
        records_by_server: dict[str, list[ResolveRecord]],
    ) -> None:
        t = self.t
        total = t.cn_weight * rep.cn.score + t.global_weight * rep.glob.score
        if rep.hijack:
            total -= t.hijack_penalty
        if rep.cn.polluted or rep.glob.polluted:
            total -= t.pollution_penalty
        rep.total = round(_clamp(total, 0.0, 100.0), 1)

        gap = rep.cn.score - rep.glob.score
        # 分组差异优先于总分：一台「国内 85 / 国外 20」的 DNS 总分会被拉到 65 分档，
        # 但它其实是 cn 组的绝佳主力。只看总分会把它误判成「备用」，而这恰恰是
        # 用户最想知道的信息——到底该把它放进哪个分组。
        if rep.hijack:
            rep.verdict = Verdict.AVOID
            rep.advice = "检测到 NXDOMAIN 劫持（返回了不该存在的解析结果），不建议作为上游"
        elif rep.cn.score >= 65 and rep.glob.score >= 60:
            rep.verdict = Verdict.RECOMMEND
            rep.advice = (
                f"国内 {rep.cn.tcp_p50_ms}ms / 国外 {rep.glob.tcp_p50_ms}ms，两组都稳，建议作为主力上游"
            )
        elif gap >= 25 and rep.cn.score >= 65:
            rep.verdict = Verdict.NARROW
            rep.advice = "国内解析优秀但国外偏弱，建议只放 cn 分组，另配一台做 overseas"
        elif gap <= -25 and rep.glob.score >= 60:
            rep.verdict = Verdict.NARROW
            rep.advice = "国外解析准确但国内调度一般，建议只放 overseas / 境外分组"
        elif rep.total >= 70 and rep.cn.score >= 60:
            rep.verdict = Verdict.RECOMMEND
            rep.advice = (
                f"国内 {rep.cn.tcp_p50_ms}ms / 国外 {rep.glob.tcp_p50_ms}ms，综合表现好，可作主力上游"
            )
        elif rep.total >= 55:
            rep.verdict = Verdict.FALLBACK
            rep.advice = "表现中规中矩，可作为备用上游"
        else:
            rep.verdict = Verdict.AVOID
            rep.advice = "综合表现不佳，不建议选用"

        rep.top_ips = self._top_ips(rep.server.id, records_by_server, ips)

    @staticmethod
    def _top_ips(
        server_id: str,
        records_by_server: dict[str, list[ResolveRecord]],
        ips: dict[str, IpProbeResult],
        limit: int = 3,
    ) -> list[tuple[str, float | None, str]]:
        """只取这台 DNS 真正返回过的 IP —— 展示别家的 IP 没有意义。"""
        seen: dict[str, IpProbeResult] = {}
        for rec in records_by_server.get(server_id, []):
            for ip in rec.answers:
                probe = ips.get(ip)
                if probe is not None and ip not in seen:
                    seen[ip] = probe
        cands = [
            (ip, p.tcp_p50_ms, p.geo.brief() if p.geo else "-")
            for ip, p in seen.items()
            if p.tcp_p50_ms is not None and not p.unreachable
        ]
        cands.sort(key=lambda x: x[1] or 1e9)
        return cands[:limit]


def hijack_from_probes(records: list[ResolveRecord]) -> dict[str, bool]:
    """随机子域探针：返回了 A 记录即判定劫持。"""
    out: dict[str, bool] = {}
    for rec in records:
        if rec.group is not Group.PROBE:
            continue
        bad = rec.answers and rec.status in (ResolveStatus.OK, ResolveStatus.ERROR)
        out[rec.server_id] = out.get(rec.server_id, False) or bool(bad)
    return out