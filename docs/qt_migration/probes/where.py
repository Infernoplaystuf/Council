"""
where.py — what line is this symbol on, today?

WHY THIS EXISTS
docs/qt_migration/phase6_port_requirements.md cites about ninety `file:line`
references, and every one of them was correct when it was written and wrong a
day later: extracting the deliberation took 1,062 lines out of
council_gui_engine.py, so everything below line ~3,500 shifted by an amount
that varies with where you look.

A document full of stale line numbers is worse than one with none — a reader
who opens the file at the cited line finds unrelated code, and cannot tell
whether the finding was wrong or the line moved. Line numbers are not a stable
address in a file that is actively being emptied. SYMBOL NAMES ARE.

So: cite the symbol, and use this to find it.

    python docs/qt_migration/probes/where.py _grapher_export
    python docs/qt_migration/probes/where.py _vmgr_ save          # substring
    python docs/qt_migration/probes/where.py --file graph_engine.py render

It parses rather than greps, so it finds the DEFINITION rather than every
mention, and it reports the span so you know how much code you are looking at.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

#: Searched in this order when --file is not given. The engine first because
#: that is where most of the citations point.
DEFAULT_FILES = (
    "council_gui_engine.py",
    "graph_engine.py",
    "graph_data.py",
    "plots_pane.py",
    "vault_analyst.py",
    "council_engine.py",
)


def definitions(path: Path):
    """(name, line, end_line, kind) for everything defined in a file.

    Methods are included and qualified, because most of what the documents
    cite is a method on CouncilConsole rather than a module-level name.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        print(f"  ! {path.name}: {exc}", file=sys.stderr)
        return

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield (prefix + child.name, child.lineno, child.end_lineno,
                       "method" if prefix else "function")
                yield from walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                yield (prefix + child.name, child.lineno, child.end_lineno,
                       "class")
                yield from walk(child, child.name + ".")
            elif isinstance(child, ast.Assign) and not prefix:
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        yield (target.id, child.lineno, child.end_lineno,
                               "assignment")

    yield from walk(tree)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Find a symbol's current line, by parsing rather than "
                    "grepping.")
    parser.add_argument("name", help="exact name, or a substring to match")
    parser.add_argument("--file", "-f", action="append", default=[],
                        help="limit the search to this file (repeatable)")
    parser.add_argument("--all", "-a", action="store_true",
                        help="search every .py at the repo root")
    args = parser.parse_args(argv)

    if args.all:
        files = sorted(p.name for p in ROOT.glob("*.py"))
    else:
        files = args.file or list(DEFAULT_FILES)

    needle = args.name.lower()
    hits = []
    for name in files:
        path = ROOT / name
        if not path.exists():
            continue
        for symbol, line, end, kind in definitions(path):
            plain = symbol.split(".")[-1].lower()
            if needle == plain or needle == symbol.lower():
                hits.append((0, name, symbol, line, end, kind))
            elif needle in symbol.lower():
                hits.append((1, name, symbol, line, end, kind))

    if not hits:
        print(f"no definition of {args.name!r} in: {', '.join(files)}")
        print("(try --all, or --file <name>; this finds DEFINITIONS, not uses)")
        return 1

    hits.sort()                       # exact matches first
    exact = [h for h in hits if h[0] == 0]
    shown = exact if exact else hits
    if exact and len(hits) > len(exact):
        print(f"{len(exact)} exact, {len(hits) - len(exact)} partial "
              f"(re-run with the full name to see only exact)\n")

    for _, file_name, symbol, line, end, kind in shown[:40]:
        span = end - line + 1
        print(f"{file_name}:{line}  {symbol}  ({kind}, {span} lines, "
              f"through :{end})")
    if len(shown) > 40:
        print(f"... and {len(shown) - 40} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
