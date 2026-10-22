"""Write a Visio drawing (.vsdx) with no Visio or other library needed.

A .vsdx file is a zip of XML parts. This writes the smallest set Visio needs: one page
with a shape per device (a box plus its icon, grouped), a dynamic connector per link
glued to both devices, and a dashed frame per group. Because the connectors are glued,
devices can be moved around in Visio and the links follow.

Visio measures in inches from the bottom-left corner of the page, so layout pixels are
converted at 96 per inch and the y axis is flipped.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from ..topology import Topology
from . import icons
from .layout import Box, Layout, compute_layout
from .style import (FRAME_FILL, FRAME_STROKE, FRAME_TEXT, ICON_LEFT, NEW_STROKE, box_lines,
                    colors, icon_color, icon_size, text_area)

PX_PER_INCH = 96
NS = "http://schemas.microsoft.com/office/visio/2012/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_VISIO = "http://schemas.microsoft.com/visio/2010/relationships"
REL_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
FONT = "Segoe UI"
XML_HEAD = "<?xml version='1.0' encoding='utf-8' ?>\n"


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _a(value) -> str:
    return escape(str(value), {"'": "&apos;", '"': "&quot;"})


def _cell(name: str, value, formula: str | None = None, unit: str | None = None) -> str:
    v = _num(value) if isinstance(value, (int, float)) else _a(value)
    out = f"<Cell N='{name}' V='{v}'"
    if unit:
        out += f" U='{unit}'"
    if formula:
        out += f" F='{_a(formula)}'"
    return out + "/>"


def _inch(px: float) -> float:
    return px / PX_PER_INCH


def _pt(points: float) -> float:
    return points / 72


class _Page:
    def __init__(self, layout: Layout):
        self.height = _inch(layout.height)
        self.next_id = 1

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def x(self, px: float) -> float:
        return _inch(px)

    def y(self, px: float) -> float:
        return self.height - _inch(px)


def _rect_geometry(no_fill: bool = False) -> str:
    rows = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    out = [f"<Section N='Geometry' IX='0'>{_cell('NoFill', int(no_fill))}{_cell('NoLine', 0)}"
           f"{_cell('NoShow', 0)}{_cell('NoSnap', 0)}"]
    for i, (x, y) in enumerate(rows, start=1):
        kind = "RelMoveTo" if i == 1 else "RelLineTo"
        out.append(f"<Row T='{kind}' IX='{i}'>{_cell('X', x)}{_cell('Y', y)}</Row>")
    out.append("</Section>")
    return "".join(out)


def _text_cells(left: float, width: float, height: float, align: int, valign: int) -> str:
    """A text block ``width`` wide starting ``left`` inches from the shape's left edge."""
    return (_cell("TxtPinX", left + width / 2, f"{_num(left)}+TxtWidth*0.5")
            + _cell("TxtPinY", height / 2, "Height*0.5")
            + _cell("TxtWidth", width, f"Width-{_num(left)}-{_num(_inch(8))}")
            + _cell("TxtHeight", height, "Height*1")
            + _cell("TxtLocPinX", width / 2, "TxtWidth*0.5")
            + _cell("TxtLocPinY", height / 2, "TxtHeight*0.5")
            + _cell("LeftMargin", 0) + _cell("RightMargin", 0)
            + _cell("TopMargin", _pt(2)) + _cell("BottomMargin", _pt(2))
            + _cell("VerticalAlign", valign)
            + f"<Section N='Paragraph'><Row IX='0'>{_cell('HorzAlign', align)}</Row></Section>")


def _char_section(rows: list[tuple[float, int, str]]) -> str:
    out = ["<Section N='Character'>"]
    for i, (size, bold, color) in enumerate(rows):
        out.append(f"<Row IX='{i}'>{_cell('Font', FONT)}{_cell('Color', color)}"
                   f"{_cell('Style', bold)}{_cell('Size', _pt(size), unit='PT')}</Row>")
    out.append("</Section>")
    return "".join(out)


