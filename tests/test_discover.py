"""域名发现器单测。

重点覆盖两个踩过的坑：
    1. 分区注释两侧有等号，startswith 判不出来 → 国内外分组全错
    2. 分层失控，上千域名全挤在一层
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from dnspick.discover import (
    _classify,
    _host_of,
    build_report,
    load_seeds,
    mine_browser_history,
    write_toml,
)


def test_load_seeds_parses_sections() -> None:
    """分区注释两侧带等号，必须用 in 判断。"""
    seeds = load_seeds()
    assert len(seeds) > 300
    groups = {g for _n, g, _s in seeds}
    assert "cn" in groups
    assert "global" in groups


def test_seed_group_matches_section() -> None:
    """回归：en.wikipedia.org 曾在国外分区却被判成 cn。"""
    seeds = load_seeds()
    lookup = {n: g for n, g, _s in seeds}
    for dom in ("en.wikipedia.org", "www.google.com", "github.com"):
        if dom in lookup:
            assert lookup[dom] == "global", dom
    for dom in ("www.taobao.com", "www.baidu.com"):
        if dom in lookup:
            assert lookup[dom] == "cn", dom


def test_classify_prefers_cn_tld() -> None:
    assert _classify("www.gov.cn") == "cn"
    assert _classify("www.foo.com.cn", "global") == "cn"
    assert _classify("example.com", "global") == "global"
    assert _classify("example.com", "cn") == "cn"
    # 分区为空时，非 cn TLD 兜底为 global（不要把未知都塞进国内组）
    assert _classify("example.com", "") == "global"


def test_host_of_filters_noise() -> None:
    assert _host_of("https://www.taobao.com/path?q=1") == "www.taobao.com"
    # 注意别用 example.com —— 它在噪声名单里（浏览器/文档常用的占位域）
    assert _host_of("http://shop.example.org:8080/x") == "shop.example.org"
    assert _host_of("https://pagead2.googlesyndication.com/x") is None
    assert _host_of("localhost") is None
    assert _host_of("") is None


def test_host_of_rejects_invalid() -> None:
    assert _host_of("not a url") is None
    assert _host_of("192.168.1.1") is None  # 纯 IP 不是域名


def test_build_report_has_three_tiers() -> None:
    rep = build_report(use_browser=False, use_seed=True, tier2_size=200)
    assert len(rep.items) > 300
    t1, t2, t3 = (len(rep.by_tier(t)) for t in (1, 2, 3))
    assert t1 >= 20, "核心层太少，测不出 CDN 调度差异"
    assert t1 <= 120
    assert t2 <= 200, "常用层必须收口，否则 --tier 2 会跑上千域名"
    assert t3 > 0


def test_tier1_contains_core_domains() -> None:
    rep = build_report(use_browser=False, use_seed=True)
    t1 = {d.name for d in rep.by_tier(1)}
    for dom in ("www.taobao.com", "www.baidu.com", "www.google.com", "github.com"):
        assert dom in t1, dom


def test_report_groups_are_only_cn_or_global() -> None:
    rep = build_report(use_browser=False, use_seed=True)
    assert {d.group for d in rep.items} <= {"cn", "global"}


def test_write_toml_roundtrip(tmp_path: Path) -> None:
    rep = build_report(use_browser=False, use_seed=True)
    out = tmp_path / "domains.toml"
    n = write_toml(rep, out)
    assert n == len(rep.items)

    # 验证 TOML 文件能被正确解析，格式为 [domains] cn = [...] global = [...]
    import tomllib

    data = tomllib.loads(out.read_text(encoding="utf-8"))
    doms = data.get("domains", {})
    cn = doms.get("cn", [])
    gl = doms.get("global", [])
    assert len(cn) + len(gl) == n, f"{len(cn) + len(gl)} != {n}"
    for d in cn + gl:
        assert isinstance(d, str)


def test_write_toml_respects_max_items(tmp_path: Path) -> None:
    rep = build_report(use_browser=False, use_seed=True)
    out = tmp_path / "d.toml"
    assert write_toml(rep, out, max_items=50) == 50


def test_mine_browser_history_is_safe_when_absent() -> None:
    """没有浏览器 / 数据库被锁 / 平台不支持，都必须安静返回空而不是抛异常。"""
    got = mine_browser_history()
    assert isinstance(got, dict)
    assert all(isinstance(v, int) for v in got.values())


@pytest.mark.parametrize("name", ["www.taobao.com", "cdn.jsdelivr.net", "a-b.example.co.uk"])
def test_valid_domain_shape(name: str) -> None:
    assert _host_of(f"https://{name}/x") == name


def test_seeded_domains_are_dns_legal() -> None:
    """种子里不能混进非法域名，否则测量阶段会被 dnspython 拒绝。"""
    import re

    seeds = load_seeds()
    pat = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z]{2,63}$")
    bad = [n for n, _g, _s in seeds if not pat.match(n)]
    assert not bad, bad[:5]


def test_no_private_addresses_in_seeds() -> None:
    """域名列表不该出现裸内网 IP。"""
    seeds = load_seeds()
    for n, _g, _s in seeds:
        try:
            ipaddress.ip_address(n)
        except ValueError:
            continue
        pytest.fail(f"种子里出现了裸 IP: {n}")