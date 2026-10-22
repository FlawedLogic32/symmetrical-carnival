import xml.etree.ElementTree as ET

from test_topology import H5, office

from netmapper.diagram import compute_layout, to_drawio, to_html, to_svg
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