def _icon_shape(sid: int, kind: str, color: str, pin_x: float, pin_y: float, size: float) -> str:
    """The icon as a shape of its own, with one geometry section per stroke."""
    s = size / icons.GRID
    out = [f"<Shape ID='{sid}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>",
           _cell("PinX", pin_x), _cell("PinY", pin_y), _cell("Width", size), _cell("Height", size),
           _cell("LocPinX", size / 2, "Width*0.5"), _cell("LocPinY", size / 2, "Height*0.5"),
           _cell("LineColor", color), _cell("LineWeight", _inch(1.6) * size / _inch(icons.GRID)),
           _cell("LineCap", 0), _cell("FillPattern", 0), _cell("LockTextEdit", 1)]
    for ix, prim in enumerate(icons.primitives(kind)):
        out.append(f"<Section N='Geometry' IX='{ix}'>{_cell('NoFill', 1)}{_cell('NoLine', 0)}"
                   f"{_cell('NoShow', 0)}{_cell('NoSnap', 0)}")
        if prim[0] == "circle":
            _, cx, cy, r = prim
            x, y = cx * s, size - cy * s
            out.append(f"<Row T='Ellipse' IX='1'>{_cell('X', x)}{_cell('Y', y)}"
                       f"{_cell('A', x + r * s)}{_cell('B', y)}{_cell('C', x)}"
                       f"{_cell('D', y + r * s)}</Row>")
        else:
            _, pts, closed = prim
            pts = list(pts) + ([pts[0]] if closed else [])
            for i, (px, py) in enumerate(pts, start=1):
                kind_ = "MoveTo" if i == 1 else "LineTo"
                fx, fy = px / icons.GRID, 1 - py / icons.GRID
                out.append(f"<Row T='{kind_}' IX='{i}'>"
                           f"{_cell('X', fx * size, f'Width*{_num(fx)}')}"
                           f"{_cell('Y', fy * size, f'Height*{_num(fy)}')}</Row>")
        out.append("</Section>")
    out.append("</Shape>")
    return "".join(out)


def _device_shape(page: _Page, box: Box, new: bool) -> tuple[int, str]:
    """A group holding the box (with the text) and the icon. Connectors glue to the group."""
    node = box.node
    fill, stroke = colors(node)
    w, h = _inch(box.w), _inch(box.h)
    gid, bid, iid = page.new_id(), page.new_id(), page.new_id()
    lines = box_lines(node, box.leaf, box.parent_port if box.leaf else None)
    text_x, text_w = text_area(0, box.w, box.leaf)
    size = icon_size(box.leaf)

    group = [f"<Shape ID='{gid}' Type='Group' LineStyle='0' FillStyle='0' TextStyle='0'>",
             _cell("PinX", page.x(box.x) + w / 2), _cell("PinY", page.y(box.y) - h / 2),
             _cell("Width", w), _cell("Height", h),
             _cell("LocPinX", w / 2, "Width*0.5"), _cell("LocPinY", h / 2, "Height*0.5"),
             _cell("SelectMode", 0),  # clicking selects the device, not the box or icon inside
             _shape_data(node), "<Shapes>"]

    body = [f"<Shape ID='{bid}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>",
            _cell("PinX", w / 2, "Sheet.%d!Width*0.5" % gid),
            _cell("PinY", h / 2, "Sheet.%d!Height*0.5" % gid),
            _cell("Width", w, "Sheet.%d!Width*1" % gid),
            _cell("Height", h, "Sheet.%d!Height*1" % gid),
            _cell("LocPinX", w / 2, "Width*0.5"), _cell("LocPinY", h / 2, "Height*0.5"),
            _cell("LineColor", NEW_STROKE if new else stroke),
            _cell("LineWeight", _inch(3 if new else 1.5)),
            _cell("LinePattern", 2 if node.inferred else 1),
            _cell("FillForegnd", fill), _cell("FillPattern", 1),
            _cell("Rounding", _inch(8)),
            _text_cells(_inch(text_x), _inch(text_w), h, 0, 1),
            _char_section([(9, 1, "#0f172a"), (7.5, 0, "#475569")]),
            _rect_geometry()]
    text = "".join(f"<cp IX='{0 if i == 0 else 1}'/>{escape(line)}" + ("\n" if i < len(lines) - 1
                                                                         else "")
                   for i, line in enumerate(lines))
    body.append(f"<Text>{text}</Text></Shape>")
    group += body
    group.append(_icon_shape(iid, node.kind, icon_color(node), _inch(ICON_LEFT + size / 2), h / 2,
                             _inch(size)))
    group.append("</Shapes></Shape>")
    return gid, "".join(group)


def _shape_data(node) -> str:
    """Device details as Visio shape data, shown in Visio's Shape Data pane."""
    rows = [("IP", "IP address", node.ip), ("MAC", "MAC address", node.mac),
            ("Vendor", "Vendor", node.vendor), ("Kind", "Type", node.kind),
            ("Description", "Description", node.description)]
    out = ["<Section N='Property'>"]
    for i, (name, label, value) in enumerate(rows):
        out.append(f"<Row N='{name}'>{_cell('Value', value or '', unit='STR')}"
                   f"{_cell('Label', label)}{_cell('Type', 0)}{_cell('SortKey', str(i))}</Row>")
    out.append("</Section>")
    return "".join(out)


