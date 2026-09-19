"""阶段三：IP 归属地判定。

定位：解释性信息，**不是**评分的必要输入。
理由：精确的省市/运营商需要 GeoIP 库，而离线库未必齐备、在线 API 未必可达。
所以真正的「就近性」由 TCP 延迟与跳数这些直接观测量来表达，归属地只用于
展示和轻度加权——拿不到就降级为未知，不影响结论成立。

Provider 链：mmdb（用户自备）→ 在线批量（线程池并发）→ 内置网段表（离线兜底）
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from dnspick import builtin
from dnspick.models import IpGeo

_BATCH_URL = "http://ip-api.com/batch?fields=status,countryCode,regionName,city,isp,as,query"
_BATCH_SIZE = 100
_TIMEOUT = 6.0


class GeoIpResolver:
    """三级 provider + 缓存。阻塞部分一律丢线程池。"""

    def __init__(
        self,
        enabled: bool = True,
        mmdb_path: str | None = None,
        extra_cn_prefixes: list[str] | None = None,
        online: bool = True,
        max_workers: int = 16,
    ) -> None:
        self.enabled = enabled
        self.mmdb_path = mmdb_path
        self.online_enabled = online
        self.max_workers = max_workers
        self._cache: dict[str, IpGeo] = {}
        self._cn_nets = self._nets(builtin.builtin_cn_prefixes())
        self._global_nets = self._nets(builtin.builtin_global_prefixes())
        for raw in extra_cn_prefixes or []:
            try:
                self._cn_nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                continue
        self._reader = None
        if mmdb_path:
            self._reader = self._open_mmdb(mmdb_path)

    @staticmethod
    def _nets(cidrs) -> list:
        nets = []
        for c in cidrs:
            try:
                nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                continue
        return nets

    # -- provider 1: mmdb -------------------------------------------------
    @staticmethod
    def _open_mmdb(path: str):
        try:
            import maxminddb  # type: ignore[import-untyped]
        except ImportError:
            return None
        try:
            return maxminddb.open_database(path)
        except Exception:
            return None

    def _from_mmdb(self, ip: str) -> IpGeo | None:
        if self._reader is None:
            return None
        try:
            data = self._reader.get(ip)
        except Exception:
            return None
        if not data:
            return None
        country = None
        region = None
        city = None
        try:
            country = (data.get("country") or {}).get("names", {}).get("zh-CN") or (
                data.get("country") or {}
            ).get("names", {}).get("en")
            subdiv = data.get("subdivisions") or []
            if subdiv:
                region = subdiv[0].get("names", {}).get("zh-CN") or subdiv[0].get("names", {}).get("en")
            city = (data.get("city") or {}).get("names", {}).get("zh-CN")
        except (AttributeError, IndexError, KeyError):
            pass
        isp = None
        if isinstance(data.get("isp"), str):
            isp = data["isp"]
        elif isinstance(data.get("autonomous_system_organization"), str):
            isp = data["autonomous_system_organization"]
        cc = None
        if isinstance(data.get("country"), dict):
            cc = data["country"].get("iso_code")
        geo = IpGeo(
            ip=ip,
            country=country or cc,
            region=region,
            city=city,
            isp=isp,
            asn=str(data.get("autonomous_system_number") or "") or None,
            org=data.get("autonomous_system_organization"),
            source="mmdb",
        )
        geo.is_domestic = (cc == "CN") if cc else self._by_prefix(ip)
        return geo

    # -- provider 3: 内置网段 ---------------------------------------------
    def _by_prefix(self, ip: str) -> bool | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for net in self._cn_nets:
            if addr.version == net.version and addr in net:
                return True
        for net in self._global_nets:
            if addr.version == net.version and addr in net:
                return False
        return None

    def from_builtin(self, ip: str) -> IpGeo:
        dom = self._by_prefix(ip)
        return IpGeo(ip=ip, country="中国" if dom else None, is_domestic=dom, source="builtin")

    # -- provider 2: 在线批量 ---------------------------------------------
    @staticmethod
    def _fetch_batch(ips: list[str]) -> dict[str, IpGeo]:
        payload = json.dumps([{"query": ip} for ip in ips]).encode()
        req = urllib.request.Request(
            _BATCH_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return {}

        out: dict[str, IpGeo] = {}
        for row in data or []:
            if not isinstance(row, dict) or row.get("status") != "success":
                continue
            ip = row.get("query")
            if not ip:
                continue
            raw_as = row.get("as") or ""
            asn = None
            org = None
            if raw_as:
                parts = raw_as.split(None, 1)
                asn = parts[0].lstrip("AS") if parts else None
                org = parts[1] if len(parts) > 1 else None
            out[ip] = IpGeo(
                ip=ip,
                country=row.get("countryCode"),
                region=row.get("regionName"),
                city=row.get("city"),
                isp=row.get("isp"),
                asn=asn,
                org=org,
                is_domestic=(row.get("countryCode") == "CN"),
                source="online",
            )
        return out

    # -- 对外 -------------------------------------------------------------
    async def resolve(self, ips: list[str]) -> dict[str, IpGeo]:
        """异步入口：在线部分走线程池，失败自动降级到内置表。"""
        result: dict[str, IpGeo] = {}
        todo = [ip for ip in ips if ip not in self._cache]

        for ip in todo:
            hit = self._from_mmdb(ip)
            if hit is not None:
                self._cache[ip] = hit

        if self.enabled and self.online_enabled:
            pending = [ip for ip in todo if ip not in self._cache]
            if pending:
                loop = asyncio.get_running_loop()
                batches = [pending[i : i + _BATCH_SIZE] for i in range(0, len(pending), _BATCH_SIZE)]
                fetched = await loop.run_in_executor(None, self._fetch_all, batches)
                self._cache.update(fetched)

        for ip in ips:
            if ip in self._cache:
                result[ip] = self._cache[ip]
            else:
                result[ip] = self.from_builtin(ip)
        return result

    def _fetch_all(self, batches: list[list[str]]) -> dict[str, IpGeo]:
        """线程池并发拉取多个批次（阻塞，供 run_in_executor 调用）。"""
        out: dict[str, IpGeo] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(batches)))) as pool:
            for batch in pool.map(self._fetch_batch, batches):
                out.update(batch)
        return out

    def close(self) -> None:
        if self._reader is not None:
            with contextlib.suppress(Exception):
                self._reader.close()
            self._reader = None


def normalize_isp(text: str | None) -> str | None:
    """把各种 isp 写法收敛成 电信/联通/移动/教育网。"""
    if not text:
        return None
    low = text.lower()
    for name, keys in builtin._ISP_KEYWORDS.items():
        if any(k in low for k in keys):
            return name
    return text
