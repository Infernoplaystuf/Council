"""
council_core.var_graph: which variables in a script feed which.

Each test is a small script with a known answer — "the bad total came from the
discount set in main", "this name is a typo" — checked against the graph.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import var_graph as vg  # noqa: E402

CART = textwrap.dedent('''
    import math

    RATE = 0.07

    def load(path):
        rows = []
        with open(path) as fh:
            for line in fh:
                rows.append(float(line))
        return rows

    def taxed(amount, rate=RATE):
        if amount < 0:
            amount = 0
        return amount * (1 + rate)

    class Cart:
        def __init__(self, items):
            self.items = items
            self.discount = 0

        def total(self):
            subtotal = sum(self.items)
            unused = 5
            return taxed(subtotal - self.discount)

    def main():
        prices = load("prices.txt")
        cart = Cart(prices)
        cart.discount = len(prices) * 2
        total = cart.total()
        rounded = math.floor(totl)
        print(total)
''')


@pytest.fixture(scope="module")
def cart():
    return vg.analyze(CART, "cart.py")


def ups(g, ref, **kw):
    return {s.var.id for s in g.upstream(g.resolve(ref).id, **kw)}


def downs(g, ref, **kw):
    return {s.var.id for s in g.downstream(g.resolve(ref).id, **kw)}


def test_upstream_follows_calls_methods_and_attributes(cart):
    up = ups(cart, "main.total")
    # through the method, the helper it calls, its default, and the
    # attribute main sets on the object
    for vid in ("Cart.total::<return>", "taxed::<return>", "taxed::amount",
                "taxed::rate", "<module>::RATE", "Cart::self.discount",
                "Cart::self.items", "main::prices", "load::rows", "load::path"):
        assert vid in up, vid
    assert "<module>::math" not in up


def test_downstream_reaches_the_result(cart):
    down = downs(cart, "main.prices")
    assert {"Cart.__init__::items", "Cart::self.items", "Cart::self.discount",
            "main::total"} <= down


def test_path_names_each_line(cart):
    chain = cart.path("<module>::RATE", "main::total")
    assert [e.line for e in chain] == [13, 16, 26, 32]
    assert chain[0].code == "def taxed(amount, rate=RATE):"


def test_edge_kinds(cart):
    kinds = {(e.src, e.dst): e.kind for e in cart.edges}
    assert kinds[("load::line", "load::rows")] == "mutate"
    assert kinds[("load::fh", "load::line")] == "iter"
    assert kinds[("load::path", "load::fh")] == "with"
    assert kinds[("main::prices", "Cart.__init__::items")] == "arg"
    assert kinds[("Cart.__init__::items", "Cart::self.items")] == "assign"


def test_findings_point_at_likely_bugs(cart):
    got = {(f.kind, f.name) for f in cart.findings}
    assert ("undefined", "totl") in got
    assert ("unused", "unused") in got
    assert ("shadowed-param", "amount") in got
    assert not any(f.name in ("print", "sum", "len", "float") for f in cart.findings)


def test_conditions_are_opt_in():
    src = "def f(flag, a, b):\n    if flag:\n        x = a\n    else:\n        x = b\n    return x\n"
    plain = vg.analyze(src)
    assert "f::flag" not in ups(plain, "f.x")
    assert {"f::a", "f::b"} <= ups(plain, "f.x")
    with_c = vg.analyze(src, conditions=True)
    assert "f::flag" in ups(with_c, "f.x")
    assert "f::flag" not in ups(with_c, "f.x", conditions=False)


def test_scopes_globals_and_comprehensions():
    src = textwrap.dedent('''
        count = 0
        def bump(n):
            global count
            count += n
        def squares(xs):
            return [x * x for x in xs if x]
        def outer():
            y = 1
            def inner():
                return y
            return inner()
    ''')
    g = vg.analyze(src)
    assert "<module>::count" in g.vars and "bump::count" not in g.vars
    assert "bump::n" in ups(g, "count")
    assert "squares::x" not in g.vars          # the comprehension's own
    assert "squares::xs" in ups(g, "squares::<return>")
    assert "outer::y" in ups(g, "outer::<return>")
    assert not [f for f in g.findings if f.kind == "undefined"]


def test_unpacking_subscripts_walrus_and_match():
    src = textwrap.dedent('''
        def f(pair, d, k, cmd):
            a, (b, *c) = pair
            d[k] = a
            if (n := len(c)) > 1:
                pass
            match cmd:
                case {"go": where}:
                    out = where
            return d, n, out
    ''')
    g = vg.analyze(src)
    for name in ("a", "b", "c"):
        assert "f::pair" in ups(g, f"f.{name}")
    assert {"f::a", "f::k"} <= ups(g, "f.d")
    assert "f::c" in ups(g, "f.n")
    assert "f::cmd" in ups(g, "f.out")


def test_resolve_and_find(cart):
    assert cart.resolve("RATE").id == "<module>::RATE"
    assert cart.resolve("main.total").id == "main::total"
    two = vg.analyze("def a(x):\n    return x\ndef b(x):\n    return x\n")
    with pytest.raises(KeyError, match="ambiguous"):
        two.resolve("x")
    assert two.resolve("b.x").id == "b::x"
    with pytest.raises(KeyError, match="no variable"):
        cart.resolve("nope")
    assert cart.find("disc")[0].name == "self.discount"


def test_trace_and_exports(cart):
    text = vg.format_trace(cart, cart.resolve("main.total"), "up")
    assert "RATE" in text and "L13" in text and "see above" in text
    dot = cart.to_dot()
    assert dot.startswith("digraph") and '"<module>::RATE" ->' in dot
    d = cart.to_dict()
    assert len(d["vars"]) == len(cart.vars) and d["findings"]


def test_command_line(tmp_path, capsys):
    script = tmp_path / "cart.py"
    script.write_text(CART, encoding="utf-8")
    assert vg.main([str(script)]) == 0
    assert "totl" in capsys.readouterr().out
    assert vg.main([str(script), "--up", "main.total", "--depth", "2"]) == 0
    assert "Cart.total() return" in capsys.readouterr().out
    assert vg.main([str(script), "--path", "RATE", "main.total"]) == 0
    assert "L32" in capsys.readouterr().out
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")
    assert vg.main([str(bad)]) == 2


def test_every_script_in_the_repo_analyzes():
    """No crash on real code: the Council's own sources are the corpus."""
    root = Path(__file__).resolve().parent.parent
    files = [p for p in sorted(root.glob("council_core/*.py"))] + \
            [p for p in sorted(root.glob("*.py"))][:40]
    assert files
    for p in files:
        try:
            g = vg.analyze_file(p)
        except SyntaxError:
            continue
        for vid in list(g.vars)[:10]:
            g.upstream(vid)
            g.downstream(vid)


