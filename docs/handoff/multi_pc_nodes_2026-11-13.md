# Several PCs as Council machines (2026-11-13)

Andy asked what it takes to connect several PCs as Council nodes, then
picked "N1 then N2": first an "Add a PC" helper and one machine list, then
the Ledger's model questions spread over the machines. Andy also asked
whether the main PC can control the others; the answer built here is the
models on each machine (list, download, remove) from the main PC. Full
desktop control is Windows Remote Desktop's job, not the Council's.

## What a "machine" is

Another PC's Ollama, reached over plain HTTP on port 11434. A machine only
answers prompts: files, the vault, OCR and every tab's own work stay on the
main PC. The list is Machines & roles (`council_core/node_routing.py`,
`<vault>/node_routing.json`), opened from the Council Map tab, the Nodes tab
(advanced) and the Quant Duel tab.

## N1 (this change)

* **Add a PC…** (`council_qt/widgets/add_pc.py`, `council_core/node_setup.py`):
  1. name, this PC's address (picked from the route to the other PC when its
     address is typed), the models it needs (the roles' US-origin Ollama
     models);
  2. **Save setup file…**: a `.bat` for the other PC, run as administrator:
     `setx OLLAMA_HOST 0.0.0.0:11434 /M`; one firewall rule
     "Ollama for the Council" (TCP 11434 inbound from this PC's address only)
     plus a list of any other rule that lets everyone reach Ollama; an Ollama
     restart; Y/N per model download (and how to copy models in when that PC
     is offline); its own addresses;
  3. **Test**: `/api/version` and `/api/tags` only, through
     `local_models.open_direct` (no proxy, no redirect);
  4. **Add to the list**: enabled only when the test of that address passed.
     Nothing is saved until Save, and the master switch stays the user's.
* Every value written into the .bat is checked first (name by node_routing's
  name rule, address as a usable IPv4, models as US-origin Ollama names), so
  nothing typed becomes a command.
* **Test selected** and **Models on selected…** in Machines & roles: whether
  a machine answers and has its roles' models; its models listed (non-US
  marked), one US-origin model downloaded when the user presses Download (the
  OTHER PC downloads it, so it needs internet there; same rule as the Pi
  wizard's "Download on the Pi"), one removed after the user confirms.
* **Model for shared work** (`Node.model`, optional, US-origin only, written
  only when set so older files are unchanged): the model a machine reads with
  when work is shared out instead of sent for a role.
  `node_routing.sharing_nodes()` = enabled machines with one, routing on.
* **Connections**: "and my other machines" (was "and my Pi nodes") now
  shares the build's reading with `sharing_nodes()` as well as the
  Apothecary's nodes (still only with `COUNCIL_REMOTE_NODES=1`); a machine in
  both is listed once.

## N2: the Ledger's model questions on several machines

`council_core/ledger_spread.py`. With "Ask Council's model" ticked, the
Ledger asks this PC (the model picked for the Ledger, one at a time) AND
every machine that takes shared work (`node_routing.sharing_nodes()`), each
with its own model, up to its "Calls at once". No machine set up that way:
nothing changes. `COUNCIL_LEDGER_SPREAD=0` turns it off.

* **Template Ledger, part questions.** `ledger_ingest` wraps the part finder
  in a `PartAsker`; `source_extract._extract_all` then runs a `_Pipeline`:
  a part-less workbook's question is submitted and the next workbooks are
  read meanwhile; `_extract_one` hands back the rest of the workbook
  (`_finish_one`) and the pipeline calls it IN FILE ORDER once the answer is
  in, so the review is the same as asked on this PC alone (pinned by
  `tests/test_ledger_spread.py`). At most twice the machines' calls wait at
  a time. Files the helper processes read are put in their turn too.
* **Table template, header layouts.** `ledger_records._ask_model_spread`
  picks the same layouts (each once, at most MODEL_MAX_LAYOUTS) and asks them
  all at once; answers go into the recipe in order.
* **A machine that fails** is not asked again in that run and its question
  goes to another machine that has not tried it. Only when all fail does
  `ledger_llm.one_shot` see a failure.
* **One thread at a time outside the model**: find_part, one_shot and the
  AssistReport were written for one thread, so each question holds a baton
  (`Spread.run`) and lets go only while a machine thinks (`Spread.__call__`).
* **Cancel** while answers are out: those workbooks are left out (what their
  reading added to the run's lists is taken out by recorded positions,
  latest first) and the run is a cancelled one.
* The summary says who answered: "Shared across machines: pc2 answered 14,
  this PC answered 9" (`AssistReport.machines`).
* The one question about left-over labels stays one question.

## Not done / limits

* The setup file is Windows-only; a Pi uses the Apothecary's "Set up a Pi".
* Plain HTTP, no login: the firewall rule is the protection. An SSH tunnel
  would need the engine to tell a tunnel from this PC's own Ollama.
* If the main PC's address changes, make a new setup file and run it again
  (a DHCP reservation avoids this).
