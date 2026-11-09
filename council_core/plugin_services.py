"""
council_core.plugin_services — what Council offers a plugin beyond its own
data folder: the first two services of the plugin API.

A plugin names the services it wants in its manifest's ``needs``; the
context Council hands it (council_core.app_plugins.PluginContext.service)
gives only those, made the first time they are asked for. Nothing here is
imported to LIST plugins.

    sources   the user's documents, read the way Connections reads them.
              A "notebook" is a named Connections graph over folders the
              user picked (council_core.kg_graphs): making one, building it
              (the same rebuild as Connections' Build graph), its documents,
              its passages of text, a ranked search over them, and the spot
              a citation points at. A notebook made here is NOT used in the
              Council's own answers unless the user ticks that in
              Connections: studying a subject should not change what the
              Council answers about the user's data.
    models    the local models a plugin may ask: US-origin only (council_
              core.local_models, as the Ledger and Connections lists), Ollama
              on this PC and Council's GGUF files, and ``chat(model)``: a
              function that sends messages to that model and returns its
              reply. Offline: localhost Ollama or a file loaded in process.

No Qt here. Each call opens what it reads and closes it, so a worker thread
may call any of it.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: The services a plugin may name in ``needs``.
NAMES: frozenset = frozenset({"sources", "models"})

#: Passage size for reading documents as text (lines per passage, overlap).
PASSAGE_LINES = 30
PASSAGE_OVERLAP = 4

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
#: Words too common to rank by.
_STOP = frozenset("""a an and are as at be been but by can could did do does for
from had has have he her his how i if in into is it its may me more most my no
not of on or our she so some such than that the their them then there these
they this those to was we were what when where which while who why will with
would you your""".split())


def words(text: str) -> List[str]:
    """The words of ``text`` for ranking: lower case, stop words dropped."""
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text or ""))
            if w not in _STOP and (len(w) > 1 or w.isdigit())]


# ======================================================================
# Passages and citations
# ======================================================================
@dataclass
class Passage:
    """A passage of one document: where it is and what it says."""
    document_id: str
    path: str                    # the store's path (data_in-relative or '<id>/...')
    display: str                 # the path as a person reads it
    page: Any                    # a PDF page number, a sheet name, or None
    line_nos: List[int]
    text: str
    chunk: int = 0
    score: float = 0.0

    @property
    def key(self) -> str:
        """Stable within one version of the document."""
        return f"{self.document_id}:{self.chunk}"

    @property
    def first_line(self) -> int:
        return self.line_nos[0] if self.line_nos else 0

    @property
    def last_line(self) -> int:
        return self.line_nos[-1] if self.line_nos else 0

    def where(self) -> str:
        """'p3 L10-L38', 'sheet Notes L2-L9', 'L1-L30'."""
        if isinstance(self.page, int):
            at = f"p{self.page} "
        elif self.page:
            at = f"{self.page} "
        else:
            at = ""
        if self.first_line == self.last_line:
            return f"{at}L{self.first_line}"
        return f"{at}L{self.first_line}-L{self.last_line}"

    def cite(self) -> str:
        """'Papers/smith2021.pdf, p3 L10-L38'."""
        return f"{self.display}, {self.where()}"

    def locator(self, line: Optional[int] = None) -> Dict[str, Any]:
        """The graph's locator for a line of this passage (its first by
        default), as knowledge_graph.source_context reads it."""
        loc: Dict[str, Any] = {"line": int(line or self.first_line)}
        if isinstance(self.page, int):
            loc["page"] = self.page
        elif self.page:
            loc["sheet"] = self.page
        return loc

    def to_dict(self) -> Dict[str, Any]:
        return {"document_id": self.document_id, "path": self.path,
                "display": self.display, "page": self.page,
                "line_nos": list(self.line_nos), "text": self.text,
                "chunk": self.chunk}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Passage":
        return cls(document_id=str(d.get("document_id", "")),
                   path=str(d.get("path", "")),
                   display=str(d.get("display") or d.get("path", "")),
                   page=d.get("page"),
                   line_nos=[int(x) for x in d.get("line_nos") or []],
                   text=str(d.get("text", "")), chunk=int(d.get("chunk") or 0))


class PassageIndex:
    """BM25 over a notebook's passages: no model, nothing to download, and
    fast enough for thousands of passages."""

    K1 = 1.4
    B = 0.75

    def __init__(self, passages: Sequence[Passage]):
        self.passages = list(passages)
        self._tf: List[Counter] = []
        self._len: List[int] = []
        df: Counter = Counter()
        for p in self.passages:
            tf = Counter(words(p.text + " " + p.display))
            self._tf.append(tf)
            self._len.append(sum(tf.values()))
            df.update(tf.keys())
        n = max(1, len(self.passages))
        self._avg = (sum(self._len) / n) if self._len else 1.0
        self._idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def search(self, query: str, k: int = 8, *,
               documents: Optional[Sequence[str]] = None,
               exclude: Sequence[str] = ()) -> List[Passage]:
        """The ``k`` passages best matching ``query`` (best first), each with
        its score; only those of ``documents`` (ids) when given."""
        terms = [w for w in dict.fromkeys(words(query)) if w in self._idf]
        if not terms:
            return []
        keep = set(documents) if documents else None
        skip = set(exclude)
        scored: List[Tuple[float, int]] = []
        for i, p in enumerate(self.passages):
            if keep is not None and p.document_id not in keep:
                continue
            if p.key in skip:
                continue
            tf, dl = self._tf[i], self._len[i] or 1
            s = 0.0
            for w in terms:
                f = tf.get(w, 0)
                if f:
                    s += self._idf[w] * f * (self.K1 + 1) / (
                        f + self.K1 * (1 - self.B + self.B * dl / self._avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out = []
        for s, i in scored[:max(0, k)]:
            p = self.passages[i]
            out.append(Passage(p.document_id, p.path, p.display, p.page,
                               list(p.line_nos), p.text, p.chunk, round(s, 3)))
        return out


# ======================================================================
# sources
# ======================================================================
class NotBuilt(LookupError):
    """A notebook that has never been built has nothing to read yet."""


@dataclass
class Notebook:
    id: str
    name: str
    folders: List[str] = field(default_factory=list)
    suffixes: List[str] = field(default_factory=list)
    is_main: bool = False
    use_in_answers: bool = False


class Sources:
    """The ``sources`` service. ``vault`` and ``root`` are for tests; Council
    passes neither and the user's vault is used."""

    def __init__(self, vault: Any = None, root: Any = None):
        from council_core import paths
        self.vault = Path(vault) if vault else paths.vault_dir()
        self.root = Path(root) if root else self.vault / "data_in"
        self._lock = threading.Lock()
        #: notebook id -> (signature, PassageIndex)
        self._cache: Dict[str, Tuple[str, PassageIndex]] = {}

    # -- notebooks --
    def _list(self):
        from council_core import kg_graphs as kgg
        return kgg.GraphList(self.vault)

    def notebooks(self) -> List[Notebook]:
        """Every Connections graph, "All documents" first."""
        return [Notebook(g.id, g.name, list(g.folders), list(g.suffixes),
                         g.is_main, g.use_in_answers) for g in self._list().all()]

    def notebook(self, nid: str) -> Notebook:
        for nb in self.notebooks():
            if nb.id == nid:
                return nb
        raise KeyError(nid)

    def create_notebook(self, name: str, folders: Sequence[Any],
                        suffixes: Sequence[str] = ()) -> Notebook:
        """A new notebook over these folders (a named Connections graph,
        not used in the Council's answers). Nothing is read until
        ``build``."""
        g = self._list().create(name, folders, suffixes, use_in_answers=False)
        return Notebook(g.id, g.name, list(g.folders), list(g.suffixes), False, False)

    def delete_notebook(self, nid: str) -> None:
        """Remove a notebook's own store (never a document; never "All
        documents")."""
        self._list().delete(nid)
        with self._lock:
            self._cache.pop(nid, None)

    def build(self, nid: str, listener: Optional[Callable[[str, float], None]] = None
              ) -> Dict[str, Any]:
        """Read the notebook's folders: Connections' rebuild of that graph
        (only new or changed files are read again). Its stats."""
        with self._list().open(nid, self.root) as kg:
            stats = kg.seed(listener=listener)
        with self._lock:
            self._cache.pop(nid, None)
        return stats

    def _open(self, nid: str):
        """The notebook's store for reading. NotBuilt when it has never been
        built."""
        from council_core import knowledge_graph as kgm
        try:
            return self._list().open(nid, self.root, readonly=True)
        except kgm.KnowledgeGraphDamaged as exc:
            if "has not been built" in str(exc):
                raise NotBuilt(f"{nid} has not been built yet") from exc
            raise

    def built(self, nid: str) -> bool:
        """Whether the notebook has been built at least once (making one
        opens its store; building it writes the graph's signature)."""
        try:
            with self._open(nid) as kg:
                return kg.meta("graph_sig") is not None
        except NotBuilt:
            return False

    def documents(self, nid: str) -> List[Dict[str, Any]]:
        """The notebook's documents as read at its last build: id, path,
        display path, status ('ok' or why not)."""
        try:
            with self._open(nid) as kg:
                rows = kg.db.execute("SELECT id, path, status FROM documents"
                                     " ORDER BY path COLLATE NOCASE").fetchall()
                return [{"document_id": r["id"], "path": r["path"],
                         "display": kg.display_path(r["path"]),
                         "status": r["status"]} for r in rows]
        except NotBuilt:
            return []

    # -- passages --
    def _signature(self, kg) -> str:
        import hashlib
        h = hashlib.sha256()
        for r in kg.db.execute("SELECT id, content_hash FROM documents"
                               " WHERE status='ok' ORDER BY id"):
            h.update(f"{r['id']}={r['content_hash']};".encode())
        return h.hexdigest()

    def index(self, nid: str) -> PassageIndex:
        """The notebook's passages, ranked by BM25; rebuilt only when a
        document changed since the last call."""
        try:
            kg = self._open(nid)
        except NotBuilt:
            return PassageIndex([])
        with kg:
            sig = self._signature(kg)
            with self._lock:
                have = self._cache.get(nid)
            if have and have[0] == sig:
                return have[1]
            got = [Passage(document_id=p["document_id"], path=p["path"],
                           display=p["display"], page=p["page"],
                           line_nos=list(p["line_nos"]), text=p["text"],
                           chunk=p["chunk"])
                   for p in kg.text_passages(PASSAGE_LINES, PASSAGE_OVERLAP)]
        idx = PassageIndex(got)
        with self._lock:
            self._cache[nid] = (sig, idx)
        return idx

    def passages(self, nid: str, document_id: Optional[str] = None) -> List[Passage]:
        ps = self.index(nid).passages
        return [p for p in ps if document_id is None or p.document_id == document_id]

    def search(self, nid: str, query: str, k: int = 8, *,
               documents: Optional[Sequence[str]] = None,
               exclude: Sequence[str] = ()) -> List[Passage]:
        return self.index(nid).search(query, k, documents=documents, exclude=exclude)

    # -- where a citation points --
    def abs_path(self, nid: str, path: str) -> Path:
        """Where a document of the notebook is on disk."""
        with self._open(nid) as kg:
            return kg.abs_path(path)

    def context(self, nid: str, path: str, locator: Dict[str, Any], *,
                radius: int = 3, quote: str = "") -> List[Tuple[str, str, bool]]:
        """The lines around a cited spot, ``[(gutter, text, is_cited)]``
        (knowledge_graph.source_context). Read-only."""
        from council_core import knowledge_graph as kgm
        p = self.abs_path(nid, path)
        return kgm.source_context(p.parent, p.name, locator, radius=radius, quote=quote)


