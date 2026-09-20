"""端到端冒烟脚本：真实网络跑一遍小样本，验证各阶段是否真的能出数。

用法：python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dnspick.config import load_config
from dnspick.domain_store import DomainStore
from dnspick.runner import Runner


async def main() -> None:
    cfg = load_config()
    # 小样本：只留国内主力 + Cloudflare
    keep = {"alidns-1", "dnspod-1", "114-1", "cf-1", "google-1"}
    cfg.set_enabled(keep)

    # 从 DomainStore 选取少量域名做快速冒烟
    dom_store = DomainStore()
    dom_store.init()
    all_doms = dom_store.get_enabled()
    cn = [d for d in all_doms if d.group.value == "cn"][:4]
    gl = [d for d in all_doms if d.group.value == "global"][:3]
    smoke_domains = cn + gl

    # 临时只启用冒烟域名
    all_names = {d.name for d in all_doms}
    dom_store.set_enabled_batch(all_names, False)
    dom_store.set_enabled_batch({d.name for d in smoke_domains}, True)

    t = cfg.tunables
    t.resolve_repeat = 2
    t.tcp_repeat = 2
    t.icmp_enabled = True
    t.geoip_enabled = True

    runner = Runner(cfg, domain_store=dom_store)
    runner.on_progress = lambda sp: print(f"  [{sp.stage_name}] {sp.done}/{sp.total} {sp.msg}", flush=True)
    result = await asyncio.wait_for(runner.run(), timeout=300)

    # 恢复全部域名启用状态
    dom_store.set_enabled_batch(all_names, True)

    print("\n=== 结果 ===")
    for i, rep in enumerate(result.ranked, 1):
        print(
            f"{i:>2}. {rep.server.name:<16} {rep.server.address:<22} "
            f"总分 {rep.total:>5}  结论 {rep.verdict.value:<9} "
            f"国内 {rep.cn.tcp_p50_ms} ms/hop {rep.cn.hop_avg} "
            f"国外 {rep.glob.tcp_p50_ms} ms  "
            f"解析 {rep.cn.resolve_success_rate:.0%} 可达 {rep.cn.ip_reachable_rate:.0%} "
            f"就近 {rep.cn.proximity:.0f} 劫持 {rep.hijack}"
        )
        for ip, ms, where in rep.top_ips:
            print(f"      最快 IP {ip:<16} {ms} ms  {where}")

    print(f"\n耗时 {result.summary.duration_s}s  出口 IP {result.summary.public_ip}  "
          f"去重 IP {result.summary.unique_ips}")
    reachable = [p for p in result.ips.values() if not p.unreachable]
    print(f"IP 探测：{len(reachable)}/{len(result.ips)} 可达，"
          f"有跳数 {sum(1 for p in result.ips.values() if p.hop_count)} 个，"
          f"有 ICMP {sum(1 for p in result.ips.values() if p.icmp_avg_ms)} 个，"
          f"有归属地 {sum(1 for p in result.ips.values() if p.geo and p.geo.source != 'unknown')} 个")

    Path("smoke-result.json").write_text(
        json.dumps(
            {
                "summary": result.summary.__dict__,
                "servers": [
                    {
                        "name": r.server.name,
                        "address": r.server.address,
                        "total": r.total,
                        "verdict": r.verdict.value,
                        "advice": r.advice,
                        "cn": r.cn.__dict__,
                        "global": r.glob.__dict__,
                    }
                    for r in result.ranked
                ],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print("已写入 smoke-result.json")


if __name__ == "__main__":
    asyncio.run(main())