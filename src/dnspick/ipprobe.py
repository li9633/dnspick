"""阶段二：IP 链路探测。

三类手段，权限与成本各不同：
    * TCP 握手（asyncio，全平台免权限）——主指标，最贴近真实访问体验
    * 跳数（TCP TTL 递增，asyncio，免权限）——不收 ICMP 也能数跳
    * ICMP / traceroute（系统命令，线程池）——辅助与深度模式

跳数用并发 TTL 探测而不是逐跳串行：把 ttl=1..N 的连接一次性并发发出，
最小成功 ttl 即跳数，耗时从 O(N×timeout) 降到约 1 个 timeout。
"""

from __future__ import annotations

import asyncio
import ipaddress
import platform
import re
import socket
import subprocess
import time
from collections.abc import Callable

from dnspick.models import IpProbeResult

_SYSTEM = platform.system().lower()

_TIME_RE = re.compile(r"(?:time|时间)[=<]\s*([0-9.]+)\s*ms", re.IGNORECASE)
_TIME_LT_RE = re.compile(r"(?:time|时间)\s*<\s*([0-9.]+)\s*ms", re.IGNORECASE)
_LOSS_RE = re.compile(r"([0-9.]+)%")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[idx]


# --------------------------------------------------------------------------
# TCP（asyncio）
# --------------------------------------------------------------------------
async def tcp_ping(
    ip: str,
    port: int = 443,
    timeout: float = 1.5,
    ttl: int | None = None,
) -> tuple[float | None, bool]:
    """TCP 握手。返回 (耗时ms, 是否到达)。

    ConnectionRefusedError 意味着包到了目标但端口没开——对测跳数而言算「到达」。
    """
    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if ttl is not None:
            opt = socket.IPPROTO_IPV6 if family == socket.AF_INET6 else socket.IPPROTO_IP
            sock.setsockopt(opt, socket.IP_TTL if family == socket.AF_INET else socket.IPV6_HOPLIMIT, ttl)
        sock.setblocking(False)
        t0 = time.perf_counter()
        await asyncio.wait_for(loop.sock_connect(sock, (ip, port)), timeout=timeout)
        return (time.perf_counter() - t0) * 1000.0, True
    except ConnectionRefusedError:
        # 包到了目标、端口没开：对测跳数而言这就是「到达」
        return None, True
    except (TimeoutError, asyncio.TimeoutError, OSError):
        return None, False
    finally:
        sock.close()


async def tcp_measure(
    ip: str,
    ports: tuple[int, ...] = (443, 80, 8443),
    repeat: int = 3,
    timeout: float = 1.5,
) -> IpProbeResult:
    """多端口 × 多次采样，取最可用端口的 p50。"""
    result = IpProbeResult(ip=ip)
    samples: dict[int, list[float]] = {p: [] for p in ports}
    attempts: dict[int, int] = {p: 0 for p in ports}

    for port in ports:
        for _ in range(repeat):
            attempts[port] += 1
            rtt, ok = await tcp_ping(ip, port, timeout)
            if ok and rtt is not None:
                samples[port].append(rtt)
            if len(samples[port]) >= 2:
                break  # 够用即可，别给目标添麻烦

    for port, vals in samples.items():
        result.tcp_ports[port] = round(_percentile(vals, 0.5), 2) if vals else None

    usable = [p for p in ports if result.tcp_ports.get(p) is not None]
    if usable:
        best = min(usable, key=lambda p: result.tcp_ports[p])  # type: ignore[arg-type]
        result.best_port = best
        result.tcp_p50_ms = result.tcp_ports[best]
        result.tcp_min_ms = round(min(samples[best]), 2)
        vals = samples[best]
        if len(vals) >= 2:
            result.tcp_jitter_ms = round(max(vals) - min(vals), 2)
        total = sum(attempts[p] for p in usable)
        hits = sum(len(samples[p]) for p in usable)
        result.tcp_success_rate = round(hits / total, 3) if total else 0.0
    else:
        result.unreachable = True
    return result


async def tcp_hop_count(
    ip: str,
    port: int = 443,
    max_hops: int = 30,
    timeout: float = 1.0,
    concurrency: int = 16,
) -> int | None:
    """并发 TTL 探测跳数：不收 ICMP、免 raw socket、免管理员。"""
    sem = asyncio.Semaphore(concurrency)
    found: dict[int, int] = {}

    async def try_ttl(ttl: int) -> None:
        async with sem:
            _rtt, ok = await tcp_ping(ip, port, timeout, ttl=ttl)
            if ok:
                found[ttl] = 1

    await asyncio.gather(*(try_ttl(t) for t in range(1, max_hops + 1)))
    if found:
        return min(found)
    return None


# --------------------------------------------------------------------------
# ICMP / traceroute（阻塞系统调用 → 线程池）
# --------------------------------------------------------------------------
def _icmp_cmd(ip: str, count: int) -> list[str]:
    if _SYSTEM == "windows":
        return ["ping", "-n", str(count), "-w", "1000", ip]
    return ["ping", "-c", str(count), "-W", "1", "-i", "0.3", ip]


def icmp_available() -> bool:
    """系统是否真的有可用的 ping（部分容器/OpenWrt 精简镜像没有）。"""
    import shutil

    return shutil.which("ping") is not None


