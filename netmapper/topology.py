"""Turn a discovery run into a topology: devices (nodes) and the cables between them (links).

How links are worked out, most reliable first:

1. LLDP/CDP: a managed device says exactly what is plugged into each port.
2. MAC tables between switches: if switch A sees switch B behind port p, B sees A
   behind port q, and nothing else known sits behind both, A:p and B:q are cabled
   together. This catches switches that don't run LLDP.
3. Endpoints: each host MAC is placed on the switch port where it is learned that
   isn't an uplink to another switch.
4. Unmanaged switches: an edge port that has several endpoint MACs behind it must
   have something in between that can't be queried, so an "unmanaged switch" node
   is inferred there. Hosts seen on a port facing a phone, an access point or a
   switch we couldn't query are attached to that device instead.
5. VM hosts: a server running virtual machines also puts several MACs on one port.
   If (nearly) all of them come from a hypervisor's MAC range, the port goes to a
   "VM host" instead: the one physical MAC if there is one, otherwise an inferred
   node, with the VMs hanging off it.

Anything seen only in an ARP cache, with no switch port, is attached to a
"location unknown" node so it still appears on the diagram.
"""

from __future__ import annotations

import logging
import socket
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import DiscoveryRun, Neighbor, SnmpDevice
from .oui import VendorLookup, virtual_nic_platform
from .util import normalize_mac

log = logging.getLogger(__name__)

# Node kinds. Step 3 picks an icon per kind.
ROUTER = "router"
SWITCH = "switch"
UNMANAGED_SWITCH = "unmanaged-switch"
VM_HOST = "vm-host"
ACCESS_POINT = "access-point"
PHONE = "phone"
NETWORK_DEVICE = "network-device"
HOST = "host"
UNKNOWN_SEGMENT = "unknown-segment"

INFRASTRUCTURE = {ROUTER, SWITCH, UNMANAGED_SWITCH, ACCESS_POINT, NETWORK_DEVICE}


@dataclass
class Node:
    id: str
    kind: str
    label: str
    ip: str | None = None
    mac: str | None = None
    vendor: str | None = None
    managed: bool = False  # answered SNMP
    inferred: bool = False  # not seen directly, deduced from MAC tables
    description: str | None = None


@dataclass
class Link:
    source: str
    target: str
    source_port: str | None = None
    target_port: str | None = None
    evidence: str = "lldp"  # lldp, cdp, mac-table, inferred
    inferred: bool = False


@dataclass
class Topology:
    generated_at: str
    source_run: str | None = None
    nodes: list[Node] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Topology:
        raw = dict(raw)
        raw["nodes"] = [Node(**n) for n in raw.get("nodes", [])]
        raw["links"] = [Link(**lk) for lk in raw.get("links", [])]
        return cls(**raw)

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)


def _short_name(name: str | None) -> str | None:
    """'core-sw.example.local' and 'CORE-SW' both become 'core-sw'."""
    if not name:
        return None
    return name.strip().split(".")[0].lower() or None


def _cisco_phone_mac(name: str | None) -> str | None:
    """Cisco phones report their CDP name as SEP followed by their MAC."""
    if name and len(name) == 15 and name.upper().startswith("SEP"):
        return normalize_mac(name[3:])
    return None


def _kind_from_capabilities(caps: Iterable[str]) -> str | None:
    caps = set(caps)
    if "wlan-ap" in caps:
        return ACCESS_POINT
    if "telephone" in caps:
        return PHONE
    if caps & {"bridge", "switch"}:
        return SWITCH
    if "router" in caps:
        return ROUTER
    if caps & {"host", "station"}:
        return HOST
    return None


