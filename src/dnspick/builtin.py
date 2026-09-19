"""内置资源：DNS 上游清单、待测域名集、离线网段表。

三者都允许被外部 TOML 覆盖/追加，这里只提供开箱即用的默认值。
"""

from __future__ import annotations

from dnspick.models import DnsServer, DomainSpec, Group, Protocol

# --------------------------------------------------------------------------
# DNS 上游清单
# --------------------------------------------------------------------------
# 覆盖国内主流 + 国外主流，并为部分厂商附带 DoT/DoH 端点。
# 默认只启用 UDP 条目 + 少量加密条目，GUI/CLI 可勾选。

_SERVERS: list[DnsServer] = [
    # ---- 国内 ----
    DnsServer("alidns-1", "阿里 AliDNS", "223.5.5.5", Protocol.UDP, "alibaba", "cn", tags=("ecs", "cdn")),
    DnsServer("alidns-2", "阿里 AliDNS", "223.6.6.6", Protocol.UDP, "alibaba", "cn", tags=("ecs", "cdn")),
    DnsServer(
        "alidns-doh",
        "阿里 DoH",
        "https://dns.alidns.com/dns-query",
        Protocol.DOH,
        "alibaba",
        "cn",
        tags=("ecs", "cdn", "encrypted"),
        enabled=False,
    ),
    DnsServer("dnspod-1", "腾讯 DNSPod", "119.29.29.29", Protocol.UDP, "tencent", "cn", tags=("ecs", "cdn")),
    DnsServer(
        "dnspod-2", "腾讯 DNSPod", "119.28.28.28", Protocol.UDP, "tencent", "cn", tags=("ecs", "cdn"), enabled=False
    ),
    DnsServer(
        "dnspod-doh",
        "腾讯 DoH",
        "https://doh.pub/dns-query",
        Protocol.DOH,
        "tencent",
        "cn",
        tags=("ecs", "cdn", "encrypted"),
        enabled=False,
    ),
    DnsServer(
        "dnspod-dot",
        "腾讯 DoT",
        "119.29.29.29",
        Protocol.DOT,
        "tencent",
        "cn",
        port=853,
        hostname="dot.pub",
        tags=("ecs", "cdn", "encrypted"),
        enabled=False,
    ),
    DnsServer("114-1", "114DNS", "114.114.114.114", Protocol.UDP, "114dns", "cn"),
    DnsServer("114-2", "114DNS", "114.114.115.115", Protocol.UDP, "114dns", "cn", enabled=False),
    DnsServer("baidu", "百度 DNS", "180.76.76.76", Protocol.UDP, "baidu", "cn"),
    DnsServer("360-ct", "360 DNS(电信)", "101.226.4.6", Protocol.UDP, "360", "cn", enabled=False),
    DnsServer("360-cu", "360 DNS(联通)", "123.125.81.6", Protocol.UDP, "360", "cn", enabled=False),
    DnsServer("cnnic", "CNNIC SDNS", "1.2.4.8", Protocol.UDP, "cnnic", "cn", enabled=False),
    DnsServer("huawei", "华为云 DNS", "122.112.208.1", Protocol.UDP, "huawei", "cn", enabled=False),
    DnsServer("onevps", "ONE DNS", "117.50.10.10", Protocol.UDP, "onevps", "cn", enabled=False),
    # ---- 国外 ----
    DnsServer("cf-1", "Cloudflare", "1.1.1.1", Protocol.UDP, "cloudflare", "global", tags=("ecs", "anycast")),
    DnsServer("cf-2", "Cloudflare", "1.0.0.1", Protocol.UDP, "cloudflare", "global",
              tags=("ecs", "anycast"), enabled=False),
    DnsServer(
        "cf-doh",
        "Cloudflare DoH",
        "https://cloudflare-dns.com/dns-query",
        Protocol.DOH,
        "cloudflare",
        "global",
        tags=("encrypted", "anycast"),
        enabled=False,
    ),
    DnsServer("google-1", "Google DNS", "8.8.8.8", Protocol.UDP, "google", "global", tags=("ecs", "anycast")),
    DnsServer(
        "google-2", "Google DNS", "8.8.4.4", Protocol.UDP, "google", "global", tags=("ecs", "anycast"), enabled=False
    ),
    DnsServer(
        "google-doh",
        "Google DoH",
        "https://dns.google/dns-query",
        Protocol.DOH,
        "google",
        "global",
        tags=("encrypted", "anycast"),
        enabled=False,
    ),
    DnsServer(
        "google-dot",
        "Google DoT",
        "8.8.8.8",
        Protocol.DOT,
        "google",
        "global",
        port=853,
        hostname="dns.google",
        tags=("encrypted", "anycast"),
        enabled=False,
    ),
    DnsServer("quad9", "Quad9", "9.9.9.9", Protocol.UDP, "quad9", "global", tags=("security", "anycast")),
    DnsServer(
        "quad9-doh",
        "Quad9 DoH",
        "https://dns.quad9.net/dns-query",
        Protocol.DOH,
        "quad9",
        "global",
        tags=("encrypted", "security", "anycast"),
        enabled=False,
    ),
    DnsServer("adguard", "AdGuard DNS", "94.140.14.14", Protocol.UDP, "adguard", "global", tags=("block",)),
    DnsServer(
        "adguard-doh",
        "AdGuard DoH",
        "https://dns.adguard-dns.com/dns-query",
        Protocol.DOH,
        "adguard",
        "global",
        tags=("block", "encrypted"),
        enabled=False,
    ),
    DnsServer(
        "adguard-dot",
        "AdGuard DoT",
        "94.140.14.14",
        Protocol.DOT,
        "adguard",
        "global",
        port=853,
        hostname="dns.adguard-dns.com",
        tags=("block", "encrypted"),
        enabled=False,
    ),
    DnsServer("opendns", "OpenDNS", "208.67.222.222", Protocol.UDP, "opendns", "global", tags=("ecs",)),
    DnsServer(
        "opendns-doh",
        "OpenDNS DoH",
        "https://doh.opendns.com/dns-query",
        Protocol.DOH,
        "opendns",
        "global",
        tags=("ecs", "encrypted"),
        enabled=False,
    ),
    DnsServer("dns-sb", "DNS.SB", "185.222.222.222", Protocol.UDP, "dnssb", "global", enabled=False),
    DnsServer(
        "dns-sb-doh",
        "DNS.SB DoH",
        "https://doh.sb/dns-query",
        Protocol.DOH,
        "dnssb",
        "global",
        tags=("encrypted",),
        enabled=False,
    ),
    DnsServer("controld", "ControlD", "76.76.2.0", Protocol.UDP, "controld", "global", enabled=False),
    # ---- 加密协议样例（默认关闭，需手工开启） ----
    DnsServer(
        "alidns-dot",
        "阿里 DoT",
        "223.5.5.5",
        Protocol.DOT,
        "alibaba",
        "cn",
        port=853,
        hostname="dns.alidns.com",
        tags=("encrypted", "ecs"),
        enabled=False,
    ),
    DnsServer(
        "cf-dot",
        "Cloudflare DoT",
        "1.1.1.1",
        Protocol.DOT,
        "cloudflare",
        "global",
        port=853,
        hostname="cloudflare-dns.com",
        tags=("encrypted", "anycast"),
        enabled=False,
    ),
    DnsServer(
        "quad9-dot",
        "Quad9 DoT",
        "9.9.9.9",
        Protocol.DOT,
        "quad9",
        "global",
        port=853,
        hostname="dns.quad9.net",
        tags=("encrypted", "security"),
        enabled=False,
    ),
]

