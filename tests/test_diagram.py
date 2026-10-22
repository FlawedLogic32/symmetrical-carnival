import io
import xml.etree.ElementTree as ET
import zipfile

from test_topology import H5, office

from netmapper.config import config_from_dict
from netmapper.diagram import (assign_groups, compute_layout, icons, to_drawio, to_html, to_svg,
                               to_vsdx)
from netmapper.diagram.style import KIND_NAMES
from netmapper.diff import diff_topologies
from netmapper.models import DiscoveryRun, Host
from netmapper.topology import build_topology


def _overlap(a, b) -> bool:
    return not (a.x + a.w <= b.x or b.x + b.w <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y)


def test_layout_places_every_node_without_overlaps():
    topo = build_topology(office())
    layout = compute_layout(topo)
    assert set(layout.boxes) == {n.id for n in topo.nodes}
    boxes = list(layout.boxes.values())
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not _overlap(a, b), (a.node.label, b.node.label)
    assert len(layout.edges) == len(topo.links)
    gw = next(b for b in boxes if b.node.label == "gw")
    assert gw.y == min(b.y for b in boxes)  # router on top


def test_many_hosts_are_stacked_in_columns():
    hosts = [Host(f"10.0.0.{i}", f"00:00:0c:00:01:{i:02x}", ["arp-cache"]) for i in range(1, 26)]
    topo = build_topology(DiscoveryRun(started_at="t", hosts=hosts))
    layout = compute_layout(topo)
    xs = {round(b.x) for b in layout.boxes.values() if b.leaf}
    assert len(xs) == 3  # 25 hosts -> columns of 10, 10, 5
    assert layout.width < 1000


def test_drawio_is_valid_and_complete():
    topo = build_topology(office())
    root = ET.fromstring(to_drawio(topo))
    cells = root.findall(".//mxCell")
    vertices = [c for c in cells if c.get("vertex") == "1"]
    edges = [c for c in cells if c.get("edge") == "1"]
    assert len(vertices) == len(topo.nodes)
    assert len(edges) == len(topo.links)
    ids = {c.get("id") for c in vertices}
    assert all(e.get("source") in ids and e.get("target") in ids for e in edges)
    assert any("dashed=1" in e.get("style") for e in edges)  # inferred links
    assert any("<b>core-sw</b>" in v.get("value") for v in vertices)


def test_html_is_self_contained_and_marks_changes():
    topo = build_topology(office())
    old = build_topology(office())
    old.nodes = [n for n in old.nodes if n.mac != H5]
    old.links = [lk for lk in old.links if f"host:{H5}" not in (lk.source, lk.target)]
    diff = diff_topologies(old, topo)
    assert [n.mac for n in diff.added] == [H5]
    page = to_html(topo, diff)
    assert page.startswith("<!doctype html>")
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in page
    assert "New: 3c:52:82:00:00:05</li>" in page
    assert page.count("<tr>") == len(topo.nodes) + 1
    assert 'class="node kind-host new"' in to_svg(topo, new_ids={f"host:{H5}"})


def test_diff_reports_gone_and_moved():
    old = build_topology(office())
    run = office()
    edge = next(d for d in run.devices if d.sys_name == "edge-sw")
    for entry in edge.fdb:
        if entry.mac == "3c:52:82:00:00:06":
            entry.port = "7"
    run.hosts = [h for h in run.hosts if h.ip != "192.168.1.99"]
    new = build_topology(run)
    diff = diff_topologies(old, new)
    assert [n.ip for n in diff.removed] == ["192.168.1.99"]
    assert [(n.mac, was, now) for n, was, now in diff.moved] == [
        ("3c:52:82:00:00:06", "edge-sw 3", "edge-sw 7")]
    assert diff_topologies(new, new).empty


def _office_groups(topo):
    cfg = config_from_dict({"diagram": {"groups": [
        {"name": "Ground floor", "members": ["access-sw"]},
        {"name": "First floor", "members": ["edge-sw*", "192.168.1.40/32"]}]}})
    return assign_groups(topo, cfg.diagram_groups)


def test_every_kind_has_an_icon():
    for kind in KIND_NAMES:
        assert kind in icons.ICONS
        svg = icons.svg_document(kind, "#000000")
        ET.fromstring(svg)  # well formed
    assert icons.drawio_image("router", "#2563eb").startswith("data:image/svg+xml,PHN2Zy")


