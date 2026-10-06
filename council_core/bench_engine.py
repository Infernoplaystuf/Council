"""
council_core.bench_engine — the benchmark's view of the REAL model path.

The "new" pipeline measures what the user runs: Describe it, the code-behind
writer and the docs answerer each call council_engine.local_chat themselves
(their own role, schema, seed, best-of-N, repair rounds). The benchmark does
not hand them a model call. It chooses the model the way the app does — a
model_slots.json in the scratch vault that puts every role on
"ollama:<name>" (use_model) — and OBSERVES each call with EngineTap, which
wraps council_engine.local_chat / chat_tools for the length of a run and
records what council_engine.last_call_stats says the call cost (prompt and
reply tokens, seconds, reply tok/s, whether a schema constrained it).

EngineTap can also SERVE the calls from a script instead of the engine: the
unit tests' model, and --replay of a recorded new-pipeline run.

Also here: the Ollama housekeeping a clean measurement needs — which models
are loaded and how much of each sits in VRAM (/api/ps), unloading one
(keep_alive 0) — and the warm speed probe check_this_pc reports.

Nothing here imports council_engine at module level; nothing loads a model
until a function is called.
"""
from __future__ import annotations

import functools
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Each reply is kept in the report up to this length.
MAX_REPLY_CHARS = 12000


@dataclass
class CallStat:
    """One model call, as the benchmark records it."""
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    hit_limit: bool = False
    error: str = ""
    #: The reply itself, so a run can be re-graded offline (--replay).
    reply: str = ""
    # From council_engine.last_call_stats when the engine served the call.
    role: str = ""
    model: str = ""
    backend: str = ""
    gen_tok_s: Optional[float] = None
    prompt_tok_s: Optional[float] = None
    ttft_s: Optional[float] = None
    load_s: Optional[float] = None
    constrained: Optional[bool] = None
    constraint: str = ""
    schema_valid: Optional[bool] = None
    kind: str = "chat"


class Backend:
    """A chat call that records what it cost. Subclasses implement _chat."""

    name = "backend"

    def __init__(self) -> None:
        self.calls: List[CallStat] = []

    def chat(self, messages: List[Dict[str, str]], *, temperature: float,
             num_predict: int, role: Optional[str] = None) -> str:
        stat = CallStat(role=role or "")
        t0 = time.perf_counter()
        try:
            text, p_tok, o_tok, hit = self._chat(
                messages, temperature=temperature, num_predict=num_predict,
                role=role)
        except Exception as exc:
            stat.seconds = round(time.perf_counter() - t0, 3)
            stat.error = repr(exc)[:300]
            self.calls.append(stat)
            raise
        stat.seconds = round(time.perf_counter() - t0, 3)
        stat.prompt_tokens, stat.output_tokens = int(p_tok), int(o_tok)
        stat.hit_limit = bool(hit)
        stat.reply = str(text)[:MAX_REPLY_CHARS]
        extra = getattr(self, "_last_extra", None)
        if extra:
            _apply_stats(stat, extra)
            self._last_extra = None
        self.calls.append(stat)
        return text

    def _chat(self, messages, *, temperature, num_predict, role):
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name}


