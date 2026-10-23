"""
council_core.ledger_llm — Council's own model helps the Ledger where label
matching runs out (user, 2026-10-19: "Label matching is nice but it will
never alone be enough").

Two questions, both asked only when the user ticks "Ask Council's model" in
the Ledger tab, and both answered as SUGGESTIONS the review shows in amber -
nothing a model says is accepted on its own:

- **Which template field is this label?** For labels no rule placed. The
  value beside the label is the cell's own; only the field is the model's.
  Only the labels are sent. Accepting such a row in the review teaches the
  mapping (apply_review), so the next run places it without a model.
- **Which part is this workbook about?** For workbooks whose values ended
  up with no part number. The user's part-name pattern (e.g. ``@@@#@@###``)
  goes in as ONE example of how their parts are named, with a made-up
  example of it, so the model can also recognise variants (separators,
  prefixes, revision suffixes). The workbook's short text cells are sent,
  identifier-looking ones first. An answer that does not look like an
  identifier, or is not written as a whole word in a cell, the sheet name
  or the file name, is dropped (the quote check kg_extract uses for links).

Both questions carry the user's own past answers as examples (few-shot):
what they answered in the Ledger tab's Teach (council_core.ledger_teach,
user 2026-10-20: "First I want an LLM guess to be made, then I give an
answer — hopefully this will help train the Ledger"). Up to MAX_EXAMPLES,
the ones most like the labels asked first (pick_examples); only labels,
field names and the part numbers the user confirmed - never other values.

Backend: council_engine.local_chat first - the user's GGUF, or the role's
Ollama - then a US-origin Ollama on this PC / the Pi nodes Council itself
enabled (source_extract.suggest_mappings_for_labels' order). Nothing leaves
the user's machines.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: (prompt, json_schema) -> (reply text, model label). Tests pass their own.
Chat = Callable[[str, Dict[str, Any]], Tuple[str, str]]

#: A model-placed value's confidence: below every auto-accept threshold.
MODEL_SCORE = 0.5
#: Cells sent for the part question (all sheets together), and their longest
#: text.
MAX_CELLS, MAX_CELL_CHARS = 200, 80


class ModelUnavailable(Exception):
    """The model is not asked again in this run (one_shot)."""


class ModelFailedAgain(Exception):
    """The model failed again with an error already in the problems: the
    caller says which workbook, not the whole error again (one_shot)."""


def _cannot(err: BaseException) -> BaseException:
    """Mark ``err`` as a model that cannot answer at all (not installed,
    would not load, nothing serves it): one_shot stops asking at once."""
    err.cannot_answer = True                               # type: ignore[attr-defined]
    return err


def _cannot_answer(exc: BaseException) -> bool:
    """Is ``exc`` (or what caused it) a model that cannot answer at all, not
    one question that failed? A timeout on one big workbook stopped every
    question after it - parts and labels - and the summary called an
    installed, answering model unavailable (review, 2026-10-19)."""
    import urllib.error
    try:
        import council_engine as ce
        down: Tuple[type, ...] = (ce.BackendUnavailable,)
    except Exception:                                      # noqa: BLE001
        down = ()
    seen = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if getattr(e, "cannot_answer", False) or isinstance(
                e, down + (ConnectionRefusedError, ImportError, FileNotFoundError)):
            return True
        if isinstance(e, urllib.error.URLError) and not isinstance(
                e, urllib.error.HTTPError) and not isinstance(e.reason, TimeoutError):
            return True                                    # nothing listens there
        if "has no model" in str(e):                       # council_engine: not installed
            return True
        e = e.__cause__ or e.__context__
    return False


#: How a timeout reads when only its words are left: council_engine's
#: LocalChatTimeout, Ollama's own 'timed out waiting for llama runner'.
_TIMED_OUT = re.compile(r"(?i)\btimed out\b|did not start answering within|sent nothing for \d"
                        r"|deadline exceeded")


def _timed_out(exc: BaseException) -> bool:
    """Did ``exc`` (or what caused it) run out of time - council_engine's
    LocalChatTimeout ('did not start answering within 300 s'), a socket
    timeout, Ollama's 'timed out waiting for llama runner'? Each question
    after it can wait as long again: in the real app every part-less
    workbook waited the full 300 s once more after the model had already
    timed out (review, 2026-10-19)."""
    import urllib.error
    seen = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, TimeoutError) or (isinstance(e, urllib.error.URLError)
                                           and isinstance(e.reason, TimeoutError)):
            return True
        if _TIMED_OUT.search(str(e)):
            return True
        e = e.__cause__ or e.__context__
    return False


_PART_SCHEMA = {
    "type": "object",
    "properties": {"part": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["part", "evidence"],
}
_MAP_SCHEMA = {"type": "object", "additionalProperties": {"type": ["string", "null"]}}


def mask_example(mask: str) -> str:
    """'@@@#@@###' -> 'ABC1DE234': a made-up part number in that shape."""
    letters, digits = "ABCDEFGHJKLMNPQRSTUVWXYZ", "1234567890"
    out, li, di = [], 0, 0
    for ch in mask or "":
        if ch == "@":
            out.append(letters[li % len(letters)])
            li += 1
        elif ch == "#":
            out.append(digits[di % len(digits)])
            di += 1
        else:
            out.append(ch)
    return "".join(out)


# ── the model's own limits (user, 2026-10-21: "the model inside of ledger
# should be able to use its max tokens and not the default set by the rest of
# the council") ────────────────────────────────────────────────────────────
#: The Ledger's model may use up to ALL its window, but each call is given the
#: window it needs, not the whole one. Sending an Ollama model its whole
#: context length on every call (128K for granite3.3, llama3.1, gpt-oss) made
#: Ollama reserve memory for all of it - ~16 GB of KV cache for an 8B model -
#: which does not fit a GPU: layers moved to the CPU and system RAM, and the
#: Ledger ran with high RAM and low CPU and GPU use (user, 2026-10-22). And
#: every switch between the council's 8K window and the Ledger's reloaded the
#: model. Now a call gets the smallest of WINDOW_STEPS that holds its prompt
#: and REPLY_ROOM for the answer - never more than the model's own - so a
#: typical call stays on the GPU and a long document still gets a long window.
#: A GGUF is loaded once: with GGUF_WINDOW at most, sized to the card by the
#: loader - never with the council's chat setting (COUNCIL_GGUF_N_CTX, the
#: Models tab's n_ctx), which was 4K on the user's PC (user, 2026-10-23:
#: "It is still using the council base token cap"). Council's own GGUF is
#: loaded the same way: the Ledger lets go of the council's copy (sized for
#: chat) and loads its own, so the file is not in memory twice. Each reply may use whatever the prompt
#: leaves of the window (num_predict 0: council_engine sizes it on the
#: model's own tokenizer, FILL). COUNCIL_LEDGER_NUM_CTX sets the window
#: outright (never past the model's own); COUNCIL_LEDGER_NUM_PREDICT the
#: reply.
#: windows a call may be given, smallest first
WINDOW_STEPS = (8192, 16384, 32768, 65536, 131072, 262144)
#: room kept for the answer when a window is chosen
REPLY_ROOM = 4096
#: the most a GGUF of the user's is loaded with (unless COUNCIL_LEDGER_NUM_CTX)
GGUF_WINDOW = 32768
#: tokens left free when the reply is sized (the chat template, rounding)
REPLY_MARGIN = 256
#: a reply never gets less than this, however long the prompt
REPLY_FLOOR = 1024
#: seconds with no new output before a call is given up (a long reply is
#: streamed, so this is a stall limit, not the whole reply's)
LEDGER_TIMEOUT = 300


def _env_int(name: str) -> int:
    import os
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 0


def _prompt_tokens(prompt: str) -> int:
    """A prompt's tokens told from its length, generously."""
    return len(prompt or "") // 3 + 64


def ledger_window(model_max: int = 0, prompt: str = "") -> int:
    """The window a call with ``prompt`` is given: the smallest of
    WINDOW_STEPS holding it and REPLY_ROOM, never past ``model_max`` (the
    model's own, 0 = not known: the engine keeps it under the model's own).
    COUNCIL_LEDGER_NUM_CTX instead when set."""
    cap = _env_int("COUNCIL_LEDGER_NUM_CTX")
    if cap:
        return min(cap, model_max) if model_max else cap
    need = _prompt_tokens(prompt) + REPLY_ROOM + REPLY_MARGIN
    window = next((w for w in WINDOW_STEPS if w >= need), WINDOW_STEPS[-1])
    return min(window, model_max) if model_max else window


def gguf_window(trained: int = 0) -> int:
    """The window a GGUF of the user's is loaded with (once, for every
    call): COUNCIL_LEDGER_NUM_CTX when set, else GGUF_WINDOW; never past the
    model's trained length."""
    want = _env_int("COUNCIL_LEDGER_NUM_CTX") or GGUF_WINDOW
    return min(want, trained) if trained else want


def reply_budget(prompt: str, window: int) -> int:
    """How long a reply may be: what the prompt leaves of the window (its
    tokens told from its length, generously), COUNCIL_LEDGER_NUM_PREDICT
    when set, never under REPLY_FLOOR."""
    fixed = _env_int("COUNCIL_LEDGER_NUM_PREDICT")
    if fixed:
        return fixed
    prompt_tokens = _prompt_tokens(prompt)
    return max(REPLY_FLOOR, int(window) - prompt_tokens - REPLY_MARGIN) if window \
        else REPLY_FLOOR * 8


