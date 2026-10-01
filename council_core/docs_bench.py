"""
council_core.docs_bench — does this model answer from documentation?

WHY AN INVENTED PACKAGE
Asking a model about numpy measures what it memorised. tests/data/docsbench/
glimmerquay is a package that exists nowhere else — real source, real
docstrings, real behaviour — so the only way to answer "what does Ledger's
capacity default to?" is to read the page the Council fetched. That makes it a
test of exactly the skill the Docs tab needs, served through the same bundled
MCP server and the same docs_qa path the user's questions take.

WHAT IS SCORED
  * questions (10)  every expected fact appears in the answer, and a cited
                    page really contains the facts (citation correctness)
  * negatives (2)   questions the docs do not answer: the right reply is
                    "not covered", not a confident invention
  * code tasks (5)  the model's code is run against hidden tests that import
                    the package, in a subprocess with a timeout, writes and
                    process launches blocked by an audit hook
plus seconds and model calls per item, and whether output was constrained.

THE CAPABILITY CHECK is a quick subset (3 questions, 1 negative, 1 code task)
the Docs tab runs to tell a user whether the model in the docs role is fit
for the job; `--full` here is the whole benchmark for the measurement phase:

    python -m council_core.docs_bench --role docs --out results.json
    python -m council_core.docs_bench --oracle      # checks the harness only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import docs_qa, docs_servers
from .docs_servers import ServerSpec

BENCH_DIR = docs_servers.APP_ROOT / "tests" / "data" / "docsbench"
BENCH_FILE = BENCH_DIR / "bench.json"
QUICK = ("q01", "q04", "q08", "n01", "c02")
CODE_TIMEOUT = 30.0
#: Below this pass rate a model is not suggested for the docs role.
GOOD_RATE = 0.8
CHECKS_FILE = "docs_checks.json"


def load_bench(path: Optional[Path] = None) -> dict:
    return json.loads(Path(path or BENCH_FILE).read_text(encoding="utf-8"))


def bench_server(bench: Optional[dict] = None) -> ServerSpec:
    """The bundled server, pointed at the benchmark's package only. No index
    cache: the package is tiny, and a stale cache must never decide a score."""
    bench = bench or load_bench()
    return docs_servers.bundled_spec(
        f"Docs benchmark ({bench['package']})", paths=[str(BENCH_DIR)],
        packages=[bench["package"]], cache=False)


# ======================================================================
# Grading
# ======================================================================

def normalize(text: str) -> str:
    t = (text or "").replace("`", "").replace("’", "'")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)          # 2,750 -> 2750
    return " ".join(t.split()).lower()


def fact_found(fact: Any, text: str) -> bool:
    """A fact (or any one of a list of spellings) in the text.

    Numbers match as whole numbers — "64" is not found in "640" — and words
    case-insensitively."""
    options = fact if isinstance(fact, list) else [fact]
    hay = normalize(text)
    for opt in options:
        o = normalize(str(opt))
        if re.fullmatch(r"[\d.]+", o):
            if re.search(rf"(?<![\d.]){re.escape(o)}(?![\d])", hay):
                return True
        elif re.search(rf"(?<![a-z0-9_]){re.escape(o)}(?![a-z0-9_])", hay):
            return True
    return False


def citation_ok(item: dict, ans: docs_qa.DocsAnswer) -> bool:
    """A cited page holds the facts, or is one of the expected pages."""
    expected = {s.lower() for s in item.get("sources") or []}
    for src in ans.cited_sources():
        if src.ref.lower() in expected or src.title.lower() in expected:
            return True
        facts = item.get("facts") or []
        if facts and all(fact_found(f, src.text) for f in facts):
            return True
    return False


#: Run before the model's code: no writes outside the temp folder, no new
#: processes, no network, no deletes. Imports the hook can't see (C code)
#: are not the risk here; a model writing `shutil.rmtree` is.
FENCE = r'''
import os, sys
sys.dont_write_bytecode = True
_ROOT = os.path.abspath(os.getcwd())
def _fence(event, args):
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.spawn",
                 "os.posix_spawn", "os.startfile", "socket.connect",
                 "socket.getaddrinfo", "os.remove", "os.unlink", "os.rmdir",
                 "os.rename", "os.replace", "shutil.rmtree", "shutil.move",
                 "webbrowser.open"):
        raise PermissionError(f"blocked in the docs benchmark: {event}")
    if event == "open" and len(args) > 1 and args[1] and any(
            c in str(args[1]) for c in "wax+"):
        target = os.path.abspath(str(args[0]))
        if not target.startswith(_ROOT):
            raise PermissionError(f"blocked write outside the sandbox: {target}")
sys.addaudithook(_fence)
'''


def run_code_test(code: str, item: dict, *, timeout: float = CODE_TIMEOUT,
                  bench_dir: Path = BENCH_DIR) -> Dict[str, Any]:
    """Run the model's code plus the hidden test; {passed, output, seconds}."""
    t0 = time.perf_counter()
    if not (code or "").strip():
        return {"passed": False, "output": "no code", "seconds": 0.0}
    with tempfile.TemporaryDirectory(prefix="docsbench_") as tmp:
        Path(tmp, "solution.py").write_text(code, encoding="utf-8")
        runner = (f"import sys\nsys.path[:0] = [{tmp!r}, {str(bench_dir)!r}]\n"
                  + FENCE + "\nfrom solution import *\n"
                  + item["hidden_test"] + "\nprint('PASS')\n")
        Path(tmp, "runner.py").write_text(runner, encoding="utf-8")
        try:
            out = subprocess.run(
                [sys.executable, "-I", "-B", "runner.py"], cwd=tmp,
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (out.stdout + out.stderr).strip()
            passed = out.returncode == 0 and text.endswith("PASS")
        except subprocess.TimeoutExpired:
            text, passed = f"timed out after {timeout:g} s", False
    return {"passed": passed, "output": text[-800:],
            "seconds": round(time.perf_counter() - t0, 3)}


# ======================================================================
# Running
# ======================================================================

@dataclass
class ItemResult:
    id: str
    kind: str                      # question | negative | code
    passed: bool
    citation_ok: Optional[bool] = None
    covered: bool = False
    seconds: float = 0.0
    model_calls: int = 0
    constrained: Optional[bool] = None
    answer: str = ""
    code: str = ""
    detail: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class BenchReport:
    model: str = ""
    role: str = ""
    items: List[ItemResult] = field(default_factory=list)
    seconds: float = 0.0
    stopped: bool = False
    error: str = ""
    started: str = ""
    #: "US" / "non-US" / "" (unknown). A non-US model can be MEASURED; it is
    #: never offered as a recommendation, however well it scores.
    origin: str = ""

    def _of(self, kind: str) -> List[ItemResult]:
        return [i for i in self.items if i.kind == kind]

    @property
    def passed(self) -> int:
        return sum(1 for i in self.items if i.passed)

    @property
    def rate(self) -> float:
        return self.passed / len(self.items) if self.items else 0.0

    @property
    def mean_seconds(self) -> float:
        return (sum(i.seconds for i in self.items) / len(self.items)
                if self.items else 0.0)

    @property
    def good(self) -> bool:
        return bool(self.items) and not self.stopped and self.rate >= GOOD_RATE

    @property
    def recommendable(self) -> bool:
        return self.good and self.origin.lower() != "non-us"

    def summary(self) -> Dict[str, Any]:
        qs, ns, cs = self._of("question"), self._of("negative"), \
            self._of("code")
        cited = [i for i in qs if i.citation_ok is not None]
        return {
            "model": self.model, "role": self.role,
            "passed": self.passed, "total": len(self.items),
            "rate": round(self.rate, 3),
            "questions": f"{sum(i.passed for i in qs)}/{len(qs)}",
            "citations_right": f"{sum(bool(i.citation_ok) for i in cited)}"
                               f"/{len(cited)}",
            "not_covered_right": f"{sum(i.passed for i in ns)}/{len(ns)}",
            "code": f"{sum(i.passed for i in cs)}/{len(cs)}",
            "mean_seconds": round(self.mean_seconds, 2),
            "total_seconds": round(self.seconds, 2),
            "mean_model_calls": round(sum(i.model_calls for i in self.items)
                                      / max(1, len(self.items)), 2),
            "constrained": any(i.constrained for i in self.items),
            "good": self.good, "recommendable": self.recommendable,
            "origin": self.origin, "stopped": self.stopped,
            "started": self.started,
        }

    def lines(self) -> List[str]:
        s = self.summary()
        if self.error and not self.items:
            return [f"Check failed: {self.error}"]
        verdict = ("stopped" if self.stopped else
                   "not reliable enough" if not self.good else
                   "good for docs questions" if self.recommendable else
                   "scored well, but non-US: measured only, not recommended")
        out = [f"{self.model or 'model'}: {s['passed']}/{s['total']} passed "
               f"({s['rate']:.0%}) — {verdict}",
               f"questions {s['questions']}, citations right "
               f"{s['citations_right']}, 'not covered' right "
               f"{s['not_covered_right']}, code {s['code']}",
               f"{s['mean_seconds']:.1f} s per item, "
               f"{s['mean_model_calls']:.1f} model calls per item"
               + ("" if s["constrained"] else
                  " (output was not schema-constrained)")]
        for i in self.items:
            if not i.passed:
                out.append(f"  ✗ {i.id} ({i.kind}): {i.detail[:160]}")
        return out

    def to_dict(self) -> dict:
        return {"summary": self.summary(),
                "items": [asdict(i) for i in self.items]}


def _select(bench: dict, items: Any) -> List[tuple]:
    chosen = []
    want = None if items in (None, "all") else set(
        QUICK if items == "quick" else items)
    for q in bench.get("questions", []):
        if want is None or q["id"] in want:
            chosen.append(("question", q))
    for q in bench.get("negatives", []):
        if want is None or q["id"] in want:
            chosen.append(("negative", q))
    for q in bench.get("code_tasks", []):
        if want is None or q["id"] in want:
            chosen.append(("code", q))
    return chosen


def run(model_call: Optional[docs_qa.ModelCall] = None, *,
        items: Any = "all", derive: str = "model", mode: str = "orchestrated",
        server: Optional[ServerSpec] = None, bench: Optional[dict] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[str], None]] = None,
        model_label: str = "", chat_tools: Optional[Callable] = None
        ) -> BenchReport:
    """Run the benchmark (or a subset) with one model. Never raises."""
    bench = bench or load_bench()
    server = server or bench_server(bench)
    report = BenchReport(model=model_label, started=time.strftime(
        "%Y-%m-%d %H:%M:%S"))
    if model_call is None:
        model_call = docs_qa.engine_model_call()
    report.role = getattr(model_call, "role", None) or \
        docs_qa.answering_role()
    if not report.model:
        report.model = docs_qa.model_label(docs_qa.role_model(report.role))
    chosen = _select(bench, items)
    t0 = time.perf_counter()
    try:
        for n, (kind, item) in enumerate(chosen, 1):
            if should_stop is not None and should_stop():
                report.stopped = True
                break
            if progress:
                progress(f"Check {n}/{len(chosen)}: {item['id']}…")
            question = item.get("question") or item.get("task", "")
            ans = docs_qa.ask(question, servers=[server],
                              packages=[bench["package"]],
                              write_code=(kind == "code"),
                              model_call=model_call, derive=derive, mode=mode,
                              should_stop=should_stop, chat_tools=chat_tools)
            if ans.stopped:
                report.stopped = True
                break
            report.items.append(_grade(kind, item, ans))
    except Exception as exc:                              # noqa: BLE001
        report.error = f"{type(exc).__name__}: {exc}"
    report.seconds = time.perf_counter() - t0
    docs_servers.release(server.name)
    return report


