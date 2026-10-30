# Ledger questions (chat in the Ledger tab), 2026-10-30

Andy: "add a chat function in the ledger process directly ... I have a filled
template in and then the ledger finds it and requests info from me of if it is
a filled copy or not", then "re-run after it runs once and asks questions and
recieves the answers and uses those to see if the data connections it made was
correct". Scope agreed: filled copies, parts, clashes (and the match check).

## What it does

After Extract, Update with new files or Re-run with my answers, the Ledger tab
shows a **Ledger questions** pane under step 3, but only when there is
something to ask. It asks one question at a time. You answer with a button, or
type a reply, which is read back to you and only acted on once you confirm it.
Extract never waits for an answer: the questions are worked out from the review
it wrote.

| Kind  | When | Answers |
|-------|------|---------|
| copy  | a sheet laid out like the template (`ledger_examples.is_filled_copy`) | trust its values / only as a guide / read it like any other file / leave it out |
| part  | a file whose part was only guessed (file name, model, index, title) | yes / another part (typed) / no part's file, leave it out |
| clash | files disagree on one value (at most 15 a round) | one of the values / none / another value (typed) |
| match | a label matched to a field whose values differ from a *trusted* copy | yes, keep it / no, it is not that field |

Answers are written into the review workbook's Accept / Value / Part cells
(`ledger_review.save`, which now takes `part`), so Apply, the Excel review and
the review windows work unchanged. They are kept beside the parent CSV in
`<csv>.answers.json` (by file name and value) and applied again to every later
review of that Ledger (`ledger_questions.apply_saved`, called in
`ledger_ingest._extract`), so they are not asked twice.

When a round's questions are all answered or skipped, the tab re-checks the
review by itself (`LedgerActions.recheck` -> `ledger_questions.recheck`). It
applies every kept answer, checks the labels against the trusted copies,
rewrites the filled templates, and asks the new questions. It stops
re-checking by itself after `ROUNDS_MAX` = 3 rounds. A re-check reads no source
workbook: on the synthetic corpus (83 files, 16,252 values) Extract took 27 s
and a re-check took 16 s (load the review, save it, write the filled
templates). Questions you skip are not asked again in that review's rounds.

Typed replies: plain-word rules first (`interpret`). When the rules can't tell
and "Ask Council's model" is ticked, the model picked for the last run is asked
to choose one of the listed answer keys (a JSON schema with an enum). Nothing
is done until you confirm.

## Files

- `council_core/ledger_questions.py`: questions, kept answers, plan/apply,
  label checks, re-check, reading a typed reply. No Qt.
- `council_qt/tabs/ledger_questions_pane.py`: the chat pane.
- `council_qt/tabs/ledger.py`: the pane under step 3, `_ask_questions`,
  `on_questions_recheck`, `_read_reply`, `LedgerActions.recheck`; Re-run works
  the questions out again after carrying answers.
- `source_extract.write_review`: the review's `_run` meta keeps
  `filled_copies` (where + parts); `ledger_review.Review.filled_copies`.
- `tests/test_ledger_questions.py`.

## Limits

- Values that agree are merged into one review row ("also at"), so a label
  confirmed by a trusted copy is counted only from rows read under their own
  label. The "N agree with your filled copy" count includes the merged ones.
- Sample Ledgers only; a table Ledger's review gets no questions.
- "Leave it out" sets the values aside through the kept answer; it does not
  add a Skip pattern, so the file is still read.