def _ollama_context(name: str, host: Optional[str] = None) -> int:
    """An Ollama model's own context length (0: not known)."""
    try:
        from council_core import local_models
        entry = local_models.ollama_model(name, host, max_age=60.0) or {}
        ctx = entry.get("context_length")
        return int(ctx) if isinstance(ctx, int) and ctx > 0 else 0
    except Exception:                                       # noqa: BLE001
        return 0


def fill_reply() -> int:
    """num_predict for a call through council_engine.local_chat: 0 (FILL -
    the engine gives the reply all the prompt leaves, counted on the model's
    own tokenizer), or COUNCIL_LEDGER_NUM_PREDICT when set."""
    return _env_int("COUNCIL_LEDGER_NUM_PREDICT")


def council_chat(timeout: float = LEDGER_TIMEOUT,
                 should_stop: Optional[Callable[[], bool]] = None) -> Chat:
    """The default backend: Council's local_chat (GGUF or role Ollama), then
    a US-origin Ollama model (source_extract's host search). Both with the
    model's own limits, not the council's chat defaults."""
    def chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        try:
            import council_engine as ce
            main = _main_gguf()
            if main:                                       # its own window, not chat's
                text = _gguf_reply(main, prompt, schema, should_stop)
            else:
                with ce.model_window(ledger_window(0, prompt)):
                    text = ce.local_chat([{"role": "user", "content": prompt}],
                                         temperature=0.0, num_predict=fill_reply(),
                                         json_schema=schema, timeout=int(max(5, timeout)),
                                         should_stop=should_stop)
            if text and text.strip():
                return text, "Council's model"
        except Exception:                                  # noqa: BLE001
            if should_stop is not None and should_stop():
                raise
        import source_extract as se
        pick = se.first_us_text_model()
        if pick is None:
            raise _cannot(se.LedgerError(
                "no local model answered: set COUNCIL_GGUF_PATH to a US-origin GGUF, "
                "or install an Ollama model (e.g. `ollama pull granite3.3:8b`)"))
        host, model = pick
        out = se._ollama("/api/chat", {
            "model": model, "stream": False, "format": schema,
            "options": _ollama_options(prompt, model, host),
            "messages": [{"role": "user", "content": prompt}]}, timeout=timeout, host=host)
        return out["message"]["content"], f"{model} @ {host}"
    return chat


def _ollama_options(prompt: str, model: str, host: Optional[str] = None) -> Dict[str, Any]:
    """A direct Ollama call's options: the model's whole window, the reply
    all the prompt leaves of it."""
    window = ledger_window(_ollama_context(model, host), prompt)
    opts: Dict[str, Any] = {"temperature": 0, "num_predict": reply_budget(prompt, window)}
    if window:
        opts["num_ctx"] = window
    return opts


# ── the user picks the model (user, 2026-10-19: "ask the user to select what
# model they want to use that is downloaded on the computer") ──────────────
def _usable(backend: str, origin: str) -> bool:
    """Council's rule for a model the user picks: an Ollama model must be of
    US origin; a GGUF file is used unless it is known to be non-US (a GGUF
    is always accepted - its name often says nothing). An unknown-origin
    Ollama model was usable here and left out of Connections' Suggest
    links (review, 2026-10-19): one rule, as there."""
    return origin == "US" if backend == "ollama" else origin != "non-US"


def available_models() -> List[Dict[str, Any]]:
    """Every model downloaded on this PC - GGUF files in Council's model
    folders and the models this PC's Ollama has - as local_models lists
    them, each with 'usable' False for one Council does not use (_usable:
    listed, never used)."""
    from council_core import local_models
    out = []
    for e in local_models.list_local_models():
        e = dict(e)
        e["usable"] = _usable(str(e.get("backend") or ""), str(e.get("origin") or ""))
        why = ("not US-origin" if e.get("origin") == "non-US" else
               "origin unknown")
        e["label"] = local_models.describe(e) + (
            "" if e["usable"] else f"   ({why}: not used by Council)")
        out.append(e)
    return out


def _choice_file():
    from council_core import paths
    return paths.app_dir() / "ledger_model.json"


def remembered_model() -> str:
    """The model id picked last time ('' when none)."""
    try:
        return str(json.loads(_choice_file().read_text(encoding="utf-8")).get("model") or "")
    except Exception:                                      # noqa: BLE001
        return ""


def remember_model(model_id: str) -> None:
    try:
        f = _choice_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"model": model_id}), encoding="utf-8")
    except Exception:                                      # noqa: BLE001
        pass


_GGUF: Dict[str, Any] = {"key": None, "llm": None}
_GGUF_LOCK = __import__("threading").Lock()


def _gguf_llm(path: str):
    """The chosen GGUF, loaded once (another choice replaces it)."""
    import os
    key = os.path.normcase(os.path.realpath(path))
    with _GGUF_LOCK:
        if _GGUF["key"] == key and _GGUF["llm"] is not None:
            return _GGUF["llm"]
        _GGUF["key"] = _GGUF["llm"] = None
        if _is_main_gguf(path):
            _release_council_model()
        __import__("gc").collect()
        import source_extract as se
        try:
            from pathlib import Path
            import council_engine as ce
            # its VRAM-aware sizing, up to gguf_window() (COUNCIL_LEDGER_NUM_CTX
            # for more): a whole 128K window's KV cache does not fit a card
            try:
                trained = ce._gguf_max_context_from_metadata(
                    ce.read_gguf_metadata(Path(path)))
            except Exception:                              # noqa: BLE001
                trained = None
            llm = ce._load_gguf(Path(path), is_main=False, use_env_n_ctx=False,
                                n_ctx_max=gguf_window(trained or 0))
        except (ImportError, AttributeError, TypeError):
            try:
                from llama_cpp import Llama
            except Exception as exc:                       # noqa: BLE001
                raise _cannot(se.LedgerError(_gguf_failed(path, exc))) from exc
            llm = Llama(model_path=path, n_ctx=gguf_window(),
                        n_gpu_layers=-1, verbose=False)
        except Exception as exc:                           # noqa: BLE001
            raise _cannot(se.LedgerError(_gguf_failed(path, exc))) from exc
        _GGUF["key"], _GGUF["llm"] = key, llm
        return llm


def _gguf_failed(path: str, exc: BaseException) -> str:
    """What to say when the picked GGUF would not load: the reason in a few
    words and what to do. llama-cpp-python's own words were the whole
    message - 'Failed to load shared library ...\\llama.dll': Could not find
    module ... Try using the full path with constructor syntax.' - with no
    way out (review, 2026-10-20)."""
    import os
    text = " ".join(str(exc).split()) or type(exc).__name__
    missing = bool(re.search(r"(?i)llama[-_]cpp[-_]python is not installed|No module named "
                             r"'?llama_cpp", text))
    if missing:
        why = "llama-cpp-python is not installed"
    elif re.search(r"(?i)shared library|\.dll\b|\.so\b|\.dylib\b|DLL load failed", text):
        why = "llama-cpp-python's llama library, or one it needs, would not load"
    else:
        why = _clip(text.split(". Install with")[0].rstrip(" .:"), 120)
    return (f"{os.path.basename(path)} could not be loaded ({why}) — pick an Ollama model "
            f"instead, or {'install' if missing else 'reinstall'} llama-cpp-python")


def _gpu_ok() -> None:
    """A GGUF loaded here answered: clear council_engine's GPU-crash marker
    (<vault>/.gpu_attempt), as its own _gguf_chat does after a good
    generation. ce._load_gguf writes the marker; nothing here cleared it, so
    a clean Ledger / Suggest-links answer left it on disk and the NEXT
    Council process read it as a CUDA crash and loaded every GGUF on the
    CPU (review, 2026-10-19)."""
    try:
        import council_engine as ce
        ce._gpu_confirm_success()
    except Exception:                                      # noqa: BLE001
        pass


def _ollama_failed(name: str, exc: BaseException) -> str:
    """What to say when the picked Ollama model did not answer. A model
    Ollama does not have read '... in the Models tab.. Is Ollama running?'
    (review, 2026-10-19): a double period, a question Ollama had just
    answered, and a tab the Ledger does not pick its model in."""
    text = str(exc).strip()
    if "has no model" in text:                     # council_engine's two messages
        return (f"{name} is not installed in Ollama — pick another model, or install "
                f"it (ollama pull {name})")
    import urllib.error
    down = isinstance(exc, ConnectionError) or (
        isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError))
    try:
        import council_engine as ce
        down = down or isinstance(exc, ce.BackendUnavailable)
    except Exception:                                      # noqa: BLE001
        pass
    return (f"{name} (Ollama) did not answer: {text.rstrip('.') or type(exc).__name__}"
            + (". Is Ollama running?" if down else ""))


def _is_main_gguf(path: str) -> bool:
    import os
    main = os.environ.get("COUNCIL_GGUF_PATH", "").strip()
    return bool(main) and os.path.normcase(os.path.realpath(main)) == \
        os.path.normcase(os.path.realpath(path))


