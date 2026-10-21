"""The knowledge graph as a picture: which entities and links to draw, and where.

Morphik showed its graph as a node-and-link picture (user, 2026-10-21: "Graph
picture"). This picks what to draw - the neighbourhood of one entity a few
links out, or, with nothing chosen, the best-connected entities of the whole
graph - and places it with a force-directed layout (Fruchterman-Reingold, in
numpy: 150 entities and 300 iterations take a few milliseconds). Positions are
the same every time for the same graph (a fixed seed), so a picture does not
jump about between looks. Toolkit-free: the Connections tab draws it.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from council_core import knowledge_graph as kgm

MAX_NODES = 120


def _display(ent: Dict[str, Any]) -> str:
    name = ent.get("name", "")
    if kgm.looks_like_code(name):
        plain = [a for a in ent.get("aliases", []) if not kgm.looks_like_code(a)
                 and kgm.fold(a) != kgm.fold(name)]
        if plain:
            return f"{name} — {max(plain, key=len)}"
    return name


def subgraph(kg: kgm.KnowledgeGraph, center: Optional[str] = None, *, hops: int = 2,
             max_nodes: int = MAX_NODES) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(nodes, edges) to draw. With ``center``: it and what is within
    ``hops`` links of it, nearest first, at most ``max_nodes``. Without: the
    ``max_nodes`` entities with most links. Rejected links and Collection
    links (project -> its documents) are not drawn."""
    rel_q = ("SELECT id, subject_id, predicate, object_id, status FROM relations WHERE"
             " status != 'rejected' AND predicate != 'DOCUMENTED_IN'")
    chosen: List[str] = []
    dist: Dict[str, int] = {}
    if center:
        center = kg.resolve(center)
        dist[center] = 0
        chosen = [center]
        frontier = [center]
        for d in range(1, max(1, hops) + 1):
            nxt = []
            for eid in frontier:
                for r in kg.db.execute(rel_q + " AND (subject_id=? OR object_id=?)",
                                       (eid, eid)):
                    other = r["object_id"] if r["subject_id"] == eid else r["subject_id"]
                    if other not in dist and len(chosen) < max_nodes:
                        dist[other] = d
                        chosen.append(other)
                        nxt.append(other)
            frontier = nxt
            if not frontier or len(chosen) >= max_nodes:
                break
    else:
        deg: Dict[str, int] = {}
        for r in kg.db.execute(rel_q):
            for e in (r["subject_id"], r["object_id"]):
                deg[e] = deg.get(e, 0) + 1
        chosen = [e for e, _n in sorted(deg.items(), key=lambda x: (-x[1], x[0]))
                  [:max_nodes]]
    keep = set(chosen)
    edges: List[Dict[str, Any]] = []
    degree: Dict[str, int] = {e: 0 for e in chosen}
    if keep:
        ids = list(keep)
        for k in range(0, len(ids), 400):
            part = ids[k:k + 400]
            marks = ",".join("?" * len(part))
            for r in kg.db.execute(rel_q + f" AND subject_id IN ({marks})", part):
                if r["object_id"] in keep:
                    edges.append({"id": r["id"], "source": r["subject_id"],
                                  "target": r["object_id"], "predicate": r["predicate"],
                                  "status": r["status"]})
                    degree[r["subject_id"]] += 1
                    degree[r["object_id"]] += 1
    nodes = []
    for eid in chosen:
        ent = kg.entity(eid)
        if ent is None or ent["type"] == "DOCUMENT":
            continue
        nodes.append({"id": eid, "name": _display(ent), "type": ent["type"],
                      "degree": degree.get(eid, 0), "hop": dist.get(eid),
                      "description": ent.get("description", ""),
                      "center": eid == center})
    have = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source"] in have and e["target"] in have]
    return nodes, edges


def layout(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], *,
           iterations: int = 300, seed: int = 7, size: float = 1000.0
           ) -> Dict[str, Tuple[float, float]]:
    """Fruchterman-Reingold positions in a ``size`` x ``size`` square
    centred on 0,0. The centre node (``center``: True) stays at 0,0 and its
    neighbours ring it; nodes that link to nothing are spread on the edge."""
    import numpy as np
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]["id"]: (0.0, 0.0)}
    idx = {nd["id"]: i for i, nd in enumerate(nodes)}
    rng = np.random.default_rng(seed)
    ang = np.linspace(0, 2 * math.pi, n, endpoint=False)
    hop = np.array([nd.get("hop") if nd.get("hop") is not None else 2 for nd in nodes],
                   dtype=float)
    radius = (0.15 + 0.3 * hop) * size / 2
    pos = np.stack([np.cos(ang) * radius, np.sin(ang) * radius], axis=1)
    pos += rng.normal(scale=size * 0.01, size=pos.shape)
    pinned = np.array([bool(nd.get("center")) for nd in nodes])
    pos[pinned] = 0.0
    src = np.array([idx[e["source"]] for e in edges], dtype=int)
    dst = np.array([idx[e["target"]] for e in edges], dtype=int)
    k = math.sqrt(size * size / n) * 0.75
    t = size / 10
    for _ in range(iterations):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.sqrt((delta ** 2).sum(-1)) + 1e-6
        np.fill_diagonal(dist, np.inf)
        disp = (delta / dist[..., None] * (k * k / dist)[..., None]).sum(1)
        if len(src):
            d = pos[src] - pos[dst]
            dl = np.sqrt((d ** 2).sum(-1)) + 1e-6
            f = (d / dl[:, None]) * (dl * dl / k)[:, None]
            np.add.at(disp, src, -f)
            np.add.at(disp, dst, f)
        # a gentle pull to the middle keeps parts that link to nothing near
        disp -= pos * 0.01
        length = np.sqrt((disp ** 2).sum(-1)) + 1e-6
        pos += disp / length[:, None] * np.minimum(length, t)[:, None]
        pos[pinned] = 0.0
        t *= 0.985
    span = np.abs(pos).max() or 1.0
    pos = pos / span * (size / 2)
    return {nd["id"]: (float(pos[i, 0]), float(pos[i, 1])) for i, nd in enumerate(nodes)}
