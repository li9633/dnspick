"""检查并移除 seed_domains.txt 中 DNS 解析失败的失效域名。"""

from __future__ import annotations

import asyncio
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "resources" / "seed_domains.txt"


def load_domains(path: Path) -> list[str]:
    domains: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            domains.append(stripped)
    return domains


def resolve(domain: str, timeout: float = 2.0) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(domain, None)
        return True
    except OSError:
        return False


async def find_dead(domains: list[str], concurrency: int = 80) -> set[str]:
    dead: set[str] = set()
    done = 0
    total = len(domains)
    lock = asyncio.Lock()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        loop = asyncio.get_running_loop()

        async def check_one(name: str) -> None:
            nonlocal done
            ok = await loop.run_in_executor(pool, resolve, name, 2.0)
            async with lock:
                nonlocal done
                done += 1
                if not ok:
                    dead.add(name)
                    print(f"[{done:4d}/{total}] DEAD  {name}")

        await asyncio.gather(*(check_one(d) for d in domains))

    return dead


def remove_dead(path: Path, dead: set[str]) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    removed = 0
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped in dead:
            removed += 1
            continue
        new_lines.append(line)
    path.write_text("".join(new_lines), encoding="utf-8")
    return removed


def main() -> None:
    domains = load_domains(SEED)
    print(f"共 {len(domains)} 个域名，开始检查...\n")

    dead = asyncio.run(find_dead(domains))
    if not dead:
        print("\n所有域名均可解析，无需移除。")
        return

    print(f"\n发现 {len(dead)} 个失效域名，开始移除...\n")
    removed = remove_dead(SEED, dead)
    remaining = len(load_domains(SEED))
    print(f"\n移除 {removed} 个，剩余 {remaining} 个。")


if __name__ == "__main__":
    main()