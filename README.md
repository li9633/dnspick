# dnspick

[简体中文](README.zh-CN.md)

DNS upstream benchmarking tool for home network environments. Measures and scores DNS resolvers
by evaluating the quality of IP addresses they return — not just their response time.
Produces ready-to-use SmartDNS configuration; does not modify any system settings.

> [!IMPORTANT]
> This tool evaluates **the quality of IP addresses a DNS resolver returns**, not how fast it
> answers queries. DNS response time (RTT) is largely irrelevant in home networks with local
> caching — what matters is whether the resolver routes you to the nearest CDN node or to a
> distant region.

---

## Motivation

Traditional DNS benchmarks measure response latency (RTT). In a home network with local
caching, RTT differences between resolvers become negligible after the first query.

The primary factor for real-world browsing experience is CDN node geography: resolving domestic
sites like `taobao.com` or `jd.com` against a DNS resolver that returns cross-province or overseas
CDN IPs produces latency that dwarfs any RTT difference between resolvers.

dnspick takes a two-phase approach:

```
DNS Resolution Matrix  →  each server × each domain × N repetitions, collecting IP sets
IP Path Probing        →  TCP handshake, hop count, ICMP, GeoIP on all unique IPs
Per-Group Scoring      →  domestic/global scored independently, producing four verdict tiers
```

The core principle: measure the quality of the IPs a DNS returns, not how fast it answers.

---

## Measurement Pipeline

| Stage | Description |
|---|---|
| Environment Detection | Public egress IP / ECS subnet auto-detection |
| Domain Collection | Load enabled domains from SQLite (built-in + browser history + manual + API discovery) |
| API Discovery (*optional*) | HTML scraping + crt.sh certificate transparency log for related domains |
| DNS Resolution | Server × Domain × repetition matrix over UDP / TCP / DoT / DoH |
| ECS Comparison (*optional*) | Compare resolution results with and without ECS subnet |
| TCP Probing | Multi-port handshake (443/80/8443) × multiple samples, p50 latency |
| ICMP Probing | System ping via thread pool + optional traceroute via TCP TTL increment |
| GeoIP | Offline CN prefix table; falls back to unknown |
| Scoring | Weighted multi-dimensional scoring, per-group verdict |

---

## Scoring Criteria

| Dimension | Method | Weight (balanced profile) |
|---|---|---|
| **Proximity** | Relative TCP latency rank of resolved IPs within the same group (domestic) | 30 |
| **Speed** | Group-median of per-domain fastest-IP TCP handshake latency | 30 |
| **Stability** | Weighted composite: resolution success rate (3/4) + IP reachability (1/4) + TCP success rate | 25 |
| **Path** | Hop count via TCP TTL increment (no raw socket required) | 15 |

### Additional Checks

- **Hijacking**: Query random non-existent subdomains; any response returned indicates hijacking.
- **Pollution**: Global-domain resolving to domestic IPs indicates DNS pollution.
- **ECS Support**: Compare resolution results with/without ECS subnet.
- **Environment Diagnosis**: Detect transparent proxies or local interception that flatten latency
  differences, rendering measurements non-differentiating.

> [!WARNING]
> In networks with transparent proxies or local interception (common in corporate VPNs and some
> ISP setups), TCP handshakes may show near-zero latency with 1-hop paths for all IPs. This makes
> latency and hop-count scores meaningless. Run measurements on a direct-connection home network
> for meaningful results.

---

## Scoring Engine

### Per-Group Independent Scoring

Domestic and global domains are scored separately (default weights: domestic 0.7, global 0.3).
DNS resolvers often perform asymmetrically across groups; merged scoring obscures this.

### Two-Stage Pipeline

1. **Per-group sub-scores**: speed, path, stability, and proximity scores computed independently
   for domestic and global groups (all normalized to 0–100).
2. **Composite verdict**:
   ```
   total = cn_weight × domestic_score + global_weight × global_score
           - hijack_penalty(60) - pollution_penalty(25)
   ```
   Four verdict tiers: **recommend / standby / group-limited / avoid**

### Preset Profiles

Switchable via `--profile` flag or GUI dropdown:

| Profile | Speed | Path | Stability | Proximity | Use Case |
|---|---|---|---|---|---|
| `balanced` | 30 | 15 | 25 | 30 | General |
| `speed` | 55 | 10 | 15 | 20 | Gaming / real-time |
| `stable` | 20 | 10 | 50 | 20 | Downloads / streaming |
| `cdn` | 20 | 10 | 15 | 55 | CDN-heavy sites |
| `privacy` | 25 | 10 | 20 | 20 | Privacy-focused |

Custom weights are configurable via slider controls in GUI settings.

