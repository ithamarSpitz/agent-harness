"""One-shot read-only graph.json query; delete after use."""
from __future__ import annotations

import json
from pathlib import Path

p = Path(r"C:/Users/itas1/dev/agent-harness/.harness/code-map/source/graphify-out/graph.json")
g = json.loads(p.read_text(encoding="utf-8"))
nodes = g["nodes"]
edges = g.get("edges") or g.get("links") or []
by_id = {n["id"]: n for n in nodes}
rels = {"calls", "imports_from", "uses", "references"}


def sf(n: dict) -> str:
    return str(n.get("source_file") or "").replace("\\", "/")


targets: dict[str, list[dict]] = {
    "review_prompt": [
        n
        for n in nodes
        if n.get("label") in {"review_prompt()", "review_prompt"}
        and sf(n).endswith("harness/prompts.py")
    ],
    "ProviderAgentRunner.review": [
        n
        for n in nodes
        if n.get("label") in {".review()", "review()"}
        and sf(n).endswith("harness/provider_runner.py")
    ],
    "ProviderAgentRunner._review_inner": [
        n
        for n in nodes
        if n.get("label") in {"._review_inner()", "_review_inner()"}
        and sf(n).endswith("harness/provider_runner.py")
    ],
    "collect_outside_impact": [
        n
        for n in nodes
        if "collect_outside_impact" in str(n.get("label") or "")
        or "collect_outside_impact" in str(n.get("id") or "")
    ],
}

lines: list[str] = []
lines.append(f"keys={sorted(g.keys())} nodes={len(nodes)} edges={len(edges)}")

# label format confirmation
kinds = {"f()": [], ".method()": [], "ClassName": []}
for n in nodes:
    if not n.get("_callable"):
        continue
    lab = n.get("label") or ""
    if lab.endswith("()") and not lab.startswith(".") and len(kinds["f()"]) < 3:
        kinds["f()"].append(lab)
    elif lab.startswith(".") and lab.endswith("()") and len(kinds[".method()"]) < 3:
        kinds[".method()"].append(lab)
    elif n.get("_callable_class") and len(kinds["ClassName"]) < 3:
        kinds["ClassName"].append(lab)
    if all(len(v) >= 3 for v in kinds.values()):
        break
lines.append(f"label_samples={kinds}")

out_cap = 20
emitted = 0
for name, ns in targets.items():
    lines.append(f"## {name} count={len(ns)}")
    for n in ns:
        lines.append(
            f"  node id={n.get('id')} label={n.get('label')} loc={n.get('source_location')}"
        )
    ids = {n["id"] for n in ns}
    for e in edges:
        tgt = e.get("target") or e.get("to")
        if tgt not in ids:
            continue
        rel = e.get("relation") or e.get("type") or e.get("label")
        if rel not in rels:
            continue
        src_id = e.get("source") or e.get("from")
        src = by_id.get(src_id, {})
        src_file = sf(src) or str(e.get("source_file") or "").replace("\\", "/")
        tgt_n = by_id.get(tgt, {})
        if src_file and sf(tgt_n) and src_file == sf(tgt_n):
            continue
        if emitted >= out_cap:
            break
        loc = str(src.get("source_location") or e.get("source_location") or "").lstrip("L")
        sym = src.get("label") or src_id
        lines.append(f"  IN {src_file}:{loc} -> {sym} [{rel}]")
        emitted += 1
    if emitted >= out_cap:
        break

out = Path(__file__).with_name("_graph_query_out.txt")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(out)
