"""
council_core.dream3d — the Dream3D tab's pipelines and pipeline chat.

TWO THINGS LIVE HERE

The picker: scan vault/pipelines/in/, label each pipeline
`name  (format, N steps)`, and resolve a row back to its Pipeline BY INDEX. Tk
had three copies of that index lookup, and a comment recording that building a
path from the displayed label was already a bug once.

The pipeline chat: the RESPONSES behind council_core.pipeline_intent — list,
show, explain, validate, graph, to-python, create, modify, export, compare —
moved from the Tk engine's `_pipeline_*_response` methods
(council_gui_engine.py:5323-5770). Each one there ended in
`_append_transcript`; here each reports through a `say(who, text, kind)`
callback the caller supplies, and does not decide its thread.

`PipelineChat.plan(text)` is the whole interface: None means "not a pipeline
command, let the Council have it"; otherwise it returns a job to run on a
worker. The decision is fast (regexes, and for to-python a directory scan, as
in Tk); the job may call a model or the nx env and take a minute.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from . import nx_ops
from . import pipeline_intent as intents

Say = Callable[[str, str, str], None]


# ============================================================
# The picker
# ============================================================

def in_dir(vault_dir: Path) -> Path:
    import pipeline_scanner
    return pipeline_scanner.vault_pipelines_in_dir(vault_dir)


def out_dir(vault_dir: Path) -> Path:
    import pipeline_scanner
    return pipeline_scanner.vault_pipelines_out_dir(vault_dir)


def scan(vault_dir: Path) -> Tuple[Path, List[Any]]:
    """(in/ folder, pipelines in it). Raises what the scanner raises."""
    import pipeline_scanner
    folder = in_dir(vault_dir)
    return folder, pipeline_scanner.scan_pipelines(folder)


def steps_note(pl) -> str:
    n = len(pl.steps)
    return f"({pl.format}, {n} step{'s' if n != 1 else ''})"


def label_for(pl) -> str:
    """Display only. Never parsed back — rows resolve by index."""
    return f"{pl.name}  {steps_note(pl)}"


def empty_message(folder: Path) -> str:
    return ("No pipelines found.\n\nAdd simplnx .py scripts or .dream3d files "
            f"to:\n  {folder}\n\nThen click ↻ Refresh.")


def render(pl) -> str:
    import pipeline_scanner
    try:
        return pipeline_scanner.render_pipeline(pl)
    except Exception as exc:                              # noqa: BLE001
        return f"render failed: {exc!r}"


def transformation_cube() -> Optional[Path]:
    """The bundled cube -> 4x4 matrix tool, or None.

    Tk looked under APP_DIR — the STATE root (~/.council), not the install —
    so the button could never find its asset in a source run. The asset ships
    beside the code, and in a PyInstaller bundle under sys._MEIPASS.
    """
    import sys
    roots = [Path(__file__).resolve().parent.parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    for root in roots:
        html = root / "assets" / "transformation_cube.html"
        if html.is_file():
            return html
    return None


# ============================================================
# The pipeline chat
# ============================================================

class PipelineChat:
    """Answers pipeline commands. ``say`` must be safe from a worker."""

    def __init__(self, vault_dir: Path, say: Say,
                 on_changed: Optional[Callable[[], None]] = None,
                 *, scanner: Any = None, editor: Any = None,
                 bridge: Any = None):
        self.vault_dir = Path(vault_dir)
        self.say = say
        self.on_changed = on_changed
        self._scanner = scanner
        self._editor = editor
        self._bridge = bridge

    # -- modules, injectable for tests ---------------------------------
    @property
    def ps(self):
        if self._scanner is None:
            import pipeline_scanner
            self._scanner = pipeline_scanner
        return self._scanner

    @property
    def pe(self):
        if self._editor is None:
            import pipeline_editor
            self._editor = pipeline_editor
        return self._editor

    def find(self, name: str):
        return self.ps.find_pipeline_by_name(self.vault_dir, name)

    def _rel(self, path: Path):
        try:
            return Path(path).relative_to(self.vault_dir)
        except Exception:                                 # noqa: BLE001
            return path

    def _changed(self) -> None:
        if self.on_changed is not None:
            self.on_changed()

    def _missing(self, name: str, hint: bool = True) -> None:
        self.say("Writer", f"No pipeline matching '{name}'."
                 + (" Type 'list pipelines' to see what's available."
                    if hint else ""), "final")

    # -- routing -----------------------------------------------------------
    def plan(self, text: str) -> Optional[Callable[[], None]]:
        """A job for this message, or None if it is not a pipeline command."""
        for intent in intents.candidates(text):
            job = self._decide(intent)
            if job is not None:
                return job
        return None

    def _decide(self, intent: intents.Intent) -> Optional[Callable[[], None]]:
        a = intent.args
        if intent.action == "to_python":
            return self._decide_to_python(a[0])
        jobs = {
            "list": lambda: self.list(),
            "show": lambda: self.show(a[0]),
            "modify": lambda: self.modify(a[0], a[1], intent.text),
            "explain": lambda: self.explain(a[0]),
            "validate": lambda: self.validate(a[0]),
            "graph": lambda: self.graph(a[0]),
            "create": lambda: self.create(a[0]),
            "export": lambda: self.export_markdown(a[0]),
            "compare": lambda: self.compare(a[0], a[1]),
        }
        return jobs.get(intent.action)

    # -- the responses -----------------------------------------------------
    def list(self) -> None:
        folder = self.ps.vault_pipelines_in_dir(self.vault_dir)
        pipelines = self.ps.scan_pipelines(folder)
        if not pipelines:
            self.say("Writer", f"No pipelines found in {folder}. Drop .py "
                     "simplnx scripts or .dream3d files in there and I'll "
                     "see them.", "final")
            return
        lines = [f"Found {len(pipelines)} pipeline"
                 f"{'s' if len(pipelines) != 1 else ''} in "
                 "vault/pipelines/in/:"]
        lines += [f"  • {pl.name} {steps_note(pl)}" for pl in pipelines]
        self.say("Writer", "\n".join(lines), "final")

    def show(self, name: str) -> None:
        pl = self.find(name)
        if pl is None:
            self.say("Writer", f"No pipeline matching '{name}' under "
                     "vault/pipelines/in/. Type 'list pipelines' to see "
                     "what's available.", "final")
            return
        self.say("Writer", self.ps.render_pipeline(pl), "final")

    def explain(self, name: str) -> None:
        pl = self.find(name)
        if pl is None:
            return self._missing(name)
        self.say("Council", f"Asking model to describe {pl.name}…",
                 "observation")
        try:
            desc = self.pe.pipeline_to_natural_language(pl)
        except Exception as exc:                          # noqa: BLE001
            self.say("Writer", f"description failed: {exc!r}", "final")
            return
        self.say("Writer", f"{pl.name}:\n\n{desc}", "final")

    def validate(self, name: str) -> None:
        pl = self.find(name)
        if pl is None:
            return self._missing(name, hint=False)
        issues = self.ps.validate_pipeline_params(pl)
        if not issues:
            n = len(pl.steps)
            self.say("Writer", f"{pl.name}: no issues found in {n} step"
                     f"{'s' if n != 1 else ''} (against known simplnx "
                     "schema).", "final")
            return
        self.say("Writer", "\n".join([f"{pl.name}: {len(issues)} issue(s):"]
                                     + [f"  • {i}" for i in issues]), "final")

    def graph(self, name: str) -> None:
        pl = self.find(name)
        if pl is None:
            return self._missing(name, hint=False)
        self.say("Writer", self.ps.pipeline_dependency_graph(pl), "final")

    def compare(self, a_name: str, b_name: str) -> None:
        a, b = self.find(a_name), self.find(b_name)
        if a is None or b is None:
            missing = [n for n, pl in ((a_name, a), (b_name, b)) if pl is None]
            self.say("Writer", f"Could not find: {', '.join(missing)}. Type "
                     "'list pipelines'.", "final")
            return
        self.say("Writer", self.ps.compare_pipelines(a, b), "final")

    def export_markdown(self, name: str) -> None:
        pl = self.find(name)
        if pl is None:
            return self._missing(name, hint=False)
        try:
            md = self.ps.export_pipeline_to_markdown(pl)
            folder = nx_ops.out_dir(self.vault_dir)
            folder.mkdir(parents=True, exist_ok=True)
            stem = Path(pl.name).stem
            path, n = folder / f"{stem}.md", 2
            while path.exists():
                path, n = folder / f"{stem}_v{n}.md", n + 1
            path.write_text(md, encoding="utf-8")
        except Exception as exc:                          # noqa: BLE001
            self.say("Writer", f"Markdown export failed: {exc!r}", "final")
            return
        self.say("Writer", f"Exported {pl.name} -> {self._rel(path)}", "final")

    def create(self, description: str) -> None:
        self.say("Council", f"Generating new pipeline from: {description}",
                 "observation")
        tokens = [t for t in re.split(r"\W+", description) if len(t) >= 3][:4]
        suggested = "_".join(t.lower() for t in tokens) or "generated_pipeline"
        try:
            path, log = self.pe.generate_pipeline_from_description(
                description, self.vault_dir, suggested_name=suggested)
        except Exception as exc:                          # noqa: BLE001
            self.say("Writer", f"generation failed: {exc!r}", "final")
            return
        if not path:
            self.say("Writer", f"couldn't generate pipeline: {log}", "final")
            return
        self.say("Writer", f"Saved new pipeline: {self._rel(path)}\n\nReview "
                 f"with: show pipeline {Path(path).name}", "final")
        self._changed()

    def modify(self, name: str, change: str, full_request: str) -> None:
        pl = self.find(name)
        if pl is None:
            self.say("Writer", f"No pipeline matching '{name}' to modify. "
                     "Type 'list pipelines' to see what's available.",
                     "final")
            return
        self.say("Council", f"Modifying {pl.name}: {change}", "observation")
        self.say("Writer", self.ps.render_pipeline(pl), "observation")
        try:
            result = self.pe.modify_pipeline_by_request(
                pl.path, full_request, self.vault_dir)
        except Exception as exc:                          # noqa: BLE001
            self.say("Writer", f"Pipeline edit failed: {exc!r}", "final")
            return
        if not result.success:
            self.say("Writer", f"Couldn't apply that change:\n  {result.error}",
                     "final")
            if result.log:
                self.say("Council", "Partial log:\n  "
                         + "\n  ".join(result.log), "observation")
            return
        lines = [f"Saved new version: {self._rel(result.new_path)}", "",
                 "Edits applied:"] + [f"  • {line}" for line in result.log]
        self.say("Writer", "\n".join(lines), "final")
        self._changed()

    # -- to Python: the one that can decline ---------------------------------
    def _decide_to_python(self, raw: str) -> Optional[Callable[[], None]]:
        """Claim the message only if it names a pipeline — or names none and
        there are pipelines to choose from. Otherwise decline, so "create a
        pipeline that writes a python file" still reaches create."""
        cleaned = intents.clean_ref(raw)
        pl = self.find(cleaned) if cleaned else None
        if pl is not None:
            return lambda: self.to_python(pl)
        try:
            pls = self.ps.scan_pipelines(
                self.ps.vault_pipelines_in_dir(self.vault_dir))
        except Exception:                                 # noqa: BLE001
            pls = []
        if not pls:
            return None
        if len(pls) == 1 and not cleaned:
            only = pls[0]
            return lambda: self.to_python(only)
        return lambda: self.choose_for_python(cleaned, pls)

    def choose_for_python(self, cleaned: str, pls: List[Any]) -> None:
        listing = "\n".join(f"  • {p.name}" for p in pls[:60])
        which = (f" I couldn't find a pipeline matching {cleaned!r}."
                 if cleaned else "")
        self.say("Writer", f"Which pipeline should I convert to Python?{which}"
                 f"\nAvailable in vault/pipelines/in/:\n{listing}\n\n"
                 f"Try: convert {pls[0].name} to python", "final")

    def to_python(self, pl) -> None:
        if Path(pl.path).suffix.lower() == ".py":
            self.say("Writer", f"{pl.name} is already a Python script — open "
                     "it from vault/pipelines/in/. This converts a saved "
                     ".d3dpipeline into Python.", "final")
            return
        res = nx_ops.transpile(pl, self.vault_dir, bridge=self._bridge,
                               heading="converted (deterministically, no "
                                       "model) →")
        if res.ok:
            self.say("Writer", res.body, "final")
        else:
            self.say("Writer", f"Could not convert {pl.name} to Python.\n\n"
                     f"{res.body}\n\nThis path is deterministic (no model), "
                     "so a failure here is the DREAM3D-NX catalog being "
                     "unavailable — run 'Check env' in the Dream3D tab.",
                     "final")
