"""Write a .drawio file (open with draw.io / diagrams.net, desktop or web, or import into Visio)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from xml.etree import ElementTree as ET

from ..topology import Topology
from . import icons
from .layout import Box, Layout, compute_layout
from .style import (FRAME_FILL, FRAME_STROKE, FRAME_TEXT, ICON_GAP, ICON_LEFT, NEW_STROKE,
                    box_lines, colors, icon_color, icon_size)


def _vertex_style(box: Box, new: bool) -> str:
    # draw.io's "label" shape is a box with an image on the left, which is exactly this.
    fill, stroke = colors(box.node)
    size = icon_size(box.leaf)
    parts = ["shape=label", "rounded=1", "whiteSpace=wrap", "html=1", "arcSize=12", "align=left",
             "verticalAlign=middle", "fontSize=11", f"fillColor={fill}",
             f"strokeColor={NEW_STROKE if new else stroke}", f"strokeWidth={3 if new else 1.5}",
             f"image={icons.drawio_image(box.node.kind, icon_color(box.node))}",
             f"imageWidth={size}", f"imageHeight={size}", "imageAlign=left",
             "imageVerticalAlign=middle", f"spacingLeft={ICON_LEFT + size + ICON_GAP - 4}",
             "spacing=4"]
    if box.node.inferred:
        parts.append("dashed=1")
    return ";".join(parts) + ";"


def _frame_style() -> str:
    # A container, so moving the frame in draw.io moves the devices in it.
    return ";".join(["rounded=1", "arcSize=4", "html=1", "container=1", "collapsible=0",
                     f"fillColor={FRAME_FILL}", f"strokeColor={FRAME_STROKE}", "dashed=1",
                     "strokeWidth=1.5", "verticalAlign=top", "align=left", "spacingLeft=12",
                     "spacingTop=4", "fontStyle=1", "fontSize=14", f"fontColor={FRAME_TEXT}"]) + ";"


def to_drawio(topo: Topology, layout: Layout | None = None,
              new_ids: set[str] | None = None) -> str:
    layout = layout or compute_layout(topo)
    new_ids = new_ids or set()
    mxfile = ET.Element("mxfile", host="netmapper",
                        modified=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    diagram = ET.SubElement(mxfile, "diagram", id="network", name="Network")
    model = ET.SubElement(diagram, "mxGraphModel", dx="1200", dy="800", grid="1",
                          gridSize="10", guides="1", tooltips="1", connect="1", arrows="1",
                          fold="1", page="1", pageScale="1",
                          pageWidth=str(int(layout.width)), pageHeight=str(int(layout.height)))
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    frames = {}
    for k, f in enumerate(layout.frames):
        frames[f.name] = (f"g{k}", f)
        cell = ET.SubElement(root, "mxCell", id=f"g{k}", value=escape(f.name), vertex="1",
                             parent="1", style=_frame_style())
        ET.SubElement(cell, "mxGeometry", x=f"{f.x:.0f}", y=f"{f.y:.0f}", width=f"{f.w:.0f}",
                      height=f"{f.h:.0f}", **{"as": "geometry"})

    cell_ids: dict[str, str] = {}
    for i, (node_id, box) in enumerate(layout.boxes.items(), start=2):
        cid = f"n{i}"
        cell_ids[node_id] = cid
        lines = box_lines(box.node, box.leaf, box.parent_port if box.leaf else None)
        value = f"<b>{escape(lines[0])}</b>" + "".join(
            f"<br><font style=\"font-size:10px\">{escape(x)}</font>" for x in lines[1:])
        parent, (x, y) = "1", (box.x, box.y)
        if box.group in frames:  # inside a frame, positions are relative to it
            parent, f = frames[box.group]
            x, y = box.x - f.x, box.y - f.y
        cell = ET.SubElement(root, "mxCell", id=cid, value=value, vertex="1", parent=parent,
                             style=_vertex_style(box, node_id in new_ids))
        ET.SubElement(cell, "mxGeometry", x=f"{x:.0f}", y=f"{y:.0f}",
                      width=f"{box.w:.0f}", height=f"{box.h:.0f}", **{"as": "geometry"})

    for j, edge in enumerate(layout.edges):
        link = edge.link
        style = ["endArrow=none", "html=1", "rounded=0", "fontSize=10",
                 "labelBackgroundColor=#ffffff", "strokeColor=#475569"]
        if link.inferred:
            style.append("dashed=1")
        value = ""
        src, dst = cell_ids[edge.parent], cell_ids[edge.child]
        if edge.tree:
            parent, child = layout.boxes[edge.parent], layout.boxes[edge.child]
            (sx, sy), (ex, ey) = edge.points[0], edge.points[-1]
            style += [f"exitX={(sx - parent.x) / parent.w:g}",
                      f"exitY={(sy - parent.y) / parent.h:g}", "exitDx=0", "exitDy=0", f"entryX={(ex - child.x) / child.w:g}",
                      f"entryY={(ey - child.y) / child.h:g}"]
            if not child.leaf:
                value = " ↔ ".join(p for p in (edge.parent_port, edge.child_port) if p)
        else:
            style.append("strokeColor=#9333ea")
            value = " ↔ ".join(p for p in (edge.parent_port, edge.child_port) if p)
        cell = ET.SubElement(root, "mxCell", id=f"e{j}", value=escape(value), edge="1",
                             parent="1", source=src, target=dst, style=";".join(style) + ";")
        geo = ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
        if edge.tree and len(edge.points) > 2:
            pts = ET.SubElement(geo, "Array", **{"as": "points"})
            for x, y in edge.points[1:-1]:
                ET.SubElement(pts, "mxPoint", x=f"{x:.0f}", y=f"{y:.0f}")

    ET.indent(mxfile)
    return ET.tostring(mxfile, encoding="unicode")