def _grade(kind: str, item: dict, ans: docs_qa.DocsAnswer) -> ItemResult:
    r = ItemResult(item["id"], kind, False, covered=ans.covered,
                   seconds=ans.timings.get("total_s", 0.0),
                   model_calls=ans.model_calls, constrained=ans.constrained,
                   answer=ans.answer[:600], code=ans.code[:3000],
                   notes=list(ans.notes)[:6])
    if ans.error and not ans.ok:
        r.detail = ans.error
        return r
    if kind == "question":
        missing = [f for f in item["facts"] if not fact_found(f, ans.answer)]
        r.citation_ok = citation_ok(item, ans)
        r.passed = ans.covered and not missing and r.citation_ok
        r.detail = ("not covered (wrong)" if not ans.covered else
                    f"missing {missing}" if missing else
                    "" if r.citation_ok else "cited the wrong page")
    elif kind == "negative":
        r.passed = not ans.covered
        r.detail = "" if r.passed else f"invented: {ans.answer[:120]}"
    else:
        test = run_code_test(ans.code, item)
        r.passed = test["passed"]
        r.detail = "" if r.passed else test["output"][-300:]
    return r


def capability_check(model_call: Optional[docs_qa.ModelCall] = None, *,
                     should_stop=None, progress=None,
                     model_label: str = "", origin: str = "") -> BenchReport:
    """The Docs tab's quick check: 5 items, model-derived queries."""
    report = run(model_call, items="quick", should_stop=should_stop,
                 progress=progress, model_label=model_label)
    report.origin = origin
    return report