def test_groups_follow_rules_then_location_then_the_uplink():
    topo = build_topology(office())
    groups = _office_groups(topo)
    by_label = {n.label: groups.get(n.id) for n in topo.nodes}
    assert by_label["access-sw"] == "Ground floor"
    assert by_label["office-ap"] == "Ground floor"  # behind access-sw
    assert by_label["edge-sw"] == "First floor"  # its own rule beats access-sw's group
    assert by_label[H5] == "Ground floor"
    assert by_label["SEP001122334455"] == "First floor"  # matched by IP
    assert by_label["core-sw"] is None and by_label["gw"] is None

    located = assign_groups(topo, locations={"dev:192.168.1.2": " Comms room "})
    assert located["dev:192.168.1.2"] == "Comms room"
    assert located["dev:192.168.1.3"] == "Comms room"  # inherited from the core
    assert "dev:192.168.1.1" not in located  # the router is above the core
    assert assign_groups(topo) == {}


def test_grouped_layout_keeps_frames_apart_and_devices_inside():
    topo = build_topology(office())
    groups = _office_groups(topo)
    layout = compute_layout(topo, groups)
    assert sorted(f.name for f in layout.frames) == ["First floor", "Ground floor"]
    assert set(layout.boxes) == {n.id for n in topo.nodes}
    boxes = list(layout.boxes.values())
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not _overlap(a, b), (a.node.label, b.node.label)
    frames = {f.name: f for f in layout.frames}
    for node_id, box in layout.boxes.items():
        for f in layout.frames:
            inside = (f.x <= box.x and box.x + box.w <= f.x + f.w
                      and f.y <= box.y and box.bottom <= f.y + f.h)
            assert inside == (groups.get(node_id) == f.name), (box.node.label, f.name)
    assert not _overlap(frames["First floor"], frames["Ground floor"])
    assert len(layout.edges) == len(topo.links)
    # Links into a frame come in from above it.
    for edge in layout.edges:
        child = layout.boxes[edge.child]
        if edge.tree and child.group and child.group != layout.boxes[edge.parent].group:
            assert min(y for _, y in edge.points) < frames[child.group].y


def test_drawio_puts_grouped_devices_in_containers():
    topo = build_topology(office())
    layout = compute_layout(topo, _office_groups(topo))
    root = ET.fromstring(to_drawio(topo, layout))
    cells = root.findall(".//mxCell")
    frames = {c.get("id"): c for c in cells if "container=1" in (c.get("style") or "")}
    assert sorted(c.get("value") for c in frames.values()) == ["First floor", "Ground floor"]
    devices = [c for c in cells if c.get("vertex") == "1" and c.get("id") not in frames]
    assert len(devices) == len(topo.nodes)
    assert all("shape=label" in d.get("style") and "image=data:image/svg+xml," in d.get("style")
               for d in devices)
    assert sum(d.get("parent") in frames for d in devices) == 8


def test_vsdx_is_a_complete_visio_package():
    topo = build_topology(office())
    layout = compute_layout(topo, _office_groups(topo))
    data = to_vsdx(topo, layout, {f"host:{H5}"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "visio/document.xml",
                "visio/pages/pages.xml", "visio/pages/page1.xml",
                "visio/pages/_rels/pages.xml.rels"} <= names
        for name in names:
            ET.fromstring(zf.read(name))  # every part is well formed
        page = ET.fromstring(zf.read("visio/pages/page1.xml"))
        types = ET.fromstring(zf.read("[Content_Types].xml"))
    overridden = {o.get("PartName").lstrip("/") for o in types if o.get("PartName")}
    assert {n for n in names if n.endswith(".xml") and "_rels" not in n and "[" not in n} \
        <= overridden
    ns = {"v": "http://schemas.microsoft.com/office/visio/2012/main"}
    top = page.findall("v:Shapes/v:Shape", ns)
    groups = [s for s in top if s.get("Type") == "Group"]
    assert len(groups) == len(topo.nodes)
    connects = page.findall("v:Connects/v:Connect", ns)
    assert len(connects) == 2 * len(topo.links)
    group_ids = {g.get("ID") for g in groups}
    assert all(c.get("ToSheet") in group_ids for c in connects)
    ids = [s.get("ID") for s in page.iter(f"{{{ns['v']}}}Shape")]
    assert len(ids) == len(set(ids))
    texts = "".join("".join(t.itertext()) for t in page.iter(f"{{{ns['v']}}}Text"))
    assert "core-sw" in texts and "Ground floor" in texts
