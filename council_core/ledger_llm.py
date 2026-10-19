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
  prefixes, revision suffixes). The sheet's short text cells are sent.
  An answer that is not literally in a cell, the sheet name or the file
  name is dropped (the quote check kg_extract uses for links).

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
#: Cells sent per sheet for the part question, and their longest text.
MAX_CELLS, MAX_CELL_CHARS = 200, 80

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
                raise se.LedgerError(f"{name} (Ollama) did not answer: {exc}. Is Ollama "
                                     "running?") from exc
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

    def describe(self) -> str:
        if not (self.labels_asked or self.parts_asked):
            return "Council's model: nothing it needed to be asked."
        bits = [f"Council's model ({self.model or 'none answered'}):"]
        if self.labels_asked:
            bits.append(f"placed {self.values_placed} value(s) from "
                        f"{self.labels_asked} unplaced label(s)")
        if self.parts_asked:
            got = ", ".join(f"{w} → {p}" for w, p in self.parts_found) or "none found"
            bits.append(f"part numbers for {self.parts_asked} workbook(s): {got}")
        return " ".join(bits[:1]) + " " + "; ".join(bits[1:]) + \
            " — all amber in the review: check them."


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
    add those values as suggested rows (via 'model')."""
    import source_extract as se
    labels = sorted({o.label.strip() for o in run.unmatched if o.label and o.label.strip()})
    labels = [x for x in labels if len(x) <= 80][:150]
    if not labels:
        return
    fields = [f.name for f in schema.fields if f.name != schema.id_field]
    report.labels_asked = len(labels)
    try:
        mapping, report.model = map_labels(labels, fields, chat)
    except Exception as exc:                               # noqa: BLE001
        report.problems.append(f"Council's model was not asked about labels: {exc}")
        return
    for o in list(run.unmatched):
        fname = mapping.get(o.label.strip())
        if not fname:
            continue
        run.obs.append(replace(o, field=fname, score=MODEL_SCORE, via="model",
                               note=se._join(o.note, f"field suggested by {report.model}"
                                                     " — check it")))
        report.values_placed += 1


def _cells_text(grids) -> List[str]:
    out: List[str] = []
    for g in grids:
        for (_r, _c), v in sorted(g.cells.items()):
            if isinstance(v, str):
                s = " ".join(v.split())
                if s and len(s) <= MAX_CELL_CHARS:
                    out.append(s)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(str(v))
            if len(out) >= MAX_CELLS:
                return out
    return out


def find_part(file_name: str, grids, pattern_mask: Optional[str], chat: Chat,
              report: AssistReport) -> Tuple[str, str]:
    """(part, where) the model found for one workbook, or ('', '')."""
    sheets = [g.sheet for g in grids]
    cells = _cells_text(grids)
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
    except Exception as exc:                               # noqa: BLE001
        report.problems.append(f"{file_name}: Council's model was not asked for its part: "
                               f"{exc}")
        return "", ""
    got = _json(text)
    answer = str((got or {}).get("part") or "").strip() if isinstance(got, dict) else ""
    if not answer or len(answer) > 60:
        return "", ""
    # The quote check: it must really be written there. A model adds words
    # around the identifier ('XYZ9AB123-R2 (rev B)' for a cell 'S/N
    # XYZ9AB123-R2  (rev B)', granite3.3:8b, 2026-10-19), so the answer's
    # longest piece with a digit in it is tried after the whole answer.
    pieces = sorted((p for p in re.split(r"[\s()\[\],;]+", answer) if len(p) >= 3
                     and any(ch.isdigit() for ch in p)), key=len, reverse=True)
    # an identifier has no spaces: with words around it, the piece first
    tries = (pieces + [answer]) if re.search(r"\s", answer) else ([answer] + pieces)
    for part in tries:
        where = _written_at(part, file_name, sheets, grids)
        if where:
            report.parts_found.append((file_name, part))
            return part, where
    report.problems.append(f"{file_name}: Council's model named part {answer!r}, which "
                           "is not written in the workbook — dropped")
    return "", ""


def _written_at(part: str, file_name: str, sheets: Sequence[str], grids) -> str:
    """Where ``part`` is written (whitespace-insensitive), or ''."""
    def fold(s: Any) -> str:
        return " ".join(str(s).split()).casefold()
    low = fold(part)
    if low in fold(file_name):
        return f"file name {file_name!r}"
    for s in sheets:
        if low in fold(s):
            return f"sheet name {s!r}"
    for g in grids:
        for (r, c), v in sorted(g.cells.items()):
            if low in fold(v):
                return g.where(r, c)
    return ""