def estimate(text: str) -> int:
    return max(1, (len(text or "") + 3) // 4) if text else 0


def _apply_stats(stat: CallStat, s: Dict[str, Any]) -> None:
    """Copy last_call_stats fields onto ``stat`` (only what is there)."""
    if s.get("prompt_tokens") is not None:
        stat.prompt_tokens = int(s["prompt_tokens"])
    if s.get("gen_tokens") is not None:
        stat.output_tokens = int(s["gen_tokens"])
    for src, dst in (("gen_tok_s", "gen_tok_s"),
                     ("prompt_tok_s", "prompt_tok_s"),
                     ("ttft_s", "ttft_s"), ("load_s", "load_s"),
                     ("constrained", "constrained"),
                     ("schema_valid", "schema_valid")):
        if s.get(src) is not None:
            setattr(stat, dst, s[src])
    stat.constraint = str(s.get("constraint") or stat.constraint or "")
    stat.model = str(s.get("model") or stat.model or "")
    stat.backend = str(s.get("backend") or stat.backend or "")
    if s.get("truncated"):
        stat.hit_limit = True


class ScriptedBackend(Backend):
    """Canned replies, in order — the unit tests' model. A reply that is an
    Exception instance is raised instead of returned."""

    name = "scripted"

    def __init__(self, replies: Sequence[Any]):
        super().__init__()
        self.replies = list(replies)
        self.prompts: List[str] = []

    def _chat(self, messages, *, temperature, num_predict, role):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if not self.replies:
            raise RuntimeError("scripted backend ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply), estimate(prompt), estimate(str(reply)), False


# ============================================================
# EngineTap — observe (or script) every engine call of a run
# ============================================================

class EngineTap(Backend):
    """Wraps council_engine.local_chat and chat_tools while installed:

        with EngineTap() as tap:
            designer_project.describe(text, pdir)   # its own model call
        tap.calls                                   # what each call cost

    ``replies`` (a list, or a callable(messages, kwargs) -> str) makes the
    tap ANSWER instead of the engine — every pipeline above it still runs
    for real. The wrappers keep the engine's signature visible
    (functools.wraps), so callers that read it to decide which keywords to
    pass (designer_project.local_chat, docs_qa.call_supported) see the
    engine's own."""

    name = "engine"

    def __init__(self, replies: Any = None):
        super().__init__()
        self._script = replies if (replies is None or callable(replies)) \
            else list(replies)
        self._engine = None
        self._orig: Dict[str, Any] = {}
        self._mine: Dict[str, Any] = {}
        self._local = threading.local()
        self.prompts: List[str] = []

    @property
    def scripted(self) -> bool:
        return self._script is not None

    # -- installing ----------------------------------------------------
    def install(self) -> "EngineTap":
        import council_engine
        self._engine = council_engine
        self._orig = {"local_chat": council_engine.local_chat,
                      "chat_tools": council_engine.chat_tools}
        self._mine = {"local_chat": self._wrap_chat(self._orig["local_chat"]),
                      "chat_tools": self._wrap_tools(self._orig["chat_tools"])}
        for k, fn in self._mine.items():
            setattr(council_engine, k, fn)
        return self

    def uninstall(self) -> None:
        eng = self._engine
        if eng is None:
            return
        for k, fn in self._orig.items():
            if getattr(eng, k, None) is self._mine.get(k):
                setattr(eng, k, fn)
        self._engine = None

    def __enter__(self) -> "EngineTap":
        return self.install()

    def __exit__(self, *exc) -> None:
        self.uninstall()

    # -- recording -----------------------------------------------------
    def _seq(self, role: Optional[str]) -> Any:
        try:
            return (self._engine.last_call_stats(role) or {}).get("seq")
        except Exception:                                 # noqa: BLE001
            return None

    def _fill(self, stat: CallStat, role: Optional[str], seq0: Any) -> None:
        try:
            s = self._engine.last_call_stats(role) or {}
        except Exception:                                 # noqa: BLE001
            s = {}
        if s and s.get("seq") != seq0:
            _apply_stats(stat, s)

    def _scripted_reply(self, messages: List[Dict[str, Any]],
                        kwargs: Dict[str, Any]) -> str:
        if callable(self._script):
            return str(self._script(messages, kwargs))
        if not self._script:
            raise RuntimeError("scripted backend ran out of replies")
        reply = self._script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply)

    def _wrap_chat(self, real: Callable[..., str]) -> Callable[..., str]:
        tap = self

        @functools.wraps(real)
        def local_chat(*args: Any, **kwargs: Any) -> str:
            messages = args[0] if args else kwargs.get("messages") or []
            role = kwargs.get("role")
            stat = CallStat(role=role or "", kind="chat")
            prompt = str((messages[-1] or {}).get("content") or "") \
                if messages else ""
            tap.prompts.append(prompt)
            seq0 = tap._seq(role)
            t0 = time.perf_counter()
            try:
                if tap.scripted:
                    if kwargs.get("should_stop") is not None and \
                            kwargs["should_stop"]():
                        raise RuntimeError("stopped by the caller")
                    text = tap._scripted_reply(messages, kwargs)
                else:
                    text = real(*args, **kwargs)
            except BaseException as exc:
                stat.seconds = round(time.perf_counter() - t0, 3)
                stat.error = repr(exc)[:300]
                if not tap.scripted:
                    tap._fill(stat, role, seq0)
                tap.calls.append(stat)
                raise
            stat.seconds = round(time.perf_counter() - t0, 3)
            stat.reply = str(text)[:MAX_REPLY_CHARS]
            if tap.scripted:
                stat.prompt_tokens = estimate(prompt)
                stat.output_tokens = estimate(str(text))
                stat.backend, stat.model = "scripted", "scripted"
                stat.constrained = kwargs.get("json_schema") is not None
            else:
                tap._fill(stat, role, seq0)
            n = kwargs.get("num_predict")
            if isinstance(n, int) and stat.output_tokens >= n - 2:
                stat.hit_limit = True
            tap.calls.append(stat)
            return text
        return local_chat

    def _wrap_tools(self, real: Callable[..., Any]) -> Callable[..., Any]:
        tap = self

        @functools.wraps(real)
        def chat_tools(*args: Any, **kwargs: Any) -> Any:
            role = kwargs.get("role")
            n0 = len(tap.calls)
            seq0 = tap._seq(role)
            t0 = time.perf_counter()
            stat = CallStat(role=role or "", kind="tools")
            try:
                if tap.scripted:
                    messages = args[0] if args else kwargs.get("messages")
                    got = tap._scripted_reply(messages or [], kwargs)
                    try:
                        out = json.loads(got)
                    except ValueError:
                        out = {"content": got, "tool_calls": []}
                else:
                    out = real(*args, **kwargs)
            except BaseException as exc:
                if len(tap.calls) == n0:
                    stat.seconds = round(time.perf_counter() - t0, 3)
                    stat.error = repr(exc)[:300]
                    if not tap.scripted:
                        tap._fill(stat, role, seq0)
                    tap.calls.append(stat)
                raise
            # The emulated path goes through local_chat, which recorded it.
            if len(tap.calls) == n0:
                stat.seconds = round(time.perf_counter() - t0, 3)
                try:
                    stat.reply = json.dumps(out)[:MAX_REPLY_CHARS]
                except (TypeError, ValueError):
                    stat.reply = str(out)[:MAX_REPLY_CHARS]
                if not tap.scripted:
                    tap._fill(stat, role, seq0)
                tap.calls.append(stat)
            return out
        return chat_tools

    def describe(self) -> Dict[str, Any]:
        models = sorted({c.model for c in self.calls if c.model})
        backends = sorted({c.backend for c in self.calls if c.backend})
        return {"backend": "+".join(backends) or ("scripted" if self.scripted
                                                  else "engine"),
                "served_by": models}


# ============================================================
# Choosing the model the way the app does
# ============================================================

def _reset_engine_routes() -> None:
    """Forget the engine's routing caches (an automatic Ollama pick, a slot
    that fell back, the windows it sent) when the engine is loaded — a
    cached fallback would otherwise win over the slot file just written."""
    eng = sys.modules.get("council_engine")
    if eng is None:
        return
    try:
        with eng._ROUTE_LOCK:                             # noqa: SLF001
            eng._AUTO_OLLAMA.clear()                      # noqa: SLF001
            eng._OLLAMA_WINDOWS.clear()                   # noqa: SLF001
            eng._AUTO_PICK = None                         # noqa: SLF001
    except Exception:                                     # noqa: BLE001
        pass


def use_model(vault: Path, model_id: str,
              n_ctx: Optional[int] = None) -> Path:
    """Put EVERY role on ``model_id`` in ``vault``'s model_slots.json — the
    Models tab's own mechanism — and make the engine read it. ``vault`` must
    be the scratch vault COUNCIL_VAULT_ROOT points at (it is set here when
    it does not): this never writes the user's vault."""
    from . import local_models, model_slots
    vault = Path(vault).resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "")
    if not env or Path(env).resolve() != vault:
        os.environ["COUNCIL_VAULT_ROOT"] = str(vault)
    path = model_id
    if local_models.is_ollama_id(model_id):
        # The installed tag, as the Roles panel saves it ("phi3.5" is
        # served as "phi3.5:latest").
        name = local_models.ollama_name(model_id)
        try:
            entry = local_models.ollama_model(name, max_age=60.0)
        except Exception:                                 # noqa: BLE001
            entry = None
        path = local_models.ollama_id((entry or {}).get("name") or name)
    cfg = model_slots.SlotConfig(
        slots={model_slots.MAIN: model_slots.Slot(model_slots.MAIN, path,
                                                  n_ctx)})
    written = model_slots.save(vault, cfg)
    model_slots.invalidate()
    _reset_engine_routes()
    return written


