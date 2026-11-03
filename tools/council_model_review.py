"""tools/council_model_review.py — many model families and sizes on this PC,
ending in one review of what each is good and bad at.

    conda run --no-capture-output -n council python tools\\council_model_review.py
    python tools\\council_model_review.py --list        what would run
    python tools\\council_model_review.py --pull        fetch missing models first
    python tools\\council_model_review.py --models phi4:14b,llama3.1:8b
    python tools\\council_model_review.py --out DIR --report-only

Each model runs the same tests, smallest model first, one at a time so nothing
competes for the GPU:

  chat         the Council tab's own turn on tools/council_ab_questions.json
  kg           Connections: links found in 30 labelled sentences
  connections  Connections end to end on the synthetic Ironbridge vault
  build        Build tab, Edit mode: four small projects with failing tests
  llm          Designer layouts, small coding tasks, generation speed
  describe     Designer "Describe it"
  codebehind   Designer "Write it with the model…"
  docs         Docs tab: answers from an invented package's docs

The Ledger is left out: it does not use the model, so it says nothing about
one (and it drives Excel, which a long unattended run should not depend on).
GPU, CPU and memory are sampled throughout. Nothing touches your vault or data;
every step uses synthetic files in the output folder.

Run it again with the same --out to carry on: finished models are not run
again (--redo NAME,NAME to run some again). The review is rewritten after every
model, so it is worth reading before the run ends.

    <out>/review.md, review.html    the review
    <out>/<model>/                  that model's results and logs
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PY = sys.executable

# (family, maker, based in, tag), smallest first within a family.
MATRIX = [
    ("Gemma", "Google", "US", "gemma3:1b"),
    ("Gemma", "Google", "US", "gemma3:4b"),
    ("Gemma", "Google", "US", "gemma3:12b"),
    ("Gemma", "Google", "US", "gemma4:26b-a4b-it-qat"),
    ("Llama", "Meta", "US", "llama3.2:1b"),
    ("Llama", "Meta", "US", "llama3.2:latest"),
    ("Llama", "Meta", "US", "llama3.1:8b"),
    ("Granite", "IBM", "US", "granite3.3:2b"),
    ("Granite", "IBM", "US", "granite3.3:8b"),
    ("Phi", "Microsoft", "US", "phi4-mini:latest"),
    ("Phi", "Microsoft", "US", "phi4:14b"),
    ("OLMo", "Allen Institute for AI", "US", "olmo2:13b"),
    ("gpt-oss", "OpenAI", "US", "gpt-oss:20b"),
    ("Mistral", "Mistral AI", "France", "mistral:7b"),
    ("Qwen", "Alibaba", "China", "qwen2.5:14b-instruct-q4_K_M"),
]

# The seven jobs the review scores a model on.
AREAS = [
    ("chat", "Council chat"),
    ("connections", "Finding links (Connections)"),
    ("build", "Fixing code (Build tab)"),
    ("coding", "Small coding tasks"),
    ("docs", "Answering from docs (Docs tab)"),
    ("layouts", "GUI layouts (Designer)"),
    ("writer", "Writing GUI code (Designer)"),
]
KINDS = {"code": "code questions", "math": "maths", "fact": "facts",
         "data": "questions about your data", "plan": "planning",
         "explain": "explanations", "tutorial": "tutorials"}
GPU_QUERY = "utilization.gpu,memory.used,power.draw"
SPLIT = re.compile(r"\d+%/\d+% CPU/GPU|100% GPU|100% CPU")


def folder_name(tag: str) -> str:
    return re.sub(r"[^\w.-]", "_", tag)


def describe_model(tag: str) -> Dict[str, str]:
    for family, maker, based, name in MATRIX:
        if name == tag:
            return {"tag": tag, "family": family, "maker": maker,
                    "based": based}
    return {"tag": tag, "family": "Other", "maker": "?", "based": "?"}


# ============================================================
# Ollama and usage sampling
# ============================================================

def installed_models() -> Optional[Dict[str, float]]:
    """{tag: size in GB}, or None when Ollama is not answering."""
    try:
        p = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode:
        return None
    return parse_ollama_list(p.stdout)


def parse_ollama_list(text: str) -> Dict[str, float]:
    got = {}
    for line in text.splitlines()[1:]:
        m = re.match(r"(\S+)\s+\S+\s+([\d.]+)\s*(MB|GB)", line)
        if m:
            got[m.group(1)] = float(m.group(2)) / (
                1000 if m.group(3) == "MB" else 1)
    return got


def stop_model(tag: str) -> None:
    try:
        subprocess.run(["ollama", "stop", tag], capture_output=True,
                       timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def read_gpu() -> List[float]:
    p = subprocess.run(["nvidia-smi", f"--query-gpu={GPU_QUERY}",
                        "--format=csv,noheader,nounits"], capture_output=True,
                       text=True, timeout=10)
    return [float(x) for x in p.stdout.splitlines()[0].split(",")]


class Sampler:
    """Appends one row every couple of seconds to usage.csv, and remembers how
    Ollama split the model between CPU and GPU, until stopped."""

    def __init__(self, folder: Path, step: str, every: float = 2.0):
        self.path = folder / "usage.csv"
        self.splits_path = folder / "splits.json"
        self.step, self.every = step, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "Sampler":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        try:
            import psutil
            psutil.cpu_percent(None)
        except ImportError:
            psutil = None
        new = not self.path.exists()
        n = 0
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["step", "seconds", "gpu_pct", "vram_mib", "gpu_w",
                            "cpu_pct", "ram_gb"])
            last = time.monotonic()
            while not self._stop.wait(self.every):
                now = time.monotonic()
                try:
                    gpu = read_gpu()
                except (OSError, IndexError, ValueError,
                        subprocess.TimeoutExpired):
                    gpu = ["", "", ""]
                w.writerow([self.step, round(now - last, 2), *gpu,
                            psutil.cpu_percent(None) if psutil else "",
                            round(psutil.virtual_memory().used / 2**30, 2)
                            if psutil else ""])
                fh.flush()
                last = now
                n += 1
                if n % 10 == 0:
                    self._note_split()

    def _note_split(self) -> None:
        try:
            out = subprocess.run(["ollama", "ps"], capture_output=True,
                                 text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        seen = set(read_json(self.splits_path) or [])
        seen |= set(SPLIT.findall(out))
        self.splits_path.write_text(json.dumps(sorted(seen)),
                                    encoding="utf-8")


def measure_idle(out: Path, seconds: float = 30) -> None:
    folder = out / "_idle"
    if (folder / "usage.csv").exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    with Sampler(folder, "idle"):
        time.sleep(seconds)


# ============================================================
# Running one model
# ============================================================

def run_logged(cmd: List[str], log: Path, limit: float) -> int:
    with log.open("a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        try:
            return subprocess.run(cmd, cwd=REPO, stdout=fh, timeout=limit,
                                  stderr=subprocess.STDOUT).returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\nstopped after {limit / 60:.0f} min\n")
            return -1


def run_model(tag: str, out: Path, repeat: int) -> bool:
    folder = out / folder_name(tag)
    folder.mkdir(parents=True, exist_ok=True)
    model = f"ollama:{tag}"
    with Sampler(folder, "chat"):
        ok = run_logged(
            [PY, "-u", str(HERE / "council_ab.py"), "--model", model,
             "--tests", "chat,kg", "--sides", "after:app", "--repeat",
             str(repeat), "--out", str(folder / "ab")],
            folder / "chat.log", 8 * 3600) == 0
    with Sampler(folder, "benches"):
        # A bench exits 1 when the model fails some of its cases; that is a
        # result, not a crash, so only a run that could not finish counts.
        code = run_logged(
            [PY, "-u", str(HERE / "council_fulltest.py"), "--model", model,
             "--only", "env,connections,build,llm,describe,codebehind,docs",
             "--out", str(folder)],
            folder / "benches.log", 16 * 3600)
    stop_model(tag)
    done = ok and code == 0
    if done:
        (folder / "done.json").write_text(json.dumps(
            {"when": time.strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")
    return done


# ============================================================
# Reading the results
# ============================================================

def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for raw in lines:
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
    return rows


def read_usage(path: Path) -> Dict[str, Any]:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return {}

    def col(key: str, only: str = "") -> List[float]:
        return [float(r[key]) for r in rows
                if r.get(key) not in ("", None)
                and (not only or r["step"] == only)]

    def avg(v: List[float]) -> Optional[float]:
        return round(sum(v) / len(v), 1) if v else None

    def watt_hours(only: str) -> Optional[float]:
        got = [(float(r["gpu_w"]), float(r["seconds"])) for r in rows
               if r.get("gpu_w") and r["step"] == only]
        return round(sum(w * s for w, s in got) / 3600, 1) if got else None

    return {"gpu_pct": avg(col("gpu_pct")), "gpu_w": avg(col("gpu_w")),
            "cpu_pct": avg(col("cpu_pct")),
            "cpu_peak": max(col("cpu_pct"), default=None),
            "vram_gb": round(max(col("vram_mib"), default=0) / 1024, 1) or None,
            "ram_gb": max(col("ram_gb"), default=None),
            "chat_wh": watt_hours("chat")}


def graded(rows: List[Dict[str, Any]], good: Sequence[str]) -> Optional[float]:
    return (sum(r.get("grade") in good for r in rows) / len(rows)
            if rows else None)


def bench_summary(llm: Dict[str, Any], suite: str) -> Dict[str, Any]:
    """llm_bench keeps one summary per pass in a list; a run here has one."""
    got = (llm.get(suite) or {}).get("summary") or {}
    if isinstance(got, list):
        got = got[-1] if got else {}
    return got


def collect(folder: Path, tag: str) -> Dict[str, Any]:
    """Everything the review says about a model; a test that did not run is
    None, never zero."""
    r: Dict[str, Any] = dict(describe_model(tag), scores={}, kinds={})
    s = r["scores"]
    chat = read_jsonl(folder / "ab" / "chat-after-app.jsonl")
    if chat:
        right = sum(bool(q.get("correct")) for q in chat)
        walls = [q.get("wall_s") or 0 for q in chat]
        s["chat"] = right / len(chat)
        r["chat"] = {
            "right": right, "total": len(chat),
            "seconds": sum(walls), "median_s": statistics.median(walls),
            "per_right": sum(walls) / right if right else None,
            "errors": sum(bool(q.get("error")) for q in chat),
            "blank": sum(not q.get("error")
                         and not (q.get("answer") or "").strip()
                         for q in chat),
            "wrong": sorted({q["id"] for q in chat if not q.get("correct")})}
        for q in chat:
            k = r["kinds"].setdefault(q.get("kind", "?"), [0, 0])
            k[0] += bool(q.get("correct"))
            k[1] += 1
    kg = read_json(folder / "ab" / "kg-after.json")
    if isinstance(kg, dict) and kg.get("f1") is not None:
        s["connections"] = kg["f1"]
        r["kg"] = kg
    conn = read_json(folder / "connections.json")
    if isinstance(conn, dict) and conn.get("found_after_model"):
        r["links"] = conn
    build = read_json(folder / "build" / "build_bench.json")
    if isinstance(build, list) and build:
        s["build"] = sum(bool(b.get("solved")) for b in build) / len(build)
        r["build"] = build
    llm = read_json(folder / "llm_bench.json")
    if isinstance(llm, dict):
        code = bench_summary(llm, "code")
        if code.get("graded"):
            s["coding"] = code.get("passed", 0) / code["graded"]
        r["tok_s"] = bench_summary(llm, "gui").get("gen_tok_s")
    docs = (read_json(folder / "docs_bench.json") or {})
    docs = docs.get("summary") if isinstance(docs, dict) else None
    if docs and docs.get("total"):
        s["docs"] = docs.get("passed", 0) / docs["total"]
    layouts = read_jsonl(folder / "describe.jsonl")
    if layouts:
        s["layouts"] = graded(layouts, ("PASS", "SAFE-REFUSED"))
        r["layouts"] = layouts
    writer = read_jsonl(folder / "codebehind.jsonl")
    if writer:
        s["writer"] = graded(writer, ("PASS",))
        r["writer"] = writer
    r["usage"] = read_usage(folder / "usage.csv")
    r["splits"] = read_json(folder / "splits.json") or []
    r["spill"] = next((x for x in r["splits"] if "/" in x), "")
    r["done"] = (folder / "done.json").exists()
    have = [s[k] for k, _ in AREAS if s.get(k) is not None]
    r["overall"] = sum(have) / len(have) if have else None
    r["missing"] = [t for k, t in AREAS if s.get(k) is None]
    return r


# ============================================================
# The review
# ============================================================

def pct(x: Optional[float]) -> str:
    return "not run" if x is None else f"{x * 100:.0f}%"


def judge(r: Dict[str, Any], rows: List[Dict[str, Any]]
          ) -> Dict[str, List[str]]:
    """Strengths and weaknesses from the scores: near the best of those tested
    and high in itself is a strength, half or less is a weakness."""
    good: List[str] = []
    bad: List[str] = []
    for key, title in AREAS:
        x = r["scores"].get(key)
        if x is None:
            continue
        best = max(o["scores"].get(key) or 0 for o in rows)
        if x >= 0.8 and x >= best - 0.1:
            good.append(f"{title}: {pct(x)}"
                        + (" (the best tested)" if x == best else ""))
        elif x <= 0.5:
            bad.append(f"{title}: {pct(x)}")
    for kind, (right, total) in sorted(r["kinds"].items()):
        if kind not in KINDS or total < 4:
            continue
        if right == total:
            good.append(f"Chat: all {total} {KINDS[kind]} right")
        elif right / total <= 0.5:
            bad.append(f"Chat: {right} of {total} {KINDS[kind]} right")
    chat = r.get("chat") or {}
    speeds = sorted(o["chat"]["per_right"] for o in rows
                    if (o.get("chat") or {}).get("per_right"))
    mine = chat.get("per_right")
    if mine and len(speeds) >= 3:
        if mine <= speeds[len(speeds) // 3]:
            good.append(f"Fast: {mine:.0f} s per right chat answer")
        elif mine >= 2 * statistics.median(speeds):
            bad.append(f"Slow: {mine:.0f} s per right chat answer, against "
                       f"a median of {statistics.median(speeds):.0f} s")
    if r["spill"]:
        bad.append(f"Does not fit on the GPU: Ollama ran it {r['spill']}, "
                   "which slows it and loads the CPU")
    vram = (r.get("usage") or {}).get("vram_gb")
    if vram and vram <= 6 and not r["spill"]:
        good.append(f"Light: {vram} GB of GPU memory at peak, including what "
                    "the PC was already using")
    if chat.get("errors"):
        bad.append(f"Unreliable: {chat['errors']} chat turns failed with an "
                   "error")
    if chat.get("blank"):
        bad.append(f"{chat['blank']} chat answers came back blank, with no "
                   "error")
    for row in r.get("layouts") or []:
        if row.get("tier") == "adversarial" and row.get("grade") == "FAIL":
            bad.append(f"Designer safety test failed: {row.get('name')}")
    for row in r.get("writer") or []:
        if row.get("tier") == "adversarial" and row.get("grade") == "FAIL":
            bad.append("Code writer safety test failed: "
                       + str(row.get("why", ""))[:100])
    return {"good": good, "bad": bad}


def best_row(rows: List[Dict[str, Any]], key: str
             ) -> List[Dict[str, Any]]:
    have = [r for r in rows if r["scores"].get(key) is not None]
    if not have:
        return []
    top = max(r["scores"][key] for r in have)
    return [r for r in have if r["scores"][key] == top]


def review(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    rows = [r for r in rows if r["scores"]]
    md = [f"# Council model review — {meta.get('when', '')}", "",
          f"Build: {meta.get('build', '?')}. Questions asked "
          f"{meta.get('repeat', '?')} times each. Models tested: {len(rows)}"
          f" of {meta.get('planned', len(rows))}.", ""]
    if not rows:
        return "\n".join(md + ["No results yet.", ""])
    ranked = sorted((r for r in rows if r["overall"] is not None),
                    key=lambda r: -r["overall"])
    md += ["## Summary", "",
           f"Highest average score: **{ranked[0]['tag']}** "
           f"({pct(ranked[0]['overall'])}). The average counts the seven "
           "jobs below equally, so read the job you care about rather than "
           "the average alone.", ""]
    md += ["## Scores", "",
           "| Model | Family | Based in | Size | Average | "
           + " | ".join(t for _, t in AREAS) + " |",
           "|" + "---|" * (5 + len(AREAS))]
    for r in ranked:
        md.append(f"| {r['tag']} | {r['family']} | {r['based']} | "
                  f"{meta.get('sizes', {}).get(r['tag'], '?')} GB | "
                  f"{pct(r['overall'])} | "
                  + " | ".join(pct(r["scores"].get(k)) for k, _ in AREAS)
                  + " |")
    md += ["", "## Best for each job", "", "| Job | Best tested | Score |",
           "|---|---|---|"]
    for key, title in AREAS:
        top = best_row(rows, key)
        if top:
            md.append(f"| {title} | {', '.join(r['tag'] for r in top)} | "
                      f"{pct(top[0]['scores'][key])} |")
    md += ["", "## Speed and hardware", "",
           "| Model | Right in chat | Chat time | Seconds per right answer | "
           "Tokens/s | GPU avg | GPU power | Peak GPU memory | CPU avg | "
           "Runs on | GPU Wh per right answer |", "|" + "---|" * 11]
    for r in ranked:
        c, u = r.get("chat") or {}, r.get("usage") or {}
        per = c.get("per_right")
        wh = (u.get("chat_wh") / c["right"]
              if u.get("chat_wh") and c.get("right") else None)
        md.append(
            f"| {r['tag']} | "
            + (f"{c['right']}/{c['total']}" if c else "not run") + " | "
            + (f"{c['seconds'] / 60:.0f} min" if c else "-") + " | "
            + (f"{per:.0f}" if per else "-") + " | "
            + (f"{r['tok_s']:.0f}" if r.get("tok_s") else "-") + " | "
            + (f"{u['gpu_pct']:.0f}%" if u.get("gpu_pct") is not None
               else "-") + " | "
            + (f"{u['gpu_w']:.0f} W" if u.get("gpu_w") else "-") + " | "
            + (f"{u['vram_gb']} GB" if u.get("vram_gb") else "-") + " | "
            + (f"{u['cpu_pct']:.0f}%" if u.get("cpu_pct") is not None
               else "-") + " | "
            + (", ".join(r["splits"]) or "-") + " | "
            + (f"{wh:.1f}" if wh else "-") + " |")
    families: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        families.setdefault(r["family"], []).append(r)
    sizes = meta.get("sizes") or {}
    for members in families.values():
        members.sort(key=lambda m: sizes.get(m["tag"]) or float("inf"))
    md += ["", "## Model by model", ""]
    for family, members in families.items():
        md += [f"### {family} ({members[0]['maker']}, "
               f"{members[0]['based']})", ""]
        if len(members) > 1:
            md += ["By size: " + ", then ".join(
                f"{m['tag']} {pct(m['overall'])}" for m in members) + ".", ""]
        for r in members:
            v = judge(r, rows)
            md += [f"**{r['tag']}**"
                   + ("" if r["done"] else " (run not finished)"), ""]
            md += [f"- Strength: {x}" for x in v["good"]] or [
                "- No clear strengths in these tests"]
            md += [f"- Weakness: {x}" for x in v["bad"]] or [
                "- No clear weaknesses in these tests"]
            if (r.get("chat") or {}).get("wrong"):
                md.append("- Chat questions it got wrong at least once: "
                          + ", ".join(r["chat"]["wrong"]))
            unsolved = [b["id"] for b in r.get("build") or []
                        if not b.get("solved")]
            if unsolved:
                md.append("- Build projects it did not fix: "
                          + ", ".join(unsolved))
            if r["missing"]:
                md.append("- Not measured: " + ", ".join(r["missing"]))
            md.append("")
    if meta.get("skipped"):
        md += ["## Not tested", ""]
        md += [f"- {tag}: {why}" for tag, why in meta["skipped"].items()]
        md.append("")
    md += ["## How far to trust this", "",
           "- Scores are automatic checks on short, synthetic tests. A few "
           "points between two models can be noise: the same model's chat "
           "score has moved by about 3 of 48 between runs.",
           "- Designer and Docs results have barely changed between past runs, "
           "so running them again adds little. Chat gains most from more "
           "repeats.",
           "- Build is four small projects, so each one is 25 points.",
           "- Power is the GPU's own reading; the CPU and the rest of the PC "
           "are not included. Peak GPU memory includes what the PC was "
           "already using"
           + (f" (about {meta['idle_vram']} GB at idle)."
              if meta.get("idle_vram") else "."),
           "- Every role ran on the one model. Mixing models by role, as the "
           "app can, was not tested.", ""]
    return "\n".join(md)


def to_html(text: str) -> str:
    """Just the Markdown review() writes: headings, tables, bullets, bold."""
    def inline(s: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s))

    out = ["<!doctype html><meta charset='utf-8'><title>Council model review"
           "</title><style>body{font:14px/1.45 system-ui,sans-serif;margin:"
           "24px;max-width:1200px;color:#1d2330;background:#fff}table{border-"
           "collapse:collapse}td,th{border:1px solid #d5d9e0;padding:4px 8px;"
           "text-align:left}th{background:#f1f3f6}</style>"]
    table, bullets = False, False
    for line in text.splitlines():
        if line.startswith("|"):
            if set(line) <= set("|-"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            tag = "td" if table else "th"
            if not table:
                out.append("<table>")
                table = True
            out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>"
                                        for c in cells) + "</tr>")
            continue
        if table:
            out.append("</table>")
            table = False
        if line.startswith("- "):
            if not bullets:
                out.append("<ul>")
                bullets = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if bullets:
            out.append("</ul>")
            bullets = False
        m = re.match(r"(#+) (.*)", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if table:
        out.append("</table>")
    if bullets:
        out.append("</ul>")
    return "\n".join(out)


def write_review(out: Path, tags: Sequence[str], meta: Dict[str, Any]) -> str:
    rows = [collect(out / folder_name(t), t) for t in tags
            if (out / folder_name(t)).is_dir()]
    idle = read_usage(out / "_idle" / "usage.csv")
    meta = dict(meta, idle_vram=idle.get("vram_gb"))
    text = review(rows, meta)
    (out / "review.md").write_text(text, encoding="utf-8")
    (out / "review.html").write_text(to_html(text), encoding="utf-8")
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="council_model_review",
                                 description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--models", default="",
                    help="Ollama tags, comma separated (default: the list "
                         "of families and sizes in this file)")
    ap.add_argument("--out", default="")
    ap.add_argument("--repeat", type=int, default=2,
                    help="ask the chat questions this many times")
    ap.add_argument("--pull", action="store_true",
                    help="download models that are not installed")
    ap.add_argument("--redo", default="",
                    help="run these models again though they finished")
    ap.add_argument("--list", action="store_true",
                    help="say what would run, and stop")
    ap.add_argument("--report-only", action="store_true",
                    help="rewrite the review from what is in --out")
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else Path.home() / "council_ab" / "model-review"
    tags = ([t.strip() for t in a.models.split(",") if t.strip()]
            or [m[3] for m in MATRIX])
    have = installed_models()
    if have is None and not a.report_only:
        print("Ollama is not answering: start it, then run this again.")
        return 2
    have = have or {}
    # Smallest first, so the quick models report early; anything still to be
    # downloaded goes last.
    if not a.models:
        tags.sort(key=lambda t: have.get(t, float("inf")))
    if a.list:
        for tag in tags:
            d = describe_model(tag)
            state = (f"{have[tag]:g} GB" if tag in have else
                     "not installed (--pull fetches it)")
            done = (out / folder_name(tag) / "done.json").exists()
            print(f"{d['family']:10} {tag:26} {state}"
                  + ("  finished" if done else ""))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / "meta.json"
    meta = read_json(meta_path) or {}
    sha = subprocess.run(["git", "-C", str(REPO), "log", "-1",
                          "--format=%h %s"], capture_output=True, text=True
                         ).stdout.strip()
    meta.update(when=time.strftime("%Y-%m-%d %H:%M"), build=sha,
                repeat=a.repeat, planned=len(tags),
                sizes=dict(meta.get("sizes") or {},
                           **{t: have[t] for t in tags if t in have}))
    meta.setdefault("skipped", {})
    if a.report_only:
        print(write_review(out, tags, meta))
        return 0

    for tag in tags:
        stop_model(tag)
    measure_idle(out)
    redo = {t for t in a.redo.split(",") if t}
    for i, tag in enumerate(tags, 1):
        folder = out / folder_name(tag)
        if (folder / "done.json").exists() and tag not in redo:
            print(f"[{i}/{len(tags)}] {tag}: finished before", flush=True)
            continue
        if tag not in have:
            if not a.pull:
                meta["skipped"][tag] = ("not installed; run with --pull to "
                                        "fetch it")
                print(f"[{i}/{len(tags)}] {tag}: not installed, skipped",
                      flush=True)
                continue
            print(f"[{i}/{len(tags)}] {tag}: downloading", flush=True)
            if subprocess.call(["ollama", "pull", tag]):
                meta["skipped"][tag] = "could not be downloaded"
                continue
            meta["sizes"][tag] = (installed_models() or {}).get(tag, "?")
        if tag in redo:
            (folder / "done.json").unlink(missing_ok=True)
        meta["skipped"].pop(tag, None)
        print(f"[{i}/{len(tags)}] {tag}: running ({time.strftime('%H:%M')})",
              flush=True)
        t0 = time.monotonic()
        finished = run_model(tag, out, a.repeat)
        print(f"   {'finished' if finished else 'did not finish; see its logs'}"
              f", {(time.monotonic() - t0) / 60:.0f} min", flush=True)
        if not finished:
            meta["skipped"][tag] = (f"the run did not finish; see "
                                    f"{folder_name(tag)}/*.log")
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        write_review(out, tags, meta)
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    write_review(out, tags, meta)
    print(f"Review: {out / 'review.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
