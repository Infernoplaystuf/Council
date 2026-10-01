"""
council_core.pc_check — "Check this PC": which installed model is good for
what, and how fast, on THIS machine.

The Models tab's button (model_jobs.check_this_pc -> llm_bench.check_this_pc
-> check_this_pc here). For each installed US-made model (or the ids given):

  1. SPEED through the app's own path (council_engine.local_chat on the
     localhost Ollama): one cold call that loads the model — its time is
     reported as "first answer after switching", never mixed into the
     speeds — then two warm calls (~1.2k prompt tokens, up to 192 reply
     tokens, fresh prompts so nothing is served from Ollama's prompt cache):
     reply tok/s and prompt tok/s.
  2. PLACEMENT from Ollama's /api/ps: size_vram against size -> "GPU",
     "part GPU" (with the share in VRAM) or "CPU".
  3. A QUICK RELIABILITY PROBE through the real pipelines (llm_bench
     --pipeline new): one simple GUI (S2, Describe it + Generate + an
     offscreen run of the app), one function-mode code case (K1, the
     code-behind writer + its hidden test), two docs questions (q01, q04,
     through docs_qa and the bundled MCP documentation server). One case
     each is a smoke test of the model, not a pass RATE — the full
     benchmark (python -m council_core.llm_bench --model ...) is that.
  4. ESTIMATES: seconds of model time per GUI, per function and per docs
     answer = calls x (prompt tokens / prompt tok/s + reply tokens / reply
     tok/s), with the call and token counts the probe actually used (a
     typical figure when the probe made none), and a PREDICTED reply speed
     from the hardware alone (memory bandwidth / bytes read per token), so a
     measured speed far below it is visible.

Each model is checked in its own child process (python -m
council_core.llm_bench --check, through child_proc: a Job Object, killed
whole on Stop), with its own scratch vault — so the app's process, its
engine caches and the user's model_slots.json are never touched. The model
is unloaded (keep_alive 0) after each, and everything resident is unloaded
first, so each placement is measured on an empty card.

Writes <vault>/model_bench.json and ranks the models per role — US-made
only; a non-US model named explicitly is measured and shown, never ranked.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

APP_ROOT = Path(__file__).resolve().parent.parent
REPORT_NAME = "model_bench.json"
#: One model's whole check (cold load + speed + the quick probe). A 14B
#: model half on the CPU took ~90 s per GUI call in the baseline.
CHILD_TIMEOUT_S = 1800

#: Typical model work per task, from the baseline runs on this PC (prompt
#: and reply tokens per call, calls per task) — used only when the probe
#: made no call of that kind (it failed before the model was asked).
TYPICAL = {"gui": {"calls": 1.5, "prompt": 1800, "reply": 550},
           "function": {"calls": 1.5, "prompt": 900, "reply": 250},
           "docs": {"calls": 2.0, "prompt": 1500, "reply": 200}}

#: Memory bandwidth (GB/s) of common NVIDIA GPUs, longest name first. A
#: laptop part shares a desktop name with less bandwidth, so "Laptop"
#: entries must match first.
GPU_BANDWIDTH_GBS: Tuple[Tuple[str, float], ...] = (
    ("rtx 4090 laptop", 576), ("rtx 4080 laptop", 432),
    ("rtx 4070 laptop", 256), ("rtx 4060 laptop", 256),
    ("rtx 4050 laptop", 192), ("rtx 3080 ti laptop", 512),
    ("rtx 3080 laptop", 448), ("rtx 3070 ti laptop", 448),
    ("rtx 3070 laptop", 448), ("rtx 3060 laptop", 336),
    ("rtx 3050 laptop", 192), ("rtx 4090", 1008), ("rtx 4080 super", 736),
    ("rtx 4080", 717), ("rtx 4070 ti super", 672), ("rtx 4070 ti", 504),
    ("rtx 4070 super", 504), ("rtx 4070", 504), ("rtx 4060 ti", 288),
    ("rtx 4060", 272), ("rtx 3090 ti", 1008), ("rtx 3090", 936),
    ("rtx 3080 ti", 912), ("rtx 3080", 760), ("rtx 3070 ti", 608),
    ("rtx 3070", 448), ("rtx 3060 ti", 448), ("rtx 3060", 360),
    ("rtx 3050", 224), ("rtx 2080 ti", 616), ("rtx 2080", 448),
    ("rtx 2070", 448), ("rtx 2060", 336))
#: Fraction of peak bandwidth decoding reaches, measured on this PC (RTX
#: 4070 Laptop 256 GB/s, DDR5-5600 dual channel 89.6 GB/s): GPU 0.79-0.93
#: (phi3.5 92.6 tok/s on 2.18 GB; qwen2.5 7B 43.6-51 tok/s on 4.68 GB, both
#: Ollama), CPU 0.42-0.54 (llama_cpp on the 8 P-cores). A rough model —
#: partial offload measured ~30 % below it (phi3.5 at 16/32 layers: 23.2).
GPU_EFFICIENCY, CPU_EFFICIENCY = 0.8, 0.5


def _noop(_s: str) -> None:
    pass


# ============================================================
# Which models
# ============================================================

def installed_models(models: Optional[Sequence[str]] = None
                     ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(entries to check, notes on what was left out). Default: every
    installed Ollama chat model of US origin. Ids given are checked whatever
    their origin (a non-US model is then measured, never ranked)."""
    from . import local_models
    try:
        listed = local_models.list_local_models(include_gguf=False)
    except Exception:                                     # noqa: BLE001
        listed = []
    skipped: List[str] = []
    if models:
        out = []
        for mid in models:
            hit = next((e for e in listed if local_models.is_ollama_id(mid)
                        and local_models._same_ollama_name(   # noqa: SLF001
                            e["name"], local_models.ollama_name(mid))), None)
            if hit is None and not local_models.is_ollama_id(mid):
                hit = local_models.gguf_entry(mid)
            if hit is None:
                name = local_models.ollama_name(mid)
                maker, origin = local_models.maker_and_origin(name)
                hit = {"id": mid, "name": name, "backend": "ollama"
                       if local_models.is_ollama_id(mid) else "gguf",
                       "maker": maker, "origin": origin, "size_bytes": 0,
                       "params_b": local_models.parse_params_b(name),
                       "capabilities": ["completion"]}
            out.append(dict(hit))
        return out, skipped
    out = []
    for e in listed:
        if e.get("backend") != "ollama":
            continue
        if e.get("origin") != "US":
            skipped.append(f"{e.get('name')} ({e.get('maker')}, "
                           f"{e.get('origin')}: not checked by default)")
            continue
        out.append(dict(e))
    return out, skipped