class _Builder:
    def __init__(self, run: DiscoveryRun, vendors: VendorLookup, names: dict[str, str]):
        self.run = run
        self.vendors = vendors
        self.names = names
        self.nodes: dict[str, Node] = {}
        self.links: dict[frozenset[str], Link] = {}
        self.notes: list[str] = []
        # identity key -> node id. Keys look like ("ip", "10.0.0.1"), ("mac", ...), ("name", ...)
        self.identity: dict[tuple[str, str], str] = {}
        # ports that face another piece of infrastructure, per managed node
        self.uplinks: dict[str, set[str]] = defaultdict(set)
        # (node id, port) -> neighbour node on that port that we could not query
        self.port_neighbor: dict[tuple[str, str], str] = {}
        self.capabilities: dict[str, set[str]] = defaultdict(set)

    # -- identities -------------------------------------------------------

    def _register(self, node_id: str, ip=None, macs=(), name=None) -> None:
        if ip:
            self.identity.setdefault(("ip", ip), node_id)
        for mac in macs:
            mac = normalize_mac(mac)
            if mac:
                self.identity.setdefault(("mac", mac), node_id)
        short = _short_name(name)
        if short:
            self.identity.setdefault(("name", short), node_id)

    def _resolve(self, ip=None, mac=None, name=None) -> str | None:
        for key in (("ip", ip), ("mac", normalize_mac(mac)), ("name", _short_name(name))):
            if key[1] and key in self.identity:
                return self.identity[key]
        return None

    def owner_of_mac(self, mac: str) -> str | None:
        return self.identity.get(("mac", mac))

    # -- links ------------------------------------------------------------

    def add_link(self, a: str, a_port, b: str, b_port, evidence: str, inferred=False) -> None:
        if a == b:
            return
        key = frozenset((a, b))
        existing = self.links.get(key)
        if existing is None:
            self.links[key] = Link(a, b, a_port, b_port, evidence, inferred)
            return
        # Seen from the other end too: fill in whichever port names are missing.
        ports = {existing.source: existing.source_port, existing.target: existing.target_port}
        ports[a] = ports[a] or a_port
        ports[b] = ports[b] or b_port
        existing.source_port = ports[existing.source]
        existing.target_port = ports[existing.target]

    # -- steps ------------------------------------------------------------

    def add_managed_devices(self) -> None:
        for dev in self.run.devices:
            node_id = f"dev:{dev.ip}"
            self.nodes[node_id] = Node(
                id=node_id, kind=NETWORK_DEVICE, label=dev.sys_name or dev.ip, ip=dev.ip,
                mac=normalize_mac(dev.chassis_id), managed=True, description=dev.sys_descr,
            )
            self._register(node_id, ip=dev.ip, name=dev.sys_name,
                           macs=[dev.chassis_id] + [i.mac for i in dev.interfaces])
            for arp in dev.arp:
                # A router's ARP table includes its own interface addresses on some platforms.
                if arp.mac in {normalize_mac(i.mac) for i in dev.interfaces}:
                    self._register(node_id, ip=arp.ip)

    def _neighbor_node(self, n: Neighbor) -> str:
        found = self._resolve(n.remote_mgmt_ip, n.remote_chassis_id, n.remote_name)
        if found:
            self.capabilities[found].update(n.remote_capabilities)
            return found
        chassis = normalize_mac(n.remote_chassis_id) or _cisco_phone_mac(n.remote_name)
        node_id = f"nbr:{chassis or _short_name(n.remote_name) or n.remote_mgmt_ip}"
        kind = _kind_from_capabilities(n.remote_capabilities) or NETWORK_DEVICE
        self.nodes[node_id] = Node(
            id=node_id, kind=kind, label=n.remote_name or n.remote_mgmt_ip or node_id[4:],
            ip=n.remote_mgmt_ip, mac=chassis, description=n.remote_description,
        )
        self.capabilities[node_id].update(n.remote_capabilities)
        self._register(node_id, ip=n.remote_mgmt_ip, macs=[chassis] if chassis else [],
                       name=n.remote_name)
        return node_id

    def add_neighbor_links(self) -> None:
        for dev in self.run.devices:
            dev_id = f"dev:{dev.ip}"
            for n in dev.neighbors:
                if n.remote_name is None and n.remote_chassis_id is None and not n.remote_mgmt_ip:
                    continue
                other = self._neighbor_node(n)
                if other == dev_id:
                    continue
                self.add_link(dev_id, n.local_port, other, n.remote_port, n.protocol)
                other_node = self.nodes[other]
                if other_node.managed:
                    self.uplinks[dev_id].add(n.local_port)
                else:
                    # Anything learned on this port sits behind (or is) that device.
                    self.port_neighbor.setdefault((dev_id, n.local_port), other)

    def classify_managed(self) -> None:
        for dev in self.run.devices:
            node = self.nodes[f"dev:{dev.ip}"]
            kind = _kind_from_capabilities(self.capabilities[node.id])
            if kind in (None, HOST):
                kind = SWITCH if dev.fdb else ROUTER if dev.arp else NETWORK_DEVICE
            node.kind = kind

    def infer_switch_links_from_mac_tables(self) -> None:
        """Link managed switches that see each other in their MAC tables (no LLDP needed)."""
        managed = {f"dev:{d.ip}": d for d in self.run.devices if d.fdb}
        # behind[node][port] = known infrastructure nodes learned on that port
        behind: dict[str, dict[str, set[str]]] = {}
        for node_id, dev in managed.items():
            per_port: dict[str, set[str]] = defaultdict(set)
            for entry in dev.fdb:
                owner = self.owner_of_mac(entry.mac)
                if owner and owner != node_id and self.nodes[owner].kind in INFRASTRUCTURE:
                    per_port[entry.port].add(owner)
            behind[node_id] = per_port
            for port, owners in per_port.items():
                if any(self.nodes[o].managed for o in owners):
                    self.uplinks[node_id].add(port)

        def port_towards(a: str, b: str) -> str | None:
            return next((p for p, owners in behind[a].items() if b in owners), None)

        ids = sorted(managed)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if frozenset((a, b)) in self.links:
                    continue
                pa, pb = port_towards(a, b), port_towards(b, a)
                if pa and pb and not (behind[a][pa] & behind[b][pb]):
                    self.add_link(a, pa, b, pb, "mac-table", inferred=True)

    def place_endpoints(self) -> None:
        managed_ids = {f"dev:{d.ip}" for d in self.run.devices}
        sightings: dict[str, list[tuple[str, str]]] = defaultdict(list)
        port_load: dict[tuple[str, str], int] = defaultdict(int)
        for dev in self.run.devices:
            dev_id = f"dev:{dev.ip}"
            for entry in dev.fdb:
                if entry.port in self.uplinks[dev_id]:
                    continue
                sightings[entry.mac].append((dev_id, entry.port))
                port_load[(dev_id, entry.port)] += 1

        ip_of = {h.mac: h.ip for h in self.run.hosts if h.mac and h.ip}
        for dev in self.run.devices:
            for arp in dev.arp:
                ip_of.setdefault(arp.mac, arp.ip)

        endpoints: dict[tuple[str, str], list[str]] = defaultdict(list)
        unplaced: list[str] = []
        for host in self.run.hosts:
            mac = host.mac
            if mac is None:
                if host.ip and not self._resolve(ip=host.ip):
                    unplaced.append(self._add_host(None, host.ip))
                continue
            owner = self.owner_of_mac(mac)
            if owner and owner not in managed_ids:
                # A phone or AP reported by LLDP; it is already linked. Fill in its IP.
                self.nodes[owner].ip = self.nodes[owner].ip or ip_of.get(mac)
                continue
            if owner:
                continue
            spots = sightings.get(mac)
            host_id = self._add_host(mac, ip_of.get(mac))
            if not spots:
                unplaced.append(host_id)
                continue
            # Normally one edge port; if several, the quietest port is the closest.
            endpoints[min(spots, key=lambda s: port_load[s])].append(host_id)

        for (dev_id, port), hosts in sorted(endpoints.items()):
            neighbor = self.port_neighbor.get((dev_id, port))
            if neighbor:
                for h in hosts:
                    self.add_link(neighbor, None, h, None, "mac-table", inferred=True)
            elif len(hosts) == 1:
                self.add_link(dev_id, port, hosts[0], None, "mac-table")
            elif self._place_vm_host(dev_id, port, hosts):
                pass
            else:
                ums = f"ums:{dev_id[4:]}:{port}"
                self.nodes[ums] = Node(id=ums, kind=UNMANAGED_SWITCH, inferred=True,
                                       label=f"Unmanaged switch ({self.nodes[dev_id].label} {port})")
                self.add_link(dev_id, port, ums, None, "inferred", inferred=True)
                for h in hosts:
                    self.add_link(ums, None, h, None, "inferred", inferred=True)

        if unplaced:
            seg = "segment:unknown"
            self.nodes[seg] = Node(id=seg, kind=UNKNOWN_SEGMENT, inferred=True,
                                   label="Location unknown")
            for h in unplaced:
                self.add_link(seg, None, h, None, "inferred", inferred=True)
            if self.run.devices:
                count = len(unplaced)
                self.notes.append(f"{count} host{'s were' if count != 1 else ' was'} not in any "
                                  "switch MAC table, so the switch port is unknown")
            else:
                self.notes.append("no managed switches were queried, so connections "
                                  "between devices are unknown")

    def _place_vm_host(self, dev_id: str, port: str, hosts: list[str]) -> bool:
        """Attach a port's hosts to a VM host if they look like VMs. False if they don't.

        A hypervisor's own NIC is often silent or uses a virtual MAC too (ESXi's vmk0,
        a Hyper-V host's vNIC), so all-virtual is the common case. With two or more
        physical MACs there really is something switching in between.
        """
        physical = [h for h in hosts if not virtual_nic_platform(self.nodes[h].mac)]
        if len(physical) > 1 or len(physical) == len(hosts):
            return False
        if physical:
            vm_host = physical[0]
            self.nodes[vm_host].kind = VM_HOST
            self.add_link(dev_id, port, vm_host, None, "mac-table")
        else:
            vm_host = f"vmhost:{dev_id[4:]}:{port}"
            self.nodes[vm_host] = Node(id=vm_host, kind=VM_HOST, inferred=True,
                                       label=f"VM host ({self.nodes[dev_id].label} {port})")
            self.add_link(dev_id, port, vm_host, None, "inferred", inferred=True)
        for h in hosts:
            if h != vm_host:
                self.add_link(vm_host, None, h, None, "inferred", inferred=True)
        return True

    def _add_host(self, mac: str | None, ip: str | None) -> str:
        node_id = f"host:{mac or ip}"
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(id=node_id, kind=HOST, label="", ip=ip, mac=mac)
            self._register(node_id, ip=ip, macs=[mac] if mac else [])
        return node_id

    def finish(self) -> None:
        for node in self.nodes.values():
            node.vendor = node.vendor or self.vendors.vendor(node.mac)
            dns = self.names.get(node.ip or "")
            if not node.label:
                node.label = dns or node.ip or node.mac or node.id
            elif dns and node.kind in (HOST, VM_HOST):
                node.label = dns


