"""
GUI Designer tab wiring.

The tab is meant to be widget construction and event wiring ONLY — every
decision lives in the gui_* modules. These tests assert that boundary
structurally (the tab must not re-implement layout, emission or policy), verify
the tab is actually reachable from the app, and drive the handlers against a
stub console so the parts that break silently in a GUI — a wrong helper call, a
write outside the vault, work landing on the Tk thread — are caught.

Run:  python -m pytest tests/test_gui_tab.py -q
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE = Path(__file__).resolve().parent.parent / "council_gui_engine.py"


def engine_tree():
    return ast.parse(ENGINE.read_text(encoding="utf-8"))


def method(name: str):
    for node in ast.walk(engine_tree()):
        if isinstance(node, ast.ClassDef) and node.name == "CouncilConsole":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == name:
                    return f
    return None


# ============================================================
# Reachability
# ============================================================

def test_the_tab_is_registered_in_the_builder_tuple():
    """Until this exists the whole subsystem is library code with tests and no
    way in from the app."""
    src = ENGINE.read_text(encoding="utf-8")
    assert "self._build_gui_designer_tab," in src, "not in the builder tuple"
    i_forge = src.index("self._build_tool_forge_tab,")
    i_gui = src.index("self._build_gui_designer_tab,")
    assert i_gui > i_forge, "the brief places it after Tool Creation"


def test_the_tab_method_exists_and_is_labelled():
    fn = method("_build_gui_designer_tab")
    assert fn is not None
    assert "GUI Designer" in ENGINE.read_text(encoding="utf-8")


def test_the_gui_modules_are_imported_behind_a_guard():
    """A missing gui_* module must not stop the whole app from starting."""
    src = ENGINE.read_text(encoding="utf-8")
    assert "_GUI_DESIGNER_OK" in src
    assert "import gui_canvas as _gc" in src


# ============================================================
# The boundary: the tab wires, it does not decide
# ============================================================

def test_the_tab_stays_under_the_line_budget():
    """This is a canary for LOGIC creeping into the tab, not a freeze.

    Baseline was 320 lines with drag/emit/policy. The ports work added rename
    marshalling (~10 lines) and the colour work added window wiring (~20).
    Each feature adds a small band of thin marshalling; when the number
    genuinely balloons the pattern is a re-implemented algorithm, and that
    is what this check catches."""
    fns = [f for f in ast.walk(engine_tree())
           if isinstance(f, ast.FunctionDef)
           and (f.name == "_build_gui_designer_tab" or f.name.startswith("_gd_"))]
    total = sum(f.end_lineno - f.lineno + 1 for f in fns)
    assert total < 450, f"the tab is {total} lines; logic has leaked into it"


def test_the_tab_does_not_reimplement_the_modules():
    """Anything it computed itself would be untestable — this file needs a
    display before a widget can even be imported from it."""
    fns = [f for f in ast.walk(engine_tree())
           if isinstance(f, ast.FunctionDef)
           and (f.name == "_build_gui_designer_tab" or f.name.startswith("_gd_"))]
    src = "\n".join(
        ast.get_source_segment(ENGINE.read_text(encoding="utf-8"), f) or ""
        for f in fns)
    for banned in ("def infer(", "cluster_edges", "def emit(", "rowconfigure(",
                   "ast.parse", "subprocess."):
        assert banned not in src, (
            f"{banned!r} in the tab — that belongs in a gui_* module")


def test_generate_runs_off_the_tk_thread():
    """Classification calls a model. On the Tk thread that freezes the app."""
    fn = method("_gd_generate")
    src = ast.get_source_segment(ENGINE.read_text(encoding="utf-8"), fn)
    assert "threading.Thread" in src, "generate must not block the UI"
    assert "self.after(0" in src, "results must be marshalled back to Tk"


def test_review_is_advisory_and_cannot_edit_code():
    """Auto-editing generated code from a critique pass is a regression
    engine — the review may only produce text."""
    fn = method("_gd_review")
    src = ast.get_source_segment(ENGINE.read_text(encoding="utf-8"), fn)
    assert "DeliberationOrchestrator" in src
    for banned in ("write_text", "emit(", "open(", ".unlink"):
        assert banned not in src, f"review must not {banned}"
    assert "_gd_log" in src, "its only output is the log pane"


def test_opening_another_project_stops_the_running_preview():
    fn = method("_gd_open")
    src = ast.get_source_segment(ENGINE.read_text(encoding="utf-8"), fn)
    assert "_grun.stop" in src, (
        "switching projects must not leave an orphan preview window")


def test_generate_blocks_on_orphans_and_backs_up_first():
    fn = method("_gd_generate")
    src = ast.get_source_segment(ENGINE.read_text(encoding="utf-8"), fn)
    assert "find_orphans" in src, "regeneration must not break app.py silently"
    i_backup, i_emit = src.index("backup("), src.index("emit(")
    assert i_backup < i_emit, "the backup has to happen BEFORE anything is written"
    assert "validate_project" in src, "emitted code is policy-checked"


# ============================================================
# Handlers driven against a stub console
# ============================================================

class StubCanvas:
    def __init__(self, shapes=None):
        self.shapes = list(shapes or [])
        self.dirty = False
        self.kind = None
    def export(self):
        return list(self.shapes)
    def load(self, s):
        self.shapes = list(s)
    def mark_saved(self):
        self.dirty = False
    def set_active_kind(self, k):
        self.kind = k


class _SyncThread:
    """Run the worker inline.

    _gd_generate correctly does its work on a real thread — the tests below
    would otherwise assert before it finished, which is exactly what they did
    on the first run. Making Thread synchronous keeps the test deterministic
    AND surfaces a worker exception in the test instead of swallowing it in a
    daemon thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        if self._t:
            self._t(*self._a, **self._k)

    def join(self, *a, **k):
        pass


