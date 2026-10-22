"""Place topology nodes on a page: a tree from the router down.

Network gear forms the tree. Devices with nothing behind them (PCs, printers, phones
with nothing plugged in) are stacked in columns under the device they connect to, so
a switch with forty hosts doesn't make the diagram forty boxes wide.

When devices are grouped (by site, floor or room), each group is laid out the same way
inside its own frame, and the frames form a tree of their own: each frame sits below the
frames it connects up to.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from ..topology import HOST, Link, Node, Topology, root_priority

NODE_W, NODE_H = 210, 64
LEAF_W, LEAF_H = 210, 52
H_GAP, V_GAP, LEAF_GAP = 40, 90, 12
LEAVES_PER_COLUMN = 10
MARGIN = 40
TRUNK_OFFSET = 18  # how far left of a leaf column its connecting line runs
FRAME_PAD, FRAME_TITLE, FRAME_GAP = 20, 30, 80


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
    group: str | None = None

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
class Frame:
    """A labelled outline around one group of devices."""

    name: str
    x: float
    y: float
    w: float
    h: float


@dataclass
class Layout:
    boxes: dict[str, Box] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
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
        self.parent: dict[str, str] = {}
        self.order: list[str] = []  # breadth first, root by root
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
                self.order.append(current)
                for child, link in sorted(self.adj[current], key=lambda c: self._sort_key(c[0])):
                    if child in seen:
                        continue
                    seen.add(child)
                    self.parent_link[child] = link
                    self.parent[child] = current
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


class _Placer:
    """Tree placement over one set of child lists (the whole topology, or one group)."""

    def __init__(self, tree: _Tree, layout: Layout, children: dict[str, list[str]],
                 leaves: dict[str, list[str]]):
        self.tree, self.layout = tree, layout
        self.children, self.leaves = children, leaves
        self.widths: dict[str, float] = {}

    def row_width(self, subtrees: list[str], leaf_count: int) -> float:
        parts = [self.width(c) for c in subtrees]
        block_w, _ = _leaf_block_size(leaf_count)
        if block_w:
            parts.append(block_w)
        return sum(parts) + H_GAP * max(0, len(parts) - 1)

    def width(self, node_id: str) -> float:
        if node_id not in self.widths:
            self.widths[node_id] = max(NODE_W, self.row_width(self.children[node_id],
                                                             len(self.leaves[node_id])))
        return self.widths[node_id]

    def place(self, node_id: str, left: float, top: float) -> None:
        w = self.width(node_id)
        self.layout.boxes[node_id] = Box(self.tree.by_id[node_id], left + (w - NODE_W) / 2, top,
                                         NODE_W, NODE_H, parent=self.tree.parent.get(node_id),
                                         via=self.tree.parent_link.get(node_id))
        self.place_row(self.children[node_id], self.leaves[node_id], left, top + NODE_H + V_GAP, w)

    def place_row(self, subtrees: list[str], leaves: list[str], left: float, top: float,
                  span: float) -> None:
        """Subtrees side by side, then a block of stacked leaves, centred in ``span``."""
        x = left + (span - self.row_width(subtrees, len(leaves))) / 2
        for child in subtrees:
            self.place(child, x, top)
            x += self.width(child) + H_GAP
        for i, leaf in enumerate(leaves):
            col, row = divmod(i, LEAVES_PER_COLUMN)
            lx = x + TRUNK_OFFSET + col * (LEAF_W + TRUNK_OFFSET + H_GAP)
            ly = top + row * (LEAF_H + LEAF_GAP)
            self.layout.boxes[leaf] = Box(self.tree.by_id[leaf], lx, ly, LEAF_W, LEAF_H, leaf=True,
                                          parent=self.tree.parent.get(leaf),
                                          via=self.tree.parent_link.get(leaf))


@dataclass
class _Block:
    """One group's devices, laid out on their own before being placed on the page."""

    group: str | None
    boxes: dict[str, Box]
    w: float
    h: float
    framed: bool
    parent: int | None = None  # index of the block above this one
    x: float = 0
    y: float = 0


def _group_blocks(tree: _Tree, groups: dict[str, str]) -> list[_Block]:
    order: list[str | None] = []
    for node_id in tree.order:
        if groups.get(node_id) not in order:
            order.append(groups.get(node_id))
    blocks = []
    for group in order:
        def same(n: str, g=group) -> bool:
            return groups.get(n) == g

        children = defaultdict(list, {p: [c for c in kids if same(c)]
                                      for p, kids in tree.children.items()})
        leaves = defaultdict(list, {p: [c for c in kids if same(c)]
                                    for p, kids in tree.leaves.items()})
        roots = [n for n in tree.order
                 if same(n) and (n not in tree.parent or not same(tree.parent[n]))]
        # Members whose neighbour is in another group become this group's top row. Those
        # with nothing behind them are stacked like any other leaves.
        root_leaves = [n for n in roots if not tree.children.get(n) and not tree.leaves.get(n)]
        root_trees = [n for n in roots if n not in root_leaves]
        part = Layout()
        placer = _Placer(tree, part, children, leaves)
        span = max(NODE_W, placer.row_width(root_trees, len(root_leaves)))
        placer.place_row(root_trees, root_leaves, 0, 0, span)
        for box in part.boxes.values():
            box.group = group
        framed = group is not None
        pad = FRAME_PAD if framed else 0
        min_x = min(b.x - (TRUNK_OFFSET if b.leaf else 0) for b in part.boxes.values())
        max_x = max(b.x + b.w for b in part.boxes.values())
        max_y = max(b.bottom for b in part.boxes.values())
        for box in part.boxes.values():  # move to the block's own origin, inside the frame
            box.x += pad - min_x
            box.y += pad + (FRAME_TITLE if framed else 0)
        blocks.append(_Block(group, part.boxes, max_x - min_x + 2 * pad,
                             max_y + 2 * pad + (FRAME_TITLE if framed else 0), framed))
    # Hang each block below the deepest block it connects up to, so links into a frame come
    # from above it. Only earlier blocks count, which rules out loops.
    index = {b.group: i for i, b in enumerate(blocks)}
    depth: dict[int, int] = {}
    for i, block in enumerate(blocks):
        upstream = {index[groups.get(tree.parent[n])] for n in tree.order
                    if groups.get(n) == block.group and n in tree.parent
                    and groups.get(tree.parent[n]) != block.group}
        upstream = sorted(u for u in upstream if u < i)
        if upstream:
            block.parent = max(upstream, key=lambda u: (depth[u], -u))
            depth[i] = depth[block.parent] + 1
        else:
            depth[i] = 0
    return blocks


