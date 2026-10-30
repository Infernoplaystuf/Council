"""
gui_reference.py — complete, working PySide6 apps, as model context.

PURE. Stdlib only; reads the .py files in examples/qt_apps/ as TEXT. Nothing
here imports Qt or runs an example.

WHY THIS EXISTS
gui_examples.py gives Describe worked WIREFRAMES: where widgets go. When a
model writes the Qt code itself — a coding agent building a tool, a widget
module, a whole app — a wireframe says nothing about how the parts work:
a worker on a QThread that reports back through Signals, a model/view table
with a proxy and undo, a matplotlib canvas that redraws once for five
ticks. A small model asked for those writes code that looks right and
freezes the window, or reaches for QSettings and is refused by the gate.
A complete example of the pattern is the stronger signal (gui_examples
measured the same for wireframes), so these are whole apps, not snippets.

WHAT IS IN examples/qt_apps/
Ten single-file apps (never more: each costs prompt tokens when picked, and
the set is meant to be read). Each one:
  * runs on its own (python examples/qt_apps/<name>.py) and offers
    make_window(argv) so a test can build it offscreen
  * passes gui_policy.validate(mode="standalone", toolkit="qt") with only
    its declared REQUIRES — so what a model copies from it is allowed in a
    generated app (tests/test_qt_app_examples.py holds every one to that)
  * starts with a docstring in one shape: a title line, WHAT IT DOES,
    SHOWS (the widgets and patterns, one bullet each), RUN, REQUIRES.
    SHOWS is what select() matches a request against.

HOW THE COUNCIL FINDS THEM
  * The Build tab's Agent mode: the built-in "Qt app builder" agent (council_core.
    code_profiles) has the folder as a reference. Its search_references
    tool then finds passages from the examples, and code_agent.context()
    adds the ONE example that best matches the task, whole (for_prompt).
  * Anything else that writes Qt code can call for_prompt(task) the same
    way, or catalogue() for the one-line list.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples" / "qt_apps"
MAX_EXAMPLES = 10
#: What for_prompt may spend on examples' source; one longer than what is
#: left is shown as its docstring and outline. The largest is about 24 000
#: characters (text_editor), the smallest about 11 500 (file_browser).
PROMPT_CHARS = 25_000

# Words a person uses for an example that its SHOWS list does not contain.
# Kept here, beside the matcher, not in the examples: an example's docstring
# describes the example.
ALIASES: Dict[str, str] = {
    "csv_plotter": "plot plotting graph graphs chart charts csv line scatter "
                   "bar histogram box violin heatmap matplotlib pandas data "
                   "folder files visualise visualize",
    "local_chatbot": "chat chatbot bot llm model models ollama gguf llama "
                     "assistant conversation prompt stream streaming reply "
                     "thread worker",
    "table_editor": "table spreadsheet grid rows columns edit csv filter sort "
                    "undo redo copy paste model view delegate",
    "file_browser": "file files folder folders browse browser explorer tree "
                    "preview path navigate",
    "settings_dialog": "settings preferences options config configuration "
                       "dialog form inputs theme colour color font save json",
    "image_viewer": "image images picture photo viewer zoom pan roi region "
                    "crop histogram pixel thumbnails slider frames",
    "job_runner": "batch jobs job queue background threads threadpool "
                  "parallel progress cancel log worker tasks",
    "live_dashboard": "dashboard live gauge gauges sparkline monitor sensor "
                      "sensors alarm alarms realtime real-time custom paint "
                      "painter animation kpi",
    "text_editor": "text editor code editor notepad tabs syntax highlight "
                   "highlighting find replace line numbers menus toolbar",
    "setup_wizard": "wizard steps step-by-step setup onboarding pages "
                    "validation tree checkable calendar summary",
}

_WORD = re.compile(r"[a-z][a-z0-9_\-]+")
_STOP = frozenset("""a an and the of to in on for with by from or as is it
its be at this that into one each every show shows make build create want
need app apps gui window qt pyside pyside6 widget widgets use using""".split())


@dataclass
class Example:
    name: str
    path: Path
    title: str
    does: str
    shows: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    qt_classes: List[str] = field(default_factory=list)


_HEADING = re.compile(r"^([A-Z][A-Z ]*[A-Z])(?:\s+(.*))?$")


def sections(doc: str) -> Dict[str, str]:
    """A docstring's all-caps headings (at the start of a line) and the
    text under each; text on the heading's own line comes first."""
    out: Dict[str, str] = {}
    current = ""
    for line in doc.splitlines():
        m = _HEADING.match(line)
        if m:
            current = m.group(1)
            out[current] = (m.group(2) or "") + "\n"
        elif current:
            out[current] += line + "\n"
    return out


def _bullets(text: str) -> List[str]:
    out: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("* "):
            out.append(s[2:])
        elif s and out:
            out[-1] += " " + s
    return out


def requires_of(line: str) -> List[str]:
    """'pandas, numpy, matplotlib' -> the names; 'nothing beyond PySide6'
    -> []."""
    first = line.strip().splitlines()[0] if line.strip() else ""
    if not first or first.lower().startswith("nothing"):
        return []
    return [w for w in re.split(r"[,\s]+", first) if w.isidentifier()]


