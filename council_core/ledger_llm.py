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
    """The model failed once in this run and is not asked again (one_shot)."""


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


def council_chat(timeout: float = 120,
                 should_stop: Optional[Callable[[], bool]] = None) -> Chat:
    """The default backend: Council's local_chat (GGUF or role Ollama), then
    a US-origin Ollama model (source_extract's host search)."""
    def chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        try:
            import council_engine as ce
            text = ce.local_chat([{"role": "user", "content": prompt}],
                                 temperature=0.0, num_predict=1024,
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
            raise se.LedgerError(
                "no local model answered: set COUNCIL_GGUF_PATH to a US-origin GGUF, "
                "or install an Ollama model (e.g. `ollama pull granite3.3:8b`)")
        host, model = pick
        out = se._ollama("/api/chat", {
            "model": model, "stream": False, "format": schema,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}]}, timeout=timeout, host=host)
        return out["message"]["content"], f"{model} @ {host}"
    return chat


# ── the user picks the model (user, 2026-10-19: "ask the user to select what
# model they want to use that is downloaded on the computer") ──────────────
def available_models() -> List[Dict[str, Any]]:
    """Every model downloaded on this PC - GGUF files in Council's model
    folders and the models this PC's Ollama has - as local_models lists
    them, each with 'usable' False for a non-US-origin one (Council's
    rule: listed, never used)."""
    from council_core import local_models
    out = []
    for e in local_models.list_local_models():
        e = dict(e)
        e["usable"] = e.get("origin") != "non-US"
        e["label"] = local_models.describe(e) + (
            "" if e["usable"] else "   (not US-origin: not used by Council)")
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
        __import__("gc").collect()
        import source_extract as se
        try:
            from pathlib import Path
            import council_engine as ce
            llm = ce._load_gguf(Path(path), is_main=False)  # its VRAM-aware sizing
        except (ImportError, AttributeError, TypeError):
            try:
                from llama_cpp import Llama
            except Exception as exc:                       # noqa: BLE001
                raise se.LedgerError(
                    "this Council install cannot run GGUF files (llama-cpp-python "
                    f"would not load: {exc}). Pick an Ollama model instead.") from exc
            llm = Llama(model_path=path, n_ctx=8192, n_gpu_layers=-1, verbose=False)
        except Exception as exc:                           # noqa: BLE001
            raise se.LedgerError(f"{os.path.basename(path)} could not be loaded: {exc}") \
                from exc
        _GGUF["key"], _GGUF["llm"] = key, llm
        return llm


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


