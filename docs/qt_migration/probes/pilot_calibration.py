"""
pilot_calibration.py — what the Vault pilot actually cost, measured.

Phase 5's exit criterion in docs/qt_full_port_scope.md is not "a working tab",
it is a re-forecast. This probe produces the inputs to that re-forecast:

  * the Tk toolkit surface of the Vault tab (the unit the estimate is in)
  * the Qt lines written to replace it
  * the logic lines pulled out into council_core on the way
  * how much of the Tk engine the extraction deleted
  * which Vault commands the Qt tab answers, and which it does not

It counts source, not calendar. A rate in days needs a human with a clock;
what can be measured honestly here is the MULTIPLIER — Qt lines written per
Tk toolkit line replaced — and the coverage. Both are what the estimate is
actually built on, so both are what the re-forecast needs.

Run:  python docs/qt_migration/probes/pilot_calibration.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

TOOLKIT = re.compile(
    r"\b(tk|ttk|tkinter|tkfont|scrolledtext|filedialog|messagebox|simpledialog)\b"
    r"\s*\.|"
    r"\b(pack|grid|place|configure|config|bind|winfo_|after|insert|delete)\s*\(")

# The widget/callback words that make a line toolkit-bound. Deliberately the
# same rule port_surface_by_tab.py uses, so the numbers are comparable.
TK_HINTS = ("tk.", "ttk.", "tkfont.", "scrolledtext.", "filedialog.",
            "messagebox.", "simpledialog.", ".pack(", ".grid(", ".place(",
            ".bind(", ".winfo_", ".insert(", "StringVar", "BooleanVar",
            "IntVar", "Treeview", "PanedWindow", "Toplevel")


def toolkit_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines()
               if any(h in ln for h in TK_HINTS))


def code_lines(path: Path) -> int:
    """Lines that are neither blank nor a comment. Docstrings count — they are
    written, reviewed and maintained like anything else."""
    n = 0
    for ln in path.read_text(encoding="utf-8").splitlines():
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            n += 1
    return n


def vault_region(engine: str) -> str:
    """Every _vmgr_* method plus the vault tab builder, as one blob."""
    tree = ast.parse(engine)
    lines = engine.splitlines()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_vmgr_") or "vault" in node.name.lower():
                out += lines[node.lineno - 1:node.end_lineno]
    return "\n".join(out)


def vault_commands(engine: str) -> set[str]:
    """The Vault tab's wired actions, as the Tk shell names them."""
    found = set()
    for match in re.finditer(r"command=self\.(_vmgr_\w+|_show_rag_misses)", engine):
        found.add(match.group(1))
    return found



def vault_surface_from_the_estimates_probe() -> int:
    """The Vault's toolkit-line count as port_surface_by_tab.py counts it.

    That probe is where the 5,375-line scope figure comes from, so it is the
    unit the forecast is in. Running it rather than re-implementing it means
    there is one definition of "a toolkit line", not two that drift.
    """
    import subprocess
    probe = Path(__file__).with_name("port_surface_by_tab.py")
    try:
        out = subprocess.run([sys.executable, str(probe)], capture_output=True,
                             text=True, timeout=180, cwd=str(ROOT))
    except Exception:                                     # noqa: BLE001
        return 0
    for line in out.stdout.splitlines():
        if line.startswith("Vault "):
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return 0


def needs_council_tab(qt_src: str) -> list:
    """Qt handlers that exist but say, in the log, that they cannot finish yet.

    Found by parsing rather than grepping, so a mention of "Council tab" in a
    docstring or a neighbouring method cannot be mistaken for one.
    """
    out = []
    for node in ast.walk(ast.parse(qt_src)):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("on_"):
            continue
        # Skip the docstring. A handler may legitimately DISCUSS the Council
        # tab — one of them documents that it used to claim a dependency on it
        # and does not have one — and counting that as a stub is how a probe
        # reports work as undone after it is done.
        body = node.body[1:] if (node.body
                                 and isinstance(node.body[0], ast.Expr)
                                 and isinstance(node.body[0].value, ast.Constant)
                                 and isinstance(node.body[0].value.value, str)
                                 ) else node.body
        for statement in body:
            for literal in ast.walk(statement):
                if (isinstance(literal, ast.Constant)
                        and isinstance(literal.value, str)
                        and "Council tab" in literal.value):
                    out.append(node.name)
                    break
            if out and out[-1] == node.name:
                break
    return out


