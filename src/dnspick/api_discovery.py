"""API 域名自动发现。

通过两种互补策略发现主域名背后的关联域名：

    策略1: HTML 抓取 —— 请求主站首页，从 HTML 中提取所有外部域名引用。
           覆盖 script / link / img / source / iframe / video / audio 标签
           以及 CSS url() 和页面中硬编码的 URL。

    策略2: 证书透明度(crt.sh) —— 查询该根域下所有签发过 HTTPS 证书的子域名。
           覆盖面极广，可发现 HTML 首页未直接引用的后台接口域名。

核心原则：不设白名单/黑名单，主域名页面引用了什么就记录什么。
公共 CDN 在 DNS 测量阶段做域名去重，不在此层过滤。
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import urllib.request
from dataclasses import dataclass, field

from dnspick.log import log
from dnspick.models import DomainSpec, Group
from dnspick.relation_store import RelationStore

# ---------------------------------------------------------------------------
# URL / 域名提取正则
# ---------------------------------------------------------------------------

# 完整 URL（http 或 https 开头）—— group(1) 捕获整个 hostname
_URL_RE = re.compile(
    r"https?://([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,63})",
    re.IGNORECASE,
)

# protocol-relative URL（// 开头）
_PROTO_REL_RE = re.compile(
    r"//([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,63})",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------

_DEPRECATED_KEYWORDS = (
    "test", "old", "deleted", "staging", "dev", "demo",
    "tmp", "temp", "deprecated",
)

_EXCLUDE_HOSTS: tuple[str, ...] = (
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "example.com", "test.com", "domain.com",
)


def _is_valid_domain(host: str) -> bool:
    """校验字符串是否为合法域名。"""
    if not host or len(host) > 253:
        return False
    host = host.lower().strip(".")
    if not host or "." not in host:
        return False
    if host[0] in (".", "-") or host[-1] in ("-",):
        return False
    if host in _EXCLUDE_HOSTS:
        return False
    # 只允许 ASCII 域名字符（拒绝中文等非 ASCII 字符）
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        return False
    # 排除纯 IPv4
    if re.fullmatch(r"\d+(?:\.\d+){3}", host):
        return False
    # 排除 IPv6
    if host.count(":") >= 2:
        return False
    # TLD 必须包含字母（排除 333.1007 等纯数字伪域名）
    tld = host.rsplit(".", 1)[-1]
    if not re.search(r"[a-zA-Z]", tld):
        return False
    # 逐 label 检查
    for label in host.split("."):
        if not label or label.startswith("-") or label.endswith("-"):
            return False
        # 连续 4 个以上连字符 → crt.sh 数据异常
        if "----" in label:
            return False
        # 废弃关键词：label 命中或 label 以 "keyword-" 开头
        for kw in _DEPRECATED_KEYWORDS:
            if label == kw or label.startswith(kw + "-"):
                return False
    return True


def _extract_host(url: str) -> str | None:
    """从 URL 提取 hostname 并校验。"""
    if not url:
        return None
    url = url.strip()
    # protocol-relative URL 补全 scheme，以便正则匹配
    if url.startswith("//"):
        url = "https:" + url
    # 切掉 scheme:// 部分
    m = re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?([^/?#:]+)", url)
    if not m:
        return None
    host = m.group(1).lower().strip(".")
    return host if _is_valid_domain(host) else None


# ---------------------------------------------------------------------------
# 策略1: HTML 抓取
# ---------------------------------------------------------------------------

# HTML 属性中可能包含外部域名的模式
_SRC_ATTRS = (
    r'src\s*=\s*["\']([^"\']+)["\']',
    r'href\s*=\s*["\']([^"\']+)["\']',
    r'data-src\s*=\s*["\']([^"\']+)["\']',
    r'content\s*=\s*["\']([^"\']+)["\']',
    r"""url\s*\(\s*["']?([^)"'\s]+)["']?\s*\)""",
)

_NON_HTTP = re.compile(
    r"^(data:|javascript:|mailto:|tel:|#|about:|blob:)", re.IGNORECASE,
)


def _extract_domains_from_html(html: str) -> set[str]:
    """从 HTML 源码中提取所有引用的外部域名。

    覆盖：
    - <script src>, <link href>, <img src>, <img data-src>
    - <source src>, <iframe src>, <video src>, <audio src>
    - <meta content>（og:image 等）
    - CSS url() 引用
    - 页面中硬编码的 https?://... 字符串
    - protocol-relative //domain.com/... URL
    """
    found: set[str] = set()

    def _add(raw_url: str) -> None:
        raw_url = raw_url.strip()
        if _NON_HTTP.match(raw_url):
            return
        host = _extract_host(raw_url)
        if host:
            found.add(host)

    # 1. 标签属性中的 URL（src, href, data-src, content, css url()）
    for pattern in _SRC_ATTRS:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            _add(m.group(1))

    # 2. 完整的 http(s) URL（兜底，覆盖 JS 中硬编码的 API 地址）
    for m in _URL_RE.finditer(html):
        host = m.group(1).lower().strip(".")
        if _is_valid_domain(host):
            found.add(host)

    # 3. protocol-relative URL（//domain.com/...）
    for m in _PROTO_REL_RE.finditer(html):
        host = m.group(1).lower().strip(".")
        if _is_valid_domain(host):
            found.add(host)

    return found


def _fetch_html(domain: str, timeout: float = 5.0) -> str:
    """同步抓取首页 HTML。读取前 500KB，足够提取域名引用。"""
    url = f"https://{domain}/"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
        return resp.read(500 * 1024).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 策略2: 证书透明度 (crt.sh)
# ---------------------------------------------------------------------------

# 已知的两级 TLD
_TWO_LEVEL_TLDS: set[str] = {
    "com", "net", "org", "gov", "edu", "co", "ac", "mil", "int",
}


def _root_domain(domain: str) -> str:
    """提取根域。

    www.bilibili.com    → bilibili.com
    api.bilibili.com    → bilibili.com
    www.example.co.uk   → example.co.uk
    """
    parts = domain.lower().split(".")
    if len(parts) <= 2:
        return domain
    # .com.cn / .co.uk 等两级 TLD
    if parts[-2] in _TWO_LEVEL_TLDS and len(parts) > 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _query_crt_sh(root_domain: str, timeout: float = 8.0) -> list[dict]:
    """查询 crt.sh 证书透明度日志。"""
    url = f"https://crt.sh/?q=%25.{root_domain}&output=json"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "dnspick/0.1.0"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _extract_domains_from_crt(entries: list[dict]) -> set[str]:
    """从 crt.sh JSON 响应中提取有效域名列表。

    过滤：
    - 通配符域名（*.bilibili.com）→ 去掉前导通配符
    - 带废弃关键词的（test-, old- 等）
    - 纯 IP、无效格式
    """
    found: set[str] = set()
    for entry in entries:
        name_value = entry.get("name_value", "")
        for name in name_value.split("\n"):
            name = name.strip().lower()
            if not name:
                continue
            # 去掉前导通配符
            name = name.lstrip("*.")
            if not name or "." not in name:
                continue
            if not _is_valid_domain(name):
                continue
            found.add(name)
    return found


# ---------------------------------------------------------------------------
# 发现结果 & 主类
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    """单次发现的结果。"""

    main_domain: str
    html_domains: set[str] = field(default_factory=set)
    crt_domains: set[str] = field(default_factory=set)
    html_success: bool = False
    crt_success: bool = False
    html_error: str = ""
    crt_error: str = ""
    from_cache: bool = False

    @property
    def all_domains(self) -> set[str]:
        return self.html_domains | self.crt_domains

    @property
    def success(self) -> bool:
        return self.html_success or self.crt_success or self.from_cache


class ApiDomainDiscovery:
    """API 域名自动发现器。"""

    def __init__(
        self,
        store: RelationStore | None = None,
        *,
        cache_days: int = 7,
        html_timeout: float = 5.0,
        crt_timeout: float = 8.0,
        max_per_source: int = 60,
        min_cache: int = 3,
    ) -> None:
        self.store = store or RelationStore()
        self.cache_days = cache_days
        self.html_timeout = html_timeout
        self.crt_timeout = crt_timeout
        self.max_per_source = max_per_source
        self.min_cache = min_cache

    # -- 缓存检查 --------------------------------------------------------

    def _check_cache(self, main_domain: str) -> set[str] | None:
        count = self.store.count_relations(main_domain, self.cache_days)
        if count >= self.min_cache:
            cached = self.store.get_relations(main_domain, self.cache_days)
            if cached:
                return set(cached)
        return None

    # -- 策略1 ------------------------------------------------------------

    def _discover_html(self, main_domain: str) -> tuple[set[str], bool, str]:
        try:
            html = _fetch_html(main_domain, self.html_timeout)
            domains = _extract_domains_from_html(html)
            domains.discard(main_domain)
            if len(domains) > self.max_per_source:
                domains = set(sorted(domains)[:self.max_per_source])
            log.debug(f"HTML 发现 {main_domain}: {len(domains)} 个关联域名")
            return domains, True, ""
        except Exception as e:
            log.warn(f"HTML 发现失败 {main_domain}: {e}")
            return set(), False, str(e)

    # -- 策略2 ------------------------------------------------------------

    def _discover_crt(self, main_domain: str) -> tuple[set[str], bool, str]:
        try:
            root = _root_domain(main_domain)
            entries = _query_crt_sh(root, self.crt_timeout)
            domains = _extract_domains_from_crt(entries)
            domains.discard(main_domain)
            if len(domains) > self.max_per_source:
                domains = set(sorted(domains)[:self.max_per_source])
            log.debug(f"crt.sh 发现 {main_domain}: {len(domains)} 个关联域名")
            return domains, True, ""
        except Exception as e:
            log.warn(f"crt.sh 查询失败 {main_domain}: {e}")
            return set(), False, str(e)

    # -- 主入口 ----------------------------------------------------------

    async def discover(self, main_domain: str) -> DiscoveryResult:
        """对单个主域名执行发现。

        先查缓存，缓存不足时依次执行 HTML 抓取和 crt.sh 查询。
        """
        cached = self._check_cache(main_domain)
        if cached is not None:
            log.debug(f"API 发现 {main_domain}: 命中缓存 ({len(cached)} 个)")
            return DiscoveryResult(
                main_domain=main_domain,
                html_domains=cached,
                from_cache=True,
            )

        result = DiscoveryResult(main_domain=main_domain)

        html_doms, ok, err = await asyncio.to_thread(
            self._discover_html, main_domain,
        )
        result.html_domains = html_doms
        result.html_success = ok
        result.html_error = err
        if ok and html_doms:
            self.store.upsert_batch(main_domain, html_doms, "html")

        crt_doms, ok2, err2 = await asyncio.to_thread(
            self._discover_crt, main_domain,
        )
        result.crt_domains = crt_doms
        result.crt_success = ok2
        result.crt_error = err2
        if ok2 and crt_doms:
            self.store.upsert_batch(main_domain, crt_doms, "crt_sh")

        total = len(result.all_domains)
        if total:
            log.info(f"API 发现 {main_domain}: 共 {total} 个关联域名")
        elif not result.from_cache:
            log.debug(f"API 发现 {main_domain}: 无关联域名")

        return result

    async def discover_all(
        self, domains: list[DomainSpec],
    ) -> dict[str, DiscoveryResult]:
        """并发发现多个主域名的关联域名。"""
        tasks = [self.discover(d.name) for d in domains]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        result: dict[str, DiscoveryResult] = {}
        for d, res in zip(domains, gathered, strict=False):
            result[d.name] = (
                res if isinstance(res, DiscoveryResult)
                else DiscoveryResult(main_domain=d.name)
            )
        return result

    @staticmethod
    def to_domain_specs(
        results: dict[str, DiscoveryResult],
        source_domains: list[DomainSpec],
    ) -> list[DomainSpec]:
        """将发现结果去重，产出 DomainSpec 列表（继承主域名的 Group）。"""
        group_map: dict[str, Group] = {d.name: d.group for d in source_domains}
        seen: set[str] = {d.name for d in source_domains}
        specs: list[DomainSpec] = []

        for main_domain, result in results.items():
            group = group_map.get(main_domain, Group.CN)
            for related in sorted(result.all_domains):
                if related in seen:
                    continue
                seen.add(related)
                specs.append(DomainSpec(
                    name=related,
                    group=group,
                    note=f"来自 {main_domain} 的关联域名",
                ))

        return specs