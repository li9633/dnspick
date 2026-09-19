"""IP 探测单测：纯逻辑与命令输出解析（不依赖真实网络）。"""

from __future__ import annotations

import ipaddress

from dnspick.ipprobe import (
    _LOSS_RE,
    _TIME_RE,
    _percentile,
    icmp_available,
    is_ip,
    traceroute_blocking,
)


def test_is_ip() -> None:
    assert is_ip("1.2.3.4")
    assert is_ip("::1")
    assert not is_ip("www.baidu.com")
    assert not is_ip("999.1.1.1")


def test_percentile() -> None:
    vals = [10.0, 1.0, 5.0, 8.0, 3.0]
    assert _percentile(vals, 0.0) == 1.0
    assert _percentile(vals, 1.0) == 10.0
    assert _percentile(vals, 0.5) == 5.0
    assert _percentile([], 0.5) != _percentile([], 0.5)  # nan


def test_time_regex_matches_linux_and_windows() -> None:
    linux = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.3 ms"
    windows = "来自 1.1.1.1 的回复: 字节=32 时间=12ms TTL=57"
    assert _TIME_RE.search(linux).group(1) == "12.3"
    assert _TIME_RE.search(windows).group(1) == "12"


def test_loss_regex() -> None:
    assert _LOSS_RE.search("4 packets transmitted, 4 received, 0% packet loss").group(1) == "0"
    assert _LOSS_RE.search("丢失 = 1 (25% 丢失)").group(1) == "25"


def test_traceroute_missing_binary_returns_empty() -> None:
    """命令不存在时必须优雅返回空列表，不能抛异常。"""
    out = traceroute_blocking("192.0.2.1", max_hops=3, timeout=0.01)
    assert isinstance(out, list)


def test_icmp_available_is_bool() -> None:
    assert isinstance(icmp_available(), bool)


def test_cn_prefixes_are_valid_cidr() -> None:
    """内置网段表必须全部合法，否则运行期会直接崩。"""
    from dnspick import builtin

    for cidr in builtin.builtin_cn_prefixes() + builtin.builtin_global_prefixes():
        ipaddress.ip_network(cidr, strict=True)
