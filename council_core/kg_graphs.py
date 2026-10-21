"""Named knowledge graphs, each over the documents the user chose.

Morphik let you create several graphs - "create_graph(name, filters)" - each
built from the documents you picked, and update them later (user, 2026-10-21:
"one graph per vault" was a wrong assumption). Here a graph is:

  * "All documents" (id ``main``): the store the Council always had,
    ``<vault>/.knowledge_graph/graph.sqlite`` - everything in data_in, the
    folders added in Connections › Folders… and the Ledgers the Ledger tab
    adds. It cannot be deleted.
  * any number of named graphs, each its own store,
    ``<vault>/.knowledge_graph/graphs/<id>/graph.sqlite``, reading only the
    documents inside the folders chosen for it (inside data_in, or outside
    the vault - those are added to that graph as read-only folders) and,
    when given, only some file types.

Every graph has its own "what to look for" (the model's extraction prompt,
Morphik's prompt override) and its own setting for where a model's links go,
both kept in its store; and a "use in answers" flag kept here, read by the
Council's answers (council_core.kg_query) - several graphs may be used.

The list is ``<vault>/.knowledge_graph/graphs.json``, written atomically. A
damaged list is reported (GraphListDamaged) and left as it is; nothing is
written over it. Deleting a graph removes only its own folder under
``.knowledge_graph/graphs`` - never a document. No pickle; local only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from council_core import knowledge_graph as kgm

MAIN = "main"
LIST_NAME = "graphs.json"
GRAPHS_DIR = "graphs"
MAIN_NAME = "All documents"


class GraphListDamaged(RuntimeError):
    """graphs.json could not be read: it is left as it is."""


@dataclass
class GraphInfo:
    id: str
    name: str
    #: absolute folders whose documents this graph reads (empty: all, main)
    folders: List[str] = field(default_factory=list)
    #: file types it reads ('.pdf', '.md' ...); empty: every type it can
    suffixes: List[str] = field(default_factory=list)
    use_in_answers: bool = True
    created: float = 0.0

    @property
    def is_main(self) -> bool:
        return self.id == MAIN


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", kgm.fold(name)).strip("-")[:24]
    return s or "graph"


class GraphList:
    """The vault's graphs. Cheap to make: it reads graphs.json each time."""

    def __init__(self, vault: Any):
        self.vault = Path(vault)
        self.dir = self.vault / kgm.STORE_DIR
        self.path = self.dir / LIST_NAME

    # ── the list ──
    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "main": {"use_in_answers": True}, "graphs": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("graphs", []), list):
                raise ValueError("not a list of graphs")
        except (OSError, ValueError) as exc:
            raise GraphListDamaged(
                f"the list of knowledge graphs {self.path} could not be read ({exc}); "
                "it was left untouched") from exc
        data.setdefault("main", {"use_in_answers": True})
        data.setdefault("graphs", [])
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> List[GraphInfo]:
        data = self._load()
        main = GraphInfo(MAIN, MAIN_NAME,
                         use_in_answers=bool(data["main"].get("use_in_answers", True)))
        out = [main]
        for g in data["graphs"]:
            try:
                out.append(GraphInfo(id=str(g["id"]), name=str(g["name"]),
                                     folders=[str(x) for x in g.get("folders") or []],
                                     suffixes=[str(x) for x in g.get("suffixes") or []],
                                     use_in_answers=bool(g.get("use_in_answers", True)),
                                     created=float(g.get("created") or 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def get(self, gid: str) -> GraphInfo:
        for g in self.all():
            if g.id == gid:
                return g
        raise KeyError(gid)

    def create(self, name: str, folders: Sequence[Any], suffixes: Sequence[str] = (),
               *, use_in_answers: bool = True, focus: str = "") -> GraphInfo:
        """A new named graph over these folders. Nothing is read until it is
        built (Connections › ⟳ Build graph)."""
        name = " ".join(str(name or "").split())
        if not name:
            raise ValueError("a graph needs a name")
        folders = [str(Path(f).expanduser().resolve()) for f in folders if str(f).strip()]
        if not folders:
            raise ValueError("choose at least one folder for the graph to read")
        data_in = self.vault / "data_in"
        for f in folders:
            if not Path(f).is_dir():
                raise ValueError(f"{f} is not a folder")
            if kgm._overlaps(f, self.vault) and not kgm._overlaps(f, data_in, inside_only=True):
                raise ValueError(f"{f}: choose folders inside data_in, or outside the vault")
        data = self._load()
        if any(kgm.fold(g["name"]) == kgm.fold(name) for g in data["graphs"]) \
                or kgm.fold(name) == kgm.fold(MAIN_NAME):
            raise ValueError(f"there is a graph called {name} already")
        gid = f"{_slug(name)}-{hashlib.sha256(f'{name}{time.time()}'.encode()).hexdigest()[:6]}"
        info = GraphInfo(gid, name, folders,
                         sorted({("." + s.lstrip(".")).lower() for s in suffixes if s}),
                         use_in_answers, time.time())
        data["graphs"].append(asdict(info))
        self._save(data)
        with self.open(gid) as kg:
            if focus:
                kg.set_focus(focus)
        return info

    def update(self, gid: str, *, name: Optional[str] = None,
               use_in_answers: Optional[bool] = None,
               suffixes: Optional[Sequence[str]] = None) -> GraphInfo:
        data = self._load()
        if gid == MAIN:
            if use_in_answers is not None:
                data["main"]["use_in_answers"] = bool(use_in_answers)
            self._save(data)
            return self.get(MAIN)
        for g in data["graphs"]:
            if g.get("id") == gid:
                if name is not None and " ".join(name.split()):
                    g["name"] = " ".join(name.split())
                if use_in_answers is not None:
                    g["use_in_answers"] = bool(use_in_answers)
                if suffixes is not None:
                    g["suffixes"] = sorted({("." + s.lstrip(".")).lower()
                                            for s in suffixes if s})
                self._save(data)
                return self.get(gid)
        raise KeyError(gid)

    def delete(self, gid: str) -> None:
        """Remove a named graph: its entry and its own store folder (what
        building it made). Documents are never touched; "All documents"
        cannot be deleted."""
        if gid == MAIN:
            raise ValueError("the graph of all documents cannot be deleted")
        data = self._load()
        keep = [g for g in data["graphs"] if g.get("id") != gid]
        if len(keep) == len(data["graphs"]):
            raise KeyError(gid)
        folder = self.store_dir(gid)
        # only ever a folder inside .knowledge_graph/graphs
        if folder.parent == (self.dir / GRAPHS_DIR) and folder.is_dir():
            shutil.rmtree(folder)
        data["graphs"] = keep
        self._save(data)

    # ── stores ──
    def store_dir(self, gid: str) -> Path:
        if not re.fullmatch(r"[a-z0-9\-]{1,40}", gid or ""):
            raise ValueError(f"not a graph id: {gid!r}")
        return self.dir / GRAPHS_DIR / gid

    def open(self, gid: str = MAIN, root: Any = None, **kw) -> kgm.KnowledgeGraph:
        """The graph's store. A named graph's folders outside data_in are
        added to it (read-only) the first time it is opened."""
        if gid == MAIN:
            return kgm.KnowledgeGraph(self.vault, root, **kw)
        info = self.get(gid)
        kg = kgm.KnowledgeGraph(self.vault, root, store=self.store_dir(gid) / kgm.STORE_NAME,
                                scope={"folders": info.folders, "suffixes": info.suffixes},
                                **kw)
        try:
            have = {kgm._real(r["path"]) for r in kg.extra_roots()}
            for f in info.folders:
                if not kgm._overlaps(f, kg.root, inside_only=True) and \
                        kgm._real(f) not in have:
                    kg.add_root(f)
        except Exception:
            kg.close()
            raise
        return kg

    def for_answers(self) -> List[GraphInfo]:
        try:
            return [g for g in self.all() if g.use_in_answers]
        except GraphListDamaged:
            return [GraphInfo(MAIN, MAIN_NAME)]
