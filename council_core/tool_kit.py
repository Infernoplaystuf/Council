"""
council_core.tool_kit — the council's working tools beyond the original
seven, and which role gets which.

WHY THESE
A council of local models answers from memory unless something lets it look.
Each tool here replaces a guess with a lookup or a computation, and most of
them are cheaper than the model call they save. None of them calls a model,
and none of them talks to a network (node_status probes only the machines
the user put in node_routing.json, and only when asked).

    Speed — skip work already done
      cached_result     a computed table whose source files have not changed
      recall_decision   how the council answered a similar question before
      data_digest       one-call map of every data file
      table_peek        columns and the first rows of one data file
    Accuracy — stop made-up facts and numbers
      calc              exact arithmetic, units, dates (no eval)
      quote_check       is this quoted passage really in the vault?
      field_lookup      files where a labelled field has a value
      column_stats      exact column statistics, streamed
      data_query        pandas code in the analyst's sandboxed child process
    Reading — less pasted into small context windows
      read_section      one heading or line range of a file
      condense_file     a large file cut to a token budget, no model
      semantic_search   the vault's search (semantic only if an index exists)
    Code
      code_outline      functions and classes with their signatures
      code_grep         regex search with file:line hits
      lint_check        compile check plus pyflakes, nothing executed
      run_tests         pytest in a child process with a time limit
      diff_preview      a proposed change as a diff; never writes
    Coordination
      shared_notes      a scratchpad for this question, shared by members
      make_chart        a chart PNG from a data file, into data_out
      node_status       which machines are up, cooling down, and how busy

WHAT THEY MAY TOUCH
Reads stay inside the vault (and any extra roots the caller names, such as a
code folder); dot-folders and the app's protected files are refused. The only
writes are a NEW chart file in data_out and run_tests' own temporary files;
diff_preview shows a change and never applies it. Code that runs (data_query,
run_tests) runs in a child process with a time limit and memory cap.

Every tool is a ToolFn: args dict in, (ok, message for the model, payload)
out, and carries a one-line `help` the agent's prompt shows.
"""
from __future__ import annotations

import ast
import difflib
import json
import math
import operator
import re
import statistics
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .deliberation import ToolFn

MAX_TEXT_FILE = 2_000_000
MAX_OUT = 6_000
TEXT_SUFFIXES = {".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".yaml",
                 ".yml", ".ini", ".cfg", ".toml", ".log", ".py", ".js", ".ts",
                 ".html", ".xml", ".sql", ".c", ".h", ".cpp", ".cs", ".java",
                 ".go", ".rs"}
CODE_SUFFIXES = {".py", ".js", ".ts", ".java", ".cs", ".cpp", ".cc", ".c",
                 ".h", ".hpp", ".go", ".rs", ".m"}
TABLE_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json",
                  ".parquet"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "venv", ".venv"}

Result = Tuple[bool, str, Dict[str, Any]]


def tool(help_text: str) -> Callable[[ToolFn], ToolFn]:
    def wrap(fn: ToolFn) -> ToolFn:
        fn.help = help_text                               # type: ignore[attr-defined]
        return fn
    return wrap


def _clip(text: str, n: int = MAX_OUT) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n] + f"\n… ({len(text) - n} more characters)"


# ============================================================
# Paths: inside the roots, never the app's own state
# ============================================================

class PathError(ValueError):
    pass


def resolve_in(raw: str, roots: Sequence[Path], vault_dir: Path,
               *, must_exist: bool = True) -> Path:
    """`raw` (relative to the first root that has it, or absolute) as a path
    inside one of `roots`. Refuses dot-folders and protected app files."""
    raw = str(raw or "").strip().strip('"').strip("'")
    if not raw:
        raise PathError("no file or path given")
    cands: List[Path] = []
    p = Path(raw).expanduser()
    if p.is_absolute():
        cands.append(p)
    else:
        cands += [Path(r) / p for r in roots]
    for c in cands:
        try:
            c = c.resolve()
        except OSError:
            continue
        for r in roots:
            try:
                rel = c.relative_to(Path(r).resolve())
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                raise PathError(f"{raw}: app folders are not readable")
            if _protected(c, vault_dir):
                raise PathError(f"{raw}: that file belongs to the app")
            if must_exist and not c.exists():
                break
            return c
    if not must_exist:
        raise PathError(f"{raw} is outside the folders the council may read")
    # A bare file name: look for it anywhere in the roots.
    if not p.is_absolute() and len(p.parts) == 1:
        for r in roots:
            for hit in sorted(Path(r).rglob(p.name)):
                try:
                    rel = hit.relative_to(r)
                except ValueError:
                    continue
                if hit.is_file() and not any(
                        x.startswith(".") or x in SKIP_DIRS for x in rel.parts) \
                        and not _protected(hit, vault_dir):
                    return hit.resolve()
    raise PathError(f"{raw} was not found")


def _protected(path: Path, vault_dir: Path) -> bool:
    try:
        from conversation_logger import is_protected_path
    except Exception:                                     # noqa: BLE001
        return False
    return bool(is_protected_path(path, vault_dir))


def _readable(path: Path, vault_dir: Path) -> bool:
    """Not in a dot-folder of the vault, not an app file."""
    try:
        rel = Path(path).resolve().relative_to(Path(vault_dir).resolve())
    except (ValueError, OSError):
        return False
    return not any(x.startswith(".") for x in rel.parts) \
        and not _protected(Path(path), vault_dir)


def walk_files(root: Path, vault_dir: Path, suffixes: Optional[set] = None,
               limit: int = 4000):
    root = Path(root)
    n = 0
    for p in sorted(root.rglob("*")):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(x.startswith(".") or x in SKIP_DIRS for x in rel.parts[:-1]):
            continue
        if not p.is_file() or (suffixes and p.suffix.lower() not in suffixes):
            continue
        if _protected(p, vault_dir):
            continue
        yield p
        n += 1
        if n >= limit:
            return


