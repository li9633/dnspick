"""DomainStore 单元测试。

验证：
    1. 建表 + 种子初始化
    2. CRUD：get_all / get_enabled / upsert / set_enabled / delete
    3. 统计：count_by_source / count_by_group
    4. reseed 幂等性
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dnspick.domain_store import DomainStore
from dnspick.models import DomainSpec, Group


@pytest.fixture
def store(tmp_path: Path) -> DomainStore:
    """使用临时数据库。"""
    path = tmp_path / "dnspick.db"
    return DomainStore(path)


# -- 建表 ----------------------------------------------------------------

def test_init_creates_tables(store: DomainStore) -> None:
    store.init()
    conn = sqlite3.connect(str(store._path))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='domain'"
    )
    assert cursor.fetchone() is not None


def test_init_is_idempotent(store: DomainStore) -> None:
    store.init()
    store.init()
    conn = sqlite3.connect(str(store._path))
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='domain'"
    )
    assert cursor.fetchone() is not None


# -- 种子 ----------------------------------------------------------------

def test_seed_includes_builtin(store: DomainStore) -> None:
    store.init()
    builtins = store.get_by_source("builtin")
    assert len(builtins) >= 22

    names = {d.name for d in builtins}
    assert "www.baidu.com" in names
    assert "www.bilibili.com" in names


def test_seed_includes_file_domains(store: DomainStore) -> None:
    store.init()
    all_doms = store.get_all()
    assert len(all_doms) > 900


def test_seed_is_idempotent(store: DomainStore) -> None:
    store.init()
    first = len(store.get_all())
    store.init()
    second = len(store.get_all())
    assert first == second


def test_seed_builtin_has_notes(store: DomainStore) -> None:
    store.init()
    builtins = store.get_by_source("builtin")
    for d in builtins:
        if d.name == "www.bilibili.com":
            assert d.note, "内置域名应带分组备注"


def test_reseed_overwrite(store: DomainStore) -> None:
    store.init()
    b, f = store.reseed(overwrite=True)
    assert b >= 20, f"builtin 种子应 >= 20，实际 {b}"
    assert f > 0, f"文件种子应 > 0，实际 {f}"
    # overwrite 后总数应不变（先删后写）
    second = len(store.get_by_source("builtin"))
    assert second == b + f


# -- 读取 ----------------------------------------------------------------

def test_get_all_returns_all(store: DomainStore) -> None:
    store.init()
    all_doms = store.get_all()
    assert len(all_doms) > 0
    assert all(isinstance(d, DomainSpec) for d in all_doms)


def test_get_enabled_all_enabled_by_default(store: DomainStore) -> None:
    store.init()
    all_doms = store.get_all()
    enabled = store.get_enabled()
    assert len(enabled) == len(all_doms)


def test_get_by_source_filter(store: DomainStore) -> None:
    store.init()
    builtins = store.get_by_source("builtin")
    for d in builtins:
        assert d.note is not None


def test_exists(store: DomainStore) -> None:
    store.init()
    assert store.exists("www.baidu.com") is True
    assert store.exists("does-not-exist.example.com") is False


def test_get_enabled_respects_disabled(store: DomainStore) -> None:
    store.init()
    all_doms = store.get_all()
    store.set_enabled(all_doms[0].name, False)
    enabled = store.get_enabled()
    assert len(enabled) == len(all_doms) - 1

    names = {d.name for d in enabled}
    assert all_doms[0].name not in names


# -- 写入 ----------------------------------------------------------------

def test_upsert_inserts_new(store: DomainStore) -> None:
    store.init()
    store.upsert("example.com", Group.CN, "user", "test domain")
    assert store.exists("example.com")
    doms = store.get_by_source("user")
    assert len(doms) == 1
    assert doms[0].name == "example.com"
    assert doms[0].group == Group.CN


def test_upsert_updates_existing(store: DomainStore) -> None:
    store.init()
    store.upsert("example.com", Group.CN, "user", "original")
    store.upsert("example.com", Group.GLOBAL, "user", "updated")
    doms = store.get_by_source("user")
    assert len(doms) == 1
    assert doms[0].group == Group.GLOBAL


def test_upsert_batch(store: DomainStore) -> None:
    store.init()
    count = store.upsert_batch(
        ["a.example.com", "b.example.com", "c.example.com"],
        Group.CN, "test", "batch"
    )
    assert count == 3
    doms = store.get_by_source("test")
    assert len(doms) == 3


def test_set_enabled_toggle(store: DomainStore) -> None:
    store.init()
    all_doms = store.get_all()
    name = all_doms[0].name

    store.set_enabled(name, False)
    enabled = store.get_enabled()
    assert name not in {d.name for d in enabled}

    store.set_enabled(name, True)
    enabled = store.get_enabled()
    assert name in {d.name for d in enabled}


def test_set_enabled_batch(store: DomainStore) -> None:
    store.init()
    all_doms = store.get_all()
    names = {d.name for d in all_doms[:3]}

    store.set_enabled_batch(names, False)
    enabled = store.get_enabled()
    for name in names:
        assert name not in {d.name for d in enabled}


def test_delete_only_non_builtin(store: DomainStore) -> None:
    store.init()
    store.upsert("user.example.com", Group.CN, "user", "test")

    assert store.delete("user.example.com") is True
    assert not store.exists("user.example.com")

    all_names = {d.name for d in store.get_all()}
    assert "www.baidu.com" in all_names
    assert store.delete("www.baidu.com") is False
    assert "www.baidu.com" in {d.name for d in store.get_all()}


def test_delete_batch(store: DomainStore) -> None:
    store.init()
    store.upsert("u1.example.com", Group.CN, "user", "")
    store.upsert("u2.example.com", Group.GLOBAL, "user", "")
    store.upsert("u3.example.com", Group.CN, "user", "")

    deleted = store.delete_batch({"u1.example.com", "u2.example.com", "www.baidu.com"})
    # builtin 域名不可删除，只删 user 源的
    assert deleted == 2
    assert not store.exists("u1.example.com")
    assert not store.exists("u2.example.com")
    assert store.exists("u3.example.com")
    assert store.exists("www.baidu.com")

    # 空集不报错
    assert store.delete_batch(set()) == 0


# -- 统计 ----------------------------------------------------------------

def test_count_by_source(store: DomainStore) -> None:
    store.init()
    counts = store.count_by_source()
    assert "builtin" in counts
    assert counts["builtin"] > 0


def test_count_by_group(store: DomainStore) -> None:
    store.init()
    counts = store.count_by_group()
    assert "cn" in counts or "global" in counts
    total = sum(counts.values())
    assert total == len(store.get_enabled())