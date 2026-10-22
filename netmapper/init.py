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
# The network(s) to map. Guessed from this PC's address; check the size (/24) is right.
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


def render_config(ip: str | None, gateway: str | None) -> str:
    subnet = str(ipaddress.IPv4Network(f"{ip}/24", strict=False)) if ip else "192.168.1.0/24"
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
    path.write_text(render_config(ip, gateway), encoding="utf-8")
    found = f"this PC is {ip}" if ip else "could not tell this PC's address"
    gw = f", gateway {gateway}" if gateway else ""
    return f"Wrote {path} ({found}{gw}). Check the subnet, and add SNMP credentials if you have them."
