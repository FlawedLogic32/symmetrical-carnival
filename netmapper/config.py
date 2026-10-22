"""Load the TOML config file. See config.example.toml for every option."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class SnmpCredential:
    version: str  # "1", "2c" or "3"
    community: str | None = None
    username: str | None = None
    auth_protocol: str | None = None  # "md5", "sha", "sha256", "sha512"
    auth_key: str | None = None
    priv_protocol: str | None = None  # "des", "aes", "aes256"
    priv_key: str | None = None

    def label(self) -> str:
        """A description safe to log: never includes the secret."""
        if self.version == "3":
            return f"v3 user '{self.username}'"
        return f"v{self.version} community"


@dataclass
class EmailAlert:
    smtp_host: str
    to: list[str]
    sender: str
    smtp_port: int = 587
    security: str = "starttls"  # "starttls", "ssl" or "none"
    username: str | None = None
    password: str | None = None


@dataclass
class WebhookAlert:
    url: str
    format: str = "json"  # "json", "teams", "slack" or "discord"


@dataclass
class AlertsConfig:
    on: set[str] = field(default_factory=lambda: {"new"})  # any of "new", "gone", "moved"
    desktop: bool = False
    email: EmailAlert | None = None
    webhook: WebhookAlert | None = None


ALERT_EVENTS = {"new", "gone", "moved"}
WEBHOOK_FORMATS = {"json", "teams", "slack", "discord"}


@dataclass
class GroupRule:
    """A site, floor or room on the diagram, and what belongs in it."""

    name: str
    # Subnets and single addresses, or lower-case name/MAC patterns such as "floor2-*".
    members: list[ipaddress.IPv4Network | str] = field(default_factory=list)


@dataclass
class Config:
    subnets: list[ipaddress.IPv4Network] = field(default_factory=list)
    ping_sweep: bool = False
    ping_interval_seconds: float = 0.5
    ping_timeout_ms: int = 1000
    snmp_enabled: bool = True
    snmp_seeds: list[str] = field(default_factory=list)
    snmp_credentials: list[SnmpCredential] = field(default_factory=list)
    snmp_timeout_seconds: float = 2.0
    snmp_retries: int = 1
    snmp_port: int = 161
    crawl_neighbors: bool = True
    probe_discovered_hosts: bool = False
    max_devices: int = 200
    output_directory: Path = Path("runs")
    resolve_names: bool = False
    oui_file: Path = Path("oui.csv")
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    diagram_groups: list[GroupRule] = field(default_factory=list)
    group_by_location: bool = False


def _secret(value: str | None) -> str | None:
    """Values written as ``env:NAME`` are read from that environment variable,
    so passwords don't have to sit in the config file."""
    if value and value.startswith("env:"):
        name = value[4:]
        if name not in os.environ:
            raise ConfigError(f"environment variable {name} is not set")
        return os.environ[name]
    return value


def _credential(raw: dict) -> SnmpCredential:
    version = str(raw.get("version", "2c")).lower().lstrip("v")
    if version not in {"1", "2c", "3"}:
        raise ConfigError(f"unsupported SNMP version {raw.get('version')!r}")
    cred = SnmpCredential(
        version=version,
        community=_secret(raw.get("community")),
        username=raw.get("username"),
        auth_protocol=(raw.get("auth_protocol") or None),
        auth_key=_secret(raw.get("auth_key")),
        priv_protocol=(raw.get("priv_protocol") or None),
        priv_key=_secret(raw.get("priv_key")),
    )
    if version in {"1", "2c"} and not cred.community:
        raise ConfigError("SNMP v1/v2c credentials need a community")
    if version == "3" and not cred.username:
        raise ConfigError("SNMP v3 credentials need a username")
    return cred