def main() -> int:
    engine_path = ROOT / "council_gui_engine.py"
    engine = engine_path.read_text(encoding="utf-8")
    region = vault_region(engine)

    qt_tab = ROOT / "council_qt" / "tabs" / "vault.py"
    core = [ROOT / "council_core" / n for n in
            ("vault_ops.py", "vault_import.py", "vault_data.py",
             "vault_search.py", "vault_jobs.py")]

    tk_surface = vault_surface_from_the_estimates_probe()
    cross_check = toolkit_lines(region)
    qt_files = [qt_tab, ROOT / "council_qt" / "tabs" / "collection_dialog.py"]
    qt_written = sum(code_lines(f) for f in qt_files if f.exists())
    core_written = sum(code_lines(p) for p in core if p.exists())

    print("=" * 68)
    print("THE VAULT PILOT, MEASURED")
    print("=" * 68)
    print(f"Tk toolkit lines (the estimate's own probe)         : {tk_surface:>6}")
    print(f"   cross-check, this probe's simpler rule            : {cross_check:>6}")
    print(f"Qt view written (tab + its dialog)                  : {qt_written:>6}")
    for f in qt_files:
        if f.exists():
            print(f"    {f.name:<20} {code_lines(f):>6}")
    print(f"Logic extracted to council_core                      : {core_written:>6}")
    for p in core:
        if p.exists():
            print(f"    {p.name:<20} {code_lines(p):>6}")
    print()
    if tk_surface:
        print(f"Multiplier (Qt lines per Tk toolkit line)           : "
              f"{qt_written / tk_surface:>6.2f}")
    print()

    # -- coverage -----------------------------------------------------------
    qt_src = qt_tab.read_text(encoding="utf-8")
    commands = sorted(vault_commands(engine))

    # Where the Qt tab answers the same command under a different name. Written
    # out rather than matched loosely, because a matcher relaxed until it agrees
    # with the author measures nothing.
    ALIASES = {
        "_vmgr_browse_zip": "_pick_file",
        "_vmgr_browse_zip_folder": "_pick_dir",
        "_vmgr_browse_folder": "_pick_dir",
        "_vmgr_browse_mongo": "_pick_file",
        "_vmgr_instant_search": "on_search",
        "_vmgr_open_converted_mongo": "on_open_converted",
    }
    answered, missing = [], []
    for cmd in commands:
        # _vmgr_build_keyword_index -> build_keyword_index / on_build_keyword_index
        stem = ALIASES.get(cmd) or cmd.replace("_vmgr_", "").replace("_show_", "")
        if stem in qt_src or stem.replace("_", "") in qt_src.replace("_", ""):
            answered.append(cmd)
        else:
            missing.append(cmd)

    # A handler existing is not the same as the work being done. The Qt tab
    # says so in its log when it cannot finish something yet; count those
    # separately rather than letting them inflate the coverage number.
    honest_stubs = sorted(needs_council_tab(qt_src))

    print(f"Vault commands wired in Tk                          : {len(commands):>6}")
    print(f"  a Qt handler exists                               : {len(answered):>6}")
    print(f"  no Qt handler                                     : {len(missing):>6}")
    for cmd in missing:
        print(f"      {cmd}")
    if honest_stubs:
        print(f"  of those, handlers that report they cannot finish : "
              f"{len(honest_stubs):>6}")
        for name in honest_stubs:
            print(f"      {name}  (needs the Council tab's model plumbing)")
    print()

    # -- what the tests cost ------------------------------------------------
    tests = {
        "tests/test_council_core.py": "the extracted logic",
        "tests/test_council_qt_foundation.py": "the Qt shell",
    }
    total_tests = 0
    for rel, what in tests.items():
        path = ROOT / rel
        if not path.exists():
            continue
        n = len(re.findall(r"^def test_", path.read_text(encoding="utf-8"),
                           re.M))
        total_tests += n
        print(f"{rel:<44} {n:>4} tests  ({what})")
    print(f"{'':<44} {total_tests:>4} total")
    print()
    print("Counts source, not calendar. See the header.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
