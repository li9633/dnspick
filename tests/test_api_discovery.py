"""API 域名发现 —— pytest 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dnspick.api_discovery import (
    _extract_domains_from_crt,
    _extract_domains_from_html,
    _extract_host,
    _is_valid_domain,
    _root_domain,
)
from dnspick.relation_store import RelationStore

# ===== 域名校验 =====


def test_is_valid_domain_basic() -> None:
    assert _is_valid_domain("api.bilibili.com")
    assert _is_valid_domain("i0.hdslb.com")
    assert _is_valid_domain("cdn.jsdelivr.net")
    assert _is_valid_domain("www.google-analytics.com")
    assert _is_valid_domain("www.example.co.uk")


def test_is_valid_domain_rejects_invalid() -> None:
    assert not _is_valid_domain("")
    assert not _is_valid_domain("localhost")
    assert not _is_valid_domain("127.0.0.1")
    assert not _is_valid_domain("::1")
    assert not _is_valid_domain("not_a_domain")
    assert not _is_valid_domain("-bad.com")
    assert not _is_valid_domain("bad-.com")


def test_is_valid_domain_rejects_deprecated() -> None:
    assert not _is_valid_domain("test-old.bilibili.com")
    assert not _is_valid_domain("staging.api.example.com")
    assert not _is_valid_domain("dev.myapp.com")
    assert not _is_valid_domain("deleted-service.example.com")


def test_is_valid_domain_rejects_numeric_tld() -> None:
    assert not _is_valid_domain("333.1007")
    assert not _is_valid_domain("version.123")


def test_is_valid_domain_rejects_non_ascii() -> None:
    assert not _is_valid_domain("中文.com")
    # punycode 编码的 IDN 域名是合法的 ASCII 域名
    assert _is_valid_domain("xn--fiq228c.com")


def test_is_valid_domain_rejects_consecutive_dashes() -> None:
    assert not _is_valid_domain("www.bilibili.com-------------------1.2pxx.com")


# ===== 域名提取 =====


def test_extract_host_full_url() -> None:
    assert _extract_host("https://api.bilibili.com/x/v2") == "api.bilibili.com"
    assert _extract_host("http://i0.hdslb.com/bfs/1.jpg") == "i0.hdslb.com"
    assert _extract_host("https://www.google.com/") == "www.google.com"


def test_extract_host_protocol_relative() -> None:
    assert _extract_host("//cdn.jsdelivr.net/npm/vue") == "cdn.jsdelivr.net"
    assert _extract_host("//s1.hdslb.com/bfs/static/main.js") == "s1.hdslb.com"


def test_extract_host_non_http() -> None:
    assert _extract_host("data:image/png;base64,xxx") is None
    assert _extract_host("javascript:void(0)") is None
    assert _extract_host("mailto:test@example.com") is None


def test_extract_host_invalid() -> None:
    assert _extract_host("") is None
    assert _extract_host("not-a-url") is None


# ===== 根域提取 =====


def test_root_domain_simple() -> None:
    assert _root_domain("www.bilibili.com") == "bilibili.com"
    assert _root_domain("api.bilibili.com") == "bilibili.com"
    assert _root_domain("bilibili.com") == "bilibili.com"
    assert _root_domain("github.com") == "github.com"


def test_root_domain_two_level_tld() -> None:
    assert _root_domain("www.example.co.uk") == "example.co.uk"
    assert _root_domain("sub.example.co.uk") == "example.co.uk"
    assert _root_domain("api.example.com.cn") == "example.com.cn"


def test_root_domain_short() -> None:
    assert _root_domain("local.host") == "local.host"


# ===== HTML 域名提取 =====


def test_extract_domains_from_html() -> None:
    html = """<html>
<head>
    <link href="https://static.hdslb.com/css/base.css" rel="stylesheet">
    <link href="//s1.hdslb.com/bfs/static/main.js" rel="preload" as="script">
    <script src="https://s1.hdslb.com/bfs/static/js/main.js"></script>
    <script src="//cdn.jsdelivr.net/npm/vue@2"></script>
</head>
<body>
    <img src="https://i0.hdslb.com/bfs/archive/abc.jpg">
    <img data-src="https://i1.hdslb.com/bfs/archive/def.jpg">
    <iframe src="https://player.bilibili.com/player.html"></iframe>
    <video>
        <source src="https://upos-sz-mirrorcos.bilivideo.com/1.mp4">
    </video>
    <meta content="https://www.bilibili.com/og-image.jpg" property="og:image">
    <a href="https://www.bilibili.com/video/BV1xx"></a>
    <div style="background: url('https://i2.hdslb.com/bg.png')"></div>
