"""配置：TOML 覆盖内置资源 + 运行参数。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 仅在旧解释器上生效
    import tomli as tomllib  # type: ignore[no-redef]

from dnspick import builtin
from dnspick.models import DnsServer, DomainSpec, Group, Protocol

_USER_CONFIG_DIR = Path(".dnspick")
_USER_CONFIG_FILE = _USER_CONFIG_DIR / "config.json"


@dataclass
class Tunables:
    """测量参数。默认值按「不失真」原则选取。"""

    # 解析阶段
    resolve_repeat: int = 3  # 每个域名查几次
    resolve_timeout: float = 2.0
    resolve_concurrency: int = 24  # 跨 server 的全局并发上限
    per_server_concurrency: int = 2  # 单 server 内并发，压太高会测成排队时间
    warmup: bool = True  # 预热一次不计时（冷启动/ARP/TLS 握手会严重污染首包）

    # IP 探测阶段
    tcp_ports: tuple[int, ...] = (443, 80, 8443)
    tcp_repeat: int = 3
    tcp_timeout: float = 1.5
    tcp_concurrency: int = 64
    hop_max: int = 30
    hop_timeout: float = 1.0
    icmp_enabled: bool = True
    icmp_count: int = 4
    icmp_timeout_s: float = 4.0
    icmp_concurrency: int = 32
    deep_traceroute: bool = False  # 深度模式：全量 traceroute（慢，且部分平台需提权）
    geoip_enabled: bool = True

    # 评分权重（GUI 里可拖动）
    w_speed: float = 30.0
    w_path: float = 15.0
    w_stability: float = 25.0
    w_proximity: float = 30.0
    hijack_penalty: float = 60.0  # 检测到劫持的总分扣减
    pollution_penalty: float = 25.0

    max_tier: int = 1  # 默认只跑核心层：长尾域名没有区分度，只会拖慢测量

    cn_weight: float = 0.7  # 总分里国内组的权重
    global_weight: float = 0.3
    _profile: str = "balanced"  # 当前生效的档位名

    def normalize(self) -> None:
        total = self.w_speed + self.w_path + self.w_stability + self.w_proximity
        if total <= 0:
            return
        if abs(total - 100.0) > 1e-6:
            k = 100.0 / total
            self.w_speed *= k
            self.w_path *= k
            self.w_stability *= k
            self.w_proximity *= k

    def with_profile(self, name: str) -> Tunables:
        """预设档位。"""
        profiles = {
            "balanced": dict(w_speed=30, w_path=15, w_stability=25, w_proximity=30),
            "speed": dict(w_speed=55, w_path=10, w_stability=15, w_proximity=20),
            "stable": dict(w_speed=20, w_path=10, w_stability=50, w_proximity=20),
            "cdn": dict(w_speed=20, w_path=10, w_stability=15, w_proximity=55),
            "privacy": dict(w_speed=25, w_path=10, w_stability=20, w_proximity=20),
        }
        if name in profiles:
            for k, v in profiles[name].items():
                setattr(self, k, v)
        self._profile = name
        return self

    @property
    def profile(self) -> str:
        return self._profile


@dataclass
class Config:
    tunables: Tunables = field(default_factory=Tunables)
    servers: list[DnsServer] = field(default_factory=builtin.builtin_servers)
    domains: list[DomainSpec] = field(default_factory=builtin.builtin_domains)
    extra_cn_prefixes: list[str] = field(default_factory=list)
    geoip_db: str | None = None  # mmdb 路径（可选）
    use_ecs: bool = False  # 是否启用 ECS 对比查询（需先探测公网 IP）

    def set_enabled(self, ids: set[str]) -> None:
        """只启用给定 id 的服务器（frozen 实例需整体替换）。"""
        self.servers = [s.with_enabled(s.id in ids) for s in self.servers]

    def active_servers(self) -> list[DnsServer]:
        return [s for s in self.servers if s.enabled]

    def active_domains(
        self,
        groups: set[Group] | None = None,
        max_tier: int | None = None,
    ) -> list[DomainSpec]:
        ds = self.domains
        if groups:
            ds = [d for d in ds if d.group in groups]
        if max_tier is not None:
            ds = [d for d in ds if d.tier <= max_tier]
        return ds

    def save(self) -> None:
        """持久化当前配置到用户目录。"""
        _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        t = self.tunables
        data = {
            "tunables": {
                "resolve_repeat": t.resolve_repeat,
                "resolve_timeout": t.resolve_timeout,
                "resolve_concurrency": t.resolve_concurrency,
                "per_server_concurrency": t.per_server_concurrency,
                "warmup": t.warmup,
                "tcp_ports": list(t.tcp_ports),
                "tcp_repeat": t.tcp_repeat,
                "tcp_timeout": t.tcp_timeout,
                "tcp_concurrency": t.tcp_concurrency,
                "hop_max": t.hop_max,
                "hop_timeout": t.hop_timeout,
                "icmp_enabled": t.icmp_enabled,
                "icmp_count": t.icmp_count,
                "icmp_timeout_s": t.icmp_timeout_s,
                "icmp_concurrency": t.icmp_concurrency,
                "deep_traceroute": t.deep_traceroute,
                "geoip_enabled": t.geoip_enabled,
                "w_speed": t.w_speed,
                "w_path": t.w_path,
                "w_stability": t.w_stability,
                "w_proximity": t.w_proximity,
                "hijack_penalty": t.hijack_penalty,
                "pollution_penalty": t.pollution_penalty,
                "max_tier": t.max_tier,
                "cn_weight": t.cn_weight,
                "global_weight": t.global_weight,
                "profile": t.profile,
            },
            "use_ecs": self.use_ecs,
            "enabled_servers": [s.id for s in self.servers if s.enabled],
        }
        _USER_CONFIG_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def load_saved(self) -> None:
        """从用户目录恢复上次保存的配置。"""
        if not _USER_CONFIG_FILE.exists():
            return
        try:
            data = json.loads(_USER_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        td = data.get("tunables", {})
        if td:
            t = self.tunables
            for key in (
                "resolve_repeat", "resolve_timeout", "resolve_concurrency",
                "per_server_concurrency", "warmup", "tcp_repeat", "tcp_timeout",
                "tcp_concurrency", "hop_max", "hop_timeout", "icmp_enabled",
                "icmp_count", "icmp_timeout_s", "icmp_concurrency",
                "deep_traceroute", "geoip_enabled",
                "w_speed", "w_path", "w_stability", "w_proximity",
                "hijack_penalty", "pollution_penalty", "max_tier",
                "cn_weight", "global_weight",
            ):
                if key in td:
                    setattr(t, key, td[key])
            if "tcp_ports" in td:
                t.tcp_ports = tuple(td["tcp_ports"])
            if "profile" in td:
                t._profile = td["profile"]

        if "use_ecs" in data:
            self.use_ecs = data["use_ecs"]

        if "enabled_servers" in data:
            enabled_ids = set(data["enabled_servers"])
            self.servers = [s.with_enabled(s.id in enabled_ids) for s in self.servers]


def _load_file(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(
    servers_file: Path | None = None,
    domains_file: Path | None = None,
    cn_list: Path | None = None,
) -> Config:
    """从 TOML 载入覆盖项，缺失则用内置值。"""
    cfg = Config()

    if servers_file and servers_file.exists():
        data = _load_file(servers_file)
        custom = []
        for item in data.get("server", []):
            try:
                custom.append(
                    DnsServer(
                        id=item.get("id") or item["address"],
                        name=item.get("name", item["address"]),
                        address=item["address"],
                        protocol=Protocol(item.get("protocol", "udp")),
                        vendor=item.get("vendor", ""),
                        region=item.get("region", "unknown"),
                        port=int(item.get("port", 53)),
                        hostname=item.get("hostname"),
                        tags=tuple(item.get("tags", ())),
                        enabled=bool(item.get("enabled", True)),
                    )
                )
            except (KeyError, ValueError):
                continue
        if custom:
            seen = {s.address for s in custom}
            cfg.servers = custom + [s for s in cfg.servers if s.address not in seen]
        if data.get("enabled_only"):
            cfg.servers = custom

    if domains_file and domains_file.exists():
        data = _load_file(domains_file)
        doms = []
        for item in data.get("domain", []):
            try:
                doms.append(
                    DomainSpec(
                        name=item["name"],
                        group=Group(item.get("group", "cn")),
                        note=item.get("note", ""),
                        tier=int(item.get("tier", 1)),
                    )
                )
            except (KeyError, ValueError):
                continue
        if doms:
            cfg.domains = doms

    if cn_list and cn_list.exists():
        cfg.extra_cn_prefixes = [
            ln.strip() for ln in cn_list.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]

    return cfg