"""Run every collector and gather the results into one DiscoveryRun."""

from __future__ import annotations

import json
import logging
import socket
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .collectors import local
from .collectors.snmp_device import ClientFactory, collect_device, connect
from .config import Config
from .models import DiscoveryRun, Host
from .util import ip_in_networks

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_factory(cfg: Config) -> ClientFactory:
    from .collectors.snmp_client import PySnmpClient

    def make(ip, cred):
        return PySnmpClient(ip, cred, timeout=cfg.snmp_timeout_seconds, retries=cfg.snmp_retries)

    return make


def merge_hosts(*groups: list[Host]) -> list[Host]:
    """Combine host sightings, keyed by MAC (or IP when no MAC is known)."""
    merged: dict[str, Host] = {}
    for group in groups:
        for h in group:
            key = h.mac or f"ip:{h.ip}"
            existing = merged.get(key)
            if existing is None:
                merged[key] = Host(ip=h.ip, mac=h.mac, sources=list(h.sources))
                continue
            existing.ip = existing.ip or h.ip
            for src in h.sources:
                if src not in existing.sources:
                    existing.sources.append(src)
    return sorted(merged.values(), key=lambda h: (h.ip is None, h.ip or "", h.mac or ""))


def run_discovery(cfg: Config, snmp_factory: ClientFactory | None = None) -> DiscoveryRun:
    run = DiscoveryRun(started_at=_now(), tool_version=__version__,
                       collector_host=socket.gethostname())

    if cfg.ping_sweep:
        if cfg.subnets:
            local.slow_ping_sweep(cfg.subnets, cfg.ping_interval_seconds, cfg.ping_timeout_ms)
        else:
            run.notes.append("ping sweep skipped: no subnets configured")

    arp_hosts = local.read_arp_cache()
    if cfg.subnets:
        arp_hosts = [h for h in arp_hosts if ip_in_networks(h.ip, cfg.subnets)]
    log.info("ARP cache: %d hosts", len(arp_hosts))

    snmp_hosts: list[Host] = []
    if cfg.snmp_enabled and cfg.snmp_credentials:
        factory = snmp_factory or _default_factory(cfg)
        seeds = list(cfg.snmp_seeds)
        if cfg.probe_discovered_hosts:
            seeds += [h.ip for h in arp_hosts if h.ip]
        run.devices = _crawl(cfg, seeds, factory, run)
        for dev in run.devices:
            snmp_hosts += [Host(ip=a.ip, mac=a.mac, sources=[f"arp@{dev.ip}"]) for a in dev.arp]
            snmp_hosts += [Host(ip=None, mac=f.mac, sources=[f"fdb@{dev.ip}"]) for f in dev.fdb]
    elif cfg.snmp_enabled:
        run.notes.append("SNMP skipped: no credentials configured")

    run.hosts = merge_hosts(arp_hosts, snmp_hosts)
    run.finished_at = _now()
    return run


# Neighbours advertising one of these are network gear worth querying. Phones,
# PCs and printers are left alone so the tool doesn't poke at endpoints.
NETWORK_CAPABILITIES = {"bridge", "switch", "router", "wlan-ap", "repeater"}


def _worth_crawling(capabilities: list[str]) -> bool:
    return not capabilities or bool(NETWORK_CAPABILITIES & set(capabilities))


def _crawl(cfg: Config, seeds: list[str], factory: ClientFactory, run: DiscoveryRun):
    """Query each seed, then follow LLDP/CDP neighbours that are network gear
    inside the configured subnets."""
    queue = deque(dict.fromkeys(seeds))
    seen: set[str] = set(queue)
    devices = []
    while queue and len(devices) < cfg.max_devices:
        ip = queue.popleft()
        client = connect(ip, cfg.snmp_credentials, factory)
        if client is None:
            run.notes.append(f"{ip}: no SNMP response with any configured credential")
            continue
        try:
            device = collect_device(client, ip)
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        log.info("%s (%s): %d neighbours, %d MAC entries, %d ARP entries", ip,
                 device.sys_name, len(device.neighbors), len(device.fdb), len(device.arp))
        devices.append(device)
        if not cfg.crawl_neighbors:
            continue
        for n in device.neighbors:
            nip = n.remote_mgmt_ip
            if (nip and nip not in seen and ip_in_networks(nip, cfg.subnets)
                    and _worth_crawling(n.remote_capabilities)):
                seen.add(nip)
                queue.append(nip)
    if queue:
        run.notes.append(f"stopped after max_devices={cfg.max_devices}; {len(queue)} not queried")
    return devices


def save_run(run: DiscoveryRun, directory: Path) -> Path:
    """Write the run as runs/discovery-<timestamp>.json and update runs/latest.json."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.replace(":", "").replace("+0000", "Z").replace("-", "")
    path = directory / f"discovery-{stamp}.json"
    text = json.dumps(run.to_dict(), indent=2)
    path.write_text(text, encoding="utf-8")
    (directory / "latest.json").write_text(text, encoding="utf-8")
    return path