def read_text(path: Path, limit: int = MAX_TEXT_FILE) -> str:
    if path.stat().st_size > limit:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    return path.read_text(encoding="utf-8", errors="replace")


def _show(path: Path, roots: Sequence[Path]) -> str:
    for r in roots:
        try:
            return path.relative_to(Path(r).resolve()).as_posix()
        except ValueError:
            continue
    return path.name


# ============================================================
# calc — arithmetic, units and dates without eval
# ============================================================

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _agg(fn):
    def call(*xs):
        if len(xs) == 1 and isinstance(xs[0], (list, tuple)):
            xs = tuple(xs[0])
        return fn(list(xs))
    return call


CALC_FUNCS: Dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10,
    "log2": math.log2, "exp": math.exp, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "degrees": math.degrees, "radians": math.radians,
    "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "factorial": lambda n: math.factorial(int(n)) if n <= 500 else
    (_ for _ in ()).throw(ValueError("factorial too large")),
    "min": _agg(min), "max": _agg(max), "sum": _agg(sum),
    "mean": _agg(statistics.fmean), "median": _agg(statistics.median),
    "stdev": _agg(statistics.stdev), "pstdev": _agg(statistics.pstdev),
}
CALC_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expr: str) -> Any:
    """A number from an arithmetic expression. Only numbers, + - * / // % **,
    parentheses, lists and the names in CALC_FUNCS / CALC_CONSTS."""
    expr = str(expr or "").replace("^", "**").replace("×", "*").replace("÷", "/")
    if len(expr) > 500:
        raise ValueError("expression too long")
    tree = ast.parse(expr, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 1000:
                raise ValueError("exponent too large")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            return _UNARY[type(n.op)](ev(n.operand))
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(x) for x in n.elts]
        if isinstance(n, ast.Name) and n.id in CALC_CONSTS:
            return CALC_CONSTS[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in CALC_FUNCS and not n.keywords:
            return CALC_FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"not allowed in calc: {ast.dump(n)[:60]}")
    return ev(tree)


#: unit -> (dimension, factor to the base unit)
UNITS: Dict[str, Tuple[str, float]] = {
    # length, base metre
    "mm": ("length", 1e-3), "cm": ("length", 1e-2), "m": ("length", 1.0),
    "km": ("length", 1e3), "in": ("length", 0.0254), "ft": ("length", 0.3048),
    "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    "um": ("length", 1e-6), "nm": ("length", 1e-9), "mil": ("length", 2.54e-5),
    # mass, base kilogram
    "mg": ("mass", 1e-6), "g": ("mass", 1e-3), "kg": ("mass", 1.0),
    "t": ("mass", 1e3), "lb": ("mass", 0.45359237), "oz": ("mass", 0.028349523125),
    # time, base second
    "ms": ("time", 1e-3), "s": ("time", 1.0), "min": ("time", 60.0),
    "h": ("time", 3600.0), "day": ("time", 86400.0), "week": ("time", 604800.0),
    # volume, base litre
    "ml": ("volume", 1e-3), "l": ("volume", 1.0), "m3": ("volume", 1e3),
    "gal": ("volume", 3.785411784), "qt": ("volume", 0.946352946),
    "floz": ("volume", 0.0295735295625), "ft3": ("volume", 28.316846592),
    # pressure, base pascal
    "pa": ("pressure", 1.0), "kpa": ("pressure", 1e3), "mpa": ("pressure", 1e6),
    "bar": ("pressure", 1e5), "psi": ("pressure", 6894.757293168),
    "atm": ("pressure", 101325.0),
    # data, base byte
    "b": ("data", 1.0), "kb": ("data", 1e3), "mb": ("data", 1e6),
    "gb": ("data", 1e9), "tb": ("data", 1e12), "kib": ("data", 1024.0),
    "mib": ("data", 1024.0 ** 2), "gib": ("data", 1024.0 ** 3),
    # energy, base joule
    "j": ("energy", 1.0), "kj": ("energy", 1e3), "cal": ("energy", 4.184),
    "kcal": ("energy", 4184.0), "wh": ("energy", 3600.0),
    "kwh": ("energy", 3.6e6), "btu": ("energy", 1055.05585262),
    # force, base newton
    "n": ("force", 1.0), "kn": ("force", 1e3), "lbf": ("force", 4.4482216152605),
    # speed, base m/s
    "m/s": ("speed", 1.0), "km/h": ("speed", 1 / 3.6), "mph": ("speed", 0.44704),
    "kn_speed": ("speed", 0.514444),
}
_UNIT_ALIASES = {"inch": "in", "inches": "in", "feet": "ft", "foot": "ft",
                 "meter": "m", "metre": "m", "meters": "m", "metres": "m",
                 "mile": "mi", "miles": "mi", "pound": "lb", "pounds": "lb",
                 "lbs": "lb", "kilogram": "kg", "kilograms": "kg",
                 "gram": "g", "grams": "g", "second": "s", "seconds": "s",
                 "sec": "s", "minute": "min", "minutes": "min", "hr": "h",
                 "hour": "h", "hours": "h", "days": "day", "weeks": "week",
                 "liter": "l", "litre": "l", "liters": "l", "litres": "l",
                 "gallon": "gal", "gallons": "gal", "µm": "um",
                 "micron": "um", "c": "degc", "f": "degf", "k": "kelvin",
                 "°c": "degc", "°f": "degf", "celsius": "degc",
                 "fahrenheit": "degf"}
_TEMPS = ("degc", "degf", "kelvin")