# ======================================================================
# Remembering checks, so models can be compared
# ======================================================================

def checks_path(base: Optional[Path] = None) -> Path:
    if base is not None:
        return Path(base) if Path(base).suffix == ".json" else \
            Path(base) / CHECKS_FILE
    from . import paths
    return paths.app_dir() / CHECKS_FILE


def load_checks(base: Optional[Path] = None) -> List[dict]:
    try:
        data = json.loads(checks_path(base).read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict)] \
            if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_check(report: BenchReport, base: Optional[Path] = None,
                 keep: int = 30) -> Path:
    rows = [report.summary()] + load_checks(base)
    p = checks_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[:keep], indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


# ======================================================================
# The answer key as a model (checks the harness, not a model)
# ======================================================================

def oracle_model_call(bench: Optional[dict] = None) -> docs_qa.ModelCall:
    """A fake model that answers from the key. If the oracle does not score
    100%, the harness — not a model — is broken."""
    bench = bench or load_bench()
    by_text = {}
    for q in bench["questions"]:
        by_text[q["question"]] = ("q", q)
    for q in bench["negatives"]:
        by_text[q["question"]] = ("n", q)
    for q in bench["code_tasks"]:
        by_text[q["task"]] = ("c", q)

    def call(messages, *, json_schema=None, temperature=0.0,
             num_predict=600, seed=None, should_stop=None):
        user = messages[-1]["content"] if messages else ""
        if "search queries" in messages[0]["content"]:
            return json.dumps({"queries": [user.split("\n")[0][10:70]],
                               "package": bench["package"]})
        text = next((t for t in by_text if t in user), None)
        if text is None:
            return json.dumps({"answer": "", "sources": [],
                               "covered": False})
        kind, item = by_text[text]
        if kind == "n":
            return json.dumps({"answer": docs_qa.NOT_COVERED, "sources": [],
                               "covered": False})
        n = _page_with(user, item) or 1
        if kind == "c":
            return json.dumps({"answer": f"See [{n}].", "sources": [n],
                               "covered": True, "code": item["reference"]})
        facts = ", ".join(f if isinstance(f, str) else f[0]
                          for f in item["facts"])
        return json.dumps({"answer": f"{facts} [{n}]", "sources": [n],
                           "covered": True})

    call.role = "oracle"                                  # type: ignore
    return call


