"""域名主存储：domain 表的 CRUD + 种子初始化。

与 store.py / relation_store.py 共用 history/dnspick.db，逻辑独立。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from dnspick import builtin
from dnspick.discover import SEED_FILE, load_seeds
from dnspick.models import DomainSpec, Group

DB_PATH = Path("history") / "dnspick.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    group_name      TEXT NOT NULL DEFAULT 'cn',
    source          TEXT NOT NULL,
    note            TEXT DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    visit_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_domain_source ON domain(source);
CREATE INDEX IF NOT EXISTS idx_domain_enabled ON domain(enabled);
CREATE INDEX IF NOT EXISTS idx_domain_group ON domain(group_name);
"""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class DomainStore:
    """domain 表的读写封装。所有域名数据的唯一入口。"""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path

    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _conn(self):
        self._ensure_dir()
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- 初始化 -----------------------------------------------------------

    def init(self) -> None:
        """建表 + 种子（如未初始化）。幂等。"""
        self._ensure_dir()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
        self._seed_if_empty()

    # -- 种子 -------------------------------------------------------------

    def _seed_if_empty(self) -> None:
        """首次运行：写入全部种子数据。已存在则跳过。"""
        if self._has_builtin_sources():
            return
        self._seed_from_builtin()
        self._seed_from_file()

    def _has_builtin_sources(self) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM domain WHERE source='builtin' LIMIT 1"
            ).fetchone()
            return row is not None

    def _seed_from_builtin(self) -> int:
        """写入 builtin.py 中的 22 个精选 CDN 域名。先写，带优质备注。"""
        count = 0
        now = _now_iso()
        with self._conn() as conn:
            for d in builtin.seed_domains():
                try:
                    conn.execute(
                        """INSERT INTO domain (name, group_name, source, note, visit_count, created_at, updated_at)
                           VALUES (?, ?, 'builtin', ?, 0, ?, ?)""",
                        (d.name, d.group.value, d.note, now, now),
                    )
                    count += 1
                except sqlite3.IntegrityError:
                    pass
        return count

    def _seed_from_file(self) -> int:
        """写入 seed_domains.txt 的 ~1000 个域名。后写，不覆盖 builtin 备注。"""
        seeds = load_seeds(SEED_FILE)
        count = 0
        now = _now_iso()
        with self._conn() as conn:
            for name, group, section in seeds:
                group_name = group if group in ("cn", "global") else "cn"
                try:
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO domain
                           (name, group_name, source, note, visit_count, created_at, updated_at)
                           VALUES (?, ?, 'builtin', ?, 0, ?, ?)""",
                        (name, group_name, section, now, now),
                    )
                    if cursor.rowcount > 0:
                        count += 1
                except sqlite3.IntegrityError:
                    pass
        return count

    def reseed(self, overwrite: bool = False) -> tuple[int, int]:
        """强制重新种子。overwrite=True 会先清空所有 source='builtin' 的行。
        返回 (from_builtin, from_file) 的写入条数。"""
        if overwrite:
            with self._conn() as conn:
                conn.execute("DELETE FROM domain WHERE source='builtin'")
        b = self._seed_from_builtin()
        f = self._seed_from_file()
        return (b, f)

    # -- 读取 -------------------------------------------------------------

    def get_all(self) -> list[DomainSpec]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, group_name, source, note, enabled, visit_count FROM domain ORDER BY source, name"
            ).fetchall()
        return [self._row_to_spec(r) for r in rows]

    def get_enabled(self) -> list[DomainSpec]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, group_name, source, note, enabled, visit_count"
                " FROM domain WHERE enabled=1 ORDER BY source, name"
            ).fetchall()
        return [self._row_to_spec(r) for r in rows]

    def get_by_source(self, source: str) -> list[DomainSpec]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, group_name, source, note, enabled, visit_count FROM domain WHERE source=? ORDER BY name",
                (source,),
            ).fetchall()
        return [self._row_to_spec(r) for r in rows]

    def exists(self, name: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM domain WHERE name=? LIMIT 1", (name,)
            ).fetchone()
            return row is not None

    @staticmethod
    def _row_to_spec(row: tuple) -> DomainSpec:
        name, group_name, source, note, enabled, visit_count = row
        return DomainSpec(
            name=name,
            group=Group(group_name),
            note=note or "",
        )

    # -- 写入 -------------------------------------------------------------

    def upsert(self, name: str, group: Group, source: str,
               note: str = "", visit_count: int = 0) -> None:
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO domain (name, group_name, source, note, enabled, visit_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     group_name=excluded.group_name,
                     source=excluded.source,
                     note=CASE WHEN domain.source='builtin' THEN domain.note ELSE excluded.note END,
                     visit_count=MAX(domain.visit_count, excluded.visit_count),
                     updated_at=excluded.updated_at""",
                (name, group.value, source, note, visit_count, now, now),
            )

    def upsert_batch(self, names: list[str], group: Group, source: str, note: str = "") -> int:
        count = 0
        now = _now_iso()
        with self._conn() as conn:
            for name in names:
                try:
                    conn.execute(
                        """INSERT INTO domain (name, group_name, source, note, enabled, created_at, updated_at)
                           VALUES (?, ?, ?, ?, 1, ?, ?)
                           ON CONFLICT(name) DO UPDATE SET
                             group_name=excluded.group_name,
                             note=CASE WHEN domain.source='builtin' THEN domain.note ELSE excluded.note END,
                             updated_at=excluded.updated_at""",
                        (name, group.value, source, note, now, now),
                    )
                    count += 1
                except sqlite3.IntegrityError:
                    pass
        return count

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE domain SET enabled=?, updated_at=? WHERE name=?",
                (int(enabled), _now_iso(), name),
            )

    def set_enabled_batch(self, names: set[str], enabled: bool) -> None:
        now = _now_iso()
        with self._conn() as conn:
            conn.executemany(
                "UPDATE domain SET enabled=?, updated_at=? WHERE name=?",
                [(int(enabled), now, n) for n in names],
            )

    def delete(self, name: str) -> bool:
        """仅对 source!='builtin' 的域名执行物理删除。返回是否成功。"""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM domain WHERE name=? AND source!='builtin'", (name,)
            )
            return cursor.rowcount > 0

    def delete_batch(self, names: set[str]) -> int:
        """批量删除非内置域名。返回成功删除数量。"""
        if not names:
            return 0
        with self._conn() as conn:
            placeholders = ",".join("?" * len(names))
            cursor = conn.execute(
                f"DELETE FROM domain WHERE name IN ({placeholders}) AND source!='builtin'",
                tuple(names),
            )
            return cursor.rowcount

    # -- 统计 -------------------------------------------------------------

    def count_by_source(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM domain WHERE enabled=1 GROUP BY source"
            ).fetchall()
        return dict(rows)

    def count_by_group(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT group_name, COUNT(*) FROM domain WHERE enabled=1 GROUP BY group_name"
            ).fetchall()
        return dict(rows)