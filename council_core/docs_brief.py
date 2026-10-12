"""
council_core.docs_brief — real package documentation for the council's
Coder, before it writes code.

The Docs tab answers from documentation servers (Python package docs served
over MCP; council_core.docs_servers), and the GUI designer's code-behind
writer reads them before it writes. The council's Coder never did: it wrote
code from the model's memory, which is how a call grows a parameter the
function does not have.

Before a question the Judge routes to the coding panel ("ide" / "coder"),
this fetches the matching pages with docs_qa.docs_context — keyword search
only, no model call, never raises — and hands them to the Coder and the
Writer for that question as a DOCUMENTATION block: use these exact names and
parameters, and say so when the docs do not cover something rather than
invent it.

No documentation server configured, none reachable, nothing found: no block.
COUNCIL_DOCS_FOR_CODER=0 turns it off.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

CODE_ROUTES = ("ide", "coder")
READERS = ("coder", "writer")
MAX_CHARS = 6_000
PAGE_CHARS = 2_000

HEADER = ("DOCUMENTATION — pages from the user's documentation servers for "
          "this question. Use exactly the names, arguments and return values "
          "they show; if they do not cover something, say so instead of "
          "inventing an API.\n")


def enabled() -> bool:
    return os.environ.get("COUNCIL_DOCS_FOR_CODER", "1").strip().lower() \
        not in ("0", "false", "no", "off")


@dataclass
class DocsBrief:
    text: str = ""
    titles: List[str] = field(default_factory=list)

    def note(self) -> str:
        if not self.titles:
            return ""
        shown = ", ".join(self.titles[:4])
        more = f" and {len(self.titles) - 4} more" if len(self.titles) > 4 \
            else ""
        return (f"Docs: {len(self.titles)} page(s) for the Coder — "
                f"{shown}{more}.")


def route_of(models: Any, question: str) -> str:
    """The Judge's keyword route for the question ("" if it cannot say)."""
    judge = getattr(models, "judge", None)
    try:
        return str(judge.route(question)) if judge is not None else ""
    except Exception:                                     # noqa: BLE001
        return ""


def build(question: str, route: str, *,
          fetch: Optional[Callable[..., Sequence[dict]]] = None,
          max_chars: int = MAX_CHARS) -> DocsBrief:
    """The DOCUMENTATION block for a coding question, or an empty brief."""
    if not enabled() or route not in CODE_ROUTES or not \
            (question or "").strip():
        return DocsBrief()
    if fetch is None:
        try:
            from .docs_qa import docs_context as fetch
        except Exception:                                 # noqa: BLE001
            return DocsBrief()
    try:
        pages = list(fetch(question, max_chars=max_chars) or [])
    except Exception:                                     # noqa: BLE001
        return DocsBrief()
    parts, titles, used = [HEADER], [], len(HEADER)
    for i, page in enumerate(pages, start=1):
        text = str(page.get("text", "")).strip()
        title = str(page.get("title") or page.get("source") or f"page {i}")
        if not text:
            continue
        if len(text) > PAGE_CHARS:
            text = text[:PAGE_CHARS] + " …"
        piece = f"\n[{i}] {title}\n{text}\n"
        if used + len(piece) > max_chars + len(HEADER):
            break
        parts.append(piece)
        used += len(piece)
        titles.append(title)
    if not titles:
        return DocsBrief()
    return DocsBrief("".join(parts).rstrip(), titles)


def readers(models: Any) -> List[Any]:
    return [m for m in (getattr(models, r, None) for r in READERS)
            if m is not None]


__all__ = ["DocsBrief", "build", "route_of", "readers", "enabled",
           "CODE_ROUTES"]