# --------------------------------------------------------------------------
# 待测域名集
# --------------------------------------------------------------------------
# 国内 CDN 组是真正决定体验的一组：这些域名的解析结果是否被调度到就近节点，
# 才是「DNS 好不好」的分水岭。

_DOMAINS: list[DomainSpec] = [
    # 国内 CDN（强调度，最能区分优劣）
    DomainSpec("www.taobao.com", Group.CN, "阿里系 CDN"),
    DomainSpec("www.tmall.com", Group.CN, "阿里系 CDN"),
    DomainSpec("www.jd.com", Group.CN, "京东 CDN"),
    DomainSpec("www.douyin.com", Group.CN, "字节 CDN"),
    DomainSpec("www.bilibili.com", Group.CN, "B站 CDN"),
    DomainSpec("v.qq.com", Group.CN, "腾讯视频 CDN"),
    DomainSpec("www.kuaishou.com", Group.CN, "快手 CDN"),
    DomainSpec("www.163.com", Group.CN, "网易 CDN"),
    # 国内普通
    DomainSpec("www.baidu.com", Group.CN, "百度"),
    DomainSpec("www.qq.com", Group.CN, "腾讯门户"),
    DomainSpec("www.zhihu.com", Group.CN, "知乎"),
    DomainSpec("www.weibo.com", Group.CN, "微博"),
    DomainSpec("www.csdn.net", Group.CN, "CSDN"),
    # 国外
    DomainSpec("www.google.com", Group.GLOBAL, "Google"),
    DomainSpec("github.com", Group.GLOBAL, "GitHub"),
    DomainSpec("www.cloudflare.com", Group.GLOBAL, "Cloudflare"),
    DomainSpec("en.wikipedia.org", Group.GLOBAL, "维基百科"),
    DomainSpec("www.microsoft.com", Group.GLOBAL, "微软"),
    DomainSpec("stackoverflow.com", Group.GLOBAL, "StackOverflow"),
    DomainSpec("www.youtube.com", Group.GLOBAL, "YouTube"),
    DomainSpec("api.openai.com", Group.GLOBAL, "OpenAI"),
]