def _place_blocks(blocks: list[_Block]) -> None:
    kids: dict[int | None, list[int]] = defaultdict(list)
    for i, b in enumerate(blocks):
        kids[b.parent].append(i)
    spans: dict[int, float] = {}

    def span(i: int) -> float:
        if i not in spans:
            below = [span(k) for k in kids[i]]
            spans[i] = max(blocks[i].w, sum(below) + FRAME_GAP * max(0, len(below) - 1))
        return spans[i]

    def place(i: int, left: float, top: float) -> None:
        b = blocks[i]
        b.x, b.y = left + (span(i) - b.w) / 2, top
        below = kids[i]
        used = sum(span(k) for k in below) + FRAME_GAP * max(0, len(below) - 1)
        x = left + (span(i) - used) / 2
        for k in below:
            place(k, x, top + b.h + FRAME_GAP)
            x += span(k) + FRAME_GAP

    x = MARGIN + FRAME_GAP / 2  # room for links routed round the left of a frame
    for i in kids[None]:
        place(i, x, MARGIN)
        x += span(i) + FRAME_GAP


def compute_layout(topo: Topology, groups: dict[str, str] | None = None) -> Layout:
    """Lay the topology out. ``groups`` maps node ids to a group name (see groups.py);
    nodes left out of it are drawn outside any frame."""
    tree = _Tree(topo)
    layout = Layout()
    groups = {k: v for k, v in (groups or {}).items() if v}

    if not groups:
        placer = _Placer(tree, layout, tree.children, tree.leaves)
        x = MARGIN
        for root in tree.roots:
            placer.place(root, x, MARGIN)
            x += placer.width(root) + H_GAP * 2
        block_bounds: dict[str | None, tuple[float, float, float, float]] = {}
    else:
        blocks = _group_blocks(tree, groups)
        _place_blocks(blocks)
        block_bounds = {}
        for b in blocks:
            for node_id, box in b.boxes.items():
                box.x += b.x
                box.y += b.y
                layout.boxes[node_id] = box
            block_bounds[b.group] = (b.x, b.y, b.w, b.h)
            if b.framed:
                layout.frames.append(Frame(b.group, b.x, b.y, b.w, b.h))
        # Keep the page order the same as without groups, so outputs are stable.
        layout.boxes = {n: layout.boxes[n] for n in tree.order}

    def clear(x: float, top: float, bottom: float, skip: set[str]) -> bool:
        """True if a vertical line at x from top to bottom misses every box."""
        return not any(b.x - 4 <= x <= b.x + b.w + 4 and b.y < bottom and b.bottom > top
                       for n, b in layout.boxes.items() if n not in skip)

    def across_clear(y: float, x1: float, x2: float) -> bool:
        """True if a horizontal line at y between x1 and x2 misses every box."""
        lo, hi = min(x1, x2) + 1, max(x1, x2) - 1
        return not any(b.y - 4 <= y <= b.bottom + 4 and b.x < hi and b.x + b.w > lo
                       for b in layout.boxes.values())

    tree_links = set()
    for box in layout.boxes.values():
        if box.parent is None or box.via is None:
            continue
        tree_links.add(id(box.via))
        parent = layout.boxes[box.parent]
        start = [(parent.cx, parent.bottom)]
        if parent.group == box.group:
            mid_y = parent.bottom + V_GAP / 2
        else:  # into another frame: turn in the gap above it
            mid_y = block_bounds[box.group][1] - FRAME_GAP / 2
            if not clear(parent.cx, parent.bottom, mid_y, {box.parent, box.node.id}):
                # Straight down would run through other devices (and look like it starts
                # from one of them), so leave from the side and go round the frame.
                bx, _, bw, _ = block_bounds[parent.group]
                sy = parent.y + parent.h / 2
                routes = [(parent.x + parent.w, bx + bw + FRAME_GAP / 2),  # right
                          (parent.x, bx - FRAME_GAP / 2)]  # left
                if box.cx < parent.cx:
                    routes.reverse()
                side_x, gutter_x = next((r for r in routes if across_clear(sy, *r)), routes[0])
                start = [(side_x, sy), (gutter_x, sy)]
        down_x = start[-1][0]
        if box.leaf:
            trunk_x = box.x - TRUNK_OFFSET / 2
            ly = box.y + box.h / 2
            points = start + [(down_x, mid_y), (trunk_x, mid_y), (trunk_x, ly), (box.x, ly)]
        else:
            points = start + [(down_x, mid_y), (box.cx, mid_y), (box.cx, box.y)]
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

    right = [b.x + b.w for b in layout.boxes.values()] + [f.x + f.w for f in layout.frames]
    bottom = [b.bottom for b in layout.boxes.values()] + [f.y + f.h for f in layout.frames]
    layout.width = max(right, default=0) + MARGIN
    layout.height = max(bottom, default=0) + MARGIN
    return layout
