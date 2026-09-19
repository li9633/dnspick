"""评分逻辑单测（不依赖网络）。"""

from __future__ import annotations

from dnspick.config import Tunables
from dnspick.models import (
    DnsServer,
    Group,
    GroupScore,
    IpProbeResult,
    Protocol,
    ResolveRecord,
    ResolveStatus,
    ServerReport,
    Verdict,
)
from dnspick.scorer import Scorer, _relative_scores, _speed_score, hijack_from_probes


def _server(sid: str = "a") -> DnsServer:
    return DnsServer(sid, "测试DNS", "1.1.1.1", Protocol.UDP, "test", "cn")


def test_speed_score_monotonic() -> None:
    assert _speed_score(5) == 100.0
    assert _speed_score(None) == 0.0
    assert _speed_score(300) == 0.0
    assert _speed_score(20) < _speed_score(10)
    assert _speed_score(100) < _speed_score(20)


def test_relative_scores_degenerate_when_no_spread() -> None:
    """所有值挤在一起时不能把噪声放大成 100 分 vs 0 分。"""
    raw = {"a": 3.42, "b": 3.45, "c": 3.50}
    out = _relative_scores(raw)
    assert min(out.values()) > 80.0, out
    assert max(out.values()) <= 100.0


def test_relative_scores_spreads_when_real_difference() -> None:
    raw = {"fast": 8.0, "mid": 40.0, "slow": 150.0}
    out = _relative_scores(raw)
    assert out["fast"] > out["mid"] > out["slow"]
    assert out["slow"] < 20.0


def test_relative_scores_handles_none() -> None:
    out = _relative_scores({"a": 10.0, "b": None, "c": 90.0})
    assert out["b"] == 0.0


def test_hijack_detection() -> None:
    """随机子域返回了 A 记录就是劫持。"""
    records = [
        ResolveRecord("a", "x.nxprobe.invalid", Group.PROBE, "A", ResolveStatus.NXDOMAIN, 10.0),
        ResolveRecord("b", "x.nxprobe.invalid", Group.PROBE, "A", ResolveStatus.OK, 10.0,
                      answers=["1.2.3.4"]),
    ]
    got = hijack_from_probes(records)
    assert got["a"] is False
    assert got["b"] is True


def test_ip_reachable_rate_is_ip_dimensional() -> None:
    """可达率分母必须是 IP 数，不能拿域名数除以 IP 数。"""
    ips = {
        "10.0.0.1": IpProbeResult(ip="10.0.0.1", tcp_p50_ms=5.0, hop_count=6),
        "10.0.0.2": IpProbeResult(ip="10.0.0.2", tcp_p50_ms=9.0, hop_count=7),
        "10.0.0.3": IpProbeResult(ip="10.0.0.3", unreachable=True),
    }
    records = [
        ResolveRecord("a", "d1.com", Group.CN, "A", ResolveStatus.OK, 5.0,
                      answers=["10.0.0.1", "10.0.0.2"]),
        ResolveRecord("a", "d2.com", Group.CN, "A", ResolveStatus.OK, 5.0,
                      answers=["10.0.0.3"]),
    ]
    gs = Scorer(Tunables()).score_group(_server(), Group.CN, records, ips)
    assert abs(gs.ip_reachable_rate - 2 / 3) < 1e-3
    # fastest-ip 口径：d1 两个 IP 取最快的 5.0；d2 唯一 IP 不可达故不计入
    assert gs.tcp_p50_ms == 5.0


def test_fastest_ip_semantics() -> None:
    """返回多 IP 的 DNS 应按最快的那个计分，而不是平均。"""
    ips = {
        "10.0.0.1": IpProbeResult(ip="10.0.0.1", tcp_p50_ms=100.0, hop_count=15),
        "10.0.0.2": IpProbeResult(ip="10.0.0.2", tcp_p50_ms=5.0, hop_count=6),
    }
    records = [
        ResolveRecord("a", "cdn.com", Group.CN, "A", ResolveStatus.OK, 5.0,
                      answers=["10.0.0.1", "10.0.0.2"]),
    ]
    gs = Scorer(Tunables()).score_group(_server(), Group.CN, records, ips)
    assert gs.tcp_p50_ms == 5.0


def test_hijacked_server_is_avoided() -> None:
    cn = GroupScore(Group.CN, score=90.0)
    gl = GroupScore(Group.GLOBAL, score=90.0)
    rep = ServerReport(_server(), cn, gl)
    scorer = Scorer(Tunables())
    scorer._finalize(rep, {}, {})
    assert rep.verdict is Verdict.RECOMMEND
    rep.hijack = True
    scorer._finalize(rep, {}, {})
    assert rep.verdict is Verdict.AVOID
    assert "劫持" in rep.advice


def test_narrow_verdict_when_only_one_group_good() -> None:
    cn = GroupScore(Group.CN, score=85.0)
    gl = GroupScore(Group.GLOBAL, score=20.0)
    rep = ServerReport(_server(), cn, gl)
    Scorer(Tunables())._finalize(rep, {}, {})
    assert rep.verdict is Verdict.NARROW
    assert "cn" in rep.advice