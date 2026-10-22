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
# Hosts have a pale outline, which is too faint for an icon.
ICON_COLORS = {"host": "#64748b"}

# Icon placement inside a box: a gap from the left edge, then the icon, then the text.
ICON_LEFT = 14
ICON_GAP = 10

# Frames drawn around a group of devices (a site, floor or room).
FRAME_FILL, FRAME_STROKE, FRAME_TEXT = "#f8fafc", "#94a3b8", "#334155"


def colors(node: Node) -> tuple[str, str]:
    return KIND_COLORS.get(node.kind, KIND_COLORS["host"])


def icon_color(node: Node) -> str:
    return ICON_COLORS.get(node.kind, colors(node)[1])


def icon_size(leaf: bool) -> float:
    return 22 if leaf else 26


def text_area(x: float, w: float, leaf: bool) -> tuple[float, float]:
    """Left edge and width of the space beside the icon for a box's text."""
    left = x + ICON_LEFT + icon_size(leaf) + ICON_GAP
    return left, x + w - 8 - left


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
    lines = lines[:3]
    return [truncate(lines[0], 22)] + [truncate(line, 26) for line in lines[1:]]
