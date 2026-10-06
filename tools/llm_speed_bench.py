#!/usr/bin/env python
"""
tools/llm_speed_bench.py — how fast does a local model run on THIS machine?

    # one GGUF, three placements, two repeats each, fresh process per run
    python tools/llm_speed_bench.py llama --gguf C:/m/phi.gguf --label phi3.5 \
        --gpu-layers 99,16,0 --threads 8 --repeats 2 --out speed.jsonl

    # the same model through a running Ollama server (localhost only)
    python tools/llm_speed_bench.py ollama --model phi3.5:latest \
        --tokenizer-gguf C:/m/phi.gguf --repeats 2 --out speed.jsonl

    # a table of medians from everything measured so far
    python tools/llm_speed_bench.py table speed.jsonl

WHAT ONE RUN MEASURES
---------------------
  load_s       constructing the model (llama_cpp) / Ollama's load_duration
  pp tok/s     prompt processing at ~2k and ~6k prompt tokens. Each prompt is
               fresh pseudo-random text (a new seed per run), so no prefix of
               it is in any KV cache; llama_cpp evaluates it after reset().
  gen tok/s    decoding ~400 tokens after a short prompt, timed from the first
               token to the last (llama_cpp) / eval_count / eval_duration
               (Ollama). End-of-generation tokens are banned on llama_cpp so
               every run decodes the same count.
  vram_mb      peak device memory used during the run minus what was in use
               before it started (nvidia-smi, sampled every 250 ms)
  ram_mb       peak working set of the process (llama_cpp) / of Ollama's
               runner process (Ollama)

EVERY llama_cpp RUN IS ITS OWN PROCESS. Load time is only meaningful from a
cold start, and a second model loaded into the same process shares CUDA
state with the first.

CPU RUNS AND WINDOWS' E-CORES
-----------------------------
On a hybrid Intel CPU Windows moves a process it considers "background" (no
foreground window — exactly what a benchmark launched from a terminal tool,
or the Council's inference thread while the user looks at another window, is)
onto the efficiency cores after a few seconds (EcoQoS). --qos high opts this
process out (SetProcessInformation / ProcessPowerThrottling — a per-process
setting, nothing system-wide is changed). --affinity p pins it to the
performance cores, read from the OS, not assumed.

"CPU ONLY" ON A CUDA BUILD IS NOT CPU ONLY
--------------------------------------------
With the CUDA wheel, --gpu-layers 0 keeps every weight in system RAM but
llama.cpp still ships large prompt batches to the GPU (op offload): measured
~1 GB of VRAM in use and prompt speeds far above what the cores can do.
Decoding (one token at a time) stays on the CPU. --hide-gpu sets
CUDA_VISIBLE_DEVICES=-1 for the run, which is what a machine without an
NVIDIA card really gets; those runs are labelled cpu-nogpu.

Stdlib only, plus llama_cpp for the `llama` mode. Never downloads anything;
Ollama must be on localhost.
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PP_SIZES = (2048, 6144)
GEN_TOKENS = 400
N_CTX = 8192
OLLAMA_HOST = "http://127.0.0.1:11434"

# A plain vocabulary for filler text. Random words, not a repeated sentence:
# a repeated sentence tokenizes into a handful of distinct tokens and some
# runtimes reuse matching prefixes.
_WORDS = (
    "time year people way day man thing woman life child world school state "
    "family student group country problem hand part place case week company "
    "system program question work government number night point home water "
    "room mother area money story fact month lot right study book eye job word "
    "business issue side kind head house service friend father power hour game "
    "line end member law car city community name president team minute idea "
    "kid body information back parent face others level office door health "
    "person art war history party result change morning reason research girl "
    "guy moment air teacher force education foot boy age policy process music "
    "market sense nation plan college interest death experience effect use "
    "class control care field development role effort rate heart drug show "
    "leader light voice wife police mind price report decision son view "
    "relationship town road arm difference value building action model season "
    "society tax director position player record paper space ground form event "
    "official matter center couple site project activity star table need court "
    "oil situation cost industry figure street image phone data picture "
    "practice piece land product doctor wall patient worker news test movie "
    "north love support technology step baby computer type attention film tree "
    "source organization hair window evidence population site camera frame "
    "laser powder layer melt pool sensor signal noise filter button label panel"
).split()


def filler_text(n_words: int, seed: int) -> str:
    rnd = random.Random(seed)
    out, line = [], []
    for i in range(n_words):
        line.append(rnd.choice(_WORDS))
        if len(line) >= rnd.randint(8, 16):
            out.append(" ".join(line).capitalize() + ".")
            line = []
    if line:
        out.append(" ".join(line).capitalize() + ".")
    return " ".join(out)


GEN_PROMPT = ("Write a long, detailed essay (at least 800 words) about the "
              "history of bridge engineering, from stone arches to modern "
              "cable-stayed bridges.")


# ============================================================
# Windows process helpers (ctypes; no psutil)
# ============================================================

def performance_cpus() -> List[int]:
    """Logical CPUs of the highest efficiency class (the P-cores on a hybrid
    Intel part). Every CPU when the OS cannot say or the CPU is not hybrid."""
    n = os.cpu_count() or 1
    if sys.platform != "win32":
        return list(range(n))
    try:
        import struct
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        size = wintypes.DWORD(0)
        k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(size))
        buf = (ctypes.c_byte * size.value)()
        if not k32.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(size)):
            return list(range(n))
        raw, off, cores = bytes(buf), 0, []
        while off < size.value:
            _rel, sz = struct.unpack_from("<II", raw, off)
            eff = raw[off + 9]
            mask = struct.unpack_from("<Q", raw, off + 32)[0]
            cores.append((eff, [i for i in range(64) if mask >> i & 1]))
            off += sz
        top = max(e for e, _ in cores)
        return sorted(c for e, cs in cores if e == top for c in cs)
    except Exception:
        return list(range(n))


def set_affinity(cpus: Sequence[int]) -> bool:
    if sys.platform != "win32" or not cpus:
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    mask = 0
    for c in cpus:
        mask |= 1 << int(c)
    return bool(k32.SetProcessAffinityMask(
        ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.c_size_t(mask)))


def set_high_qos() -> bool:
    """Opt THIS process out of EcoQoS (execution-speed power throttling)."""
    if sys.platform != "win32":
        return False

    class _PPTS(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                    ("StateMask", ctypes.c_ulong)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    state = _PPTS(1, 0x1, 0x0)       # EXECUTION_SPEED controlled, and OFF
    return bool(k32.SetProcessInformation(
        ctypes.c_void_p(k32.GetCurrentProcess()), 4,   # ProcessPowerThrottling
        ctypes.byref(state), ctypes.sizeof(state)))


def process_memory_mb(pid: Optional[int] = None) -> Dict[str, float]:
    """{peak_working_set_mb, working_set_mb, private_mb} for a process."""
    if sys.platform != "win32":
        return {}

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    if pid is None:
        h = k32.GetCurrentProcess()
    else:
        h = k32.OpenProcess(0x0400 | 0x0010, False, int(pid))
        if not h:
            return {}
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)
    ok = psapi.GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(pmc),
                                    pmc.cb)
    if pid is not None:
        k32.CloseHandle(ctypes.c_void_p(h))
    if not ok:
        return {}
    mb = 1024.0 * 1024.0
    return {"peak_working_set_mb": round(pmc.PeakWorkingSetSize / mb),
            "working_set_mb": round(pmc.WorkingSetSize / mb),
            "private_mb": round(pmc.PrivateUsage / mb),
            "peak_private_mb": round(pmc.PeakPagefileUsage / mb)}


# ============================================================
# VRAM sampling
# ============================================================

def vram_used_mb() -> Optional[int]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"], capture_output=True,
            text=True, timeout=15).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


class VramPeak:
    """nvidia-smi in loop mode; keeps the highest reading seen."""

    def __init__(self, interval_ms: int = 250):
        self.peak: Optional[int] = None
        self._proc = None
        self._interval = interval_ms

    def __enter__(self) -> "VramPeak":
        try:
            self._proc = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits", f"-lms={self._interval}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            self._proc = None
            return self
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                v = int(line.strip())
            except ValueError:
                continue
            self.peak = v if self.peak is None else max(self.peak, v)

    def __exit__(self, *exc) -> None:
        if self._proc is not None:
            time.sleep(self._interval / 1000.0 * 2)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()


# ============================================================
# llama_cpp — one run, in this process
# ============================================================

def _ban_eog(llm: Any):
    """A logits processor that makes end-of-generation impossible, so every
    run decodes exactly the asked-for number of tokens."""
    banned = {int(llm.token_eos())}
    for s in ("<|im_end|>", "<|endoftext|>", "<|end|>", "<|eot_id|>",
              "<|return|>", "<end_of_turn>"):
        try:
            toks = llm.tokenize(s.encode(), add_bos=False, special=True)
        except Exception:
            continue
        if len(toks) == 1:
            banned.add(int(toks[0]))
    banned = sorted(banned)

    def proc(_ids, scores):
        scores[banned] = -float("inf")
        return scores
    return proc, banned


def exact_tokens(llm: Any, n: int, seed: int) -> List[int]:
    text = filler_text(int(n * 0.9) + 50, seed)
    toks = llm.tokenize(text.encode("utf-8"), add_bos=True)
    while len(toks) < n:
        text += " " + filler_text(n // 2, seed + len(toks))
        toks = llm.tokenize(text.encode("utf-8"), add_bos=True)
    return list(toks[:n])


def run_llama(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Load, warm up, prompt-process each size, then decode. One process."""
    rec: Dict[str, Any] = dict(cfg)
    if cfg.get("affinity") == "p":
        rec["affinity_cpus"] = performance_cpus()
        rec["affinity_set"] = set_affinity(rec["affinity_cpus"])
    if cfg.get("qos") == "high":
        rec["qos_set"] = set_high_qos()
    base = vram_used_mb()
    rec["vram_before_mb"] = base
    seed = int(cfg.get("seed", 1))
    with VramPeak() as vp:
        t0 = time.perf_counter()
        from llama_cpp import Llama
        llm = Llama(model_path=cfg["gguf"], n_ctx=int(cfg.get("n_ctx", N_CTX)),
                    n_gpu_layers=int(cfg["gpu_layers"]),
                    n_threads=int(cfg["threads"]),
                    n_threads_batch=int(cfg.get("threads_batch")
                                        or cfg["threads"]),
                    flash_attn=bool(cfg.get("flash_attn", False)),
                    seed=seed, verbose=False)
        rec["load_s"] = round(time.perf_counter() - t0, 3)
        llm.reset()
        llm.eval(exact_tokens(llm, 64, seed + 999))       # warm-up, untimed
        for n in cfg.get("pp_sizes", PP_SIZES):
            toks = exact_tokens(llm, int(n), seed * 1000 + int(n))
            llm.reset()
            t = time.perf_counter()
            llm.eval(toks)
            dt = time.perf_counter() - t
            rec[f"pp{n}_s"] = round(dt, 3)
            rec[f"pp{n}_tps"] = round(len(toks) / dt, 1)
        gen_n = int(cfg.get("gen_tokens", GEN_TOKENS))
        proc, banned = _ban_eog(llm)
        from llama_cpp import LogitsProcessorList
        prompt = llm.tokenize(GEN_PROMPT.encode(), add_bos=True)
        llm.reset()
        stamps = []
        t = time.perf_counter()
        for i, _tok in enumerate(llm.generate(
                prompt, temp=0.7, top_k=40, top_p=0.95,
                logits_processor=LogitsProcessorList([proc]))):
            stamps.append(time.perf_counter())
            if i + 1 >= gen_n:
                break
        rec["gen_tokens"] = len(stamps)
        rec["gen_first_token_s"] = round(stamps[0] - t, 3) if stamps else None
        if len(stamps) > 1:
            rec["gen_tps"] = round((len(stamps) - 1) / (stamps[-1] - stamps[0]),
                                   1)
        rec["eog_banned"] = banned
    rec["vram_peak_mb"] = vp.peak
    if vp.peak is not None and base is not None:
        rec["vram_mb"] = vp.peak - base
    rec.update(process_memory_mb())
    rec["ram_mb"] = rec.get("peak_working_set_mb")
    try:
        rec["n_layers"] = int(llm.metadata.get(
            f"{llm.metadata.get('general.architecture')}.block_count", 0))
    except Exception:
        pass
    return rec


