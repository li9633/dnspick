"""域名关系存储：domain_relation 表的 CRUD。

与 store.py 完全独立，互不引用、互不依赖。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

DB_PATH = Path("history") / "dnspick.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_relation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    main_domain     TEXT NOT NULL,
    related_domain  TEXT NOT NULL,
    source          TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(main_domain, related_domain, source)
);

CREATE INDEX IF NOT EXISTS idx_dr_main
    ON domain_relation(main_domain);
CREATE INDEX IF NOT EXISTS idx_dr_related
    ON domain_relation(related_domain);
CREATE INDEX IF NOT EXISTS idx_dr_last_seen
    ON domain_relation(main_domain, last_seen_at);
"""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class RelationStore:
    """domain_relation 表的读写封装。"""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path

    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _conn(self):
        self._ensure_dir()
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        """建表（幂等）。"""
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # -- 写入 ------------------------------------------------------------

    def upsert(self, main_domain: str, related_domain: str, source: str) -> None:
        """写入或更新一条域名关系。"""
        self.init()
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO domain_relation
                   (main_domain, related_domain, source, first_seen_at, last_seen_at, seen_count)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(main_domain, related_domain, source) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   seen_count = seen_count + 1""",
                (main_domain, related_domain, source, now, now),
            )

    def upsert_batch(self, main_domain: str, related_domains: set[str], source: str) -> int:
        """批量写入，返回实际写入数。"""
        count = 0
        for rd in related_domains:
            self.upsert(main_domain, rd, source)
            count += 1
        return count

    # -- 查询 ------------------------------------------------------------

    def get_relations(
        self, main_domain: str, min_cache_days: int = 7
    ) -> list[str]:
        """获取指定主域名的关联域名列表（缓存有效期内的）。"""
        self.init()
        cutoff = (
            datetime.now(UTC) - timedelta(days=min_cache_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT related_domain FROM domain_relation WHERE main_domain = ? AND last_seen_at > ?",
                (main_domain, cutoff),
            ).fetchall()
        return [r[0] for r in rows]

    def get_all_relations(self, main_domain: str) -> list[dict]:
        """获取指定主域名的全部关联域名（不过滤时间）。调试和分析用。"""
        self.init()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM domain_relation WHERE main_domain = ? ORDER BY last_seen_at DESC",
                (main_domain,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_reverse_relations(self, related_domain: str) -> list[str]:
        """反向查询：哪些主域名引用了这个域名。"""
        self.init()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT main_domain FROM domain_relation WHERE related_domain = ?",
                (related_domain,),
            ).fetchall()
        return [r[0] for r in rows]

    def count_relations(self, main_domain: str, min_cache_days: int = 7) -> int:
        """统计缓存有效期内的关系数量。"""
        self.init()
        cutoff = (
            datetime.now(UTC) - timedelta(days=min_cache_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM domain_relation WHERE main_domain = ? AND last_seen_at > ?",
                (main_domain, cutoff),
            ).fetchone()
        return row[0] if row else 0

    def get_all_main_domains(self) -> list[str]:
        """获取所有已记录的主域名。"""
        self.init()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT main_domain FROM domain_relation"
            ).fetchall()
        return [r[0] for r in rows]

    # -- 清理 ------------------------------------------------------------

    def delete_relations(self, main_domain: str) -> int:
        """删除指定主域名的全部关系记录。"""
        self.init()
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM domain_relation WHERE main_domain = ?", (main_domain,)
            )
            return cur.rowcount

    def delete_stale(self, older_than_days: int = 90) -> int:
        """清理长时间未确认的记录。"""
        self.init()
        cutoff = (
            datetime.now(UTC) - timedelta(days=older_than_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM domain_relation WHERE last_seen_at < ? AND seen_count < 3",
                (cutoff,),
            )
            return cur.rowcount