def _frame_shape(page: _Page, frame) -> str:
    w, h = _inch(frame.w), _inch(frame.h)
    return "".join([
        f"<Shape ID='{page.new_id()}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>",
        _cell("PinX", page.x(frame.x) + w / 2), _cell("PinY", page.y(frame.y) - h / 2),
        _cell("Width", w), _cell("Height", h),
        _cell("LocPinX", w / 2, "Width*0.5"), _cell("LocPinY", h / 2, "Height*0.5"),
        _cell("LineColor", FRAME_STROKE), _cell("LineWeight", _inch(1.5)), _cell("LinePattern", 2),
        _cell("FillForegnd", FRAME_FILL), _cell("FillPattern", 1), _cell("Rounding", _inch(12)),
        _text_cells(_inch(16), w - _inch(24), h, 0, 0),
        _char_section([(10.5, 1, FRAME_TEXT)]),
        _rect_geometry(),
        f"<Text><cp IX='0'/>{escape(frame.name)}</Text></Shape>"])


def _connector_shape(page: _Page, sid: int, points: list[tuple[float, float]], begin_id: int,
                     end_id: int, dashed: bool, color: str, label: str, right_angle: bool) -> str:
    pts = [(page.x(x), page.y(y)) for x, y in points]
    (bx, by), (ex, ey) = pts[0], pts[-1]
    w, h = ex - bx, ey - by
    glue = "_WALKGLUE(BegTrigger,EndTrigger,WalkPreference)"
    glue_end = "_WALKGLUE(EndTrigger,BegTrigger,WalkPreference)"
    # Put the port label near the device it describes: the far end of the line.
    tx, ty = (w, h + _pt(9)) if right_angle else (w / 2, h / 2)
    out = [f"<Shape ID='{sid}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>",
           _cell("PinX", bx + w / 2, "GUARD((BeginX+EndX)/2)"),
           _cell("PinY", by + h / 2, "GUARD((BeginY+EndY)/2)"),
           _cell("Width", w, "GUARD(EndX-BeginX)"), _cell("Height", h, "GUARD(EndY-BeginY)"),
           _cell("LocPinX", w / 2, "GUARD(Width*0.5)"),
           _cell("LocPinY", h / 2, "GUARD(Height*0.5)"),
           _cell("BeginX", bx, glue), _cell("BeginY", by, glue),
           _cell("EndX", ex, glue_end), _cell("EndY", ey, glue_end),
           _cell("TxtPinX", tx), _cell("TxtPinY", ty),
           _cell("TxtWidth", 1.2), _cell("TxtHeight", _pt(12)),
           _cell("TxtLocPinX", 0.6, "TxtWidth*0.5"), _cell("TxtLocPinY", _pt(6), "TxtHeight*0.5"),
           _cell("LineColor", color), _cell("LineWeight", _inch(1.5)),
           _cell("LinePattern", 2 if dashed else 1), _cell("BeginArrow", 0), _cell("EndArrow", 0),
           _cell("NoAlignBox", 1), _cell("DynFeedback", 2), _cell("GlueType", 2),
           _cell("ObjType", 2), _cell("NoLiveDynamics", 1),
           _cell("BegTrigger", 2, f"_XFTRIGGER(Sheet.{begin_id}!EventXFMod)"),
           _cell("EndTrigger", 2, f"_XFTRIGGER(Sheet.{end_id}!EventXFMod)"),
           _cell("ShapeRouteStyle", 1 if right_angle else 2), _cell("ConFixedCode", 1),
           _char_section([(7.5, 0, "#475569")]),
           f"<Section N='Geometry' IX='0'>{_cell('NoFill', 1)}{_cell('NoLine', 0)}"
           f"{_cell('NoShow', 0)}{_cell('NoSnap', 0)}"]
    for i, (x, y) in enumerate(pts, start=1):
        kind = "MoveTo" if i == 1 else "LineTo"
        out.append(f"<Row T='{kind}' IX='{i}'>{_cell('X', x - bx)}{_cell('Y', y - by)}</Row>")
    out.append("</Section>")
    if label:
        out.append(f"<Text>{escape(label)}</Text>")
    out.append("</Shape>")
    return "".join(out)