def icmp_ping_blocking(ip: str, count: int = 4, timeout: float = 6.0) -> tuple[float | None, float | None]:
    """返回 (平均延迟ms, 丢包率 0~1)。失败返回 (None, None)。"""
    try:
        proc = subprocess.run(
            _icmp_cmd(ip, count),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="ignore",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None

    out = proc.stdout
    times = [float(m) for m in _TIME_RE.findall(out)]
    times += [float(m) / 2 for m in _TIME_LT_RE.findall(out)]

    loss = None
    m = _LOSS_RE.search(out)
    if m:
        try:
            loss = float(m.group(1)) / 100.0
        except ValueError:
            loss = None
    if loss is None:
        loss = 1.0 if not times else 0.0

    avg = round(sum(times) / len(times), 2) if times else None
    return avg, loss


def _trace_cmd(ip: str, max_hops: int) -> list[str]:
    if _SYSTEM == "windows":
        return ["tracert", "-d", "-h", str(max_hops), "-w", "1000", ip]
    return ["traceroute", "-n", "-w", "1", "-m", str(max_hops), ip]


def traceroute_blocking(ip: str, max_hops: int = 30, timeout: float = 25.0) -> list[str]:
    """返回路径上的 IP 列表（去重保序）。失败返回空列表。"""
    try:
        proc = subprocess.run(
            _trace_cmd(ip, max_hops),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="ignore",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    hops: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("traceroute", "tracert", "tracing")):
            continue
        ips = _IP_RE.findall(line)
        if not ips:
            continue
        hop_ip = ips[0]
        if hop_ip not in hops:
            hops.append(hop_ip)
    return hops[:max_hops]


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
class IpProber:
    """对去重后的 IP 池做批量探测。"""

    def __init__(
        self,
        ports: tuple[int, ...] = (443, 80, 8443),
        tcp_repeat: int = 3,
        tcp_timeout: float = 1.5,
        tcp_concurrency: int = 64,
        hop_max: int = 30,
        hop_timeout: float = 1.0,
        icmp_enabled: bool = True,
        icmp_count: int = 4,
        icmp_timeout: float = 4.0,
        icmp_concurrency: int = 32,
        deep_traceroute: bool = False,
        on_progress: Callable[[str, int, int, str], None] | None = None,
    ) -> None:
        self.ports = ports
        self.tcp_repeat = tcp_repeat
        self.tcp_timeout = tcp_timeout
        self.tcp_concurrency = tcp_concurrency
        self.hop_max = hop_max
        self.hop_timeout = hop_timeout
        self.icmp_enabled = icmp_enabled
        self.icmp_count = icmp_count
        self.icmp_timeout = icmp_timeout
        self.icmp_concurrency = icmp_concurrency
        self.deep_traceroute = deep_traceroute
        self.on_progress = on_progress
        self._max_workers = max(8, min(icmp_concurrency * 2, 64))

    async def run(self, ips: list[str]) -> dict[str, IpProbeResult]:
        results: dict[str, IpProbeResult] = {}
        sem = asyncio.Semaphore(self.tcp_concurrency)
        tcp_total = len(ips)
        tcp_done = 0

        async def probe(ip: str) -> None:
            nonlocal tcp_done
            async with sem:
                res = await tcp_measure(ip, self.ports, self.tcp_repeat, self.tcp_timeout)
            if not res.unreachable:
                # 用真正握手成功的端口探跳数：443 被墙而 80 通的情况很常见
                res.hop_count = await tcp_hop_count(
                    ip, res.best_port or self.ports[0], self.hop_max, self.hop_timeout
                )
            results[ip] = res
            tcp_done += 1
            if self.on_progress:
                self.on_progress("TCP 探测", tcp_done, tcp_total, f"探测 {ip}")

        await asyncio.gather(*(probe(ip) for ip in ips))
        await self._run_blocking(ips, results)
        return results

    async def _run_blocking(
        self, ips: list[str], results: dict[str, IpProbeResult]
    ) -> None:
        """ICMP 与 traceroute 走线程池，避免阻塞事件循环。

        逐个 IP 提交 + 逐个报告进度，不再把所有工作一次性提交后等全部完成才报进度。
        """
        if not self.icmp_enabled and not self.deep_traceroute:
            return

        loop = asyncio.get_running_loop()
        do_icmp = self.icmp_enabled and icmp_available()
        sem = asyncio.Semaphore(self._max_workers)
        icmp_total = len(ips)
        icmp_done = 0

        async def process_one(ip: str) -> None:
            nonlocal icmp_done
            async with sem:

                async def _icmp() -> tuple:
                    return await loop.run_in_executor(
                        None, icmp_ping_blocking, ip, self.icmp_count, self.icmp_timeout,
                    )

                async def _trace() -> list[str]:
                    return await loop.run_in_executor(
                        None, traceroute_blocking, ip, self.hop_max,
                    )

                # ICMP 和 traceroute 可以并行提交到线程池
                icmp_task = asyncio.create_task(_icmp()) if do_icmp else None
                trace_task = asyncio.create_task(_trace()) if self.deep_traceroute else None

                res = results.setdefault(ip, IpProbeResult(ip=ip))
                if icmp_task:
                    avg, loss = await icmp_task
                    res.icmp_avg_ms = avg
                    res.icmp_loss = loss
                if trace_task:
                    res.route = list(await trace_task)

            icmp_done += 1
            if self.on_progress:
                self.on_progress("ICMP 探测", icmp_done, icmp_total, f"链路探测 {ip}")

        await asyncio.gather(*(process_one(ip) for ip in ips))