def _alerts(raw: dict) -> AlertsConfig:
    on = {str(e).lower() for e in raw.get("on", ["new"])}
    if on - ALERT_EVENTS:
        raise ConfigError(f"alerts.on: unknown events {sorted(on - ALERT_EVENTS)}; "
                          f"use {sorted(ALERT_EVENTS)}")
    alerts = AlertsConfig(on=on, desktop=bool(raw.get("desktop", False)))

    email = raw.get("email", {})
    if email.get("enabled", True) and email.get("smtp_host"):
        to = email.get("to", [])
        to = [to] if isinstance(to, str) else [str(t) for t in to]
        if not to:
            raise ConfigError("alerts.email needs at least one 'to' address")
        security = str(email.get("security", "starttls")).lower()
        if security not in {"starttls", "ssl", "none"}:
            raise ConfigError("alerts.email.security must be starttls, ssl or none")
        default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
        username = _secret(email.get("username"))
        alerts.email = EmailAlert(
            smtp_host=str(email["smtp_host"]),
            smtp_port=int(email.get("smtp_port", default_port)),
            security=security,
            username=username,
            password=_secret(email.get("password")),
            sender=str(email.get("from") or username or f"netmapper@{email['smtp_host']}"),
            to=to,
        )

    hook = raw.get("webhook", {})
    if hook.get("enabled", True) and hook.get("url"):
        fmt = str(hook.get("format", "json")).lower()
        if fmt not in WEBHOOK_FORMATS:
            raise ConfigError(f"alerts.webhook.format must be one of {sorted(WEBHOOK_FORMATS)}")
        url = _secret(str(hook["url"]))
        if not url.lower().startswith(("https://", "http://")):
            raise ConfigError("alerts.webhook.url must start with https://")
        alerts.webhook = WebhookAlert(url=url, format=fmt)
    return alerts


def _group(raw: dict) -> GroupRule:
    name = str(raw.get("name", "")).strip()
    if not name:
        raise ConfigError("each [[diagram.groups]] entry needs a name")
    members: list[ipaddress.IPv4Network | str] = []
    for member in raw.get("members", []):
        member = str(member).strip()
        try:
            members.append(ipaddress.IPv4Network(member, strict=False))
        except ValueError:
            members.append(member.lower())
    return GroupRule(name, members)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return config_from_dict(raw, base_dir=path.parent)


def config_from_dict(raw: dict, base_dir: Path = Path(".")) -> Config:
    disc = raw.get("discovery", {})
    snmp = raw.get("snmp", {})
    out = raw.get("output", {})
    topo = raw.get("topology", {})
    alerts = raw.get("alerts", {})
    diagram = raw.get("diagram", {})

    try:
        subnets = [ipaddress.IPv4Network(s, strict=False) for s in disc.get("subnets", [])]
    except ValueError as exc:
        raise ConfigError(f"bad subnet: {exc}") from None

    output_dir = Path(out.get("directory", "runs"))
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir
    oui_file = Path(topo.get("oui_file", "oui.csv"))
    if not oui_file.is_absolute():
        oui_file = base_dir / oui_file

    return Config(
        subnets=subnets,
        ping_sweep=bool(disc.get("ping_sweep", False)),
        ping_interval_seconds=float(disc.get("ping_interval_seconds", 0.5)),
        ping_timeout_ms=int(disc.get("ping_timeout_ms", 1000)),
        snmp_enabled=bool(snmp.get("enabled", True)),
        snmp_seeds=[str(s) for s in snmp.get("seeds", [])],
        snmp_credentials=[_credential(c) for c in snmp.get("credentials", [])],
        snmp_timeout_seconds=float(snmp.get("timeout_seconds", 2.0)),
        snmp_retries=int(snmp.get("retries", 1)),
        snmp_port=int(snmp.get("port", 161)),
        crawl_neighbors=bool(snmp.get("crawl_neighbors", True)),
        probe_discovered_hosts=bool(snmp.get("probe_discovered_hosts", False)),
        max_devices=int(snmp.get("max_devices", 200)),
        output_directory=output_dir,
        resolve_names=bool(topo.get("resolve_names", False)),
        oui_file=oui_file,
        alerts=_alerts(alerts),
        diagram_groups=[_group(g) for g in diagram.get("groups", [])],
        group_by_location=bool(diagram.get("group_by_snmp_location", False)),
    )
