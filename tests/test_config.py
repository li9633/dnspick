"""配置与内置资源单测。"""

from __future__ import annotations

from pathlib import Path

from dnspick import builtin
from dnspick.config import Config, Tunables, load_config
from dnspick.models import Group, Protocol


def test_builtin_servers_are_usable() -> None:
    servers = builtin.builtin_servers()
    assert len(servers) > 15
    ids = [s.id for s in servers]
    assert len(ids) == len(set(ids)), "服务器 id 必须唯一"
    for s in servers:
        assert s.address
        if s.protocol is Protocol.DOT:
            assert s.hostname, "DoT 必须有 SNI 主机名"


def test_default_enabled_subset_is_reasonable() -> None:
    cfg = Config()
    active = cfg.active_servers()
    assert 5 <= len(active) <= 15, len(active)
    assert any(s.address == "223.5.5.5" for s in active)
    assert any(s.address == "119.29.29.29" for s in active)


def test_domains_cover_both_groups() -> None:
    doms = builtin.builtin_domains()
    assert any(d.group is Group.CN for d in doms)
    assert any(d.group is Group.GLOBAL for d in doms)


def test_probe_domains_are_random_and_reserved() -> None:
    a = builtin.probe_domains()
    b = builtin.probe_domains()
    assert a[0].name != b[0].name
    assert a[0].name.endswith(".invalid"), "必须用保留 TLD，正常 DNS 只会回 NXDOMAIN"


def test_tunables_normalize_to_100() -> None:
    t = Tunables(w_speed=10, w_path=10, w_stability=10, w_proximity=10)
    t.normalize()
    total = t.w_speed + t.w_path + t.w_stability + t.w_proximity
    assert abs(total - 100.0) < 1e-6


def test_profiles_change_weights() -> None:
    t = Tunables().with_profile("cdn")
    assert t.w_proximity > t.w_speed
    t2 = Tunables().with_profile("speed")
    assert t2.w_speed > t2.w_proximity


def test_frozen_server_can_be_toggled() -> None:
    cfg = Config()
    target = cfg.servers[0].id
    cfg.set_enabled({target})
    assert cfg.active_servers() == [s for s in cfg.servers if s.id == target]
    assert cfg.servers[0].enabled is True


def test_load_config_reads_toml(tmp_path: Path) -> None:
    f = tmp_path / "s.toml"
    f.write_text(
        '[[server]]\nid="x"\nname="自定义"\naddress="9.9.9.9"\nprotocol="udp"\n',
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert any(s.id == "x" for s in cfg.servers)
    assert any(s.address == "9.9.9.9" for s in cfg.servers)


def test_load_config_ignores_malformed_entries(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    f.write_text('[[server]]\nname="缺字段"\n', encoding="utf-8")
    cfg = load_config(f)
    assert len(cfg.servers) >= 1  # 坏条目被跳过，内置清单保留