def _page_xml(topo: Topology, layout: Layout, new_ids: set[str]) -> tuple[str, float, float]:
    page = _Page(layout)
    shapes, connects = [], []
    for frame in layout.frames:  # first, so they sit behind everything else
        shapes.append(_frame_shape(page, frame))
    edge_ids = [page.new_id() for _ in layout.edges]  # connectors behind the devices
    device_xml, sheet = [], {}
    for node_id, box in layout.boxes.items():
        sid, xml = _device_shape(page, box, node_id in new_ids)
        sheet[node_id] = sid
        device_xml.append(xml)
    for sid, edge in zip(edge_ids, layout.edges):
        a, b = sheet[edge.parent], sheet[edge.child]
        if edge.tree:
            leaf = layout.boxes[edge.child].leaf
            label = "" if leaf else " ↔ ".join(p for p in (edge.parent_port, edge.child_port) if p)
            color = "#475569"
        else:
            label = " ↔ ".join(p for p in (edge.parent_port, edge.child_port) if p)
            color = "#9333ea"
        shapes.append(_connector_shape(page, sid, edge.points, a, b, edge.link.inferred, color,
                                       label, edge.tree))
        connects.append(f"<Connect FromSheet='{sid}' FromCell='BeginX' FromPart='9' "
                        f"ToSheet='{a}' ToCell='PinX' ToPart='3'/>")
        connects.append(f"<Connect FromSheet='{sid}' FromCell='EndX' FromPart='12' "
                        f"ToSheet='{b}' ToCell='PinX' ToPart='3'/>")
    shapes += device_xml
    xml = (XML_HEAD + f"<PageContents xmlns='{NS}' xmlns:r='{NS_R}' xml:space='preserve'>"
           f"<Shapes>{''.join(shapes)}</Shapes>"
           + (f"<Connects>{''.join(connects)}</Connects>" if connects else "")
           + "</PageContents>")
    return xml, _inch(layout.width), page.height


def _document_xml() -> str:
    style = "".join([
        _cell("EnableLineProps", 1), _cell("EnableFillProps", 1), _cell("EnableTextProps", 1),
        _cell("HideForApply", 0), _cell("LineWeight", _inch(1)), _cell("LineColor", 0),
        _cell("LinePattern", 1), _cell("Rounding", 0), _cell("EndArrowSize", 2),
        _cell("BeginArrow", 0), _cell("EndArrow", 0), _cell("LineCap", 0),
        _cell("BeginArrowSize", 2), _cell("FillForegnd", 1), _cell("FillBkgnd", 0),
        _cell("FillPattern", 1), _cell("ShdwForegnd", 0), _cell("ShdwPattern", 0),
        _cell("LeftMargin", 0), _cell("RightMargin", 0), _cell("TopMargin", 0),
        _cell("BottomMargin", 0), _cell("VerticalAlign", 1), _cell("TextBkgnd", 0),
        _cell("TextDirection", 0),
        f"<Section N='Character'><Row IX='0'>{_cell('Font', FONT)}{_cell('Color', 0)}"
        f"{_cell('Style', 0)}{_cell('Size', _pt(10), unit='PT')}</Row></Section>",
        f"<Section N='Paragraph'><Row IX='0'>{_cell('HorzAlign', 1)}</Row></Section>"])
    return (XML_HEAD + f"<VisioDocument xmlns='{NS}' xmlns:r='{NS_R}' xml:space='preserve'>"
            "<DocumentSettings TopPage='0' DefaultTextStyle='0' DefaultLineStyle='0' "
            "DefaultFillStyle='0' DefaultGuideStyle='0'><GlueSettings>9</GlueSettings>"
            "<SnapSettings>65847</SnapSettings><SnapExtensions>34</SnapExtensions>"
            "<DynamicGridEnabled>1</DynamicGridEnabled><ProtectStyles>0</ProtectStyles>"
            "<ProtectShapes>0</ProtectShapes><ProtectMasters>0</ProtectMasters>"
            "<ProtectBkgnds>0</ProtectBkgnds></DocumentSettings>"
            f"<FaceNames><FaceName NameU='{FONT}'/></FaceNames>"
            "<StyleSheets><StyleSheet ID='0' NameU='No Style' IsCustomNameU='1' Name='No Style' "
            f"IsCustomName='1'>{style}</StyleSheet></StyleSheets></VisioDocument>")


