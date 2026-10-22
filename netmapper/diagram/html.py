"""A single, self-contained HTML page: the diagram, what changed, and a searchable device list.

No internet access is needed to open it, so it can be emailed or kept with a report.
"""

from __future__ import annotations

from html import escape

from ..diff import TopologyDiff
from ..topology import Topology
from .layout import compute_layout
from .style import KIND_COLORS, KIND_NAMES
from .svg import to_svg

_CSS = """
:root{--bg:#f8fafc;--panel:#ffffff;--text:#0f172a;--muted:#64748b;--border:#e2e8f0;
--accent:#2563eb;--good:#059669;--warn:#b45309;--canvas:#ffffff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0b1120;--panel:#111827;
--text:#e5e7eb;--muted:#94a3b8;--border:#1f2937;--accent:#60a5fa;--good:#34d399;--warn:#fbbf24;
--canvas:#f8fafc}}
:root[data-theme="dark"]{--bg:#0b1120;--panel:#111827;--text:#e5e7eb;--muted:#94a3b8;
--border:#1f2937;--accent:#60a5fa;--good:#34d399;--warn:#fbbf24;--canvas:#f8fafc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
main{max-width:1400px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.chip{background:var(--panel);border:1px solid var(--border);border-radius:999px;padding:4px 12px;
display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
.card ul{margin:0;padding-left:18px}
.card li{margin:2px 0}
.new{color:var(--good)} .gone{color:#dc2626} .moved{color:var(--warn)}
.muted{color:var(--muted)}
.diagram{background:var(--panel);border:1px solid var(--border);border-radius:12px;margin-bottom:20px}
.toolbar{display:flex;gap:8px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border);
flex-wrap:wrap}
.toolbar button{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;
padding:4px 12px;font:inherit;cursor:pointer}
.toolbar button:hover{border-color:var(--accent)}
.canvas{overflow:auto;max-height:75vh;background:var(--canvas);border-radius:0 0 12px 12px;cursor:grab}
.canvas svg{display:block}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-left:auto;font-size:12px;color:var(--muted)}
.legend span{display:flex;align-items:center;gap:6px}
.swatch{width:14px;height:10px;border-radius:3px;border:1.5px solid}
input[type=search]{width:100%;max-width:360px;padding:8px 12px;border-radius:8px;
border:1px solid var(--border);background:var(--bg);color:var(--text);font:inherit;margin-bottom:12px}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:720px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
td.mono{font-family:Consolas,"Cascadia Mono",monospace;font-size:13px}
"""

_JS = """
(function(){
  var canvas=document.querySelector('.canvas'), svg=canvas.querySelector('svg');
  var baseW=+svg.getAttribute('width'), baseH=+svg.getAttribute('height'), scale=1;
  function apply(){svg.setAttribute('width',baseW*scale);svg.setAttribute('height',baseH*scale);
    document.getElementById('zoom').textContent=Math.round(scale*100)+'%';}
  function fit(){scale=Math.min(1,(canvas.clientWidth-8)/baseW);apply();}
  document.getElementById('zin').onclick=function(){scale=Math.min(3,scale*1.25);apply();};
  document.getElementById('zout').onclick=function(){scale=Math.max(0.1,scale/1.25);apply();};
  document.getElementById('zfit').onclick=fit;
  document.getElementById('z100').onclick=function(){scale=1;apply();};
  var drag=null;
  canvas.addEventListener('mousedown',function(e){drag={x:e.clientX,y:e.clientY,
    l:canvas.scrollLeft,t:canvas.scrollTop};canvas.style.cursor='grabbing';});
  window.addEventListener('mouseup',function(){drag=null;canvas.style.cursor='';});
  window.addEventListener('mousemove',function(e){if(!drag)return;
    canvas.scrollLeft=drag.l-(e.clientX-drag.x);canvas.scrollTop=drag.t-(e.clientY-drag.y);});
  fit();
  var search=document.getElementById('filter'), rows=document.querySelectorAll('tbody tr');
  search.addEventListener('input',function(){var q=search.value.toLowerCase();
    rows.forEach(function(r){r.style.display=r.textContent.toLowerCase().indexOf(q)<0?'none':'';});});
})();
"""


def _e(value) -> str:
    return escape(str(value)) if value is not None else ""