# ======================================================================
# models
# ======================================================================
ChatFn = Callable[..., str]


class Models:
    """The ``models`` service: the US-origin local models, and a chat
    function for one of them."""

    def available(self) -> List[Tuple[str, str]]:
        """``[(label, model id)]``: Ollama models on this PC, then GGUF
        files (the id is the file's path). US-origin only; an unknown origin
        is not US."""
        from council_core import local_models as lm
        out: List[Tuple[str, str]] = []
        try:
            import council_engine as ce
            tags = ce._ollama_tags("http://127.0.0.1:11434") or {}
        except Exception:                                  # noqa: BLE001
            tags = {}
        for m in sorted(tags.get("models", []) or [], key=lambda m: m.get("name", "")):
            det = m.get("details") or {}
            name = m.get("name", "")
            if name and lm.maker_and_origin(name, str(det.get("family") or ""),
                                            det.get("families") or ())[1] == "US":
                out.append((name, name))
        try:
            files = lm.list_local_models(include_ollama=False)
        except Exception:                                  # noqa: BLE001
            files = []
        for e in files:
            if e.get("path") and e.get("origin") == "US":
                out.append((f"GGUF: {e.get('name') or e.get('file')}", str(e["path"])))
        return out

    @staticmethod
    def is_us(model: str) -> bool:
        from council_core import local_models as lm
        name = Path(model).name if str(model).lower().endswith(".gguf") else model
        return lm.maker_and_origin(str(name or ""))[1] == "US"

    def chat(self, model: str) -> ChatFn:
        """``chat(messages, *, max_tokens=800, schema=None) -> str`` on
        ``model`` (an Ollama name or a GGUF path), temperature 0. ``schema``
        constrains the reply to that JSON schema. A model that is not of US
        origin is refused (ValueError)."""
        if not model:
            raise ValueError("choose a model")
        if not self.is_us(model):
            raise ValueError(f"{model} is not a US-made model; Council only "
                             f"uses US-made models")
        from council_core import kg_extract as kx
        inner = kx.gguf_chat(model) if kx.is_gguf(model) else kx.ollama_chat(model)

        def chat(messages, *, max_tokens: int = 800, schema: Any = None) -> str:
            return inner(list(messages), num_predict=int(max_tokens), schema=schema)
        return chat


# ======================================================================
# Making a service
# ======================================================================
def make(name: str) -> Any:
    """A new instance of the service ``name`` (one of NAMES)."""
    if name == "sources":
        return Sources()
    if name == "models":
        return Models()
    raise KeyError(f"Council offers no service called {name!r}")