# ----------------------------------------------------------------------
# across files: a GUI and the scripts it calls
# ----------------------------------------------------------------------
#: Shaped like a GUI Designer project (main.py, app.py = HandlerMixin +
#: MainUi, handlers.py, ui/ports.py) with the scripts it hands values to:
#: by a direct call (frame_timing), a thread (scripts/analysis) and a
#: subprocess (worker.py), and a config module read both ways.
GUI_PROJECT = {
    "main.py": '''
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent))
        from app import App

        if __name__ == "__main__":
            App().run()
    ''',
    "app.py": '''
        from handlers import HandlerMixin
        from ui.main_ui import MainUi


        class App(HandlerMixin, MainUi):
            def run(self):
                self.build()
    ''',
    "handlers.py": '''
        import subprocess
        import sys
        import threading

        from scripts.analysis import summarize


        class HandlerMixin:
            def on_scan(self):
                try:
                    from frame_timing import scan_report
                    result = scan_report(self.ports.capture_folder.get())
                    self.ports.bad_count.set(result["count"])
                except Exception as exc:
                    self.report_error("Scan", exc)

            def on_export(self):
                folder = self.ports.capture_folder.get()
                limit = self.ports.limit.get()
                subprocess.run([sys.executable, "worker.py", folder, str(limit)])

            def on_summary(self):
                t = threading.Thread(target=summarize,
                                     args=(self.ports.capture_folder.get(),))
                t.start()

            def report_error(self, title, exc):
                self.last_error = f"{title}: {exc}"
                self.status_text = self.last_error
    ''',
    "ui/__init__.py": "",
    "ui/ports.py": '''
        class _TextPort:
            def __init__(self, name, widget):
                self.name = name
                self.widget = widget


        class Ports:
            def __init__(self, ui):
                self.capture_folder = _TextPort("capture_folder", ui.folder_edit)
                self.limit = _TextPort("limit", ui.limit_spin)
                self.bad_count = _TextPort("bad_count", ui.count_label)
    ''',
    "ui/main_ui.py": '''
        from .ports import Ports


        class MainUi:
            def build(self):
                self.ports = Ports(self)
                self.status_text = ""
    ''',
    "frame_timing.py": '''
        import numpy as np

        import config
        from config import THRESHOLD


        def list_frames(folder):
            return sorted(folder.glob("*.png"))


        def scan_report(folder):
            frames = list_frames(folder)
            gaps = np.diff([f.stat().st_mtime for f in frames])
            bad = [g for g in gaps if g > THRESHOLD * config.SCALE]
            return {"count": len(bad), "names": bad}
    ''',
    "config.py": '''
        THRESHOLD = 0.5
        SCALE = 2
    ''',
    "scripts/analysis.py": '''
        def summarize(folder):
            total = len(list(folder.iterdir()))
            return total
    ''',
    "worker.py": '''
        import argparse
        import sys

        folder = sys.argv[1]
        limit = int(sys.argv[2])
        parser = argparse.ArgumentParser()
        args = parser.parse_args()
        print(folder, limit, args)
    ''',
    # Not part of the project: the Designer's backups are skipped.
    ".backups/old_handlers.py": "def broken(:\n",
}


