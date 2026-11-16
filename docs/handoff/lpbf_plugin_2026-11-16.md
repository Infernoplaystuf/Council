# LPBF / Dyndrite plugin (2026-11-16)

Andy asked (2026-11-16) for a local AI setup that drives Dyndrite LPBF Pro,
built as a Council plugin, and asked how a local model can use the auth
token of Dyndrite's MCP server. Proposal doc:
https://claude.ai/artifact/NcFQX5SMKEZYRhSyV2ceRb

## The token: Council holds it, the model never does

A model only writes text; the MCP client sends the requests, so the client
attaches the token. Council's MCP client already sent headers; what was
missing was a place to keep them.

- `council_core/secret_source.py`: a token SOURCE is `env:NAME` (an
  environment variable) or `credential:NAME` (a Windows Credential Manager
  generic credential, read with ctypes `CredReadW`; make one with
  `cmdkey /generic:council/dyndrite /user:token /pass:<token>`). `${NAME}`
  in a header value is filled from the environment at connect time.
- `docs_servers.ServerSpec` gained `headers`, `token_source`,
  `token_header` (default Authorization), `token_scheme` (default Bearer)
  and `token_env` (stdio: the variable the server process gets the token
  in). `validate` refuses a credential header saved as plain text.
  `open_client` reads the token only when it connects and masks it out of
  error text. Saved files hold only where the token is.
- Plugin service `mcp` (`council_core/plugin_services.Mcp`): problem,
  check (tools with their input schemas and annotations), tools, call,
  release. Local servers only unless that server's "allow remote" is on.
  (`council_core/plugin_services.py` is Council's app-plugin service list;
  the Typhon camera plugin guide does not cover it, so it was not changed.)

## The plugin (`council_plugins/lpbf`, id `council.lpbf`, off until ticked)

- `engine/limits.py`: parameters in base units (W, mm/s, mm, deg), unit
  conversion, machine/material profiles (`profiles.json` in the plugin data
  folder). The shipped example has MADE-UP limits and says so.
- `engine/doe.py`: full factorial, 2-level fractional (k 2-7), Latin
  hypercube (maximin, seeded), face-centred central composite, Box-Behnken;
  all points inside [low, high].
- `engine/spec.py`: the experiment spec; `from_request` asks a model for
  JSON (schema-constrained) and converts aliases and units. The model never
  lists runs or limits.
- `engine/plan.py`: runs with specimen IDs (`EXP-YYYYMMDD-NN-S001`), LED
  and VED; deterministic checks (BLOCK: out of limits, no limit for a
  parameter, too many specimens, design impossible; WARN: VED window, thin
  LHS, no centre points); manifest CSV; results join on specimen ID,
  summary, correlation (constant inputs get no r).
- `engine/store.py`: SQLite `experiments.sqlite`; append-only audit
  (triggers refuse UPDATE/DELETE); approval bound to the checked hash,
  re-checked against current limits, needs a name; only approved plans
  export; Dyndrite tool requests pending -> approved -> run once.
- `engine/dyndrite.py`: settings (`dyndrite.json`), tool rules read /
  change / blocked (user override, else MCP annotations, else name; unknown
  = change); `ask` is orchestrated, read tools only, arguments checked
  against the input schema, at most 3 calls.
- `tab.py`: Plan, Experiments, Results, Limits, Dyndrite pages.
- `tools/fake_dyndrite_mcp_server.py`: a made-up stand-in server (stdio or
  `--http PORT`) that wants a token, for trying the Dyndrite page before the
  real server is reachable.

Tests: `tests/test_lpbf_plugin.py`.

## Not done / next

- Real Dyndrite: needs its endpoint, auth scheme, tool list, docs and
  example recipes (questions in the proposal doc).
- Recipe drafting checked against Dyndrite's API docs (Milestone 3).
- Random Forest / Gaussian process with uncertainty, Bayesian and Pareto
  suggestions (Milestones 4-5); Equations page already does linear to
  power-law fits on Ledger data.
- Token fields in the Docs tab's server dialog (the setting exists; only
  the plugin's page shows it).
