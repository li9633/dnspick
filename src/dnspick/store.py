"""SQLite 结果存储与查询层。

替代之前的 JSON 文件方案，支持：
- 筛选（协议 / 地区 / 标签 / 结论 / 分数）
- 分组对比（按服务器名 + 协议次级分组）
- 时间趋势
- 旧 JSON 数据自动迁移
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dnspick.models import RunResult

DB_PATH = Path("history") / "dnspick.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurement (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    profile     TEXT NOT NULL DEFAULT 'balanced',
    public_ip   TEXT,
    ecs_subnet  TEXT,
    duration_s  REAL NOT NULL DEFAULT 0,
    servers_cnt INTEGER NOT NULL DEFAULT 0,
    domains_cnt INTEGER NOT NULL DEFAULT 0,
    unique_ips  INTEGER NOT NULL DEFAULT 0,
    api_discovered INTEGER NOT NULL DEFAULT 0,
    warning     TEXT DEFAULT '',
    config_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS server_score (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id  TEXT NOT NULL REFERENCES measurement(id) ON DELETE CASCADE,
    server_id       TEXT NOT NULL,
    server_name     TEXT NOT NULL,
    address         TEXT NOT NULL,
    protocol        TEXT NOT NULL,
    vendor          TEXT NOT NULL DEFAULT '',
    region          TEXT NOT NULL DEFAULT 'unknown',
    tags            TEXT NOT NULL DEFAULT '',
    total_score     REAL NOT NULL DEFAULT 0,
    verdict         TEXT NOT NULL DEFAULT 'avoid',
    advice          TEXT NOT NULL DEFAULT '',
    cn_delay_ms     REAL,
    gl_delay_ms     REAL,
    cn_hops         REAL,
    gl_hops         REAL,
    cn_success_rate REAL,
    gl_success_rate REAL,
    cn_reach_rate   REAL,
    gl_reach_rate   REAL,
    cn_proximity    REAL,
    gl_proximity    REAL,
    cn_score        REAL NOT NULL DEFAULT 0,
    gl_score        REAL NOT NULL DEFAULT 0,
    cn_stability    REAL NOT NULL DEFAULT 0,
    gl_stability    REAL NOT NULL DEFAULT 0,
    cn_speed        REAL NOT NULL DEFAULT 0,
    gl_speed        REAL NOT NULL DEFAULT 0,
    cn_path         REAL NOT NULL DEFAULT 0,
    gl_path         REAL NOT NULL DEFAULT 0,
    dns_rtt_p50_ms  REAL,
    hijack          INTEGER NOT NULL DEFAULT 0,
    ecs_supported   INTEGER,
    polluted        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_score_measurement ON server_score(measurement_id);
CREATE INDEX IF NOT EXISTS idx_score_protocol ON server_score(protocol);
CREATE INDEX IF NOT EXISTS idx_score_region ON server_score(region);
CREATE INDEX IF NOT EXISTS idx_score_verdict ON server_score(verdict);
CREATE INDEX IF NOT EXISTS idx_score_vendor ON server_score(vendor);
CREATE INDEX IF NOT EXISTS idx_measurement_created ON measurement(created_at DESC);
"""


@dataclass
class ScoreFilter:
    """server_score 表的筛选条件。全部为 None 表示不过滤。"""

    protocols: list[str] | None = None
    regions: list[str] | None = None
    vendors: list[str] | None = None
    tags: list[str] | None = None
    verdicts: list[str] | None = None
    server_ids: list[str] | None = None
    min_score: float | None = None
    max_score: float | None = None


@dataclass
class SubGroup:
    key: str
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GroupResult:
    key: str
    sub_groups: list[SubGroup] = field(default_factory=list)


