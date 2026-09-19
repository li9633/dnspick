"""核心数据模型。全部为纯数据容器，不含 IO，便于 CLI / GUI / 测试共用。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum


class Protocol(str, Enum):
    """DNS 传输协议。"""

    UDP = "udp"
    TCP = "tcp"
    DOT = "dot"
    DOH = "doh"


class Group(str, Enum):
    """域名分组：国内与国外必须分开评估，混在一起打分会掩盖真相。"""

    CN = "cn"
    GLOBAL = "global"
    PROBE = "probe"


ENCRYPTED = {Protocol.DOT, Protocol.DOH}


@dataclass(frozen=True)
class DnsServer:
    """一台待评估的 DNS 上游。"""

    id: str
    name: str
    address: str  # IP 或 DoH URL
    protocol: Protocol
    vendor: str = ""  # 厂商标识，如 "alibaba"、"tencent"、"cloudflare"
    region: str = "unknown"  # cn / global —— 用于默认分组展示
    port: int = 53
    hostname: str | None = None  # DoT 的 SNI
    tags: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def encrypted(self) -> bool:
        return self.protocol in ENCRYPTED

    def with_enabled(self, enabled: bool) -> DnsServer:
        """frozen 实例不可变，需要改字段时用这个产出副本。"""
        return replace(self, enabled=enabled)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.address})"


@dataclass(frozen=True)
class DomainSpec:
    """一个待测域名。

    tier 是分层：
        1 = 核心层（CDN 强调度 + 你最常访问的站点）——默认只跑这一层。
           真正决定「就近调度好不好」的就是这一层。
        2 = 常用层（主流站点）——想看更全面的国内/国外表现时加上。
        3 = 长尾层（自动发现的长尾域名）——绝大多数静态小站解析到哪都一样，
           对评分没有区分度，只会稀释信号并把测量时间拉长，默认不跑。
    """

    name: str
    group: Group
    note: str = ""
    tier: int = 1


class ResolveStatus(str, Enum):
    OK = "ok"
    NXDOMAIN = "nxdomain"
    SERVFAIL = "servfail"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class ResolveRecord:
    """一次 DNS 查询的观测结果。"""

    server_id: str
    domain: str
    group: Group
    rdtype: str
    status: ResolveStatus
    rtt_ms: float | None
    answers: list[str] = field(default_factory=list)
    ttl: int | None = None
    error: str | None = None
    ecs: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ResolveStatus.OK and bool(self.answers)


@dataclass
class IpProbeResult:
    """对一个 IP 的链路探测结果。"""

    ip: str
    tcp_ports: dict[int, float | None] = field(default_factory=dict)  # port -> p50 ms
    tcp_success_rate: float = 0.0
    tcp_p50_ms: float | None = None
    tcp_min_ms: float | None = None
    tcp_jitter_ms: float | None = None
    best_port: int | None = None  # 实际握手成功的端口，跳数探测要复用它
    hop_count: int | None = None
    icmp_avg_ms: float | None = None
    icmp_loss: float | None = None
    route: list[str] = field(default_factory=list)  # 完整 traceroute 路径（深度模式）
    geo: IpGeo | None = None
    unreachable: bool = False

    @property
    def domestic(self) -> bool | None:
        return None if self.geo is None else self.geo.is_domestic


@dataclass
class IpGeo:
    """IP 归属地（解释性信息，缺失不影响主评分）。"""

    ip: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    isp: str | None = None
    asn: str | None = None
    org: str | None = None
    is_domestic: bool | None = None  # None 表示无法判定
    source: str = "unknown"

    def brief(self) -> str:
        parts = [p for p in (self.country, self.region, self.isp) if p]
        return "/".join(parts) if parts else "-"


@dataclass
class GroupScore:
    """单个 DNS 在某个域名分组上的表现。"""

    group: Group
    resolve_success_rate: float = 0.0
    ip_reachable_rate: float = 0.0
    tcp_p50_ms: float | None = None
    hop_avg: float | None = None
    proximity: float = 0.0  # 0~100 就近性
    speed: float = 0.0  # 0~100 延迟分
    path: float = 0.0  # 0~100 跳数分
    stability: float = 0.0  # 0~100 稳定性
    polluted: bool = False
    score: float = 0.0


class Verdict(str, Enum):
    RECOMMEND = "recommend"  # 推荐作为主力
    FALLBACK = "fallback"  # 可作备用
    NARROW = "narrow"  # 只适合特定分组
    AVOID = "avoid"  # 不建议使用


@dataclass
class ServerReport:
    """一台 DNS 的完整评估报告。"""

    server: DnsServer
    cn: GroupScore
    glob: GroupScore
    hijack: bool = False
    ecs_supported: bool | None = None
    dns_rtt_p50_ms: float | None = None
    total: float = 0.0
    verdict: Verdict = Verdict.AVOID
    advice: str = ""
    top_ips: list[tuple[str, float | None, str]] = field(default_factory=list)  # ip, p50, geo

    def group_of(self, group: Group) -> GroupScore:
        return self.cn if group is Group.CN else self.glob


@dataclass
class RunSummary:
    """一次测量的环境信息。"""

    started_at: float = field(default_factory=time.time)
    public_ip: str | None = None
    ecs_subnet: str | None = None
    local_hint: str = ""
    warning: str = ""
    duration_s: float = 0.0
    servers: int = 0
    domains: int = 0
    unique_ips: int = 0


@dataclass
class RunResult:
    reports: list[ServerReport] = field(default_factory=list)
    ips: dict[str, IpProbeResult] = field(default_factory=dict)
    records: list[ResolveRecord] = field(default_factory=list)
    summary: RunSummary = field(default_factory=RunSummary)

    @property
    def ranked(self) -> list[ServerReport]:
        return sorted(self.reports, key=lambda r: r.total, reverse=True)