def to_html(topo: Topology, diff: TopologyDiff | None = None, first_run: bool = False,
            title: str = "Network map") -> str:
    layout = compute_layout(topo)
    new_ids = {n.id for n in diff.added} if diff else set()
    svg = to_svg(topo, layout, new_ids)

    counts: dict[str, int] = {}
    for n in topo.nodes:
        counts[n.kind] = counts.get(n.kind, 0) + 1
    chips = "".join(
        f'<span class="chip"><span class="dot" style="background:{KIND_COLORS[k][1]}"></span>'
        f'{c} {_e(KIND_NAMES.get(k, k))}</span>'
        for k, c in sorted(counts.items(), key=lambda kv: list(KIND_NAMES).index(kv[0])
                           if kv[0] in KIND_NAMES else 99))

    if first_run or diff is None:
        changes = '<p class="muted">First run, so there is nothing to compare against yet.</p>'
    elif diff.empty:
        changes = '<p class="muted">No changes since the last run.</p>'
    else:
        def extra(n):
            other = next((x for x in (n.ip, n.mac) if x and x != n.label), "")
            return f' <span class="muted">{_e(other)}</span>' if other else ""

        items = [f'<li class="new">New: {_e(n.label)}{extra(n)}</li>' for n in diff.added]
        items += [f'<li class="gone">Gone: {_e(n.label)}{extra(n)}</li>' for n in diff.removed]
        items += [f'<li class="moved">Moved: {_e(n.label)} <span class="muted">from {_e(a)} '
                  f'to {_e(b)}</span></li>' for n, a, b in diff.moved]
        changes = f"<ul>{''.join(items)}</ul>"

    notes = "".join(f"<li>{_e(n)}</li>" for n in topo.notes)
    notes_html = f"<ul>{notes}</ul>" if notes else '<p class="muted">Nothing to flag.</p>'

    legend = "".join(
        f'<span><span class="swatch" style="background:{f};border-color:{s}"></span>'
        f'{_e(KIND_NAMES[k])}</span>' for k, (f, s) in KIND_COLORS.items() if k in counts)
    legend += '<span><svg width="26" height="6"><line x1="0" y1="3" x2="26" y2="3" ' \
              'stroke="#64748b" stroke-width="1.5" stroke-dasharray="5 4"/></svg>Inferred link</span>'

    rows = []
    for node_id, box in sorted(layout.boxes.items(), key=lambda kv: (kv[1].node.kind == "host",
                                                                    kv[1].node.label.lower())):
        n = box.node
        if box.parent:
            parent = layout.boxes[box.parent].node.label
            where = f"{parent} {box.parent_port}" if box.parent_port else parent
        else:
            where = ""
        mark = ' <span class="new">(new)</span>' if node_id in new_ids else ""
        rows.append(f"<tr><td>{_e(n.label)}{mark}</td><td>{_e(KIND_NAMES.get(n.kind, n.kind))}</td>"
                    f'<td class="mono">{_e(n.ip)}</td><td class="mono">{_e(n.mac)}</td>'
                    f"<td>{_e(n.vendor)}</td><td>{_e(where)}</td></tr>")

    links = len(topo.links)
    inferred = sum(1 for lk in topo.links if lk.inferred)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>{_e(title)}</h1>
<p class="sub">Generated {_e(topo.generated_at)} from {_e(topo.source_run or "a discovery run")}.
{len(topo.nodes)} devices, {links} connections ({inferred} inferred).</p>
<div class="chips">{chips}</div>
<div class="grid">
<section class="card"><h2>Changes since the last run</h2>{changes}</section>
<section class="card"><h2>Notes</h2>{notes_html}</section>
</div>
<section class="diagram">
<div class="toolbar">
<button id="zout" aria-label="Zoom out">−</button><button id="zin" aria-label="Zoom in">+</button>
<button id="zfit">Fit</button><button id="z100">100%</button><span id="zoom" class="muted"></span>
<div class="legend">{legend}</div>
</div>
<div class="canvas">{svg}</div>
</section>
<section class="card">
<h2>Devices</h2>
<input type="search" id="filter" placeholder="Filter by name, IP, MAC, vendor…">
<div class="table-wrap"><table>
<thead><tr><th>Name</th><th>Type</th><th>IP</th><th>MAC</th><th>Vendor</th><th>Connected to</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>
</section>
</main>
<script>{_JS}</script>
</body>
</html>
"""
