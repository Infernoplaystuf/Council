"""Generate the Tk project from an example .gspec, outside the app.

Proves the pipeline runs headlessly and measures what a Qt target must match.
Writes only into the scratchpad.
"""
import json
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\apkun\Downloads\Council-Demo\Council-Demo\.claude\worktrees\priceless-vaughan-cc9023")
OUT = Path(r"C:\Users\apkun\AppData\Local\Temp\claude\C--Users-apkun-Downloads-Council-Demo-Council-Demo--claude-worktrees-priceless-vaughan-cc9023\486b5624-72b4-418a-b22e-cbd5562732ac\scratchpad\gen_tk")
sys.path.insert(0, str(ROOT))

import gui_emit as ge  # noqa: E402
import gui_layout as gl  # noqa: E402
import gui_policy as gp  # noqa: E402
import gui_spec as gsp  # noqa: E402
from gui_shapes import load_gspec  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "barbie_capture_v2"
proj = load_gspec(ROOT / "examples" / "gui" / f"{name}.gspec")
shapes = proj.shapes
win = proj.window

tree = gl.infer(shapes, proj.canvas.w, proj.canvas.h)
spec = gsp.build(
    shapes, tree, {},
    project=name,
    mode=proj.mode,
    title=win.title,
    min_w=win.min_w, min_h=win.min_h,
    root_bg=win.bg, root_fg=win.fg,
    root_font=win.font,
    requires=getattr(proj, "requires", []) or [])

ok, errs = gsp.validate(spec)
print("spec valid:", ok, errs[:5])

pdir = OUT / name
pdir.mkdir(parents=True, exist_ok=True)
res = ge.emit(spec, pdir)
print("written:", [Path(p).name for p in res.files_written])
print("warnings:", res.warnings)

# Line counts of what a Qt target has to reproduce
for p in sorted(pdir.rglob("*.py")):
    print(f"  {p.relative_to(pdir).as_posix():24} {len(p.read_text(encoding='utf-8').splitlines()):5} lines")

ok2, errs2 = gp.validate_dir(pdir, spec.mode, spec.requires)
print("policy gate on the generated Tk project:", ok2, errs2[:5])

# The two facts the Qt target must preserve
main_ui = (pdir / "ui" / "main_ui.py").read_text(encoding="utf-8")
print("stop marker present:", "_watch_for_stop(self)" in main_ui)
print("ports declared:", [p.name for p in spec.ports])

# Idempotence: emit twice, compare bytes
before = {p: p.read_bytes() for p in sorted(pdir.rglob("*.py"))}
ge.emit(spec, pdir)
after = {p: p.read_bytes() for p in sorted(pdir.rglob("*.py"))}
print("byte-identical on re-emit:", before == after)
