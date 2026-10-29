"""
council_qt.tabs.council — the Council tab, ported.

THE APP'S FRONT DOOR, AND THE LARGEST SINGLE METHOD IN IT
`_build_council_tab` is 300 lines of Tk and the turn behind it (`_send`) is
1,166 more. This file is written against
`docs/qt_migration/phase6_port_requirements.md` section A, NOT against the Tk
source — because six of the nine confirmed defects in that tab are logic
defects that a faithful translation would carry across silently.

The five that are designed out here, rather than fixed later:

  A1  THE WORKER NEVER READS A WIDGET. The Tk worker reads thirteen Tk
      variables off the GUI thread, which survives only because Tkinter
      marshals `Variable.get()` internally. Qt does not, so the direct
      translation is a data race on every send. `options()` snapshots into a
      frozen `CouncilOptions` on the GUI thread and the worker gets the
      snapshot.

  A2  ONE TURN AT A TIME. `_send` has no re-entrancy guard and is reachable
      eight ways; two concurrent turns patch and restore `model.respond` on top
      of each other. `begin_turn()` is the only door and it refuses a second
      turn OUT LOUD — an ignored click reads as a broken button.

  A3  THE VERDICT BAR IS TIED TO A VERDICT. In Tk the fast path shows the bar
      for a turn that produced no verdict, and agreeing then stamps
      `user_agreed=True` on the last line of `verdict_history.jsonl` — an
      unrelated earlier deliberation. Here the bar is shown only with a verdict
      id in hand, and agree/disagree carry that id.

  A4  PER-TURN STATE IS RESET IN ONE PLACE. `PER_TURN_FIELDS` names every
      field a turn sets, `reset_turn()` clears exactly those, and a test
      asserts the two agree — because the Tk reset block clears four fields and
      misses two, leaving a live button holding a stale question.

  A5  INTENT DETECTION READS WHAT THE USER TYPED. The Tk code rebinds
      `user_text = augmented` and then scans it for the lead role, LaTeX and
      script keywords, so injected spreadsheet contents steer them. Here the
      typed text and the augmented text are separate values with separate
      names and never the same variable.

WHAT IS AND IS NOT HERE
The VIEW is complete and the turn is real: `CouncilActions.send` runs
council_core.council_turn (or one Writer, on the fast path). What is not wired
yet — Look Up, Find & Chart, Defer to Vault, instructions, content style, the
per-role override, the specialist pin, verdict responses, Expand in the
DEMO_MODE build, and the switches council_options.SWITCHES marks unavailable —
is shown DISABLED with a "not available in this build yet" tooltip
(_label_unavailable) rather than looking live;
docs/qt_migration/remaining_scope_2026-10-06.md says which batch wires each.

THE CONVERSATION IS KEPT (Batch 1)
Every entry the transcript shows is kept the way the Tk shell keeps it, in the
same files (council_core.conversations): the question the moment it is asked,
everything the turn says when the turn ends — the answer with its provenance —
and anything unsaved when the window closes. Follow-ups see this session's
earlier turns and nothing older ("This conversation only",
council_core.memory_scope); an older session gets in only when the user loads
it from the Sessions tab, and the transcript says so. History lists the
questions both shells were asked.

THE VAULT IS IN FRONT OF THE QUESTION (Batch 2)
Each turn goes through council_core.turn_context.prepare_turn — the Tk shell's
sequence, in council_core: the files the user names, vault and value matches,
the analyst's computed answer (given straight back when it is a direct one, with
no model asked), all in front of the question for the Writer on the default
build and for every role in the full council. The judge routes on what was
typed. The files behind an answer are chips under the transcript; a click
shows the file read-only in the app, with a button to show it in the file
manager.

THE COUNCIL MAP'S PIECES (merged from council-map)
Depth per question, the Tools, Vault and Parallel switches, the vault
passages, the Sage's taught knowledge, documentation for coding questions
and, in the memory scope "all" only, how similar questions were decided
before — gathered side by side, then fitted around prepare_turn's blocks
(CouncilActions.send says the order). After an answer: what it taught
(after_turn — role and project memory only in "all"), the decision, a
passed answer to offer again for the same question (shown, never kept in
this conversation unless the scope lets other conversations in), and who
helped (verdict_log). Typing warms the Writer's and the Judge's models;
Expand with council re-asks a fast answer through the whole council at Deep
depth.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QTextEdit, QVBoxLayout, QWidget)

from council_core import council_options
from council_core import council_turn
from council_core import conversations
from council_core import memory_scope
from council_core import paths
from council_core import transcript as transcript_core

from .. import dialogs, theme
from ..view import ViewHelpers, amp
from ..widgets.source_preview import SourcePreview, reveal_in_file_manager
from ..widgets.transcript import (MirroredTranscript, StreamView,
                                  TranscriptView)


#: Every field a turn sets. `reset_turn()` clears exactly these, and a test
#: asserts this list and that method agree.
#:
#: A4: the Tk reset block clears four fields and misses `_expand_btn`'s enabled
#: state and `_last_fast_question`, so after one fast answer the "Expand with
#: council" button stays live holding a stale question and re-asks the wrong
#: thing. A hand-maintained reset drifts; a declared list that a test checks
#: does not.
PER_TURN_FIELDS = (
    "_last_query",          # what the user typed, before augmentation
    "_last_answer",         # the final text, for Save answer
    "_last_route",          # which route answered
    "_last_sources",        # the source chips
    "_last_verdict_id",     # A3 — the bar is shown only when this is set
    "_last_fast_question",  # A4 — what "Expand with council" would re-ask
    "_force_full_council",
    "_shown_finals",        # final texts already in the transcript this turn
    "_turn_rates",          # speaker -> tokens/s, for the tps label
    "_turn_stats_floor",    # the engine's stats seq when the turn began
)

#: What a control that does nothing yet says when hovered. It is DISABLED
#: and says this rather than being removed: a removed button is a feature
#: the user cannot tell is coming, and a live one that does nothing — or
#: answers a click with "needs the deliberation extracted" in the transcript
#: — reads as broken. docs/qt_migration/remaining_scope_2026-10-06.md lists
#: which batch wires each; its line in _label_unavailable goes when it is.
NOT_AVAILABLE = "{what} — not available in this build yet."

#: Speakers the "▶ who" label names: the personalities' display names, from
#: the one table the transcript uses, and the Judge. The orchestrator's phase
#: markers name a role ("▶ Writer — drafting answer") or a stage ("▶ Round
#: 1/2 — …"); only the first kind is an active personality.
_SPEAKERS = frozenset(council_turn.AGENT_NAMES.values()) | {"Judge"}


def _notice_sink(on_event, on_notice):
    """``notice(who, text, kind)`` for the app's own lines in a turn: to
    ``on_notice`` when the caller takes them apart from the turn's events,
    else to ``on_event`` as an observation."""
    def notice(who: str, text: str, kind: str = "observation") -> None:
        who, kind = who or "Council", kind or "observation"
        if on_notice is not None:
            on_notice(who, text, kind)
        elif on_event is not None:
            from council_core.deliberation import AgentEvent
            on_event(AgentEvent(who, kind, text))
    return notice


def docs_readers(models, docs) -> list:
    """Who reads the documentation block: the Coder and the Writer."""
    if not getattr(docs, "text", ""):
        return []
    from council_core import docs_brief
    return docs_brief.readers(models)


def start_parallel(steps):
    """Start each non-None step on its own thread now; `.wait()` for
    {name: result} (council_core.side_by_side). A turn that never waits —
    an answer straight from the data — leaves them to finish on their own."""
    from council_core import side_by_side
    return side_by_side.start(steps)


def prepare_parallel(steps):
    """Run each non-None step at the same time; {name: result}. A step that
    raises is left out and its error kept under "_errors", so one failed
    lookup costs that lookup, not the question."""
    return start_parallel(steps).wait()


class CouncilActions:
    """What the Council tab can ask the application to do.

    `send` runs a real turn. What is still missing raises NotYetExtracted
    (recording a verdict response), and the view reports it rather than doing
    nothing — one method to replace when it lands.
    """

    class NotYetExtracted(RuntimeError):
        pass

    def __init__(self, vault_dir: Optional[Path] = None, demo_mode: bool = False,
                 conversation=None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.demo_mode = bool(demo_mode)
        #: This launch's conversation (council_core.conversations). The tab
        #: sets it when it is not given; a turn reads it on its worker.
        self.conversation = conversation
        self._models = None
        self._models_problem = ""
        #: This conversation's retrieval memory (council_core.turn_context
        #: .TurnState): the task memo, the files just shown, filename pins,
        #: a warm data index. Built on the first turn, on the worker.
        self._turn_state = None
        self._tools = None
        from council_core.preload import Preloader
        self._preloader = Preloader()
        #: The session of actions used WITHOUT a conversation: one per
        #: instance, so runs are told apart (council-map). With one — the tab
        #: always gives one — its claimed id is the session (`session_id`).
        import time as _time
        self._own_session_id = "qt-" + _time.strftime("%Y%m%d-%H%M%S")
        #: The task memo's condenser: "engine" (prepare_turn's — the main
        #: model, temperature 0), None for the keyword rules only, or a
        #: callable ``llm(prompt) -> str``. The memo itself is the
        #: conversation's one TaskMemory (`task_memory`).
        self.memo_llm: Any = "engine"

    @property
    def session_id(self) -> str:
        """The session this turn's personalities read their earlier turns
        from: the conversation's (renamed with it), else this instance's."""
        if self.conversation is not None:
            return self.conversation.snapshot().session_id
        return self._own_session_id

    @property
    def task_memory(self):
        """The [TASK MEMO] carried from question to question — the one
        prepare_turn updates and puts in front of the question (TurnState's).
        One memo per conversation: council-map's own second memo, updated
        by a second model call and given to every member as well, would
        have been the same block twice."""
        return self.turn_state().task_memory

    def _scope(self) -> str:
        """The memory scope of this conversation (council_core.memory_scope;
        the Qt shell's, "This conversation only", unless the environment
        says otherwise)."""
        if self.conversation is not None:
            return self.conversation.snapshot().scope
        return memory_scope.for_qt()

    # -- implemented -----------------------------------------------------
    def specialists(self):
        """The registry as the pin needs it: labels to show, ids to act on.

        This replaces a specialist_names() that had been returning [] since it
        was written: it called SpecialistRegistry() with no argument, the
        constructor requires vault_dir, and a bare `except Exception` turned
        the TypeError into an empty list. An empty dropdown caused by a
        swallowed error looks exactly like an empty dropdown caused by having
        no specialists.
        """
        from council_core import specialists_ops
        return specialists_ops.load(self.vault_dir)

    def save_answer(self, text: str, path: Path) -> str:
        """Write the last answer out. Returns a line for the transcript."""
        path = Path(path)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return f"Could not save: {exc}"
        return f"Saved to {path}"

    def question_history(self, limit: int = 40) -> Optional[List[str]]:
        """The recent questions, newest first — from question_history.json,
        which both shells write (the Tk History reads the same file) — or
        None when it cannot be read.

        None is not []: "no questions asked yet" is a claim, and with no store
        it is false. This used to import `convo_store`, a module that does not
        exist, so it was always None."""
        try:
            import question_history
            items = question_history.QuestionHistory(self.vault_dir).recent(
                limit)
        except Exception:                                 # noqa: BLE001
            return None
        return [str(item.get("q", "")) for item in items if item.get("q")]

    # -- the turn --------------------------------------------------------
    def models(self):
        """(personalities, problem). Exactly one is meaningful.

        Loaded lazily and kept: building the personalities loads models from
        disk, and a tab the user never sends from should not pay for it.

        The first version of this returned the `council_engine` MODULE, on the
        assumption that the slots were attributes on it. They are not — the
        module exposes build_personalities(), which returns a dict that the Tk
        console unpacks onto itself. Every turn would have reported "No judge
        model is loaded", and no test caught it because they all inject
        stand-ins. See council_core.council_turn.Personalities.
        """
        from council_core import council_turn

        if self._models is None and not self._models_problem:
            # Built for THIS launch's session: the personalities read the
            # conversation's earlier turns under its id (it was "qt" for
            # every launch). apply_to() keeps it current after a rename.
            self._models, self._models_problem = (
                council_turn.load_personalities(
                    self.vault_dir, session_id=self.session_id,
                    scope=self._scope()))
        return self._models, self._models_problem

    def preload(self) -> bool:
        """Warm the Writer's and the Judge's models while the user types
        (council_core.preload). Only once the real personalities are
        loaded — never for stand-ins, and never by loading them itself."""
        models = self._models
        if models is None:
            return False
        import sys
        engine = sys.modules.get("council_engine")
        if engine is None:
            return False
        real = [r for r in ("writer", "judge")
                if isinstance(getattr(models, r, None),
                              engine.PersonalityModel)]
        return self._preloader.kick(real) if real else False

    def tools(self):
        """The members' tools (a tool_kit.ToolSet: each role gets its own
        short list), built on the first turn that asks.

        council_core.council_tools, over this vault. Without it the Tools
        switch reached run_turn as enable_tools with tools=None, and
        ModelAgent ran with an empty tool table: the switch did nothing.

        Without recall_decision unless the memory scope lets other
        conversations in (memory_scope.uses_other_sessions — "all"): it
        reads how earlier conversations' questions were decided into the
        caller's prompt, and "This conversation only" keeps those out.
        """
        if self._tools is None:
            import council_engine
            from council_core import council_tools
            # No LocalRunner and no <vault>/workspace: run_python runs the
            # model's code in the analyst's sandbox (council_tools), which
            # writes nothing. The LocalRunner wrote scratch.py there and ran
            # it with full access to the user's files.
            librarian = council_engine.Librarian(
                self.vault_dir, self.vault_dir / "logs" / "council.log")
            tools = council_tools.make_tools(None, librarian,
                                             self.vault_dir)
            if not memory_scope.uses_other_sessions(self._scope()):
                tools.pop("recall_decision", None)
            # run_tests runs the tests of an OPEN code project; the Council
            # tab names no code roots, so it only ever refuses. Drop it so
            # the Coder does not spend a round on a tool that cannot work
            # here (its help line reads as though a project were open).
            tools.pop("run_tests", None)
            self._tools = tools
        return self._tools

    def send(self, typed_text: str, options, *, on_event=None,
             on_token=None, on_notice=None):
        """Run one turn through council_core.council_turn.

        Takes the TYPED text and a frozen options snapshot — never a widget and
        never the augmented text (A1, A5). Reports progress by calling
        ``on_event``; the caller decides which thread that lands on.

        ``on_notice(who, text, kind)`` takes the app's OWN lines for this turn
        — prepare_turn's notices ("Tracking → goal", "Reading: …", the
        context-window warnings, "Your files could not be read …") and the
        note under an answer from the data. They are not the models' turn
        events: the Tk shell keeps them in the conversation, and so does the
        tab (found in review — they went to the debug log only). Without
        ``on_notice`` they arrive through ``on_event`` as observations.

        WHAT THE COUNCIL IS GIVEN, in this order:

          1. started side by side (start_parallel), council-map's lookups:
             the vault passages that match the question (council_core
             .vault_context — the Vault switch), the Sage's taught knowledge,
             how similar questions were decided before (council_core
             .past_decisions — only when the memory scope lets other
             conversations in, memory_scope.uses_other_sessions), and the
             documentation for a coding question (docs_brief);
          2. while they run, turn_context.begin_turn: the task memo (the
             Intern's model) and the analyst (the Coder's), side by side. A
             question the data answers is answered now — no model asked,
             and the lookups not waited for;
          3. for this question only, the Sage's knowledge on the Sage, the
             past decisions on the Writer, the docs on the Coder and the
             Writer (their standing context, folded into every respond());
          4. the rest of prepare_turn: the memo, the analyst's figures, the
             files the user names, vault and value matches — in front of the
             question, sized to the Writer's call WITH step 3's blocks in it
             (prompt_frame measures the standing context) and at the
             thinking level that call is made at;
          5. the vault passages of step 1, without any from a file step 4
             already put in front of the question and cut to the room the
             Writer's call has left (vault_context.fit), on every member —
             the engine gates them per role — and, as evidence, the Judge.

        One task memo and one analyst: prepare_turn's. council-map ran its own
        of each as well; side by side they were the same block twice, from
        a second model call each (see `task_memory`).
        """
        from council_core import council_turn, past_decisions, vault_context
        from council_core.deliberation import THINK, AgentEvent
        from council_core.docs_brief import DocsBrief

        # What this turn may remember, taken once: the session, the prior the
        # user loaded, the memory scope. Applied to every personality before
        # any model call, so a Load as Prior made while a turn runs counts
        # from the NEXT turn, never halfway through this one. A loaded prior
        # another launch has since renamed is dropped first, and said.
        if self.conversation is not None:
            self.conversation.check_prior()
        snapshot = (self.conversation.snapshot()
                    if self.conversation is not None else None)
        scope = snapshot.scope if snapshot is not None else self._scope()

        # The personalities, pointed at this session BEFORE the context is
        # built: the vault blocks are sized around the Writer's real call —
        # its instructions, the earlier turns it is given, its answer's room
        # (turn_context.prepare_turn(writer=…)). Building them loads no
        # model; that happens at a model's first call, so an answer from
        # the data still asks none.
        models, problem = self.models()
        if models is not None and self.conversation is not None:
            self.conversation.apply_to(models, snapshot)

        def say(who: str, text: str) -> None:
            if on_event is not None and text:
                on_event(AgentEvent(who, "observation", text))

        asking = models is not None
        sage = getattr(models, "sage", None)
        writer = getattr(models, "writer", None)
        deliberate = bool(getattr(options, "deliberate", True))

        # 1. council-map's lookups, started side by side: the wait is the
        # slowest step, not the sum. Earlier conversations' decisions only
        # where the memory scope lets other conversations in: "This
        # conversation only" (the Qt default) keeps every older session out
        # of a prompt unless the user loads it. The Sage's own knowledge
        # base (taught by the user in the Agents tab — facts, and
        # corrections that quote older questions) is left out when memory
        # is off ("none": nothing remembered); in "This conversation only"
        # it is kept as what the user taught it on purpose (decided on the
        # review of the council-map merge; the scope doc, section 8).
        lookups = start_parallel({
            "brief": ((lambda: self.vault_brief(typed_text))
                      if asking and getattr(options, "vault", True)
                      else None),
            "sage_kb": ((lambda: vault_context.sage_block(typed_text,
                                                          self.vault_dir))
                        if sage is not None
                        and scope != memory_scope.NONE else None),
            "past": ((lambda: past_decisions.recall(self.vault_dir,
                                                    typed_text))
                     if asking and memory_scope.uses_other_sessions(scope)
                     else None),
            "docs": ((lambda: self.docs_brief(typed_text, models))
                     if asking else None),
        })
        # 2. Meanwhile, the task memo and the analyst (turn_context
        # .begin_turn): neither needs what the lookups add. A question the
        # data answers (a count, a summary, a fresh derived result) is
        # answered without waiting for the lookups at all: no model is
        # asked, and none of their results would be used.
        think = THINK.get("synthesis") if deliberate else None
        started = self.begin(typed_text, on_event=on_event,
                             on_notice=on_notice)
        if started is not None and started.answerable_from_data and \
                self._fast_answers():
            prepared = self.prepare(
                typed_text, on_event=on_event, on_notice=on_notice,
                writer=writer, think=think, started=started) or started
            result = self._from_data(prepared, on_event=on_event,
                                     on_notice=on_notice)
            return self._finish(typed_text, result, prepared, None, None,
                                models, snapshot)

        got = lookups.wait()
        sage_kb = got.get("sage_kb") or ""
        past = got.get("past") or []
        past_block = past_decisions.block(past)
        docs = got.get("docs") or DocsBrief()

        brief = None
        # 3. Those blocks on the roles that read them, for this question.
        with vault_context.applied([sage] if sage_kb else [], sage_kb), \
                vault_context.applied([writer] if past_block else [],
                                      past_block), \
                vault_context.applied(docs_readers(models, docs), docs.text):
            # 4. The vault, in front of the question — the code the Tk shell
            # ran (council_core.turn_context.prepare_turn, continuing from
            # step 2). The models are sent `prepared.text`; everything that
            # decides WHAT to do reads the typed text (A5). Sized for the
            # Writer's call as it will be made: its plain respond() on the
            # fast path, its synthesis — the step that thinks hardest — in
            # the council.
            prepared = (self.prepare(
                typed_text, on_event=on_event, on_notice=on_notice,
                writer=writer, think=think, started=started)
                if started is not None else None)
            sent = prepared.text if prepared is not None else typed_text

            if prepared is not None and prepared.answerable_from_data and \
                    self._fast_answers():
                # Answered from the data — a count, a summary, a fresh
                # derived result. No model is asked.
                result = self._from_data(prepared, on_event=on_event,
                                         on_notice=on_notice)
                answered_with = None
            elif models is None:
                return council_turn.TurnResult(False, message=problem)
            else:
                # 5. The passages, in what the Writer's call has left. The
                # Judge's copy is not cut to the Writer's room (its calls
                # are its own); neither repeats a file already in front of
                # the question, which the Judge reads too.
                judge_evidence = ""
                raw = got.get("brief")
                brief = self._fit_brief(raw, prepared)
                if brief is not None:
                    judge_evidence = vault_context.evidence(
                        self._fit_brief(raw, prepared, to_room=False))
                    if brief.sources or brief.problem or not (
                            prepared is not None and prepared.injected):
                        say("Librarian", brief.note())
                if sage_kb:
                    n = sum(1 for line in sage_kb.splitlines()
                            if line.strip().startswith(("[", "•")))
                    say("Librarian",
                        f"Sage: {n} item(s) from its knowledge base.")
                if past:
                    say("Librarian", past_decisions.note(past))
                if past_block:
                    judge_evidence = "\n\n".join(
                        x for x in (judge_evidence, past_block) if x)
                say("Librarian", docs.note())
                for name, exc in got.get("_errors", {}).items():
                    say("Librarian", f"{name}: skipped ({exc})")
                with vault_context.applied(
                        vault_context.turn_models(models),
                        brief.text if brief is not None else ""):
                    result = self._answer(sent, typed_text, models, options,
                                          judge_evidence, on_event=on_event,
                                          on_token=on_token)
                answered_with = models

        return self._finish(typed_text, result, prepared, brief,
                            answered_with, models, snapshot)

    def _finish(self, typed_text, result, prepared, brief, answered_with,
                models, snapshot):
        """What every answered turn ends with: the files behind it (chips,
        provenance, what a reuse checks), the pins for the follow-up, the
        session's name, and _after_answer."""
        sources = list(prepared.sources) if prepared is not None else []
        paths = list(prepared.source_paths) if prepared is not None else []
        if brief is not None and brief.sources:
            # The passages' files are behind the answer too: chips, the
            # answer's provenance, and what a reuse checks for changes.
            seen = {self._key(p) for p in paths}
            for path in self._brief_paths(brief.sources):
                if self._key(path) not in seen:
                    seen.add(self._key(path))
                    paths.append(path)
                    sources.append(str(path))
        if prepared is not None or paths:
            result.sources = sources
            result.source_paths = paths
        if result.ok:
            if self._turn_state is not None:
                # The files the answer names are pinned for the follow-up.
                self._turn_state.update_pins(result.answer)
            result.provenance = self._provenance(result, answered_with,
                                                 snapshot)
            result.session_name = self._session_name(typed_text, result,
                                                     answered_with)
            self._after_answer(typed_text, result, models, prepared)
        return result

    def _after_answer(self, typed_text, result, models, prepared) -> None:
        """Before the next question can start (council-map): a deliberated
        answer is a decision to remember (past_decisions — read back only
        in the memory scope "all"), a passed one an answer to offer again
        (answer_reuse — offered in the UI, never put in a prompt), and the
        question a line on who helped (verdict_log).

        The exchange itself is kept by the conversation the tab gives these
        actions (council_core.conversations: the question when it is asked,
        the answer with its provenance at the turn's end). Only actions with
        no conversation append it here (after_turn.record_exchange) — with
        one, it would be in the file, and in the next RECENT CONVERSATION,
        twice."""
        from council_core import after_turn, past_decisions
        if self.conversation is None and models is not None:
            after_turn.record_exchange(models, typed_text, result.answer)
        if getattr(result, "critique", ""):
            past_decisions.record(self.vault_dir, typed_text, result.answer,
                                  result.verdict, result.panel)
            self._keep_for_reuse(typed_text, result, prepared)
            from council_core import verdict_log
            verdict_log.record(self.vault_dir, verdict_log.entry(
                result, getattr(result, "candidates", {}) or {}))

    def _keep_for_reuse(self, question, result, prepared=None) -> None:
        """A passed answer, with the vault files it rested on — the answer's
        sources (prepare_turn's and the passages') and, when the analyst
        computed from the data, every data file — for answer_reuse."""
        from council_core import answer_reuse
        try:
            sources = [Path(p) for p in (getattr(result, "source_paths",
                                                 None) or [])]
            if prepared is not None and getattr(prepared, "analyst_block",
                                                None):
                import vault_analyst
                from council_core.analyst_step import data_folder
                sources += vault_analyst.list_data_files(
                    [data_folder(self.vault_dir)])
            answer_reuse.record(self.vault_dir, question, result.answer,
                                result.verdict, sources)
        except Exception:                                 # noqa: BLE001
            pass

    def reusable(self, question: str):
        """An earlier passed answer to this same question whose files are
        unchanged (council_core.answer_reuse), or None. Fast: one file.
        The tab SHOWS it and offers it; it never goes into a prompt."""
        from council_core import answer_reuse
        try:
            return answer_reuse.find(self.vault_dir, question)
        except Exception:                                 # noqa: BLE001
            return None

    def _answer(self, sent, typed_text, models, options, judge_evidence, *,
                on_event=None, on_token=None):
        """The answer itself: one personality, or the council. ``sent`` is
        what the models are given (the vault in front of the question);
        ``typed_text`` is what the Judge routes on and the depth is decided
        on (A5)."""
        from council_core import council_turn
        if not getattr(options, "deliberate", True):
            # The fast path: one personality, no panel, no verdict. It is a
            # real answer and it is NOT a deliberation, so it produces no
            # verdict id — which is what keeps the verdict bar honest (A3).
            # It gets the vault context: the default build is this path, and
            # without it the default build was a chatbot.
            return self._direct(
                sent, models, on_event=on_event,
                on_token=on_token if getattr(options, "stream", True) else None)

        enable_tools = bool(getattr(options, "tools", False))
        return council_turn.run_turn(
            sent, models,
            enable_tools=enable_tools,
            tools=self.tools() if enable_tools else None,
            parallel_members=bool(getattr(options, "parallel", False)),
            depth=str(getattr(options, "depth", "auto") or "auto"),
            extra_ctx=({"judge_evidence": judge_evidence}
                       if judge_evidence else None),
            on_event=on_event,
            on_token=on_token if getattr(options, "stream", True) else None,
            route_text=typed_text)

    # -- the vault context ---------------------------------------------
    def turn_state(self):
        """This conversation's retrieval memory, built on first use."""
        if self._turn_state is None:
            from council_core import turn_context
            self._turn_state = turn_context.TurnState(self.vault_dir)
        return self._turn_state

    def begin(self, typed_text: str, *, on_event=None, on_notice=None):
        """council_core.turn_context.begin_turn for this conversation — the
        task memo and the analyst, prepare_turn's first two steps — its
        notices reported through ``on_notice`` (see send). None if it could
        not run at all: the question is then asked without the vault, and
        the transcript says so."""
        notice = _notice_sink(on_event, on_notice)
        try:
            from council_core import turn_context
            return turn_context.begin_turn(
                typed_text, self.turn_state(),
                condense=self._condenser(), on_notice=notice)
        except Exception as exc:                          # noqa: BLE001
            self._unread(notice, exc)
            return None

    def prepare(self, typed_text: str, *, on_event=None, on_notice=None,
                writer=None, think=None, started=None):
        """council_core.turn_context.prepare_turn for this conversation, its
        notices reported through ``on_notice`` (see send), its vault blocks
        sized around ``writer``'s call at thinking level ``think``;
        ``started`` is what `begin` returned (the memo and the analyst are
        then not run again). None if it could not run at all — the question
        is then asked without the vault, and the transcript says so (a turn
        must not die because a file would not open)."""
        notice = _notice_sink(on_event, on_notice)

        try:
            from council_core import turn_context
            return turn_context.prepare_turn(
                typed_text, self.turn_state(),
                condense=self._condenser(),
                on_notice=notice, writer=writer, think=think,
                started=started)
        except Exception as exc:                          # noqa: BLE001
            self._unread(notice, exc)
            return None

    @staticmethod
    def _unread(notice, exc) -> None:
        notice("Council", f"Your files could not be read for this "
                          f"question ({exc!r}), so it is answered "
                          f"without them.")

    def _condenser(self):
        """The task memo's model call (``memo_llm``): "engine" is
        prepare_turn's own (turn_context.condense_with_local_model — the
        Intern's model, as council-map asked it)."""
        llm = self.memo_llm
        if llm == "engine":
            from council_core import turn_context
            return turn_context.condense_with_local_model
        return llm if callable(llm) else None

    def _fit_brief(self, brief, prepared, *, to_room: bool = True):
        """council-map's vault passages, fitted behind prepare_turn's blocks:
        none from a file already in front of the question, and — with
        ``to_room`` — no more than the Writer's call has room left for
        (prepared.budget: what the call sends and what its window allows
        beside the reply, counted the way the engine fits it; nothing when
        it has none). None stays None."""
        from council_core import vault_context
        if brief is None or not (getattr(brief, "text", "") or "").strip():
            return brief
        limit = None
        budget = (getattr(prepared, "budget", None) or {}) if to_room else {}
        try:
            if "safe_input" in budget and "input_tokens" in budget:
                import council_engine
                # The fold adds "COUNCIL CONTEXT:" and blank lines; a little
                # is kept for them, as prepare_turn keeps for its own tags.
                left = (int(budget["safe_input"])
                        - int(budget["input_tokens"]) - 64)
                limit = max(0, int(left * council_engine._budget_cpt()))
        except Exception:                                 # noqa: BLE001
            limit = None
        in_front = {self._key(p) for p in
                    (getattr(prepared, "source_paths", None) or [])}

        def skip(source: str) -> bool:
            return any(self._key(p) in in_front
                       for p in self._brief_paths([source]))

        return vault_context.fit(brief, limit, skip=skip)

    def _brief_paths(self, sources) -> List[Path]:
        """The vault files the passages came from (the vault search names
        them relative to the vault): existing, in the vault, and not the
        app's own (conversation_logger.is_protected_path)."""
        import conversation_logger
        import path_contain
        out: List[Path] = []
        for source in sources or ():
            path = Path(str(source or ""))
            if not str(source or "").strip() or str(source) == "?":
                continue
            if not path.is_absolute():
                path = self.vault_dir / path
            try:
                if (path.is_file()
                        and path_contain.is_under(path, self.vault_dir)
                        and not conversation_logger.is_protected_path(
                            path, self.vault_dir)):
                    out.append(path)
            except Exception:                             # noqa: BLE001
                continue
        return out

    @staticmethod
    def _key(path) -> str:
        try:
            return str(Path(path).resolve()).lower()
        except Exception:                                 # noqa: BLE001
            return str(path).lower()

    def _fast_answers(self) -> bool:
        from council_core import turn_context
        return turn_context.fast_answers_enabled(self.vault_dir)

    def forget(self, typed_text: str) -> Optional[str]:
        """"forget WORD" — a close match the turn's notice offered, put on
        the vault's list never to use again (turn_context.forget_command):
        what to say, or None when ``typed_text`` is not that command (a
        conversation that was offered nothing has nothing to forget)."""
        if self._turn_state is None:
            return None
        from council_core import turn_context
        return turn_context.forget_command(typed_text, self._turn_state)

    def _from_data(self, prepared, *, on_event=None, on_notice=None):
        """The answer the analyst computed, given as the answer, and the
        app's note under it (a notice — see send)."""
        from council_core import council_turn, turn_context
        from council_core.deliberation import AgentEvent
        events = [AgentEvent("Writer", "final", prepared.fast_answer),
                  AgentEvent("Council", "observation",
                             turn_context.FAST_ANSWER_NOTE)]
        if on_event is not None:
            on_event(events[0])
        _notice_sink(on_event, on_notice)("Council",
                                          turn_context.FAST_ANSWER_NOTE)
        return council_turn.TurnResult(True, answer=prepared.fast_answer,
                                       route="data", events=events)

    def _provenance(self, result, models, snapshot) -> dict:
        """Where this answer came from, saved with it (see TurnResult)."""
        council = result.route not in ("direct", "data")
        roles = (list(result.panel or []) if council
                 else ["writer"] if result.route == "direct" else [])
        backends = {}
        for role in roles + (["judge"] if council else []):
            key = getattr(getattr(models, role, None), "backend_key", None)
            if isinstance(key, str):
                backends[role] = key
        return {
            "shell": "qt",
            "route": result.route,
            "panel": list(result.panel or []),
            "backends": backends,
            "memory": snapshot.scope if snapshot else memory_scope.for_qt(),
            "prior_session": snapshot.prior_session_id if snapshot else None,
            "sources": [str(p) for p in (result.source_paths or [])]
            or list(result.sources or []),
            "verdict_id": result.verdict_id or None,
        }

    def _session_name(self, typed_text: str, result, models) -> str:
        """The name for a session that has none yet, or "".

        After a deliberation the judge names it, as in the Tk shell
        (JudgeModel.name_session — the judge is loaded and has just ruled).
        On the fast path — the default build, one Writer — the name is made
        from the question's words: Tk names nothing there, and asking a
        model would cost a second call or a second model loaded for a file
        name."""
        conversation = self.conversation
        if conversation is None or conversation.named:
            return ""
        judge = getattr(models, "judge", None)
        if result.route not in ("direct", "data") and hasattr(
                judge, "name_session"):
            try:
                name = conversations.clean_name(
                    judge.name_session(typed_text, result.critique or ""))
                if name:
                    return name
            except Exception:                             # noqa: BLE001
                pass
        return conversations.name_from_question(typed_text)

    def docs_brief(self, question: str, models):
        """The DOCUMENTATION block for a coding question (blocking: a
        keyword search on the documentation servers, no model)."""
        from council_core import docs_brief
        return docs_brief.build(question, docs_brief.route_of(models,
                                                               question))

    def vault_brief(self, question: str):
        """The VAULT CONTEXT for a question (blocking: a search)."""
        from council_core import vault_context
        return vault_context.build(question, self.vault_dir)

    def _direct(self, typed_text: str, models, *, on_event=None,
                on_token=None):
        """One personality answering directly, with no council.

        ``on_token(who, token)`` streams the Writer's answer as the council's
        members stream theirs. It was never passed here, so with one Writer
        the Stream tokens box did nothing and the answer came all at once."""
        from council_core import council_turn
        from council_core.deliberation import AgentEvent

        writer = getattr(models, "writer", None)
        if writer is None:
            return council_turn.TurnResult(
                False, message="No writer model is loaded.")
        if on_event is not None:
            # BEFORE the call, so the tab can say who is answering while it
            # waits ("▶ Writer"). A thought: progress, never a transcript line.
            on_event(AgentEvent("Writer", "thought", "Answering…"))
        try:
            if on_token is not None:
                answer = writer.respond(
                    typed_text,
                    token_callback=lambda token: on_token("Writer", token))
            else:
                answer = writer.respond(typed_text)
        except Exception as exc:                          # noqa: BLE001
            return council_turn.TurnResult(
                False, message=f"The answer failed: {exc!r}", error=exc)
        event = AgentEvent("Writer", "final", answer)
        if on_event is not None:
            on_event(event)
        return council_turn.TurnResult(True, answer=answer, route="direct",
                                       events=[event])

    def last_call_stats(self) -> dict:
        """council_engine.last_call_stats() — what the most recent model call
        cost (gen_tok_s, role, seq) — when the engine is loaded; {} when it is
        not. Never imports it: this is read on the GUI thread, and a turn
        that has called a model has loaded it already."""
        import sys
        engine = sys.modules.get("council_engine")
        getter = getattr(engine, "last_call_stats", None) if engine else None
        try:
            return dict(getter() or {}) if callable(getter) else {}
        except Exception:                                 # noqa: BLE001
            return {}

    def learn(self, result):
        """What this answered question leaves behind (after_turn.learn).
        Blocking: the tab calls it on a worker."""
        from council_core import after_turn
        models, _problem = self.models()
        if models is None:
            return None
        return after_turn.learn(models, result, self.vault_dir)

    def record_verdict_response(self, verdict_id: str, agreed: bool,
                                objection: str = ""):
        """Stamp a verdict BY ID.

        A3: the Tk version stamps the last line of verdict_history.jsonl, which
        after a direct-mode turn belongs to an unrelated earlier deliberation.
        Taking an id makes that mistake impossible to express.
        """
        raise self.NotYetExtracted(
            "recording a verdict response needs the verdict store extracted")