@pytest.fixture()
def console(tmp_path, monkeypatch):
    import threading as _th
    import council_gui_engine as cge
    import gui_projects as gp
    monkeypatch.setattr(cge, "VAULT_DIR", tmp_path / "vault", raising=False)
    monkeypatch.setattr(_th, "Thread", _SyncThread)

    class C(cge.CouncilConsole):
        def __init__(self):
            self.logged = []
            self._gd_project = None
            self._gd_questions = []
            self.gui_canvas = StubCanvas()
            class _V:
                def __init__(s): s.v = ""
                def set(s, x): s.v = x
                def get(s): return s.v
            self._gd_status = _V()
        def _gd_log(self, text):
            self.logged.append(str(text))
        def after(self, _ms, fn):
            fn()
    return C(), gp


def test_save_round_trips_through_gui_projects(console, tmp_path):
    from gui_shapes import new_shape
    c, gp = console
    gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    c.gui_canvas.load([new_shape("button", 10, 10)])
    c._gd_save()
    assert len(gp.open_project("demo", vault_dir=tmp_path / "vault").shapes) == 1
    assert not c.gui_canvas.dirty


def test_generate_writes_a_runnable_project(console, tmp_path):
    """The tab's own path, end to end, with no model needed (typed shapes)."""
    from gui_shapes import new_shape
    c, gp = console
    gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    tb = new_shape("toolbar", 0, 0)
    img = new_shape("image_canvas", 0, 40)
    img.w, img.h = 700, 500
    c.gui_canvas.load([tb, img])

    c._gd_generate()
    log = "\n".join(c.logged)
    assert "no untyped shapes" in log, "typed shapes must not call a model"
    assert "policy: OK" in log, log
    pdir = tmp_path / "vault" / "GUI_Projects" / "demo"
    assert (pdir / "ui" / "main_ui.py").exists()
    assert (pdir / "app.py").exists()
    ast.parse((pdir / "ui" / "main_ui.py").read_text(encoding="utf-8"))
    # The name registry was persisted, so the next generation is stable.
    assert gp.load_manifest(pdir).widget_names


