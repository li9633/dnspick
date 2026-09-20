"""API 域名发现 —— www.bilibili.com 实测脚本。

对 www.bilibili.com 执行真实网络发现，打印结果。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dnspick.api_discovery import ApiDomainDiscovery


async def discover_bilibili() -> None:
    discovery = ApiDomainDiscovery(
        cache_days=0,
        html_timeout=10.0,
        crt_timeout=15.0,
        max_per_source=60,
    )

    print("=" * 70)
    print("API 域名自动发现 —— www.bilibili.com 实测")
    print("=" * 70)

    result = await discovery.discover("www.bilibili.com")

    # -- 策略1 --
    print(f"\n策略1 (HTML 抓取): {'成功' if result.html_success else '失败'}")
    if result.html_error:
        print(f"  错误: {result.html_error}")
    print(f"  发现 {len(result.html_domains)} 个域名:")
    for d in sorted(result.html_domains):
        print(f"    - {d}")

    # -- 策略2 --
    print(f"\n策略2 (crt.sh): {'成功' if result.crt_success else '失败'}")
    if result.crt_error:
        print(f"  错误: {result.crt_error}")
    print(f"  发现 {len(result.crt_domains)} 个域名:")
    for d in sorted(result.crt_domains):
        print(f"    - {d}")

    # -- 合并 --
    all_d = result.all_domains
    html_only = result.html_domains - result.crt_domains
    crt_only = result.crt_domains - result.html_domains
    both = result.html_domains & result.crt_domains

    print("\n汇总:")
    print(f"  总计唯一域名: {len(all_d)}")
    print(f"  仅 HTML 发现: {len(html_only)}")
    print(f"  仅 crt.sh  发现: {len(crt_only)}")
    print(f"  两者都发现:   {len(both)}")

    if result.from_cache:
        print("  (来自缓存)")

    # 分类
    groups: dict[str, list[str]] = {
        "bilibili.com 子域": [],
        "hdslb.com": [],
        "bilivideo.com": [],
        "其他 bilibili 相关": [],
        "第三方 CDN": [],
        "其他": [],
    }

    for d in sorted(all_d):
        if d.endswith(".bilibili.com") or d == "bilibili.com":
            groups["bilibili.com 子域"].append(d)
        elif "hdslb.com" in d:
            groups["hdslb.com"].append(d)
        elif "bilivideo.com" in d:
            groups["bilivideo.com"].append(d)
        elif "bilibili" in d:
            groups["其他 bilibili 相关"].append(d)
        elif any(
            kw in d
            for kw in (
                "cdn.", "static.", "img.", "jsdelivr", "cloudflare",
                "cloudfront", "akamai", "gstatic", "ggpht",
            )
        ):
            groups["第三方 CDN"].append(d)
        else:
            groups["其他"].append(d)

    print("\n按类型分组:")
    for label, items in groups.items():
        if items:
            print(f"\n  [{label}] ({len(items)} 个):")
            for d in items:
                print(f"    - {d}")

    print("\n说明:")
    print("  - bilibili.com 子域: 包含 API、数据、用户等内部服务域名")
    print("  - hdslb.com: B站静态资源 CDN（图片、CSS、JS）")
    print("  - bilivideo.com: B站视频流 CDN")
    print("  - 第三方 CDN: 公共 CDN / 第三方服务")


if __name__ == "__main__":
    asyncio.run(discover_bilibili())