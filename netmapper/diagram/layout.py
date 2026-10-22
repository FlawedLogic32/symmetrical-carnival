"""Place topology nodes on a page: a tree from the router down.

Network gear forms the tree. Devices with nothing behind them (PCs, printers, phones
with nothing plugged in) are stacked in columns under the device they connect to, so
a switch with forty hosts doesn't make the diagram forty boxes wide.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from ..topology import HOST, Link, Node, Topology, root_priority

NODE_W, NODE_H = 180, 64
LEAF_W, LEAF_H = 180, 52
H_GAP, V_GAP, LEAF_GAP = 40, 90, 12
LEAVES_PER_COLUMN = 10
MARGIN = 40
TRUNK_OFFSET = 18  # how far left of a leaf column its connecting line runs


@dataclass
class Box:
    node: Node
    x: float
    y: float
    w: float
    h: float
    leaf: bool = False
    parent: str | None = None
    via: Link | None = None  # the link to the parent
    parent_port: str | None = None  # the parent's port this node plugs into

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def bottom(self) -> float:
        return self.y + self.h


@dataclass
class Edge:
    link: Link
    points: list[tuple[float, float]]
    tree: bool  # False for extra links that close a loop (redundant cabling)
    parent_port: str | None = None
    child_port: str | None = None
    parent: str = ""  # node ids; for non-tree edges, simply the two ends
    child: str = ""


@dataclass
class Layout:
    boxes: dict[str, Box] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    width: float = 0
    height: float = 0


class _Tree:
    def __init__(self, topo: Topology):
        self.by_id = {n.id: n for n in topo.nodes}
        self.adj: dict[str, list[tuple[str, Link]]] = defaultdict(list)
        for link in topo.links:
            self.adj[link.source].append((link.target, link))
            self.adj[link.target].append((link.source, link))
        self.children: dict[str, list[str]] = defaultdict(list)
        self.leaves: dict[str, list[str]] = defaultdict(list)
        self.parent_link: dict[str, Link] = {}
        self.roots: list[str] = []
        self._build(topo)

    def _sort_key(self, node_id: str):
        n = self.by_id[node_id]
        return (n.kind == HOST, n.label.lower())

    def _build(self, topo: Topology) -> None:
        roots = sorted(topo.nodes, key=lambda n: root_priority(n, len(self.adj[n.id])))
        seen: set[str] = set()
        for root in roots:
            if root.id in seen:
                continue
            self.roots.append(root.id)
            seen.add(root.id)
            queue = [root.id]
            while queue:  # breadth first, so each node hangs off its nearest neighbour
                current = queue.pop(0)
                for child, link in sorted(self.adj[current], key=lambda c: self._sort_key(c[0])):
                    if child in seen:
                        continue
                    seen.add(child)
                    self.parent_link[child] = link
                    self.children[current].append(child)
                    queue.append(child)
        # Split each node's children into subtrees and stackable leaves.
        for parent, kids in list(self.children.items()):
            self.leaves[parent] = [k for k in kids if not self.children.get(k)]
            self.children[parent] = [k for k in kids if self.children.get(k)]


def _leaf_block_size(count: int) -> tuple[float, float]:
    if not count:
        return 0, 0
    cols = math.ceil(count / LEAVES_PER_COLUMN)
    rows = min(count, LEAVES_PER_COLUMN)
    return (cols * (LEAF_W + TRUNK_OFFSET) + (cols - 1) * H_GAP,
            rows * LEAF_H + (rows - 1) * LEAF_GAP)


def compute_layout(topo: Topology) -> Layout:
    tree = _Tree(topo)
    widths: dict[str, float] = {}

    def width(node_id: str) -> float:
        if node_id in widths:
            return widths[node_id]
        parts = [width(c) for c in tree.children[node_id]]
        block_w, _ = _leaf_block_size(len(tree.leaves[node_id]))
        if block_w:
            parts.append(block_w)
        total = sum(parts) + H_GAP * max(0, len(parts) - 1)
        widths[node_id] = max(NODE_W, total)
        return widths[node_id]

    layout = Layout()

    def place(node_id: str, left: float, top: float, parent: str | None) -> None:
        w = width(node_id)
        box = Box(tree.by_id[node_id], left + (w - NODE_W) / 2, top, NODE_W, NODE_H,
                  parent=parent, via=tree.parent_link.get(node_id))
        layout.boxes[node_id] = box
        child_top = top + NODE_H + V_GAP
        parts = [(c, width(c)) for c in tree.children[node_id]]
        block_w, _ = _leaf_block_size(len(tree.leaves[node_id]))
        used = sum(p for _, p in parts) + (block_w if block_w else 0)
        used += H_GAP * max(0, len(parts) + (1 if block_w else 0) - 1)
        x = left + (w - used) / 2
        for child, cw in parts:
            place(child, x, child_top, node_id)
            x += cw + H_GAP
        for i, leaf in enumerate(tree.leaves[node_id]):
            col, row = divmod(i, LEAVES_PER_COLUMN)
            lx = x + TRUNK_OFFSET + col * (LEAF_W + TRUNK_OFFSET + H_GAP)
            ly = child_top + row * (LEAF_H + LEAF_GAP)
            layout.boxes[leaf] = Box(tree.by_id[leaf], lx, ly, LEAF_W, LEAF_H, leaf=True,
                                     parent=node_id, via=tree.parent_link.get(leaf))

    x = MARGIN
    for root in tree.roots:
        place(root, x, MARGIN, None)
        x += width(root) + H_GAP * 2

    tree_links = set()
    for box in layout.boxes.values():
        if box.parent is None or box.via is None:
            continue
        tree_links.add(id(box.via))
        parent = layout.boxes[box.parent]
        mid_y = parent.bottom + V_GAP / 2
        if box.leaf:
            trunk_x = box.x - TRUNK_OFFSET / 2
            ly = box.y + box.h / 2
            points = [(parent.cx, parent.bottom), (parent.cx, mid_y), (trunk_x, mid_y),
                      (trunk_x, ly), (box.x, ly)]
        else:
            points = [(parent.cx, parent.bottom), (parent.cx, mid_y), (box.cx, mid_y),
                      (box.cx, box.y)]
        if box.via.source == box.parent:
            pp, cp = box.via.source_port, box.via.target_port
        else:
            pp, cp = box.via.target_port, box.via.source_port
        box.parent_port = pp
        layout.edges.append(Edge(box.via, points, True, pp, cp, box.parent, box.node.id))

    for link in topo.links:
        if id(link) in tree_links:
            continue
        a, b = layout.boxes[link.source], layout.boxes[link.target]
        layout.edges.append(Edge(link, [(a.cx, a.y + a.h / 2), (b.cx, b.y + b.h / 2)], False,
                                 link.source_port, link.target_port, link.source, link.target))

    layout.width = max((b.x + b.w for b in layout.boxes.values()), default=0) + MARGIN
    layout.height = max((b.bottom for b in layout.boxes.values()), default=0) + MARGIN
    return layout
