"""解析层单测：重点是 ECS 参数构造（曾因传 CIDR 字符串导致整轮测量崩溃）。"""

from __future__ import annotations

import asyncio

import dns.message
import pytest

from dnspick.models import DnsServer, DomainSpec, Group, Protocol, ResolveStatus
from dnspick.resolver import DnsResolver, _make_query, parse_ecs_subnet


def test_parse_ecs_subnet_ipv4() -> None:
    assert parse_ecs_subnet("1.2.3.0/24") == ("1.2.3.0", 24)
    assert parse_ecs_subnet("1.2.3.4/32") == ("1.2.3.4", 32)


def test_parse_ecs_subnet_ipv6() -> None:
    addr, plen = parse_ecs_subnet("2001:db8::/56") or (None, None)
    assert plen == 56
    assert ":" in addr


def test_parse_ecs_subnet_rejects_garbage() -> None:
    """非法输入返回 None，绝不抛异常——否则会中断整轮测量。"""
    assert parse_ecs_subnet("不是IP") is None
    assert parse_ecs_subnet("999.999.999.999/24") is None
    assert parse_ecs_subnet("") is None
    assert parse_ecs_subnet(None) is None


def test_make_query_accepts_cidr_without_raising() -> None:
    """回归测试：dnspython 的 ECSOption 不接受 CIDR，曾直接抛 ValueError。"""
    q = _make_query("www.taobao.com", "A", "1.2.3.0/24")
    assert isinstance(q, dns.message.Message)
    opt = q.options[0]
    assert opt.otype == 8 or opt.otype.name == "ECS"
    assert opt.address == "1.2.3.0"
    assert opt.srclen == 24


def test_make_query_without_ecs_has_no_option() -> None:
    q = _make_query("www.taobao.com", "A", None)
    assert not q.options


def _server() -> DnsServer:
    return DnsServer("t", "测试", "223.5.5.5", Protocol.UDP, "test", "cn")


def _domain() -> DomainSpec:
    return DomainSpec("www.taobao.com", Group.CN)


def test_query_once_survives_bad_ecs_subnet() -> None:
    """ECS 子网非法时应降级为不带 ECS 的查询，而不是抛异常。"""
    srv = _server()
    resolver = DnsResolver([srv], ecs_subnet="不是IP", timeout=2.0)

    async def go():
        return await resolver.query_once(srv, _domain(), "A", ecs=True)

    rec = asyncio.run(go())
    assert rec.status in (ResolveStatus.OK, ResolveStatus.TIMEOUT, ResolveStatus.ERROR)
    assert not rec.answers or all(a.count(".") == 3 for a in rec.answers)


def test_query_once_returns_error_record_on_malformed_domain() -> None:
    """域名不合法时返回 ERROR 记录，不能让 gather 里的一颗老鼠屎坏一锅粥。"""
    srv = _server()
    resolver = DnsResolver([srv], timeout=2.0)
    # 空字符串会被 dnspython 当成根域 "." 而正常通过；用超长标签才能触发构造异常
    bad = DomainSpec("x" * 64 + ".com", Group.CN)

    async def go():
        return await resolver.query_once(srv, bad, "A")

    rec = asyncio.run(go())
    assert rec.status is ResolveStatus.ERROR
    assert rec.error and "make_query" in rec.error


def test_resolver_builds_same_number_of_jobs_as_repeat() -> None:
    """repeat 与域名数决定任务数，预热另算。"""
    srv = _server()
    resolver = DnsResolver([srv], repeat=2, warmup=False)
    doms = [_domain(), DomainSpec("github.com", Group.GLOBAL)]
    records = asyncio.run(resolver.run(doms))
    assert len(records) == 2 * len(doms)


@pytest.mark.parametrize("subnet", ["1.2.3.0/24", "2001:db8::/56", "bad", None])
def test_make_query_never_raises(subnet: str | None) -> None:
    for _ in range(1):
        _make_query("www.taobao.com", "A", subnet)