# --------------------------------------------------------------------------
# 内置网段表（离线兜底）
# --------------------------------------------------------------------------
# 说明：这不是完整的中国 IP 库，无法覆盖全部地址空间。它的定位是
# 「高置信快速判定」——命中的一定可信，未命中的交给在线 provider 或标记为未知。
# 用户可以随时用 --cn-list 导入完整列表（如 Loyalsoldier/geoip 的 cn.txt）覆盖。

_CN_PREFIXES: tuple[str, ...] = (
    # 中国电信
    "1.0.1.0/24", "1.0.2.0/23", "1.0.8.0/21", "1.0.32.0/19", "1.1.0.0/24",
    "27.8.0.0/13", "27.16.0.0/12", "27.36.0.0/14", "27.40.0.0/13",
    "36.0.0.0/11", "36.32.0.0/12", "36.48.0.0/13", "36.56.0.0/15",
    "49.64.0.0/11", "49.96.0.0/12", "49.112.0.0/13", "49.120.0.0/14",
    "58.16.0.0/13", "58.24.0.0/14", "58.32.0.0/11", "58.64.0.0/11",
    "59.32.0.0/11", "59.64.0.0/11", "59.96.0.0/13", "59.104.0.0/13",
    "61.128.0.0/10", "61.192.0.0/10",
    "101.64.0.0/10", "101.128.0.0/11", "101.160.0.0/12",
    "116.0.0.0/10", "116.64.0.0/11", "116.96.0.0/12",
    "118.64.0.0/10", "118.128.0.0/11", "118.160.0.0/12",
    "119.0.0.0/11", "119.32.0.0/12",
    "120.64.0.0/10", "120.128.0.0/12",
    "121.0.0.0/10", "121.64.0.0/11",
    "125.64.0.0/11", "125.96.0.0/12",
    "171.8.0.0/13", "171.16.0.0/12", "171.32.0.0/11",
    "183.0.0.0/10", "183.64.0.0/11", "183.96.0.0/12",
    "202.96.0.0/12", "202.112.0.0/13", "202.120.0.0/14",
    "210.5.0.0/16", "211.136.0.0/13", "211.144.0.0/12",
    "218.0.0.0/11", "218.32.0.0/12", "218.48.0.0/13",
    "219.72.0.0/13", "219.128.0.0/11", "219.160.0.0/12",
    "221.192.0.0/13", "221.224.0.0/12",
    "222.64.0.0/11", "222.96.0.0/12", "222.160.0.0/11",
    # 中国联通
    "27.0.0.0/12", "27.32.0.0/14",
    "43.224.0.0/12", "43.240.0.0/13",
    "58.240.0.0/12", "58.248.0.0/13", "58.246.0.0/15",
    "60.0.0.0/10", "60.64.0.0/11", "60.208.0.0/12",
    "61.16.0.0/12", "61.32.0.0/11", "61.240.0.0/13",
    "101.16.0.0/12", "101.32.0.0/12", "101.48.0.0/13",
    "112.64.0.0/10", "112.128.0.0/11", "112.224.0.0/11",
    "113.0.0.0/10", "113.64.0.0/11", "113.96.0.0/12",
    "114.240.0.0/12", "115.0.0.0/10", "115.64.0.0/11",
    "116.128.0.0/11", "116.160.0.0/12",
    "117.0.0.0/10", "117.64.0.0/11", "117.96.0.0/12",
    "118.192.0.0/11", "119.64.0.0/11", "119.96.0.0/12",
    "120.16.0.0/12", "120.32.0.0/11", "120.80.0.0/13",
    "121.128.0.0/11", "121.160.0.0/12",
    "123.0.0.0/10", "123.64.0.0/11", "123.96.0.0/12",
    "124.64.0.0/11", "124.96.0.0/12",
    "125.32.0.0/12", "125.48.0.0/13",
    "157.0.0.0/10", "157.64.0.0/11",
    "163.0.0.0/10", "163.64.0.0/11", "163.96.0.0/12",
    "175.0.0.0/10", "175.64.0.0/11",
    "202.96.128.0/19", "202.99.0.0/16", "202.102.0.0/15",
    "202.110.0.0/15", "202.122.0.0/15",
    "210.0.0.0/12", "210.16.0.0/13", "210.24.0.0/14",
    "211.88.0.0/13", "211.96.0.0/12", "211.112.0.0/13",
    "218.24.0.0/13", "218.56.0.0/13", "218.104.0.0/13",
    "219.144.0.0/13", "219.232.0.0/13",
    "221.0.0.0/12", "221.16.0.0/13", "221.208.0.0/12",
    "222.32.0.0/11", "222.128.0.0/11",
    # 中国移动 / 铁通
    "36.128.0.0/10", "36.192.0.0/11",
    "39.64.0.0/10", "39.128.0.0/11", "39.160.0.0/12",
    "43.224.0.0/11", "43.248.0.0/13",
    "45.128.0.0/10", "45.192.0.0/11",
    "111.0.0.0/10", "111.64.0.0/11", "111.96.0.0/12",
    "112.0.0.0/10", "112.192.0.0/11",
    "117.128.0.0/11", "117.160.0.0/12",
    "120.192.0.0/10", "121.192.0.0/11",
    "122.0.0.0/10", "122.64.0.0/11", "122.96.0.0/12",
    "123.128.0.0/11", "123.160.0.0/12",
    "183.192.0.0/10", "184.64.0.0/10",
    "211.103.0.0/16", "211.140.0.0/14", "211.148.0.0/14",
    "218.200.0.0/13", "218.208.0.0/12",
    "221.176.0.0/12", "221.128.0.0/12",
    "223.64.0.0/10", "223.128.0.0/11", "223.160.0.0/12",
    # 教育网 / 科技网 / 主要云厂商
    "202.112.0.0/12", "202.38.0.0/15", "202.4.128.0/19",
    "159.226.0.0/16", "159.226.128.0/17",
    "8.129.0.0/16", "47.92.0.0/14", "47.96.0.0/13", "47.104.0.0/14",
    "120.24.0.0/13", "120.55.0.0/16", "121.40.0.0/14", "121.196.0.0/14",
    "139.196.0.0/14", "140.205.0.0/16", "106.11.0.0/16", "114.80.0.0/12",
    "129.204.0.0/15", "134.175.0.0/16", "150.109.0.0/16", "150.138.0.0/15",
    "152.136.0.0/13", "157.255.0.0/16", "162.14.0.0/16", "163.177.0.0/16",
    "180.76.0.0/16", "180.101.0.0/16", "180.163.0.0/16", "182.140.0.0/15",
    "203.208.0.0/16", "211.151.0.0/16", "218.30.0.0/15", "222.73.0.0/16",
)