def convert(value: float, src: str, dst: str) -> float:
    s = _UNIT_ALIASES.get(src.strip().lower(), src.strip().lower())
    d = _UNIT_ALIASES.get(dst.strip().lower(), dst.strip().lower())
    if s in _TEMPS or d in _TEMPS:
        if s not in _TEMPS or d not in _TEMPS:
            raise ValueError(f"cannot convert {src} to {dst}")
        k = {"degc": value + 273.15, "degf": (value - 32) * 5 / 9 + 273.15,
             "kelvin": value}[s]
        return {"degc": k - 273.15, "degf": (k - 273.15) * 9 / 5 + 32,
                "kelvin": k}[d]
    if s not in UNITS or d not in UNITS:
        unknown = src if s not in UNITS else dst
        raise ValueError(f"unknown unit {unknown!r}")
    if UNITS[s][0] != UNITS[d][0]:
        raise ValueError(f"{src} is {UNITS[s][0]}, {dst} is {UNITS[d][0]}")
    return value * UNITS[s][1] / UNITS[d][1]


def _fmt_num(x: Any) -> str:
    if isinstance(x, float):
        if x.is_integer() and abs(x) < 1e15:
            return str(int(x))
        return f"{x:.10g}"
    if isinstance(x, list):
        return "[" + ", ".join(_fmt_num(v) for v in x) + "]"
    return str(x)


@tool('{"expr": "2*(3+4)/7"} | {"convert": [12, "in", "cm"]} | '
      '{"days_between": ["2026-01-01", "2026-03-01"]} | {"today": true} — '
      "exact arithmetic (sqrt, log, mean, median, stdev, min, max, sum), "
      "unit conversion and dates; use it instead of doing maths in your head")
def calc(args: Dict[str, Any]) -> Result:
    if args.get("today"):
        now = datetime.now()
        return True, now.strftime("Today is %A %Y-%m-%d, %H:%M local time."), \
            {"date": now.date().isoformat()}
    try:
        if "days_between" in args:
            a, b = args["days_between"]
            n = (date.fromisoformat(str(b)) - date.fromisoformat(str(a))).days
            return True, f"{n} day(s) from {a} to {b}.", {"days": n}
        if "convert" in args:
            value, src, dst = args["convert"]
            out = convert(float(value), str(src), str(dst))
            return True, (f"{_fmt_num(float(value))} {src} = "
                          f"{_fmt_num(out)} {dst}"), {"value": out}
    except (ValueError, TypeError) as exc:
        return False, f"calc: {exc}", {}
    expr = str(args.get("expr", "")).strip()
    if not expr:
        return False, "Give {'expr': ...}, {'convert': [value, from, to]}, " \
            "{'days_between': [a, b]} or {'today': true}.", {}
    try:
        val = safe_eval(expr)
    except ZeroDivisionError:
        return False, "division by zero", {}
    except (ValueError, TypeError, SyntaxError, OverflowError,
            statistics.StatisticsError) as exc:
        return False, f"calc could not evaluate {expr!r}: {exc}", {}
    return True, f"{expr} = {_fmt_num(val)}", {"value": val}


# ============================================================
# quote_check — is a quoted passage really in the vault?
# ============================================================

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'") \
        .replace("“", '"').replace("”", '"')
    return _WS.sub(" ", text).strip().lower()


QUOTE_READ_BUDGET = 200_000_000   # bytes read per check, at most
QUOTE_FILE_LIMIT = 1_000_000      # bytes read from any one file


def find_quote(quote: str, files: Sequence[Path]) -> Dict[str, Any]:
    """Exact (whitespace- and case-insensitive) match of `quote` in `files`,
    else the closest passage and how close it is (0–1). Reads at most
    QUOTE_READ_BUDGET bytes in all, so a huge vault cannot stall a turn."""
    q = _norm(quote)
    words = [w for w in re.findall(r"\w+", q) if len(w) > 3]
    rare = sorted(set(words), key=len, reverse=True)[:3]
    best = {"found": False, "file": "", "ratio": 0.0, "closest": ""}
    budget = QUOTE_READ_BUDGET
    for f in files:
        if budget <= 0:
            best["partial"] = True
            break
        try:
            text = read_text(f, QUOTE_FILE_LIMIT)
        except OSError:
            continue
        budget -= len(text)
        t = _norm(text)
        if q and q in t:
            i = t.index(q)
            return {"found": True, "file": str(f), "ratio": 1.0,
                    "closest": t[max(0, i - 60): i + len(q) + 60]}
        if not rare or not any(w in t for w in rare):
            continue
        for w in rare:
            for m in list(re.finditer(re.escape(w), t))[:20]:
                lo = max(0, m.start() - len(q))
                window = t[lo: m.end() + len(q)]
                sm = difflib.SequenceMatcher(None, q, window, autojunk=False)
                blk = sm.find_longest_match(0, len(q), 0, len(window))
                start = max(0, blk.b - blk.a)
                cand = window[start: start + len(q)]
                r = difflib.SequenceMatcher(None, q, cand).ratio()
                if r > best["ratio"]:
                    best = {"found": False, "file": str(f),
                            "ratio": round(r, 2), "closest": cand}
    return best


# ============================================================
# The tool set
# ============================================================

