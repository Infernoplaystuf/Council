"""Per-tab attribution, this time following REFERENCES not just calls.

The first pass walked `self.x()` calls only, so every handler wired by
`command=self.x` or `.bind("<Return>", self.x)` looked like it belonged to no
tab -- 199 methods and 530 toolkit-bound lines of phantom work. A button's
callback is part of its tab; this pass counts any `self.x` mentioned anywhere
inside a reachable method, which is how the wiring actually works.
"""
import ast
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\apkun\Downloads\Council-Demo\Council-Demo\.claude\worktrees\priceless-vaughan-cc9023")
SRC = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8", errors="replace")
LINES = SRC.splitlines()
TREE = ast.parse(SRC)

TK_PAT = re.compile(
    r"\b(tk|ttk)\.|"
    r"\.(pack|grid|place|pack_forget|grid_forget|destroy|winfo_\w+|"
    r"bind|bind_all|tag_bind|tag_configure|tag_config|protocol|"
    r"after|after_cancel|update_idletasks|mainloop|grab_set|focus_set|"
    r"curselection|heading|column|yview|clipboard_\w+)\(|"
    r"\b(StringVar|IntVar|BooleanVar|DoubleVar|PhotoImage|Toplevel|Canvas|"
    r"messagebox|filedialog|simpledialog)\b|"
    r"textvariable=|variable=|command=|sticky=|padx=|pady=")

cls = next(n for n in TREE.body
           if isinstance(n, ast.ClassDef) and n.name == "CouncilConsole")
methods = {}
for item in cls.body:
    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a, b = item.lineno, item.end_lineno or item.lineno
        body = LINES[a - 1:b]
        # EVERY self.<name> mentioned, called or merely referenced.
        refs = {n.attr for n in ast.walk(item)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "self"}
        methods[item.name] = {"lines": len(body),
                              "tk": sum(1 for ln in body if TK_PAT.search(ln)),
                              "refs": refs}

TABS = {
    "Council": "_build_council_tab", "Dream3D": "_build_dream3d_tab",
    "Grapher": "_build_grapher_tab", "Specialists": "_build_specialists_tab",
    "Models": "_build_model_finder_tab", "Lens": "_build_lens_tab",
    "Sessions": "_build_sessions_tab", "Vault": "_build_vault_manager_tab",
    "Agent Jobs": "_build_agent_jobs_tab", "Tool Creation": "_build_tool_forge_tab",
    "GUI Designer": "_build_gui_designer_tab", "Speech": "_build_speech_tab",
    "Changelog": "_build_changelog_tab", "Diagnostics": "_build_diagnostics_tab",
    "IDE (adv)": "_build_ide_tab", "Librarian (adv)": "_build_librarian_tab",
    "Agents (adv)": "_build_agents_tab", "Nodes (adv)": "_build_nodes_tab",
    "Vault Health (adv)": "_build_vault_health_tab",
    "Apothecary (adv)": "_build_apoth_tab",
}
# Every tab builder is a BOUNDARY: walking out of one tab into another would
# make every tab reach every other through __init__/_build_ui and collapse the
# whole attribution, which is exactly what the first attempt at this did.
BOUNDARY = set(TABS.values())

reach = {}
for tab, entry in TABS.items():
    seen, stack = set(), [entry]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in methods:
            continue
        if cur in BOUNDARY and cur != entry:
            continue
        seen.add(cur)
        stack.extend(methods[cur]["refs"])
    reach[tab] = seen

# The lifecycle core is what remains once tabs have claimed theirs: the startup
# chain, shutdown, and the dispatcher.
life = set()

owner = defaultdict(set)
for tab, names in reach.items():
    for n in names:
        owner[n].add(tab)

rows = []
for tab, names in reach.items():
    own = [n for n in names if len(owner[n]) == 1 and n not in life]
    rows.append((tab, len(own),
                 sum(methods[n]["lines"] for n in own),
                 sum(methods[n]["tk"] for n in own)))

print("PER TAB, following command=/bind wiring (own = only this tab reaches it,")
print("and not part of the lifecycle core)")
print(f"{'tab':<22}{'methods':>8}{'lines':>8}{'tk-lines':>10}")
print("-" * 48)
for r in sorted(rows, key=lambda r: -r[3]):
    print(f"{r[0]:<22}{r[1]:>8}{r[2]:>8}{r[3]:>10}")
print("-" * 48)
print(f"{'SUM (tab work)':<22}{sum(r[1] for r in rows):>8}{sum(r[2] for r in rows):>8}"
      f"{sum(r[3] for r in rows):>10}")

shared = {n for n, t in owner.items() if len(t) > 1 and n not in life}
print(f"\nSHARED by 2+ tabs      : {len(shared):>4} methods "
      f"{sum(methods[n]['lines'] for n in shared):>7} lines "
      f"{sum(methods[n]['tk'] for n in shared):>5} tk")
print(f"LIFECYCLE core         : {len(life):>4} methods "
      f"{sum(methods[n]['lines'] for n in life):>7} lines "
      f"{sum(methods[n]['tk'] for n in life):>5} tk")
orphan = set(methods) - set(owner) - life
print(f"STILL unattributed     : {len(orphan):>4} methods "
      f"{sum(methods[n]['lines'] for n in orphan):>7} lines "
      f"{sum(methods[n]['tk'] for n in orphan):>5} tk")
if orphan:
    top = sorted(orphan, key=lambda n: -methods[n]["tk"])[:10]
    print("  heaviest:", ", ".join(f"{n}({methods[n]['tk']})" for n in top))