# ============================================================
# Ollama — one run against the server
# ============================================================

_LOOPBACK = re.compile(
    r"^https?://(localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\])(:\d+)?(/|$)",
    re.IGNORECASE)


def _is_loopback_url(url: str) -> bool:
    """council_core.local_models.is_loopback_url, copied: this tool runs as
    a script from tools/, where council_core is not importable. The host is
    matched WHOLE — the startswith test this replaces passed
    http://localhost.evil.example and http://localhost@evil.example (a user
    name; the host is evil.example), and _post sends whole prompts."""
    m = _LOOPBACK.match((url or "").strip())
    if m is None:
        return False
    host = m.group(1)
    if host[0].isdigit():
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:          # "127.999.0.1" would be looked up by NAME
            return False
    return True


def _post(path: str, payload: Dict[str, Any], host: str = OLLAMA_HOST,
          timeout: int = 900) -> Dict[str, Any]:
    if not _is_loopback_url(host):
        raise SystemExit(f"refusing non-local Ollama host {host}")
    req = urllib.request.Request(
        host.rstrip("/") + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _get(path: str, host: str = OLLAMA_HOST) -> Dict[str, Any]:
    with urllib.request.urlopen(host.rstrip("/") + path, timeout=30) as resp:
        return json.loads(resp.read().decode())


def ollama_unload_all(host: str = OLLAMA_HOST) -> List[str]:
    """keep_alive 0 for every resident model; waits until none is left."""
    names = [m["name"] for m in _get("/api/ps", host).get("models", [])]
    for n in names:
        _post("/api/generate", {"model": n, "keep_alive": 0}, host)
    for _ in range(60):
        if not _get("/api/ps", host).get("models"):
            break
        time.sleep(0.5)
    return names


def ollama_runner_memory_mb() -> Optional[int]:
    """Working set of the largest ollama.exe (the model runner), from
    tasklist — no psutil."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/fi", "imagename eq ollama.exe", "/fo", "csv",
             "/nh"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    best = None
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 5:
            kb = int("".join(ch for ch in parts[4] if ch.isdigit()) or 0)
            best = kb if best is None else max(best, kb)
    return None if best is None else round(best / 1024)


def _tokenizer(gguf: str):
    from llama_cpp import Llama
    return Llama(model_path=gguf, vocab_only=True, verbose=False)


def run_ollama(cfg: Dict[str, Any]) -> Dict[str, Any]:
    rec: Dict[str, Any] = dict(cfg)
    host = cfg.get("host", OLLAMA_HOST)
    model = cfg["model"]
    num_ctx = int(cfg.get("n_ctx", N_CTX))
    seed = int(cfg.get("seed", 1))
    tok = _tokenizer(cfg["tokenizer_gguf"]) if cfg.get("tokenizer_gguf") else None
    ollama_unload_all(host)
    time.sleep(1.0)
    base = vram_used_mb()
    rec["vram_before_mb"] = base
    opts = {"num_ctx": num_ctx, "seed": seed, "temperature": 0.7}
    opts.update(cfg.get("options") or {})
    with VramPeak() as vp:
        first = True
        for n in cfg.get("pp_sizes", PP_SIZES):
            if tok is not None:
                ids = exact_tokens(tok, int(n), seed * 1000 + int(n))
                text = tok.detokenize(ids[1:]).decode("utf-8", errors="ignore")
            else:
                text = filler_text(int(int(n) * 0.75), seed * 1000 + int(n))
            r = _post("/api/generate", {
                "model": model, "prompt": text, "raw": True, "stream": False,
                "options": dict(opts, num_predict=1)}, host)
            if first:
                rec["load_s"] = round(r.get("load_duration", 0) / 1e9, 3)
                first = False
            pe, pd = r.get("prompt_eval_count", 0), r.get("prompt_eval_duration", 0)
            rec[f"pp{n}_count"] = pe
            rec[f"pp{n}_s"] = round(pd / 1e9, 3)
            rec[f"pp{n}_tps"] = round(pe / (pd / 1e9), 1) if pd else None
        r = _post("/api/generate", {
            "model": model, "prompt": GEN_PROMPT, "stream": False,
            "options": dict(opts, num_predict=int(cfg.get("gen_tokens",
                                                          GEN_TOKENS)))}, host)
        ec, ed = r.get("eval_count", 0), r.get("eval_duration", 0)
        rec["gen_tokens"] = ec
        rec["gen_tps"] = round(ec / (ed / 1e9), 1) if ed else None
        ps = [m for m in _get("/api/ps", host).get("models", [])
              if m.get("name") == model or m.get("model") == model]
        if ps:
            size, size_vram = ps[0].get("size", 0), ps[0].get("size_vram", 0)
            rec["ollama_size_mb"] = round(size / 2**20)
            rec["ollama_size_vram_mb"] = round(size_vram / 2**20)
            rec["ollama_gpu_fraction"] = round(size_vram / size, 3) if size else None
            rec["ollama_context_length"] = ps[0].get("context_length")
        rec["ram_mb"] = ollama_runner_memory_mb()
    rec["vram_peak_mb"] = vp.peak
    if vp.peak is not None and base is not None:
        rec["vram_mb"] = vp.peak - base
    if cfg.get("unload_after", True):
        ollama_unload_all(host)
    return rec


# ============================================================
# Driver: a fresh process per run, JSONL out, medians in a table
# ============================================================

def _child(argv: List[str]) -> int:
    cfg = json.loads(argv[0])
    try:
        rec = run_llama(cfg) if cfg["runtime"] == "llama_cpp" else run_ollama(cfg)
        rec["ok"] = True
    except Exception as exc:                                   # noqa: BLE001
        rec = dict(cfg, ok=False, error=repr(exc))
    print("RESULT " + json.dumps(rec), flush=True)
    return 0


def _spawn(cfg: Dict[str, Any], python: str, timeout: int) -> Dict[str, Any]:
    t = time.perf_counter()
    env = dict(os.environ)
    if cfg.get("hide_gpu"):
        # A CUDA build of llama.cpp with n_gpu_layers=0 still ships large
        # prompt batches to the GPU (op offload): measured 1 GB of VRAM and
        # ~10x the prompt speed of a real CPU. Hiding the device is what a
        # machine without an NVIDIA card actually gets.
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    try:
        proc = subprocess.run(
            [python, str(Path(__file__).resolve()), "_child", json.dumps(cfg)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return dict(cfg, ok=False, error=f"timeout after {timeout}s")
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            rec = json.loads(line[7:])
            rec["wall_s"] = round(time.perf_counter() - t, 1)
            return rec
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
    return dict(cfg, ok=False, error=f"exit {proc.returncode}: "
                + " | ".join(tail))


def _emit(rec: Dict[str, Any], out: Optional[str]) -> None:
    keep = {k: v for k, v in rec.items() if k not in ("eog_banned",)}
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(keep) + "\n")
    print(_row_text(rec), flush=True)


def _row_text(r: Dict[str, Any]) -> str:
    if not r.get("ok"):
        return f"FAILED {r.get('label')} {r.get('placement', '')}: {r.get('error')}"
    pp = "  ".join(f"pp{n}={r.get(f'pp{n}_tps')}" for n in PP_SIZES)
    return (f"{r.get('label'):<14} {r.get('runtime'):<9} "
            f"{r.get('placement', ''):<18} load={r.get('load_s')}s {pp}  "
            f"gen={r.get('gen_tps')} ({r.get('gen_tokens')} tok)  "
            f"vram={r.get('vram_mb')}MB ram={r.get('ram_mb')}MB")


def _placement(gl: int, threads: int, affinity: str, qos: str) -> str:
    where = "gpu-all" if gl >= 99 or gl < 0 else ("cpu" if gl == 0
                                                  else f"gpu-{gl}L")
    return f"{where}/t{threads}/{affinity}/{qos}"


def cmd_llama(args: argparse.Namespace) -> int:
    for gl in [int(x) for x in args.gpu_layers.split(",")]:
        for th in [int(x) for x in args.threads.split(",")]:
            for r in range(args.repeats):
                cfg = {"runtime": "llama_cpp", "label": args.label,
                       "gguf": args.gguf, "gpu_layers": gl, "threads": th,
                       "affinity": args.affinity, "qos": args.qos,
                       "flash_attn": args.flash_attn, "n_ctx": args.n_ctx,
                       "pp_sizes": list(PP_SIZES), "gen_tokens": args.gen,
                       "seed": 100 + r, "repeat": r + 1,
                       "placement": _placement(gl, th, args.affinity, args.qos)}
                if args.flash_attn:
                    cfg["placement"] += "/fa"
                if args.threads_batch:
                    # Prompt processing is compute-bound and decoding is
                    # memory-bound, so they may want different counts.
                    cfg["threads_batch"] = args.threads_batch
                    cfg["placement"] += f"/tb{args.threads_batch}"
                if args.hide_gpu:
                    cfg["hide_gpu"] = True
                    cfg["placement"] = cfg["placement"].replace(
                        "cpu/", "cpu-nogpu/", 1)
                _emit(_spawn(cfg, args.python, args.timeout), args.out)
    return 0


def cmd_ollama(args: argparse.Namespace) -> int:
    for r in range(args.repeats):
        cfg = {"runtime": "ollama", "label": args.label or args.model,
               "model": args.model, "tokenizer_gguf": args.tokenizer_gguf,
               "n_ctx": args.n_ctx, "pp_sizes": list(PP_SIZES),
               "gen_tokens": args.gen, "seed": 100 + r, "repeat": r + 1,
               "placement": "ollama-default/ctx%d" % args.n_ctx}
        _emit(_spawn(cfg, args.python, args.timeout), args.out)
    return 0


def summarise(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Median of every numeric field per (label, runtime, placement)."""
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("ok"):
            groups.setdefault((r.get("label"), r.get("runtime"),
                               r.get("placement")), []).append(r)
    out = []
    for (label, runtime, placement), rs in groups.items():
        med: Dict[str, Any] = {"label": label, "runtime": runtime,
                               "placement": placement, "n": len(rs)}
        for key in ("load_s", "pp2048_tps", "pp6144_tps", "gen_tps",
                    "vram_mb", "ram_mb", "ollama_gpu_fraction"):
            vals = [r[key] for r in rs if isinstance(r.get(key), (int, float))]
            if vals:
                med[key] = round(statistics.median(vals), 2)
                med[key + "_all"] = vals
        out.append(med)
    return out


def cmd_table(args: argparse.Namespace) -> int:
    rows = [json.loads(line) for line in Path(args.jsonl).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    print(f"{'model':<14} {'runtime':<9} {'placement':<26} {'n':>2} "
          f"{'load s':>7} {'pp@2k':>8} {'pp@6k':>8} {'gen':>7} "
          f"{'VRAM MB':>8} {'RAM MB':>7}")
    for m in summarise(rows):
        print(f"{str(m['label']):<14} {m['runtime']:<9} {m['placement']:<26} "
              f"{m['n']:>2} {m.get('load_s', ''):>7} "
              f"{m.get('pp2048_tps', ''):>8} {m.get('pp6144_tps', ''):>8} "
              f"{m.get('gen_tps', ''):>7} {m.get('vram_mb', ''):>8} "
              f"{m.get('ram_mb', ''):>7}")
    if args.json:
        Path(args.json).write_text(json.dumps(summarise(rows), indent=1),
                                   encoding="utf-8")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_child":
        return _child(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repeats", type=int, default=2)
    common.add_argument("--out", default="")
    common.add_argument("--n-ctx", type=int, default=N_CTX)
    common.add_argument("--gen", type=int, default=GEN_TOKENS)
    common.add_argument("--python", default=sys.executable)
    common.add_argument("--timeout", type=int, default=1800)
    p = sub.add_parser("llama", parents=[common])
    p.add_argument("--gguf", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--gpu-layers", default="99",
                   help="comma list; 99 = all, 0 = CPU")
    p.add_argument("--threads", default="8", help="comma list")
    p.add_argument("--threads-batch", type=int, default=0,
                   help="threads for prompt processing (default: --threads)")
    p.add_argument("--affinity", choices=("all", "p"), default="all")
    p.add_argument("--qos", choices=("default", "high"), default="high")
    p.add_argument("--flash-attn", action="store_true")
    p.add_argument("--hide-gpu", action="store_true",
                   help="CUDA_VISIBLE_DEVICES=-1: a true CPU-only machine")
    p.set_defaults(fn=cmd_llama)
    p = sub.add_parser("ollama", parents=[common])
    p.add_argument("--model", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--tokenizer-gguf", default="",
                   help="the same model's GGUF, to size prompts exactly")
    p.set_defaults(fn=cmd_ollama)
    p = sub.add_parser("table")
    p.add_argument("jsonl")
    p.add_argument("--json", default="")
    p.set_defaults(fn=cmd_table)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