def _pages_xml(width: float, height: float) -> str:
    return (XML_HEAD + f"<Pages xmlns='{NS}' xmlns:r='{NS_R}' xml:space='preserve'>"
            "<Page ID='0' NameU='Network' Name='Network'><PageSheet LineStyle='0' FillStyle='0' "
            f"TextStyle='0'>{_cell('PageWidth', width)}{_cell('PageHeight', height)}"
            f"{_cell('PageScale', 1, unit='IN')}{_cell('DrawingScale', 1, unit='IN')}"
            f"{_cell('DrawingSizeType', 3)}{_cell('DrawingScaleType', 0)}"
            f"{_cell('InhibitSnap', 0)}{_cell('PrintPageOrientation', 2 if width > height else 1)}"
            f"{_cell('PageLeftMargin', 0.25)}{_cell('PageRightMargin', 0.25)}"
            f"{_cell('PageTopMargin', 0.25)}{_cell('PageBottomMargin', 0.25)}"
            f"{_cell('RouteStyle', 1)}</PageSheet><Rel r:id='rId1'/></Page></Pages>")


def _rels(*rels: tuple[str, str, str]) -> str:
    body = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{target}"/>'
                   for i, t, target in rels)
    return (XML_HEAD + f'<Relationships xmlns="{REL_PACKAGE}">{body}</Relationships>')


def _content_types() -> str:
    overrides = [("/visio/document.xml", "application/vnd.ms-visio.drawing.main+xml"),
                 ("/visio/pages/pages.xml", "application/vnd.ms-visio.pages+xml"),
                 ("/visio/pages/page1.xml", "application/vnd.ms-visio.page+xml"),
                 ("/visio/windows.xml", "application/vnd.ms-visio.windows+xml"),
                 ("/docProps/core.xml",
                  "application/vnd.openxmlformats-package.core-properties+xml"),
                 ("/docProps/app.xml",
                  "application/vnd.openxmlformats-officedocument.extended-properties+xml")]
    body = "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides)
    return (XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f"{body}</Types>")


def _windows_xml(width: float, height: float) -> str:
    return (XML_HEAD + f"<Windows ClientWidth='1600' ClientHeight='900' xmlns='{NS}' "
            f"xmlns:r='{NS_R}' xml:space='preserve'><Window ID='0' WindowType='Drawing' "
            "WindowState='1073741824' WindowLeft='0' WindowTop='0' WindowWidth='1600' "
            f"WindowHeight='900' ContainerType='Page' Page='0' ViewScale='-1' "
            f"ViewCenterX='{_num(width / 2)}' ViewCenterY='{_num(height / 2)}'>"
            "<ShowRulers>1</ShowRulers><ShowGrid>0</ShowGrid><ShowPageBreaks>0</ShowPageBreaks>"
            "<ShowGuides>1</ShowGuides><ShowConnectionPoints>1</ShowConnectionPoints>"
            "<GlueSettings>9</GlueSettings><SnapSettings>65847</SnapSettings>"
            "<SnapExtensions>34</SnapExtensions><DynamicGridEnabled>1</DynamicGridEnabled>"
            "<TabSplitterPos>0.5</TabSplitterPos></Window></Windows>")


def _core_xml(title: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (XML_HEAD + '<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>netmapper</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
            "</cp:coreProperties>")


def _app_xml() -> str:
    return (XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
            'extended-properties"><Application>netmapper</Application><Template/>'
            "</Properties>")


def to_vsdx(topo: Topology, layout: Layout | None = None, new_ids: set[str] | None = None,
            title: str = "Network map") -> bytes:
    layout = layout or compute_layout(topo)
    page, width, height = _page_xml(topo, layout, new_ids or set())
    parts = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _rels(("rId1", f"{REL_VISIO}/document", "visio/document.xml"),
                             ("rId2", f"{REL_PACKAGE}/metadata/core-properties",
                              "docProps/core.xml"),
                             ("rId3", f"{REL_OFFICE}/extended-properties", "docProps/app.xml")),
        "docProps/core.xml": _core_xml(title),
        "docProps/app.xml": _app_xml(),
        "visio/document.xml": _document_xml(),
        "visio/_rels/document.xml.rels": _rels(("rId1", f"{REL_VISIO}/pages", "pages/pages.xml"),
                                               ("rId2", f"{REL_VISIO}/windows", "windows.xml")),
        "visio/windows.xml": _windows_xml(width, height),
        "visio/pages/pages.xml": _pages_xml(width, height),
        "visio/pages/_rels/pages.xml.rels": _rels(("rId1", f"{REL_VISIO}/page", "page1.xml")),
        "visio/pages/page1.xml": page,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in parts.items():
            zf.writestr(name, text.encode("utf-8"))
    return buf.getvalue()