def parse(path: Path) -> Example:
    source = path.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(source)) or ""
    # "name.py — what it is", joined over wrapped lines -> "what it is".
    head = " ".join(doc.split("\n\n", 1)[0].split())
    title = re.sub(r"^\S+\.py\s+[—-]\s+", "", head) or path.stem
    sec = sections(doc)
    return Example(
        name=path.stem, path=path, title=title,
        does=" ".join(sec.get("WHAT IT DOES", "").split()),
        shows=_bullets(sec.get("SHOWS", "")),
        requires=requires_of(sec.get("REQUIRES", "")),
        qt_classes=sorted(set(re.findall(r"\bQ[A-Z][A-Za-z]+\b", source))))


def names(folder: Path = EXAMPLES_DIR) -> List[str]:
    return sorted(p.stem for p in folder.glob("*.py")
                  if not p.name.startswith("_"))


def load(name: str, folder: Path = EXAMPLES_DIR) -> Example:
    path = folder / f"{name}.py"
    if not path.is_file():
        raise KeyError(f"no Qt example named {name!r}; have: "
                       f"{', '.join(names(folder))}")
    return parse(path)


def all_examples(folder: Path = EXAMPLES_DIR) -> List[Example]:
    return [load(n, folder) for n in names(folder)]


def _words(text: str) -> List[str]:
    return [w for w in _WORD.findall(str(text or "").lower())
            if w not in _STOP]


def score(ex: Example, text: str) -> float:
    """How well a request matches an example. A word in the aliases or the
    title counts most, one in SHOWS next, a Qt class the source uses least
    (QWidget is in all of them and says nothing)."""
    want = set(_words(text))
    if not want:
        return 0.0
    strong = set(_words(ALIASES.get(ex.name, "") + " " + ex.title
                        + " " + ex.name.replace("_", " ")))
    medium = set(_words(" ".join(ex.shows)))
    classes = {c.lower() for c in ex.qt_classes}
    s = 0.0
    for w in want:
        if w in strong:
            s += 3
        elif w in medium:
            s += 1
        elif w in classes:
            s += 0.5
    return s


def select(text: str, k: int = 1, folder: Path = EXAMPLES_DIR
           ) -> List[Example]:
    """The k examples that best match ``text``, best first; [] when none
    matches at all."""
    scored = [(score(ex, text), ex.name, ex) for ex in all_examples(folder)]
    scored = [t for t in scored if t[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored[:k]]


def catalogue(folder: Path = EXAMPLES_DIR) -> str:
    """One line per example: name, path, what it is."""
    lines = []
    for ex in all_examples(folder):
        rel = f"examples/qt_apps/{ex.path.name}"
        lines.append(f"  {ex.name} ({rel}): {ex.title}")
    return "\n".join(lines)


def _outline(source: str) -> str:
    """Classes and their methods, for an example too long to show whole."""
    out = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            out.append(f"class {node.name}:")
            out += [f"    def {n.name}(...)" for n in node.body
                    if isinstance(n, ast.FunctionDef)]
        elif isinstance(node, ast.FunctionDef):
            out.append(f"def {node.name}(...)")
    return "\n".join(out)


def for_prompt(text: str, *, k: int = 1, max_chars: int = PROMPT_CHARS,
               folder: Path = EXAMPLES_DIR) -> str:
    """A QT EXAMPLE block for a prompt about writing Qt code: the best
    matching example(s) whole, and the list of the rest. "" when nothing
    matches, so a prompt about something else pays nothing."""
    picked = select(text, k=k, folder=folder)
    if not picked:
        return ""
    parts = ["QT EXAMPLE APPS (complete, working PySide6 code that passes "
             "the generated-app policy; follow its structure — threads, "
             "signals, model/view, layouts — rather than inventing one):"]
    budget = max_chars
    for ex in picked:
        source = ex.path.read_text(encoding="utf-8")
        header = f"--- examples/qt_apps/{ex.path.name} ---"
        if len(source) <= budget:
            parts.append(f"{header}\n{source}")
            budget -= len(source)
        else:
            doc = ast.get_docstring(ast.parse(source)) or ""
            parts.append(f"{header} (too long to show whole: its docstring "
                         f"and outline)\n\"\"\"{doc}\"\"\"\n"
                         f"{_outline(source)}")
    others = [ex for ex in all_examples(folder)
              if ex.name not in {p.name for p in picked}]
    if others:
        parts.append("Other examples (search_references finds passages "
                     "from them):\n" + "\n".join(
                         f"  {ex.name}: {ex.title}" for ex in others))
    return "\n\n".join(parts)


def attached(references: Sequence[str]) -> bool:
    """Whether a project's or agent's references include this folder."""
    for r in references or ():
        try:
            if Path(r).resolve() == EXAMPLES_DIR:
                return True
        except OSError:
            continue
    return False


__all__ = ["EXAMPLES_DIR", "MAX_EXAMPLES", "Example", "parse", "names",
           "load", "all_examples", "sections", "score", "select", "catalogue",
           "for_prompt", "attached", "requires_of"]
