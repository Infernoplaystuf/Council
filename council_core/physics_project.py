"""
council_core.physics_project — physics models in the Build tab (user,
2026-11-05: "making a tab and giving it a goal on a physics based model to
make ... a PVC pipe organ ... radio antenna creation. Eddy current
calculations. Water erosion on various materials"; then "wouldn't it being
a feature inside of the Build Tab be worthwhile?"; and "The physics models
should have something that allows for them to connect to a folder,
knowledge, or ledger containing data to help inform the model").

A physics model is a folder (physics_kit's docstring says what is in it).
This module makes one, connects it to data, runs it and calibrates it; the
Build tab's Model view (council_qt.widgets.physics_panel) is its face, and
the Build tab's Agent mode with the "Physics modeller" profile builds a new
one from a goal.

    EXAMPLES                       the four worked examples (examples/physics_models)
    create(dest, example, goal)    a new model folder: an example, or the blank
                                   template for a goal; physics_kit copied in;
                                   a git repository with one commit (Agent mode
                                   needs one)
    is_model(folder)               it has a model.py and a physics.json
    Link / links / save_links      what data the model is connected to:
                                   a folder, a saved Ledger, a Connections graph
    gather(folder, vault)          that data read by COUNCIL (never by the
                                   model's code): tables and texts, ready for
                                   the model as `data`
    describe(folder, python)       the model's inputs, fits, assumptions (in the sandbox)
    run(folder, python, inputs, data)  its Result as plain data (in the sandbox)
    calibrate(fit, x, y, ...)      a Fit spec fitted to measured points
                                   (council_core.physics_fit): the inputs it sets
    goal_request(goal, links)      what Agent mode is asked to build for a goal

THE MODEL'S CODE NEVER RUNS IN COUNCIL
It runs with the Build tab's "Run with" Python in council_core.build_sandbox
(offline, time and memory limits), and gets the connected data as a JSON
file Council wrote. Council itself only reads JSON back.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import build_sandbox as sb

ROOT = Path(__file__).resolve().parent.parent
KIT_DIR = ROOT / "physics_kit"
EXAMPLES_DIR = ROOT / "examples" / "physics_models"
MANIFEST = "physics.json"
MODEL_FILE = "model.py"
PROFILE_ID = "physics-modeller"

#: key -> (title, one line)
EXAMPLES: Dict[str, tuple] = {
    "pvc_organ": ("PVC pipe organ", "Cut lengths for every note of a scale, open or stopped "
                  "pipes, or struck chimes; calibrate from a measured pipe."),
    "antenna": ("Radio antenna", "Dipole, ground plane or Yagi for a frequency: what to cut, "
                "impedance, SWR, gain and pattern; read an analyzer sweep."),
    "eddy_current": ("Eddy currents", "Depth of reach for eddy-current testing, induction "
                     "heating, plate losses, magnetic braking; calibrate the conductivity."),
    "water_erosion": ("Water erosion", "Sand-laden water, flowing water on a bed, sliding "
                      "wear; compare materials and calibrate K and n from tests."),
}

#: what a folder link reads
TABLE_SUFFIXES = (".csv", ".tsv", ".xlsx", ".xls", ".xlsm")
TEXT_SUFFIXES = (".txt", ".md")
MAX_FILES = 40
MAX_ROWS = 5000
MAX_TEXT = 6000
RUN_TIMEOUT_S = 120
DESCRIBE_TIMEOUT_S = 60

FOLDER, LEDGER, KNOWLEDGE = "folder", "ledger", "knowledge"


class PhysicsError(RuntimeError):
    """Said to the user as it is."""


# ── making a model ──────────────────────────────────────────────────────
def is_model(folder) -> bool:
    f = Path(folder)
    return (f / MODEL_FILE).is_file() and (f / MANIFEST).is_file()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:40] or "physics_model"


def default_parent(vault_dir) -> Path:
    return Path(vault_dir) / "physics_models"


def copy_kit(folder) -> None:
    """physics_kit into the model folder (replacing an older copy), so the
    model runs on any Python with numpy and the agent can read the blocks."""
    dest = Path(folder) / "physics_kit"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(KIT_DIR, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


BLANK_MODEL = '''"""
{title}

Goal: {goal}