def chat_for(model_id: str, timeout: float = 120,
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
        if origin == "non-US":
            raise se.LedgerError(f"{name} is not a US-origin model; Council does not use it")

        def chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
            try:
                import council_engine as ce
                text = ce.local_chat([{"role": "user", "content": prompt}],
                                     temperature=0.0, num_predict=1024, json_schema=schema,
                                     timeout=int(max(5, timeout)), model=model_id,
                                     should_stop=should_stop)
            except ImportError:
                out = se._ollama("/api/chat", {
                    "model": name, "stream": False, "format": schema,
                    "options": {"temperature": 0},
                    "messages": [{"role": "user", "content": prompt}]}, timeout=timeout)
                text = out["message"]["content"]
            except Exception as exc:                       # noqa: BLE001
                raise se.LedgerError(_ollama_failed(name, exc)) from exc
            return text, name
        return chat

    path = model_id
    label = __import__("os").path.basename(path)
    if _is_main_gguf(path):
        def main_chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
            import council_engine as ce
            try:
                return ce.local_chat([{"role": "user", "content": prompt}],
                                     temperature=0.0, num_predict=1024, json_schema=schema,
                                     timeout=int(max(5, timeout)),
                                     should_stop=should_stop), label
            except Exception as exc:                       # noqa: BLE001
                raise se.LedgerError(f"{label} did not answer: {exc}") from exc
        return main_chat

    def gguf_chat(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        llm = _gguf_llm(path)
        kw = dict(messages=[{"role": "user", "content": prompt}], temperature=0.0,
                  max_tokens=1024)
        with _GGUF_LOCK:
            try:
                out = llm.create_chat_completion(
                    response_format={"type": "json_object", "schema": schema}, **kw)
            except TypeError:                              # an older llama-cpp-python
                out = llm.create_chat_completion(**kw)
        _gpu_ok()
        return out["choices"][0]["message"]["content"] or "", label
    return gguf_chat


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
    #: Why the model stopped being asked (one_shot), '' while it answers.
    unavailable: str = ""

    def describe(self) -> str:
        # the error's first sentence: the problem line below has all of it
        down = (self.unavailable.splitlines() or [""])[0].strip()
        if down.endswith(":") and ". " in down:            # '... installed. Install with:'
            down = down.rsplit(". ", 1)[0]
        down = down[:200].rstrip(".:")
        if down and not (self.values_placed or self.parts_found):
            return (f"Council's model was unavailable, so nothing in the review is its "
                    f"suggestion: {down}.")
        if not (self.labels_asked or self.parts_asked):
            return "Council's model: nothing it needed to be asked."
        bits = [f"Council's model ({self.model or 'none answered'}):"]
        if self.labels_asked:
            bits.append(f"placed {self.values_placed} value(s) from "
                        f"{self.labels_asked} unplaced label(s)")
        if self.parts_asked:
            got = ", ".join(f"{w} → {p}" for w, p in self.parts_found) or "none found"
            bits.append(f"part numbers for {self.parts_asked} workbook(s): {got}")
        tail = (f". Then it stopped answering and was not asked again: {down}" if down
                else "")
        return " ".join(bits[:1]) + " " + "; ".join(bits[1:]) + \
            " — all amber in the review: check them" + tail + "."


def one_shot(chat: Chat, report: AssistReport,
             should_stop: Optional[Callable[[], bool]] = None) -> Chat:
    """``chat`` asked only while it answers. A model that could not answer
    was asked again for every workbook (and a GGUF that would not load was
    loaded again each time): the same multi-line error was printed N+1
    times (review, 2026-10-19). The first failure is one problem line and
    ``report.unavailable``; every later question raises ModelUnavailable
    without asking. Cancel is not a failure."""
    def ask(prompt: str, schema: Dict[str, Any]) -> Tuple[str, str]:
        if report.unavailable:
            raise ModelUnavailable(report.unavailable)
        try:
            return chat(prompt, schema)
        except Exception as exc:                           # noqa: BLE001
            if should_stop is not None and should_stop():
                raise
            report.unavailable = str(exc).strip() or type(exc).__name__
            report.problems.append("Council's model did not answer, so it was not asked "
                                   f"again in this run: {report.unavailable}")
            raise ModelUnavailable(report.unavailable) from exc
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


def map_labels(labels: Sequence[str], fields: Sequence[str], chat: Chat
               ) -> Tuple[Dict[str, Optional[str]], str]:
    """{label: field or None} from the model, and which model answered.

    Asked as a LIST of {label, field} with both constrained to the given
    names: asked for {"label": "field"}, a real model (granite3.3:8b,
    2026-10-19) answered {"label": "Frequency (kHz)"} - the example taken
    literally. The old object shape is still read."""
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


def place_labels(run, schema, chat: Chat, report: AssistReport) -> None:
    """Values under labels no rule placed: ask which field each label is and
    add those values as suggested rows (via 'model'), in place of their
    'Labels to map' rows."""
    import source_extract as se
    if report.unavailable:                                 # one_shot: not asked again
        return
    labels = sorted({o.label.strip() for o in run.unmatched if o.label and o.label.strip()})
    labels = [x for x in labels if len(x) <= 80][:150]
    if not labels:
        return
    fields = [f.name for f in schema.fields if f.name != schema.id_field]
    report.labels_asked = len(labels)
    try:
        mapping, report.model = map_labels(labels, fields, chat)
    except ModelUnavailable:
        return                                             # said once, by one_shot
    except Exception as exc:                               # noqa: BLE001
        report.problems.append(f"Council's model was not asked about labels: {exc}")
        return
    placed = set()
    for o in list(run.unmatched):
        fname = mapping.get((o.label or "").strip())
        if not fname:
            continue
        run.obs.append(replace(o, field=fname, score=MODEL_SCORE, via="model",
                               note=se._join(o.note, f"field suggested by {report.model}"
                                                     " — check it")))
        placed.add(id(o))
        report.values_placed += 1
    # a placed value is no longer one to map: it also stayed on 'Labels to
    # map', with the rules' guesses contradicting the model's field, and was
    # counted as unmatched (review, 2026-10-19)
    if placed:
        run.unmatched[:] = [o for o in run.unmatched if id(o) not in placed]


#: Not part numbers, however they are written: a measured value (61.35,
#: 1,234, -12), a year, a date.
_NUMBER = re.compile(r"[+-]\d+|[+-]?\d+(?:[.,]\d+)+|[+-]?\d*\.\d+")
_YEAR = re.compile(r"(?:19|20)\d\d")
_DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")
#: What is trimmed off a word before it is judged ('(XYZ9AB123),').
_TRIM = "()[],;:"


def _mask_re(pattern_mask: Optional[str]):
    if not pattern_mask:
        return None
    try:
        import source_extract as se
        return se.mask_to_regex(pattern_mask)
    except Exception:                                      # noqa: BLE001
        return None


def looks_like_part(text: Any, pattern_mask: Optional[str] = None) -> bool:
    """Could ``text`` be a part identifier: the user's pattern exactly, or
    one word of 4+ characters with a digit in it that is not a number, a
    year or a date (only digits: 5+ of them, unless the user's pattern is
    all digits). The quote check alone took a title ('Laser build record',
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
        return not (s.isdigit() and len(s) < 5 and not digits_mask)
    return test


def _cells_text(grids, pattern_mask: Optional[str] = None) -> List[str]:
    """What the part question shows of a workbook, from ALL its sheets:
    cells with an identifier in them (or the user's pattern) first, then
    the other short text (labels, titles), plain numbers last - they are
    not part ids. The first 200 cells went, numbers included, so an id
    below a data table or on a later sheet never reached the model
    (review, 2026-10-19)."""
    rx = _mask_re(pattern_mask)
    is_part = _part_test(pattern_mask)
    ids: List[str] = []
    text: List[str] = []
    numbers: List[str] = []
    for g in grids:
        for (_r, _c), v in sorted(g.cells.items()):
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                s = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
                (ids if is_part(s) else numbers).append(s)
                continue
            if not isinstance(v, str):
                continue
            s = " ".join(v.split())
            found = [w.strip(_TRIM) for w in s.split() if is_part(w.strip(_TRIM))]
            if rx is not None:
                found += [m.group(0) for m in rx.finditer(s)]
            if s and len(s) <= MAX_CELL_CHARS:
                (ids if found else text).append(s)
            else:                                          # a long cell: its ids alone
                ids.extend(found)
    out: List[str] = []
    for s in dict.fromkeys(ids + text + numbers):
        out.append(s)
        if len(out) >= MAX_CELLS:
            break
    return out


def find_part(file_name: str, grids, pattern_mask: Optional[str], chat: Chat,
              report: AssistReport) -> Tuple[str, str]:
    """(part, where) the model found for one workbook, or ('', ''). The part
    is given as it is written in the workbook."""
    if report.unavailable:                                 # one_shot: not asked again
        return "", ""
    sheets = [g.sheet for g in grids]
    cells = _cells_text(grids, pattern_mask)
    shape = ""
    if pattern_mask:
        shape = (f"The user's part numbers often look like {pattern_mask!r} ('@' a letter, "
                 f"'#' a digit), for example {mask_example(pattern_mask)!r}, but other formats "
                 "also occur (separators, prefixes, revision suffixes, shorter or longer "
                 "variants). ")
    prompt = ("Which part (sample) is this inspection workbook about? " + shape +
              "Answer with the part identifier exactly as written in the workbook, and the "
              "cell text, sheet name or file name it is in. If no part identifier is "
              "written, answer with an empty part. Reply with JSON only: "
              "{\"part\": \"...\", \"evidence\": \"...\"}.\n\n"
              f"File name: {file_name}\nSheet names: {json.dumps(sheets)}\n"
              f"Cells:\n{json.dumps(cells, ensure_ascii=False)}")
    report.parts_asked += 1
    try:
        text, report.model = chat(prompt, _PART_SCHEMA)
    except ModelUnavailable:
        return "", ""                                      # said once, by one_shot
    except Exception as exc:                               # noqa: BLE001
        report.problems.append(f"{file_name}: Council's model was not asked for its part: "
                               f"{exc}")
        return "", ""
    got = _json(text)
    answer = str((got or {}).get("part") or "").strip() if isinstance(got, dict) else ""
    if not answer or len(answer) > 60:
        return "", ""
    # The quote check: it must look like an identifier and be written there
    # as a whole word. A model adds words around the identifier
    # ('XYZ9AB123-R2 (rev B)' for a cell 'S/N XYZ9AB123-R2  (rev B)',
    # granite3.3:8b, 2026-10-19), so the answer's pieces are tried too,
    # longest first.
    pieces = sorted((p for p in re.split(r"[\s()\[\],;:]+", answer) if len(p) >= 3),
                    key=len, reverse=True)
    # an identifier has no spaces: with words around it, the piece first
    tries = (pieces + [answer]) if re.search(r"\s", answer) else ([answer] + pieces)
    is_part = _part_test(pattern_mask)
    tries = [p for p in dict.fromkeys(tries) if is_part(p)]
    for part in tries:
        where, written = _written_at(part, file_name, sheets, grids)
        if where:
            report.parts_found.append((file_name, written))
            return written, where
    why = ("is not written in the workbook as a whole word" if tries else
           "does not look like a part number (a title, label, value or date?)")
    report.problems.append(f"{file_name}: Council's model named part {answer!r}, which "
                           f"{why} — dropped")
    return "", ""


def _whole_word(part: str, text: Any) -> str:
    """``part`` as it is written in ``text`` as a whole word - no letter or
    digit right before or after it; case and runs of spaces aside - or ''.
    A plain substring took 'efg5hj67' from 'efg5hj678' (review,
    2026-10-19)."""
    words = part.split()
    if not words:
        return ""
    body = r"\s+".join(re.escape(w) for w in words)
    m = re.search(r"(?<![^\W_])" + body + r"(?![^\W_])", str(text), re.IGNORECASE)
    return m.group(0) if m else ""


def _written_at(part: str, file_name: str, sheets: Sequence[str], grids
                ) -> Tuple[str, str]:
    """(where ``part`` is written as a whole word, how it is written there),
    or ('', '')."""
    got = _whole_word(part, file_name)
    if got:
        return f"file name {file_name!r}", got
    for s in sheets:
        got = _whole_word(part, s)
        if got:
            return f"sheet name {s!r}", got
    for g in grids:
        for (r, c), v in sorted(g.cells.items()):
            got = _whole_word(part, v)
            if got:
                return g.where(r, c), got
    return "", ""