# ============================================================
# One model (runs in the child: llm_bench --check)
# ============================================================

def _brief(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The probe's result for one suite, compactly."""
    if not rows:
        return {"ran": 0}
    keys = ("id", "passed", "category", "detail", "seconds", "model_calls",
            "prompt_tokens", "output_tokens", "gen_tok_s", "mode",
            "repair_rounds", "citation_ok")
    graded = [r for r in rows if r.get("category") != "harness_error"]
    return {"ran": len(rows), "graded": len(graded),
            "passed": sum(1 for r in graded if r.get("passed")),
            "cases": [{k: r.get(k) for k in keys if k in r} for r in rows],
            "seconds": round(sum(float(r.get("seconds") or 0) for r in rows),
                             1)}


def estimate_seconds(speed: Dict[str, Any], rows: List[Dict[str, Any]],
                     kind: str) -> Optional[Dict[str, Any]]:
    """Seconds of MODEL time per task of ``kind`` from the measured speeds:
    calls x (prompt tokens / prompt tok/s + reply tokens / reply tok/s).
    Generate, smoke runs and tests add a second or two on top."""
    gen = speed.get("gen_tok_s")
    pp = speed.get("prompt_tok_s")
    if not gen:
        return None
    calls = sum(int(r.get("model_calls") or 0) for r in rows)
    if calls:
        per_task = calls / max(1, len(rows))
        p = sum(int(r.get("prompt_tokens") or 0) for r in rows) / calls
        g = sum(int(r.get("output_tokens") or 0) for r in rows) / calls
        basis = "this probe's calls"
    else:
        t = TYPICAL[kind]
        per_task, p, g = t["calls"], t["prompt"], t["reply"]
        basis = "typical token counts"
    secs = per_task * (g / float(gen) + (p / float(pp) if pp else 0.0))
    return {"seconds": round(secs, 1), "calls": round(per_task, 2),
            "prompt_tokens_per_call": int(p), "reply_tokens_per_call": int(g),
            "basis": basis}


def check_one(model_id: str, vault: Path, *,
              say: Callable[[str], None] = _noop,
              runtime_python: Optional[str] = None,
              should_stop: Optional[Callable[[], bool]] = None
              ) -> Dict[str, Any]:
    """Speed, placement, the quick probe and the estimates for ONE model.
    Puts every role on ``model_id`` in ``vault`` (a scratch vault) — run it
    in a process of its own (llm_bench --check)."""
    from . import bench_engine, llm_bench
    t0 = time.perf_counter()
    row: Dict[str, Any] = {"model": model_id,
                           "measured": time.strftime("%Y-%m-%d %H:%M:%S")}
    bench_engine.use_model(vault, model_id)
    sp = bench_engine.speed_probe(model_id, say=say, should_stop=should_stop)
    row["speed"] = sp
    for k in ("gen_tok_s", "prompt_tok_s", "cold_s", "load_s", "placement",
              "vram_pct", "size_mb", "vram_mb"):
        row[k] = sp.get(k)
    if sp.get("error"):
        row["error"] = f"speed probe: {sp['error']}"
        row["seconds"] = round(time.perf_counter() - t0, 1)
        return row
    say(f"{sp.get('gen_tok_s')} tok/s, {sp.get('placement')} — "
        f"quick probe (1 GUI, 1 function, 2 docs questions)…")
    tap = llm_bench.EngineTap()
    only = ",".join(llm_bench.QUICK_GUI + llm_bench.QUICK_CODE
                    + llm_bench.QUICK_DOCS)
    rep = llm_bench.run(("gui", "code", "docs"), tap, only=only,
                        pipeline="new", vault=vault, generate=True,
                        runtime_python=runtime_python, progress=say,
                        should_stop=should_stop)
    rows = {s: (rep.get(s) or {}).get("passes", [[]]) for s in
            ("gui", "code", "docs")}
    rows = {s: (p[0] if p else []) for s, p in rows.items()}
    row["gui"] = _brief(rows["gui"])
    row["code"] = _brief(rows["code"])
    row["docs"] = _brief(rows["docs"])
    ds = ((rep.get("docs") or {}).get("summary_docs") or [{}])
    row["docs"]["summary"] = ds[0] if ds else {}
    row["estimates"] = {
        "gui": estimate_seconds(sp, rows["gui"], "gui"),
        "function": estimate_seconds(sp, rows["code"], "function"),
        "docs_answer": estimate_seconds(
            sp, [r for r in rows["docs"] if r.get("tier") != "code"],
            "docs")}
    gui_prof = next((r.get("profile") for r in rows["gui"]
                     if r.get("profile")), None)
    if gui_prof:
        row["describe_profile"] = gui_prof
    if rep["meta"].get("interrupted"):
        row["error"] = f"probe {rep['meta']['interrupted']}"
    row["seconds"] = round(time.perf_counter() - t0, 1)
    return row


# ============================================================
# Hardware -> predicted speed
# ============================================================

def gpu_bandwidth(name: str) -> Optional[float]:
    low = " ".join(str(name or "").lower().split())
    for key, bw in GPU_BANDWIDTH_GBS:
        if key in low:
            return float(bw)
    return None


def ram_bandwidth(raw: Dict[str, Any]) -> Optional[float]:
    """GB/s from the DIMM speed, assuming DUAL channel (2 x 64-bit) — what
    a laptop or a two-stick desktop has."""
    mts = raw.get("ram_speed_mts")
    try:
        return round(float(mts) * 8 * 2 / 1000.0, 1) if mts else None
    except (TypeError, ValueError):
        return None


def _active_fraction(entry: Dict[str, Any]) -> float:
    """Share of the weights read per token: 1 for a dense model; for an MoE
    the catalog's active/total (gpt-oss-20b: 3.6 of 20.9), else 0.2."""
    from . import local_models
    if not local_models.is_moe(entry):
        return 1.0
    try:
        import model_catalog
        name = local_models.ollama_name(str(entry.get("id") or ""))
        for spec in model_catalog.MODELS:
            if spec.active_params_b and spec.ollama and \
                    local_models._same_ollama_name(  # noqa: SLF001
                        spec.ollama, name):
                return float(spec.active_params_b) / float(spec.params_b)
    except Exception:                                     # noqa: BLE001
        pass
    return 0.2


def predict_gen_tok_s(entry: Dict[str, Any], vram_frac: Optional[float],
                      hw_raw: Dict[str, Any]) -> Optional[float]:
    """Reply tokens/s predicted from memory bandwidth alone: each token reads
    the active weights once — the GPU's share at the card's bandwidth, the
    rest at the RAM's — at the efficiencies measured here. None when a
    bandwidth it needs is unknown."""
    size = float(entry.get("size_bytes") or 0)
    if not size or vram_frac is None:
        return None
    gb = size * _active_fraction(entry) / 1e9
    gpu_bw = gpu_bandwidth(hw_raw.get("gpu_name") or "")
    ram_bw = ram_bandwidth(hw_raw)
    t = 0.0
    if vram_frac > 0:
        if not gpu_bw:
            return None
        t += vram_frac * gb / (gpu_bw * GPU_EFFICIENCY)
    if vram_frac < 1:
        if not ram_bw:
            return None
        t += (1 - vram_frac) * gb / (ram_bw * CPU_EFFICIENCY)
    return round(1.0 / t, 1) if t > 0 else None


def _hardware() -> Dict[str, Any]:
    try:
        from . import model_jobs
        hw = model_jobs.detect_hardware()
        return {"summary": hw.summary, "gpu": hw.gpu, "vram_gb": hw.vram_gb,
                "ram_gb": hw.ram_gb, "raw": dict(hw.raw or {})}
    except Exception as exc:                              # noqa: BLE001
        return {"summary": f"hardware not detected ({exc!r})", "raw": {}}


# ============================================================
# Ranking
# ============================================================

def _est(row: Dict[str, Any], key: str) -> float:
    e = (row.get("estimates") or {}).get(key) or {}
    s = e.get("seconds")
    return float(s) if isinstance(s, (int, float)) else math.inf


def rank(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Per role, best first, US-made models only. Reliability first (the
    probe's passes), then the time estimate; "writer" (prose roles) by
    reply speed alone — the probe does not grade prose."""
    us = [r for r in rows if r.get("origin") == "US" and not r.get("error")]

    def passed(r: Dict[str, Any], suite: str) -> Tuple[int, int]:
        s = r.get(suite) or {}
        return int(s.get("passed") or 0), int(s.get("graded") or 0)

    def frac(r: Dict[str, Any], *suites: str) -> float:
        p = sum(passed(r, s)[0] for s in suites)
        n = sum(passed(r, s)[1] for s in suites)
        return p / n if n else 0.0

    coder = sorted(us, key=lambda r: (-frac(r, "gui", "code"),
                                      _est(r, "gui") + _est(r, "function")))
    docs = sorted(us, key=lambda r: (-frac(r, "docs"), _est(r, "docs_answer")))
    writer = sorted(us, key=lambda r: -(r.get("gen_tok_s") or 0))

    def entry(r: Dict[str, Any], why: str) -> Dict[str, Any]:
        return {"model": r["model"], "name": r.get("name") or r["model"],
                "why": why}

    def gui_why(r):
        g, c = passed(r, "gui"), passed(r, "code")
        return (f"GUI {g[0]}/{g[1]}, code {c[0]}/{c[1]}, ~"
                f"{_fmt_s(_est(r, 'gui'))} per GUI")

    def docs_why(r):
        d = passed(r, "docs")
        return f"docs {d[0]}/{d[1]}, ~{_fmt_s(_est(r, 'docs_answer'))} each"

    return {"coder": [entry(r, gui_why(r)) for r in coder],
            "docs": [entry(r, docs_why(r)) for r in docs],
            "writer": [entry(r, f"{r.get('gen_tok_s')} tok/s, "
                                f"{r.get('placement')}") for r in writer]}


def _fmt_s(s: float) -> str:
    if s is None or s == math.inf:
        return "? s"
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.1f} min"


def message(rows: List[Dict[str, Any]], ranking: Dict[str, Any],
            seconds: float, hw: Dict[str, Any], stopped: bool,
            skipped: Sequence[str]) -> str:
    lines = [f"{'Stopped after' if stopped else 'Checked'} {len(rows)} "
             f"model(s) in {_fmt_s(seconds)} — quick probe: 1 GUI, 1 "
             f"function, 2 docs questions each (the full benchmark gives "
             f"pass RATES)."]
    for r in rows:
        name = r.get("name") or r["model"]
        if r.get("error"):
            lines.append(f"• {name}: {r['error'][:160]}")
            continue
        g, c, d = (r.get("gui") or {}), (r.get("code") or {}), \
            (r.get("docs") or {})
        where = r.get("placement") or "?"
        if where == "part GPU" and r.get("vram_pct") is not None:
            where = f"{r['vram_pct']:.0f}% on GPU"
        pred = r.get("predicted_gen_tok_s")
        tag = "" if r.get("origin") == "US" else \
            f" [{r.get('origin', 'origin unknown')}: measured, not ranked]"
        lines.append(
            f"• {name}{tag}: {where}, {r.get('gen_tok_s')} tok/s"
            + (f" (hardware predicts ~{pred})" if pred else "")
            + f"; GUI {g.get('passed', 0)}/{g.get('graded', 0)}, code "
            f"{c.get('passed', 0)}/{c.get('graded', 0)}, docs "
            f"{d.get('passed', 0)}/{d.get('graded', 0)}; ~"
            f"{_fmt_s(_est(r, 'gui'))} per GUI, ~"
            f"{_fmt_s(_est(r, 'function'))} per function, ~"
            f"{_fmt_s(_est(r, 'docs_answer'))} per docs answer; first "
            f"answer after switching {r.get('cold_s')} s")
    best = []
    for role, label in (("coder", "GUIs and code"), ("docs", "docs"),
                        ("writer", "fastest replies")):
        top = (ranking.get(role) or [None])[0]
        if top:
            best.append(f"{label}: {top['name']}")
    if best:
        lines.append("Best — " + "; ".join(best) + ".")
    if skipped:
        lines.append("Not checked: " + "; ".join(skipped) + ".")
    return "\n".join(lines)


# ============================================================
# All models (the Models tab)
# ============================================================

def _run_child(model_id: str, say: Callable[[str], None],
               should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """check_one in a fresh process with its own scratch vault."""
    from . import child_proc
    tmp = Path(tempfile.mkdtemp(prefix="cpc_"))
    out, vault = tmp / "check.json", tmp / "v"
    argv = [sys.executable, "-m", "council_core.llm_bench", "--model",
            model_id, "--check", "--out", str(out), "--vault", str(vault),
            "--no-unload"]
    env = child_proc.child_env({
        "COUNCIL_VAULT_ROOT": str(vault), "COUNCIL_NO_DIALOGS": "1",
        "QT_QPA_PLATFORM": "offscreen", "PYTHONUNBUFFERED": "1"})

    def line(text: str) -> None:
        t = text.strip()
        if t and t[0] not in "{}[]\"" and not text.startswith((" ", "\t")):
            say(t[:200])
    try:
        r = child_proc.run(argv, cwd=str(APP_ROOT), env=env,
                           timeout=CHILD_TIMEOUT_S, should_stop=should_stop,
                           on_line=line, retries=1)
        row: Dict[str, Any] = {"model": model_id}
        if out.is_file():
            try:
                row = json.loads(out.read_text(encoding="utf-8"))["check"]
            except (ValueError, KeyError):
                row["error"] = "the check wrote an unreadable report"
        if r.stopped:
            row["stopped"] = True
            row.setdefault("error", "stopped")
        elif r.timed_out:
            row["error"] = f"timed out after {CHILD_TIMEOUT_S} s"
        elif r.infra:
            row["error"] = r.infra
        elif not out.is_file():
            tail = (r.stderr or r.stdout or r.error).strip().splitlines()
            row["error"] = ("the check did not finish: "
                            + (tail[-1] if tail else f"exit {r.returncode}"))
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _speed_only(model_id: str, say, should_stop) -> Dict[str, Any]:
    """A frozen build has no Python to start a check in: speed and
    placement only (both pure HTTP through the engine, no config touched)."""
    from . import bench_engine
    sp = bench_engine.speed_probe(model_id, say=say, should_stop=should_stop)
    row = {"model": model_id, "speed": sp,
           "note": "speed only — the reliability probe needs the Council's "
                   "Python (not available in this build)"}
    for k in ("gen_tok_s", "prompt_tok_s", "cold_s", "load_s", "placement",
              "vram_pct", "size_mb", "vram_mb"):
        row[k] = sp.get(k)
    if sp.get("error"):
        row["error"] = sp["error"]
    for kind, key in (("gui", "gui"), ("function", "function"),
                      ("docs", "docs_answer")):
        row.setdefault("estimates", {})[key] = estimate_seconds(sp, [], kind)
    return row


def check_this_pc(models: Optional[Sequence[str]] = None,
                  vault_dir: Any = None,
                  on_progress: Optional[Callable[[str], None]] = None,
                  should_stop: Optional[Callable[[], bool]] = None
                  ) -> Dict[str, Any]:
    """{ok, message, rows, report_path}. BLOCKING — run it on a worker."""
    from . import bench_engine
    say = on_progress or _noop
    stop = should_stop or (lambda: False)
    t0 = time.perf_counter()
    entries, skipped = installed_models(models)
    if not entries:
        from . import local_models
        host = local_models.ollama_host()
        if not local_models.ollama_reachable(host):
            return {"ok": False, "rows": [], "report_path": None,
                    "message": f"No Ollama server answers at {host} — start "
                               f"Ollama, then check again."}
        return {"ok": False, "rows": [], "report_path": None,
                "message": "No installed US-made model to check — install "
                           "one in Ollama (e.g. `ollama pull llama3.1:8b`); "
                           "the Council never downloads."
                           + (f" Not checked: {'; '.join(skipped)}."
                              if skipped else "")}
    say("Detecting the hardware…")
    hw = _hardware()
    say("Unloading resident models so each placement starts clean…")
    try:
        bench_engine.unload_all()
    except Exception:                                     # noqa: BLE001
        pass
    frozen = bool(getattr(sys, "frozen", False))
    rows: List[Dict[str, Any]] = []
    stopped = False
    for i, e in enumerate(entries, 1):
        if stop():
            stopped = True
            break
        tag = f"[{i}/{len(entries)}] {e.get('name') or e['id']}"
        say(f"{tag}: measuring…")

        def sub(text: str, tag: str = tag) -> None:
            say(f"{tag}: {text}")
        row = (_speed_only(e["id"], sub, stop) if frozen
               else _run_child(e["id"], sub, stop))
        for k in ("name", "maker", "origin", "params_b", "quant", "family",
                  "size_bytes"):
            row.setdefault(k, e.get(k))
        vram = row.get("vram_pct")
        row["predicted_gen_tok_s"] = predict_gen_tok_s(
            e, None if vram is None else float(vram) / 100.0,
            hw.get("raw") or {})
        rows.append(row)
        try:
            bench_engine.unload(e["id"])
        except Exception:                                 # noqa: BLE001
            pass
        if row.get("stopped"):
            stopped = True
            break
    ranking = rank(rows)
    seconds = time.perf_counter() - t0
    report = {"measured": time.strftime("%Y-%m-%d %H:%M:%S"),
              "seconds": round(seconds, 1), "stopped": stopped,
              "hardware": {k: v for k, v in hw.items() if k != "raw"},
              "hardware_raw": hw.get("raw") or {},
              "probe": {"gui": list(_quick("QUICK_GUI")),
                        "code": list(_quick("QUICK_CODE")),
                        "docs": list(_quick("QUICK_DOCS"))},
              "estimates_note": "model time only (calls x tokens / measured "
                                "speed); Generate, smoke runs and tests add "
                                "a second or two",
              "rows": rows, "ranking": ranking, "not_checked": skipped}
    path: Optional[Path] = None
    try:
        from . import paths
        base = Path(vault_dir) if vault_dir else paths.vault_dir()
        base.mkdir(parents=True, exist_ok=True)
        path = base / REPORT_NAME
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(report, indent=1, default=str),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:                              # noqa: BLE001
        skipped = list(skipped) + [f"report not saved: {exc!r}"]
        path = None
    text = message(rows, ranking, seconds, hw, stopped, skipped)
    if path is not None:
        text += f"\nSaved to {path}."
    ok = bool(rows) and any(not r.get("error") for r in rows)
    return {"ok": ok, "message": text, "rows": rows, "report_path": path}


def _quick(name: str) -> Sequence[str]:
    from . import llm_bench
    return getattr(llm_bench, name)


def load_report(vault_dir: Any = None) -> Optional[Dict[str, Any]]:
    """The last check's report, or None."""
    try:
        from . import paths
        base = Path(vault_dir) if vault_dir else paths.vault_dir()
        return json.loads((base / REPORT_NAME).read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return None