def make_extra_tools(vault_dir: Path, *, notes: "SharedNotes",
                     roots: Sequence[Path] = (),
                     test_timeout: float = 300.0) -> Dict[str, ToolFn]:
    vault_dir = Path(vault_dir)
    read_roots = [vault_dir.resolve()] + [Path(r).resolve() for r in roots]

    def data_root() -> Path:
        import data_index
        return data_index.input_dir(vault_dir)

    def find(raw: str) -> Path:
        return resolve_in(raw, read_roots, vault_dir)

    def load_frame(raw: str, nrows: Optional[int] = None):
        import pandas as pd
        p = find(raw)
        suf = p.suffix.lower()
        if suf in (".xlsx", ".xls"):
            return p, pd.read_excel(p, nrows=nrows)
        if suf == ".json":
            return p, pd.read_json(p)
        if suf == ".parquet":
            return p, pd.read_parquet(p)
        sep = "\t" if suf == ".tsv" else None
        return p, pd.read_csv(p, sep=sep, engine="python", nrows=nrows)

    # -- speed ------------------------------------------------------------
    @tool('{"query": "average runtime by pump"} — a table the app already '
          "computed for a matching question, if its source files are unchanged")
    def cached_result(args):
        query = str(args.get("query", "")).strip()
        if not query:
            return False, "No query provided.", {}
        import derived_results
        hit = derived_results.DerivedStore(vault_dir).find_fresh(query)
        if hit is None:
            return True, "No saved result matches; compute it.", {"found": False}
        try:
            import pandas as pd
            head = pd.read_csv(hit.output, nrows=25).to_string(index=False)
        except Exception as exc:                          # noqa: BLE001
            head = f"(the saved table could not be read: {exc})"
        msg = (f"Saved result for {hit.label!r} ({hit.rows} rows; sources "
               f"unchanged since it was computed):\n{head}")
        return True, _clip(msg), {"found": True, "id": hit.id,
                                  "label": hit.label, "output": hit.output}

    @tool('{"question": "...", "k": 3} — earlier council verdicts on '
          "similar questions")
    def recall_decision(args):
        from . import past_decisions
        q = str(args.get("question", "") or args.get("query", "")).strip()
        if not q:
            return False, "No question provided.", {}
        k = max(1, min(8, int(args.get("k", 3) or 3)))
        found = past_decisions.recall(vault_dir, q, k=k)
        if not found:
            return True, "No earlier decision on a similar question.", \
                {"decisions": []}
        return True, _clip(past_decisions.block(found)), \
            {"decisions": [d.question for d in found]}

    @tool("{} — every data file in data_in: type, rows x columns, column "
          "names")
    def data_digest(args):
        import dataset_digest
        text = dataset_digest.get_digest(vault_dir)
        if not text:
            return True, "The data folder holds no data files.", {"files": 0}
        return True, text, {"chars": len(text)}

    @tool('{"file": "runs.csv", "rows": 10} — columns, types and the first '
          "rows of one data file")
    def table_peek(args):
        rows = max(1, min(50, int(args.get("rows", 10) or 10)))
        p, df = load_frame(str(args.get("file", "")), nrows=rows)
        types = ", ".join(f"{c} ({t})" for c, t in df.dtypes.astype(str).items())
        msg = (f"{_show(p, read_roots)} — columns: {types}\n"
               f"{df.head(rows).to_string(index=False, max_colwidth=40)}")
        return True, _clip(msg), {"file": str(p), "columns": list(map(str, df.columns))}

    # -- accuracy ---------------------------------------------------------
    @tool('{"quote": "exact words", "file": "optional.md"} — checks that a '
          "passage you quote is really in the vault, else shows the closest")
    def quote_check(args):
        quote = str(args.get("quote", "")).strip()
        if len(quote) < 8:
            return False, "Give a quote of at least 8 characters.", {}
        if args.get("file"):
            files = [find(str(args["file"]))]
        else:
            files = list(walk_files(vault_dir, vault_dir, TEXT_SUFFIXES))
        res = find_quote(quote, files)
        where = _show(Path(res["file"]), read_roots) if res["file"] else ""
        if res["found"]:
            msg = f"VERIFIED: the quote appears in {where}."
        elif res["ratio"] >= 0.6:
            msg = (f"NOT FOUND as written. Closest ({res['ratio']:.0%} alike) "
                   f"in {where}: \"{res['closest']}\"")
        else:
            msg = "NOT FOUND: nothing in the vault says this."
        if res.get("partial") and not res["found"]:
            msg += " (the vault is large; only part of it was searched)"
        return True, msg, dict(res, file=where)

    @tool('{"field": "Point of Contact", "value": "Bob"} finds files; '
          '{"field": "...", "file": "report.csv"} reads the value in one file')
    def field_lookup(args):
        import field_search as fs
        field = str(args.get("field", "")).strip()
        if not field:
            return False, "No field provided.", {}
        if args.get("file"):
            p = find(str(args["file"]))
            vals = fs.extract_field_value(p, field) or []
            return True, (f"{field} in {_show(p, read_roots)}: "
                          + (", ".join(vals) if vals else "(not found)")), \
                {"values": vals}
        value = str(args.get("value", "")).strip()
        if not value:
            return False, "Give a value to look for, or a file to read.", {}
        stats: Dict[str, Any] = {}
        hits = fs.find_files_with_field_value(vault_dir, field, value,
                                              limit=40, stats=stats)
        hits = [(f, c) for f, c in hits if _readable(Path(f), vault_dir)]
        lines = [f"- {_show(Path(f).resolve(), read_roots)}: {c[:160]}"
                 for f, c in hits]
        head = f"{len(hits)} file(s) with {field} = {value}. " + \
            fs.coverage_line(stats)
        return True, _clip(head + "\n" + "\n".join(lines)), \
            {"files": [f for f, _ in hits]}

    @tool('{"file": "runs.csv", "column": "runtime"} — exact count, missing, '
          "min, max, mean, std (or unique values) for a column, or all columns")
    def column_stats(args):
        p = find(str(args.get("file", "")))
        col = str(args.get("column", "")).strip()
        if p.suffix.lower() in (".csv", ".tsv", ".txt"):
            import stats_cache
            st = stats_cache.compute_column_stats(
                p, sep="\t" if p.suffix.lower() == ".tsv" else ",")
            cols = st.get("column_stats", {})
            rows = st.get("rows")
        else:
            _p, df = load_frame(str(p))
            rows = len(df)
            cols = {}
            for c in df.columns:
                s = df[c]
                if s.dtype.kind in "iuf":
                    cols[str(c)] = {"dtype": "numeric", "count": int(s.count()),
                                    "missing": int(s.isna().sum()),
                                    "min": float(s.min()), "max": float(s.max()),
                                    "mean": float(s.mean()), "std": float(s.std()),
                                    "sum": float(s.sum())}
                else:
                    vc = s.astype(str).value_counts()
                    cols[str(c)] = {"dtype": "text", "count": int(s.count()),
                                    "missing": int(s.isna().sum()),
                                    "n_unique": int(len(vc)),
                                    "top": str(vc.index[0]) if len(vc) else ""}
        if col:
            match = [c for c in cols if c.lower() == col.lower()]
            if not match:
                return False, (f"No column {col!r}. Columns: "
                               + ", ".join(list(cols)[:40])), {}
            cols = {match[0]: cols[match[0]]}
        lines = [f"{_show(p, read_roots)}: {rows} rows"]
        for c, s in list(cols.items())[:40]:
            parts = ", ".join(f"{k}={_fmt_num(v) if isinstance(v, float) else v}"
                              for k, v in s.items())
            lines.append(f"- {c}: {parts}")
        return True, _clip("\n".join(lines)), {"rows": rows, "columns": cols}

    @tool('{"code": "result_df = pd.read_csv(\'runs.csv\').groupby(...)..."}'
          " — pandas over data_in in a sandboxed child process (120 s); "
          "assign the answer to `result_df`")
    def data_query(args):
        code = str(args.get("code", "")).strip()
        if not code:
            return False, "No code provided.", {}
        if "result_df" not in code and re.search(r"^result\s*=", code, re.M):
            code += "\nresult_df = result\n"   # the name models reach for
        from . import analyst_step
        df, log = analyst_step.run_code(code, data_root())
        if df is None:
            return False, _clip(f"The query failed: {log}", 2000), {}
        import vault_analyst as va
        table = va.format_result_for_prompt(df, max_rows=100, max_chars=5000)
        return True, table, {"rows": int(df.shape[0]), "cols": int(df.shape[1])}

    # -- reading ----------------------------------------------------------
    @tool('{"name": "notes.md", "heading": "Results"} or {"name": ..., '
          '"start": 40, "end": 90} — part of a file, with line numbers')
    def read_section(args):
        p = find(str(args.get("name", "") or args.get("file", "")))
        lines = read_text(p).splitlines()
        heading = str(args.get("heading", "")).strip().lower()
        if heading:
            start = None
            level = 0
            for i, ln in enumerate(lines):
                m = re.match(r"^(#+)\s*(.*)$", ln.strip())
                if m and heading in m.group(2).lower():
                    start, level = i, len(m.group(1))
                    break
                if not m and ln.strip().lower().rstrip(":") == heading:
                    start, level = i, 99
                    break
            if start is None:
                heads = [ln.strip() for ln in lines if ln.strip().startswith("#")]
                return False, (f"No heading {heading!r}. Headings: "
                               + "; ".join(heads[:30])), {}
            end = len(lines)
            for j in range(start + 1, len(lines)):
                m = re.match(r"^(#+)\s", lines[j].strip())
                if m and len(m.group(1)) <= level:
                    end = j
                    break
                if level == 99 and not lines[j].strip() and j + 1 < len(lines) \
                        and lines[j + 1].strip().endswith(":"):
                    end = j
                    break
        else:
            start = max(0, int(args.get("start", 1) or 1) - 1)
            end = min(len(lines), int(args.get("end", start + 80) or start + 80))
        body = "\n".join(f"{i + 1:5d}  {lines[i]}" for i in range(start, end))
        return True, _clip(f"{_show(p, read_roots)} lines {start + 1}-{end} "
                           f"of {len(lines)}:\n{body}", 8000), \
            {"start": start + 1, "end": end, "total": len(lines)}

    @tool('{"name": "big_report.md", "focus": "pump failures", "tokens": '
          "1500} — a large file cut to fit, keeping the lines about the focus")
    def condense_file(args):
        import context_condenser as cc
        p = find(str(args.get("name", "") or args.get("file", "")))
        text = read_text(p)
        target = max(200, min(6000, int(args.get("tokens", 1500) or 1500)))
        terms = cc.extract_terms(str(args.get("focus", "")))
        out = cc.condense_deterministic(text, target, terms=terms)
        return True, f"{_show(p, read_roots)} (condensed):\n{out}", \
            {"chars_in": len(text), "chars_out": len(out)}

    @tool('{"query": "...", "k": 5} — the vault passages that best match, '
          "with their files")
    def semantic_search(args):
        query = str(args.get("query", "")).strip()
        if not query:
            return False, "No query provided.", {}
        k = max(1, min(10, int(args.get("k", 5) or 5)))
        from . import vault_context
        rag = vault_context._rag_for(vault_dir)
        result = rag.search(query, n_results=k)
        chunks = list(getattr(result, "chunks", result) or [])
        backend = str(getattr(rag, "backend_name", "") or "keyword")
        if not chunks:
            return True, f"No passages match ({backend} search).", {"hits": 0}
        parts = [f"{len(chunks)} passage(s), {backend} search:"]
        for i, c in enumerate(chunks[:k], 1):
            parts.append(f"[{i}] {c.get('source', '?')}\n"
                         f"{str(c.get('text', '')).strip()[:700]}")
        return True, _clip("\n\n".join(parts)), {"hits": len(chunks),
                                                 "backend": backend}

    # -- code -------------------------------------------------------------
    @tool('{"path": "tools/report.py"} — every function, class and method '
          "with its parameters (a folder lists its code files)")
    def code_outline(args):
        p = find(str(args.get("path", "") or args.get("file", "")))
        if p.is_dir():
            files = [_show(f, read_roots) for f in walk_files(p, vault_dir,
                                                              CODE_SUFFIXES, 300)]
            return True, _clip("\n".join(files) or "(no code files)"), \
                {"files": files}
        import code_chunks
        src = read_text(p)
        if p.suffix.lower() == ".py":
            sigs = code_chunks.extract_signatures(src, p.name)
            if not sigs and src.strip():
                return False, f"{p.name} does not parse as Python.", {}
            lines = []
            for s in sigs:
                params = ", ".join(
                    x.name + (f": {x.annotation}" if x.annotation else "")
                    + (f"={x.default}" if x.default else "") for x in s.params)
                ret = f" -> {s.returns}" if s.returns else ""
                doc = f"  — {s.doc.splitlines()[0][:80]}" if s.doc else ""
                lines.append(f"{s.lineno:5d}  {s.kind} {s.qualname}({params})"
                             f"{ret}{doc}")
        else:
            chunks = code_chunks.chunk_source(src, p.name, p.suffix.lower())
            lines = [f"{c.lineno:5d}  {c.kind} {c.name}".rstrip()
                     for c in chunks]
        return True, _clip(f"{_show(p, read_roots)}:\n" + "\n".join(lines)), \
            {"count": len(lines)}

    @tool('{"pattern": "def load_", "path": "optional/folder", "glob": "*.py"}'
          " — regex search in code and text files, file:line hits")
    def code_grep(args):
        pat = str(args.get("pattern", ""))
        if not pat:
            return False, "No pattern provided.", {}
        try:
            rx = re.compile(pat, re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            return False, f"Bad regex: {exc}", {}
        base = find(str(args["path"])) if args.get("path") else vault_dir
        glob = str(args.get("glob", "") or "")
        hits: List[str] = []
        files = [base] if base.is_file() else walk_files(
            base, vault_dir, CODE_SUFFIXES | TEXT_SUFFIXES)
        for f in files:
            if glob and not f.match(glob):
                continue
            try:
                for n, ln in enumerate(read_text(f, 1_000_000).splitlines(), 1):
                    if len(ln) <= 2000 and rx.search(ln):
                        hits.append(f"{_show(f, read_roots)}:{n}: {ln.strip()[:200]}")
                        if len(hits) >= 80:
                            break
            except OSError:
                continue
            if len(hits) >= 80:
                hits.append("… (stopped at 80 hits; narrow the pattern or path)")
                break
        return True, _clip("\n".join(hits) or "(no matches)"), {"hits": len(hits)}

    @tool('{"code": "..."} or {"path": "x.py"} — syntax errors, undefined '
          "names and unused imports, without running anything")
    def lint_check(args):
        if args.get("path"):
            p = find(str(args["path"]))
            code, name = read_text(p), _show(p, read_roots)
        else:
            code, name = str(args.get("code", "")), "<code>"
        if not code.strip():
            return False, "No code provided.", {}
        problems = lint(code, name)
        if not problems:
            return True, f"{name}: no problems found.", {"problems": []}
        return True, _clip(f"{name}: {len(problems)} problem(s)\n"
                           + "\n".join(problems)), {"problems": problems}

    @tool('{"path": "tests/test_report.py", "k": "optional -k filter"} — runs'
          " pytest on it in a child process (5 min limit); pass/fail summary")
    def run_tests(args):
        p = find(str(args.get("path", "")))
        from . import child_proc
        cwd = p if p.is_dir() else p.parent
        for parent in [cwd, *cwd.parents]:
            if any((parent / m).exists() for m in
                   ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini",
                    ".git")):
                cwd = parent
                break
            if parent in read_roots:
                break
        argv = [sys.executable, "-m", "pytest", "-q", "-x", "--no-header",
                "-p", "no:cacheprovider", str(p)]
        if args.get("k"):
            argv += ["-k", str(args["k"])]
        res = child_proc.run(argv, cwd=str(cwd), timeout=test_timeout,
                             env=child_proc.child_env(
                                 {"PYTHONDONTWRITEBYTECODE": "1",
                                  "QT_QPA_PLATFORM": "offscreen",
                                  "COUNCIL_NO_DIALOGS": "1"}),
                             memory_limit_mb=4096)
        if res.timed_out:
            return False, f"Tests stopped after {test_timeout:.0f} s.", {}
        out = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
        tail = "\n".join(out.strip().splitlines()[-40:])
        ok = res.returncode == 0
        return True, f"pytest {'PASSED' if ok else 'FAILED'} " \
            f"(exit {res.returncode}):\n{tail}", {"passed": ok,
                                                  "returncode": res.returncode}

    @tool('{"path": "tools/report.py", "content": "the whole new file"} — '
          "the change as a unified diff; nothing is written")
    def diff_preview(args):
        content = str(args.get("content", ""))
        raw = str(args.get("path", "") or args.get("name", ""))
        try:
            p = find(raw)
            old, label = read_text(p), _show(p, read_roots)
        except PathError:
            resolve_in(raw, read_roots, vault_dir, must_exist=False)
            old, label = "", raw
        diff = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=f"a/{label}", tofile=f"b/{label}"))
        if not diff:
            return True, "No change.", {"changed": False}
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+")
                    and not ln.startswith("+++"))
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-")
                      and not ln.startswith("---"))
        return True, _clip(f"+{added} -{removed} (not written)\n{diff}", 8000), \
            {"changed": True, "added": added, "removed": removed}

    # -- coordination -----------------------------------------------------
    @tool('{"post": "a fact the others should reuse"} or {"read": true} — '
          "this question's shared scratchpad")
    def shared_notes(args):
        if args.get("post"):
            n = notes.post(str(args.get("_role") or "member"), str(args["post"]))
            return True, f"Posted (note {n}).", {"count": n}
        text = notes.text()
        return True, text or "(no notes yet)", {"count": len(notes)}

    @tool('{"file": "runs.csv", "kind": "line", "columns": ["date", '
          '"runtime"]} — a chart PNG into data_out; without kind, lists the '
          "charts that fit")
    def make_chart(args):
        from . import grapher
        cols = args.get("columns") or []
        if isinstance(cols, str):
            cols = [c.strip() for c in cols.split(",") if c.strip()]
        p, df = load_frame(str(args.get("file", "")))
        df = grapher._with_real_dates(df)
        kind = str(args.get("kind", "")).strip()
        if not kind:
            fits = grapher.choices_for(df, cols)
            if not fits:
                return True, grapher.hint_for(cols, fits), {"choices": []}
            return True, "Charts that fit: " + ", ".join(
                f"{c.key} ({c.label})" for c in fits), \
                {"choices": [c.key for c in fits]}
        fig = grapher.build_figure(df, kind, cols)
        if not fig.ok:
            return False, fig.message, {}
        import data_index
        out_dir = data_index.output_dir(vault_dir) / "charts"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w-]+", "_", f"{p.stem}_{kind}")[:60]
        out = out_dir / f"{stem}.png"
        i = 2
        while out.exists():
            out = out_dir / f"{stem}_{i}.png"
            i += 1
        tmp = out.with_suffix(".png.tmp")
        fig.figure.savefig(tmp, format="png", dpi=110)
        tmp.replace(out)
        return True, f"Chart saved: {_show(out, read_roots)} ({fig.message})", \
            {"path": str(out)}

    @tool('{} or {"probe": true} — the machines the council routes to: up, '
          "cooling down after a failure, and recent load")
    def node_status(args):
        from . import node_routing, usage_log
        routing = node_routing.load(vault_dir)
        nodes = node_routing.enabled_nodes(routing)
        since = time.time() - 86400
        summary = usage_log.summarise(usage_log.read(vault_dir, since))
        by_host: Dict[str, Dict[str, Any]] = {}
        for g in summary:
            h = by_host.setdefault(g["host"], {"calls": 0, "seconds": 0.0,
                                               "roles": set()})
            h["calls"] += g["calls"]
            h["seconds"] += g["seconds"]
            h["roles"].add(g["role"])
        lines = [f"This PC: {by_host.get('local', {}).get('calls', 0)} "
                 "model call(s) in the last 24 h."]
        rows = []
        for n in nodes:
            url = n.url
            cool = node_routing.cooling(url)
            up = ""
            if args.get("probe"):
                up = " — " + ("answering" if _probe(url) else "NOT answering")
            use = next((v for k, v in by_host.items()
                        if k and k != "local" and _same_host(k, url)), None)
            load = (f"{use['calls']} call(s), {use['seconds']:.0f} s, roles "
                    f"{', '.join(sorted(use['roles']))}") if use else "idle"
            state = f"cooling down {cool:.0f} s" if cool > 0 else "available"
            lines.append(f"- {n.name or url} ({url}): {state}; last 24 h: "
                         f"{load}{up}")
            rows.append({"url": url, "cooling_s": cool,
                         "calls": use["calls"] if use else 0})
        if not nodes:
            lines.append("No other machines are set up (Council Map ▸ "
                         "Machines & roles).")
        return True, "\n".join(lines), {"nodes": rows}

    tools = {"cached_result": cached_result, "recall_decision": recall_decision,
             "data_digest": data_digest, "table_peek": table_peek,
             "calc": calc, "quote_check": quote_check,
             "field_lookup": field_lookup, "column_stats": column_stats,
             "data_query": data_query, "read_section": read_section,
             "condense_file": condense_file,
             "semantic_search": semantic_search,
             "code_outline": code_outline, "code_grep": code_grep,
             "lint_check": lint_check, "run_tests": run_tests,
             "diff_preview": diff_preview, "shared_notes": shared_notes,
             "make_chart": make_chart, "node_status": node_status}
    return {n: _guarded(fn) for n, fn in tools.items()}


