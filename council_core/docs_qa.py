"""
council_core.docs_qa — answer a question, or write code, from documentation.

THE COUNCIL RETRIEVES; THE MODEL ONLY READS AND WRITES
Free-form tool use ("here are search and fetch, go") is what a 70B model does
well and a 4-8B model does badly: it searches for the wrong thing, fetches
nothing, or stops calling tools and answers from memory. So the default path
is ORCHESTRATED, and the model makes at most two kinds of small call:

  1. derive 1-3 search queries   — a JSON-schema-constrained call, ~120 tokens
                                   out; a no-model keyword query is always
                                   added, and used alone if the model fails
  2. search every enabled server — the Council calls the search tool
  3. fetch the best pages        — the Council calls the fetch tool (or
                                   resources/read), and trims each page to the
                                   paragraphs that match the question
  4. answer from those pages     — JSON-schema-constrained
                                   {answer, sources[], covered, code?}

Native tool calling (chat_tools) is an option for models that do it well; it
is not the default, and it is measured by the same benchmark.

NEVER INVENT A SOURCE
Sources are the pages the Council fetched, numbered by the Council. A model
can only point at them: the schema limits `sources` to those numbers, and any
[n] in the text that is not one of them is removed with a note saying so. When
retrieval finds nothing, the answer is "The documentation doesn't cover this."
WITHOUT asking a model — a model handed no documentation answers from memory,
which is exactly the failure this module exists to prevent.

CODE IS CHECKED AGAINST THE DOCS IT WAS WRITTEN FROM
"Write code" returns one Python block that is ast-parsed and then checked
against the fetched pages: every name used from the package must appear in
them, every keyword passed to a documented function must be one of its
documented parameters, and every name must be defined or imported. Problems go
back to the model as exact text ("line 4: Ledger.push is not in the
documentation you were given — did you mean append?") for up to two repairs.
What still fails is shown to the user, flagged, never silently dropped.

MODELS ARE INJECTED
`model_call(messages, *, json_schema, temperature, num_predict, seed,
should_stop) -> str`. The default calls council_engine.local_chat with the
"docs" role. Arguments the engine does not support yet (json_schema on an
older engine) are dropped and the reply parsed leniently instead — so this
module works before and after the engine gains constrained decoding.
"""
from __future__ import annotations

import ast
import builtins
import difflib
import inspect
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, \
    Set, Tuple

from . import docs_servers
from .docs_servers import ServerSpec
from .mcp_client import McpError, ToolResult, content_text

NOT_COVERED = "The documentation doesn't cover this."
DOCS_ROLE = "docs"
#: An unassigned docs role borrows the coder's model when the user chose one:
#: answering from documentation and writing code from it is the coder's job
#: more than the writer's.
FALLBACK_ROLES = ("coder",)

#: How much documentation goes into the answer prompt, in characters. 6000
#: chars is ~2.2k tokens at the 2.69 chars/token measured for phi3.5's
#: tokenizer — with the question, rules and a 700-token reply it fits a 4096
#: window, the smallest any council slot is loaded with.
CONTEXT_CHARS = 6000
MAX_PAGES = 3
MAX_QUERIES = 3
MAX_REPAIRS = 2

ANSWER_TEMPERATURE = 0.1
ANSWER_NUM_PREDICT = 700
CODE_NUM_PREDICT = 1100
QUERY_NUM_PREDICT = 120
SEED = 7

STOPWORDS = set("""
a an and are as at be by can could do does did for from get got have how i if
in into is it its me my of on or the that this to use used using what when
where which who why will with would you your want need make should please
python package module function method class call example code write show give
tell return returns value values way list there their them they these those
than then also just only about any some all each every one two three first
""".split())

ModelCall = Callable[..., str]
Progress = Optional[Callable[[str], None]]
ShouldStop = Optional[Callable[[], bool]]


class Stopped(Exception):
    """The user pressed Stop."""


# ======================================================================
# Results
# ======================================================================

@dataclass
class Source:
    """One fetched page, numbered as the model sees it."""
    n: int
    server: str
    ref: str
    title: str
    text: str
    uri: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Hit:
    server: str
    ref: str
    title: str
    snippet: str = ""
    uri: str = ""
    text: str = ""          # the hit already IS the page (no fetch tool)
    score: float = 0.0


@dataclass
class DocsAnswer:
    ok: bool = False
    covered: bool = False
    answer: str = ""
    #: Every page the model was shown, numbered.
    sources: List[Source] = field(default_factory=list)
    #: The numbers the answer actually cites (a subset of sources).
    cited: List[int] = field(default_factory=list)
    code: str = ""
    code_ok: Optional[bool] = None
    code_issues: List[str] = field(default_factory=list)
    queries: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    timings: Dict[str, float] = field(default_factory=dict)
    model_calls: int = 0
    constrained: Optional[bool] = None
    mode: str = "orchestrated"
    role: str = ""
    stopped: bool = False
    error: str = ""
    raw: str = ""

    @property
    def grounded(self) -> bool:
        return self.covered and bool(self.cited)

    @property
    def code_block(self) -> str:
        return f"```python\n{self.code.rstrip()}\n```" if self.code else ""

    def cited_sources(self) -> List[Source]:
        return [s for s in self.sources if s.n in self.cited]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["grounded"] = self.grounded
        return d


# ======================================================================
# Calling a model (whatever the engine supports)
# ======================================================================

_INFO = threading.local()


