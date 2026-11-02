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


#: Text values drawn under one header in a file's picture.
VALUES_PER_FIELD = 4
#: Headers drawn for one file.
FIELDS_DRAWN = 30


def contents_subgraph(kg: kgm.KnowledgeGraph, document_id: str, *,
                      max_nodes: int = MAX_NODES
                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(nodes, edges) of one file's contents (council_core/kg_contents.py;
    user, 2026-10-26: "an excel file has the head node with the file name
    then lets say stat headers/labels and then that data is connected to the
    labels"): the file, its headers and labels (a numeric one names its
    range), each text header's commonest values, and the OTHER files that
    hold one of those values - the connections no rule had to make. Node
    ids are 'c:file:<document id>', 'c:field:<id>', 'c:val:<id>'."""
    from council_core import kg_contents as kgc
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    have: set = set()

    def node(nid: str, name: str, ntype: str, **kw) -> bool:
        if nid in have:
            return True
        if len(nodes) >= max_nodes:
            return False
        have.add(nid)
        nodes.append(dict({"id": nid, "name": name, "type": ntype, "degree": 0,
                           "hop": None, "description": "", "center": False}, **kw))
        return True

    def edge(a: str, pred: str, b: str) -> None:
        eid = f"{a}>{pred}>{b}"
        if a in have and b in have and not any(e["id"] == eid for e in edges):
            edges.append({"id": eid, "source": a, "target": b, "predicate": pred,
                          "status": "seeded"})

    files = {f["document_id"]: f for f in kg.contents_files()}
    me = files.get(document_id)
    if me is None:
        return [], []
    fid0 = f"c:file:{document_id}"
    node(fid0, me["file"], "FILE", center=True, hop=0)
    fields = [f for f in kg.contents_fields(document_id) if f["kind"] != "loose"]
    sheets = {f["sheet"] for f in fields}
    for f in fields[:FIELDS_DRAWN]:
        name = f["label"] if len(sheets) <= 1 or not f["sheet"] else \
            f"{f['sheet']} › {f['label']}"
        if f["numeric"] and f["num_min"] is not None:
            name += f" ({kgc._num(f['num_min'])} to {kgc._num(f['num_max'])})"
        nid = f"c:field:{f['id']}"
        if node(nid, name, "NUMBERS" if f["numeric"] else "HEADER", hop=1):
            edge(fid0, "HAS_HEADER", nid)
    for f in fields[:FIELDS_DRAWN]:
        if f["numeric"] or f"c:field:{f['id']}" not in have:
            continue
        got = kg.field_values(f["id"], limit=VALUES_PER_FIELD)
        for v in got["values"]:
            if v.get("value_id") is None:
                continue
            vid = f"c:val:{v['value_id']}"
            if node(vid, v["text"], "VALUE", hop=2):
                edge(f"c:field:{f['id']}", "HAS_VALUE", vid)
    # the other files that hold a value drawn
    for n in list(nodes):
        if n["type"] != "VALUE":
            continue
        vid = int(n["id"].split(":")[-1])
        others = {p["document_id"] for p in kg.value_places(value_id=vid, limit=50)}
        for other in sorted(others - {document_id}):
            o = files.get(other)
            if o is None:
                continue
            oid = f"c:file:{other}"
            if node(oid, o["file"], "FILE", hop=3):
                edge(n["id"], "ALSO_IN", oid)
    degree: Dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    for n in nodes:
        n["degree"] = degree.get(n["id"], 0)
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