class CouncilTab(ViewHelpers, QWidget):
    """The transcript, the judge panel, the stream, and the input bar."""

    def __init__(self, window=None, actions: Optional[CouncilActions] = None,
                 demo_mode: bool = False):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.demo_mode = bool(demo_mode)
        self.actions = actions or CouncilActions(demo_mode=demo_mode)
        self._tokens = theme.tokens("dark")

        self._turn_active = False
        self._checkboxes = {}
        #: what -> the widgets labelled "not available in this build yet".
        self.unavailable = {}
        self._specialists = None

        # This launch's conversation: the one the actions were given, else a
        # new one in their vault. Shared through the window so the Sessions
        # tab can load an older session into it, and closed with the window.
        self.conversation = (self.actions.conversation
                             or conversations.Conversation(
                                 self.actions.vault_dir))
        self.actions.conversation = self.conversation
        if window is not None and getattr(window, "conversation", None) is None:
            window.conversation = self.conversation
        hooks = getattr(window, "close_hooks", None)
        if isinstance(hooks, list):
            hooks.append(self.close_conversation)
        self.conversation.subscribe(self._on_conversation)
        #: What the running turn has said, saved when the turn ends (None
        #: when no turn is running: entries are saved as they come).
        self._pending: Optional[list] = None
        self._turn_provenance: Optional[dict] = None

        # The Profile box shows what the engine will do (it cannot be
        # changed here yet — see council_options.SWITCHES): in "This
        # conversation only" no profile reaches a prompt.
        self._opts = council_options.CouncilOptions.defaults(
            demo_mode=self.demo_mode,
            profile_enabled=council_options.profile_reaches_prompt(
                self.conversation.scope))
        for field in PER_TURN_FIELDS:
            setattr(self, field, None)

        # Other views that show this transcript too — the Dream3D chat adds
        # itself. Empty until one does, which costs nothing.
        self.mirror = MirroredTranscript()
        # Called (on the GUI thread) after a pipeline command created or
        # modified a pipeline, so a picker can rescan.
        self.pipelines_changed: List[Callable[[], None]] = []
        self._pipeline_chat = None
        self._model_chat = None
        # Asks before a script take-over (_take_over). Yes only on a click;
        # COUNCIL_NO_DIALOGS answers No.
        self.confirm = dialogs.confirm

        self._build()
        self.refresh_specialists()
        if self.conversation.unsaved:
            # The vault cannot keep conversations (read-only, permission
            # denied): the tab still opens and answers, and says so.
            self._on_conversation("not_saved",
                                  message=self.conversation
                                  .not_saved_message())

    # ==================================================================
    # Layout
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._transcript_side())
        right = self._judge_side()
        # DEMO_MODE is a single-personality Q&A: no panel, no verdicts, so the
        # right pane is not useful to show. The widgets are still CREATED, as
        # in Tk, so anything that reaches for them stays safe.
        if not self.demo_mode:
            split.addWidget(right)
            split.setSizes([700, 380])
        else:
            right.setParent(self)
            right.hide()
        outer.addWidget(split, 1)

        outer.addWidget(self._input_side())
        self._label_unavailable()

    def _label_unavailable(self) -> None:
        """Disable every control that does nothing yet, and say so on hover.

        Each of these used to look live: a click either did nothing or put
        "<what> needs the deliberation extracted — it is the rest of phase 6"
        into the transcript. The handlers stay (a later batch fills them in);
        only the controls' state changes."""
        def label(what: str, *widgets, why: str = "") -> None:
            tip = NOT_AVAILABLE.format(what=what) + (f" {why}" if why else "")
            for widget in widgets:
                widget.setEnabled(False)
                widget.setToolTip(tip)
            self.unavailable.setdefault(what, []).extend(widgets)

        label("Find & Chart", self.find_chart_btn)
        label("Look Up", self.look_up_btn)
        label("Defer to Vault", self.defer_btn)
        if self.demo_mode:
            # The full build expands a fast answer through the whole council
            # at Deep depth (on_expand_with_council); this build answers with
            # one Writer and no panel, so an expand would repeat the answer.
            label("Expand with council", self.expand_btn,
                  why="This build answers with a single model, so there is "
                      "no fuller council to send the question to.")
        label("Council instructions", self.inst_name, self.inst_text,
              self.inst_add_btn, self.inst_manage_btn)
        label("Content style", self.content_style_btn)
        label("The per-role model override", self.backend_box,
              why="Every role answers from the model set in the Models tab.")
        label("Asking a specialist", self.specialist_box,
              why="Every question is answered without a specialist.")
        label("Responding to a verdict", self.vfb_agree, self.vfb_disagree,
              self.redeliberate_btn,
              why="Each verdict is recorded in the verdict history, but "
                  "agreeing or disagreeing with one is not wired up yet.")
        for switch in council_options.SWITCHES:
            box = self._checkboxes.get(switch.key)
            if box is not None and not switch.available:
                label(switch.name, box, why=switch.unavailable_why)

    def _transcript_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Transcript"))
        self.transcript = TranscriptView()
        layout.addWidget(self.transcript, 1)
        layout.addWidget(self._sources_bar())
        return panel

    #: How many source chips one answer shows before "+N more".
    MAX_CHIPS = 12

    def _sources_bar(self) -> QWidget:
        """The files behind the last answer, one chip each — click to see
        the file, read-only, in the app (preview_source), with a button that
        shows it in the file manager. Hidden while there are none, so an
        answer that used no file looks like one."""
        self.sources_frame = QFrame()
        self._sources_row = QHBoxLayout(self.sources_frame)
        self._sources_row.setContentsMargins(0, 2, 0, 2)
        self.sources_frame.hide()
        self.source_chips: List[QPushButton] = []
        #: What a chip click does with its path.
        self.open_source = self.preview_source
        #: Shows a file in the file manager ("Show in folder"). Replaced in
        #: tests: the desktop is not theirs to open.
        self.reveal_source = reveal_in_file_manager
        #: The one preview window, reused (built on the first click).
        self.source_preview: Optional[SourcePreview] = None
        return self.sources_frame

    def preview_source(self, path) -> None:
        """Show a file behind the answer, read-only, in the app (read on a
        worker — the click returns at once); a folder is listed. One that
        has moved or gone since is said in the transcript instead. (A folder
        chip said "is no longer there": only files were taken — found in
        review.)"""
        path = Path(path)
        if not path.exists():
            self.append("Council", f"{path.name} is no longer there "
                                   f"({path.parent}).", "observation",
                        keep=False)
            return
        if self.source_preview is None:
            self.source_preview = SourcePreview(
                self, reveal=lambda p: self.reveal_source(p))
        if path.is_dir():
            self.source_preview.show_folder(path)
        else:
            self.source_preview.show_file(path)

    def show_sources(self, paths) -> None:
        """Chips for ``paths`` under the transcript, replacing the last
        answer's; nothing when there are none."""
        self.clear_sources()
        paths = [Path(p) for p in (paths or [])]
        if not paths:
            return
        title = QLabel("Sources:")
        title.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        self._sources_row.addWidget(title)
        for path in paths[:self.MAX_CHIPS]:
            chip = QPushButton(f"{'📁' if path.is_dir() else '📄'} "
                               f"{path.name or path}")
            chip.setFlat(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip(f"{path}\nClick to preview it here (read-only).")
            chip.clicked.connect(lambda _=False, p=path: self.open_source(p))
            self._sources_row.addWidget(chip)
            self.source_chips.append(chip)
        if len(paths) > self.MAX_CHIPS:
            more = QLabel(f"(+{len(paths) - self.MAX_CHIPS} more)")
            more.setStyleSheet(f"color: {self._tokens['muted_fg']};")
            self._sources_row.addWidget(more)
        self._sources_row.addStretch(1)
        self.sources_frame.show()

    def clear_sources(self) -> None:
        while self._sources_row.count():
            item = self._sources_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.source_chips = []
        self.sources_frame.hide()

    def _judge_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Judge Panel"))
        self.judge_box = QTextEdit()
        self.judge_box.setReadOnly(True)
        layout.addWidget(self.judge_box, 1)

        layout.addWidget(self._verdict_bar())
        layout.addWidget(self._objection_panel())

        layout.addWidget(QLabel("Live Token Stream"))
        self.stream_box = StreamView()
        layout.addWidget(self.stream_box, 1)
        return panel

    def _verdict_bar(self) -> QWidget:
        self.vfb_frame = QFrame()
        row = QHBoxLayout(self.vfb_frame)
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(QLabel("Do you agree with the verdict?"))
        self.vfb_agree = self._button(row, amp("✓ Agree"), self.on_agree)
        self.vfb_disagree = self._button(row, amp("✗ Disagree"),
                                         self.on_disagree_open)
        row.addStretch(1)
        self.vfb_frame.hide()                 # A3 — shown only with a verdict
        return self.vfb_frame

    def _objection_panel(self) -> QWidget:
        self.vfb_detail = QFrame()
        layout = QVBoxLayout(self.vfb_detail)
        layout.setContentsMargins(0, 2, 0, 0)
        prompt = QLabel("Your objection (the council will re-deliberate "
                        "with it):")
        prompt.setStyleSheet(f"color: {self._tokens['warning']};")
        layout.addWidget(prompt)
        self.objection = QPlainTextEdit()
        self.objection.setMaximumHeight(70)
        layout.addWidget(self.objection)
        row = QHBoxLayout()
        self.redeliberate_btn = self._button(
            row, amp("↩ Re-deliberate with objection"),
            self.on_disagree_submit)
        self._button(row, "Cancel", self.on_disagree_cancel)
        hint = QLabel("Ctrl+Enter to submit")
        hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(hint)
        row.addStretch(1)
        layout.addLayout(row)
        self._shortcut("Ctrl+Return", self.on_disagree_submit, self.objection)
        self.vfb_detail.hide()
        return self.vfb_detail

    def _input_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Input"))
        self.input = QPlainTextEdit()
        self.input.setMaximumHeight(96)
        layout.addWidget(self.input)
        # Typing a question warms the models it will need (preload).
        self.input.textChanged.connect(self._on_typing)
        self._shortcut("Ctrl+Return", self.on_send, self.input)

        layout.addLayout(self._action_row())
        layout.addLayout(self._toggle_row(1))
        if not self.demo_mode:
            layout.addLayout(self._toggle_row(2))
        layout.addWidget(self._instruction_bar())
        layout.addWidget(self._save_panel())
        layout.addWidget(self._clarification_panel())
        layout.addWidget(self._reuse_bar())
        layout.addLayout(self._override_row())
        return panel

    def _reuse_bar(self) -> QWidget:
        """[Use it] [Ask the council again] under an earlier answer shown
        for a repeated question (council_core.answer_reuse)."""
        self.reuse_frame = QFrame()
        row = QHBoxLayout(self.reuse_frame)
        row.setContentsMargins(0, 4, 0, 0)
        row.addWidget(QLabel("Earlier answer shown above."))
        self._button(row, amp("✓ Use it"), self.on_reuse_accept)
        self._button(row, amp("↻ Ask the council again"), self.on_reuse_again)
        row.addStretch(1)
        self.reuse_frame.hide()
        self._reuse_question = ""
        self._reuse_answer = ""
        return self.reuse_frame

    def on_reuse_accept(self) -> None:
        """Use the earlier answer. Where the memory scope lets other
        conversations in ("all"), it joins this conversation — the question
        and the answer, kept — so a follow-up sees it. In "This conversation
        only" it stays shown and is not kept: it came from another
        conversation, and the transcript says a follow-up will not see it."""
        question, self._reuse_question = self._reuse_question, ""
        answer, self._reuse_answer = self._reuse_answer, ""
        self.reuse_frame.hide()
        if not question:
            return
        scope = self.conversation.scope
        if memory_scope.uses_other_sessions(scope):
            # Shown (and logged) when offered; kept now.
            self.conversation.record("User", question, logged=True)
            self.conversation.record("Writer", answer, logged=True)
            return
        self.append("Council", "The earlier answer came from another "
                               "conversation, so follow-up questions will "
                               f"not see it ({memory_scope.describe(scope)})"
                               ". Ask the council again to make it part of "
                               "this one.", "observation", keep=False)

    def on_reuse_again(self) -> None:
        question, self._reuse_question = self._reuse_question, ""
        self._reuse_answer = ""
        self.reuse_frame.hide()
        if question:
            self.input.setPlainText(question)
            self._send(reuse=False)

    def _action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.send_btn = self._button(row, "Send  [Ctrl+Enter]", self.on_send)
        # The bare caption: _button escapes it. Wrapped in amp() here too it
        # rendered as "Find && Chart" (found in review).
        self.find_chart_btn = self._button(row, "📊 Find & Chart",
                                           self.on_find_and_chart)
        self.look_up_btn = self._button(row, amp("🔍 Look Up"),
                                        self.on_look_up)
        self._button(row, "Clear", self.on_clear_input)
        self.defer_btn = self._button(row, amp("⤓ Defer to Vault"),
                                      self.on_defer_to_vault)
        # Re-asks the last FAST question through the full council (A4:
        # per-turn state, reset in reset_turn()). Enabled by finish_turn after
        # a fast answer in the full build; labelled unavailable in DEMO_MODE.
        self.expand_btn = self._button(row, amp("⤢ Expand with council"),
                                       self.on_expand_with_council)
        self.expand_btn.setEnabled(False)
        self.save_btn = self._button(row, amp("💾 Save answer"),
                                     self.on_save_answer)
        self.history_btn = self._button(row, amp("🕘 History"),
                                        self.on_history)
        self._button(row, amp("💡 What can I ask?"), self.on_examples)
        row.addStretch(1)

        # Fed by the turn's own events: who is answering now ("▶ Writer",
        # cleared when the turn ends), and how fast the models generated
        # this turn (council_engine.last_call_stats). Tk's colours.
        self.agent_label = QLabel("")
        self.agent_label.setStyleSheet("color: #a6e3a1;")
        row.addWidget(self.agent_label)
        self.tps_label = QLabel("")
        self.tps_label.setStyleSheet("color: #d32f2f;")
        self.tps_label.setToolTip("Generation speed this turn, from the "
                                  "engine's own measurement of each call.")
        row.addWidget(self.tps_label)
        # What the council remembers between questions, said plainly — the
        # user's decision, and the first thing to check when a follow-up
        # "forgot" or "knew" something.
        self.memory_label = QLabel("")
        self.memory_label.setStyleSheet(
            f"color: {self._tokens['muted_fg']};")
        self._show_memory()
        row.addWidget(self.memory_label)
        self.status = QLabel("● idle")
        self.status.setStyleSheet(f"color: {self._tokens['success']};")
        row.addWidget(self.status)
        return row

    def _toggle_row(self, which: int) -> QHBoxLayout:
        """One toolbar row, built from council_core.council_options.

        The switches, their defaults and which of them DEMO_MODE hides are all
        described in one place, so the two front ends cannot disagree about the
        state the tab opens in — which no test would catch, because no test
        looks at a checkbox's initial value.
        """
        row = QHBoxLayout()
        if which == 2:
            label = QLabel("Personalities:")
            label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
            row.addWidget(label)
        for switch in council_options.visible_switches(self.demo_mode,
                                                       row=which):
            box = QCheckBox(amp(switch.label))
            box.setChecked(getattr(self._opts, switch.key))
            box.toggled.connect(
                lambda on, key=switch.key: self.on_toggle(key, on))
            if switch.hint:
                box.setToolTip(switch.hint)
            row.addWidget(box)
            self._checkboxes[switch.key] = box
            if switch.hint and which == 2:
                hint = QLabel(f"({switch.hint})")
                hint.setStyleSheet(f"color: {self._tokens['muted_fg']};")
                row.addWidget(hint)
        row.addStretch(1)
        return row

    def _instruction_bar(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 2, 0, 0)
        label = QLabel(amp("⚡ Instruction:"))
        row.addWidget(label)
        self.inst_name = QLineEdit()
        self.inst_name.setPlaceholderText("Name (optional)")
        self.inst_name.setFixedWidth(140)
        row.addWidget(self.inst_name)
        self.inst_text = QLineEdit()
        self.inst_text.setPlaceholderText(
            "e.g. always show your working as a table")
        row.addWidget(self.inst_text, 1)
        self.inst_text.returnPressed.connect(self.on_add_instruction)
        self.inst_add_btn = self._button(row, "Add  [Enter]",
                                         self.on_add_instruction)
        self.inst_manage_btn = self._button(row, amp("Manage…"),
                                            self.on_manage_instructions)
        self.content_style_btn = self._button(row, amp("Content Style…"),
                                              self.on_content_style)
        self.inst_active = QLabel("")
        self.inst_active.setStyleSheet(f"color: {self._tokens['success']};")
        row.addWidget(self.inst_active)
        return box

    def _save_panel(self) -> QWidget:
        self.save_frame = QFrame()
        row = QHBoxLayout(self.save_frame)
        row.setContentsMargins(0, 4, 0, 2)
        title = QLabel(amp("💾 Save output as:"))
        row.addWidget(title)
        for label, suffix in ((amp("📄 Text file (.txt)"), ".txt"),
                              (amp("📝 Markdown (.md)"), ".md"),
                              (amp("📐 LaTeX (.tex)"), ".tex")):
            self._button(row, label,
                         lambda _=False, s=suffix: self.on_save_output(s))
        row.addStretch(1)
        self._button(row, amp("✕"), self.save_frame.hide)
        self.save_frame.hide()                # shown when output is ready
        return self.save_frame

    def _clarification_panel(self) -> QWidget:
        self.clarif_frame = QFrame()
        layout = QVBoxLayout(self.clarif_frame)
        layout.setContentsMargins(0, 2, 0, 0)
        title = QLabel(amp("🤔 A personality needs your input:"))
        layout.addWidget(title)
        self.clarif_question = QLabel("")
        self.clarif_question.setWordWrap(True)
        layout.addWidget(self.clarif_question)
        row = QHBoxLayout()
        self.clarif_answer = QLineEdit()
        row.addWidget(self.clarif_answer, 1)
        self.clarif_answer.returnPressed.connect(self.on_clarify)
        self._button(row, "Answer  [Enter]", self.on_clarify)
        self._button(row, "Skip", lambda: self.on_clarify(skip=True))
        layout.addLayout(row)
        self.clarif_frame.hide()
        return self.clarif_frame

    def _override_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel("Override: ")
        label.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(label)
        self.backend_box = QComboBox()
        self.backend_box.addItems(council_options.BACKEND_CHOICES)
        row.addWidget(self.backend_box)

        row.addSpacing(12)
        depth = QLabel("Depth:")
        depth.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(depth)
        self.depth_box = QComboBox()
        for label, level in council_options.DEPTH_CHOICES:
            self.depth_box.addItem(label, level)
        self.depth_box.setToolTip(council_options.DEPTH_HINT)
        row.addWidget(self.depth_box)

        row.addSpacing(12)
        ask = QLabel("Ask:")
        ask.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(ask)
        self.specialist_box = QComboBox()
        self.specialist_box.setMinimumWidth(180)
        row.addWidget(self.specialist_box)
        row.addStretch(1)
        return row

    # -- small helpers ---------------------------------------------------
    # ==================================================================
    # The turn
    # ==================================================================
    def options(self) -> council_options.CouncilOptions:
        """A frozen snapshot of the switches, taken on the GUI thread.

        A1: this is the whole answer to the thirteen off-thread Tk variable
        reads. The worker is handed the result of this call and never sees a
        widget, so there is nothing for it to race on.
        """
        snapshot = council_options.CouncilOptions(**{
            key: box.isChecked() for key, box in self._checkboxes.items()})
        # Anything DEMO_MODE hides keeps its default rather than whatever a
        # missing checkbox would imply.
        for switch in council_options.SWITCHES:
            if switch.key not in self._checkboxes:
                setattr(snapshot, switch.key, getattr(self._opts, switch.key))
        box = getattr(self, "depth_box", None)
        if box is not None:
            snapshot.depth = box.currentData() or "auto"
        return snapshot.effective(self.demo_mode)

    def begin_turn(self) -> bool:
        """Claim the turn, or refuse out loud.

        A2: `_send` in Tk has no guard and is reachable eight ways, and two
        concurrent turns patch and restore `model.respond` over each other.
        Refusing silently would read as a broken button, so it says so.
        """
        if self._turn_active:
            # No "or press Stop": this tab has no Stop button (stopping a
            # running turn is not built in either shell yet), and pointing
            # at a control that is not there is worse than saying nothing.
            self.append("Council", "A turn is already running — wait for it "
                                   "to finish.", "observation")
            return False
        self._turn_active = True
        self.send_btn.setEnabled(False)
        self.set_status("● thinking", self._tokens["accent"])
        return True

    def end_turn(self) -> None:
        self._turn_active = False
        # The turn is over, whatever became of it: what it said is saved now.
        self._save_pending()
        self.send_btn.setEnabled(True)
        self.set_status("● idle", self._tokens["success"])
        # Nobody is answering any more. The speed stays: it describes the
        # turn that just finished, which is when it is worth reading.
        self.agent_label.setText("")

    def reset_turn(self) -> None:
        """Clear everything the previous turn left behind.

        A4: one place, driven by PER_TURN_FIELDS, with a test that the list and
        this method agree. The Tk reset clears four fields and misses two.
        """
        for field in PER_TURN_FIELDS:
            setattr(self, field, None)
        self.expand_btn.setEnabled(False)
        self.save_frame.hide()
        self.clarif_frame.hide()
        self.hide_verdict_bar()
        self.stream_box.clear_all()
        self.clear_sources()

    def _offer_reuse(self, typed: str) -> bool:
        """Show an earlier passed answer to the same question, with
        [Use it] / [Ask the council again]. True when one was shown.

        SHOWN, NOT KEPT (keep=False): the answer is another conversation's,
        and a kept line is in the next prompt's RECENT CONVERSATION — the way
        an older session must not get in under "This conversation only"
        (council_core.memory_scope). Using it keeps it only where the scope
        lets other conversations in (on_reuse_accept); asking again sends
        the question as a new turn, which keeps it then."""
        find = getattr(self.actions, "reusable", None)
        hit = find(typed) if find is not None else None
        if hit is None:
            return False
        self.append("User", typed, keep=False)
        self.input.clear()
        self.append("Librarian", hit.note(), "observation", keep=False)
        self.append("Writer", hit.answer, keep=False)
        self._reuse_question = typed
        self._reuse_answer = hit.answer
        self.reuse_frame.show()
        return True

    def _on_typing(self) -> None:
        preload = getattr(self.actions, "preload", None)
        if preload is not None and self.input.toPlainText().strip():
            try:
                preload()
            except Exception:                             # noqa: BLE001
                pass

    def on_send(self) -> None:
        # No arguments: QPushButton.clicked passes `checked`, which a
        # keyword here would swallow.
        self._send(reuse=True)

    def _send(self, *, reuse: bool, full: bool = False) -> None:
        """`reuse`: offer an earlier passed answer first (answer_reuse).
        `full`: the whole council at Deep depth, whatever the switches say
        (Expand with council)."""
        typed = self.input.toPlainText().strip()
        if not typed:
            return
        # "Expand with council" re-sends an earlier QUESTION through here
        # (full, and _force_full_council); only words typed for this send can
        # ask for a take-over — an expanded "take over x.py" is answered by
        # the pipeline chat, never acted on (test_expand_with_council_is_not_a_door).
        if not (full or self._force_full_council) and self._take_over(typed):
            return
        if self._send_command(typed):
            return
        if reuse and not self._turn_active and self._offer_reuse(typed):
            return
        if not self.begin_turn():
            return
        self.reset_turn()
        # Only calls made from here on describe THIS turn's speed, and the
        # label is cleared with the rates it shows: kept, the previous turn's
        # "Writer 40 tok/s" stood all through a turn that then failed — and
        # after it — under a tooltip that says "this turn" (found in review).
        self._turn_stats_floor = self.actions.last_call_stats().get("seq") or 0
        self.tps_label.setText("")
        self._last_query = typed
        # The question is saved the moment it is asked (a crash mid-turn
        # keeps it); what the turn says is saved when it ends (end_turn).
        self.append("User", typed)
        self._pending = []
        self.input.clear()

        options = self.options()          # on the GUI thread, before the worker
        if full:
            options.deliberate = True
            options.depth = "deep"

        def work() -> None:
            try:
                result = self.actions.send(
                    typed, options,
                    on_event=lambda ev: self._to_ui(
                        lambda ev=ev: self.on_event(ev)),
                    on_token=lambda who, tok: self._to_ui(
                        lambda who=who, tok=tok: self.on_token(who, tok)),
                    # The app's own lines: kept in the conversation, as Tk
                    # keeps them (not turn events — see _save_pending).
                    on_notice=lambda who, text, kind: self._to_ui(
                        self.append, who, text, kind))
                if result is not None:
                    self._to_ui(lambda: self.finish_turn(result))
                    self._learn_later(result)
            # THE MESSAGE IS BOUND HERE, NOT READ LATER. Python deletes the
            # `except ... as exc` name at the end of the block, so a lambda
            # that closes over `exc` and runs later raises NameError instead of
            # showing the error. It worked only while `_to_ui` called back
            # synchronously — which is the test path, never the app's.
            except CouncilActions.NotYetExtracted as exc:
                said = str(exc)
                self._to_ui(lambda said=said: self.append("Council", said,
                                                          "observation"))
            except Exception as exc:                      # noqa: BLE001
                said = repr(exc)
                self._to_ui(lambda said=said: self.append("ERROR", said,
                                                          "error"))
            finally:
                self._to_ui(self.end_turn)

        threading.Thread(target=work, name="council-turn", daemon=True).start()

    def _learn_later(self, result) -> None:
        """Keep what the question taught — wishlist gaps, role and project
        memory (council_core.after_turn) — on its own worker, so the next
        question need not wait. Its model calls queue behind any new
        question's on the same model or machine."""
        learn = getattr(self.actions, "learn", None)
        if learn is None or not getattr(result, "ok", False) \
                or not getattr(result, "panel", None):
            return

        def work() -> None:
            try:
                learned = learn(result)
            except Exception as exc:                      # noqa: BLE001
                said = f"Could not keep what this question taught: {exc!r}"
                self._to_ui(lambda: self.append("Librarian", said,
                                                "observation"))
                return
            if learned is not None and (learned.roles_updated
                                        or learned.gaps_logged
                                        or learned.sage_gap
                                        or learned.errors):
                said = "Kept from this question: " + learned.summary()
                self._to_ui(lambda: self.append("Librarian", said,
                                                "observation"))

        threading.Thread(target=work, name="council-turn-learn",
                         daemon=True).start()

    def pipeline_chat(self):
        """The pipeline-command responder, built on first use."""
        if self._pipeline_chat is None:
            from council_core import dream3d
            self._pipeline_chat = dream3d.PipelineChat(
                self.actions.vault_dir,
                say=lambda who, text, kind: self._to_ui(
                    self.append, who, text, kind),
                on_changed=lambda: self._to_ui(self._pipelines_changed))
        return self._pipeline_chat

    def _pipelines_changed(self) -> None:
        for hook in list(self.pipelines_changed):
            hook()

    def model_chat(self):
        """The model-catalog responder ("what models can I download?")."""
        if self._model_chat is None:
            from council_core import model_chat
            self._model_chat = model_chat.ModelChat(
                say=lambda who, text, kind: self._to_ui(
                    self.append, who, text, kind))
        return self._model_chat

    def _take_over(self, typed: str) -> bool:
        """A take-over ("take over <script>") TYPED in this box, or in the
        Dream3D chat, which sends through it — the chat's one door to it
        (council_core.script_takeover, "ONLY THE USER CAN ASK"). Read here,
        before any other command, from the text the user typed; the pipeline
        chat behind _send_command answers the same words from anywhere else
        without acting on them. Asks first, on the GUI thread; True if
        handled."""
        from council_core import pipeline_intent, script_takeover
        ref = pipeline_intent.take_over_ref(typed)
        if ref is None:
            return False
        try:
            plan = script_takeover.decide(self.actions.vault_dir, ref, typed)
        except Exception as exc:                          # noqa: BLE001
            plan = script_takeover.Plan(
                shown=ref, refusal=f"The take-over failed: {exc!r}. Nothing "
                                   f"changed.")
        if plan is None:
            return False
        self.append("User", typed)
        self.input.clear()
        said = script_takeover.run(
            plan, self.actions.vault_dir,
            confirm=lambda title, text: self.confirm(title, text, parent=self),
            via=f"typed in the chat: {typed.splitlines()[0][:200]!r}")
        self.append("Council", said, "observation")
        self._pipelines_changed()
        return True

    def _send_command(self, typed: str) -> bool:
        """Answer app commands — pipelines, then models — without the council,
        as Tk's _send does before deliberating. True if handled.

        Deciding is fast (regexes, at most a folder scan); the answer can call
        a model, the DREAM3D-NX env or the network, so it runs on a worker.

        First "forget WORD", which the close-match notice under a vault
        search offers (both shells show it — turn_context.prepare_turn): a
        small file written here, no model.
        """
        try:
            reply = self.actions.forget(typed)
        except Exception as exc:                          # noqa: BLE001
            reply = f"Nothing was forgotten: {exc!r}"
        if reply is not None:
            self.append("User", typed)
            self.input.clear()
            self.append("Council", reply, "observation")
            return True
        job = None
        for responder in (self.pipeline_chat, self.model_chat):
            try:
                job = responder().plan(typed)
            except Exception:                             # noqa: BLE001
                job = None            # a broken responder must not block chat
            if job is not None:
                break
        if job is None:
            return False
        self.append("User", typed)
        self.input.clear()

        def work() -> None:
            try:
                job()
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.append, "Writer",
                            f"That command failed: {exc!r}", "final")

        threading.Thread(target=work, name="council-command",
                         daemon=True).start()
        return True

    def on_event(self, event) -> None:
        """One progress event from the turn, always on the GUI thread."""
        kind = getattr(event, "kind", None) or (
            event[0] if isinstance(event, (tuple, list)) else "")
        if kind == "token":
            who = getattr(event, "who", "Writer")
            self.stream_box.append_token(who, getattr(event, "text", ""))
            return
        self._note_speaker(event, kind)
        self._refresh_tps()
        if kind == "phase":
            self.append("", getattr(event, "text", ""), "phase",
                        turn_event=True)
            return
        if kind == "thought":
            # Progress ("Generating response…"), not conversation: it moves
            # the "▶ who" label above and stays out of the transcript, as in
            # Tk's live_event handler.
            return
        if kind == "verdict":
            # A3: the bar appears because a verdict arrived, carrying its id.
            self.show_verdict_bar(getattr(event, "verdict_id", None))
            return
        if kind == "final":
            # A "final" is shown AS a final, as Tk's live_event handler does:
            # that is what makes the Writer's the answer (_last_answer, the
            # save panel). Remembered, because the TurnResult carries the
            # same text again at the end — see finish_turn.
            text = getattr(event, "text", "")
            self.append(getattr(event, "who", "Council"), text, "final",
                        turn_event=True)
            self._shown_finals = (self._shown_finals or set()) | {text}
            return
        self.append(getattr(event, "who", "Council"),
                    getattr(event, "text", str(event)), "observation",
                    turn_event=True)

    def _note_speaker(self, event, kind: str) -> None:
        """"▶ Writer" while the Writer is the one being waited on.

        Two sources, because the turn reports in two ways: the orchestrator's
        phase markers name the role BEFORE its call ("▶ Writer — drafting
        answer"), and an event from a personality names it directly. A stage
        ("▶ Round 1/2 — …") or the app itself ("Orchestrator", "Council")
        moves nothing."""
        who = getattr(event, "who", "") or ""
        if kind == "phase":
            text = (getattr(event, "text", "") or "").lstrip("▶ ").strip()
            who = text.split(" — ", 1)[0].strip()
        if who in _SPEAKERS:
            self.agent_label.setText(f"▶ {who}")

    def _refresh_tps(self) -> None:
        """Tokens/s from the engine's own measurement of each call this turn
        (last_call_stats: gen_tok_s, per role). Up to the three most recent
        speakers, as Tk's label shows them. A call from before this turn —
        its seq at or below the floor taken in on_send — is not this turn's."""
        stats = self.actions.last_call_stats()
        rate, seq = stats.get("gen_tok_s"), stats.get("seq")
        if not rate or seq is None or seq <= (self._turn_stats_floor or 0):
            return
        role = str(stats.get("role") or "")
        name = council_turn.AGENT_NAMES.get(role, role.title() or "Model")
        rates = dict(self._turn_rates or {})
        rates.pop(name, None)                  # most recent last
        rates[name] = rate
        self._turn_rates = rates
        shown = list(rates.items())[-3:]
        self.tps_label.setText(
            " · ".join(f"{who} {value:g}" for who, value in shown) + " tok/s")

    def on_token(self, who: str, token: str) -> None:
        """One streamed token. The stream box, never the transcript.

        The transcript does not see tokens in either front end — an AST pass
        over all 285 `_append_transcript` call sites in the Tk engine confirms
        kind="token" is never passed to it.
        """
        self.stream_box.append_token(who, token)
        self.stream_box.flush()

    def finish_turn(self, result) -> None:
        """Render what the turn produced, on the GUI thread."""
        self._refresh_tps()
        if not result.ok:
            self.append("Council", result.message or "The turn failed.",
                        "observation")
            return
        # ONCE. The turn reports its answer twice — as the Writer's "final"
        # event while it runs, and in the result at the end — and this used
        # to render both, so every answer appeared twice in the transcript.
        # The result is shown only when no event already carried it (a turn
        # that reports no events still gets its answer on screen).
        if result.answer and result.answer not in (self._shown_finals or ()):
            self.append("Writer", result.answer, "final")
        if result.critique:
            self.set_judge(result.critique)
        self._turn_provenance = dict(getattr(result, "provenance", None) or {})
        # The chips under the answer: the files it was given (Tk's
        # _render_source_chips). Said in the transcript too, not kept — the
        # answer's provenance carries them.
        source_paths = list(getattr(result, "source_paths", None) or [])
        self._last_sources = source_paths
        if source_paths:
            self.show_sources(source_paths)
            self.append("Council", "Sources: " + ", ".join(
                Path(p).name for p in source_paths[:self.MAX_CHIPS]),
                "phase", keep=False)
        # The name FIRST, then the verdict under it. The other way round (as
        # the Tk shell does it) the first verdict of every judged session was
        # recorded under the launch's raw id, which this rename retires — so
        # a one-deliberation session never had its badge (found in review).
        if getattr(result, "session_name", ""):
            named = self.conversation.rename(result.session_name)
            if named.ok:
                self.append("Council", named.message, "observation")
        if getattr(result, "verdict_id", ""):
            # Recorded where the Tk shell records it, with this turn's id. The
            # bar that would let the user answer it stays as Batch 0 left it.
            self.conversation.record_verdict(conversations.verdict_record(
                result, self.conversation.session_id, self._last_query or ""))
        if result.route == "direct" and result.answer:
            # Only a fast answer can be expanded, and only until the next turn
            # resets it (A4). The full build re-asks it through the whole
            # council at Deep depth (_send(full=True) sets the options itself,
            # so nothing depends on _force_full_council surviving the reset).
            # The DEMO_MODE build has no panel to expand to: there the button
            # stays disabled and labelled (_label_unavailable).
            self._last_fast_question = self._last_query
            if not self.demo_mode:
                self.expand_btn.setEnabled(True)
        self._last_route = result.route
        # A3: the bar follows the verdict id, which a turn without a verdict
        # does not have.
        self.show_verdict_bar(result.verdict_id)

    def flush(self) -> None:
        """Called once per queue drain — see the stream box's own note."""
        self.stream_box.flush()

    # ==================================================================
    # The transcript
    # ==================================================================
    def append(self, who: str, text: str, kind: str = "final", *,
               turn_event: bool = False, keep: bool = True) -> None:
        """One entry, formatted by the shared policy, and kept.

        Goes through council_core.transcript, so the Council transcript, the
        Dream3D mirror and the Tk shell all say the same thing — and through
        the conversation, so it is saved where the Tk shell saves its own.
        ``turn_event``: it came from the running turn's own events (a draft,
        a cross-examination, a ranking — or the answer); see _save_pending.
        ``keep=False``: shown, and in the debug log, but NOT part of the
        conversation — for lines that quote OTHER sessions (History's list
        of past questions, the name of a loaded session). Kept, they would
        be in the next prompt's RECENT CONVERSATION, which is exactly how an
        older session must not get in.
        """
        self.transcript.append_entry(who, text, kind)
        self.mirror.append_entry(who, text, kind)
        if transcript_core.is_final_answer(who, kind):
            self._last_answer = text
            self.save_frame.show()
        if not keep:
            self.conversation.log(who, text, kind)
        elif self._pending is not None:
            # The logs take it NOW, under the id it was said under, as the Tk
            # shell's do; only whether it joins the conversation file waits
            # for the turn's end (_save_pending). It waited whole before, so
            # a turn's entries were logged at its end under the renamed id,
            # and a crash mid-turn lost a deliberation's drafts from both.
            self.conversation.log(who, text, kind)
            self._pending.append((who, text, kind, turn_event))
        else:
            self.conversation.record(who, text, kind)

    # ==================================================================
    # The conversation
    # ==================================================================
    def _save_pending(self) -> None:
        """Save what the turn said: the answer, with its provenance, and the
        app's own lines (errors, the context notices — "Tracking → goal",
        "Reading: …", the context-window warnings — the note under an answer
        from the data, the session's name), in the order they were said, as
        the Tk shell keeps them. Everything was logged when it was said.

        The turn's OTHER events — a full council's drafts, cross-examination,
        rankings and critiques — are in the debug log only. The Tk shell
        stores them all, and one full-council turn is forty-odd entries: the
        twelve-entry RECENT CONVERSATION a follow-up sees was then one turn's
        debate, and the question before it had already dropped out (found by
        the planted-fact test, tests/test_qt_remembers.py). The default build
        has one Writer, whose one event IS the answer, so nothing changes
        there."""
        pending, self._pending = self._pending, None
        if not pending:
            self.conversation.flush()
            return
        provenance = self._turn_provenance or None
        answer = self._last_answer
        last_answer_at = max((i for i, (who, text, kind, _turn)
                              in enumerate(pending)
                              if transcript_core.is_final_answer(who, kind)
                              and text == answer), default=None)
        for i, (who, text, kind, turn_event) in enumerate(pending):
            # Every one was logged when it was said (append).
            if i == last_answer_at:
                extra = {"provenance": provenance} if provenance else {}
                self.conversation.record(who, text, kind, logged=True,
                                         **extra)
            elif not turn_event:
                self.conversation.record(who, text, kind, logged=True)
        self._turn_provenance = None
        self.conversation.flush()

    def close_conversation(self) -> None:
        """On close: save anything a running turn has said, end the log."""
        self._save_pending()
        self.conversation.close()

    def _show_memory(self) -> None:
        """The toolbar's memory line: the scope — or, while this
        conversation is not being saved, that follow-ups see nothing (the
        personalities read earlier turns from the saved file)."""
        if self.conversation.unsaved:
            self.memory_label.setText("Memory: not saved — follow-ups will "
                                      "not see earlier questions")
            self.memory_label.setToolTip(
                self.conversation.not_saved_message())
            return
        self.memory_label.setText(memory_scope.describe(
            self.conversation.scope))
        self.memory_label.setToolTip(memory_scope.explain(
            self.conversation.scope))

    def _on_conversation(self, event: str, **details) -> None:
        """Say in the transcript when an older session is brought into this
        conversation, or taken out of it — the user should never have to
        guess what the council can see — and when this conversation stops
        (or starts again) being saved."""
        if event in ("prior_loaded", "prior_cleared", "not_saved", "saving"):
            # Shown, not kept: the line names the other session, and the
            # PRIOR SESSION CONTEXT block is how a loaded one gets in.
            self._to_ui(self.append, "Council", details.get("message", ""),
                        "observation", keep=False)
        if event in ("not_saved", "saving"):
            self._to_ui(self._show_memory)

    def receive_notice(self, who: str, text: str, kind: str = "final", *,
                       source: str = "") -> None:
        """A report from another tab — the IDE's snapshot path, the
        Librarian's commit receipt, a node rebuild — via
        CouncilWindow.append_transcript. On the GUI thread; the window
        guarantees that.

        LABELLED, AND NEVER AN ANSWER. It is the app reporting on something
        that happened elsewhere, so it says where it came from, and it is
        written as an observation whatever kind the caller passed: the callers
        say "final", and a "final" from "Writer" would become the last answer
        that Save answer writes out.
        """
        label = f"Notice from the {source} tab" if source else "Notice"
        self.append(who or "Council", f"[{label}] {text}",
                    "error" if kind == "error" else "observation")

    def set_status(self, text: str, colour: Optional[str] = None) -> None:
        self.status.setText(text)
        if colour:
            self.status.setStyleSheet(f"color: {colour};")

    def set_judge(self, text: str) -> None:
        self.judge_box.setPlainText(text)

    # ==================================================================
    # The verdict feedback bar
    # ==================================================================
    def show_verdict_bar(self, verdict_id: Optional[str]) -> None:
        """Show the bar — only with a verdict to attach an answer to.

        A3: the Tk build shows it on every `done`, including the direct-mode
        fast path that never produced a verdict, and then stamps the answer
        onto an unrelated earlier deliberation. No id, no bar.
        """
        if not verdict_id:
            return
        self._last_verdict_id = verdict_id
        self.vfb_frame.show()

    def hide_verdict_bar(self) -> None:
        self.vfb_frame.hide()
        self.vfb_detail.hide()

    def on_agree(self) -> None:
        self._record_verdict(True)

    def on_disagree_open(self) -> None:
        self.vfb_detail.show()
        self.objection.setFocus()

    def on_disagree_cancel(self) -> None:
        self.vfb_detail.hide()
        self.objection.clear()

    def on_disagree_submit(self) -> None:
        objection = self.objection.toPlainText().strip()
        if not objection:
            self.append("Council", "Say what you disagree with, or Cancel.",
                        "observation")
            return
        self._record_verdict(False, objection)

    def _record_verdict(self, agreed: bool, objection: str = "") -> None:
        if not self._last_verdict_id:
            # Unreachable through the bar, which is hidden without an id. Kept
            # because that invariant is the fix, and an assertion that states
            # it is cheaper than rediscovering why the bar is conditional.
            self.append("Council", "There is no verdict to respond to.",
                        "observation")
            return
        try:
            self.actions.record_verdict_response(
                self._last_verdict_id, agreed, objection)
        except CouncilActions.NotYetExtracted as exc:
            self.append("Council", str(exc), "observation")
            return
        self.hide_verdict_bar()
        self.objection.clear()

    # ==================================================================
    # The rest of the buttons
    # ==================================================================
    def on_toggle(self, key: str, on: bool) -> None:
        setattr(self._opts, key, bool(on))

    def on_clear_input(self) -> None:
        self.input.clear()

    def refresh_specialists(self) -> None:
        """Fill the Ask: pin, and say so when the registry will not load.

        A registry that fails is not the same as a registry that is empty, and
        the Tk shell's version of this cannot tell them apart either.
        """
        from council_core import specialists_ops

        self._specialists = self.actions.specialists()
        self.specialist_box.clear()
        self.specialist_box.addItems(specialists_ops.choices(self._specialists))
        if not self._specialists.ok:
            self.append("Council", self._specialists.message, "observation")

    def pinned_specialist(self) -> Optional[str]:
        """The specialist ID to force onto this query, or None for automatic.

        Resolved through the map the labels were BUILT from. The previous
        version built {name: name}, so even with entries it would have pinned
        a NAME where the resolver wants an ID.
        """
        from council_core import specialists_ops
        return specialists_ops.pinned_id(self.specialist_box.currentText(),
                                         self._specialists)

    def backend_override(self) -> Optional[str]:
        return council_options.backend_override(self.backend_box.currentText())

    def on_save_answer(self) -> None:
        if not self._last_answer:
            self.append("Council", "There is no answer to save yet.",
                        "observation")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save answer", "answer.md", "Markdown (*.md);;Text (*.txt)")
        if not path:
            return
        self.append("Council", self.actions.save_answer(self._last_answer,
                                                        Path(path)),
                    "observation")

    def on_save_output(self, suffix: str) -> None:
        if not self._last_answer:
            self.append("Council", "There is no output to save yet.",
                        "observation")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save as {suffix}", f"output{suffix}",
            f"*{suffix}")
        if not path:
            return
        self.append("Council", self.actions.save_answer(self._last_answer,
                                                        Path(path)),
                    "observation")

    def on_history(self) -> None:
        questions = self.actions.question_history()
        if questions is None:
            self._not_yet("History")
            return
        if not questions:
            self.append("Council", "No questions asked yet.", "observation",
                        keep=False)
            return
        # Shown, never kept: these are other sessions' questions too, and a
        # kept line would put them in the next prompt (see append).
        self.append("Council",
                    "Recent questions:\n" + "\n".join(f"  • {q}"
                                                      for q in questions[:20]),
                    "observation", keep=False)

    def on_examples(self) -> None:
        self.append("Council", EXAMPLES, "observation", keep=False)

    def on_expand_with_council(self) -> None:
        """Re-ask the last fast question through the full council.

        A4: reachable only while `_last_fast_question` is set, and
        `reset_turn()` clears both it and this button's enabled state — so it
        cannot re-ask a question from two turns ago, which is what the Tk
        version does.
        """
        if not self._last_fast_question:
            self.append("Council", "There is no fast answer to expand.",
                        "observation")
            return
        self._force_full_council = True
        self.input.setPlainText(self._last_fast_question)
        # The whole council, at Deep depth, and no earlier answer offered
        # instead: `full` carries that through the send itself, because
        # reset_turn clears _force_full_council before the options are taken.
        self._send(reuse=False, full=True)

    def on_clarify(self, skip: bool = False) -> None:
        self.clarif_frame.hide()
        self.clarif_answer.clear()

    # -- not available yet, and saying so --------------------------------
    def _not_yet(self, what: str) -> None:
        """What one of these says if it is reached at all — the controls are
        disabled (_label_unavailable), so only code can get here. It used to
        say "needs the deliberation extracted — it is the rest of phase 6",
        which is the porter's to-do list, not something a user can act on."""
        self.append("Council", NOT_AVAILABLE.format(what=what),
                    "observation")

    def on_find_and_chart(self) -> None:
        self._not_yet("Find & Chart")

    def on_look_up(self) -> None:
        self._not_yet("Look Up")

    def on_defer_to_vault(self) -> None:
        self._not_yet("Defer to Vault")

    def on_add_instruction(self) -> None:
        self._not_yet("Council instructions")

    def on_manage_instructions(self) -> None:
        self._not_yet("Managing instructions")

    def on_content_style(self) -> None:
        self._not_yet("Content style")


EXAMPLES = """Things you can ask:
  • which file has the Q3 invoice totals?
  • summarise sales.csv by region
  • chart revenue by month
  • what changed between the two job folders?
  • write a summary I can send to a client"""


def build_council(window) -> QWidget:
    """Factory for the tab registry."""
    import branding
    # The tab's demo_mode means "one Writer, no panel": the single-model
    # chat (COUNCIL_CHAT_MODE=single), no longer the DEMO_MODE build.
    return CouncilTab(window,
                      demo_mode=bool(getattr(branding, "SINGLE_MODEL_COUNCIL",
                                             False)))
