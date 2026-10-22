"""Write a .drawio file (open with draw.io / diagrams.net, desktop or web, or import into Visio)."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from xml.etree import ElementTree as ET

from ..topology import Topology
from .layout import Layout, compute_layout
from .style import NEW_STROKE, box_lines, colors


def _vertex_style(fill: str, stroke: str, dashed: bool, new: bool) -> str:
    parts = ["rounded=1", "whiteSpace=wrap", "html=1", "arcSize=12", "align=center",
             "verticalAlign=middle", "fontSize=11", f"fillColor={fill}",
             f"strokeColor={NEW_STROKE if new else stroke}",
             f"strokeWidth={3 if new else 1.5}"]
    if dashed:
        parts.append("dashed=1")
    return ";".join(parts) + ";"


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

    cell_ids: dict[str, str] = {}
    for i, (node_id, box) in enumerate(layout.boxes.items(), start=2):
        cid = f"n{i}"
        cell_ids[node_id] = cid
        lines = box_lines(box.node, box.leaf, box.parent_port if box.leaf else None)
        value = f"<b>{escape(lines[0])}</b>" + "".join(
            f"<br><font style=\"font-size:10px\">{escape(x)}</font>" for x in lines[1:])
        fill, stroke = colors(box.node)
        cell = ET.SubElement(root, "mxCell", id=cid, value=value, vertex="1", parent="1",
                             style=_vertex_style(fill, stroke, box.node.inferred,
                                                 node_id in new_ids))
        ET.SubElement(cell, "mxGeometry", x=f"{box.x:.0f}", y=f"{box.y:.0f}",
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
            leaf = layout.boxes[edge.child].leaf
            style += ["exitX=0.5", "exitY=1", "exitDx=0", "exitDy=0"]
            style += ["entryX=0", "entryY=0.5"] if leaf else ["entryX=0.5", "entryY=0"]
            if not leaf:
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
