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

## N2 (next)

Ledger Extract's model questions spread over `sharing_nodes()` the way the
Connections build shares its passages.

## Not done / limits

* The setup file is Windows-only; a Pi uses the Apothecary's "Set up a Pi".
* Plain HTTP, no login: the firewall rule is the protection. An SSH tunnel
  would need the engine to tell a tunnel from this PC's own Ollama.
* If the main PC's address changes, make a new setup file and run it again
  (a DHCP reservation avoids this).