---

## Installation

Requires **Python 3.13.7+**. Package management via [uv](https://docs.astral.sh/uv/):

> [!NOTE]
> On OpenWrt or other headless environments, use `--no-group gui --no-group dev` to skip PySide6.
> This avoids pulling ~600 MB of GUI dependencies.

```bash
# Desktop (GUI + dev tools)
uv sync

# Headless (CLI only, no PySide6)
uv sync --no-group gui --no-group dev

# Optional GeoIP support (requires MaxMind mmdb)
uv sync
pip install maxminddb
```

### Platform Support

| Platform | CLI | GUI |
|---|---|---|
| Windows | ✓ | ✓ |
| Linux | ✓ | ✓ |
| OpenWrt | ✓ | — |
| macOS | ✓ | ✓ |

---

## Usage

### GUI

```bash
uv run dnspick-gui
```

Features:
- Protocol distribution dashboard with one-click run/stop
- Settings panel: server toggle, measurement parameters, custom scoring weights
- Real-time stage progress panel
- Streaming log output with configurable log level
- History analysis with tabbed views (overview / group comparison / recommendations)
- SmartDNS config export (recommend-tier servers only, respects active filters and sort order)

### CLI

```bash
# Basic run
uv run dnspick run

# Scoring profile
uv run dnspick run --profile speed

# Limit output
uv run dnspick run --top 5

# Protocol filter
uv run dnspick run --protocol dot,doh

# Export results
uv run dnspick run --json result.json --csv result.csv

# Deep traceroute mode
uv run dnspick run --deep

# Custom server list
uv run dnspick run --servers my-servers.toml

# Enable API domain discovery
uv run dnspick run --api-discovery

# Full option list
uv run dnspick run --help
```

### Domain Discovery

Collect domains from browser history and built-in seeds:

```bash
uv run dnspick discover

# Crawl external links from a seed URL
uv run dnspick discover --crawl https://example.com

# Fetch online top-site rankings
uv run dnspick discover --online

# Browser history only, skip built-in seeds
uv run dnspick discover --no-seed

# Limit output count
uv run dnspick discover --max 500
```

---

## Built-in Resources

- **30+ DNS upstreams**: Alibaba, Tencent, Baidu, 114DNS, Cloudflare, Google, Quad9, AdGuard,
  and others. Each vendor includes UDP endpoints with optional DoT/DoH. Only primary UDP nodes
  are enabled by default.
- **370+ seed domains**: Major domestic/international sites, CDN endpoints. Stored in SQLite with
  category-based filtering.
- **Offline GeoIP**: Built-in CN prefix table for domestic/global classification without network access.

All built-in resources can be overridden or extended via external TOML files.

---

## Project Structure

```
dnspick/
├── src/dnspick/
│   ├── gui/               # PySide6 GUI layer
│   │   ├── app.py         # Entry point
│   │   ├── main_window.py # Run configuration dashboard
│   │   ├── settings.py    # Settings dialog (servers / weights / parameters)
│   │   ├── history.py     # History analysis window (tabbed)
│   │   ├── progress_panel.py  # Stage progress panel
│   │   ├── worker.py      # Measurement worker (QThread)
│   │   └── runner_config.py   # Config signal stubs (UI binding)
│   ├── cli.py             # CLI entry (Typer + Rich)
│   ├── runner.py          # Measurement orchestrator (stage scheduler)
│   ├── resolver.py        # DNS resolution matrix (bounded worker pool, multi-protocol)
│   ├── ipprobe.py         # IP path probing (TCP / ICMP / hop count / traceroute)
│   ├── scorer.py          # Scoring engine (per-group scoring + verdict)
│   ├── discover.py        # Domain discovery (browser history / seeds / online)
│   ├── api_discovery.py   # API domain auto-discovery (HTML + crt.sh)
│   ├── geoip.py           # GeoIP resolution
│   ├── builtin.py         # Built-in DNS servers + domains
│   ├── config.py          # Configuration loading / persistence
│   ├── models.py          # Data models (pure containers)
│   ├── store.py           # SQLite result storage
│   ├── domain_store.py    # Domain database
│   ├── relation_store.py  # Domain relationship storage
│   └── log.py             # Global logging (RotatingFile + Qt Signal)
├── resources/
│   ├── servers.toml       # Built-in DNS server registry
│   └── seed_domains.txt   # Seed domain list (imported into SQLite on first run)
├── tests/                 # Unit tests (pytest)
├── doc/                   # Design documents
├── scripts/               # Utility scripts
└── pyproject.toml         # Project metadata and dependencies
```

---

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check
uv run ruff check --fix
```

---

## License

MIT