def lookup_names(ips: Iterable[str], workers: int = 4) -> dict[str, str]:
    """Reverse DNS for each IP, using the PC's normal DNS server. Unresolved IPs are skipped."""

    def one(ip: str) -> tuple[str, str | None]:
        try:
            return ip, socket.gethostbyaddr(ip)[0].split(".")[0]
        except OSError:
            return ip, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return {ip: name for ip, name in pool.map(one, sorted(set(ips))) if name}


def build_topology(run: DiscoveryRun, vendors: VendorLookup | None = None,
                   resolve: Callable[[Iterable[str]], dict[str, str]] | None = None,
                   source: str | None = None) -> Topology:
    """Build the topology. ``resolve``, if given, maps IPs to names (e.g. lookup_names)."""
    ips = {h.ip for h in run.hosts if h.ip} | {a.ip for d in run.devices for a in d.arp}
    names = resolve(ips) if resolve else {}
    b = _Builder(run, vendors or VendorLookup(), names)
    b.add_managed_devices()
    b.add_neighbor_links()
    b.classify_managed()
    b.infer_switch_links_from_mac_tables()
    b.place_endpoints()
    b.finish()
    order = {ROUTER: 0, SWITCH: 1, NETWORK_DEVICE: 2, UNMANAGED_SWITCH: 3, ACCESS_POINT: 4,
             PHONE: 5, VM_HOST: 6, HOST: 7, UNKNOWN_SEGMENT: 8}
    nodes = sorted(b.nodes.values(), key=lambda n: (order.get(n.kind, 9), n.label.lower()))
    return Topology(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    source_run=source, nodes=nodes, links=list(b.links.values()),
                    notes=b.notes)


