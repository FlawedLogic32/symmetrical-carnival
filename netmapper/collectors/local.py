"""Collect from the machine running the tool: its ARP cache, and an optional slow ping sweep.

Reading the ARP cache sends no traffic at all. The ping sweep is off by default; when
enabled it pings one address at a time with a pause in between, which looks like
ordinary troubleshooting rather than a scan.
"""

from __future__ import annotations

import ipaddress
import logging
import platform
import re
import subprocess
import time
from collections.abc import Callable, Iterable

from ..models import Host
from ..util import is_unicast_mac, normalize_mac

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
# Stops a console window flashing up for every subprocess on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_WIN_ARP_LINE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+(\w+)"
)
_LINUX_NEIGH_LINE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})\s+.*\blladdr\s+([0-9a-fA-F:]{17})\s+(\w+)"
)


def _run(args: list[str], timeout: float = 15) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    return result.stdout


def parse_windows_arp(output: str) -> list[Host]:
    """Parse ``arp -a`` output from Windows."""
    hosts = []
    for line in output.splitlines():
        m = _WIN_ARP_LINE.match(line)
        if not m:
            continue
        mac = normalize_mac(m.group(2))
        if is_unicast_mac(mac):
            hosts.append(Host(ip=m.group(1), mac=mac, sources=["arp-cache"]))
    return hosts


def parse_linux_neigh(output: str) -> list[Host]:
    """Parse ``ip neigh show`` output (handy for testing on Linux)."""
    hosts = []
    for line in output.splitlines():
        m = _LINUX_NEIGH_LINE.match(line.strip())
        if not m or m.group(3) in {"FAILED", "INCOMPLETE"}:
            continue
        mac = normalize_mac(m.group(2))
        if is_unicast_mac(mac):
            hosts.append(Host(ip=m.group(1), mac=mac, sources=["arp-cache"]))
    return hosts


def read_arp_cache() -> list[Host]:
    try:
        if IS_WINDOWS:
            return parse_windows_arp(_run(["arp", "-a"]))
        return parse_linux_neigh(_run(["ip", "-4", "neigh", "show"]))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not read ARP cache: %s", exc)
        return []


def ping_once(ip: str, timeout_ms: int) -> bool:
    if IS_WINDOWS:
        args = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        args = ["ping", "-c", "1", "-W", str(max(1, round(timeout_ms / 1000))), ip]
    try:
        out = _run(args, timeout=timeout_ms / 1000 + 5)
    except (OSError, subprocess.SubprocessError):
        return False
    # Windows ping exits 0 for "Destination host unreachable", so check for a real reply.
    return "ttl=" in out.lower()


def slow_ping_sweep(
    networks: Iterable[ipaddress.IPv4Network],
    interval_seconds: float,
    timeout_ms: int,
    ping: Callable[[str, int], bool] = ping_once,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Ping each address in turn, one at a time, pausing between them.

    Returns the addresses that answered. Its main purpose is to fill the ARP cache,
    which is read again afterwards.
    """
    alive = []
    for net in networks:
        count = net.num_addresses - 2 if net.prefixlen < 31 else net.num_addresses
        log.info("ping sweep of %s (%d addresses, ~%.0fs)", net, count, count * interval_seconds)
        for addr in net.hosts():
            ip = str(addr)
            if ping(ip, timeout_ms):
                alive.append(ip)
            sleep(interval_seconds)
    return alive
