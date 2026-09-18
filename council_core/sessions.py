"""
council_core.sessions — past conversations, and loading one as prior context.

A session is a .jsonl file in <vault>/conversations. Loading one as PRIOR
rebuilds every personality against it, so the council answers the next question
with last week's context in mind. That is the widest-reaching button in the
app: it mutates global model state, and the Tk version does it on the GUI
thread while a model call runs on a worker that writes into the transcript.

FIVE DEFECTS, AND THE FIRST TWO ARE THE SAME MISTAKE THIS PORT KEEPS FINDING

1. THE SESSION ID ROUND-TRIPS THROUGH THE LIST LABEL.
   Both "Load as Prior" and "Preview" recover the id with
   `label.split("  [")[0].strip()`. Presentation IS the data model: change the
   badge format and both silently break, and an id containing the separator
   would be truncated. This is the fourth place today — the chart overlay, the
   specialist pin, the job queue and now this — where an identifier was
   recovered from display text. `SessionRow` carries its id.

2. THE PASS/FAIL BADGE IS THE HIGHEST-CONFIDENCE VERDICT, NOT THE LATEST.
   The lookup keeps, per session, the record with the greatest `confidence`.
   So a session that failed at confidence 9 and later passed at 6 is badged as
   FAILED, forever. The badge answers "how did this end?", so the answer is
   the last record, not the proudest one.

3. "…DELIBERATIONS" COUNTS ONLY WHAT IT READ.
   The verdict summary loads the last 200 records and then reports
   "{total} deliberations | {passed} passed", where total is the length of
   what it loaded. A vault with 5,000 deliberations reports 200. The count
   here is of the file, and the sample is named separately.

4. EVERY KEYSTROKE IN THE FILTER RE-PARSES UP TO 500 VERDICT RECORDS.
   The filter variable has a write trace onto the full refresh, which re-globs
   the conversations directory and re-reads the verdict file. Filtering is a
   string test over rows already in memory.

5. THE SUMMARY WORKER WRITES WIDGETS FROM A BACKGROUND THREAD.
   An independent AST pass over all 61 `Thread(target=...)` sites in the
   engine found this is the ONLY one that touches a UI method directly — it
   calls `_append_transcript` from inside the worker, twice. Everything here
   reports through a callback and lets the caller decide the thread.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: How many verdict records the badge lookup reads. The Tk number.
VERDICT_SAMPLE = 500

#: How many turns of a session the summary is written from.
SUMMARY_TURNS = 40

#: Room for the 200-350 words the prompt asks for.
SUMMARY_MAX_TOKENS = 450


@dataclass(frozen=True)
class SessionRow:
    """One row. `id` is the address; `label` is for a human to read."""
    id: str
    label: str
    confidence: Optional[int] = None
    passed: Optional[bool] = None

    def __str__(self) -> str:
        return self.label


@dataclass
class SessionListResult:
    ok: bool
    message: str
    rows: List[SessionRow] = field(default_factory=list)
    error: Optional[BaseException] = None


@dataclass
class OpResult:
    ok: bool
    message: str
    error: Optional[BaseException] = None


# ============================================================
# Verdicts, and the badge they produce
# ============================================================

def read_verdicts(path: Any, *, last_n: int = VERDICT_SAMPLE) -> Tuple[List[dict], int]:
    """(the last ``last_n`` records, how many the file holds).

    Both numbers, because the Tk summary reports the length of what it read as
    if it were the total. A vault with 5,000 deliberations reports 200.
    """
    path = Path(path)
    if not path.exists():
        return [], 0
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    except Exception:                                     # noqa: BLE001
        return [], 0
    records = []
    for line in lines[-last_n:]:
        try:
            records.append(json.loads(line))
        except Exception:                                 # noqa: BLE001
            pass                    # one torn line must not lose the rest
    return records, len(lines)


def latest_by_session(records: Sequence[dict]) -> Dict[str, dict]:
    """The LAST verdict per session, in file order.

    Not the highest-confidence one. The badge answers "how did this end?", and
    a session that failed at confidence 9 and later passed at 6 ended by
    passing — the Tk lookup badges it as failed, permanently.
    """
    latest: Dict[str, dict] = {}
    for record in records:
        session_id = record.get("session_id") or ""
        if session_id:
            latest[session_id] = record          # later wins
    return latest


def session_label(session_id: str, record: Optional[dict]) -> str:
    """The row text. Never parsed back — see SessionRow."""
    if not record:
        return session_id
    confidence = record.get("confidence", 0)
    badge = "✓" if record.get("passed") else "✗"
    return f"{session_id:<30}  [{confidence}/10 {badge}]"


def verdict_summary(total: int, sample: Sequence[dict]) -> str:
    """The line under the verdict history.

    Says what it actually read. "200 deliberations" when the file holds 5,000
    is not a rounding error, it is a wrong answer to "how have we been doing".
    """
    passed = sum(1 for r in sample if r.get("passed"))
    if not total:
        return "No deliberations recorded yet."
    if len(sample) >= total:
        return f"{total} deliberations | {passed} passed."
    return (f"{total} deliberations recorded | last {len(sample)} read, "
            f"{passed} of those passed.")


# ============================================================
# The list
# ============================================================

def list_sessions(store: Any, verdict_path: Any, *,
                  last_n: int = VERDICT_SAMPLE) -> SessionListResult:
    """Every past session, badged with how it ended."""
    try:
        ids = list(store.list_sessions())
    except Exception as exc:                              # noqa: BLE001
        return SessionListResult(False,
                                 f"Could not read the sessions: {exc!r}",
                                 error=exc)

    records, _total = read_verdicts(verdict_path, last_n=last_n)
    latest = latest_by_session(records)

    rows = []
    for session_id in ids:
        record = latest.get(session_id)
        rows.append(SessionRow(
            id=session_id,
            label=session_label(session_id, record),
            confidence=(record or {}).get("confidence"),
            passed=(record or {}).get("passed")))

    return SessionListResult(
        True,
        (f"{len(rows)} session(s)." if rows else
         "No past sessions yet — they appear here after you close one."),
        rows=rows)


def filter_rows(rows: Sequence[SessionRow], term: str) -> List[SessionRow]:
    """Narrow a loaded list. No disk, no glob, no verdict file.

    The Tk filter traces onto the full refresh, so every keystroke re-globs
    the conversations directory and re-parses up to 500 verdict records.
    """
    term = (term or "").strip().lower()
    if not term:
        return list(rows)
    return [row for row in rows if term in row.id.lower()]


# ============================================================
# Loading one as prior context
# ============================================================

SUMMARY_PROMPT = (
    "Summarise this past council session in 200–350 words.\n"
    "Focus on: what the user was trying to achieve, key decisions made, "
    "what worked, what was unresolved, and anything worth remembering for "
    "future sessions.\n"
    "Write in plain prose — no bullet points, no headers.\n\n"
)


def build_summary_prompt(turns: Sequence[dict]) -> str:
    lines = [f"{t.get('who', '?')}: {t.get('text', '')[:600]}" for t in turns]
    return (SUMMARY_PROMPT
            + f"SESSION TRANSCRIPT (most recent {len(turns)} turns):\n"
            + "\n".join(lines))


def ensure_summary(store: Any, writer: Any, session_id: str) -> OpResult:
    """Write a prose summary for a session, once, and cache it.

    BLOCKING — it calls a model. Call it from a worker and report the result
    through that worker's normal hop; the Tk version calls `_append_transcript`
    from inside the thread, which is the only one of the engine's 61 threads
    that touches a UI method directly.
    """
    if not session_id:
        return OpResult(False, "No session selected.")
    if store is None or writer is None:
        return OpResult(False, "No conversation store or writer model.")
    try:
        if store.load_generated_summary(session_id):
            return OpResult(True, "")          # already generated; say nothing
        turns = store.load_last(session_id, n=SUMMARY_TURNS)
        if not turns:
            return OpResult(True, "")          # nothing to summarise
        text = writer.respond(build_summary_prompt(turns),
                              max_tokens=SUMMARY_MAX_TOKENS)
        if not (text or "").strip():
            return OpResult(False, "The summary came back empty.")
        store.save_session_summary(session_id, text)
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Summary generation skipped: {exc}", error=exc)
    return OpResult(True, f"Session summary generated for '{session_id}'.")


def rebuild_with_prior(vault_dir: Any, session_id: Optional[str], *,
                       current_session: str = "",
                       dispatcher: Any = None) -> Tuple[Any, str]:
    """(personalities, problem) rebuilt against ``session_id`` as prior.

    BLOCKING — it loads models. The Tk version does this on the GUI thread,
    which is a freeze of however long the weights take.

    ``session_id=None`` clears the prior, which is the same operation.
    """
    from . import council_turn

    try:
        import council_engine
        pins = council_engine.load_personality_pins(
            Path(vault_dir) / "personality_backends.json")
        models = council_engine.build_personalities(
            pins=pins, vault_dir=Path(vault_dir),
            session_id=current_session or "qt", trace=False,
            dispatcher=dispatcher, prior_session_id=session_id)
    except Exception as exc:                              # noqa: BLE001
        return None, f"Could not rebuild the personalities: {exc!r}"

    loaded = council_turn.Personalities(models)
    if loaded.missing:
        return None, ("Missing required personalities: "
                      f"{', '.join(loaded.missing)}.")
    return loaded, ""


def prior_label(session_id: Optional[str]) -> str:
    return f"Prior: {session_id}" if session_id else "Prior: none"


def preview(store: Any, session_id: str, *, turns: int = SUMMARY_TURNS) -> str:
    """A readable excerpt of a past session."""
    if not session_id:
        return ""
    try:
        rows = store.load_last(session_id, n=turns) or []
    except Exception as exc:                              # noqa: BLE001
        return f"Could not read {session_id}: {exc}"
    if not rows:
        return f"{session_id} has no recorded turns."
    return "\n\n".join(f"{t.get('who', '?')}:\n{t.get('text', '')}"
                       for t in rows)
