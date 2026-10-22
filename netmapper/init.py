"""``netmapper init``: write a starter config.toml for the network this PC is on.

Finding the local address uses a UDP socket that is "connected" but never sends
anything, and the default gateway comes from the PC's own routing table, so this
creates no network traffic.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from pathlib import Path

from .collectors.local import IS_WINDOWS, _run

TEMPLATE = """\
# netmapper configuration. Created by "netmapper init"; edit freely.
# Every option is explained in config.example.toml in the project.

[discovery]
# The network(s) to map, worked out from this PC's address and subnet mask.
subnets = ["{subnet}"]

# Off by default. Pings one address at a time with a pause between each.
ping_sweep = false
ping_interval_seconds = 0.5

[snmp]
# Managed switches and routers to read. {seed_comment}
seeds = [{seeds}]
crawl_neighbors = true
probe_discovered_hosts = false

# Until you add read-only SNMP credentials, netmapper uses only this PC's ARP cache.
# Uncomment one block, and set the secret as an environment variable, for example
# in PowerShell:  $env:SNMP_COMMUNITY = "your-read-only-community"
#
# [[snmp.credentials]]
# version = "2c"
# community = "env:SNMP_COMMUNITY"
#
# [[snmp.credentials]]
# version = "3"
# username = "netmapper"
# auth_protocol = "sha"
# auth_key = "env:SNMP_AUTH"
# priv_protocol = "aes"
# priv_key = "env:SNMP_PRIV"

[topology]
resolve_names = false
oui_file = "oui.csv"

[diagram]
# Draw a frame round each site, floor or room. See config.example.toml.
group_by_snmp_location = false
# [[diagram.groups]]
# name = "First floor"
# members = ["floor1-sw*", "192.168.20.0/24"]

[output]
directory = "runs"
"""


def local_ipv4() -> str | None:
    """The address this PC uses to reach other networks. Sends no packets."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET address; connect() on UDP only picks a route
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def local_prefix(ip: str) -> int | None:
    """The prefix length of this PC's address, read from ipconfig / ip addr."""
    try:
        if IS_WINDOWS:
            out = _run(["ipconfig"])
            # The subnet mask is the first 255.x.x.x after our address in its adapter block.
            after = out[out.find(ip) + len(ip):] if ip in out else ""
            m = re.search(r"\b(255\.\d+\.\d+\.\d+)\b", after)
            if m:
                return ipaddress.IPv4Network(f"0.0.0.0/{m.group(1)}").prefixlen
        else:
            out = _run(["ip", "-4", "-o", "addr", "show"])
            m = re.search(rf"inet {re.escape(ip)}/(\d+)", out)
            if m:
                return int(m.group(1))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def guess_subnet(ip: str | None, prefix: int | None, gateway: str | None) -> str:
    if not ip:
        return "192.168.1.0/24"
    # Very large networks would make the optional ping sweep take days; cap at /16.
    prefix = 24 if prefix is None else max(16, prefix)
    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    if gateway and ipaddress.IPv4Address(gateway) not in net and prefix > 16:
        # The real mask is wider than we thought; widen until the gateway fits.
        while prefix > 16 and ipaddress.IPv4Address(gateway) not in net:
            prefix -= 1
            net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    return str(net)


def default_gateway() -> str | None:
    try:
        if IS_WINDOWS:
            out = _run(["route", "print", "-4", "0.0.0.0"])
            m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out, re.M)
        else:
            out = _run(["ip", "-4", "route", "show", "default"])
            m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
    except (OSError, subprocess.SubprocessError):
        return None
    return m.group(1) if m else None


def render_config(ip: str | None, gateway: str | None, prefix: int | None = None) -> str:
    subnet = guess_subnet(ip, prefix, gateway)
    if gateway:
        seeds, comment = f'"{gateway}"', "Starts with your default gateway."
    else:
        seeds, comment = "", 'Add your core switch and router, e.g. ["192.168.1.1"].'
    return TEMPLATE.format(subnet=subnet, seeds=seeds, seed_comment=comment)


def write_starter_config(path: Path) -> str:
    """Write the config and return a one-line summary. Never overwrites an existing file."""
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    ip, gateway = local_ipv4(), default_gateway()
    prefix = local_prefix(ip) if ip else None
    path.write_text(render_config(ip, gateway, prefix), encoding="utf-8")
    found = f"this PC is {ip}" if ip else "could not tell this PC's address"
    gw = f", gateway {gateway}" if gateway else ""
    return f"Wrote {path} ({found}{gw}). Check the subnet, and add SNMP credentials if you have them."
