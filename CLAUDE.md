# The Council — notes for Claude Code

## This branch: `knowledge-graph`

Work here is the **knowledge graph** (people, parts and projects across the vault
documents), done on the user's desktop with its larger vault. **Before writing code,
read `docs/handoff/knowledge_graph_desktop.md`** (decisions, plan, first steps,
questions to ask) and `docs/morphik_assessment.md` (the researched plan with file:line).
Push only this branch; the laptop merges it into `qt-migration` after review.

## Standing rules (all branches)

- Offline by design: localhost only, no telemetry, no cloud LLM calls; nothing leaves
  the PC unless the user opts in. Keep everything local — a networked/server Council is
  only a possible future path; don't build server pieces now.
- Never delete or overwrite user data; never modify existing vault files. Test against
  temporary vaults (`COUNCIL_VAULT_ROOT`) or copies.
- No pickle. Atomic writes. Python 3.11+ compatible.
- Only US-origin models may be recommended (Llama, Phi, Gemma, Granite, OLMo, gpt-oss).
  The Council never downloads models itself.
- Verify offscreen (`QT_QPA_PLATFORM=offscreen`, `COUNCIL_NO_DIALOGS=1`); don't open
  windows on the user's screen unless asked.
- Never push to `main` or `Work-Build`.
- Tests that fail before and pass after; report results faithfully, failures included.