def _accepted(fn: Callable) -> Optional[Set[str]]:
    """Keyword names `fn` accepts; None when it takes **kwargs or cannot be
    inspected (then everything is tried)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
        return None
    return {n for n, p in sig.parameters.items()
            if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}


def call_supported(fn: Callable, *args, **kwargs) -> Tuple[Any, List[str]]:
    """Call `fn`, dropping keyword arguments it does not take.

    The shared contract adds json_schema/seed/stop/should_stop to local_chat,
    but this branch must also run against an engine without them. The
    signature is read first — retrying on TypeError alone would re-run a
    whole generation when the TypeError came from INSIDE the call — and
    "unexpected keyword argument" is still caught for wrappers whose
    signature lies."""
    dropped: List[str] = []
    accepted = _accepted(fn)
    if accepted is not None:
        for k in list(kwargs):
            if k not in accepted:
                kwargs.pop(k)
                dropped.append(k)
    for _ in range(len(kwargs) + 1):
        try:
            return fn(*args, **kwargs), dropped
        except TypeError as exc:
            m = re.search(r"unexpected keyword argument '(\w+)'", str(exc))
            if not m or m.group(1) not in kwargs:
                raise
            kwargs.pop(m.group(1))
            dropped.append(m.group(1))
    return fn(*args, **kwargs), dropped


def answering_role(config: Any = None) -> str:
    """The role whose model answers docs questions.

    "docs" when the user assigned it; else a fallback role they did assign
    ("coder"); else "docs" anyway, which the engine answers from the main
    model."""
    try:
        from . import model_slots
        cfg = config if config is not None else model_slots.current()
        roles = getattr(cfg, "roles", {}) or {}
        if DOCS_ROLE in roles:
            return DOCS_ROLE
        for r in FALLBACK_ROLES:
            if r in roles:
                return r
    except Exception:                                     # noqa: BLE001
        pass
    return DOCS_ROLE


def last_call_info() -> dict:
    """What the most recent engine call on THIS thread reported."""
    return dict(getattr(_INFO, "value", {}) or {})


def engine_model_call(*, role: Optional[str] = None,
                      model: Optional[str] = None,
                      timeout: float = 180.0) -> ModelCall:
    """A model_call that uses council_engine.local_chat.

    `role` None means "decide per call" (answering_role), so a model chosen in
    the Docs tab applies to the next question without rebuilding anything."""

    def call(messages: List[dict], *, json_schema: Optional[dict] = None,
             temperature: float = ANSWER_TEMPERATURE,
             num_predict: int = ANSWER_NUM_PREDICT,
             seed: Optional[int] = None, stop: Optional[List[str]] = None,
             should_stop: ShouldStop = None) -> str:
        import council_engine
        use_role = role or answering_role()
        kwargs: Dict[str, Any] = {"temperature": temperature,
                                  "num_predict": num_predict,
                                  "timeout": int(timeout), "role": use_role}
        if model:
            kwargs["model"] = model
        for k, v in (("json_schema", json_schema), ("seed", seed),
                     ("stop", stop), ("should_stop", should_stop)):
            if v is not None:
                kwargs[k] = v
        text, dropped = call_supported(council_engine.local_chat, messages,
                                       **kwargs)
        stats: dict = {}
        getter = getattr(council_engine, "last_call_stats", None)
        if callable(getter):
            try:
                stats = dict(getter(use_role) or {})
            except Exception:                             # noqa: BLE001
                stats = {}
        if json_schema is not None and "json_schema" in dropped:
            stats["constrained"] = False
        _INFO.value = {"dropped": dropped, "stats": stats, "role": use_role}
        return str(text or "")

    call.role = role                                      # type: ignore
    return call


def _model(model_call: ModelCall, messages: List[dict], *,
           json_schema: Optional[dict], temperature: float, num_predict: int,
           seed: Optional[int], should_stop: ShouldStop) -> Tuple[str, dict]:
    _INFO.value = {}
    kwargs: Dict[str, Any] = {"temperature": temperature,
                              "num_predict": num_predict}
    for k, v in (("json_schema", json_schema), ("seed", seed),
                 ("should_stop", should_stop)):
        if v is not None:
            kwargs[k] = v
    text, dropped = call_supported(model_call, messages, **kwargs)
    info = last_call_info()
    info.setdefault("dropped", [])
    info["dropped"] = sorted(set(info["dropped"]) | set(dropped))
    stats = info.setdefault("stats", {})
    if json_schema is not None and "json_schema" in info["dropped"]:
        stats["constrained"] = False
    return str(text or ""), info


# ======================================================================
# Words
# ======================================================================

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed") and not w.endswith("eed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def content_terms(text: str, drop: Iterable[str] = ()) -> List[str]:
    """Stemmed content words, identifiers split, stopwords and `drop`
    (package names) removed. The same rules the bundled server indexes with,
    so "a word matched" means the same thing on both sides."""
    dropped = {d.lower() for d in drop}
    out: List[str] = []
    for word in _IDENT.findall(text or ""):
        lower = word.lower()
        if lower in dropped:
            continue
        parts = [p.lower() for chunk in word.split("_") if chunk
                 for p in _CAMEL.findall(chunk)]
        for p in parts:
            if p not in STOPWORDS and p not in dropped and len(p) > 1:
                out.append(_stem(p))
    return out


def keyword_query(question: str, packages: Sequence[str] = ()) -> str:
    """The no-model query: identifiers first, then content words.

    Identifiers (snake_case, CamelCase, dotted, `backticked`) are what a
    documentation index matches best, so they lead."""
    idents: List[str] = []
    for tok in re.findall(r"`([^`]+)`|([A-Za-z_][A-Za-z0-9_.]*)",
                          question or ""):
        word = (tok[0] or tok[1]).strip(".")
        if not word:
            continue
        looks_code = ("_" in word or "." in word or tok[0]
                      or re.search(r"[a-z][A-Z]", word))
        if looks_code and word.lower() not in {p.lower() for p in packages}:
            idents.append(word)
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", question or "")
             if w.lower() not in STOPWORDS
             and w.lower() not in {p.lower() for p in packages}]
    seen, out = set(), []
    for w in idents + words:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return " ".join(out[:10])


def parse_packages(text: Any) -> List[str]:
    if isinstance(text, (list, tuple)):
        items = [str(t) for t in text]
    else:
        items = re.split(r"[,\s]+", str(text or ""))
    return [i.strip() for i in items if i.strip()]


# ======================================================================
# Step 1: queries
# ======================================================================

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES,
                    "items": {"type": "string", "minLength": 2,
                              "maxLength": 60}},
        "package": {"type": "string", "maxLength": 40},
    },
    "required": ["queries", "package"],
    "additionalProperties": False,
}


def query_messages(question: str, packages: Sequence[str]) -> List[dict]:
    pk = f"\nPackage: {', '.join(packages)}" if packages else ""
    return [
        {"role": "system",
         "content": "You turn a question about a Python package into short "
                    "documentation search queries."},
        {"role": "user",
         "content": f"Question: {question.strip()}{pk}\n\n"
                    "Reply with JSON: {\"queries\": [1 to 3 short searches, "
                    "each a few words or the likely function, class or "
                    "parameter name], \"package\": \"the top-level Python "
                    "package the question is about, or \\\"\\\"\"}"},
    ]


def derive_queries(question: str, packages: Sequence[str] = (),
                   model_call: Optional[ModelCall] = None, *,
                   should_stop: ShouldStop = None
                   ) -> Tuple[List[str], str, dict]:
    """(queries, package, info). Never raises for a model fault: the keyword
    query is always there, and is all there is when the model fails."""
    kw = keyword_query(question, packages)
    queries: List[str] = []
    package = ""
    info: dict = {}
    if model_call is not None:
        try:
            text, info = _model(model_call, query_messages(question, packages),
                                json_schema=QUERY_SCHEMA, temperature=0.0,
                                num_predict=QUERY_NUM_PREDICT, seed=SEED,
                                should_stop=should_stop)
            obj = _json_object(text)
            if isinstance(obj, dict):
                for q in obj.get("queries") or []:
                    q = " ".join(str(q).split())[:80]
                    if len(q) >= 2:
                        queries.append(q)
                package = str(obj.get("package") or "").strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package):
                    package = ""
            info["ok"] = bool(queries)
        except Stopped:
            raise
        except Exception as exc:                          # noqa: BLE001
            if should_stop is not None and should_stop():
                raise Stopped() from None
            info = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out: List[str] = []
    for q in queries[:MAX_QUERIES - 1] + [kw] + queries[MAX_QUERIES - 1:]:
        if q and q.lower() not in {o.lower() for o in out}:
            out.append(q)
    return out[:MAX_QUERIES], package, info


# ======================================================================
# Steps 2-3: search and fetch
# ======================================================================

def _hits_from_items(server: str, items: Sequence[Any]) -> List[Hit]:
    hits = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = next((str(item[k]) for k in ("name", "id", "ref", "uri", "url",
                                           "path", "libraryId", "title")
                    if item.get(k)), "")
        if not ref:
            continue
        title = str(item.get("title") or item.get("name") or ref)
        snippet = " ".join(str(item.get(k) or "") for k in (
            "signature", "summary", "description", "snippet")).strip()
        text = str(item.get("text") or item.get("content") or "")
        uri = str(item.get("uri") or "")
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        hits.append(Hit(server, ref, title, snippet, uri, text, score))
    return hits


RESULT_KEYS = ("results", "hits", "items", "matches", "documents", "docs")
_NO_RESULTS = re.compile(r"\s*(no (results|documentation|matches|match|"
                         r"documents|pages)\b|nothing (was )?found|"
                         r"0 results)", re.I)


def parse_search(server: str, result: ToolResult) -> List[Hit]:
    """Hits from whatever shape a search tool returned.

    structuredContent first (the bundled server's), then a JSON list in the
    text, then resource links / embedded resources, and last the text
    itself, split into blocks — a server whose search returns documentation
    directly (no separate fetch) lands here, and each block is a page."""
    s = result.structured
    if isinstance(s, dict):
        for key in RESULT_KEYS:
            value = s.get(key)
            if isinstance(value, list) and all(isinstance(v, dict)
                                               for v in value):
                # Trusted even when empty: an empty result list is the
                # server saying "nothing", and its text ("No documentation
                # matches 'zebra'") must not become a page that echoes the
                # question back and passes the relevance check.
                return _hits_from_items(server, value)
        for value in s.values():
            if isinstance(value, list) and value and isinstance(value[0],
                                                                 dict):
                hits = _hits_from_items(server, value)
                if hits:
                    return hits
    text = result.text
    if _NO_RESULTS.match(text or ""):
        return []
    obj = _json_any(text)
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, list) and value and isinstance(value[0],
                                                                 dict):
                obj = value
                break
    if isinstance(obj, list):
        hits = _hits_from_items(server, obj)
        if hits:
            return hits
    hits = []
    for item in result.content:
        if item.get("type") == "resource_link" and item.get("uri"):
            hits.append(Hit(server, str(item["uri"]),
                            str(item.get("title") or item.get("name")
                                or item["uri"]),
                            str(item.get("description") or ""),
                            uri=str(item["uri"])))
        elif item.get("type") == "resource":
            res = item.get("resource") or {}
            if res.get("text"):
                hits.append(Hit(server, str(res.get("uri") or ""),
                                str(res.get("uri") or "resource"),
                                text=str(res["text"])))
    if hits:
        return hits
    blocks = [b.strip() for b in re.split(r"\n\s*(?:-{3,}|={3,})\s*\n|\n{2,}",
                                          text or "") if b.strip()]
    for i, b in enumerate(blocks[:8]):
        first = b.split("\n", 1)[0].strip().lstrip("#").strip()
        hits.append(Hit(server, f"{server}#{i + 1}", first[:120] or
                        f"result {i + 1}", text=b))
    return hits


def _search_args(roles: docs_servers.Roles, query: str,
                 package: str) -> Dict[str, Any]:
    args: Dict[str, Any] = {roles.search_arg: query}
    if roles.package_arg and package:
        args[roles.package_arg] = package
    return args


def focus(text: str, terms: Sequence[str], limit: int) -> str:
    """Trim a page to `limit` chars, keeping its head and its best parts.

    The head carries the signature and summary; the rest of the budget goes
    to the paragraphs with the most question words, in page order. A numpy
    page is mostly Examples — cutting at a fixed length would keep the
    examples of the first half and lose the parameter the question asked
    about."""
    text = text or ""
    if len(text) <= limit:
        return text
    paras = re.split(r"\n\s*\n", text)
    head_budget = limit // 2
    keep: List[int] = []
    used = 0
    for i, p in enumerate(paras):
        if used + len(p) + 2 > head_budget:
            break
        keep.append(i)
        used += len(p) + 2
    want = set(terms)
    scored = []
    for i, p in enumerate(paras):
        if i in keep:
            continue
        hits = sum(1 for t in content_terms(p) if t in want)
        if hits:
            scored.append((-hits, i))
    for _neg, i in sorted(scored):
        size = len(paras[i]) + 2
        if used + size > limit:
            continue
        keep.append(i)
        used += size
    if not keep:
        return text[:limit - 1] + "…"
    keep.sort()
    out, last = [], -1
    for i in keep:
        if last >= 0 and i != last + 1:
            out.append("[…]")
        out.append(paras[i])
        last = i
    joined = "\n\n".join(out)
    if keep[-1] != len(paras) - 1:
        joined += "\n\n[…]"
    return joined[:limit]


@dataclass
class Retrieval:
    pages: List[Source] = field(default_factory=list)
    hits: List[Hit] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    search_s: float = 0.0
    fetch_s: float = 0.0
    servers_used: int = 0


def _servers(servers: Any, path=None) -> List[ServerSpec]:
    if servers is None:
        return [s for s in docs_servers.load(path) if s.enabled]
    out: List[ServerSpec] = []
    named = None
    for s in servers:
        if isinstance(s, ServerSpec):
            out.append(s)
        else:
            if named is None:
                named = {x.name: x for x in docs_servers.load(path)}
            if str(s) in named:
                out.append(named[str(s)])
    return out


def retrieve(question: str, queries: Sequence[str], *,
             servers: Any = None, packages: Sequence[str] = (),
             max_pages: int = MAX_PAGES, context_chars: int = CONTEXT_CHARS,
             should_stop: ShouldStop = None, progress: Progress = None,
             config_path=None) -> Retrieval:
    """Search every server with every query, fuse, fetch the best pages."""
    out = Retrieval()
    specs = _servers(servers, config_path)
    if not specs:
        out.notes.append("No documentation server is configured.")
        return out
    q_terms = content_terms(question, packages)
    scores: Dict[Tuple[str, str], float] = {}
    best: Dict[Tuple[str, str], Hit] = {}
    clients: Dict[str, Tuple[Any, docs_servers.Roles]] = {}
    t0 = time.perf_counter()
    for spec in specs:
        _check_stop(should_stop)
        if progress:
            progress(f"Searching {spec.name}…")
        try:
            client, roles = docs_servers.pooled(spec, should_stop=should_stop)
        except McpError as exc:
            _check_stop(should_stop)
            out.errors.append(f"{spec.name}: {exc}")
            continue
        if not roles.usable:
            out.errors.append(f"{spec.name}: no search tool was found.")
            continue
        clients[spec.name] = (client, roles)
        out.servers_used += 1
        pkgs = list(packages) or list(spec.packages) or [""]
        for q in queries:
            for pkg in pkgs[:3]:
                _check_stop(should_stop)
                try:
                    result = client.call_tool(
                        roles.search_tool, _search_args(roles, q, pkg),
                        timeout=spec.timeout, should_stop=should_stop)
                except McpError as exc:
                    _check_stop(should_stop)
                    out.errors.append(f"{spec.name}: search failed: {exc}")
                    continue
                if result.is_error:
                    msg = " ".join(result.text.split())[:240]
                    if msg and msg not in out.notes:
                        out.notes.append(f"{spec.name}: {msg}")
                    continue
                for rank, hit in enumerate(parse_search(spec.name, result)):
                    key = (spec.name, hit.ref)
                    # Reciprocal rank fusion: robust to servers whose scores
                    # are not comparable, and to queries of uneven quality.
                    scores[key] = scores.get(key, 0.0) + 1.0 / (10 + rank)
                    best.setdefault(key, hit)
    out.search_s = time.perf_counter() - t0
    if not best:
        return out

    def overlap(hit: Hit) -> int:
        words = set(content_terms(f"{hit.title} {hit.ref} {hit.snippet} "
                                  f"{hit.text[:400]}"))
        return sum(1 for t in set(q_terms) if t in words)

    ranked = sorted(best, key=lambda k: (-(scores[k] + 0.05 * overlap(
        best[k])), k))
    out.hits = [best[k] for k in ranked]
    chosen = [k for k in ranked if overlap(best[k]) > 0 or not q_terms]
    chosen = chosen[:max_pages]
    if not chosen:
        out.notes.append("The search found pages, but none mention anything "
                         "in the question.")
        return out
    t1 = time.perf_counter()
    budget = context_chars
    fetched: List[Tuple[Hit, str]] = []
    for k in chosen:
        _check_stop(should_stop)
        hit = best[k]
        client, roles = clients[hit.server]
        spec = next(s for s in specs if s.name == hit.server)
        if progress:
            progress(f"Reading {hit.title}…")
        text = hit.text
        if not text and roles.fetch_tool:
            try:
                res = client.call_tool(roles.fetch_tool,
                                       {roles.fetch_arg: hit.ref},
                                       timeout=spec.timeout,
                                       should_stop=should_stop)
                text = "" if res.is_error else res.text
                if res.is_error:
                    out.errors.append(f"{hit.server}: could not read "
                                      f"{hit.ref}: {res.text[:160]}")
            except McpError as exc:
                _check_stop(should_stop)
                out.errors.append(f"{hit.server}: could not read {hit.ref}: "
                                  f"{exc}")
        elif not text and roles.via_resources and hit.uri:
            try:
                text = content_text(client.read_resource(
                    hit.uri, timeout=spec.timeout, should_stop=should_stop))
            except McpError as exc:
                _check_stop(should_stop)
                out.errors.append(f"{hit.server}: could not read {hit.uri}: "
                                  f"{exc}")
        if not text:
            text = f"{hit.title}\n{hit.snippet}".strip()
        fetched.append((hit, text))
    out.fetch_s = time.perf_counter() - t1
    # Share the budget: a short page gives its unused share to the next.
    remaining = budget
    for i, (hit, text) in enumerate(fetched):
        share = remaining // max(1, len(fetched) - i)
        trimmed = focus(text, q_terms, max(400, share))
        remaining -= len(trimmed)
        out.pages.append(Source(len(out.pages) + 1, hit.server, hit.ref,
                                hit.title, trimmed, hit.uri))
    return out


def _check_stop(should_stop: ShouldStop) -> None:
    if should_stop is not None and should_stop():
        raise Stopped()


# ======================================================================
# Step 4: the answer
# ======================================================================

def answer_schema(n_sources: int, write_code: bool) -> dict:
    """{answer, sources, covered[, code]} with sources limited to the pages
    that exist.

    NO maxLength ON THE TEXT FIELDS, measured: llama.cpp's JSON-schema
    converter (llama-cpp-python 0.3.35) writes maxLength N as N nested
    optional groups, so `code` at 3000 made a 34 KB grammar three thousand
    parentheses deep, against 1.2 KB for the query schema. The length is
    bounded by num_predict instead, and a reply cut off by it is salvaged
    field by field (parse_answer) rather than thrown away."""
    props: Dict[str, Any] = {
        "answer": {"type": "string"},
        "sources": {"type": "array", "maxItems": max(1, n_sources),
                    "items": {"type": "integer",
                              "enum": list(range(1, max(1, n_sources) + 1))}},
        "covered": {"type": "boolean"},
    }
    required = ["answer", "sources", "covered"]
    if write_code:
        props["code"] = {"type": "string"}
        required.append("code")
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


SYSTEM_ANSWER = (
    "You answer questions about Python packages using ONLY the numbered "
    "documentation excerpts you are given. Cite the excerpts you used like "
    "[1]. If the excerpts do not contain the answer, set \"covered\" to false "
    "and say what is missing. Never use outside knowledge about the package "
    "and never invent functions, parameters or values.")

CODE_RULES = (
    "Write the code using only the functions, classes, methods and "
    "parameters shown in the documentation above. Import everything you use. "
    "Put the complete Python code in \"code\" as plain code (no ``` fences); "
    "put a one-sentence explanation with citations in \"answer\".")


def render_docs(pages: Sequence[Source]) -> str:
    return "\n\n".join(f"[{p.n}] {p.title}\n{p.text.strip()}" for p in pages)


def answer_messages(question: str, pages: Sequence[Source],
                    write_code: bool) -> List[dict]:
    shape = ('{"answer": "... [1]", "sources": [1], "covered": true'
             + (', "code": "..."' if write_code else "") + "}")
    task = "TASK" if write_code else "QUESTION"
    user = (f"DOCUMENTATION\n{render_docs(pages)}\n\n{task}\n"
            f"{question.strip()}\n\n"
            + (CODE_RULES + "\n" if write_code else "")
            + f"Reply with JSON only: {shape}")
    return [{"role": "system", "content": SYSTEM_ANSWER},
            {"role": "user", "content": user}]


def _json_object(text: str) -> Optional[dict]:
    obj = _json_any(text)
    return obj if isinstance(obj, dict) else None


def _json_any(text: str) -> Any:
    """json.loads, else the first balanced {...} or [...] in the text, with
    fences, comments and trailing commas forgiven — an unconstrained small
    model wraps JSON in all three."""
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    s = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", s)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(s)):
                ch = s[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        chunk = s[start:i + 1]
                        for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1",
                                                        chunk)):
                            try:
                                return json.loads(candidate)
                            except ValueError:
                                continue
                        break
            start = s.find(opener, start + 1)
    return None


_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)(?:```|\Z)",
                    re.S)


def extract_code(text: str) -> str:
    """The Python in a reply: the first python (or untagged) fenced block,
    else the text itself when it parses as Python."""
    if not text:
        return ""
    blocks = [(lang.lower(), body) for lang, body in _FENCE.findall(text)]
    for lang, body in blocks:
        if lang in ("python", "py", "python3", ""):
            return body.strip("\n")
    if blocks:
        return blocks[0][1].strip("\n")
    return text.strip("\n")


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("false", "no", "0", "n"):
            return False
        if v in ("true", "yes", "1", "y"):
            return True
    if isinstance(value, (int, float)):
        return bool(value)
    return default


#: A citation is [n] standing alone — not `samples[0]` or `x[1][2]`, which a
#: code-ish answer is full of and which must not be read (or deleted) as one.
CITATION = re.compile(r"(?<![\w\]\)'\"])\[(\d{1,2})\]")

_NOT_COVERED_TEXT = re.compile(
    r"(doesn'?t|does not|do not|don'?t) (cover|contain|mention|say|include)"
    r"|not (covered|documented|mentioned)|no information", re.I)


def _json_string_prefix(text: str, key: str) -> Optional[str]:
    """The value of "key": "..." even when the string was never closed."""
    m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % re.escape(key), text,
                  re.S)
    if not m:
        return None
    raw = m.group(1)
    for cut in range(0, 7):                 # a half-written \uXXXX escape
        try:
            return json.loads('"' + raw[:len(raw) - cut] + '"')
        except ValueError:
            continue
    return raw


def _salvage(text: str) -> Optional[dict]:
    """Fields of a JSON reply cut off by num_predict — a grammar can keep a
    reply well-formed but cannot close it once tokens run out."""
    if '"answer"' not in text and '"code"' not in text:
        return None
    answer = _json_string_prefix(text, "answer")
    if answer is None:
        return None
    m = re.search(r'"sources"\s*:\s*\[([\d,\s]*)', text)
    sources = [int(x) for x in re.findall(r"\d+", m.group(1))] if m else []
    c = re.search(r'"covered"\s*:\s*(true|false)', text)
    return {"answer": answer.strip(), "sources": sources,
            "covered": (c.group(1) == "true") if c else True,
            "code": (_json_string_prefix(text, "code") or "").strip("\n"),
            "format": "cut-off"}


def parse_answer(text: str, n_sources: int, write_code: bool) -> dict:
    """{answer, sources, covered, code, format} from a reply of any shape."""
    obj = _json_object(text)
    if obj is not None and ("answer" in obj or "code" in obj):
        sources: List[int] = []
        raw_sources = obj.get("sources") or []
        if not isinstance(raw_sources, list):
            raw_sources = [raw_sources]
        for s in raw_sources:
            m = re.search(r"\d+", str(s))
            if m:
                sources.append(int(m.group(0)))
        code = str(obj.get("code") or "")
        return {"answer": str(obj.get("answer") or "").strip(),
                "sources": sources,
                "covered": _as_bool(obj.get("covered"), True),
                "code": extract_code(code) if "```" in code else code.strip(
                    "\n"),
                "format": "json"}
    salvaged = _salvage(text or "")
    if salvaged is not None:
        return salvaged
    code = ""
    prose = text or ""
    if write_code:
        m = _FENCE.search(prose)
        if m:
            code = m.group(2).strip("\n")
            prose = (prose[:m.start()] + prose[m.end():]).strip()
    return {"answer": prose.strip(),
            "sources": [int(x) for x in CITATION.findall(prose)],
            "covered": not _NOT_COVERED_TEXT.search(prose or ""),
            "code": code, "format": "text"}


def apply_citation_rules(parsed: dict, n_sources: int,
                         notes: List[str]) -> Tuple[str, List[int]]:
    """(answer text, cited numbers): only pages that exist may be cited."""
    valid = set(range(1, n_sources + 1))
    answer = parsed["answer"]
    inline = [int(x) for x in CITATION.findall(answer)]
    claimed = inline + list(parsed["sources"])
    bad = sorted({k for k in claimed if k not in valid})
    if bad:
        answer = CITATION.sub(lambda m: m.group(0) if int(m.group(1)) in valid
                              else "", answer)
        answer = re.sub(r"[ \t]+([.,;:])", r"\1", answer)
        answer = re.sub(r"[ \t]{2,}", " ", answer)
        notes.append(f"Removed citation(s) {', '.join(f'[{b}]' for b in bad)}"
                     f": only {n_sources} page(s) were retrieved.")
    cited = sorted({k for k in claimed if k in valid})
    return answer.strip(), cited


# ======================================================================
# Code checks
# ======================================================================

@dataclass
class CodeCheck:
    ok: bool
    issues: List[str] = field(default_factory=list)
    parsed: bool = False
    checked_names: int = 0


_SIG = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(((?:[^()]|\([^()]*\))*)\)")


def documented_api(pages: Sequence[Source]) -> Tuple[Set[str],
                                                     Dict[str, List[Tuple[
                                                         List[str], bool]]]]:
    """(every identifier the pages mention, signatures by short name).

    A signature is read from any `name(params)` text in the pages — the
    bundled server writes them as `glimmerquay.encode_frame(payload: bytes,
    *, checksum: str = 'fletcher16', pad_to: int = 8)`; other servers write
    similar text. A params list is kept only when every piece looks like a
    parameter, so prose in parentheses is not mistaken for one."""
    text = "\n".join(f"{p.title}\n{p.ref}\n{p.text}" for p in pages)
    idents = set(_IDENT.findall(text))
    sigs: Dict[str, List[Tuple[List[str], bool]]] = {}
    for m in _SIG.finditer(text):
        name, inner = m.group(1), m.group(2)
        params: List[str] = []
        varkw = False
        good = True
        depth, cur, pieces = 0, "", []
        for ch in inner:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                pieces.append(cur)
                cur = ""
            else:
                cur += ch
        pieces.append(cur)
        for piece in pieces:
            p = piece.strip()
            if not p or p in ("/", "*", "..."):
                continue
            if p.startswith("**"):
                varkw = True
                continue
            if p.startswith("*"):
                continue
            pname = re.split(r"[:=]", p, 1)[0].strip()
            if not pname.isidentifier():
                good = False
                break
            params.append(pname)
        if good and (params or varkw or inner.strip() == ""):
            sigs.setdefault(name, []).append((params, varkw))
    return idents, sigs


class _Names(ast.NodeVisitor):
    """Names bound anywhere (flat — scope is ignored on purpose: the check is
    for 'used but never defined', and a flat view never cries wolf about a
    name defined in another scope)."""

    def __init__(self) -> None:
        self.bound: Set[str] = set()
        self.used: List[Tuple[str, int]] = []

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        else:
            self.used.append((node.id, node.lineno))

    def visit_FunctionDef(self, node) -> None:
        self.bound.add(node.name)
        a = node.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [
                x for x in (a.vararg, a.kwarg) if x]:
            self.bound.add(arg.arg)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        a = node.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [
                x for x in (a.vararg, a.kwarg) if x]:
            self.bound.add(arg.arg)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node) -> None:
        for a in node.names:
            self.bound.add(a.asname or a.name.split(".")[0])

    def visit_ImportFrom(self, node) -> None:
        for a in node.names:
            self.bound.add(a.asname or a.name)

    def visit_ExceptHandler(self, node) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self.bound.add(node.arg)

    def visit_Global(self, node) -> None:
        self.bound.update(node.names)

    def visit_MatchAs(self, node) -> None:                # pragma: no cover
        if getattr(node, "name", None):
            self.bound.add(node.name)
        self.generic_visit(node)


def _dotted(node: ast.AST) -> Optional[List[str]]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _suggest(name: str, idents: Iterable[str]) -> str:
    pool = [i for i in idents if len(i) > 2]
    close = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
    return f" — did you mean {close[0]}?" if close else ""


def check_code(code: str, pages: Sequence[Source],
               packages: Iterable[str] = ()) -> CodeCheck:
    """Parse, then check names and keywords against the documentation.

    `packages` are the top-level names whose use is checked; names from
    anything else (the standard library, numpy when the docs are about
    something else) are not the docs' business."""
    if not (code or "").strip():
        return CodeCheck(False, ["No code was returned."])
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        return CodeCheck(False, [f"line {exc.lineno}: SyntaxError: {exc.msg}"
                                 + (f" — `{line}`" if line else "")])
    issues: List[str] = []
    roots = {p.split(".")[0] for p in packages if p}

    # Undefined names (the classic: a class used without its import).
    names = _Names()
    names.visit(tree)
    known = names.bound | set(dir(builtins)) | {"__name__", "__file__"}
    seen_undefined: Set[str] = set()
    for name, line in names.used:
        if name not in known and name not in seen_undefined:
            seen_undefined.add(name)
            issues.append(f"line {line}: name '{name}' is used but never "
                          f"defined or imported")

    if not roots or not pages:
        return CodeCheck(not issues, issues, True, 0)

    idents, sigs = documented_api(pages)
    aliases: Dict[str, List[str]] = {}       # local name -> dotted path
    var_types: Dict[str, List[str]] = {}     # variable -> what made it
    flagged: Set[str] = set()
    checked = 0

    def flag(line: int, dotted: str, missing: str) -> None:
        if dotted in flagged:
            return
        flagged.add(dotted)
        issues.append(f"line {line}: {dotted} is not in the documentation "
                      f"you were given{_suggest(missing, idents)}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in roots:
                    local = a.asname or a.name.split(".")[0]
                    aliases[local] = (a.name.split(".") if a.asname
                                      else [a.name.split(".")[0]])
        elif isinstance(node, ast.ImportFrom) and node.module and \
                node.module.split(".")[0] in roots and not node.level:
            for a in node.names:
                if a.name == "*":
                    continue
                checked += 1
                aliases[a.asname or a.name] = node.module.split(".") + [
                    a.name]
                if a.name not in idents:
                    flag(node.lineno, f"{node.module}.{a.name}", a.name)

    def resolve(expr: ast.AST) -> Optional[List[str]]:
        parts = _dotted(expr)
        if not parts:
            return None
        head = parts[0]
        if head in aliases:
            return aliases[head] + parts[1:]
        if head in var_types:
            return var_types[head] + parts[1:]
        return None

    # Variables made by calling something from the package.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name) and \
                isinstance(node.value, ast.Call):
            made = resolve(node.value.func)
            if made:
                var_types[node.targets[0].id] = made + ["()"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            full = resolve(node)
            if not full:
                continue
            base = _dotted(node) or []
            # Check each attribute written in THIS expression; the alias's
            # own path was checked at its import.
            written = full[len(full) - (len(base) - 1):] if len(base) > 1 \
                else []
            for i, part in enumerate(written):
                if part == "()":
                    continue
                checked += 1
                if part not in idents:
                    shown = ".".join(p for p in full[:len(full) - len(
                        written) + i + 1] if p != "()")
                    flag(node.lineno, shown, part)
                    break
        if isinstance(node, ast.Call):
            full = resolve(node.func)
            if not full:
                continue
            short = full[-1]
            known_sigs = sigs.get(short)
            if not known_sigs or not node.keywords:
                continue
            if any(varkw for _p, varkw in known_sigs):
                continue
            allowed = {p for params, _v in known_sigs for p in params}
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                checked += 1
                if kw.arg not in allowed:
                    params = ", ".join(known_sigs[0][0])
                    issues.append(
                        f"line {node.lineno}: {short}() has no parameter "
                        f"'{kw.arg}' in the documentation (documented: "
                        f"{short}({params}))")
    return CodeCheck(not issues, issues, True, checked)


def repair_message(issues: Sequence[str]) -> str:
    bullet = "\n".join(f"- {i}" for i in list(issues)[:8])
    return ("Your code has these problems:\n" + bullet + "\n\nFix them using "
            "only names and parameters shown in the documentation above. "
            "Reply with the same JSON shape.")


def _package_roots(packages: Sequence[str], pages: Sequence[Source],
                   hinted: str = "") -> List[str]:
    roots = [p.split(".")[0] for p in packages if p]
    if hinted:
        roots.append(hinted)
    for p in pages:
        head = re.match(r"[A-Za-z_][A-Za-z0-9_]*", p.ref or "")
        if head and "." in (p.ref or "") and head.group(0) not in roots:
            roots.append(head.group(0))
    return roots


# ======================================================================
# The whole question
# ======================================================================

def ask(question: str, *, servers: Any = None, packages: Any = (),
        write_code: bool = False, model_call: Optional[ModelCall] = None,
        derive: str = "model", mode: str = "orchestrated",
        max_pages: int = MAX_PAGES, context_chars: int = CONTEXT_CHARS,
        max_repairs: int = MAX_REPAIRS, should_stop: ShouldStop = None,
        progress: Progress = None, chat_tools: Optional[Callable] = None,
        config_path=None) -> DocsAnswer:
    """Answer `question` (or write the code it asks for) from the docs.

    Never raises: a failure is a DocsAnswer with `error` set, and Stop is a
    DocsAnswer with `stopped` set — the same contract gui_describe keeps, so
    a worker thread needs no try of its own."""
    result = DocsAnswer(mode=mode)
    t_all = time.perf_counter()
    pkgs = parse_packages(packages)
    if model_call is None:
        model_call = engine_model_call()
    result.role = getattr(model_call, "role", None) or answering_role()
    try:
        if not (question or "").strip():
            result.error = "Ask a question first."
            return result
        if mode == "tools":
            return _ask_with_tools(question, result, servers=servers,
                                   packages=pkgs, write_code=write_code,
                                   model_call=model_call,
                                   chat_tools=chat_tools,
                                   should_stop=should_stop,
                                   progress=progress,
                                   config_path=config_path,
                                   context_chars=context_chars,
                                   max_repairs=max_repairs, t_all=t_all)
        # 1. queries
        t = time.perf_counter()
        if derive == "model":
            if progress:
                progress("Working out what to search for…")
            queries, hinted, info = derive_queries(
                question, pkgs, model_call, should_stop=should_stop)
            result.model_calls += 1
            if not info.get("ok"):
                result.notes.append("The model gave no usable search queries;"
                                    " searched by keywords instead.")
        else:
            queries, hinted = [keyword_query(question, pkgs)], ""
        result.timings["queries_s"] = round(time.perf_counter() - t, 3)
        result.queries = [q for q in queries if q]
        search_pkgs = pkgs or ([hinted] if hinted else [])

        # 2-3. search and fetch
        found = retrieve(question, result.queries, servers=servers,
                         packages=search_pkgs, max_pages=max_pages,
                         context_chars=context_chars,
                         should_stop=should_stop, progress=progress,
                         config_path=config_path)
        if not found.pages and hinted and not pkgs:
            # The model's package guess may be wrong; one more pass without.
            found = retrieve(question, result.queries, servers=servers,
                             packages=(), max_pages=max_pages,
                             context_chars=context_chars,
                             should_stop=should_stop, progress=progress,
                             config_path=config_path)
        result.timings["search_s"] = round(found.search_s, 3)
        result.timings["fetch_s"] = round(found.fetch_s, 3)
        result.sources = found.pages
        result.notes += found.notes
        result.notes += found.errors
        if not found.pages:
            result.ok = not (found.errors and not found.servers_used)
            result.covered = False
            result.answer = NOT_COVERED
            if found.errors and not found.servers_used:
                result.error = "No documentation server could be reached."
            return result

        # 4. answer (+ code checks and repairs)
        roots = _package_roots(pkgs, found.pages, hinted)
        _answer(question, result, found.pages, write_code=write_code,
                model_call=model_call, roots=roots,
                max_repairs=max_repairs, should_stop=should_stop,
                progress=progress)
        return result
    except Stopped:
        result.stopped = True
        result.error = "Stopped."
        return result
    except Exception as exc:                              # noqa: BLE001
        if should_stop is not None and should_stop():
            result.stopped = True
            result.error = "Stopped."
        else:
            result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result.timings["total_s"] = round(time.perf_counter() - t_all, 3)


def _answer(question: str, result: DocsAnswer, pages: List[Source], *,
            write_code: bool, model_call: ModelCall, roots: Sequence[str],
            max_repairs: int, should_stop: ShouldStop,
            progress: Progress) -> None:
    schema = answer_schema(len(pages), write_code)
    base = answer_messages(question, pages, write_code)
    messages = list(base)
    t_model = 0.0
    parsed: dict = {}
    for attempt in range(1 + (max_repairs if write_code else 0)):
        _check_stop(should_stop)
        if progress:
            progress("Writing the answer…" if attempt == 0 else
                     f"Fixing the code (attempt {attempt + 1})…")
        t = time.perf_counter()
        text, info = _model(model_call, messages, json_schema=schema,
                            temperature=ANSWER_TEMPERATURE,
                            num_predict=(CODE_NUM_PREDICT if write_code
                                         else ANSWER_NUM_PREDICT),
                            seed=SEED + attempt, should_stop=should_stop)
        t_model += time.perf_counter() - t
        result.model_calls += 1
        result.raw = text
        stats = info.get("stats") or {}
        if "constrained" in stats:
            result.constrained = bool(stats["constrained"])
        parsed = parse_answer(text, len(pages), write_code)
        if not write_code:
            break
        check = check_code(parsed["code"], pages, roots)
        result.code_ok = check.ok
        result.code_issues = check.issues
        if check.ok or attempt == max_repairs:
            break
        messages = base + [{"role": "assistant", "content": text[:4000]},
                           {"role": "user",
                            "content": repair_message(check.issues)}]
        result.notes.append(f"Repair {attempt + 1}: " + "; ".join(
            check.issues[:3]))
    result.timings["model_s"] = round(t_model, 3)
    if parsed.get("format") == "text" and result.constrained is not False:
        result.notes.append("The reply was not JSON; it was read as text.")
    elif parsed.get("format") == "cut-off":
        result.notes.append("The reply was cut off by the length limit; "
                            "what arrived was kept.")
    answer, cited = apply_citation_rules(parsed, len(pages), result.notes)
    result.covered = bool(parsed.get("covered", True)) and bool(
        answer or parsed.get("code"))
    if not result.covered:
        if answer and answer != NOT_COVERED:
            result.notes.append("The model said: " + answer[:300])
        result.answer = NOT_COVERED
        result.cited = []
        result.code = parsed.get("code", "") if write_code else ""
    else:
        result.answer = answer
        result.cited = cited
        result.code = parsed.get("code", "") if write_code else ""
        if not cited:
            result.notes.append("The answer cites no source, so nothing ties "
                                "it to the documentation — treat it as "
                                "unverified.")
    result.ok = True


# ======================================================================
# Native tool calling (optional)
# ======================================================================

def _tool_def(name: str, description: str, arg: str) -> dict:
    """One tool, readable two ways: Ollama/OpenAI ({type, function}) and
    MCP ({name, description, inputSchema}). The chat_tools contract does not
    fix the shape, and extra keys are ignored by either reader."""
    params = {"type": "object", "properties": {arg: {"type": "string"}},
              "required": [arg]}
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": params},
            "name": name, "description": description, "inputSchema": params}


TOOL_DEFS = [
    _tool_def("search_docs", "Search the package documentation. Returns "
                             "numbered results with names you can pass to "
                             "get_doc.", "query"),
    _tool_def("get_doc", "Read one documentation page by a name search_docs "
                         "returned. The reply starts with a source number "
                         "[n] to cite.", "name"),
]


def _run_tool_call(name: str, args: dict, question: str,
                   packages: Sequence[str], pkg: str, spec: ServerSpec,
                   client, roles, pages: List[Source], budget: int,
                   result: DocsAnswer, should_stop: ShouldStop) -> str:
    """One model-requested tool call, executed by the Council; the reply
    text the model sees next."""
    if name == "search_docs":
        q = str(args.get("query") or question)
        res = client.call_tool(roles.search_tool, _search_args(roles, q, pkg),
                               timeout=spec.timeout, should_stop=should_stop)
        result.queries.append(q)
        if res.is_error:
            return res.text[:400] or "The search failed."
        hits = parse_search(spec.name, res)[:8]
        return "\n".join(f"{i}. {h.ref} — {h.snippet[:160]}"
                         for i, h in enumerate(hits, 1)) or "No results."
    if name == "get_doc":
        ref = str(args.get("name") or "")
        if not roles.fetch_tool:
            return "Pages cannot be fetched from this server."
        res = client.call_tool(roles.fetch_tool, {roles.fetch_arg: ref},
                               timeout=spec.timeout, should_stop=should_stop)
        if res.is_error or not res.text.strip():
            return res.text[:400] or f"No page called {ref!r}."
        text = focus(res.text, content_terms(question, packages),
                     max(600, budget // 2))
        n = len(pages) + 1
        pages.append(Source(n, spec.name, ref, ref, text))
        return f"[{n}] {ref}\n{text}"
    return f"There is no tool called {name!r}."


def _engine_chat_tools() -> Optional[Callable]:
    try:
        import council_engine
    except Exception:                                     # noqa: BLE001
        return None
    fn = getattr(council_engine, "chat_tools", None)
    return fn if callable(fn) else None


def _ask_with_tools(question: str, result: DocsAnswer, *, servers, packages,
                    write_code, model_call, chat_tools, should_stop,
                    progress, config_path, context_chars, max_repairs,
                    t_all) -> DocsAnswer:
    """Let the model drive search_docs/get_doc itself (chat_tools).

    Pages it reads are numbered as they arrive; the final answer may cite
    only those. No chat_tools in this engine -> the orchestrated path, with a
    note saying so."""
    fn = chat_tools or _engine_chat_tools()
    if fn is None:
        fallback = ask(question, servers=servers, packages=packages,
                       write_code=write_code, model_call=model_call,
                       should_stop=should_stop, progress=progress,
                       config_path=config_path, context_chars=context_chars,
                       max_repairs=max_repairs)
        fallback.notes.insert(0, "This engine has no native tool calling; "
                                 "used the orchestrated path instead.")
        return fallback
    specs = _servers(servers, config_path)
    usable = []
    for spec in specs:
        try:
            client, roles = docs_servers.pooled(spec, should_stop=should_stop)
        except McpError as exc:
            result.notes.append(f"{spec.name}: {exc}")
            continue
        if roles.usable:
            usable.append((spec, client, roles))
    if not usable:
        result.answer = NOT_COVERED
        result.error = "No documentation server could be reached."
        return result
    spec, client, roles = usable[0]
    pkg = (packages or spec.packages or [""])[0]
    system = ("You answer questions about Python packages. Use search_docs "
              "and get_doc to find the documentation, then answer from it, "
              "citing the source numbers [n] get_doc gave you. If the "
              "documentation does not cover the question, say so."
              + (" Then write the code in one ```python block." if write_code
                 else ""))
    messages: List[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question.strip()
         + (f"\n(Package: {pkg})" if pkg else "")}]
    pages: List[Source] = []
    final = ""
    t_model = 0.0
    budget = context_chars
    for step in range(5):
        _check_stop(should_stop)
        if progress:
            progress(f"Model step {step + 1}…")
        t = time.perf_counter()
        out, _dropped = call_supported(fn, messages, TOOL_DEFS,
                                       role=result.role, temperature=0.1,
                                       num_predict=CODE_NUM_PREDICT
                                       if write_code else ANSWER_NUM_PREDICT,
                                       seed=SEED)
        t_model += time.perf_counter() - t
        result.model_calls += 1
        calls = list((out or {}).get("tool_calls") or [])
        content = str((out or {}).get("content") or "")
        if not calls:
            final = content
            break
        messages.append({"role": "assistant", "content": content,
                         "tool_calls": [{"function": {
                             "name": c.get("name"),
                             "arguments": c.get("arguments") or {}}}
                             for c in calls]})
        for c in calls[:3]:
            name = str(c.get("name") or "")
            args = c.get("arguments") or {}
            if isinstance(args, str):
                args = _json_object(args) or {}
            try:
                reply = _run_tool_call(name, args, question, packages, pkg,
                                       spec, client, roles, pages, budget,
                                       result, should_stop)
            except McpError as exc:
                _check_stop(should_stop)
                reply = f"The tool failed: {exc}"
            budget = max(600, context_chars - sum(len(p.text)
                                                  for p in pages))
            messages.append({"role": "tool", "tool_name": name, "name": name,
                             "content": reply})
    else:
        messages.append({"role": "user", "content": "Answer now, citing [n]."})
        t = time.perf_counter()
        final, _info = _model(model_call, messages, json_schema=None,
                              temperature=0.1,
                              num_predict=ANSWER_NUM_PREDICT, seed=SEED,
                              should_stop=should_stop)
        t_model += time.perf_counter() - t
        result.model_calls += 1
    result.timings["model_s"] = round(t_model, 3)
    result.timings["total_s"] = round(time.perf_counter() - t_all, 3)
    result.sources = pages
    result.raw = final
    parsed = parse_answer(final, len(pages), write_code)
    answer, cited = apply_citation_rules(parsed, len(pages), result.notes)
    if not pages:
        result.notes.append("The model read no documentation page.")
    result.covered = bool(pages) and parsed["covered"] and bool(answer)
    result.answer = answer if result.covered else NOT_COVERED
    result.cited = cited if result.covered else []
    if write_code:
        result.code = parsed["code"]
        check = check_code(result.code, pages,
                           _package_roots(packages, pages))
        result.code_ok, result.code_issues = check.ok, check.issues
    result.ok = True
    return result


# ======================================================================
# For other Council features: documentation as context, no model
# ======================================================================

def docs_context(question: str, *, packages: Sequence[str] = (),
                 servers: Any = None, max_chars: int = 6000,
                 should_stop: ShouldStop = None,
                 config_path=None) -> List[dict]:
    """Documentation snippets for `question`: [{server, source, title, text}].

    The shared contract's entry point for features that write code (the
    code-behind writer): keyword search only — no model call, so it costs
    milliseconds once a server is warm — and it NEVER raises. No server
    configured, none reachable, nothing found: all return []."""
    try:
        pkgs = parse_packages(packages)
        found = retrieve(question, [keyword_query(question, pkgs)],
                         servers=servers, packages=pkgs,
                         max_pages=MAX_PAGES, context_chars=max_chars,
                         should_stop=should_stop, config_path=config_path)
        return [{"server": p.server, "source": p.ref, "title": p.title,
                 "text": p.text} for p in found.pages]
    except Exception:                                     # noqa: BLE001
        return []


# ======================================================================
# Which model answers
# ======================================================================

def model_label(model_id: str) -> str:
    """'ollama:llama3.1:8b' -> 'llama3.1:8b (Ollama)'; a path -> its file
    name; '' -> 'the main model'."""
    m = (model_id or "").strip()
    if not m:
        return "the main model"
    if m.lower().startswith("ollama:"):
        return f"{m[7:]} (Ollama)"
    return re.split(r"[\\/]", m)[-1]


def role_model(role: str, vault_dir=None) -> str:
    """The model id/path the role answers with ("" = the main model's)."""
    try:
        from . import model_slots, paths
        vault = vault_dir or paths.vault_dir()
        cfg = model_slots.load(vault)
        slot = cfg.slot_for(role)
        path = cfg.slots[slot].path
        if path:
            return path
        import os
        env = os.environ.get("COUNCIL_GGUF_PATH", "").strip()
        if env:
            return env
        try:
            import onboarding
            return onboarding.load_gguf_path(vault) or ""
        except Exception:                                 # noqa: BLE001
            return ""
    except Exception:                                     # noqa: BLE001
        return ""


def assign_docs_model(model_id: str, vault_dir=None) -> str:
    """Point the docs role at `model_id` (an "ollama:<name>" id or a GGUF
    path); "" returns it to the fallback. Returns a sentence for the user.

    Writes model_slots.json through model_slots itself, so the Models tab and
    the engine read the same map. A slot this leaves serving no role is
    removed, so an abandoned choice does not keep a model loaded."""
    from . import model_slots, paths
    vault = vault_dir or paths.vault_dir()
    cfg = model_slots.load(vault)
    old_slot = cfg.roles.get(DOCS_ROLE)
    model_id = (model_id or "").strip()
    if not model_id:
        cfg.roles.pop(DOCS_ROLE, None)
    else:
        slot = next((n for n, s in cfg.slots.items()
                     if s.path and s.path == model_id), None)
        if slot is None:
            stem = model_label(model_id).replace(" (Ollama)", "")
            stem = re.sub(r"\.gguf$", "", stem, flags=re.I)
            slot = model_slots._slot_name(stem, cfg.slots)  # noqa: SLF001
            cfg.slots[slot] = model_slots.Slot(slot, model_id)
        cfg.roles[DOCS_ROLE] = slot
    if old_slot and old_slot != model_slots.MAIN and old_slot not in \
            cfg.roles.values():
        cfg.slots.pop(old_slot, None)
    model_slots.save(vault, cfg)
    if not model_id:
        return ("Docs questions now use the "
                + ("coder's model." if "coder" in cfg.roles
                   else "main model."))
    return f"Docs questions now go to {model_label(model_id)}."


def list_models() -> List[dict]:
    """Models a user can pick for the docs role.

    council_engine.list_local_models() when the engine has it (Ollama and
    GGUF, with origin); otherwise the GGUF files in the model folders. Every
    entry: {id, name, backend, origin, size_bytes}."""
    try:
        import council_engine
        lister = getattr(council_engine, "list_local_models", None)
        if callable(lister):
            out = []
            for m in lister() or []:
                if isinstance(m, dict) and m.get("id"):
                    out.append(dict(m))
            return out
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from . import model_slots
        return [{"id": str(p), "name": p.stem, "backend": "gguf",
                 "origin": "unknown",
                 "size_bytes": p.stat().st_size if p.exists() else 0}
                for p in model_slots.known_files()]
    except Exception:                                     # noqa: BLE001
        return []


__all__ = ["ask", "docs_context", "DocsAnswer", "Source", "NOT_COVERED",
           "derive_queries", "keyword_query", "retrieve", "check_code",
           "parse_answer", "answer_schema", "engine_model_call",
           "answering_role", "assign_docs_model", "role_model", "model_label",
           "list_models", "call_supported", "focus", "QUERY_SCHEMA"]
