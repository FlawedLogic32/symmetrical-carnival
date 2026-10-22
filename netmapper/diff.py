"""Compare two topologies so each run can say what changed since the last one."""

from __future__ import annotations

from dataclasses import dataclass, field

from .topology import Node, Topology


@dataclass
class TopologyDiff:
    added: list[Node] = field(default_factory=list)
    removed: list[Node] = field(default_factory=list)
    moved: list[tuple[Node, str, str]] = field(default_factory=list)  # node, was, now

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.moved)

    def lines(self) -> list[str]:
        out = [f"New: {_describe(n)}" for n in self.added]
        out += [f"Gone: {_describe(n)}" for n in self.removed]
        out += [f"Moved: {_describe(n)} from {was} to {now}" for n, was, now in self.moved]
        return out


def _describe(n: Node) -> str:
    extra = [x for x in (n.ip, n.mac) if x and x != n.label]
    return f"{n.label} ({', '.join([n.kind] + extra)})"


def _attachments(topo: Topology) -> dict[str, set[str]]:
    """For each node, where it plugs in, e.g. {'core-sw Gi1/0/4'}."""
    label = {n.id: n.label for n in topo.nodes}
    where: dict[str, set[str]] = {}
    for lk in topo.links:
        for here, there, there_port in ((lk.target, lk.source, lk.source_port),
                                        (lk.source, lk.target, lk.target_port)):
            name = label.get(there, there)
            spot = f"{name} {there_port}" if there_port else name
            where.setdefault(here, set()).add(spot)
    return where


def diff_topologies(old: Topology | None, new: Topology) -> TopologyDiff:
    if old is None:
        return TopologyDiff()
    # Inferred placeholders (unmanaged switches, "location unknown") are named after
    # where they were found, so they aren't reported as devices in their own right.
    real = lambda t: {n.id: n for n in t.nodes if not n.inferred}  # noqa: E731
    before, after = real(old), real(new)
    result = TopologyDiff(
        added=[after[i] for i in after.keys() - before.keys()],
        removed=[before[i] for i in before.keys() - after.keys()],
    )
    was, now = _attachments(old), _attachments(new)
    for node_id in sorted(before.keys() & after.keys()):
        node = after[node_id]
        if node.kind not in ("host", "vm-host"):
            continue
        a, b = was.get(node_id, set()), now.get(node_id, set())
        if a and b and a != b:
            result.moved.append((node, ", ".join(sorted(a)), ", ".join(sorted(b))))
    result.added.sort(key=lambda n: n.label.lower())
    result.removed.sort(key=lambda n: n.label.lower())
    return result
