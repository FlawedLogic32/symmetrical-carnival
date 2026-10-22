"""DHCP leases: every device that asked for an address, even ones that are quiet now.

Two ways in, both off until configured:

* Lease files. Reading a file sends nothing at all. Understood formats:
  dnsmasq (most home/SMB routers, OpenWrt, Pi-hole), ISC dhcpd.leases (pfSense and
  Linux servers), and CSV or tab-separated exports with a header row, which covers
  Windows Server (``Get-DhcpServerv4Lease | Export-Csv``, or "Export List" in the DHCP
  console), Kea, and most router web pages that offer a download.
* Windows DHCP servers, read with PowerShell's ``Get-DhcpServerv4Lease``. That is the
  same management call the DHCP console makes, and needs the DhcpServer PowerShell
  module (RSAT) and an account allowed to read the server (DHCP Users group).
"""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import re
import subprocess
from pathlib import Path

from ..models import Host
from ..util import is_unicast_mac, normalize_mac
from .local import _run

log = logging.getLogger(__name__)

# Header names seen in real exports, lowercased with spaces and underscores removed.
_IP_COLUMNS = {"ipaddress", "ip", "address", "clientipaddress", "ipv4address", "leasedip"}
_MAC_COLUMNS = {"clientid", "macaddress", "mac", "hwaddr", "hardwareaddress", "uniqueid",
                "physicaladdress", "clientmac"}
_NAME_COLUMNS = {"hostname", "name", "host", "clienthostname", "devicename"}
_STATE_COLUMNS = {"addressstate", "state", "bindingstate", "status"}
_INACTIVE = ("expired", "declined", "inactive", "free", "released", "abandoned")

_DNSMASQ_LINE = re.compile(r"^\d+\s+(\S+)\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\S+)")
_ISC_LEASE = re.compile(r"lease\s+(\d{1,3}(?:\.\d{1,3}){3})\s*\{(.*?)\}", re.S)


def _key(header: str) -> str:
    return re.sub(r"[\s_\-]+", "", header.strip().lower())


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    name = name.strip().strip('"').rstrip(".")
    return None if name in {"", "*", "-"} else name.split(".")[0]


def _host(ip: str | None, mac: str | None, name: str | None, source: str) -> Host | None:
    try:
        ip = str(ipaddress.IPv4Address(ip.strip())) if ip else None
    except ValueError:
        ip = None
    mac = normalize_mac(mac) if mac else None
    if mac and not is_unicast_mac(mac):
        mac = None
    if not ip and not mac:
        return None
    return Host(ip=ip, mac=mac, sources=[source], name=_clean_name(name))


def parse_dnsmasq(text: str, source: str = "dhcp") -> list[Host]:
    hosts = []
    for line in text.splitlines():
        m = _DNSMASQ_LINE.match(line.strip())
        if m and (h := _host(m.group(2), m.group(1), m.group(3), source)):
            hosts.append(h)
    return hosts


def parse_isc(text: str, source: str = "dhcp") -> list[Host]:
    by_ip: dict[str, Host] = {}
    for m in _ISC_LEASE.finditer(text):
        body = m.group(2)
        state = re.search(r"^\s*binding state\s+(\w+)", body, re.M)
        if state and state.group(1).lower() != "active":
            by_ip.pop(m.group(1), None)  # later entries replace earlier ones
            continue
        mac = re.search(r"hardware ethernet\s+([0-9a-fA-F:]+)", body)
        name = re.search(r'client-hostname\s+"([^"]*)"', body)
        h = _host(m.group(1), mac.group(1) if mac else None, name.group(1) if name else None,
                  source)
        if h:
            by_ip[m.group(1)] = h
    return list(by_ip.values())


def parse_table(text: str, source: str = "dhcp") -> list[Host]:
    """CSV, semicolon or tab separated, with a header row naming the columns."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#TYPE")]
    if not lines:
        return []
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
    header = [_key(h) for h in rows[0]]

    def column(names: set[str]) -> int | None:
        return next((i for i, h in enumerate(header) if h in names), None)

    ip_col, mac_col = column(_IP_COLUMNS), column(_MAC_COLUMNS)
    name_col, state_col = column(_NAME_COLUMNS), column(_STATE_COLUMNS)
    if ip_col is None or mac_col is None:
        return []
    hosts = []
    for row in rows[1:]:
        def cell(i):
            return row[i].strip() if i is not None and i < len(row) else None

        state = (cell(state_col) or "").lower()
        if any(word in state for word in _INACTIVE):
            continue
        if header[state_col or 0] == "state" and state.isdigit() and state != "0":
            continue  # Kea: 0 is a live lease, 1 declined, 2 expired
        if h := _host(cell(ip_col), cell(mac_col), cell(name_col), source):
            hosts.append(h)
    return hosts


def parse_leases(text: str, source: str = "dhcp") -> list[Host]:
    """Work out the format from the content and parse it."""
    if _ISC_LEASE.search(text):
        return parse_isc(text, source)
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if _DNSMASQ_LINE.match(first.strip()):
        return parse_dnsmasq(text, source)
    return parse_table(text, source)


def read_lease_file(path: Path) -> list[Host]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("could not read DHCP lease file %s: %s", path, exc)
        return []
    # PowerShell's Export-Csv writes UTF-16 on Windows PowerShell 5.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    hosts = parse_leases(text, source=f"dhcp-file:{path.name}")
    log.info("DHCP lease file %s: %d leases", path, len(hosts))
    return hosts


_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-_]*$")

_POWERSHELL = (
    "$ErrorActionPreference = 'Stop'; "
    "Get-DhcpServerv4Scope -ComputerName '{server}' | "
    "ForEach-Object {{ Get-DhcpServerv4Lease -ComputerName '{server}' -ScopeId $_.ScopeId }} | "
    "Select-Object IPAddress, ClientId, HostName, AddressState | "
    "ConvertTo-Csv -NoTypeInformation"
)


def read_windows_dhcp(server: str) -> tuple[list[Host], str | None]:
    """Read every IPv4 lease from a Windows DHCP server. Returns (hosts, error)."""
    if not _SERVER_NAME.match(server):
        return [], f"DHCP server name {server!r} doesn't look like a host name or address"
    command = _POWERSHELL.format(server=server)
    try:
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                   timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"DHCP server {server}: could not run PowerShell ({exc})"
    hosts = parse_table(out, source=f"dhcp@{server}")
    if not hosts and "IPAddress" not in out:
        return [], (f"DHCP server {server}: no leases read. It needs the DhcpServer PowerShell "
                    "module (RSAT) and an account in the server's DHCP Users group")
    log.info("DHCP server %s: %d leases", server, len(hosts))
    return hosts, None
