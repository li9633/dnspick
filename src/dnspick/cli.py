"""命令行入口。无 GUI 环境（OpenWrt / Linux / headless）就用它。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from dnspick import __version__
from dnspick.config import Tunables, load_config
from dnspick.discover import build_report, write_toml
from dnspick.log import log
from dnspick.models import ServerReport, StageProgress, Verdict
from dnspick.runner import Runner

app = typer.Typer(add_completion=False, help="DNS -> IP -> 链路 两段式测量器")
console = Console()
err_console = Console(stderr=True)

_VERDICT_STYLE = {
    Verdict.RECOMMEND: "bold green",
    Verdict.FALLBACK: "yellow",
    Verdict.NARROW: "cyan",
    Verdict.AVOID: "red",
}
_VERDICT_TEXT = {
    Verdict.RECOMMEND: "推荐",
    Verdict.FALLBACK: "备用",
    Verdict.NARROW: "限定分组",
    Verdict.AVOID: "避免",
}


def _fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}"


def _build_table(reports: list[ServerReport]) -> Table:
    table = Table(title="DNS 评估结果（按综合分降序）", header_style="bold magenta")
    table.add_column("#", width=3)
    table.add_column("名称", overflow="fold")
    table.add_column("地址", overflow="fold")
    table.add_column("综合分", justify="right")
    table.add_column("结论")
    table.add_column("国内中位延迟", justify="right")
    table.add_column("国外中位延迟", justify="right")
    table.add_column("跳数", justify="right")
    table.add_column("解析成功率", justify="right")
    table.add_column("IP可达率", justify="right")
    table.add_column("就近", justify="right")
    table.add_column("劫持")
    table.add_column("ECS")

    for i, rep in enumerate(reports, 1):
        table.add_row(
            str(i),
            rep.server.name,
            rep.server.address,
            f"{rep.total:.1f}",
            f"[{_VERDICT_STYLE[rep.verdict]}]{_VERDICT_TEXT[rep.verdict]}[/]",
            _fmt_ms(rep.cn.tcp_p50_ms),
            _fmt_ms(rep.glob.tcp_p50_ms),
            _fmt_ms(rep.cn.hop_avg),
            f"{rep.cn.resolve_success_rate:.0%}",
            f"{rep.cn.ip_reachable_rate:.0%}",
            f"{rep.cn.proximity:.0f}",
            "[red]是[/]" if rep.hijack else "否",
            "-"
            if rep.ecs_supported is None
            else ("支持" if rep.ecs_supported else "无差异"),
        )
    return table


def _print_advice(reports: list[ServerReport]) -> None:
    top = [r for r in reports if r.verdict is Verdict.RECOMMEND][:3]
    console.print()
    if not top:
        console.print("[yellow]没有出现「推荐」级别的 DNS，可放宽超时或换一组域名再试。[/]")
        return
    console.rule("可直接填进 SmartDNS 的建议")
    for rep in top:
        console.print(
            f"[bold cyan]{rep.server.name}[/] [dim]{rep.server.address}[/] "
            f"({rep.server.protocol.value}) — {rep.advice}"
        )
        for ip, ms, where in rep.top_ips:
            console.print(f"    [dim]最快 IP[/] {ip}  {_fmt_ms(ms)}ms  {where}")
    overseas = [r for r in reports if r.verdict is Verdict.NARROW and r.glob.score > r.cn.score]
    if overseas:
        best = overseas[0]
        console.print(
            f"[bold cyan]{best.server.name}[/] [dim]{best.server.address}[/] "
            f"— 建议只放 overseas 分组（国外 {_fmt_ms(best.glob.tcp_p50_ms)}ms）"
        )


@app.command()
def run(
    profile: str = typer.Option("balanced", "--profile", "-p", help="评分档位：balanced/speed/stable/cdn/privacy"),
    top: int = typer.Option(0, "--top", "-n", help="只显示前 N 名，0 表示全部"),
    protocol: str = typer.Option("", "--protocol", help="只测指定协议，逗号分隔：udp,tcp,dot,doh"),
    servers_file: Path | None = typer.Option(None, "--servers", help="自定义 DNS 清单 TOML"),
    cn_list: Path | None = typer.Option(None, "--cn-list", help="完整中国 IP 段列表（每行一条 CIDR）"),
    repeat: int = typer.Option(0, "--repeat", "-r", help="每个域名查询次数"),
    timeout: float = typer.Option(0, "--timeout", help="单次 DNS 超时（秒）"),
    deep: bool = typer.Option(False, "--deep", help="深度模式：对每个 IP 跑完整 traceroute（慢）"),
    no_icmp: bool = typer.Option(False, "--no-icmp", help="关闭 ICMP ping"),
    no_geoip: bool = typer.Option(False, "--no-geoip", help="关闭归属地查询"),
    ecs: bool = typer.Option(False, "--ecs", help="启用 ECS 对比查询（需探测公网 IP）"),
    api_discovery: bool = typer.Option(
        False, "--api-discovery",
        help="启用 API 域名自动发现（从主站首页 + crt.sh 证书日志）",
    ),
    json_out: Path | None = typer.Option(None, "--json", help="结果输出为 JSON"),
    csv_out: Path | None = typer.Option(None, "--csv", help="结果输出为 CSV"),
    geoip_db: str | None = typer.Option(None, "--geoip-db", help="MaxMind mmdb 路径（可选）"),
) -> None:
    """跑一次完整测量。"""
    cfg = load_config(servers_file, cn_list)
    cfg.use_ecs = ecs
    cfg.geoip_db = geoip_db
    t: Tunables = cfg.tunables.with_profile(profile)
    if repeat:
        t.resolve_repeat = repeat
    if timeout:
        t.resolve_timeout = timeout
    if deep:
        t.deep_traceroute = True
    if no_icmp:
        t.icmp_enabled = False
    if no_geoip:
        t.geoip_enabled = False
    if api_discovery:
        t.api_discovery_enabled = True
    if protocol:
        wanted = {p.strip().lower() for p in protocol.split(",")}
        keep = {s.id for s in cfg.servers if s.enabled and s.protocol.value in wanted}
        cfg.set_enabled(keep)

    if not cfg.active_servers():
        err_console.print("[red]没有启用的 DNS 服务器，请检查 --servers 或协议过滤条件[/]")
        raise typer.Exit(1)

    runner = Runner(cfg)

    log.set_console(console)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        stage_tasks: dict[str, object] = {}

        def on_progress(sp: StageProgress) -> None:
            task = stage_tasks.get(sp.stage_name)
            if task is None:
                task = progress.add_task(sp.stage_name, total=max(sp.total, 1))
                stage_tasks[sp.stage_name] = task
            if sp.status == "done":
                progress.update(task, completed=sp.total, description=f"[green]✓[/] {sp.stage_name}: {sp.msg}")
            elif sp.status == "skipped":
                progress.update(task, visible=False)
            else:
                progress.update(task, completed=sp.done, total=max(sp.total, 1),
                                 description=f"{sp.stage_name}: {sp.msg}")

        runner.on_progress = on_progress
        import asyncio

        try:
            result = asyncio.run(runner.run())
        except KeyboardInterrupt:
            err_console.print("[yellow]已取消[/]")
            raise typer.Exit(130) from None
        except asyncio.CancelledError:
            err_console.print("[yellow]已取消[/]")
            raise typer.Exit(130) from None

    shown = result.ranked[:top] if top > 0 else result.ranked
    console.print(_build_table(shown))
    console.print(
        f"\n[dim]耗时 {result.summary.duration_s}s · 出口 IP {result.summary.public_ip or '未知'} · "
        f"域名 {result.summary.domains} 个 · 去重 IP {result.summary.unique_ips} 个"
    )
    if result.summary.api_discovered:
        console.print(
            f"[dim]API发现 +{result.summary.api_discovered} 个关联域名[/]"
        )
    if result.summary.warning:
        console.print(f"\n[bold red]环境提示[/] {result.summary.warning}")
    _print_advice(shown)

    if json_out:
        json_out.write_text(
            json.dumps(_to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[dim]JSON 已写入 {json_out}[/]")
    if csv_out:
        csv_out.write_text(_to_csv(result.ranked), encoding="utf-8")
        console.print(f"[dim]CSV 已写入 {csv_out}[/]")


@app.command()
def discover(
    out: Path = typer.Option(Path("resources/domains.toml"), "--out", "-o", help="输出文件"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不读浏览器历史"),
    no_seed: bool = typer.Option(False, "--no-seed", help="不用内置种子库"),
    online: bool = typer.Option(False, "--online", help="尝试拉取在线 Top 榜单（需外网）"),
    crawl: list[str] = typer.Option([], "--crawl", help="入口 URL，抓外链域名，可重复指定"),
    top_n: int = typer.Option(60, "--top-n", help="浏览器历史里访问最多的 N 个"),
    top_ext: int = typer.Option(300, "--top-ext", help="再往下 N 个为扩展层"),
    min_hits: int = typer.Option(2, "--min-hits", help="浏览器历史最少访问次数"),
    max_items: int = typer.Option(0, "--max", help="最多写多少条，0 表示不限"),
) -> None:
    """自动发现域名并生成分层 domains.toml。

    默认读浏览器历史（你自己真访问过的域名最有用）+ 内置千级种子库。
    """
    import urllib.parse

    crawl_seeds: list[str] = []
    for raw in crawl:
        url = raw if "://" in raw else f"https://{raw}"
        if urllib.parse.urlparse(url).scheme in ("http", "https"):
            crawl_seeds.append(url)

    with console.status("正在发现域名…"):
        rep = build_report(
            use_browser=not no_browser,
            use_seed=not no_seed,
            online=online,
            crawl_seeds=crawl_seeds or None,
            tier1_size=top_n,
            tier2_size=top_ext,
            min_hits=min_hits,
        )
    n = write_toml(rep, out, max_items=max_items)

    console.print(f"[green]已写出 {n} 条域名 → {out}[/]")
    for src in rep.sources:
        console.print(f"  [dim]· {src}[/]")
    if not rep.sources:
        console.print("[yellow]没有发现任何域名，检查参数或种子文件是否缺失[/]")

    console.print(f"\n共发现 {len(rep.items)} 个域名，已写入 {n} 条")
    console.print("[dim]在域名管理器中管理启用/禁用的域名[/]")


@app.command()
def list_servers(
    servers_file: Path | None = typer.Option(None, "--servers"),
    all_: bool = typer.Option(False, "--all", help="包含默认关闭的条目"),
) -> None:
    """列出内置 / 自定义的 DNS 清单。"""
    cfg = load_config(servers_file)
    table = Table(title="DNS 清单")
    table.add_column("启用")
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("地址")
    table.add_column("协议")
    table.add_column("标签")
    for s in cfg.servers:
        if not all_ and not s.enabled:
            continue
        table.add_row(
            "[green]✓[/]" if s.enabled else "[dim]✗[/]",
            s.id,
            s.name,
            s.address,
            s.protocol.value,
            ",".join(s.tags),
        )
    console.print(table)


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"dnspick {__version__} (python {sys.version.split()[0]})")


def _to_dict(result) -> dict:
    return {
        "summary": {
            "duration_s": result.summary.duration_s,
            "public_ip": result.summary.public_ip,
            "ecs_subnet": result.summary.ecs_subnet,
            "servers": result.summary.servers,
            "domains": result.summary.domains,
            "unique_ips": result.summary.unique_ips,
            "api_discovered": result.summary.api_discovered,
            "warning": result.summary.warning,
        },
        "servers": [
            {
                "id": r.server.id,
                "name": r.server.name,
                "address": r.server.address,
                "protocol": r.server.protocol.value,
                "total": r.total,
                "verdict": r.verdict.value,
                "advice": r.advice,
                "hijack": r.hijack,
                "ecs_supported": r.ecs_supported,
                "cn": {
                    "tcp_p50_ms": r.cn.tcp_p50_ms,
                    "hop_avg": r.cn.hop_avg,
                    "resolve_success_rate": r.cn.resolve_success_rate,
                    "ip_reachable_rate": r.cn.ip_reachable_rate,
                    "proximity": r.cn.proximity,
                    "score": r.cn.score,
                    "polluted": r.cn.polluted,
                },
                "global": {
                    "tcp_p50_ms": r.glob.tcp_p50_ms,
                    "hop_avg": r.glob.hop_avg,
                    "resolve_success_rate": r.glob.resolve_success_rate,
                    "ip_reachable_rate": r.glob.ip_reachable_rate,
                    "proximity": r.glob.proximity,
                    "score": r.glob.score,
                    "polluted": r.glob.polluted,
                },
            }
            for r in result.ranked
        ],
    }


def _to_csv(reports: list[ServerReport]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "rank", "name", "address", "protocol", "total", "verdict",
            "cn_p50_ms", "cn_hops", "cn_resolve_rate", "cn_proximity", "cn_score",
            "gl_p50_ms", "gl_hops", "gl_resolve_rate", "gl_proximity", "gl_score",
            "hijack", "advice",
        ]
    )
    for i, r in enumerate(reports, 1):
        w.writerow(
            [
                i, r.server.name, r.server.address, r.server.protocol.value, r.total,
                r.verdict.value, r.cn.tcp_p50_ms, r.cn.hop_avg, r.cn.resolve_success_rate,
                r.cn.proximity, r.cn.score, r.glob.tcp_p50_ms, r.glob.hop_avg,
                r.glob.resolve_success_rate, r.glob.proximity, r.glob.score,
                r.hijack, r.advice,
            ]
        )
    return buf.getvalue()


if __name__ == "__main__":
    app()