def _main_gguf() -> str:
    """Council's own GGUF (COUNCIL_GGUF_PATH) when it is a file and
    llama-cpp-python is here to load it, else '' (Council's model is then
    an Ollama one, asked through local_chat with the window the call needs)."""
    import importlib.util
    import os
    main = os.environ.get("COUNCIL_GGUF_PATH", "").strip()
    if not main or not os.path.isfile(main):
        return ""
    try:
        return main if importlib.util.find_spec("llama_cpp") is not None else ""
    except (ImportError, ValueError):
        return ""


def _release_council_model() -> None:
    """Let go of the council's loaded copy of a GGUF the Ledger loads
    itself (council_engine._release_slots: the next council answer loads it
    again, as after a Models-tab change)."""
    try:
        import council_engine as ce
        if ce._SLOT_INSTANCES:
            ce._release_slots()
    except Exception:                                      # noqa: BLE001
        pass


def chat_for(model_id: str, timeout: float = LEDGER_TIMEOUT,
             should_stop: Optional[Callable[[], bool]] = None) -> Chat:
    """A Chat on the model the user picked: an Ollama model ('ollama:<name>')
    through Council's local_chat, a GGUF file in process (Council's main
    model itself when it is that file)."""
    from council_core import local_models
    import source_extract as se
    if not model_id:
        return council_chat(timeout, should_stop)
    if local_models.is_ollama_id(model_id):
        name = local_models.ollama_name(model_id)
        _, origin = local_models.maker_and_origin(name)
        if not _usable("ollama", origin):
            raise se.LedgerError(
                f"{name} is not a US-origin model; Council does not use it" if
                origin == "non-US" else
                f"Council cannot tell where {name} comes from, so it does not use it "
                "— pick a US-origin model (Granite, Llama, Gemma, Phi, gpt-oss)")

        def chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
            try:
                import council_engine as ce
                with ce.model_window(ledger_window(0, prompt)):
                    text = ce.local_chat([{"role": "user", "content": prompt}],
                                         temperature=0.0, num_predict=fill_reply(),
                                         json_schema=schema, timeout=int(max(5, timeout)),
                                         model=model_id, should_stop=should_stop)
            except ImportError:
                out = se._ollama("/api/chat", {
                    "model": name, "stream": False, "format": schema,
                    "options": _ollama_options(prompt, name),
                    "messages": [{"role": "user", "content": prompt}]}, timeout=timeout)
                text = out["message"]["content"]
            except Exception as exc:                       # noqa: BLE001
                raise se.LedgerError(_ollama_failed(name, exc)) from exc
            return text, name
        return chat

    # a GGUF - Council's own too - is loaded by the Ledger with its own
    # window (_gguf_llm), not used at the council's chat window
    path = model_id
    label = __import__("os").path.basename(path)

    def gguf_chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        return _gguf_reply(path, prompt, schema, should_stop), label
    return gguf_chat


def _gguf_reply(path: str, prompt: str, schema: Dict[str, Any],
                should_stop: Optional[Callable[[], bool]] = None) -> str:
    """One answer from the GGUF at ``path``, loaded by the Ledger
    (_gguf_llm); the reply gets all the window the prompt leaves. Streamed,
    so Cancel stops it between tokens."""
    llm = _gguf_llm(path)
    try:
        window = int(llm.n_ctx())
    except Exception:                                      # noqa: BLE001
        window = 0
    try:                                                   # its own tokenizer's count
        used = len(llm.tokenize(prompt.encode("utf-8", errors="ignore"))) + 64
    except Exception:                                      # noqa: BLE001
        used = len(prompt) // 3 + 64
    fixed = _env_int("COUNCIL_LEDGER_NUM_PREDICT")
    kw = dict(messages=[{"role": "user", "content": prompt}], temperature=0.0,
              max_tokens=fixed or (max(REPLY_FLOOR, window - used - REPLY_MARGIN)
                                   if window else REPLY_FLOOR * 8))
    if should_stop is not None:
        kw["stream"] = True
    from council_core import structured_output as so
    with _GGUF_LOCK:
        try:                    # grammar_safe: labels with quote marks (2026-10-23)
            out = llm.create_chat_completion(
                response_format={"type": "json_object",
                                 "schema": so.grammar_safe(schema)}, **kw)
        except TypeError:                                  # an older llama-cpp-python
            out = llm.create_chat_completion(**kw)
        if isinstance(out, dict):
            text = out["choices"][0]["message"]["content"] or ""
        else:
            parts = []
            for chunk in out:
                if should_stop is not None and should_stop():
                    raise RuntimeError("stopped by the caller")
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                parts.append(delta.get("content") or "")
            text = "".join(parts)
    _gpu_ok()
    return text


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:                                      # noqa: BLE001
        m = re.search(r"\{.*\}", text or "", re.S)         # a model that talked around it
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:                              # noqa: BLE001
                return None
    return None


@dataclass
class AssistReport:
    model: str = ""
    labels_asked: int = 0
    values_placed: int = 0
    parts_asked: int = 0
    parts_found: List[Tuple[str, str]] = field(default_factory=list)   # (workbook, part)
    problems: List[str] = field(default_factory=list)
    #: What the summary says it was asked and did (review, 2026-10-20: 'part
    #: numbers for 7 workbook(s)' listed the 3 it found). Answers dropped by
    #: the checks (workbook, answer); answers with no part; labels it gave a
    #: field; values left on Labels to map (a per-item field, no item).
    parts_dropped: List[Tuple[str, str]] = field(default_factory=list)
    parts_none: int = 0
    labels_mapped: int = 0
    values_held: int = 0
    #: The label question failed (its error is a problem line): '0 given a
    #: field, 0 value(s) placed' read as answered (second review, 2026-10-20).
    labels_failed: bool = False
    #: The template's item field ('Flaw #'), for what values_held says.
    item_field: str = ""
    #: Why the model stopped being asked (one_shot), '' while it answers.
    unavailable: str = ""
    #: It stopped being asked after failing twice in a row, not because it
    #: cannot answer at all.
    gave_up: bool = False
    #: It stopped being asked after running out of time once (one_shot).
    timed_out: bool = False
    #: What the question being asked is about - a workbook's name, or the
    #: labels left over - so a timeout can say which one it was.
    asking: str = ""
    #: ``asking`` when the model stopped being asked.
    stopped_on: str = ""
    #: Cancel stopped the question being asked (one_shot): the model did
    #: not fail, and no problem line says it did not answer.
    cancelled: bool = False

    def _ran_out(self) -> str:
        """Why a model that timed out was not asked again: one slow workbook
        stops the rest too, so the summary has to say so (review,
        2026-10-19)."""
        on = f" on {self.stopped_on}" if self.stopped_on else ""
        return (f"ran out of time{on} and was not asked again in this run (each later "
                "question could have waited as long)")

    def describe(self) -> str:
        # the error's first sentence: the problem line below has all of it
        down = (self.unavailable.splitlines() or [""])[0].strip()
        if down.endswith(":") and ". " in down:            # '... installed. Install with:'
            down = down.rsplit(". ", 1)[0]
        down = _clip(down, 200)
        stop = f"Cancel stopped it while it was asked about {self.asking or 'the workbooks'}"
        if self.cancelled and not (self.values_placed or self.parts_found):
            # not '(none answered) ... did not answer: stopped by the
            # caller' (review, 2026-10-20)
            return f"Council's model: {stop}, so nothing in the review is its suggestion."
        if down and not (self.values_placed or self.parts_found):
            lead = ("Council's model " + self._ran_out() if self.timed_out else
                    "Council's model did not answer twice in a row and was not asked "
                    "again" if self.gave_up else "Council's model was unavailable")
            return f"{lead}, so nothing in the review is its suggestion: {_end(down)}"
        if not (self.labels_asked or self.parts_asked):
            return "Council's model: nothing it needed to be asked."
        # what it was asked, what it answered, what was used and what was
        # dropped: 'part numbers for 7 workbook(s): <3 listed> — all amber'
        # read as 7 found, and 'Council's model (Council's model)' named
        # nothing (review, 2026-10-20)
        name = "" if self.model in ("", "Council's model") else self.model
        bits = []
        if self.parts_asked:
            found, dropped = len(self.parts_found), len(self.parts_dropped)
            said = [f"{found} found" + (" (" + ", ".join(f"{w} → {p}" for w, p in
                                                         self.parts_found) + ")"
                                        if found else "")]
            if dropped:
                said.append(f"{dropped} answer(s) dropped as not a part number written "
                            "there (see the problems)")
            if self.parts_none:
                said.append(f"{self.parts_none} with no part in its answer")
            silent = self.parts_asked - found - dropped - self.parts_none
            if silent > 0:
                said.append(f"{silent} not answered")
            bits.append(f"asked which part {self.parts_asked} workbook(s) are about: "
                        + ", ".join(said))
        if self.labels_asked and self.labels_failed:
            bits.append(f"asked which field {self.labels_asked} unplaced label(s) are: "
                        "not answered (see the problems)")
        elif self.labels_asked:
            said = [f"{self.labels_mapped} given a field", f"{self.values_placed} value(s) "
                    "placed"]
            if self.values_held:
                item = self.item_field or "item"
                said.append(f"{self.values_held} value(s) left on Labels to map (their field "
                            f"is kept per {item} and they have no {item})")
            bits.append(f"asked which field {self.labels_asked} unplaced label(s) are: "
                        + ", ".join(said))
        what = ("all amber in the review: check them" if self.values_placed or self.parts_found
                else "nothing in the review is its suggestion")
        stopped = (self._ran_out() if self.timed_out else
                   "stopped answering and was not asked again")
        tail = (f". Then it {stopped}: {down}" if down else "") + (
            f". Then {stop}" if self.cancelled else "")
        return _end("Council's model" + (f" ({name})" if name else "") + ": "
                    + "; ".join(bits) + f" — {what}" + tail)