class ResultStore:
    """SQLite 结果存储。CLI 和 GUI 共用。"""

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
        """创建表结构（幂等）。"""
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # -- 写入 -------------------------------------------------------------
    def save(
        self,
        result: RunResult,
        config_snapshot: dict[str, Any] | None = None,
    ) -> str:
        """保存一次完整测量结果，返回 measurement_id。"""
        self.init()
        s = result.summary
        now = datetime.now(UTC).astimezone()
        mid = now.strftime("%Y%m%dT%H%M%S")

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO measurement
                   (id, created_at, profile, public_ip, ecs_subnet, duration_s,
                    servers_cnt, domains_cnt, unique_ips, api_discovered, warning, config_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                    config_snapshot.get("profile", "") if config_snapshot else "",
                    s.public_ip,
                    s.ecs_subnet,
                    s.duration_s,
                    s.servers,
                    s.domains,
                    s.unique_ips,
                    s.api_discovered,
                    s.warning,
                    json.dumps(config_snapshot or {}, ensure_ascii=False),
                ),
            )

            for rep in result.reports:
                sv = rep.server
                tags = ",".join(sv.tags) if sv.tags else ""
                conn.execute(
                    """INSERT INTO server_score
                       (measurement_id, server_id, server_name, address, protocol,
                        vendor, region, tags, total_score, verdict, advice,
                        cn_delay_ms, gl_delay_ms, cn_hops, gl_hops,
                        cn_success_rate, gl_success_rate, cn_reach_rate, gl_reach_rate,
                        cn_proximity, gl_proximity, cn_score, gl_score,
                        cn_stability, gl_stability, cn_speed, gl_speed,
                        cn_path, gl_path, dns_rtt_p50_ms, hijack, ecs_supported, polluted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?,
                               ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mid, sv.id, sv.name, sv.address, sv.protocol.value,
                        getattr(sv, 'vendor', ''), sv.region, tags,
                        rep.total, rep.verdict.value, rep.advice,
                        rep.cn.tcp_p50_ms, rep.glob.tcp_p50_ms,
                        rep.cn.hop_avg, rep.glob.hop_avg,
                        rep.cn.resolve_success_rate, rep.glob.resolve_success_rate,
                        rep.cn.ip_reachable_rate, rep.glob.ip_reachable_rate,
                        rep.cn.proximity, rep.glob.proximity,
                        rep.cn.score, rep.glob.score,
                        rep.cn.stability, rep.glob.stability,
                        rep.cn.speed, rep.glob.speed,
                        rep.cn.path, rep.glob.path,
                        rep.dns_rtt_p50_ms,
                        1 if rep.hijack else 0,
                        {
                            True: 1,
                            False: 0,
                            None: None,
                        }.get(rep.ecs_supported),
                        1 if (rep.cn.polluted or rep.glob.polluted) else 0,
                    ),
                )

        return mid

    # -- 历史列表 ---------------------------------------------------------
    def list_measurements(self, limit: int = 50) -> list[dict[str, Any]]:
        """返回测量记录列表（按时间倒序），每条含概要信息。"""
        self.init()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*,
                          (SELECT server_name FROM server_score
                           WHERE measurement_id = m.id
                           ORDER BY total_score DESC LIMIT 1) as top_name,
                          (SELECT total_score FROM server_score
                           WHERE measurement_id = m.id
                           ORDER BY total_score DESC LIMIT 1) as top_score
                   FROM measurement m
                   ORDER BY m.created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- 单次得分查询 -----------------------------------------------------
    def get_scores(
        self,
        measurement_id: str,
        filters: ScoreFilter | None = None,
        order_by: str = "total_score DESC",
    ) -> list[dict[str, Any]]:
        """获取某次测量的 DNS 得分列表（支持筛选排序）。"""
        self.init()
        where = "WHERE measurement_id = ?"
        params: list[Any] = [measurement_id]
        where, params = self._apply_filters(where, params, filters, "AND")
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM server_score {where} ORDER BY {order_by}",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    # -- 分组对比 ---------------------------------------------------------
    def compare(
        self,
        measurement_id: str,
        group_by: str = "server_name",
        sub_group_by: str = "protocol",
        filters: ScoreFilter | None = None,
    ) -> list[GroupResult]:
        """分组对比查询。

        group_by: 主分组字段（server_name / protocol / region / verdict）
        sub_group_by: 次级分组字段
        """
        valid = {"server_name", "protocol", "region", "verdict", "vendor"}
        if group_by not in valid or sub_group_by not in valid:
            raise ValueError(f"group_by/sub_group_by 必须是 {valid} 之一")

        where = "WHERE measurement_id = ?"
        params: list[Any] = [measurement_id]
        where, params = self._apply_filters(where, params, filters, "AND")

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM server_score {where} ORDER BY {group_by}, {sub_group_by}",
                params,
            ).fetchall()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in rows:
            d = dict(row)
            gk = d.get(group_by, "")
            sgk = d.get(sub_group_by, "")
            grouped.setdefault(gk, {}).setdefault(sgk, []).append(d)

        results: list[GroupResult] = []
        for gk in sorted(grouped):
            sub_groups = [
                SubGroup(key=sgk, items=items)
                for sgk, items in grouped[gk].items()
            ]
            results.append(GroupResult(key=gk, sub_groups=sub_groups))
        return results

    # -- 时间趋势 ---------------------------------------------------------
    def trend(
        self,
        server_id: str,
        metric: str = "total_score",
        limit: int = 30,
    ) -> list[tuple[str, float]]:
        """某个 DNS 在指定指标上的时间趋势。"""
        valid_metrics = {
            "total_score", "cn_delay_ms", "gl_delay_ms", "cn_score", "gl_score",
        }
        if metric not in valid_metrics:
            raise ValueError(f"metric 必须是 {valid_metrics} 之一")
        self.init()
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT m.created_at, s.{metric}
                    FROM server_score s
                    JOIN measurement m ON s.measurement_id = m.id
                    WHERE s.server_id = ?
                    ORDER BY m.created_at DESC
                    LIMIT ?""",
                (server_id, limit),
            ).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    # -- 管理 -------------------------------------------------------------
    def delete(self, measurement_id: str) -> None:
        self.init()
        with self._conn() as conn:
            conn.execute("DELETE FROM measurement WHERE id = ?", (measurement_id,))

    def get_config(self, measurement_id: str) -> dict[str, Any]:
        """获取某次测量的配置快照。"""
        self.init()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT config_json FROM measurement WHERE id = ?",
                (measurement_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return {}

    # -- 筛选子句构造 -----------------------------------------------------
    @staticmethod
    def _apply_filters(
        where: str,
        params: list[Any],
        filters: ScoreFilter | None,
        prefix: str = "AND",
    ) -> tuple[str, list[Any]]:
        if filters is None:
            return where, params
        if filters.protocols:
            placeholders = ",".join("?" * len(filters.protocols))
            where += f" {prefix} protocol IN ({placeholders})"
            params.extend(filters.protocols)
            prefix = "AND"
        if filters.regions:
            placeholders = ",".join("?" * len(filters.regions))
            where += f" {prefix} region IN ({placeholders})"
            params.extend(filters.regions)
            prefix = "AND"
        if filters.vendors:
            placeholders = ",".join("?" * len(filters.vendors))
            where += f" {prefix} vendor IN ({placeholders})"
            params.extend(filters.vendors)
            prefix = "AND"
        if filters.tags:
            clauses = " OR ".join("tags LIKE ?" for _ in filters.tags)
            where += f" {prefix} ({clauses})"
            params.extend(f"%{t}%" for t in filters.tags)
            prefix = "AND"
        if filters.verdicts:
            placeholders = ",".join("?" * len(filters.verdicts))
            where += f" {prefix} verdict IN ({placeholders})"
            params.extend(filters.verdicts)
            prefix = "AND"
        if filters.server_ids:
            placeholders = ",".join("?" * len(filters.server_ids))
            where += f" {prefix} server_id IN ({placeholders})"
            params.extend(filters.server_ids)
            prefix = "AND"
        if filters.min_score is not None:
            where += f" {prefix} total_score >= ?"
            params.append(filters.min_score)
            prefix = "AND"
        if filters.max_score is not None:
            where += f" {prefix} total_score <= ?"
            params.append(filters.max_score)
            prefix = "AND"
        return where, params

    # -- 厂商 × 协议 推荐摘要 --------------------------------------------
    def vendor_protocol_summary(
        self,
        measurement_id: str,
        filters: ScoreFilter | None = None,
    ) -> list[dict[str, Any]]:
        """按厂商聚合，返回每个厂商下的协议推荐摘要。"""
        self.init()
        where = "WHERE measurement_id = ?"
        params: list[Any] = [measurement_id]
        where, params = self._apply_filters(where, params, filters, "AND")

        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT vendor, server_name, protocol,
                           MAX(total_score) AS best_score,
                           MIN(cn_delay_ms) AS cn_delay_ms,
                           MAX(CASE WHEN hijack = 1 THEN 1 ELSE 0 END) AS has_hijack
                    FROM server_score
                    {where}
                    GROUP BY vendor, protocol
                    ORDER BY vendor, best_score DESC""",
                params,
            ).fetchall()

        vendor_verdicts: dict[tuple[str, str], str] = {}
        with self._conn() as conn:
            vrows = conn.execute(
                """SELECT vendor, protocol, verdict
                    FROM server_score
                    WHERE measurement_id = ?
                    ORDER BY total_score DESC""",
                (measurement_id,),
            ).fetchall()
            for vr in vrows:
                key = (vr["vendor"], vr["protocol"])
                if key not in vendor_verdicts:
                    vendor_verdicts[key] = vr["verdict"]

        by_vendor: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in rows:
            v = row["vendor"] or "unknown"
            p = row["protocol"]
            by_vendor.setdefault(v, {}).setdefault("_name", row["server_name"])
            by_vendor[v][p] = {
                "protocol": p,
                "score": round(row["best_score"], 1),
                "cn_delay_ms": round(row["cn_delay_ms"], 1)
                if row["cn_delay_ms"] else None,
                "hijack": bool(row["has_hijack"]),
                "verdict": vendor_verdicts.get((v, p), "avoid"),
            }

        results: list[dict[str, Any]] = []
        for vendor, data in by_vendor.items():
            name = data.pop("_name", vendor)
            protocols = list(data.values())
            protocols.sort(key=lambda x: x["score"], reverse=True)
            verdicts = {p["verdict"] for p in protocols}
            results.append({
                "vendor": vendor,
                "display_name": name,
                "protocols": protocols,
                "best_protocol": protocols[0]["protocol"] if protocols else "",
                "has_recommend": "recommend" in verdicts,
                "has_avoid": "avoid" in verdicts,
            })

        results.sort(
            key=lambda x: (
                not x["has_recommend"],
                -sum(1 for p in x["protocols"]
                     if p["verdict"] == "recommend"),
                -max((p["score"] for p in x["protocols"]), default=0),
            )
        )
        return results