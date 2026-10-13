# Test day — the council on the working machine

Branch `council-map`. Everything below was tested with scripted stand-in
models only; this is the first run with real ones. Go in order: each step
needs the one before it.

## 1. Readiness (5 minutes)

Diagnostics tab ▸ **Readiness check**, then **…with live model calls**.

- Every `FAIL` first: git, pytest, the offscreen GUI check, Ollama.
- `role coder`: should say *native tool calls* and 16k+ context.
- Live: each model answers; *structured reply* OK; *coder tool call* OK.
- Copy the report into a note — it is the "before" picture.

If a model breaks structured replies, set `COUNCIL_STRUCTURED=0`; if its
tool calls misfire, `COUNCIL_NATIVE_TOOLS=0`. Both fall back to the old
behaviour.

## 2. The council (15 minutes)

Council tab, Depth **Auto**:

1. `thanks!` — should be Quick: one answer, ~2 calls (last line of the
   transcript: calls, seconds, model loads).
2. A plain factual question — Standard: no cross-fire.
3. A code question — Deep: the full debate; the Judge's ranking shows
   percentages; members end with `CONFIDENCE: n%`.
4. Ask question 2 again — the earlier answer is offered (Use it / Ask again).
5. Turn **Tools** on and ask something with a number in it — a member should
   call `calc`.

Watch for: "models are swapping" in the last line (then give roles one
model, or move one in Machines & roles).

## 3. Benchmarks (30–60 minutes, can run unattended)

- Council Map ▸ **Benchmark…**: label `deep`, Depth Deep, Run. Then label
  `auto`, Depth per question, Run. Compare the two.
- Code tab ▸ make the project first (step 4), then **Benchmark on history**.
  Expect a low solve rate on small local models; it is the baseline that
  hardware upgrades are measured against.

## 4. The Code tab (30 minutes)

1. **New…** project: folder = the Council checkout; test command = one fast
   file, e.g. `python -m pytest -q tests/test_tool_kit.py`; GUI check e.g.
   `council_qt.widgets.code_dialogs:ProjectDialog`.
2. **Brief…** — check it shows CLAUDE.md.
3. Agent **Developer**, a small task with a test, e.g. *"Add a `calc`
   conversion for kelvin to rankine in council_core/tool_kit.py, with a
   test in tests/test_tool_kit.py"*. **Plan**, read and edit the plan,
   **Run**.
4. **Diff** — read it. **Discard** this first one even if it is good
   (practice), or **Merge** if you are happy.
5. Agent **Reviewer (report)** on a file you know — compare its report
   with what you know.

## 5. Tool creation (10 minutes)

Tool Creation tab: describe a small data tool, Generate — tests appear
with it. Run Tests, then Approve for `intern`. Ask the council a question
with Tools on that needs it.

## What to bring back

- The readiness report.
- Benchmark summaries (`<vault>/.council_bench/runs.jsonl`,
  `code-runs.jsonl`).
- For any code job that went wrong:
  `<vault>/.council_projects/<project>/jobs/<job>.transcript.jsonl` —
  every message the coder saw and every tool call it made.
- Anything that froze, crashed or confused you, with the time.