# 常见 anycast / 海外大厂段（用于快速判「境外」）
_GLOBAL_PREFIXES: tuple[str, ...] = (
    "1.1.1.0/24", "1.0.0.0/24", "8.8.8.0/24", "8.8.4.0/24", "8.34.208.0/20",
    "9.9.9.0/24", "149.112.112.0/24", "208.67.220.0/22", "94.140.14.0/24",
    "13.32.0.0/15", "13.35.0.0/16", "13.107.0.0/16", "20.0.0.0/11",
    "31.13.0.0/16", "34.0.0.0/8", "35.0.0.0/8", "40.64.0.0/10",
    "52.0.0.0/8", "54.0.0.0/8", "64.233.160.0/19", "66.102.0.0/20",
    "66.249.64.0/19", "72.14.192.0/18", "74.125.0.0/16", "104.16.0.0/13",
    "108.177.0.0/17", "142.250.0.0/15", "151.101.0.0/16", "157.240.0.0/16",
    "172.217.0.0/16", "173.194.0.0/16", "185.222.222.0/24", "192.30.252.0/22",
    "199.36.158.0/23", "199.232.0.0/16", "204.79.197.0/24", "216.58.192.0/19",
)

# 运营商关键字（在线 provider 返回 isp 时的归一化映射）
_ISP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "电信": ("chinanet", "telecom", "ctcc", "chinatelecom"),
    "联通": ("unicom", "cnc", "cucc", "china unicom"),
    "移动": ("mobile", "cmcc", "china mobile", "chinamobile", "tietong"),
    "教育网": ("cernet", "education", "edu"),
}


def builtin_servers() -> list[DnsServer]:
    return list(_SERVERS)


def builtin_domains() -> list[DomainSpec]:
    return list(_DOMAINS)


def builtin_cn_prefixes() -> tuple[str, ...]:
    return _CN_PREFIXES


def builtin_global_prefixes() -> tuple[str, ...]:
    return _GLOBAL_PREFIXES


def probe_domains(seed: int = 0) -> list[DomainSpec]:
    """随机子域探针：正常情况下必须返回 NXDOMAIN，返回 A 记录即判定劫持。"""
    import secrets

    token = secrets.token_hex(4)
    return [
        DomainSpec(f"{token}-a.nxprobe.invalid", Group.PROBE, "NXDOMAIN 探针"),
        DomainSpec(f"{token}-b.nxprobe.invalid", Group.PROBE, "NXDOMAIN 探针"),
    ]