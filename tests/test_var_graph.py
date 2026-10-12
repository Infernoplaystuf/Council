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
