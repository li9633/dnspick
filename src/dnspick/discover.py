"""域名发现器：把「测哪些域名」这件事自动化。

三个来源，按价值排序：

    1. 浏览器历史（最有价值）—— 挖的是**你自己**真正访问过的域名。
       1000 个别人列表里的域名，不如 200 个你家每天真打开的域名有说服力。
    2. 内置种子库 resources/seed_domains.txt —— 1000+ 精选真实域名，覆盖国内外主流。
    3. 在线榜单 / 入口爬虫（可选）—— 需要外网，失败自动跳过。

产出按 tier 分层写入 domains.toml：
    tier1 核心层（默认只跑这层）→ tier2 常用层 → tier3 长尾层
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SEED_FILE = Path(__file__).resolve().parents[2] / "resources" / "seed_domains.txt"

# 中国大陆常用 TLD，用于给自动发现的域名定 group
_CN_TLDS = (
    ".cn", ".com.cn", ".net.cn", ".org.cn", ".gov.cn", ".edu.cn",
    ".ac.cn", ".hk", ".mo", ".tw", ".中国", ".公司", ".网络",
)

# 核心层白名单：这些是 CDN 调度最敏感、最能区分 DNS 优劣的域名。
# 即便浏览器历史里没出现过，也应当留在 tier1。
_TIER1_CORE = {
    # 国内 CDN 强调度：最能暴露「就近调度」差异
    "www.taobao.com", "www.tmall.com", "www.jd.com", "www.douyin.com",
    "www.bilibili.com", "v.qq.com", "www.kuaishou.com", "www.163.com",
    "www.baidu.com", "www.zhihu.com", "weibo.com", "www.xiaohongshu.com",
    "www.pinduoduo.com", "www.meituan.com", "www.dianping.com",
    "www.aliyun.com", "www.qq.com", "www.iqiyi.com", "www.youku.com",
    "www.ctrip.com", "www.mi.com", "www.suning.com", "www.vip.com",
    "mail.163.com", "www.126.com", "map.baidu.com",
    "www.amap.com", "pan.baidu.com", "tieba.baidu.com", "www.douban.com",
    # 国外：能同时验证「有没有被污染 / 国外解析准不准」
    "www.google.com", "github.com", "www.cloudflare.com", "en.wikipedia.org",
    "www.microsoft.com", "stackoverflow.com", "www.youtube.com",
    "www.apple.com", "www.amazon.com", "www.reddit.com", "twitter.com",
    "www.netflix.com", "www.bbc.com", "www.nytimes.com", "openai.com",
}

# 白名单中属于境外分组的部分，用于给无条件入池的核心域名定 group
_TIER1_CORE_GLOBAL = {
    "www.google.com", "github.com", "www.cloudflare.com", "en.wikipedia.org",
    "www.microsoft.com", "stackoverflow.com", "www.youtube.com",
    "www.apple.com", "www.amazon.com", "www.reddit.com", "twitter.com",
    "www.netflix.com", "www.bbc.com", "www.nytimes.com", "openai.com",
}

_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b", re.IGNORECASE
)

# 明显不是用户访问目标站点的东西：CDN 资源、分析脚本、浏览器内部地址
_NOISE_PARTS = (
    "doubleclick", "googlesyndication", "google-analytics", "googletagmanager",
    "gstatic", "googleusercontent", "ytimg", "ggpht", "googlevideo",
    "cloudflareinsights", "hotjar", "fullstory", "segment.io", "branch.io",
    "amplitude", "mixpanel", "heapanalytics", "onesignal",
    "localhost", "127.0.0.1", "example.com", "test.com",
)


@dataclass
class Discovered:
    name: str
    group: str
    tier: int
    hits: int = 0  # 浏览器访问次数
    source: str = "seed"
    note: str = ""

    def sort_key(self) -> tuple:
        return (self.tier, -self.hits, self.name)


@dataclass
class DiscoverReport:
    items: list[Discovered] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    browser_domains: int = 0
    seed_domains: int = 0
    crawled_domains: int = 0

    def by_tier(self, tier: int) -> list[Discovered]:
        return [d for d in self.items if d.tier == tier]


# --------------------------------------------------------------------------
# 来源 1：浏览器历史
# --------------------------------------------------------------------------
def _chrome_history_paths() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        out += [
            local / "Google" / "Chrome" / "User Data" / "Default" / "History",
            local / "Microsoft" / "Edge" / "User Data" / "Default" / "History",
            local / "Google" / "Chrome" / "User Data" / "Profile 1" / "History",
            local / "Microsoft" / "Edge" / "User Data" / "Profile 1" / "History",
        ]
    elif os.name == "posix":
        import platform

        if platform.system() == "Darwin":
            out += [
                home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "History",
                home / "Library" / "Application Support" / "Microsoft Edge" / "Default" / "History",
            ]
        else:
            out += [
                home / ".config" / "google-chrome" / "Default" / "History",
                home / ".config" / "microsoft-edge" / "Default" / "History",
                home / ".config" / "chromium" / "Default" / "History",
            ]
    return out


def _firefox_places_paths() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    if os.name == "nt":
        roots.append(Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Mozilla" / "Firefox" / "Profiles")
    elif os.name == "posix":
        import platform

        roots.append(
            home / "Library" / "Application Support" / "Firefox" / "Profiles"
            if platform.system() == "Darwin"
            else home / ".mozilla" / "firefox"
        )
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.glob("*/places.sqlite"):
            found.append(p)
        for p in root.glob("*.default*/places.sqlite"):
            if p not in found:
                found.append(p)
    return found


def _copy_and_query(db: Path, sql: str) -> list[tuple[str, int]]:
    """浏览器会锁住 sqlite 文件，必须先复制再读。"""
    if not db.exists() or db.stat().st_size == 0:
        return []
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as fh:
            tmp = Path(fh.name)
        shutil.copy2(db, tmp)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True, timeout=2.0)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, shutil.Error):
        return []
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)


def mine_browser_history(min_hits: int = 2) -> dict[str, int]:
    """从 Chrome/Edge/Firefox 历史里统计域名访问次数。失败返回空。"""
    counts: Counter[str] = Counter()

    chrome_sql = "SELECT url, visit_count FROM urls"
    for db in _chrome_history_paths():
        for url, hits in _copy_and_query(db, chrome_sql):
            dom = _host_of(url)
            if dom:
                counts[dom] += max(int(hits or 0), 1)

    firefox_sql = "SELECT url, visit_count FROM moz_places"
    for db in _firefox_places_paths():
        for url, hits in _copy_and_query(db, firefox_sql):
            dom = _host_of(url)
            if dom:
                counts[dom] += max(int(hits or 0), 1)

    return {d: c for d, c in counts.items() if c >= min_hits}


def _host_of(url: str) -> str | None:
    if not url:
        return None
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", url)
    host = m.group(1) if m else url.split("/")[0]
    host = host.split(":")[0].lower().strip(".")
    if not host or "." not in host:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*\.[a-z]{2,63}", host):
        return None
    if any(p in host for p in _NOISE_PARTS):
        return None
    return host


# --------------------------------------------------------------------------
# 来源 2：内置种子库
# --------------------------------------------------------------------------
def load_seeds(path: Path | None = None) -> list[tuple[str, str, str]]:
    """读种子文件，返回 (域名, group, section)。

    分区注释形如 "# ===== 国外 · Google 系 ====="。注意两侧有等号，
    必须用 `in` 而不是 startswith —— 之前写成 startswith("国外") 导致
    分区永远解析不出来，所有域名都被判成 cn，国内外分组直接错乱。
    """
    p = path or SEED_FILE
    if not p.exists():
        return []
    group = ""
    section = ""
    out: list[tuple[str, str, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#"):
            body = raw.lstrip("#").strip("= ").strip()
            has_cn = "国内" in body
            has_gl = "国外" in body
            if has_cn and not has_gl:
                group = "cn"
            elif has_gl and not has_cn:
                group = "global"
            else:
                group = ""  # 混合分区（如「补充 · 国内外常用站点」）交给 TLD 判定
            section = body
            continue
        name = raw.split("#")[0].strip()
        if not name:
            continue
        if any(p_ in name for p_ in _NOISE_PARTS):
            continue
        out.append((name, group, section))
    return out


# --------------------------------------------------------------------------
# 来源 3：在线抓取（可选，失败即跳过）
# --------------------------------------------------------------------------
def fetch_online_list(timeout: float = 8.0, limit: int = 5000) -> list[str]:
    """Tranco / Cloudflare Radar 等公开榜单。沙盒或内网可能不通，失败返回空。"""
    import urllib.request

    urls = (
        "https://tranco-list.eu/top-1m.csv.zip",
        "https://downloads.majestic.com/majestic_million.csv",
    )
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read(20 * 1024 * 1024)
        except Exception:
            continue
        text = raw.decode("utf-8", "replace")
        if url.endswith(".zip"):
            try:
                import io
                import zipfile

                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    text = zf.read(zf.namelist()[0]).decode("utf-8", "replace")
            except Exception:
                continue
        doms = []
        for line in text.splitlines()[:limit]:
            part = line.split(",")[-1].strip() if "," in line else line.strip()
            if _host_of(part):
                doms.append(_host_of(part))  # type: ignore[arg-type]
        if doms:
            return doms[:limit]
    return []


async def crawl_domains(
    seeds: list[str], max_pages: int = 30, concurrency: int = 8, timeout: float = 6.0
) -> list[str]:
    """轻量爬虫：从入口页面抓外链域名（广度 1 层）。

    不递归深挖——DNS 测量要的是「用户会访问的站点」，不是全站地图。
    """
    import asyncio

    import httpx

    found: set[str] = set()
    queue = list(seeds[:max_pages])
    seen_pages: set[str] = set()
    sem = asyncio.Semaphore(concurrency)

    async def one(url: str) -> None:
        async with sem:
            if url in seen_pages:
                return
            seen_pages.add(url)
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, verify=False
                ) as client:
                    r = await client.get(url)
                    html = r.text[:500_000]
            except Exception:
                return
            for m in _DOMAIN_RE.finditer(html):
                host = _host_of(m.group(0))
                if host:
                    found.add(host)

    if not queue:
        return []
    with contextlib.suppress(Exception):
        await asyncio.gather(*(one(u) for u in queue))
    return sorted(found)


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------
def _classify(name: str, default_group: str = "") -> str:
    """定 group：TLD 优先，分区次之，都不行按常见 TLD 兜底。"""
    low = name.lower()
    if low.endswith(_CN_TLDS):
        return "cn"
    if default_group in ("cn", "global"):
        return default_group
    # 兜底：主流通用 TLD 默认算境外，避免把整批未知域名都塞进国内组
    return "global"


def build_report(
    use_browser: bool = True,
    use_seed: bool = True,
    online: bool = False,
    crawl_seeds: list[str] | None = None,
    tier1_size: int = 60,
    tier2_size: int = 300,
    min_hits: int = 2,
) -> DiscoverReport:
    rep = DiscoverReport()
    merged: dict[str, Discovered] = {}

    def add(d: Discovered) -> None:
        old = merged.get(d.name)
        if old is None:
            merged[d.name] = d
            return
        # 合并：保留更高优先级（tier 更小）与更大命中数
        merged[d.name] = Discovered(
            name=d.name,
            group=old.group if old.group == d.group else _classify(d.name, d.group),
            tier=min(old.tier, d.tier),
            hits=max(old.hits, d.hits),
            source=old.source if old.hits >= d.hits else d.source,
            note=old.note or d.note,
        )

    # 浏览器历史
    history: dict[str, int] = {}
    if use_browser:
        history = mine_browser_history(min_hits=min_hits)
        if history:
            rep.sources.append(f"浏览器历史 {len(history)} 个域名")
            rep.browser_domains = len(history)

    # 种子
    seeds: list[tuple[str, str]] = []
    if use_seed:
        seeds = load_seeds()
        if seeds:
            rep.sources.append(f"内置种子 {len(seeds)} 个域名")
            rep.seed_domains = len(seeds)

    # 在线与爬虫
    extra: list[str] = []
    if online:
        got = fetch_online_list()
        if got:
            rep.sources.append(f"在线榜单 {len(got)} 个域名")
            extra += got
    if crawl_seeds:
        import asyncio

        got = asyncio.run(crawl_domains(crawl_seeds))
        if got:
            rep.sources.append(f"入口爬虫 {len(got)} 个域名")
            rep.crawled_domains = len(got)
            extra += got

    # 核心白名单必须无条件入池：种子文件可能没收录其中某些域名，
    # 但「测不到 github.com」这种事不该发生——它是判断国外解析准不准的关键样本。
    for name in _TIER1_CORE:
        add(
            Discovered(
                name=name,
                group=_classify(name, "global" if name in _TIER1_CORE_GLOBAL else "cn"),
                tier=1,
                hits=history.get(name, 0),
                source="core",
            )
        )

    # 种子入池：核心白名单 tier1；CDN 资源节点 tier3（测一百个图片节点没意义）；其余 tier2
    for name, group, section in seeds:
        if name in _TIER1_CORE or history.get(name, 0) >= 20:
            tier = 1
        elif "CDN" in section:
            tier = 3
        else:
            tier = 2
        add(
            Discovered(
                name=name,
                group=_classify(name, group),
                tier=tier,
                hits=history.get(name, 0),
                source="seed",
            )
        )

    # 在线/爬虫来的额外域名：长尾，tier3（除非历史里很常用）
    for name in extra:
        hits = history.get(name, 0)
        tier = 3 if hits < 3 else 2
        add(
            Discovered(
                name=name,
                group=_classify(name, "global"),
                tier=tier,
                hits=hits,
                source="crawl" if crawl_seeds else "online",
            )
        )

    # 历史域名入池：访问越频繁，层越高
    if history:
        ranked = sorted(history.items(), key=lambda kv: kv[1], reverse=True)
        for idx, (name, hits) in enumerate(ranked):
            if idx < tier1_size:
                tier = 1
            elif idx < tier1_size + tier2_size:
                tier = 2
            else:
                tier = 3
            add(
                Discovered(
                    name=name,
                    group=_classify(name, "cn"),
                    tier=tier,
                    hits=hits,
                    source="browser",
                    note=f"访问 {hits} 次",
                )
            )

    # tier2 收口到 tier2_size：一个家庭真正常用的站点不会超过几百个，
    # 剩下的降为长尾层。否则跑 --tier 2 会变成上千域名 × 多台 DNS 的灾难。
    tier2_pool = sorted(
        [d for d in merged.values() if d.tier == 2],
        key=lambda d: (-d.hits, "www." not in d.name, d.name),
    )
    for idx, d in enumerate(tier2_pool):
        if idx >= tier2_size:
            merged[d.name] = Discovered(
                name=d.name, group=d.group, tier=3, hits=d.hits,
                source=d.source, note=d.note,
            )

    rep.items = sorted(merged.values(), key=lambda d: d.sort_key())
    return rep


def write_toml(rep: DiscoverReport, out: Path, max_items: int = 0) -> int:
    items = rep.items
    if max_items and len(items) > max_items:
        items = items[:max_items]

    lines: list[str] = [
        "# dnspick 域名集（由 dnspick discover 生成）",
        "#",
        "# group 只有 cn / global 两种取值，决定计入哪个分组。",
        "# 启用/禁用状态由域名管理器在 GUI 中控制。",
        "",
    ]
    for d in items:
        note = d.note or d.source
        lines.append("[[domain]]")
        lines.append(f'name = "{d.name}"')
        lines.append(f'group = "{d.group}"')
        lines.append(f'note = "{note}"')
        lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(items)