def _guarded(fn: ToolFn) -> ToolFn:
    """`fn`, with a refused path reported as a failed call, not raised."""
    def call(args: Dict[str, Any]) -> Result:
        try:
            return fn(args)
        except PathError as exc:
            return False, str(exc), {}
    call.help = getattr(fn, "help", "")                  # type: ignore[attr-defined]
    call.__name__ = fn.__name__
    return call


def _same_host(host: str, url: str) -> bool:
    from urllib.parse import urlparse
    a = urlparse(host if "://" in host else f"http://{host}")
    b = urlparse(url if "://" in url else f"http://{url}")
    return (a.hostname, a.port or 11434) == (b.hostname, b.port or 11434)


def _probe(url: str, timeout: float = 2.0) -> bool:
    """Is the Ollama server at `url` answering? Only for a configured node."""
    import urllib.request
    from . import node_routing
    if not node_routing.is_allowed_host(url):
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:                                     # noqa: BLE001
        return False


def lint(code: str, name: str = "<code>") -> List[str]:
    """Syntax errors, then pyflakes' findings if pyflakes is installed.
    Parses only: nothing is imported or executed."""
    try:
        compile(code, name, "exec", dont_inherit=True,
                flags=ast.PyCF_ONLY_AST)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: SyntaxError: {exc.msg}"]
    try:
        from pyflakes import api as _api
        from pyflakes import reporter as _rep
    except Exception:                                     # noqa: BLE001
        return []
    import io
    out, err = io.StringIO(), io.StringIO()
    _api.check(code, name, _rep.Reporter(out, err))
    lines = [ln for ln in (out.getvalue() + err.getvalue()).splitlines() if ln]
    return [ln.replace(f"{name}:", "line ", 1) for ln in lines][:60]


