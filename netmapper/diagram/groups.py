"""Decide which group (site, floor, room) each device is drawn in.

In order of precedence:

1. The first ``[[diagram.groups]]`` rule that matches the device, by subnet, IP address
   or name (wildcards allowed).
2. The SNMP location (sysLocation) of a managed switch or router, if turned on.
3. Otherwise the group of the device it connects through, so listing the switch for
   each floor is usually enough to put everything on that floor in its frame.

Devices that end up in no group are drawn outside any frame.
"""

from __future__ import annotations

import ipaddress
from fnmatch import fnmatchcase

from ..config import GroupRule
from ..topology import Node, Topology
from .layout import _Tree


def _matches(rule: GroupRule, node: Node) -> bool:
    for member in rule.members:
        if isinstance(member, ipaddress.IPv4Network):
            try:
                if node.ip and ipaddress.IPv4Address(node.ip) in member:
                    return True
            except ValueError:
                continue
        elif any(fnmatchcase(value.lower(), member)
                 for value in (node.label, node.ip, node.mac) if value):
            return True
    return False


def assign_groups(topo: Topology, rules: list[GroupRule] | None = None,
                  locations: dict[str, str] | None = None) -> dict[str, str]:
    """Map node ids to group names. ``locations`` maps node ids to an SNMP location."""
    rules = rules or []
    locations = {k: v.strip() for k, v in (locations or {}).items() if v and v.strip()}
    if not rules and not locations:
        return {}
    tree = _Tree(topo)
    groups: dict[str, str] = {}
    for node_id in tree.order:  # parents before children
        node = tree.by_id[node_id]
        rule = next((r for r in rules if _matches(r, node)), None)
        if rule:
            groups[node_id] = rule.name
        elif node_id in locations:
            groups[node_id] = locations[node_id]
        elif tree.parent.get(node_id) in groups:
            groups[node_id] = groups[tree.parent[node_id]]
    return groups
