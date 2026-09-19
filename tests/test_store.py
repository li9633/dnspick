"""store.py 冒烟测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dnspick.models import (
    DnsServer,
    Group,
    GroupScore,
    Protocol,
    RunResult,
    RunSummary,
    ServerReport,
    Verdict,
)
from dnspick.store import ResultStore, ScoreFilter


def _make_fake_result() -> RunResult:
    sv_ali_udp = DnsServer(
        "alidns-1", "阿里 AliDNS", "223.5.5.5", Protocol.UDP, "alibaba", "cn",
        tags=("ecs", "cdn"),
    )
    sv_ali_doh = DnsServer(
        "alidns-doh", "阿里 DoH", "https://dns.alidns.com/dns-query",
        Protocol.DOH, "alibaba", "cn", tags=("ecs", "cdn", "encrypted"),
    )
    sv_ali_dot = DnsServer(
        "alidns-dot", "阿里 DoT", "223.5.5.5",
        Protocol.DOT, "alibaba", "cn", port=853, hostname="dns.alidns.com",
        tags=("ecs", "cdn", "encrypted"),
    )
    sv_cf = DnsServer(
        "cf-1", "Cloudflare", "1.1.1.1", Protocol.UDP, "cloudflare", "global",
        tags=("ecs", "anycast"),
    )
    sv_google = DnsServer(
        "google-1", "Google DNS", "8.8.8.8", Protocol.UDP, "google", "global",
        tags=("ecs", "anycast"),
    )

    def _cn(score, delay, hops):
        return GroupScore(
            group=Group.CN,
            resolve_success_rate=0.95,
            ip_reachable_rate=0.9,
            tcp_p50_ms=delay,
            hop_avg=hops,
            proximity=80.0,
            speed=85.0,
            path=75.0,
            stability=90.0,
            score=score,
        )

    def _gl(score, delay, hops):
        return GroupScore(
            group=Group.GLOBAL,
            resolve_success_rate=0.9,
            ip_reachable_rate=0.85,
            tcp_p50_ms=delay,
            hop_avg=hops,
            proximity=60.0,
            speed=40.0,
            path=45.0,
            stability=80.0,
            score=score,
        )

    reports = [
        ServerReport(
            server=sv_ali_udp, cn=_cn(88.5, 18.2, 8), glob=_gl(55.0, 245.0, 18),
            total=78.5, verdict=Verdict.RECOMMEND, advice="国内表现优秀",
            dns_rtt_p50_ms=5.2,
        ),
        ServerReport(
            server=sv_ali_doh, cn=_cn(84.3, 22.1, 8), glob=_gl(50.0, 260.0, 19),
            total=74.0, verdict=Verdict.RECOMMEND, advice="DoH 表现良好",
            dns_rtt_p50_ms=12.8,
        ),
        ServerReport(
            server=sv_ali_dot, cn=_cn(42.0, 85.7, 12), glob=_gl(35.0, 310.0, 22),
            total=38.0, verdict=Verdict.AVOID, advice="DoT 延迟过高",
            dns_rtt_p50_ms=60.0,
        ),
        ServerReport(
            server=sv_cf, cn=_cn(25.0, 85.0, 22), glob=_gl(96.0, 8.0, 8),
            total=45.2, verdict=Verdict.NARROW, advice="仅适合国外分组",
            dns_rtt_p50_ms=80.0,
        ),
        ServerReport(
            server=sv_google, cn=_cn(20.0, 200.0, 25), glob=_gl(90.0, 15.0, 10),
            total=41.0, verdict=Verdict.NARROW, advice="仅适合国外分组",
            dns_rtt_p50_ms=200.0,
        ),
    ]
    summary = RunSummary(
        public_ip="1.2.3.4", duration_s=45.2, servers=5, domains=32, unique_ips=48,
    )
    return RunResult(reports=reports, summary=summary)


def test_save_and_list():
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    result = _make_fake_result()
    mid = store.save(result, {"profile": "balanced", "deep": False})
    assert mid

    items = store.list_measurements()
    assert len(items) == 1
    assert items[0]["top_name"] == "阿里 AliDNS"
    assert items[0]["servers_cnt"] == 5


def test_get_scores_filter():
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    result = _make_fake_result()
    mid = store.save(result)

    scores = store.get_scores(mid, ScoreFilter(verdicts=["recommend"]))
    assert len(scores) == 2
    assert all(s["verdict"] == "recommend" for s in scores)

    scores = store.get_scores(mid, ScoreFilter(protocols=["udp"]))
    assert len(scores) == 3  # alidns-1, cf-1, google-1

    scores = store.get_scores(mid, ScoreFilter(regions=["global"]))
    assert len(scores) == 2
    assert all(s["region"] == "global" for s in scores)

    scores = store.get_scores(mid, ScoreFilter(tags=["cdn"]))
    assert len(scores) == 3  # alidns-1, alidns-doh, alidns-dot

    scores = store.get_scores(mid, ScoreFilter(min_score=70.0))
    assert len(scores) == 2
    assert all(s["total_score"] >= 70.0 for s in scores)

    scores = store.get_scores(
        mid, ScoreFilter(protocols=["udp", "doh"], regions=["cn"])
    )
    assert len(scores) == 2


def test_compare():
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    result = _make_fake_result()
    mid = store.save(result)

    groups = store.compare(mid, group_by="server_name", sub_group_by="protocol")
    # 5 个不同的 server_name，每个组只有 1 条记录
    assert len(groups) == 5

    # 按协议分组
    groups = store.compare(mid, group_by="protocol", sub_group_by="region")
    protocols = {g.key for g in groups}
    assert protocols == {"doh", "dot", "udp"}
    udp_group = next(g for g in groups if g.key == "udp")
    # UDP 有 cn 和 global 两个 region
    regions = {sg.key for sg in udp_group.sub_groups}
    assert regions == {"cn", "global"}

    # 带筛选
    groups = store.compare(
        mid, group_by="server_name", sub_group_by="protocol",
        filters=ScoreFilter(regions=["cn"]),
    )
    assert len(groups) == 3  # 只有国内的三台


def test_trend():
    import time
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    store.save(_make_fake_result())
    time.sleep(1.1)  # 避免同秒 ID 冲突
    store.save(_make_fake_result())
    trend = store.trend("alidns-1", "total_score")
    assert len(trend) == 2
    assert trend[0][1] == 78.5
    assert trend[1][1] == 78.5


def test_delete():
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    result = _make_fake_result()
    mid = store.save(result)
    assert len(store.list_measurements()) == 1

    store.delete(mid)
    assert len(store.list_measurements()) == 0

    # 级联删除：server_score 也应清空
    store.init()
    with store._conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM server_score").fetchone()[0]
        assert cnt == 0


def test_get_config():
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    cfg = {"profile": "speed", "deep": True, "ecs": True}
    mid = store.save(_make_fake_result(), cfg)
    loaded = store.get_config(mid)
    assert loaded == cfg


def test_migrate_from_json():
    store = ResultStore(Path(tempfile.mkdtemp()) / "test.db")
    # 迁移空的/不存在的文件不报错
    assert store.migrate_from_json(Path("/nonexistent/path.json")) == 0