def root_priority(node: Node, degree: int) -> tuple:
    """Sort key for where to start drawing: a router, then queried switches, then the rest."""
    return (node.kind != ROUTER, not node.managed, node.kind == HOST,
            node.kind == UNKNOWN_SEGMENT, -degree, node.label.lower())


def render_tree(topo: Topology) -> str:
    """A plain-text outline of the topology, starting from the router (or busiest device)."""
    adj: dict[str, list[tuple[str, Link]]] = defaultdict(list)
    for link in topo.links:
        adj[link.source].append((link.target, link))
        adj[link.target].append((link.source, link))
    by_id = {n.id: n for n in topo.nodes}

    def describe(n: Node) -> str:
        bits = [n.kind]
        bits += [x for x in (n.ip, n.mac, n.vendor) if x and x != n.label]
        return f"{n.label} ({', '.join(bits)})"

    lines: list[str] = []
    seen: set[str] = set()

    def walk(node_id: str, depth: int, via: Link | None) -> None:
        seen.add(node_id)
        node = by_id[node_id]
        prefix = "    " * depth
        if via is None:
            lines.append(f"{prefix}{describe(node)}")
        else:
            if via.target == node_id:
                parent_port, child_port = via.source_port, via.target_port
            else:
                parent_port, child_port = via.target_port, via.source_port
            port = f"[{parent_port}] " if parent_port else ""
            if child_port and not child_port.lower().startswith("port"):
                child_port = f"port {child_port}"
            remote = f" {child_port}" if child_port else ""
            guess = " ~" if via.inferred else ""
            lines.append(f"{prefix}{port}->{guess} {describe(node)}{remote}")
        children = sorted(adj[node_id], key=lambda c: (by_id[c[0]].kind == HOST,
                                                          by_id[c[0]].label.lower()))
        for child, link in children:
            if child not in seen:
                walk(child, depth + 1, link)

    roots = sorted(topo.nodes, key=lambda n: root_priority(n, len(adj[n.id])))
    for root in roots:
        if root.id not in seen:
            walk(root.id, 0, None)
            lines.append("")
    if any(lk.inferred for lk in topo.links):
        lines.append("~ = inferred from MAC tables rather than reported by LLDP/CDP")
    return "\n".join(lines).rstrip() + "\n"
