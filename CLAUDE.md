# The Council — notes for Claude Code

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

## Where things are described

- The knowledge graph (Connections tab), agent profiles (Agent Creator tab) and the
  "Set up a Pi" wizard: `docs/handoff/knowledge_graph_desktop.md` (decisions, plan and
  progress; it was written for the desktop's `knowledge-graph` branch).
