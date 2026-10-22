"""Colours and wording per node kind, shared by every output format."""

from __future__ import annotations

from ..topology import Node

KIND_NAMES = {
    "router": "Router",
    "switch": "Switch",
    "unmanaged-switch": "Unmanaged switch (inferred)",
    "access-point": "Access point",
    "phone": "Phone",
    "network-device": "Network device",
    "host": "Device",
    "unknown-segment": "Location unknown",
}

# fill, stroke
KIND_COLORS = {
    "router": ("#dbeafe", "#2563eb"),
    "switch": ("#dcfce7", "#16a34a"),
    "unmanaged-switch": ("#f1f5f9", "#64748b"),
    "access-point": ("#fef3c7", "#d97706"),
    "phone": ("#ede9fe", "#7c3aed"),
    "network-device": ("#e0f2fe", "#0284c7"),
    "host": ("#ffffff", "#94a3b8"),
    "unknown-segment": ("#fee2e2", "#dc2626"),
}
NEW_STROKE = "#059669"


def colors(node: Node) -> tuple[str, str]:
    return KIND_COLORS.get(node.kind, KIND_COLORS["host"])


def truncate(text: str, limit: int = 26) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def box_lines(node: Node, leaf: bool, parent_port: str | None) -> list[str]:
    """Text shown inside a node's box: the label first, then up to two detail lines."""
    if node.kind == "unmanaged-switch":
        return ["Unmanaged switch", "inferred from MAC tables"]
    if node.kind == "unknown-segment":
        return [node.label, "not seen on any switch port"]
    lines = [node.label]
    if leaf and node.kind == "host":
        detail = [x for x in (node.ip, node.mac) if x and x != node.label]
        if detail:
            lines.append(detail[0])
        tail = [node.vendor] if node.vendor else []
        if parent_port:
            tail.insert(0, f"port {parent_port}")
        if tail:
            lines.append(" · ".join(tail))
    else:
        kind = KIND_NAMES.get(node.kind, node.kind)
        lines.append(f"{kind} · {node.vendor}" if node.vendor else kind)
        detail = [x for x in (node.ip, node.mac) if x and x != node.label]
        if detail:
            lines.append(detail[0])
    return [truncate(line) for line in lines[:3]]