Built with physics_kit (read physics_kit/__init__.py and the module for this
kind of physics first). compute() takes the inputs and the connected data and
returns a Result: values, tables (the design sheet first), charts and notes.
"""
from __future__ import annotations

from physics_kit import Chart, Fit, Input, Model, Result


def compute(p, data) -> Result:
    res = Result()
    res.notes.append("This model has not been built yet: ask the Physics modeller in "
                     "Agent mode to build it from the goal.")
    return res


MODEL = Model(
    name={title!r},
    goal={goal!r},
    inputs=[],
    compute=compute,
    assumptions=[],
    limits=[],
    fits=[],
)
'''

BLANK_TEST = '''"""Reference cases this model must pass: textbook values, limiting cases, units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model import MODEL  # noqa: E402


def test_the_model_runs_with_its_defaults():
    MODEL.run({})
'''


def create(dest, example: str = "", goal: str = "", title: str = "",
           links: Sequence["Link"] = (), git: bool = True) -> Path:
    """A new model folder at ``dest`` (refused when it exists and isn't empty)."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise PhysicsError(f"{dest} already has files in it: pick a new folder.")
    if example:
        if example not in EXAMPLES:
            raise PhysicsError(f"No example '{example}'.")
        shutil.copytree(EXAMPLES_DIR / example, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    else:
        if not (goal or "").strip():
            raise PhysicsError("Say what the model is for: the goal is what the agent builds.")
        (dest / "tests").mkdir(parents=True, exist_ok=True)
        name = title or goal.strip().split(".")[0][:60]
        (dest / MODEL_FILE).write_text(BLANK_MODEL.format(title=name, goal=goal.strip()),
                                       encoding="utf-8")
        (dest / "tests" / "test_model.py").write_text(BLANK_TEST, encoding="utf-8")
        (dest / "README.md").write_text(f"# {name}\n\nGoal: {goal.strip()}\n", encoding="utf-8")
    copy_kit(dest)
    manifest = load_manifest(dest) if (dest / MANIFEST).is_file() else {}
    manifest.setdefault("data", [])
    manifest["goal"] = goal.strip() or manifest.get("goal", "")
    manifest["example"] = example
    manifest["kit_version"] = _kit_version()
    for link in links:
        manifest["data"].append(asdict(link))
    _write_json(dest / MANIFEST, manifest)
    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.council_run/\n",
                                     encoding="utf-8")
    if git:
        _git_init(dest)
    return dest


def _kit_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (KIT_DIR / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def _git_init(folder: Path) -> bool:
    """git init and one commit, so Agent mode can work on a branch of it.
    False (and nothing said) when git isn't installed."""
    env = dict(os.environ, GIT_AUTHOR_NAME="Council", GIT_AUTHOR_EMAIL="council@localhost",
               GIT_COMMITTER_NAME="Council", GIT_COMMITTER_EMAIL="council@localhost")
    flags = 0x08000000 if sys.platform == "win32" else 0       # CREATE_NO_WINDOW
    try:
        for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                     ["git", "commit", "-q", "-m", "New physics model"]):
            subprocess.run(argv, cwd=str(folder), env=env, check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=flags)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ── data links ──────────────────────────────────────────────────────────
@dataclass
class Link:
    kind: str                     # folder | ledger | knowledge
    target: str                   # a folder, a Ledger's CSV, or a graph id ('' = main)
    name: str = ""
    question: str = ""            # knowledge: what to ask the graph about

    def label(self) -> str:
        what = {FOLDER: "Folder", LEDGER: "Ledger", KNOWLEDGE: "Connections"}.get(self.kind, self.kind)
        main = self.name or (Path(self.target).name if self.kind != KNOWLEDGE else
                             (self.target or "main graph"))
        extra = f': "{self.question}"' if self.kind == KNOWLEDGE and self.question else ""
        return f"{what}: {main}{extra}"


def load_manifest(folder) -> Dict[str, Any]:
    p = Path(folder) / MANIFEST
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def links(folder) -> List[Link]:
    out = []
    for d in load_manifest(folder).get("data") or []:
        try:
            out.append(Link(**{k: d.get(k, "") for k in ("kind", "target", "name", "question")}))
        except TypeError:
            continue
    return out


def save_links(folder, items: Sequence[Link]) -> None:
    m = load_manifest(folder)
    m["data"] = [asdict(x) for x in items]
    _write_json(Path(folder) / MANIFEST, m)


def resolve_target(folder, link: Link) -> Path:
    """A folder or Ledger path: relative ones are inside the model folder."""
    p = Path(link.target)
    return p if p.is_absolute() else Path(folder) / p


_UNIT = re.compile(r"^(.*?)\s*[\(\[]\s*([^\)\]]{1,16})\s*[\)\]]\s*$")


def unit_of(column: str) -> str:
    """'Length (mm)' -> 'mm'."""
    m = _UNIT.match(column or "")
    return m.group(2).strip() if m else ""


def _frame_table(df, source: str, units: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    import pandas as pd
    df = df.head(MAX_ROWS)
    df = df.where(pd.notna(df), None)
    cols = [str(c) for c in df.columns]
    rows = []
    for rec in df.itertuples(index=False):
        row = {}
        for c, v in zip(cols, rec):
            if hasattr(v, "item"):
                try:
                    v = v.item()
                except (ValueError, AttributeError):
                    v = str(v)
            if isinstance(v, float) and v != v:
                v = None
            if v is not None and not isinstance(v, (int, float, str, bool)):
                v = str(v)
            row[c] = v
        rows.append(row)
    u = {c: unit_of(c) for c in cols if unit_of(c)}
    u.update({k: v for k, v in (units or {}).items() if v})
    return {"columns": cols, "rows": rows, "units": u, "source": source}


def _read_table(p: Path) -> List[tuple]:
    """(name, table) pairs from one file; a workbook gives one per sheet."""
    import pandas as pd
    if p.suffix.lower() in (".csv", ".tsv"):
        df = pd.read_csv(p, sep="\t" if p.suffix.lower() == ".tsv" else ",",
                         encoding_errors="replace")
        return [(p.stem, _frame_table(df, str(p)))]
    sheets = pd.read_excel(p, sheet_name=None)
    return [((p.stem if len(sheets) == 1 else f"{p.stem} - {s}"), _frame_table(df, str(p)))
            for s, df in sheets.items()]


@dataclass
class Gathered:
    tables: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    texts: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"tables": self.tables, "texts": self.texts}


def gather(folder, vault_dir=None, items: Optional[Sequence[Link]] = None) -> Gathered:
    """Read every linked source. A source that can't be read is a note, not
    an error, so one bad file doesn't stop the model."""
    out = Gathered()
    for link in (links(folder) if items is None else items):
        try:
            if link.kind == FOLDER:
                _gather_folder(resolve_target(folder, link), out, link)
            elif link.kind == LEDGER:
                _gather_ledger(resolve_target(folder, link), out, link)
            elif link.kind == KNOWLEDGE:
                _gather_knowledge(vault_dir, out, link, load_manifest(folder).get("goal", ""))
            else:
                out.notes.append(f"Unknown kind of data '{link.kind}'.")
        except Exception as exc:                          # noqa: BLE001 - say it, carry on
            out.notes.append(f"{link.label()}: couldn't read it ({type(exc).__name__}: {exc}).")
    return out


def _unique(name: str, taken) -> str:
    base, i = name, 2
    while name in taken:
        name, i = f"{base} ({i})", i + 1
    return name


def _gather_folder(path: Path, out: Gathered, link: Link) -> None:
    if not path.is_dir():
        out.notes.append(f"{link.label()}: the folder {path} isn't there.")
        return
    files = sorted(p for p in path.rglob("*") if p.is_file()
                   and p.suffix.lower() in TABLE_SUFFIXES + TEXT_SUFFIXES
                   and not p.name.startswith(("~$", ".")))
    if len(files) > MAX_FILES:
        out.notes.append(f"{link.label()}: {len(files)} files; the first {MAX_FILES} were read.")
        files = files[:MAX_FILES]
    for p in files:
        if p.suffix.lower() in TEXT_SUFFIXES:
            text = p.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT]
            out.texts[_unique(p.name, out.texts)] = text
            continue
        try:
            for name, table in _read_table(p):
                out.tables[_unique(name, out.tables)] = table
        except Exception as exc:                          # noqa: BLE001
            out.notes.append(f"{p.name}: couldn't read it ({exc}).")


def _gather_ledger(csv: Path, out: Gathered, link: Link) -> None:
    from council_core import ledger_analytics as la
    if not csv.is_file():
        out.notes.append(f"{link.label()}: {csv} isn't there.")
        return
    data = la.load(csv)
    df = data.frame.copy()
    for f, info in data.fields.items():           # numbers as numbers ('12.5 mm' -> 12.5)
        if info.kind == la.NUMBER:
            df[f] = [la.to_number(v) for v in df[f]]
    units = {f: info.unit for f, info in data.fields.items() if info.unit}
    name = link.name or f"Ledger {csv.stem}"
    out.tables[_unique(name, out.tables)] = _frame_table(df, str(csv), units)


def _gather_knowledge(vault_dir, out: Gathered, link: Link, goal: str) -> None:
    from council_core import kg_query
    if vault_dir is None:
        out.notes.append(f"{link.label()}: no vault to read the graph from.")
        return
    question = link.question or goal
    if not question:
        out.notes.append(f"{link.label()}: say what to ask the graph about.")
        return
    graphs = [link.target] if link.target else None
    ctxs = kg_query.gather(vault_dir, question, graphs=graphs)
    if not ctxs:
        out.notes.append(f"{link.label()}: the graph knows nothing that the question names.")
        return
    text = "\n\n".join(c["text"] for c in ctxs)[:MAX_TEXT]
    out.texts[_unique(link.name or f"Connections: {question[:40]}", out.texts)] = text


# ── running the model (in the sandbox) ─────────────────────────────────
@dataclass
class RunResult:
    ok: bool
    payload: Dict[str, Any]
    output: str
    error: str = ""


def _child(folder, python: str, args: List[str], timeout: float,
           stop: Optional[threading.Event] = None) -> RunResult:
    folder = Path(folder)
    if not (folder / "physics_kit").is_dir():
        copy_kit(folder)
    work = Path(tempfile.mkdtemp(prefix="council_physics_"))
    try:
        out_file = work / "out.json"
        lines: List[str] = []
        argv = [python, "-m", "physics_kit.run", str(folder / MODEL_FILE), *args,
                "--out", str(out_file)]
        env = sb.sandbox_env({"PYTHONPATH": os.pathsep.join(
            [str(sb.HOOK_DIR), str(folder)])})
        oc = sb.run(argv, folder, lines.append, stop, timeout=timeout, env=env)
        text = "\n".join(lines)
        payload: Dict[str, Any] = {}
        if out_file.is_file():
            try:
                payload = json.loads(out_file.read_text(encoding="utf-8"))
            except ValueError:
                payload = {}
        if payload.get("error"):
            return RunResult(False, payload, payload.get("traceback") or text, payload["error"])
        if not oc.ok or not payload:
            return RunResult(False, payload, text, oc.summary("The model") if not oc.error
                             else oc.error)
        return RunResult(True, payload, text)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def describe(folder, python: str, stop=None) -> RunResult:
    return _child(folder, python, ["--describe"], DESCRIBE_TIMEOUT_S, stop)


def run(folder, python: str, inputs: Dict[str, Any], data: Optional[Gathered] = None,
        stop=None) -> RunResult:
    """The model's Result (values, tables, charts, notes, fit_constants)."""
    folder = Path(folder)
    work = folder / ".council_run"
    work.mkdir(exist_ok=True)
    (work / "inputs.json").write_text(json.dumps(inputs, default=str), encoding="utf-8")
    (work / "data.json").write_text(json.dumps((data or Gathered()).to_json(), default=str),
                                    encoding="utf-8")
    res = _child(folder, python, ["--inputs", str(work / "inputs.json"),
                                  "--data", str(work / "data.json")], RUN_TIMEOUT_S, stop)
    if res.ok and data is not None and data.notes:
        res.payload.setdefault("notes", []).extend(data.notes)
    return res


# ── calibration ─────────────────────────────────────────────────────────
X_UNITS = {"m": 1.0, "mm": 1e-3, "cm": 1e-2, "in": 0.0254, "inch": 0.0254, "ft": 0.3048,
           "Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "m/s": 1.0}


def convert_x(values: Sequence[float], have: str, want: str) -> List[float]:
    """``values`` in unit ``have`` as unit ``want`` (lengths and frequencies)."""
    a, b = X_UNITS.get(have), X_UNITS.get(want)
    if not have or a is None or b is None or have == want:
        return list(values)
    return [v * a / b for v in values]


@dataclass
class Calibration:
    fit: Any                      # physics_fit.Fit
    sets: Dict[str, float]        # model input -> new value
    text: str


def calibrate(spec: Dict[str, Any], xs: Sequence[float], ys: Sequence[float],
              constants: Dict[str, float], x_unit: str = "",
              guesses: Optional[Dict[str, float]] = None) -> Calibration:
    """Fit ``spec`` (a physics_kit Fit as JSON) to the points; the run's
    fit_constants are its fixed values. ``x_unit`` is the data's unit."""
    from council_core import physics_fit as pf
    want = spec.get("x_unit") or ""
    xs = convert_x(xs, x_unit, want) if want else list(xs)
    fixed = {k: float(v) for k, v in (constants or {}).items() if v is not None}
    eq = pf.parse(spec["equation"], (spec["input"],), tuple(guesses or ()), fixed=fixed)
    missing = [p for p in eq.params if p not in (spec.get("sets") or {})]
    if missing:
        raise PhysicsError(f"The equation has names the model didn't supply ({', '.join(missing)}): "
                           "run the model first, then calibrate.")
    f = pf.fit(eq, xs, ys, guesses, x_name=spec.get("x_hint") or spec["input"],
               y_name=spec.get("y_hint") or "y", x_unit=want)
    sets = {spec["sets"][p.name]: p.value for p in f.params}
    lines = [f"{p.name} = {p.value:.5g} ± {p.stderr:.2g} (95%: {p.low:.4g} to {p.high:.4g})"
             for p in f.params]
    text = (f"Fitted {spec['equation']} to {f.n} points: R² = {f.r2:.4f}. "
            + "; ".join(lines) + (". " + " ".join(f.notes) if f.notes else ""))
    return Calibration(f, sets, text)


def fits_for(described: Dict[str, Any], inputs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The model's fits that apply with these inputs (a Fit's ``when``)."""
    out = []
    for f in described.get("fits") or []:
        when = f.get("when") or {}
        if all(inputs.get(k) == v for k, v in when.items()):
            out.append(f)
    return out


def guess_column(columns: Sequence[str], hint: str) -> str:
    h = (hint or "").lower()
    for c in columns:
        if h and h in c.lower():
            return c
    return columns[0] if columns else ""


# ── building from a goal (Agent mode) ──────────────────────────────────
AGENT_INSTRUCTIONS = (
    "You build physics models with physics_kit (it is in the project: read "
    "physics_kit/__init__.py and the module for this physics before writing "
    "anything; the worked examples are in your references). model.py defines "
    "MODEL = Model(name, goal, inputs, compute, assumptions, limits, fits); "
    "compute(p, data) returns a Result with values (number, unit), tables "
    "(the design sheet first: what to cut, build or set), charts and notes. "
    "SI units inside; every input has its unit. Use physics_kit's tested "
    "functions and its materials table; never type a physical constant or a "
    "material property from memory - if physics_kit lacks it, add it to "
    "physics_kit with a test against a cited textbook value. When `data` has "
    "tables or texts (the user's measurements, a Ledger, what Connections "
    "knows), use them: compare the model with measurements in a table and "
    "add a Fit so the user can calibrate unknown constants. Tests in "
    "tests/test_model.py: a textbook reference case with its source in a "
    "comment, a limiting case, and a units check; run_tests must pass. Say "
    "plainly in limits where the model stops being trustworthy.")


def goal_request(goal: str, items: Sequence[Link] = (), described: str = "") -> str:
    """The request Agent mode plans from."""
    lines = [f"Build the physics model in this folder for this goal: {goal.strip()}",
             "",
             "Plan it so the first step writes down, in README.md: what will be computed, the "
             "inputs with units, the outputs (the design sheet), the physics and the "
             "physics_kit blocks used, the assumptions, and the reference cases the tests "
             "will check. Then build model.py and tests/test_model.py step by step."]
    if items:
        lines += ["", "Data the user connected (it reaches compute() as `data`):"]
        lines += [f"- {x.label()}" for x in items]
    if described:
        lines += ["", described]
    return "\n".join(lines)
