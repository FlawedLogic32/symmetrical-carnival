"""Render a topology as standalone SVG (used inside the HTML page, also viewable on its own)."""

from __future__ import annotations

from html import escape

from ..topology import Topology
from .layout import Layout, compute_layout
from . import icons
from .style import (FRAME_FILL, FRAME_STROKE, FRAME_TEXT, ICON_LEFT, KIND_NAMES, NEW_STROKE,
                    box_lines, colors, icon_color, icon_size, text_area)


def _attr(value) -> str:
    return escape(str(value), quote=True)


def to_svg(topo: Topology, layout: Layout | None = None, new_ids: set[str] | None = None) -> str:
    layout = layout or compute_layout(topo)
    new_ids = new_ids or set()
    w, h = max(layout.width, 200), max(layout.height, 120)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" class="netmap" viewBox="0 0 {w:.0f} {h:.0f}" '
           f'width="{w:.0f}" height="{h:.0f}" font-family="Segoe UI, system-ui, sans-serif">']

    if layout.frames:
        out.append('<g class="frames">')
        for f in layout.frames:
            out.append(f'<g class="frame"><rect x="{f.x:.1f}" y="{f.y:.1f}" width="{f.w:.0f}" '
                       f'height="{f.h:.0f}" rx="12" fill="{FRAME_FILL}" stroke="{FRAME_STROKE}" '
                       f'stroke-width="1.5" stroke-dasharray="8 5"/>'
                       f'<text class="frame-name" x="{f.x + 16:.1f}" y="{f.y + 22:.1f}" '
                       f'fill="{FRAME_TEXT}">{_attr(f.name)}</text></g>')
        out.append("</g>")
    out.append('<g class="links">')

    for edge in layout.edges:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in edge.points)
        cls = "link" + (" inferred" if edge.link.inferred else "") + ("" if edge.tree else " extra")
        title = f"{edge.link.evidence}" + (" (inferred)" if edge.link.inferred else "")
        out.append(f'<polyline class="{cls}" points="{pts}"><title>{_attr(title)}</title></polyline>')
        child = layout.boxes[edge.child]
        if edge.tree and not child.leaf:
            label = " ↔ ".join(p for p in (edge.parent_port, edge.child_port) if p)
            if label:
                out.append(f'<text class="port" x="{child.cx + 6:.1f}" y="{child.y - 8:.1f}">'
                           f'{_attr(label)}</text>')
        elif not edge.tree:
            (x1, y1), (x2, y2) = edge.points[0], edge.points[-1]
            label = " ↔ ".join(p for p in (edge.parent_port, edge.child_port) if p)
            if label:
                out.append(f'<text class="port extra" x="{(x1 + x2) / 2:.1f}" '
                           f'y="{(y1 + y2) / 2 - 4:.1f}" text-anchor="middle">{_attr(label)}</text>')
    out.append("</g><g class=\"nodes\">")

    for node_id, box in layout.boxes.items():
        node = box.node
        fill, stroke = colors(node)
        is_new = node_id in new_ids
        dash = ' stroke-dasharray="6 4"' if node.inferred else ""
        tooltip = "\n".join(x for x in (
            node.label, KIND_NAMES.get(node.kind, node.kind), node.ip, node.mac, node.vendor,
            node.description, "New since the last run" if is_new else None) if x)
        out.append(f'<g class="node kind-{_attr(node.kind)}{" new" if is_new else ""}" '
                   f'data-id="{_attr(node_id)}"><title>{_attr(tooltip)}</title>')
        out.append(f'<rect x="{box.x:.1f}" y="{box.y:.1f}" width="{box.w:.0f}" height="{box.h:.0f}" '
                   f'rx="8" fill="{fill}" stroke="{NEW_STROKE if is_new else stroke}" '
                   f'stroke-width="{3 if is_new else 1.5}"{dash}/>')
        out.append(f'<rect x="{box.x:.1f}" y="{box.y:.1f}" width="6" height="{box.h:.0f}" rx="3" '
                   f'fill="{stroke}"/>')
        size = icon_size(box.leaf)
        out.append(icons.svg_elements(node.kind, box.x + ICON_LEFT, box.y + (box.h - size) / 2,
                                      size, icon_color(node)))
        lines = box_lines(node, box.leaf, box.parent_port if box.leaf else None)
        first_y = box.y + box.h / 2 - (len(lines) - 1) * 7.5 + 4
        text_x, _ = text_area(box.x, box.w, box.leaf)
        for i, line in enumerate(lines):
            cls = "name" if i == 0 else "detail"
            out.append(f'<text class="{cls}" x="{text_x:.1f}" y="{first_y + i * 15:.1f}">'
                       f'{_attr(line)}</text>')
        if is_new:
            out.append(f'<text class="badge" x="{box.x + box.w - 8:.1f}" y="{box.y + 13:.1f}" '
                       f'text-anchor="end">NEW</text>')
        out.append("</g>")
    out.append("</g>")
    out.append("<style>"
               ".netmap .link{fill:none;stroke:#64748b;stroke-width:1.5}"
               ".netmap .link.inferred{stroke-dasharray:5 4}"
               ".netmap .link.extra{stroke:#9333ea}"
               ".netmap .port{font-size:10px;fill:#475569}"
               ".netmap .name{font-size:12.5px;font-weight:600;fill:#0f172a}"
               ".netmap .detail{font-size:10.5px;fill:#475569}"
               ".netmap .badge{font-size:9px;font-weight:700;fill:#059669}"
               ".netmap .frame-name{font-size:14px;font-weight:600}"
               "</style>")
    out.append("</svg>")
    return "\n".join(out)