def write_project(root: Path, files=GUI_PROJECT) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


@pytest.fixture
def gui(tmp_path):
    return vg.analyze_paths([write_project(tmp_path / "proj")])


def test_project_files_names_modules_and_skips_backups(tmp_path):
    root = write_project(tmp_path / "proj")
    mods = vg.project_files([root])
    assert set(mods) == {"main", "app", "handlers", "ui", "ui.ports",
                         "ui.main_ui", "frame_timing", "config",
                         "scripts.analysis", "worker"}
    assert mods["ui"].endswith("__init__.py")


def test_gui_port_flows_into_the_called_script_and_back(gui):
    port = gui.resolve("ports.capture_folder")
    assert port.kind == "port"
    down = downs(gui, "ports.capture_folder")
    assert "frame_timing:scan_report::folder" in down       # direct call
    assert "frame_timing:list_frames::folder" in down       # and on within it
    assert "ports.bad_count" in {gui.vars[v].name for v in down}   # result shown
    chain = gui.path(port.id, "<gui ports>::ports.bad_count")
    files = [Path(e.file).name for e in chain]
    assert files[0] == "handlers.py" and "frame_timing.py" in files
    assert files[-1] == "handlers.py"


def test_the_widget_feeds_its_port(gui):
    up = ups(gui, "ports.capture_folder")
    assert "ui.ports:Ports.__init__::ui" in up


def test_subprocess_arguments_become_the_scripts_argv(gui):
    up = ups(gui, "worker.folder")
    assert {"worker:<module>::sys.argv[1]", "handlers:HandlerMixin.on_export::folder",
            "<gui ports>::ports.capture_folder"} <= up
    assert "<gui ports>::ports.limit" in ups(gui, "worker.limit")
    assert "<gui ports>::ports.limit" not in ups(gui, "worker.folder")
    # argparse reads all of sys.argv
    assert "<gui ports>::ports.capture_folder" in ups(gui, "worker.args")


def test_thread_target_gets_its_args(gui):
    assert "<gui ports>::ports.capture_folder" in ups(gui, "scripts.analysis.summarize.total")


def test_module_variables_cross_files(gui):
    up = ups(gui, "frame_timing.scan_report.bad")
    assert "config:<module>::THRESHOLD" in up              # from config import
    assert "config:<module>::SCALE" in up                  # config.SCALE
    kinds = {(e.src, e.dst): e.kind for e in gui.edges}
    assert kinds[("config:<module>::THRESHOLD",
                  "frame_timing:<module>::THRESHOLD")] == "import"


def test_outside_packages_are_not_followed(gui):
    # numpy is a black box: np is a name in frame_timing, nothing inside it
    assert not any(v.module == "numpy" or v.scope.startswith("numpy")
                   for v in gui.vars.values())
    assert "frame_timing:<module>::np" in ups(gui, "frame_timing.scan_report.gaps")