# ============================================================
# Shared notes, the per-question cache and the role split
# ============================================================

class SharedNotes:
    """One question's scratchpad. Cleared at the start of every question."""

    MAX = 60

    def __init__(self) -> None:
        self._items: List[Tuple[str, str]] = []
        self._lock = threading.Lock()

    def post(self, role: str, text: str) -> int:
        with self._lock:
            if len(self._items) < self.MAX:
                self._items.append((role, text.strip()[:1200]))
            return len(self._items)

    def text(self) -> str:
        with self._lock:
            return "\n".join(f"[{i}] {r}: {t}"
                             for i, (r, t) in enumerate(self._items, 1))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


#: Tools whose answer depends only on their arguments and the files, so a
#: repeat within one question is served from the cache.
CACHEABLE = frozenset({
    "vault_list", "vault_read", "vault_search", "api_search", "api_signature",
    "cached_result", "recall_decision", "data_digest", "table_peek", "calc",
    "quote_check", "field_lookup", "column_stats", "data_query",
    "read_section", "condense_file", "semantic_search", "code_outline",
    "code_grep", "lint_check", "diff_preview", "node_status"})

#: role -> its tools. Short lists on purpose: a small model picks worse from
#: a long menu, and every tool call is another model round-trip.
ROLE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "coder": ("run_python", "lint_check", "run_tests", "code_outline",
              "code_grep", "diff_preview", "api_search", "api_signature",
              "data_query", "cached_result", "vault_read", "vault_search",
              "shared_notes"),
    "intern": ("data_digest", "table_peek", "column_stats", "field_lookup",
               "data_query", "cached_result", "calc", "run_python",
               "vault_list", "vault_read", "vault_search", "vault_save",
               "shared_notes"),
    "skeptic": ("quote_check", "column_stats", "calc", "vault_read",
                "vault_search", "shared_notes"),
    "sage": ("semantic_search", "read_section", "condense_file", "vault_read",
             "shared_notes"),
    "writer": ("read_section", "condense_file", "semantic_search",
               "vault_read", "calc", "shared_notes"),
    "strategist": ("recall_decision", "data_digest", "semantic_search",
                   "node_status", "calc", "shared_notes"),
    "artist": ("make_chart", "table_peek", "shared_notes"),
    "peasant": ("field_lookup", "calc", "shared_notes"),
    "content": ("read_section", "semantic_search", "shared_notes"),
    "director": ("read_section", "semantic_search", "shared_notes"),
}

