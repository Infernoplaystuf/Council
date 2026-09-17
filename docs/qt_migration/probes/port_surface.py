"""Measure the ACTUAL toolkit surface of the whole app, method by method.

The point is a bottom-up estimate: not "the file is 22,610 lines" but "these
methods contain these many toolkit calls", clustered into the units a port
would actually be done in.
"""
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\apkun\Downloads\Council-Demo\Council-Demo\.claude\worktrees\priceless-vaughan-cc9023")

# Every spelling that ties a line to Tk. Counted per line, not per token.
TK_PAT = re.compile(
    r"\b(tk|ttk)\.|"
    r"\.(pack|grid|place|pack_forget|grid_forget|grid_remove|destroy|winfo_\w+|"
    r"bind|bind_all|unbind_all|tag_bind|tag_configure|tag_config|protocol|"
    r"after|after_cancel|after_idle|update_idletasks|mainloop|grab_set|"
    r"grab_release|focus_set|focus_force|iconbitmap|iconphoto|wm_\w+|"
    r"curselection|insert|delete|see|heading|column|configure|config|cget|"
    r"state|selection|get_children|yview|xview|clipboard_\w+)\(|"
    r"\b(StringVar|IntVar|BooleanVar|DoubleVar|PhotoImage|Toplevel|Canvas|"
    r"messagebox|filedialog|simpledialog|scrolledtext|colorchooser)\b|"
    r"textvariable=|variable=|command=|relief=|sticky=|padx=|pady=")

# Lines that are Tk but trivially mechanical (a pack with no options, etc.)
NEUTRAL_HINT = re.compile(r"^\s*(#|\"\"\"|'''|$)")

CLUSTERS = [
    ("Council tab (chat + deliberation)", r"^(_build_council|_send|_append_transcript|_flush_stream|_on_.*response|_render_|_chat|_transcript|_clarif|_stream)"),
    ("Vault manager", r"^(_vmgr|_build_vault_manager|_vault_|_defer_to_vault)"),
    ("Grapher", r"^(_build_grapher|_grapher|_plot|_chart)"),
    ("GUI Designer tab", r"^(_gd_|_build_gui_designer)"),
    ("Models / HF finder", r"^(_build_model_finder|_model_|_hf_)"),
    ("Dream3D / NX", r"^(_build_dream3d|_nx_|_d3d)"),
    ("Lens", r"^(_build_lens|_lens_)"),
    ("Sessions", r"^(_build_sessions|_session)"),
    ("Agent Jobs", r"^(_build_agent_jobs|_jobs_|_agent_job)"),
    ("Tool Creation / forge", r"^(_build_tool_forge|_forge_|_tool_)"),
    ("Speech / TTS", r"^(_build_speech|_speech|_tts|_voice)"),
    ("Changelog", r"^(_build_changelog|_changelog)"),
    ("Diagnostics", r"^(_build_diagnostics|_diag)"),
    ("Specialists", r"^(_build_specialists|_spec_)"),
    ("IDE / Runner (adv)", r"^(_build_ide|_ide_)"),
    ("Librarian (adv)", r"^(_build_librarian|_librarian)"),
    ("Agents (adv)", r"^(_build_agents|_agents_)"),
    ("Nodes (adv)", r"^(_build_nodes|_node)"),
    ("Vault Health (adv)", r"^(_build_vault_health|_health)"),
    ("Apothecary (adv)", r"^(_build_apoth|_apoth)"),
    ("Theme / scaling / chrome", r"^(_apply_dark_theme|_make_text|_apply_ui_scale|_adjust_ui_scale|_reset_ui_scale|_pump_splash|_refresh_title|_set_status|_build_ui)"),
    ("Shutdown / lifecycle", r"^(_on_app_close|_on_close|_shutdown|_quit|main)"),
    ("UI queue / marshalling", r"^(_poll_ui_queue|_ui_|_emit_|call_soon)"),
    ("Engine settings / dialogs", r"^(_open_engine_settings|_engine_|_settings)"),
]


def classify(name):
    for label, pat in CLUSTERS:
        if re.match(pat, name):
            return label
    return "other / uncategorised"