def test_mixins_share_self_attributes_and_methods(gui):
    # App(HandlerMixin, MainUi): MainUi sets self.status_text, the mixin's
    # report_error sets it too - one node, in the App family
    v = gui.resolve("self.status_text")
    assert v.scope == "app:App"
    assert "app:App::self.last_error" in ups(gui, "self.status_text")
    # self.report_error(...) inside the mixin lands in the mixin's method
    assert "handlers:HandlerMixin.on_scan::exc" in ups(gui, "handlers.HandlerMixin.report_error.exc")
    assert not [f for f in gui.findings if f.kind == "undefined"]


def test_relative_imports_and_findings_carry_files(tmp_path):
    root = write_project(tmp_path / "pkg", {
        "pkg/__init__.py": "",
        "pkg/a.py": "from .b import helper\n\ndef run(x):\n    y = helper(x)\n    return y\n",
        "pkg/b.py": "def helper(v):\n    unused = 1\n    return v * 2\n",
    })
    g = vg.analyze_paths([root])
    assert "pkg.a:run::x" in ups(g, "pkg.b.helper.v")
    f = next(f for f in g.findings if f.kind == "unused")
    assert f.file.endswith("b.py") and "pkg.b" in f.message


def test_a_broken_file_is_skipped_and_reported(tmp_path):
    root = write_project(tmp_path / "p", {"good.py": "x = 1\n",
                                          "bad.py": "def (:\n"})
    g = vg.analyze_paths([root])
    assert "good:<module>::x" in g.vars
    assert any(f.kind == "syntax" and f.file.endswith("bad.py") for f in g.findings)


def test_files_and_folders_mix(tmp_path):
    root = write_project(tmp_path / "gui", {"gui.py": "from tool import go\n\ndef click(v):\n    go(v)\n"})
    lib = write_project(tmp_path / "elsewhere", {"tool.py": "def go(n):\n    return n\n"})
    g = vg.analyze_paths([root, lib / "tool.py"])
    assert "gui:click::v" in ups(g, "tool.go.n")


def test_command_line_on_a_folder(tmp_path, capsys):
    root = write_project(tmp_path / "proj")
    assert vg.main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "10 files" in out
    assert vg.main([str(root), "--up", "worker.folder"]) == 0
    out = capsys.readouterr().out
    assert "handlers.py:" in out and "ports.capture_folder" in out
    assert vg.main([str(tmp_path / "nope")]) == 2


# ----------------------------------------------------------------------
# the trace as text
# ----------------------------------------------------------------------
def test_trace_report_has_both_directions_and_findings(cart):
    text = vg.trace_report(cart, cart.resolve("taxed.amount"), max_depth=3)
    assert text.startswith("Variable Map trace")
    assert "Variable    : amount  (taxed)" in text
    feeds = text.index("WHAT FEEDS IT")
    fed = text.index("WHAT IT FEEDS")
    assert feeds < fed
    assert "subtotal  (Cart.total)" in text[feeds:fed]        # upstream
    assert "taxed() return" in text[fed:]                       # downstream
    assert "WORTH A LOOK ON THIS PATH" in text
    assert "overwritten on line 15" in text
    assert "Depth       : 3 hops; if/while conditions off" in text


def test_trace_report_says_nothing_when_nothing(cart):
    text = vg.trace_report(cart, cart.resolve("main.total"))
    assert "WHAT IT FEEDS (where its value goes)\n  (nothing)" in text
    assert "Depth       : all" in text


def test_trace_report_across_files_names_them(gui):
    text = vg.trace_report(gui, gui.resolve("worker.folder"))
    assert "10 files under" in text
    assert "handlers.py:" in text and "ports.capture_folder" in text


def test_write_text_replaces_whole(tmp_path):
    out = tmp_path / "t" / "trace.txt"
    vg.write_text(out, "first")
    vg.write_text(out, "├─ second")
    assert out.read_text(encoding="utf-8") == "├─ second\n"
    assert [p.name for p in out.parent.iterdir()] == ["trace.txt"]


def test_command_line_trace(tmp_path, capsys):
    script = tmp_path / "cart.py"
    script.write_text(CART, encoding="utf-8")
    assert vg.main([str(script), "--trace", "main.total"]) == 0
    assert "WHAT FEEDS IT" in capsys.readouterr().out
    out = tmp_path / "trace.txt"
    assert vg.main([str(script), "--trace", "main.total", "--out", str(out)]) == 0
    assert "WHAT FEEDS IT" in out.read_text(encoding="utf-8")