def _end(text: str) -> str:
    """``text`` as a sentence: one full stop, unless it already ends in
    '.', '?', '!' or '…' ('... Is Ollama running?.' - review, 2026-10-19)."""
    text = text.rstrip().rstrip(":;,").rstrip()
    return text if text.endswith((".", "?", "!", "…")) else text + "."


def _clip(text: str, most: int) -> str:
    """``text`` in at most about ``most`` characters, cut at the end of a
    clause ('; ', ', ', '. ') or else of a word ('…'), never inside a word:
    council_engine's own timeout words ended '... the request was canc.' in
    the summary (second review, 2026-10-19). The problem line has it all."""
    if len(text) <= most:
        return text
    cut = max(text.rfind(s, 0, most) for s in ("; ", ", ", ". "))
    if cut > most // 2:
        return text[:cut + (text[cut] == ".")]
    cut = text.rfind(" ", 0, most)
    return (text[:cut] if cut > most // 2 else text[:most]).rstrip(" ,;:") + "…"


def one_shot(chat: Chat, report: AssistReport,
             should_stop: Optional[Callable[[], bool]] = None) -> Chat:
    """``chat`` asked only while it answers. A model that could not answer
    was asked again for every workbook (and a GGUF that would not load was
    loaded again each time): the same multi-line error was printed N+1
    times (review, 2026-10-19).

    A model that cannot answer at all (_cannot_answer: not installed, would
    not load, no Ollama) is one problem line and ``report.unavailable``;
    every later question raises ModelUnavailable without asking. So does
    one that ran out of time (_timed_out), at its first timeout: asked
    again, each part-less workbook waited the full Ollama timeout once more
    (300 s each in the real app - review, 2026-10-19). The price is that one
    slow workbook stops the questions after it, labels too; the summary
    says so and which one (AssistReport._ran_out). One that failed a
    question quickly (a reply error) is asked again - it stopped every later
    workbook and the labels too - until it fails twice in a row; an error
    already said once raises ModelFailedAgain, so the caller names the
    workbook without the whole error again. Cancel is not a failure."""
    state = {"fails": 0, "said": set()}

    def ask(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        if report.unavailable:
            raise ModelUnavailable(report.unavailable)
        try:
            out = chat(prompt, schema)
        except Exception as exc:                           # noqa: BLE001
            if should_stop is not None and should_stop():
                report.cancelled = True                    # said by the caller, not here
                raise
            msg = str(exc).strip() or type(exc).__name__
            state["fails"] += 1
            said = msg in state["said"]
            state["said"].add(msg)
            fatal = _cannot_answer(exc)
            slow = not fatal and _timed_out(exc)
            if fatal or slow or state["fails"] >= 2:
                report.unavailable = msg
                report.gave_up = not (fatal or slow)
                report.timed_out = slow
                report.stopped_on = report.asking
                why = (report._ran_out() if slow else
                       "did not answer twice in a row, so it was not asked again in this run"
                       if report.gave_up else
                       "did not answer, so it was not asked again in this run")
                report.problems.append(
                    f"Council's model {why}: "
                    + ("the same error as above" if said else msg))
                raise ModelUnavailable(msg) from exc
            if said:
                raise ModelFailedAgain(msg) from exc
            raise
        state["fails"] = 0
        return out
    return ask


def _field_named(answer: Any, fields: Sequence[str]) -> Optional[str]:
    """The template field a model's answer names: exactly, ignoring case, or
    by its name without the section ('Flaw Length (in)' for 'Flaw Attributes:
    Flaw Length (in)') when only one field has it."""
    if not isinstance(answer, str) or not answer.strip():
        return None
    a = " ".join(answer.split()).casefold()
    for f in fields:
        if f.casefold() == a:
            return f
    plain = [f for f in fields if f.split(": ", 1)[-1].casefold() == a]
    return plain[0] if len(plain) == 1 else None


#: Past answers of the user's that go into one question as examples
#: (few-shot): enough to show their corrections, short enough for a small
#: model's context.
MAX_EXAMPLES = 20

#: (label, the field it means or None, why) - one answer the user gave in
#: the Ledger tab's Teach (council_core.ledger_teach.label_examples).
LabelExample = Tuple[str, Optional[str], str]


def _label_key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def pick_examples(examples: Sequence[LabelExample], labels: Sequence[str],
                  limit: int = MAX_EXAMPLES) -> List[LabelExample]:
    """The user's past answers most like the labels being asked, at most
    ``limit``: the same label first, then by shared words and spelling, then
    the newest (``examples`` oldest first, as the examples file keeps them).
    One answer per label: the newest. So the model's next guesses follow the
    user's corrections (user, 2026-10-20: "hopefully this will help train
    the Ledger")."""
    import difflib
    newest: Dict[str, Tuple[int, LabelExample]] = {}
    for i, ex in enumerate(examples):
        key = _label_key(ex[0])
        if key:
            newest[key] = (i, ex)
    asked = [(_label_key(x), set(_label_key(x).split())) for x in labels]
    asked = [(k, w) for k, w in asked if k]

    def near(key: str) -> float:
        words = set(key.split())
        best = 0.0
        for k, w in asked:
            if k == key:
                return 2.0
            shared = len(words & w) / max(len(words | w), 1)
            best = max(best, shared, difflib.SequenceMatcher(None, key, k).ratio() * 0.9)
        return best
    ranked = sorted(newest.items(), key=lambda kv: (-near(kv[0]), -kv[1][0]))
    return [ex for _k, (_i, ex) in ranked[:max(0, int(limit))]]


def _examples_text(examples: Sequence[LabelExample], fields: Sequence[str]) -> str:
    """The user's past answers as lines for a prompt: 'label -> field', or
    '-> null (why)' for one that is no field. An answer naming a field the
    question does not offer is left out: the model may only name those."""
    valid = set(fields)
    lines = []
    for ex in list(examples)[:MAX_EXAMPLES]:
        label, answer = ex[0], ex[1]
        why = ex[2] if len(ex) > 2 else ""
        if answer is not None and answer not in valid:
            continue
        lines.append(f"- {json.dumps(label, ensure_ascii=False)} -> "
                     + (json.dumps(answer, ensure_ascii=False) if answer else "null")
                     + (f" ({why})" if why and not answer else ""))
    if not lines:
        return ""
    return ("The user answered these labels before, for this same template; follow them for "
            "the same or similar labels:\n" + "\n".join(lines) + "\n\n")


def map_labels(labels: Sequence[str], fields: Sequence[str], chat: Chat,
               examples: Optional[Sequence[LabelExample]] = None,
               notes: str = "") -> Tuple[Dict[str, Optional[str]], str]:
    """{label: field or None} from the model, and which model answered.

    Asked as a LIST of {label, field} with both constrained to the given
    names: asked for {"label": "field"}, a real model (granite3.3:8b,
    2026-10-19) answered {"label": "Frequency (kHz)"} - the example taken
    literally. The old object shape is still read.

    ``examples``: the user's past answers (pick_examples), shown first so
    the model follows them; only labels and field names, never values."""
    labels, fields = list(labels), list(fields)
    schema = {"type": "object", "required": ["mappings"], "properties": {"mappings": {
        "type": "array", "items": {
            "type": "object", "required": ["label", "field"],
            "properties": {"label": {"type": "string", "enum": labels},
                           "field": {"type": ["string", "null"],
                                     "enum": fields + [None]}}}}}}
    prompt = ("These labels come from inspection spreadsheets. For EACH label, give the "
              "ONE template field it means, or null when none fits or you are unsure.\n"
              "Reply with JSON only, shaped like "
              "{\"mappings\": [{\"label\": <a label below>, \"field\": <a template field "
              "below, or null>}]}.\n\n"
              + _examples_text(examples or (), fields) + (notes or "") +
              f"Template fields:\n{json.dumps(fields)}\n\nLabels:\n{json.dumps(labels)}")
    text, model = chat(prompt, schema)
    got = _json(text)
    pairs: List[Tuple[Any, Any]] = []
    if isinstance(got, dict) and isinstance(got.get("mappings"), list):
        pairs = [(d.get("label"), d.get("field")) for d in got["mappings"]
                 if isinstance(d, dict)]
    elif isinstance(got, dict):
        pairs = list(got.items())
    by_fold = {lab.casefold(): lab for lab in labels}
    out: Dict[str, Optional[str]] = {lab: None for lab in labels}
    for lab, f in pairs:
        key = by_fold.get(str(lab or "").strip().casefold())
        if key is not None:
            out[key] = _field_named(f, fields)
    return out, model


def place_labels(run, schema, chat: Chat, report: AssistReport,
                 examples: Optional[Sequence[LabelExample]] = None,
                 guide: str = "") -> None:
    """Values under labels no rule placed: ask which field each label is and
    add those values as suggested rows (via 'model'), in place of their
    'Labels to map' rows - except a value with no item for a field kept
    per item (source_extract.needs_item), which stays there with a note.

    ``examples``: the user's answers from the Ledger tab's Teach
    (council_core.ledger_teach.label_examples); the ones most like these
    labels go in the question (pick_examples)."""
    import source_extract as se
    if report.unavailable:                                 # one_shot: not asked again
        return
    labels = sorted({o.label.strip() for o in run.unmatched if o.label and o.label.strip()})
    labels = [x for x in labels if len(x) <= 80][:150]
    if not labels:
        return
    fields = [f.name for f in schema.fields if f.name != schema.id_field]
    report.labels_asked = len(labels)
    report.asking = "the labels left over"
    try:
        import docs_open
        mapping, report.model = map_labels(
            labels, fields, chat, examples=pick_examples(examples or (), labels),
            # what the user's documents say these labels mean (docs_open)
            notes=docs_open.notes_text(getattr(run, "explanations", None) or [], labels)
            # the Ledger's filled example: what each field holds
            # (ledger_examples.filled_example_text, user 2026-10-23)
            + (guide or ""))
    except ModelUnavailable:
        report.labels_failed = True
        return                                             # said once, by one_shot
    except ModelFailedAgain:
        report.labels_failed = True
        report.problems.append("Council's model did not answer about the labels (the "
                               "same error as above)")
        return
    except Exception as exc:                               # noqa: BLE001
        if not report.cancelled:                           # Cancel is not a failure
            report.labels_failed = True
            report.problems.append(f"Council's model was not asked about labels: {exc}")
        return
    report.labels_mapped = sum(1 for f in mapping.values() if f)
    held: Dict[Tuple[str, str], int] = {}
    # one pass, into a new list: past its limit the old one is on disk and
    # gives copies (source_extract.ObsSpool) - a note added to one, or one
    # left out by identity, did not stick
    kept = se.ObsSpool()
    for o in run.unmatched:
        fname = mapping.get((o.label or "").strip())
        if not fname:
            kept.append(o)
            continue
        if se.needs_item(schema, fname, o.item):
            # A field kept per item (per flaw) for a value with no item: it
            # was placed with no flaw code - a 9-way red clash, and the one
            # accepted went to EVERY flaw row of the part (review,
            # 2026-10-20). It stays on 'Labels to map', the model's field
            # in its note.
            o.note = se._join(o.note, f"Council's model suggests {fname!r}, a field kept "
                                      f"per {schema.item_field}; this value has no "
                                      f"{schema.item_field}, so it was not placed")
            key = ((o.label or "").strip(), fname)
            held[key] = held.get(key, 0) + 1
            report.values_held += 1
            kept.append(o)
            continue
        run.obs.append(replace(o, field=fname, score=MODEL_SCORE, via="model",
                               note=se._join(o.note, f"field suggested by {report.model}"
                                                     " — check it")))
        report.values_placed += 1
    if held:
        report.item_field = schema.item_field
    for (label, fname), n in held.items():
        report.problems.append(
            f"Council's model suggested {fname!r} for label {label!r}, a field kept per "
            f"{schema.item_field}, but {n} value(s) under it have no {schema.item_field} "
            "beside them — left on 'Labels to map', not placed")
    # a placed value is no longer one to map: it also stayed on 'Labels to
    # map', with the rules' guesses contradicting the model's field, and was
    # counted as unmatched (review, 2026-10-19)
    if hasattr(run.unmatched, "close"):
        run.unmatched.close()
    run.unmatched = kept


#: Not part numbers, however they are written: a measured value (61.35,
#: 1,234, -12), a year, a date.
_NUMBER = re.compile(r"[+-]\d+|[+-]?\d+(?:[.,]\d+)+|[+-]?\d*\.\d+")
_YEAR = re.compile(r"(?:19|20)\d\d")
_DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")
#: ... nor a value with its unit ('61.35mm', '100kHz', '12.5%'), a time
#: ('10:30'), a date with a month name ('05-Oct-2026') or an ISO date-time:
#: all of them passed as part numbers (review, 2026-10-19). Units are
#: matched as written (mA, kg, ms - not MA, KG, MS, which end part numbers),
#: and an integer with a one-letter unit ('4471A') stays a possible part.
_UNIT = (r"(?:%|‰|°[CFK]?|deg[CF]?|Ω|ohms?|(?i:psi|ksi|rpm|ppm|bar|mbar|dBm?)"
         r"|(?i:[kmg]?hz)|[kMG]?Pa|[kmµμun]?[WVAJN]|[kmcµμun]?m|[mµμun]?s|[kmµμ]?g|[mµμ]?[lL]"
         r"|in|ft|lbs?|oz|min|hrs?|sec)(?:/(?:s|min|h|hr|rev|mm|m|in))?[23²³]?")
_MEASURE = re.compile(rf"(?P<num>[+-]?(?:\d+(?:[.,]\d+)*|\d*\.\d+))\s?(?P<unit>{_UNIT})")
_TIME_RX = r"\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s?(?i:[ap]\.?m\.?))?"
_TIME = re.compile(_TIME_RX)
_MON = (r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_MONTH_DATE = re.compile(
    rf"\d{{1,2}}[-/. ]?{_MON}[-/., ]?\d{{2,4}}"            # 05-Oct-2026, 5Oct26
    rf"|{_MON}[-/. ]?\d{{1,2}}(?i:st|nd|rd|th)?[-/., ]+\d{{2,4}}"   # Oct-05-2026
    rf"|\d{{4}}[-/. ]?{_MON}[-/. ]?\d{{1,2}}"              # 2026-Oct-05
    rf"|{_MON}[-/. ]?(?:19|20)?\d\d")                      # Oct2026, Oct-26
_ISO_DT = re.compile(rf"\d{{4}}-?\d{{2}}-?\d{{2}}[T_ ]{_TIME_RX}(?:Z|[+-]\d{{2}}:?\d{{2}})?"
                     r"|\d{8}T\d{4,6}Z?")
#: A container's default or generic name with a number: Excel's 'Sheet1' is
#: in almost every workbook, so a model that answered the sheet name merged
#: unrelated workbooks into one phantom part (review, 2026-10-19).
_GENERIC = re.compile(
    r"(?i:sheet|page|tab|table|chart|graph|plot|fig(?:ure)?|run|data(?:set)?|series"
    r"|row|col(?:umn)?|step|item|trial|test|rev(?:ision)?|ver(?:sion)?|log|scan|report"
    r"|file|book|workbook|untitled|image|img|photo|pic|dsc)[\s_\-#.]*\d+")
#: A spec or standard's number ('AMS2750', 'ASTM E1444', 'ISO 9001',
#: 'MIL-STD-1916', 'NAS410', 'AWS D17.1', 'BS EN 10204'): written in many
#: inspection workbooks and not a part. Taken as the part, part-less
#: workbooks merged into one phantom part 'AMS2750' (review, 2026-10-20).
#: The user's own pattern still wins (_part_test, _spaced_part).
#:
#: Only the standard's own shape: its body's name in capitals, then a
#: number - digits, a revision letter, a year ('ISO 9001:2015', 'AMS2750E')
#: - with the letters a body puts first ('E1444', 'AS9100', '-STD-'). Any
#: prefix and any code after it refused real ids ('EN12AB345', 'DIN7AB123',
#: 'BS-4471A', 'AMS-4UV-567'), and an inspector's initials before one
#: ('BS AB12CD345') made it a standard's (second review, 2026-10-20).
_STD_NUM = (r"\d+(?:[.:\-]\d+)*[A-Z]?(?:[.:\-]\d+)*"
            r"(?:/[A-Z]{0,2}\d+(?:[.:\-]\d+)*[A-Z]?)?")    # 'E1444/E1444M', 'D17.1/D17.1M'
_STANDARD = re.compile(
    r"(?:(?:BS|DIN|EN|ISO)(?:[\s/]+(?:EN|ISO|IEC))+\s*"    # 'BS EN 10204', 'ISO/IEC 17025'
    r"|(?:EN|BS)\s*"                                       # 'EN 4179', 'EN4179' - not 'BS-4471A'
    r"|(?:ISO|DIN|NAS|ARP)[\s\-]?"                         # 'ISO 9001', 'NAS410', 'ARP4754A'
    r"|(?:ASTM|AWS|ANSI|ASME|SAE)[\s\-]?(?:[A-Z]{1,2}[\s\-]?)?"   # 'ASTM E1444', 'SAE AS9100'
    r"|(?:MIL|AMS)(?:[\s\-][A-Z]{1,4}[\s\-]|[\s\-]?))"     # 'MIL-STD-1916', 'AMS2750E'
    + _STD_NUM)
#: What is trimmed off a word before it is judged ('(XYZ9AB123),').
_TRIM = "()[],;:"
#: A word of an id written with spaces ('QRS 6TU 789'): letters, digits,
#: '-', '_', '.', '/' - nothing in brackets.
_CODE_WORD = re.compile(r"[^\W_][\w\-./]*")
#: An id's label written before it ('S/N', 'P/N', 'ID', 'No.', 'PART',
#: 'Lot'): not a word of the id.
_ID_LABEL = re.compile(r"(?i)[sp]/?n|id|no\.?|nr\.?|part|serial|sample|specimen|lot|batch")
#: A month's name ('Oct', 'MAY'): not a word of an id.
_MONTH = re.compile(_MON)
#: A number written in groups of digits ('12 345', '1 234 567'): a value.
_GROUPED = re.compile(r"\d{1,3}(?: \d{3})+")
#: A name and a number - an axis, a channel, a cell ('X12', 'CH1', 'R1').
_NAMED = re.compile(r"([A-Za-z]+)\d+")


def _measured(s: str) -> bool:
    """A value with its unit - but not an integer with a one-letter unit,
    which is also how part numbers end ('4471A')."""
    m = _MEASURE.fullmatch(s)
    if not m:
        return False
    unit, num = m.group("unit"), m.group("num").lstrip("+-")
    return not (num.isdigit() and len(unit) == 1 and unit.isascii() and unit.isalpha())


#: A loose pattern's regex -> the same pattern taken exactly: words joined
#: into one part are held to the exact shape (_mask_whole). Loose, 'Lab 3
#: Bay 12' was a part for @@@#@@###.
_EXACT: Dict[str, Any] = {}


def _mask_re(pattern_mask: Optional[str]):
    if not pattern_mask:
        return None
    try:
        import source_extract as se
        rx = se.mask_to_regex(pattern_mask)
        _EXACT.setdefault(rx.pattern, se.mask_to_regex(pattern_mask, exact=True))
        return rx
    except Exception:                                      # noqa: BLE001
        return None


def looks_like_part(text: Any, pattern_mask: Optional[str] = None) -> bool:
    """Could ``text`` be a part identifier: the user's pattern exactly, or
    one word of 4+ characters with a digit in it that is not a number, a
    year, a date or time, a value with its unit or a default name like
    'Sheet1' (only digits: 5+ of them, unless the user's pattern is all
    digits). The quote check alone took a title ('Laser build record',
    llama3.1:8b - three workbooks merged into that phantom part), the sheet
    name, a label, a measured value, a year and a lone digit (review,
    2026-10-19)."""
    return _part_test(pattern_mask)(str(text or "").strip())


def _part_test(pattern_mask: Optional[str]) -> Callable[[str], bool]:
    """looks_like_part for one pattern, compiled once (a whole workbook's
    cells go through it)."""
    rx = _mask_re(pattern_mask)
    digits_mask = bool(pattern_mask) and "#" in pattern_mask and not any(
        ch == "@" or ch.isalpha() for ch in pattern_mask)

    def test(s: str) -> bool:
        if rx is not None and s and rx.fullmatch(s):
            return True
        if len(s) < 4 or re.search(r"\s", s) or not any(ch.isdigit() for ch in s):
            return False
        if _NUMBER.fullmatch(s) or _YEAR.fullmatch(s) or _DATE.fullmatch(s):
            return False
        if (_measured(s) or _TIME.fullmatch(s) or _MONTH_DATE.fullmatch(s)
                or _ISO_DT.fullmatch(s) or _GENERIC.fullmatch(s) or _STANDARD.fullmatch(s)):
            return False
        return not (s.isdigit() and len(s) < 5 and not digits_mask)
    return test


def _mask_whole(text: str, rx) -> bool:
    """Does the user's pattern match ``text`` whole - as written, or with
    the spaces inside it left out ('QRS 6TU 789' for @@@#@@###)? Then it is
    the id, whatever is written next to it ('QRS 6TU 789 A' was dropped -
    second review, 2026-10-19). Not a number written in groups ('12 345'
    for #####) nor words with a year ('FY 2026' for @@####): values."""
    if rx is None:
        return False
    words = text.split()
    spaced = " ".join(words)
    if rx.fullmatch(spaced):
        return True
    exact = _EXACT.get(rx.pattern, rx)
    return (len(words) > 1 and bool(exact.fullmatch("".join(words)))
            and not _GROUPED.fullmatch(spaced) and not any(_YEAR.fullmatch(w) for w in words))


def _spaced_part(text: str, is_part: Callable[[str], bool], rx) -> bool:
    """Is ``text`` - written in a cell with spaces inside it - one part
    identifier ('QRS 6TU 789')? looks_like_part refuses every space, so the
    id was dropped, or cut to a piece of it (review, 2026-10-19). Yes when
    the user's pattern matches it whole (_mask_whole).

    Otherwise only an id made of code groups: a model that copies the cell
    glued the words next to an id to it ('JD ABC1DE234', 'ABC1DE234 TOP',
    two ids), and labels with their values passed ('ISO 9001', 'CH 12',
    'Q3 2026', '10 30 AM') - phantom parts again (second review,
    2026-10-19). So the identifier rules hold for the words together
    ('Sheet 1', 'Run 3 of 5', a value with its unit - '280 W' - a date like
    'Oct 5 2026'); no word is an id by itself (_alone), nor is a word of
    letters alone outside what the user's pattern matches in it ('UT QRS
    6TU 789 A': a method and a side); one word at least mixes letters and
    digits ('6TU': not a label and its number); 6+ characters, not axes or
    a list of channels ('X12 Y34', 'CH1 CH2'); and each word is a code
    group (_code)."""
    words = text.split()
    spaced, joined = " ".join(words), "".join(words)
    if _mask_whole(spaced, rx):
        return True
    if (len(words) < 2 or len(joined) < 6 or joined.isdigit() or not is_part(joined)
            or _MEASURE.fullmatch(spaced) or _MONTH_DATE.fullmatch(spaced)):
        return False
    if any(_STANDARD.fullmatch(" ".join(words[a:b])) for a in range(len(words))
           for b in range(a + 1, len(words) + 1)):
        return False                                       # 'AWS D17.1 rev G' (2026-10-20)
    if any(_GENERIC.fullmatch(f"{u} {v}") for u, v in zip(words, words[1:])):
        return False                                       # 'Run 3 of 5', 'X7 Rev 2'
    if any(_alone(w, is_part, rx) for w in words):
        return False                                       # 'JD ABC1DE234', 'ABC1DE234 TOP'
    if rx is not None and any(
            _mask_whole(" ".join(words[a:b]), rx) and any(
                w.isalpha() for w in words[:a] + words[b:])
            for a in range(len(words)) for b in range(a + 1, len(words) + 1)):
        return False                                       # 'UT QRS 6TU 789 A'
    named = [_NAMED.fullmatch(w) for w in words]
    if all(named) and (all(len(m.group(1)) == 1 for m in named)
                       or len({m.group(1).casefold() for m in named}) == 1):
        return False                                       # 'X12 Y34', 'CH1 CH2'
    if not any(_mixed(w) for w in words):
        return False                                       # 'ISO 9001', 'CH 12', '10 30 AM'
    return all(_code(w) for w in words)


def _alone(w: str, is_part: Callable[[str], bool], rx) -> bool:
    """Is ``w`` an id by itself - the user's pattern, or looks_like_part
    with 6+ characters ('ABC1DE234')? A short one ('AB12') is also how an
    id written with spaces is grouped ('AB12 CD34')."""
    return bool(rx is not None and rx.fullmatch(w)) or (len(w) >= 6 and is_part(w))


def _mixed(w: str) -> bool:
    return any(ch.isalpha() for ch in w) and any(ch.isdigit() for ch in w)


def _code(w: str) -> bool:
    """Can ``w`` be one word of an id written with spaces (_spaced_part)?
    Not in brackets, not an id's label ('S/N', 'No.', 'PART'), a value
    ('61.35'), a year or a month; letters alone only as a group of at most
    3 in one case ('QRS') - not 'Rev', 'Tag', 'PROBE' or 'record': a label,
    a title."""
    if not _CODE_WORD.fullmatch(w) or _ID_LABEL.fullmatch(w) or _GENERIC.fullmatch(w):
        return False
    if _a_value(w) or _YEAR.fullmatch(w) or _MONTH.fullmatch(w):
        return False
    return not w.isalpha() or (len(w) <= 3 and (w.isupper() or w.islower()))


def _cells_text(grids, pattern_mask: Optional[str] = None) -> List[str]:
    """What the part question shows of a workbook, from ALL its sheets:
    cells with an identifier in them (or the user's pattern) first, then
    the other short text (labels, titles), plain numbers last - they are
    not part ids. The first 200 cells went, numbers included, so an id
    below a data table or on a later sheet never reached the model
    (review, 2026-10-19).

    A number - stored as one or as text - is never an identifier here: a
    table of whole-number readings (Excel stores 10200 Hz as 10200.0) went
    first as 'ids' and filled every place ahead of the S/N below it
    (review, 2026-10-19). One the user's pattern matches whole goes after
    the text ids."""
    rx = _mask_re(pattern_mask)
    is_part = _part_test(pattern_mask)
    ids: List[str] = []
    masked: List[str] = []
    text: List[str] = []
    numbers: List[str] = []

    def number(s: str) -> None:
        (masked if rx is not None and rx.fullmatch(s) else numbers).append(s)

    for g in grids:
        for (_r, _c), v in sorted(g.cells.items()):
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                number(str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                continue
            if not isinstance(v, str):
                continue
            s = " ".join(v.split())
            if not s:
                continue
            if s.lstrip("+-").isdigit() or _NUMBER.fullmatch(s):
                number(s)                                  # a number stored as text
                continue
            words = [w.strip(_TRIM) for w in s.split()]
            found = [w for w in words if is_part(w) and not w.isdigit()]
            if rx is not None:
                found += [m.group(0) for m in rx.finditer(s)]
            if len(s) <= MAX_CELL_CHARS:
                (ids if found else text).append(s)
            else:                                          # a long cell: its ids alone
                ids.extend(found)
    out: List[str] = []
    for s in dict.fromkeys(ids + masked + text + numbers):
        out.append(s)
        if len(out) >= MAX_CELLS:
            break
    return out


def _parts_text(examples: Sequence[Tuple[str, str]]) -> str:
    """Part numbers the user confirmed in other workbooks (Teach), newest
    first, as a sentence for the part question: what theirs look like."""
    seen: Dict[str, str] = {}
    for part, label in examples:
        part = " ".join(str(part or "").split())
        if part and len(part) <= 40 and part not in seen:
            seen[part] = " ".join(str(label or "").split())[:40]
        if len(seen) >= MAX_EXAMPLES:
            break
    if not seen:
        return ""
    said = ", ".join(json.dumps(p, ensure_ascii=False)
                     + (f" (labelled {json.dumps(lab, ensure_ascii=False)})" if lab else "")
                     for p, lab in seen.items())
    return ("Part numbers the user confirmed in other workbooks of this kind: " + said
            + " - this workbook's may look like them, but it must be written in THIS "
              "workbook. ")


def find_part(file_name: str, grids, pattern_mask: Optional[str], chat: Chat,
              report: AssistReport,
              examples: Optional[Sequence[Tuple[str, str]]] = None) -> Tuple[str, str]:
    """(part, where) the model found for one workbook, or ('', ''). The part
    is given as it is written in the workbook.

    ``examples``: (part, its label) the user confirmed in the Ledger tab's
    Teach, newest first (council_core.ledger_teach.part_examples): shown as
    what their part numbers look like. The quote check still holds."""
    if report.unavailable:                                 # one_shot: not asked again
        return "", ""
    sheets = [g.sheet for g in grids]
    cells = _cells_text(grids, pattern_mask)
    shape = ""
    if pattern_mask:
        import source_extract as se
        shapes = se.mask_shapes(pattern_mask) or [pattern_mask]
        shape = (f"The user's part numbers often look like "
                 + " or ".join(repr(m) for m in shapes) + " ('@' a letter, "
                 f"'#' a digit), for example {mask_example(shapes[0])!r}, but other formats "
                 "also occur (separators, prefixes, revision suffixes, shorter or longer "
                 "variants). ")
    shape += _parts_text(examples or ())
    prompt = ("Which part (sample) is this inspection workbook about? " + shape +
              "Answer with the part identifier exactly as written in the workbook, and the "
              "cell text, sheet name or file name it is in. If no part identifier is "
              "written, answer with an empty part. Reply with JSON only: "
              "{\"part\": \"...\", \"evidence\": \"...\"}.\n\n"
              f"File name: {file_name}\nSheet names: {json.dumps(sheets)}\n"
              f"Cells:\n{json.dumps(cells, ensure_ascii=False)}")
    report.parts_asked += 1
    report.asking = file_name
    try:
        text, report.model = chat(prompt, _PART_SCHEMA)
    except ModelUnavailable:
        return "", ""                                      # said once, by one_shot
    except ModelFailedAgain:
        report.problems.append(f"{file_name}: Council's model did not answer for its part "
                               "(the same error as above)")
        return "", ""
    except Exception as exc:                               # noqa: BLE001
        if not report.cancelled:                           # Cancel is not a failure
            report.problems.append(f"{file_name}: Council's model was not asked for its "
                                   f"part: {exc}")
        return "", ""
    got = _json(text)
    answer = str((got or {}).get("part") or "").strip() if isinstance(got, dict) else ""
    if not answer:
        report.parts_none += 1
        return "", ""
    if len(answer) > 60:
        report.parts_dropped.append((file_name, answer))
        report.problems.append(f"{file_name}: Council's model named a part of "
                               f"{len(answer)} characters, too long for a part number "
                               "— dropped")
        return "", ""
    # The quote check: it must look like an identifier and be written there
    # as a whole word - compared ignoring case and the spaces inside it, and
    # given as the workbook writes it. The whole answer first: an id written
    # with spaces ('QRS 6TU 789', what gemma3, granite3.3, llama3.1 and phi4
    # all answered) was dropped or cut to a piece of it ('6TU'), and the
    # answer 'QRS6TU789' for that cell was dropped (review, 2026-10-19).
    # Then the answer without the words around it that are no id's ('Part
    # QRS 6TU 789', 'S/N: AB12 CD34'), what the user's pattern matches whole
    # in it ('QRS 6TU 789' of 'QRS 6TU 789 Rev B'), then its pieces alone,
    # longest first: a model adds words around the identifier ('XYZ9AB123-R2
    # (rev B)' for a cell 'S/N XYZ9AB123-R2  (rev B)', granite3.3:8b,
    # 2026-10-19), or copies the cell ('JD ABC1DE234' - _spaced_part).
    is_part, rx = _part_test(pattern_mask), _mask_re(pattern_mask)

    def ok(s: str, in_cell: bool = True, in_name: bool = False) -> bool:
        """An identifier as written: spaces inside it only in a cell; in the
        file name, one code alone (_name_code). A cell or a sheet's name
        that writes the file's name is judged by _named_file: the stricter
        rule everywhere dropped real ids the cells write that are also the
        file's name ('PANEL1234' of PANEL1234.xlsx - second review,
        2026-10-20)."""
        if re.search(r"\s", s):
            return in_cell and _spaced_part(s, is_part, rx)
        if not is_part(s):
            return False
        return _name_code(s, rx) if in_name else not _named_file(s, file_name, pattern_mask)

    words = answer.split()
    i, j = 0, len(words)
    while i < j and not _code(words[i]):
        i += 1
    while j > i and not _code(words[j - 1]):
        j -= 1
    masked = sorted((s for s in (" ".join(words[a:b]) for a in range(len(words))
                                 for b in range(a + 2, len(words) + 1))
                     if rx is not None and _mask_whole(s, rx)), key=len, reverse=True)
    pieces = sorted((p for p in re.split(r"[\s()\[\],;:]+", answer) if len(p) >= 3),
                    key=len, reverse=True)
    # what the user's pattern matches inside a word: 'GHJ7KL890' of the
    # stem 'wb07_GHJ7KL890' (review, 2026-10-20)
    inside = [m.group(0) for m in rx.finditer(answer)] if rx is not None else []
    tries = [p for p in dict.fromkeys([" ".join(words), " ".join(words[i:j])] + masked
                                      + pieces + inside) if p]
    looks = any(ok(p) for p in tries)
    # judged as the workbook writes it: 'efg5hj 678' for a sheet 'EFG5HJ678'.
    # Only when nothing is written as the model wrote it, the separators
    # aside: 'ABC1DE234' for a cell 'ABC-1DE-234' was dropped (review,
    # 2026-10-20) - and is given as the cell writes it.
    for modes in ((True, False), ("sep",)):
        for part in (p for p in tries if ok(p) or ok("".join(p.split()))):
            where, written = _written_at(part, file_name, sheets, grids, ok, rx, modes)
            if where:
                report.parts_found.append((file_name, written))
                return written, where
    why = ("is not written in the workbook as a whole word" if looks else
           "does not look like a part number (a title, label, value, date, sheet or "
           "file name, a standard's number?)")
    if looks and _only_in_name(tries, file_name, ok, rx):
        why = ("is written only in the file name, where only one code of letters and digits "
               "(or your part-name pattern) is taken as the part, not a word with a number "
               "('week41', 'wb08', 'Results2026')")
    report.parts_dropped.append((file_name, answer))
    report.problems.append(f"{file_name}: Council's model named part {answer!r}, which "
                           f"{why} — dropped")
    return "", ""


def _named_file(part: str, file_name: str, pattern_mask: Optional[str]) -> bool:
    """``part``, written in a cell or a sheet's name, is the workbook's file
    name, not a part number: the whole name with its extension
    ('wb08.xlsx' - review, 2026-10-20), or the whole stem made of words
    ('inspection_run_3': it passed, '_' being a word boundary - review,
    2026-10-19). A stem that is one code ('AB12CD345', 'PANEL1234') can be
    the part, and so can whatever the user's pattern matches whole. In the
    file name itself _name_code judges: stricter."""
    from pathlib import PurePath
    folded = part.casefold()
    if folded in (file_name.casefold(), PurePath(file_name).name.casefold()):
        return True
    if folded != PurePath(file_name).stem.casefold():
        return False
    rx = _mask_re(pattern_mask)
    if rx is not None and rx.fullmatch(part):
        return False
    return any(len(w) >= 4 and w.isalpha() for w in re.split(r"[\s_\-.]+", part))


def _name_code(s: str, rx) -> bool:
    """Can ``s``, written in the file name, be the part? Only one code
    (_one_code) or what the user's pattern matches whole. Any piece of the
    name was taken: llama3.2:1b answered each workbook's own name, and
    'week41' of EC_log_week41.xlsx, 'wb08' of wb08_final.xlsx and
    'Results_2026' of Inspection_Results_2026.xlsx were the part (second
    review, 2026-10-20)."""
    return bool(rx is not None and rx.fullmatch(s)) or _one_code(s)


def _only_in_name(tries: Sequence[str], file_name: str, ok: Callable[..., bool], rx) -> bool:
    """Is one of the answer's ``tries`` an identifier written in the file
    name - where _name_code refused it - as a whole word? Then the problem
    line says so."""
    import os
    stem = os.path.splitext(file_name)[0]
    return any(_whole_word(p, stem, lambda s: ok(s, False), rx, exact)
               for p in tries for exact in (True, False, "sep"))


def _one_code(stem: str) -> bool:
    """Can a file's whole stem be the part? One code - groups of letters
    and digits joined by '-' at most ('AB12CD345', 'XYZ9AB123-R2',
    'RST-4UV-567') - with 6+ of them, letters and digits both, and no run
    of 4+ letters (a word: 'Results2026', 'week41'), nor a default name
    ('book12345', 'data_2')."""
    if not re.fullmatch(r"[^\W_]+(?:-[^\W_]+)*", stem):
        return False                                       # words: '_', spaces, '.'
    joined = stem.replace("-", "")
    return (len(joined) >= 6 and _mixed(joined) and not re.search(r"[^\W\d_]{4,}", joined)
            and not _GENERIC.fullmatch(stem))


def _squash(text: Any) -> str:
    return "".join(str(text).split()).casefold()


#: What the separator pass of _whole_word lets differ between the model's
#: answer and the cell: spaces, '-', '_', '.', '/'.
_SEPS = re.compile(r"[\s\-_./]+")


def _whole_word(part: str, text: Any, ok: Optional[Callable[[str], bool]] = None,
                rx=None, exact: Any = True) -> str:
    """``part`` as it is written in ``text`` as a whole word - no letter or
    digit right before or after it; case and runs of spaces aside, and with
    ``exact`` False the spaces inside it too ('QRS 6TU 789' for
    'QRS6TU789', and the other way round) - or ''. With ``exact`` 'sep'
    the separators inside it too ('ABC-1DE-234' for the answer
    'ABC1DE234': a model that left them out was dropped - review,
    2026-10-20). ``ok`` judges how it is written there. A plain substring
    took 'efg5hj67' from 'efg5hj678' (review, 2026-10-19)."""
    text = str(text)
    if exact == "sep":
        chars = _SEPS.sub("", part)
        if not chars or chars.casefold() not in _SEPS.sub("", text).casefold():
            return ""
        # ... and never a piece of an id written with them: 'ABC1DE' is
        # not 'ABC-1DE' of 'ABC-1DE-234', nor 'RST_4UV' of 'RST_4UV_567'
        # ('_' joins an id's groups here too - second review, 2026-10-20)
        body = (r"(?<![^\W_][\-./_])" + r"[\s\-_./]*".join(re.escape(ch) for ch in chars)
                + r"(?![\-./_][^\W_])")
    else:
        chars = "".join(part.split())
        if not chars or _squash(chars) not in _squash(text):   # most cells: no regex
            return ""
        body = (r"\s+".join(re.escape(w) for w in part.split()) if exact else
                r"\s*".join(re.escape(ch) for ch in chars))
    for m in re.finditer(r"(?<![^\W_])" + body + r"(?![^\W_])", text, re.IGNORECASE):
        got = m.group(0)
        if _inside_a_value(text, m.start(), m.end()) or (ok is not None and not ok(got)):
            continue
        if _in_a_standard(text, m.start(), m.end()) and not (rx is not None
                                                              and rx.fullmatch(got)):
            continue
        if ok is not None and _more_of_it(text, m.start(), m.end(), ok, rx):
            continue
        return " ".join(got.split())        # one space between its words, as the model saw it
    return ""


def _more_of_it(text: str, start: int, end: int, ok: Callable[[str], bool], rx) -> bool:
    """Is text[start:end] a piece of a longer id written there with spaces
    inside ('QRS 6TU' of 'QRS 6TU 789', 'ABC1' of 'ABC1 DE23 4XY')? A piece
    of a spaced id is what was wrong (review, 2026-10-19). What the user's
    pattern matches whole is no piece, whatever is written next to it, and
    a word of letters alone next to it ('A', 'TOP', 'UT', 'Rev' - a side, a
    method) does not make it longer: 'QRS 6TU 789 A' was dropped (second
    review, 2026-10-19)."""
    mid = text[start:end]
    if _mask_whole(mid, rx):
        return False
    before = text[:start].split()[-4:] if start and text[start - 1].isspace() else []
    after = text[end:].split()[:4] if end < len(text) and text[end].isspace() else []
    if rx is not None and any(_mask_whole(" ".join(before[len(before) - b:] + [mid] + after[:a]),
                                          rx)
                              for b in range(len(before) + 1) for a in range(len(after) + 1)
                              if a or b):
        return True                                        # '6TU 789' of 'QRS 6TU 789'
    return any(ok(" ".join(span)) for word, span in ((before[-1:], before[-1:] + [mid]),
                                                     (after[:1], [mid] + after[:1]))
               if word and any(ch.isdigit() for ch in word[0]))


def _in_a_standard(text: str, start: int, end: int) -> bool:
    """Is text[start:end] the number of a standard written there with its
    name before it - 'E1444' of 'ASTM E1444', '10204' of 'BS EN 10204',
    'AS9100' of 'SAE AS9100' (review, 2026-10-20)? The name in capitals and
    the number of a standard's shape (_STANDARD): initials before an id
    ('BS AB12CD345', 'AMS ABC1DE234') are no standard's name."""
    head = text[:start]
    if not head or not (head[-1].isspace() or head[-1] in "-/"):
        return False
    first = start + len(text[start:end].split()[0])         # 'E1444' of 'E1444 rev G'
    words = list(re.finditer(r"\S+", head))[-3:]
    return any(_STANDARD.fullmatch(" ".join(text[w.start():first].split())) for w in words)


def _inside_a_value(text: str, start: int, end: int) -> bool:
    """Is text[start:end] a piece of a date, time or measured value written
    there ('2026-10-05T10' of '2026-10-05T10:30': the answer's pieces are
    split at ':' - review, 2026-10-19)? One with spaces inside is judged
    with the words written before and after it too: 'Oct 5' of 'Oct 5,
    2026', 'AB 12' of 'AB 12 mm'."""
    a = max(text.rfind(ch, 0, start) for ch in " \t\n") + 1
    b = min((i for i in (text.find(ch, end) for ch in " \t\n") if i >= 0), default=len(text))
    token = text[a:b].strip(_TRIM + ".")
    if token != text[start:end]:
        return _a_value(token)
    if not re.search(r"\s", token):
        return False
    a = _word_before(text[:start])
    nxt = re.match(r"[\s,;:]*[^\s,;:]+", text[end:])
    a = start if a is None else a
    b = end + nxt.end() if nxt else end
    first, last = start + len(token.split()[0]), end - len(token.split()[-1])
    spans = {text[a:end], text[start:b], text[a:b], text[a:first], text[last:b]}
    return any(_a_value(" ".join(s.split()).strip(_TRIM + "."))
               for s in spans if s != token)


def _word_before(s: str) -> Optional[int]:
    """Where r'\\S+[\\s,;:]*$' first matches in ``s`` - the word written
    last, with the ',', ';', ':' and spaces after it - or None; read from
    the end, once. The pattern was tried from every character and rescanned
    the rest: a cell of 2,000 ':' and a word before a spaced part id held
    the GIL for 6 s while the extract asked for its part (second
    review, 2026-10-20)."""
    r = s[::-1]
    tail = re.match(r"[\s,;:]*", r).end()                 # one class: no backtracking
    if tail == len(s):                                     # ',', ';', ':' and spaces only
        m = re.search(r"\S", s)
        return m.start() if m else None
    return len(s) - tail - re.match(r"\S*", r[tail:]).end()


def _a_value(s: str) -> bool:
    """A number, date, time or measured value, as written."""
    return bool(_NUMBER.fullmatch(s) or _DATE.fullmatch(s) or _measured(s)
                or _TIME.fullmatch(s) or _MONTH_DATE.fullmatch(s) or _ISO_DT.fullmatch(s))


def _written_at(part: str, file_name: str, sheets: Sequence[str], grids,
                ok: Optional[Callable[..., bool]] = None, rx=None,
                modes: Sequence[Any] = (True, False)) -> Tuple[str, str]:
    """(where ``part`` is written as a whole word, how it is written there),
    or ('', ''). ``ok(written, in_cell)`` judges how it is written: an id
    with spaces inside is taken only as a cell writes it. Written as the
    model wrote it first, anywhere, and only then with other spaces inside:
    with 'QRS 6TU 789' in A1 and 'QRS6TU789' in A2, the answer 'QRS6TU789'
    was given as A1 writes it - the ledger compares ids with their spaces,
    so one sample became two (second review, 2026-10-19). ``modes``: how
    _whole_word compares, in turn.

    In the file name, only the name without its extension is read: the
    piece '2026.xlsx' of 'Inspection Results 2026.xlsx' was the part
    (review, 2026-10-20). What is found there is judged with
    ``ok(written, False, True)``: a stricter rule (find_part's _name_code)."""
    import os
    named = os.path.splitext(file_name)[0]

    def fits(in_cell: bool) -> Optional[Callable[[str], bool]]:
        return None if ok is None else (lambda s: ok(s, in_cell))
    in_name = None if ok is None else (lambda s: ok(s, False, True))
    for exact in modes:
        got = _whole_word(part, named, in_name, rx, exact)
        if got:
            return f"file name {file_name!r}", got
        for s in sheets:
            got = _whole_word(part, s, fits(False), rx, exact)
            if got:
                return f"sheet name {s!r}", got
        in_cell = fits(True)
        for g in grids:
            for (r, c), v in sorted(g.cells.items()):
                got = _whole_word(part, v, in_cell, rx, exact)
                if got:
                    return g.where(r, c), got
    return "", ""
