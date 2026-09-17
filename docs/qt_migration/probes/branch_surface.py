"""What do the other branches add to the port surface?

Reads each branch's files straight out of git (no checkout), finds the UI modules
Work-Build does not have, and measures them the same way the local surface was
measured, so the numbers are comparable.
"""
import ast
import re
import subprocess
import sys
from collections import defaultdict

ROOT = r"C:\Users\apkun\Downloads\Council-Demo\Council-Demo\.claude\worktrees\priceless-vaughan-cc9023"
BASE = "origin/Work-Build"
BRANCHES = ["origin/main", "origin/odysseus-council", "origin/game-dev",
            "origin/commercial", "origin/Demo", "origin/Demo-Laptop",
            "origin/Work-Build-App", "origin/Database-grabber"]

TK_PAT = re.compile(
    r"\b(tk|ttk)\.|"
    r"\.(pack|grid|place|pack_forget|grid_forget|destroy|winfo_\w+|"
    r"bind|bind_all|tag_bind|tag_configure|tag_config|protocol|"
    r"after|after_cancel|update_idletasks|mainloop|grab_set|focus_set|"
    r"curselection|heading|column|yview|clipboard_\w+)\(|"
    r"\b(StringVar|IntVar|BooleanVar|DoubleVar|PhotoImage|Toplevel|Canvas|"
    r"messagebox|filedialog|simpledialog)\b|"
    r"textvariable=|variable=|command=|sticky=|padx=|pady=")
IMPORTS_TK = re.compile(r"^\s*(import tkinter|from tkinter)", re.M)


def run(args):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def files(branch):
    return set(run(["git", "ls-tree", "-r", "--name-only", branch]).splitlines())


def show(branch, path):
    return run(["git", "show", f"{branch}:{path}"])


base_files = {f for f in files(BASE)
              if f.endswith(".py") and not f.startswith("Version_History/")}
print(f"{BASE}: {len(base_files)} python files\n")

seen = {}
for branch in BRANCHES:
    branch_files = {f for f in files(branch)
                    if f.endswith(".py") and not f.startswith("Version_History/")}
    extra = sorted(branch_files - base_files)
    ui_extra = []
    for path in extra:
        src = show(branch, path)
        if not src or not IMPORTS_TK.search(src):
            continue
        lines = src.splitlines()
        tk = sum(1 for ln in lines if TK_PAT.search(ln))
        ui_extra.append((path, len(lines), tk))
        prev = seen.get(path)
        if prev is None or tk > prev[1]:
            seen[path] = (len(lines), tk, branch)
    total_lines = sum(x[1] for x in ui_extra)
    total_tk = sum(x[2] for x in ui_extra)
    print(f"{branch}")
    print(f"   {len(extra)} python files not on Work-Build; "
          f"{len(ui_extra)} of them are UI ({total_lines} lines, {total_tk} tk-lines)")
    for path, n, tk in sorted(ui_extra, key=lambda x: -x[2])[:12]:
        print(f"      {path:<38}{n:>6} lines {tk:>5} tk")
    print()

print("=" * 72)
print("UNION — every UI module that exists on some branch but not on Work-Build")
print(f"{'module':<40}{'lines':>7}{'tk':>6}  first seen on")
print("-" * 72)
tot_l = tot_tk = 0
for path, (n, tk, branch) in sorted(seen.items(), key=lambda kv: -kv[1][1]):
    print(f"{path:<40}{n:>7}{tk:>6}  {branch}")
    tot_l += n
    tot_tk += tk
print("-" * 72)
print(f"{'TOTAL ADDED BY OTHER BRANCHES':<40}{tot_l:>7}{tot_tk:>6}")
print()
print(f"Work-Build surface measured earlier : 4,459 tk-lines")
print(f"Widened target                      : {4459 + tot_tk:,} tk-lines")
