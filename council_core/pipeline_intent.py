"""
council_core.pipeline_intent — which pipeline command, if any, a message is.

The regexes of the Tk engine's `_handle_pipeline_intent`
(council_gui_engine.py:5059-5259), moved verbatim, with the dispatch turned
into data: `candidates(text)` yields `Intent`s in the order Tk tries them, and
a handler that declines one (returns False) lets the next be tried. That is
exactly Tk's shape for "convert X to python": it only claims the message when X
names a pipeline, otherwise the create/export/... patterns still get a look.

Only the FIRST LINE is matched, as in Tk — a multi-line message is a question
with pasted context, not a command.

NOT HERE: "download <repo> <file>.gguf" and "peek <file>". Tk routes them
through this function only because it is the chat entry point; neither touches
a pipeline (see the requirements doc). They are not ported with Dream3D.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Tuple

_I = re.IGNORECASE

LIST_RE = re.compile(
    r"^\s*(?:list|show|what)\s+(?:are\s+)?"
    r"(?:my\s+|the\s+)?(?:available\s+)?pipelines?\??\s*$", _I)
SHOW_RE = re.compile(
    r"^\s*(?:show|display|view|render|render\s+the|view\s+the)"
    r"(?:\s+(?:me|the))?\s+pipelines?\s+(.+?)\s*[.!?]?\s*$", _I)
MODIFY_RE = re.compile(
    r"^\s*(?:modify|edit|change|update|tweak)"
    r"(?:\s+(?:the|my))?\s+pipelines?\s+(.+?)\s+(?:to|so\s+that|so|with|by|"
    r"using)\s+(.+?)\s*[.!?]?\s*$", _I)
EXPLAIN_RE = re.compile(
    r"^\s*(?:explain|describe|what\s+does)"
    r"(?:\s+(?:the|my))?\s+pipelines?\s+(.+?)\s*(?:do)?\s*[.?!]?\s*$", _I)
VALIDATE_RE = re.compile(
    r"^\s*(?:validate|check|verify)"
    r"(?:\s+(?:the|my))?\s+pipelines?\s+(.+?)\s*[.!?]?\s*$", _I)
GRAPH_RE = re.compile(
    r"^\s*(?:graph|data\s*flow|dependencies\s+of)"
    r"(?:\s+(?:the|my|for))?\s+pipelines?\s+(.+?)\s*[.!?]?\s*$", _I)
CREATE_RE = re.compile(
    r"^\s*(?:create|generate|make|write)"
    r"(?:\s+(?:me|a))?\s+(?:new\s+)?pipelines?\s+(?:that\s+|to\s+|which\s+)?"
    r"(.+?)\s*[.!?]?\s*$", _I)
# Convert an EXISTING pipeline to Python — deterministic (nx_transpile), never
# the model. `(?:\w+\s+){0,3}python` tolerates "into a DREAM3D python script".
TO_PYTHON_RES: Tuple[re.Pattern, ...] = (
    re.compile(
        r"^\s*(?:convert|transpile|translate|turn|render|rewrite|export)\s+"
        r"(?:the\s+|my\s+|a\s+|an\s+)?(?:pipeline\s+)?(.+?)\s+"
        r"(?:in)?to\s+(?:a\s+|an\s+|its\s+|the\s+)?(?:\w+\s+){0,3}python\b"
        r"[\w\s]*?[.!?]?\s*$", _I),
    re.compile(
        r"^\s*(?:convert|make|give\s+me|write|get|show(?:\s+me)?)?\s*"
        r"(?:the\s+)?(?:\w+\s+){0,2}python\s+"
        r"(?:script|code|equivalent|version|file)?\s*(?:of|for)\s+"
        r"(?:the\s+|my\s+|pipeline\s+)*(.+?)\s*[.!?]?\s*$", _I),
    re.compile(
        r"^\s*transpile\s+(?:the\s+|my\s+)?(?:pipeline\s+)?(.+?)\s*[.!?]?\s*$",
        _I),
    re.compile(
        r"^\s*(?:convert\s+)?(?:the\s+|my\s+|a\s+|an\s+)?(?:pipeline\s+)?(.+?)"
        r"\s+(?:in)?to\s+(?:a\s+|an\s+|its\s+)?(?:\w+\s+){0,3}python\b"
        r"[\w\s]*?[.!?]?\s*$", _I),
)
EXPORT_RE = re.compile(
    r"^\s*(?:export|save)\s+pipelines?\s+(.+?)\s+(?:as|to)\s+"
    r"(?:markdown|md|a\s+markdown\s+file)\s*[.!?]?\s*$", _I)
COMPARE_RE = re.compile(
    r"^\s*(?:compare|diff)\s+pipelines?\s+(.+?)\s+(?:and|vs\.?|versus|to|with)"
    r"\s+(.+?)\s*[.!?]?\s*$", _I)

#: Words that describe a pipeline rather than name one.
GENERIC_TOKENS = frozenset({
    "a", "an", "the", "my", "this", "that", "some", "any", "it",
    "dream3d", "d3d", "simplnx", "nx", "pipeline", "pipelines",
    "script", "file", "saved", "existing", "example",
})

#: Actions, in the order Tk tries them.
ACTIONS = ("list", "show", "modify", "explain", "validate", "graph",
           "to_python", "create", "export", "compare")


@dataclass(frozen=True)
class Intent:
    action: str
    args: Tuple[str, ...] = ()
    text: str = ""           # the whole message, for the model-backed actions


def _q(s: str) -> str:
    return s.strip().strip("'\"`")


def clean_ref(name: str) -> str:
    """Drop generic words: "a dream3d pipeline" -> "", "my seg pipeline" ->
    "seg", "job_417.d3dpipeline" unchanged."""
    toks = [t for t in re.split(r"\s+", (name or "").strip()) if t]
    return " ".join(t for t in toks
                    if t.lower().strip(".") not in GENERIC_TOKENS).strip()


def candidates(text: str) -> Iterator[Intent]:
    """Every intent the first line could be, in Tk's precedence order."""
    if not text:
        return
    line = text.split("\n", 1)[0]
    if LIST_RE.match(line):
        yield Intent("list", (), text)
    m = SHOW_RE.match(line)
    if m:
        yield Intent("show", (_q(m.group(1)),), text)
    m = MODIFY_RE.match(line)
    if m:
        yield Intent("modify", (_q(m.group(1)), _q(m.group(2)).strip(".")),
                     text)
    for action, rx in (("explain", EXPLAIN_RE), ("validate", VALIDATE_RE),
                       ("graph", GRAPH_RE)):
        m = rx.match(line)
        if m:
            yield Intent(action, (_q(m.group(1)),), text)
    for rx in TO_PYTHON_RES:
        m = rx.match(line)
        if m:
            name = _q(m.group(1)).strip(".")
            if name:
                yield Intent("to_python", (name,), text)
            break                    # only the first matching phrasing, as Tk
    m = CREATE_RE.match(line)
    if m:
        yield Intent("create", (_q(m.group(1)).strip("."),), text)
    m = EXPORT_RE.match(line)
    if m:
        yield Intent("export", (_q(m.group(1)),), text)
    m = COMPARE_RE.match(line)
    if m:
        yield Intent("compare", (_q(m.group(1)), _q(m.group(2))), text)


def looks_like_pipeline_command(text: str) -> bool:
    return next(candidates(text), None) is not None