</body>
</html>"""
    domains = _extract_domains_from_html(html)

    assert "static.hdslb.com" in domains
    assert "s1.hdslb.com" in domains
    assert "cdn.jsdelivr.net" in domains
    assert "i0.hdslb.com" in domains
    assert "i1.hdslb.com" in domains
    assert "player.bilibili.com" in domains
    assert "upos-sz-mirrorcos.bilivideo.com" in domains
    assert "i2.hdslb.com" in domains
    assert "www.bilibili.com" in domains


def test_extract_domains_skips_non_http() -> None:
    html = """
    <a href="javascript:void(0)">click</a>
    <a href="mailto:test@example.com">email</a>
    <img src="data:image/png;base64,iVBOR...">
    <a href="#section">anchor</a>
    <script src="https://real.cdn.com/app.js"></script>
    """
    domains = _extract_domains_from_html(html)
    assert "real.cdn.com" in domains
    assert len(domains) == 1


def test_extract_domains_html5_tags() -> None:
    html = """
    <video src="https://media.example.com/v.mp4" poster="https://img.example.com/p.jpg"></video>
    <audio src="https://audio.example.com/s.mp3"></audio>
    <source src="https://source.example.com/v.webm">
    """
    domains = _extract_domains_from_html(html)
    assert len(domains) == 4


# ===== crt.sh 域名提取 =====


def test_extract_domains_from_crt() -> None:
    entries = [
        {"name_value": "*.bilibili.com\nbilibili.com"},
        {"name_value": "api.bilibili.com"},
        {"name_value": "data.bilibili.com\npassport.bilibili.com"},
        {"name_value": "test-old.bilibili.com"},
        {"name_value": "staging.api.bilibili.com"},
        {"name_value": "*.hdslb.com"},
    ]
    domains = _extract_domains_from_crt(entries)

    assert "bilibili.com" in domains
    assert "api.bilibili.com" in domains
    assert "data.bilibili.com" in domains
    assert "passport.bilibili.com" in domains
    assert "hdslb.com" in domains
    assert "test-old.bilibili.com" not in domains
    assert "staging.api.bilibili.com" not in domains


# ===== RelationStore CRUD =====


def test_relation_store_upsert_and_query(tmp_path: Path) -> None:
    db = tmp_path / "test_dr.db"
    store = RelationStore(db)

    store.upsert("www.bilibili.com", "api.bilibili.com", "html")
    store.upsert("www.bilibili.com", "i0.hdslb.com", "html")
    store.upsert("www.bilibili.com", "api.bilibili.com", "crt_sh")

    all_r = store.get_all_relations("www.bilibili.com")
    assert len(all_r) == 3


def test_relation_store_count(tmp_path: Path) -> None:
    store = RelationStore(tmp_path / "test_dr.db")

    store.upsert("main.example.com", "d1.example.com", "html")
    store.upsert("main.example.com", "d2.example.com", "html")

    assert store.count_relations("main.example.com", min_cache_days=365) == 2
    assert store.count_relations("nonexistent.example.com") == 0


def test_relation_store_batch(tmp_path: Path) -> None:
    store = RelationStore(tmp_path / "test_dr.db")

    count = store.upsert_batch("main.example.com", {"a.com", "b.com", "c.com"}, "html")
    assert count == 3

    # 重复写入不增加行数
    store.upsert_batch("main.example.com", {"a.com", "b.com"}, "html")
    assert store.count_relations("main.example.com", min_cache_days=365) == 3


def test_relation_store_reverse(tmp_path: Path) -> None:
    store = RelationStore(tmp_path / "test_dr.db")

    store.upsert("site-a.example.com", "cdn.jsdelivr.net", "html")
    store.upsert("site-b.example.com", "cdn.jsdelivr.net", "html")

    rev = store.get_reverse_relations("cdn.jsdelivr.net")
    assert sorted(rev) == ["site-a.example.com", "site-b.example.com"]


def test_relation_store_main_domains(tmp_path: Path) -> None:
    store = RelationStore(tmp_path / "test_dr.db")

    store.upsert("www.taobao.com", "img.alicdn.com", "html")
    store.upsert("www.bilibili.com", "i0.hdslb.com", "html")

    mains = store.get_all_main_domains()
    assert "www.taobao.com" in mains
    assert "www.bilibili.com" in mains


def test_relation_store_delete(tmp_path: Path) -> None:
    store = RelationStore(tmp_path / "test_dr.db")

    store.upsert_batch("main.example.com", {"a.com", "b.com"}, "html")
    assert store.count_relations("main.example.com", min_cache_days=365) == 2

    deleted = store.delete_relations("main.example.com")
    assert deleted == 2
    assert store.count_relations("main.example.com", min_cache_days=365) == 0


def test_relation_store_delete_stale(tmp_path: Path) -> None:
    store = RelationStore(tmp_path / "test_dr.db")

    # 正常写入（会在 now 时间戳，肯定不会过期）
    store.upsert("main.example.com", "fresh.example.com", "html")
    assert store.count_relations("main.example.com", min_cache_days=365) == 1

    # stale 清理（90 天阈值，新数据不会被删）
    deleted = store.delete_stale(older_than_days=90)
    assert deleted == 0
    assert store.count_relations("main.example.com", min_cache_days=365) == 1