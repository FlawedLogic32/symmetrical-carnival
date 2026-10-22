"""A small line icon per node kind, drawn the same way in every output format.

Each icon is a list of simple shapes on a 24x24 grid (y pointing down): polylines and
circles. That's all SVG, draw.io and Visio need to draw them natively, so the icons
stay sharp and can be recoloured or resized like any other shape.
"""

from __future__ import annotations

import base64
import math

# ("line", [(x, y), ...], closed) or ("circle", cx, cy, r)
Primitive = tuple

GRID = 24


def _rect(x1: float, y1: float, x2: float, y2: float) -> Primitive:
    return ("line", [(x1, y1), (x2, y1), (x2, y2), (x1, y2)], True)


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 10) -> Primitive:
    """An arc as a polyline. Angles in degrees, 0 = right, increasing clockwise (y down)."""
    pts = []
    for i in range(steps + 1):
        a = math.radians(start + (end - start) * i / steps)
        pts.append((round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)))
    return ("line", pts, False)


def _arrow(x1: float, y1: float, x2: float, y2: float, head: float = 2.5) -> list[Primitive]:
    """A line from (x1, y1) with an arrow head at (x2, y2)."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    left = (x2 - head * (ux + uy), y2 - head * (uy - ux))
    right = (x2 - head * (ux - uy), y2 - head * (uy + ux))
    return [("line", [(x1, y1), (x2, y2)], False), ("line", [left, (x2, y2), right], False)]


ICONS: dict[str, list[Primitive]] = {
    # A circle with four arrows, the usual router symbol.
    "router": [("circle", 12, 12, 10), *_arrow(12, 12, 12, 4.5), *_arrow(12, 12, 12, 19.5),
               *_arrow(4.5, 12, 9.5, 12), *_arrow(19.5, 12, 14.5, 12)],
    # A box with traffic flowing both ways.
    "switch": [_rect(2, 6, 22, 18), *_arrow(6, 10, 18, 10), *_arrow(18, 14, 6, 14)],
    # A plain box with a row of ports.
    "unmanaged-switch": [_rect(2, 7, 22, 17)] + [_rect(x, 11, x + 2.5, 13.5)
                                                 for x in (5, 9, 13, 17)],
    # Wi-Fi waves over a base.
    "access-point": [_rect(5, 17, 19, 21), ("circle", 12, 13.5, 1),
                     _arc(12, 13.5, 4, 225, 315), _arc(12, 13.5, 7.5, 225, 315),
                     _arc(12, 13.5, 11, 230, 310)],
    # A desk phone: handset over a keypad.
    "phone": [("line", [(3, 9), (3, 5), (21, 5), (21, 9), (17, 9), (17, 7), (7, 7), (7, 9)], True),
              _rect(5, 11, 19, 21), ("line", [(8, 14), (16, 14)], False),
              ("line", [(8, 17), (16, 17)], False)],
    # Rack-mounted gear.
    "network-device": [_rect(2, 5, 22, 19), ("line", [(2, 12), (22, 12)], False),
                       ("circle", 5.5, 8.5, 1), ("circle", 5.5, 15.5, 1),
                       ("line", [(10, 8.5), (19, 8.5)], False),
                       ("line", [(10, 15.5), (19, 15.5)], False)],
    # A computer screen on a stand.
    "host": [_rect(3, 4, 21, 16), ("line", [(12, 16), (12, 20)], False),
             ("line", [(7, 20.5), (17, 20.5)], False)],
    # A server holding several virtual machines.
    "vm-host": [_rect(3, 3, 21, 21), _rect(6, 6, 18, 9.5), _rect(6, 10.5, 18, 14),
                _rect(6, 15, 18, 18.5)],
    # A question mark in a circle.
    "unknown-segment": [("circle", 12, 12, 10), _arc(12, 9.5, 3, 180, 405, 12),
                        ("line", [(14.12, 11.62), (12, 13), (12, 14.5)], False),
                        ("circle", 12, 17.5, 0.6)],
}


def primitives(kind: str) -> list[Primitive]:
    return ICONS.get(kind, ICONS["host"])


def svg_elements(kind: str, x: float, y: float, size: float, color: str,
                 stroke_width: float = 1.6) -> str:
    """The icon as SVG elements, placed with its top-left corner at (x, y)."""
    s = size / GRID
    out = [f'<g class="icon" transform="translate({x:.1f},{y:.1f}) scale({s:.4f})" fill="none" '
           f'stroke="{color}" stroke-width="{stroke_width}" stroke-linecap="round" '
           f'stroke-linejoin="round">']
    for p in primitives(kind):
        if p[0] == "circle":
            out.append(f'<circle cx="{p[1]}" cy="{p[2]}" r="{p[3]}"/>')
        else:
            pts = " ".join(f"{px:g},{py:g}" for px, py in p[1])
            out.append(f'<{"polygon" if p[2] else "polyline"} points="{pts}"/>')
    out.append("</g>")
    return "".join(out)


def svg_document(kind: str, color: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}" '
            f'width="{GRID}" height="{GRID}">{svg_elements(kind, 0, 0, GRID, color)}</svg>')


def drawio_image(kind: str, color: str) -> str:
    """The icon as draw.io expects it in a style: a data URI with the ';base64' left out,
    because ';' separates style entries."""
    data = base64.b64encode(svg_document(kind, color).encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml,{data}"
