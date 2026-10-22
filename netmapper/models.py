"""Plain data records produced by discovery and saved as JSON.

Step 2 (topology building) reads these back, so keep them simple and stable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Host:
    """An endpoint seen on the network (anything with an IP and/or MAC)."""

    ip: str | None
    mac: str | None
    sources: list[str] = field(default_factory=list)


@dataclass
class Interface:
    if_index: int
    name: str
    mac: str | None = None


@dataclass
class Neighbor:
    """A directly connected device reported by LLDP or CDP."""

    protocol: str  # "lldp" or "cdp"
    local_port: str
    remote_name: str | None = None
    remote_port: str | None = None
    remote_chassis_id: str | None = None
    remote_mgmt_ip: str | None = None
    remote_description: str | None = None
    remote_capabilities: list[str] = field(default_factory=list)


@dataclass
class FdbEntry:
    """A MAC address the switch has learned on one of its ports."""

    mac: str
    port: str
    if_index: int | None = None
    vlan: int | None = None


@dataclass
class ArpEntry:
    ip: str
    mac: str
    if_index: int | None = None


@dataclass
class SnmpDevice:
    """Everything read from one managed switch or router."""

    ip: str
    sys_name: str | None = None
    sys_descr: str | None = None
    sys_object_id: str | None = None
    sys_location: str | None = None  # free text set by the admin, e.g. "Floor 2 comms room"
    chassis_id: str | None = None
    interfaces: list[Interface] = field(default_factory=list)
    neighbors: list[Neighbor] = field(default_factory=list)
    fdb: list[FdbEntry] = field(default_factory=list)
    arp: list[ArpEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DiscoveryRun:
    started_at: str
    finished_at: str | None = None
    tool_version: str = ""
    collector_host: str | None = None
    hosts: list[Host] = field(default_factory=list)
    devices: list[SnmpDevice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DiscoveryRun:
        devices = []
        for d in raw.get("devices", []):
            d = dict(d)
            d["interfaces"] = [Interface(**i) for i in d.get("interfaces", [])]
            d["neighbors"] = [Neighbor(**n) for n in d.get("neighbors", [])]
            d["fdb"] = [FdbEntry(**f) for f in d.get("fdb", [])]
            d["arp"] = [ArpEntry(**a) for a in d.get("arp", [])]
            devices.append(SnmpDevice(**d))
        run = dict(raw)
        run["hosts"] = [Host(**h) for h in raw.get("hosts", [])]
        run["devices"] = devices
        return cls(**run)