def measure(path):
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    tree = ast.parse(src)
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a, b = node.lineno, (node.end_lineno or node.lineno)
        body = lines[a - 1:b]
        total = len(body)
        tk = sum(1 for ln in body if TK_PAT.search(ln) and not NEUTRAL_HINT.match(ln))
        rows.append({"name": node.name, "lines": total, "tk_lines": tk,
                     "start": a, "cluster": classify(node.name)})
    return rows, len(lines)


out = {}
rows, total = measure(ROOT / "council_gui_engine.py")
by_cluster = defaultdict(lambda: {"methods": 0, "lines": 0, "tk_lines": 0})
for r in rows:
    c = by_cluster[r["cluster"]]
    c["methods"] += 1
    c["lines"] += r["lines"]
    c["tk_lines"] += r["tk_lines"]

print(f"council_gui_engine.py: {total} lines, {len(rows)} functions/methods")
print(f"{'cluster':<38} {'methods':>7} {'lines':>7} {'tk-lines':>9} {'tk%':>5}")
print("-" * 72)
grand_l = grand_t = 0
for label, d in sorted(by_cluster.items(), key=lambda kv: -kv[1]["tk_lines"]):
    pct = (100.0 * d["tk_lines"] / d["lines"]) if d["lines"] else 0
    print(f"{label:<38} {d['methods']:>7} {d['lines']:>7} {d['tk_lines']:>9} {pct:>4.0f}%")
    grand_l += d["lines"]
    grand_t += d["tk_lines"]
print("-" * 72)
print(f"{'TOTAL (inside functions)':<38} {len(rows):>7} {grand_l:>7} {grand_t:>9} "
      f"{100.0 * grand_t / grand_l:>4.0f}%")

print("\n\nOTHER MODULES THAT WOULD HAVE TO MOVE")
print(f"{'module':<26} {'lines':>7} {'tk-lines':>9} {'tk%':>5}  role")
print("-" * 78)
ROLES = {
    "gui_canvas.py": "Designer canvas (drag/drop editor)",
    "splash.py": "animated startup window",
    "onboarding.py": "first-run wizard (modal)",
    "activation_dialog.py": "licensing gate (modal)",
    "crash_reporter.py": "crash dialog + Tk exception hook",
    "branding.py": "window icon (ctypes)",
    "plots_pane.py": "matplotlib embed + thumbnails",
    "apothecary_engine.py": "SSH nodes panel (advanced)",
    "sage_agent.py": "Sage tuning panel (advanced)",
    "vault_agent.py": "Vault agent panel (advanced)",
    "db_connect_wizard.py": "DB connection wizard (modal)",
    "gui_wizard.py": "Designer on-ramp (modal)",
    "gui_runwith.py": "interpreter picker",
    "python_envs.py": "probe REQUIRES tkinter of the target",
    "agent_panel.py": "DEAD - never loaded",
    "system_panel.py": "DEAD - never loaded",
    "grapher_app.py": "DEAD - never loaded",
    "tab_grapher.py": "DEAD - never loaded (broken import)",
    "council_modules.py": "DEAD - never loaded",
    "phase1_ai_model_council.py": "DEAD - never loaded",
}
mod_tot = {"live": [0, 0], "dead": [0, 0]}
for name, role in ROLES.items():
    p = ROOT / name
    if not p.exists():
        continue
    rows2, tot = measure(p)
    tk = sum(r["tk_lines"] for r in rows2)
    src_lines = tot
    pct = 100.0 * tk / max(1, sum(r["lines"] for r in rows2))
    print(f"{name:<26} {src_lines:>7} {tk:>9} {pct:>4.0f}%  {role}")
    key = "dead" if role.startswith("DEAD") else "live"
    mod_tot[key][0] += src_lines
    mod_tot[key][1] += tk
print("-" * 78)
print(f"{'live modules':<26} {mod_tot['live'][0]:>7} {mod_tot['live'][1]:>9}")
print(f"{'dead modules':<26} {mod_tot['dead'][0]:>7} {mod_tot['dead'][1]:>9}")

print("\n\nTEST COVER OVER THE UI BEING REWRITTEN")
tests = list((ROOT / "tests").glob("*.py"))
constructs = 0
for t in tests:
    s = t.read_text(encoding="utf-8", errors="replace")
    if "CouncilConsole" in s:
        constructs += 1
print(f"test files referencing CouncilConsole: {constructs} of {len(tests)}")