#: What the Judge gets — not a tool loop (the Judge ranks; it does not act)
#: but checks the app runs for it before ranking: see judge_checks.
JUDGE_CHECKS = ("quote_check",)


class ToolSet(dict):
    """The tools by name, plus the per-question cache, the shared notes, the
    role split and the usage meter. A plain dict to anything that only wants
    the tools."""

    def __init__(self, tools: Dict[str, ToolFn], *, notes: SharedNotes,
                 vault_dir: Optional[Path] = None,
                 role_tools: Optional[Dict[str, Sequence[str]]] = None):
        super().__init__(tools)
        self.notes = notes
        self.vault_dir = Path(vault_dir) if vault_dir else None
        self.role_tools = dict(role_tools or ROLE_TOOLS)
        self._cache: Dict[str, Result] = {}
        self._lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []

    def new_turn(self) -> None:
        """A new question: empty the cache and the shared notes."""
        with self._lock:
            self._cache.clear()
            self.calls.clear()
        self.notes.clear()

    def names_for(self, role: str) -> List[str]:
        return [n for n in self.role_tools.get(role, ()) if n in self]

    def for_role(self, role: str) -> Dict[str, ToolFn]:
        """`role`'s tools, each wrapped to cache, to catch, and to count."""
        return {n: self._wrap(role, n, self[n]) for n in self.names_for(role)}

    def _wrap(self, role: str, name: str, fn: ToolFn) -> ToolFn:
        def call(args: Dict[str, Any]) -> Result:
            key = ""
            if name in CACHEABLE:
                try:
                    key = name + json.dumps(args, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    key = ""
                with self._lock:
                    hit = self._cache.get(key) if key else None
                if hit is not None:
                    self._meter(role, name, hit[0], 0.0, cached=True)
                    ok, msg, payload = hit
                    return ok, msg + "\n(cached: same call earlier in this " \
                        "question)", payload
            if name == "shared_notes":
                args = dict(args, _role=role)
            t0 = time.perf_counter()
            try:
                res = fn(args)
            except PathError as exc:
                res = (False, str(exc), {})
            except Exception as exc:                      # noqa: BLE001
                res = (False, f"{name} failed: {type(exc).__name__}: {exc}", {})
            self._meter(role, name, res[0], time.perf_counter() - t0)
            if key and res[0]:
                with self._lock:
                    self._cache[key] = res
            return res
        call.help = getattr(fn, "help", "")              # type: ignore[attr-defined]
        call.__name__ = name
        return call

    def _meter(self, role: str, name: str, ok: bool, seconds: float,
               cached: bool = False) -> None:
        entry = {"role": role, "tool": name, "ok": bool(ok),
                 "seconds": round(seconds, 3), "cached": cached}
        with self._lock:
            self.calls.append(entry)
        if self.vault_dir is not None:
            from . import usage_log
            usage_log.record_tool(self.vault_dir, entry)


# ============================================================
# The Judge's checks
# ============================================================

_QUOTED = re.compile(r"[\"“]([^\"“”\n]{25,400})[\"”]")


def judge_checks(tools: Dict[str, ToolFn], candidates: Dict[str, Any],
                 *, max_quotes: int = 8) -> str:
    """Every passage a candidate quotes, checked against the vault — for the
    Judge's ranking. "" when no candidate quotes anything."""
    qc = tools.get("quote_check")
    if qc is None:
        return ""
    lines: List[str] = []
    n = 0
    for role, data in candidates.items():
        answer = str((data or {}).get("answer", ""))
        for m in _QUOTED.finditer(answer):
            if n >= max_quotes:
                break
            n += 1
            try:
                ok, msg, _p = qc({"quote": m.group(1)})
            except Exception as exc:                      # noqa: BLE001
                ok, msg = False, f"could not check ({exc})"
            short = m.group(1)[:90] + ("…" if len(m.group(1)) > 90 else "")
            lines.append(f"- {role} quotes \"{short}\": {msg.splitlines()[0]}")
    if not lines:
        return ""
    return ("QUOTE CHECKS — each passage a candidate quoted, looked up in the "
            "user's vault. A candidate that quotes text the vault does not "
            "contain is inventing a source; rank it down.\n" + "\n".join(lines))


__all__ = ["make_extra_tools", "ToolSet", "SharedNotes", "ROLE_TOOLS",
           "CACHEABLE", "judge_checks", "calc", "safe_eval", "convert",
           "find_quote", "lint", "resolve_in", "PathError", "tool"]