def _page_with(prompt: str, item: dict) -> int:
    pages = re.split(r"\n\n(?=\[\d+\] )", prompt.split("DOCUMENTATION\n", 1)
                     [-1])
    for page in pages:
        m = re.match(r"\[(\d+)\] (\S+)", page)
        if not m:
            continue
        if m.group(2) in (item.get("sources") or []):
            return int(m.group(1))
    for page in pages:
        m = re.match(r"\[(\d+)\]", page)
        if m and all(fact_found(f, page) for f in item.get("facts") or
                     ["\x00"]):
            return int(m.group(1))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run the docs benchmark.")
    ap.add_argument("--role", default="", help="council role to use "
                    "(default: the docs role, else its fallback)")
    ap.add_argument("--model", default="", help="model id, e.g. "
                    "ollama:llama3.1:8b (needs an engine that honours it)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--derive", choices=("model", "keywords"),
                    default="model")
    ap.add_argument("--mode", choices=("orchestrated", "tools"),
                    default="orchestrated")
    ap.add_argument("--oracle", action="store_true",
                    help="answer from the key: checks the harness only")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    bench = load_bench()
    if args.oracle:
        model_call = oracle_model_call(bench)
        label = "oracle"
    else:
        model_call = docs_qa.engine_model_call(role=args.role or None,
                                               model=args.model or None)
        label = args.model or ""
    report = run(model_call, items="quick" if args.quick else "all",
                 derive=args.derive, mode=args.mode, bench=bench,
                 model_label=label, progress=lambda s: print(s, flush=True))
    print("\n".join(report.lines()))
    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), indent=1),
                                  encoding="utf-8")
    return 0 if report.items else 1


if __name__ == "__main__":
    sys.exit(main())