# ============================================================
# Ollama housekeeping (localhost only)
# ============================================================

def _host() -> str:
    from . import local_models
    host = local_models.ollama_host()
    local_models.require_local(host)
    return host


def _http(path: str, body: Optional[dict] = None, timeout: float = 30.0
          ) -> Any:
    """One call to this PC's Ollama — never through a proxy (see
    local_models.open_direct: with HTTP_PROXY set, /api/ps and the unload
    request went to the proxy instead)."""
    from . import local_models
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _host() + path, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"})
    with local_models.open_direct(req, timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def _same(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        s = s[len("ollama:"):] if s.startswith("ollama:") else s
        return s if ":" in s else s + ":latest"
    return norm(a) == norm(b)


def ollama_ps() -> List[Dict[str, Any]]:
    """What Ollama holds in memory now ([] when it cannot say)."""
    try:
        return list((_http("/api/ps", timeout=5.0) or {}).get("models") or [])
    except Exception:                                     # noqa: BLE001
        return []


def placement(model: str) -> Dict[str, Any]:
    """{placement: "GPU" | "part GPU" | "CPU" | "not loaded", vram_pct,
    size_mb, vram_mb} for ``model`` from /api/ps."""
    for m in ollama_ps():
        if _same(str(m.get("name") or m.get("model") or ""), model):
            size = int(m.get("size") or 0)
            vram = int(m.get("size_vram") or 0)
            pct = round(100.0 * vram / size, 1) if size else None
            if not size:
                where = "unknown"
            elif vram >= size * 0.98:
                where = "GPU"
            elif vram <= size * 0.02:
                where = "CPU"
            else:
                where = "part GPU"
            return {"placement": where, "vram_pct": pct,
                    "size_mb": size // (1024 * 1024),
                    "vram_mb": vram // (1024 * 1024)}
    return {"placement": "not loaded", "vram_pct": None, "size_mb": None,
            "vram_mb": None}


def unload(model: str, wait: float = 20.0) -> bool:
    """keep_alive 0 for ``model``; True once /api/ps no longer lists it."""
    from . import local_models
    name = local_models.ollama_name(model)
    try:
        entry = local_models.ollama_model(name, max_age=60.0)
        name = (entry or {}).get("name") or name      # the installed tag
    except Exception:                                     # noqa: BLE001
        pass
    try:
        _http("/api/generate", {"model": name, "keep_alive": 0}, timeout=30.0)
    except Exception:                                     # noqa: BLE001
        return False
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if not any(_same(str(m.get("name") or ""), name) for m in ollama_ps()):
            return True
        time.sleep(0.25)
    return False


def unload_all(wait: float = 30.0) -> List[str]:
    names = [str(m.get("name") or "") for m in ollama_ps()]
    for n in names:
        unload(n, wait=wait)
    return names


# ============================================================
# The warm speed probe
# ============================================================

_WORDS = ("plate part layer laser powder scan track melt pool frame camera "
          "image pixel stack slice bracket lattice support anneal titanium "
          "steel alloy grain crack pore density sensor signal sample value "
          "column table window button label entry report summary record "
          "thermal visual build chamber recoater nozzle vector hatch contour "
          "offset gain filter kernel median noise edge mask region volume "
          "count angle ratio error limit range batch queue stream buffer").split()


def filler(n_words: int, seed: int) -> str:
    """Pseudo-random words: a fresh prompt per call, so no prefix of it is in
    Ollama's prompt cache and the prefill is really measured."""
    rng = random.Random(seed)
    return " ".join(rng.choice(_WORDS) for _ in range(n_words))


SPEED_ROLE = "bench"


def speed_probe(model_id: str, *, warm_calls: int = 2,
                prompt_words: int = 900, gen_tokens: int = 192,
                timeout: float = 600.0,
                should_stop: Optional[Callable[[], bool]] = None,
                say: Optional[Callable[[str], None]] = None
                ) -> Dict[str, Any]:
    """Speed through council_engine.local_chat — the app's own path — on
    ``model_id``: one cold call (it loads the model; its time is reported
    as cold_s and NEVER mixed into the speeds), then ``warm_calls`` calls
    of ~1.2k prompt tokens and up to ``gen_tokens`` reply tokens, each with
    a fresh prompt. Then where Ollama put the weights (/api/ps).

    {cold_s, load_s, gen_tok_s, prompt_tok_s, ttft_s, warm: [...],
     placement, vram_pct, size_mb, vram_mb, backend, model, error}"""
    import council_engine
    say = say or (lambda _s: None)
    out: Dict[str, Any] = {"model": model_id, "error": ""}
    try:
        say("loading the model (a cold call, not counted as speed)…")
        t0 = time.perf_counter()
        council_engine.local_chat(
            [{"role": "user", "content": "Reply with the one word: ready"}],
            model=model_id, role=SPEED_ROLE, num_predict=8, temperature=0.0,
            timeout=int(timeout), seed=1, should_stop=should_stop)
        out["cold_s"] = round(time.perf_counter() - t0, 2)
        cold = council_engine.last_call_stats(SPEED_ROLE) or {}
        out["load_s"] = cold.get("load_s")
        out["backend"] = cold.get("backend")
        warm = []
        for i in range(max(1, int(warm_calls))):
            if should_stop is not None and should_stop():
                out["error"] = "stopped"
                return out
            say(f"warm speed call {i + 1}/{warm_calls}…")
            prompt = (f"Notes {i}: " + filler(prompt_words, 1000 + i)
                      + "\n\nIgnore the notes above. Write the whole numbers "
                        "from 1 to 400 in order, separated by single spaces, "
                        "and nothing else.")
            council_engine.local_chat(
                [{"role": "user", "content": prompt}], model=model_id,
                role=SPEED_ROLE, num_predict=int(gen_tokens),
                temperature=0.0, timeout=int(timeout), seed=7 + i,
                should_stop=should_stop)
            s = dict(council_engine.last_call_stats(SPEED_ROLE) or {})
            warm.append({k: s.get(k) for k in (
                "prompt_tokens", "gen_tokens", "seconds", "gen_tok_s",
                "prompt_tok_s", "ttft_s", "load_s")})
        out["warm"] = warm

        def med(key: str) -> Optional[float]:
            vals = [w[key] for w in warm if isinstance(w.get(key),
                                                       (int, float))]
            return round(statistics.median(vals), 2) if vals else None
        out.update(gen_tok_s=med("gen_tok_s"), prompt_tok_s=med("prompt_tok_s"),
                   ttft_s=med("ttft_s"))
    except Exception as exc:                              # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    try:
        from . import local_models
        if local_models.is_ollama_id(model_id):
            out.update(placement(model_id))
    except Exception:                                     # noqa: BLE001
        pass
    return out