def test_generate_refuses_when_hand_written_code_would_break(console, tmp_path):
    from gui_shapes import new_shape
    c, gp = console
    gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    c.gui_canvas.load([new_shape("button", 0, 0, label="Go")])
    c._gd_generate()
    pdir = tmp_path / "vault" / "GUI_Projects" / "demo"
    (pdir / "app.py").write_text(
        "class App:\n    def go(self):\n        self.btn_gone.invoke()\n",
        encoding="utf-8")

    c.logged.clear()
    c._gd_generate()
    log = "\n".join(c.logged)
    assert "BLOCKED" in log and "btn_gone" in log


def test_generate_writes_a_runnable_project_that_carries_ports(console, tmp_path):
    """Step 5+7: `self.ports = Ports(self)` reaches generated code, and the
    manifest keeps the port registry across regenerations."""
    from gui_shapes import new_shape
    c, gp = console
    gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    e = new_shape("entry", 0, 0, label="Scan Folder")
    b = new_shape("button", 0, 60, label="Start scan")
    c.gui_canvas.load([e, b])
    c._gd_generate()
    pdir = tmp_path / "vault" / "GUI_Projects" / "demo"
    src = (pdir / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "self.ports = Ports(self)" in src
    ast.parse((pdir / "ui" / "ports.py").read_text(encoding="utf-8"))
    man = gp.load_manifest(pdir)
    # port_names is stored, and re-generation reuses it
    assert "scan_folder" in set(man.port_names.values())


def test_generate_aliases_a_renamed_port_that_is_still_referenced(console, tmp_path):
    """Step 7 through the tab. Rename the port, keep the old name in app.py,
    regenerate. The Generate log announces the alias; ports.py carries the
    RENAMED entry; app.py's old reference keeps working."""
    from gui_shapes import new_shape
    c, gp = console
    gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    e = new_shape("entry", 0, 0, label="Scan Folder")
    c.gui_canvas.load([e])
    c._gd_generate()                       # first pass writes ports.py

    pdir = tmp_path / "vault" / "GUI_Projects" / "demo"
    # App.py still uses the OLD name.
    (pdir / "app.py").write_text(
        "class App:\n    def start(self):\n"
        "        p = self.ports.scan_folder\n", encoding="utf-8")

    # Now the user renames the port on the shape. load() deep-copied, so the
    # canvas's shape is the one to mutate.
    c.gui_canvas.shapes[0].port = {"name": "scan_dir"}
    c.logged.clear()
    c._gd_generate()
    log = "\n".join(c.logged)
    assert "port renamed" in log and "scan_folder -> scan_dir" in log, log

    ports_src = (pdir / "ui" / "ports.py").read_text(encoding="utf-8")
    assert 'RENAMED["scan_folder"] = "scan_dir"' in ports_src


def test_generate_blocks_when_a_port_is_removed_that_the_app_uses(console, tmp_path):
    """The self-expiring alias covers renames; a genuine DELETE has to block,
    because there is nothing to alias the old name TO."""
    from gui_shapes import new_shape
    c, gp = console
    gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    e = new_shape("entry", 0, 0, label="Scan Folder")
    c.gui_canvas.load([e])
    c._gd_generate()

    pdir = tmp_path / "vault" / "GUI_Projects" / "demo"
    (pdir / "app.py").write_text(
        "class App:\n    def start(self):\n"
        "        p = self.ports.scan_folder\n", encoding="utf-8")

    # Drop the entry — the port is genuinely gone.
    c.gui_canvas.load([new_shape("button", 0, 0, label="Only")])
    c.logged.clear()
    c._gd_generate()
    log = "\n".join(c.logged)
    assert "BLOCKED" in log and "scan_folder" in log


def test_generate_is_disabled_on_a_detached_project(console, tmp_path):
    from gui_shapes import new_shape
    c, gp = console
    pdir = gp.create("demo", vault_dir=tmp_path / "vault")
    c._gd_project = "demo"
    c.gui_canvas.load([new_shape("button", 0, 0, label="Go")])
    gp.detach(pdir)
    c._gd_generate()
    assert any("detached" in m for m in c.logged)
