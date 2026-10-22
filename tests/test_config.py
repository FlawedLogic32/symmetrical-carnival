import pytest

from netmapper.config import ConfigError, config_from_dict, load_config


def test_example_config_loads(monkeypatch):
    monkeypatch.setenv("SNMP_COMMUNITY", "s3cret")
    cfg = load_config("config.example.toml")
    assert str(cfg.subnets[0]) == "192.168.1.0/24"
    assert cfg.ping_sweep is False
    assert cfg.probe_discovered_hosts is False
    assert cfg.snmp_credentials[0].community == "s3cret"
    assert "s3cret" not in cfg.snmp_credentials[0].label()


def test_missing_env_secret_is_an_error(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        config_from_dict({"snmp": {"credentials": [{"version": "2c", "community": "env:NOPE"}]}})


def test_bad_values():
    with pytest.raises(ConfigError):
        config_from_dict({"discovery": {"subnets": ["not-a-net"]}})
    with pytest.raises(ConfigError):
        config_from_dict({"snmp": {"credentials": [{"version": "3"}]}})
