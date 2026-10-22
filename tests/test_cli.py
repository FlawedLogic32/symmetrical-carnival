from unittest import mock

from fake_snmp import FakeSnmp
from test_snmp_device import core_switch

from netmapper import cli
from netmapper.config import load_config
from netmapper.init import render_config
from netmapper.models import Host


def test_starter_config_loads():
    cfg_text = render_config("192.168.7.23", "192.168.7.1")
    assert 'subnets = ["192.168.7.0/24"]' in cfg_text
    assert 'seeds = ["192.168.7.1"]' in cfg_text
    assert "\n[[snmp.credentials]]" not in cfg_text  # commented out until the user adds some


def test_starter_config_without_network(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(render_config(None, None), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.snmp_seeds == [] and cfg.snmp_credentials == []


def test_discover_twice_writes_diagrams_and_reports_changes(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(
        '[discovery]\nsubnets = ["192.168.1.0/24"]\n'
        '[snmp]\nseeds = ["192.168.1.2"]\n'
        '[[snmp.credentials]]\nversion = "2c"\ncommunity = "public"\n', encoding="utf-8")
    first = [Host("192.168.1.20", "a4:5e:60:01:01:01", ["arp-cache"])]
    second = first + [Host("192.168.1.77", "a4:5e:60:09:09:09", ["arp-cache"])]

    def factory(cfg):
        return lambda ip, cred: FakeSnmp(core_switch(neighbor_ip="10.9.9.9"))

    with mock.patch("netmapper.discovery._default_factory", factory):
        with mock.patch("netmapper.discovery.local.read_arp_cache", return_value=first):
            assert cli.main(["discover", "-c", str(config)]) == 0
        out = capsys.readouterr().out
        assert "First run" in out
        with mock.patch("netmapper.discovery.local.read_arp_cache", return_value=second):
            assert cli.main(["discover", "-c", str(config), "--show"]) == 0
        out = capsys.readouterr().out

    assert "New: 192.168.1.77" in out
    assert "core-sw (switch" in out
    runs = tmp_path / "runs"
    for name in ("latest.json", "topology-latest.json", "network-latest.html",
                 "network-latest.drawio"):
        assert (runs / name).exists(), name
    assert "core-sw" in (runs / "network-latest.html").read_text(encoding="utf-8")

    # Rebuilding from the saved run touches no network and needs no SNMP.
    with mock.patch("netmapper.discovery.local.read_arp_cache") as arp:
        assert cli.main(["topology", "-c", str(config)]) == 0
        arp.assert_not_called()


def test_init_refuses_to_overwrite(tmp_path):
    path = tmp_path / "config.toml"
    assert cli.main(["init", "-c", str(path)]) == 0
    assert load_config(path).subnets
    assert cli.main(["init", "-c", str(path)]) == 1
