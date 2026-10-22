# Skipping files that stall the Council — 2026-10-22

User: "have the council (connections, ledgers, and any other relevant tabs) skip
files that take too long to process and cause the app to stall out. I've noticed
some files have something inside them that breaks the system."

## What it does

`council_core/file_guard.py` guards every bulk read of a user's file.

| Where | The read guarded |
|---|---|
| Ledger, sample Extract | `source_extract._extract_one` → `load_workbook_grids`, and the text-document pre-pass `document_explanations` |
| Ledger, table Extract (and Teach) | `ledger_records._extract` → `read_book` |
| Connections, Rebuild graph (and the Ledger's Rebuild) | `knowledge_graph._seed` → `read_records`, both calls |
| Vault, keyword index | `vault_index._index_file` (through `_index_file_guarded`) |
| Vault, data index (search) | `data_index.DataIndex._profile_file` |

For each file:

1. **On the skip list → not opened.** `Skipped` is raised at once, with the
   reason. The list is one for the whole app: `<app dir>/skipped_files.json`.
   So a file that stalled the Ledger is skipped by the Connections graph and
   the Vault too. An entry holds the file's size and modified time; once the
   file changes it is read again.
2. **Too slow → left behind.** The read runs on a helper thread. Past
   `COUNCIL_FILE_TIME_LIMIT_S` (default **180 s**, `0` = no limit), the file
   goes on the list and the run goes on with the next file. 180 s is past
   Excel's own 2-minute open timeout, which says better why. A Python thread
   cannot be stopped: the helper is abandoned and its answer thrown away.
3. **Stalled or crashed the whole app → skipped next time.** A reader that
   holds the interpreter so nothing else runs, or that crashes the process,
   cannot be caught while it happens. So the file is written down before the
   read, in `<app dir>/reading_now.json`, and crossed out after.
   - A normal exit crosses out its own notes (atexit).
   - A note left by a session that is gone — killed while frozen, or crashed —
     puts that file on the list at the next read: "the Council stopped while
     reading it".
   - Another Council still running (its pid alive) is not blamed.
   - The Vault writes its note only after 2 s (`note_after`). Its thousands of
     small files would otherwise cost two writes each. So an instant crash in
     a Vault read is not caught.

4. **Too big → not opened, or left behind** (added the same day, "Add a memory cap
   for files that eat RAM"):
   - **Before opening:** an Office file is a zip whose own directory says what it
     unpacks to. Past `COUNCIL_FILE_UNPACKED_LIMIT_MB` (default 1,024; 0 = off) it
     is not opened at all.
   - **While reading:** the app's memory is watched (working set on Windows,
     resident on Linux). A read that adds more than `COUNCIL_FILE_MEMORY_LIMIT_MB`
     (default 4,096; 0 = off) is left behind like a slow one.
   - **Caveats:** the memory is the whole process's, so another tab's work at the
     same time counts too; the limit is generous for that. And a read left behind
     keeps its memory until it ends, because a thread cannot be stopped.

A Cancel (the Ledger's) stops the wait without blaming the file.

The reading thread sees the asking thread's state:

- Context variables go across with the context (the graph's image reader).
- Registered `threading.local`s are copied in, and copied back when the read
  finishes in time: `file_guard.carry(...)`. Today these are `pptx_open._LOCAL`
  (the deck links of a Ledger run) and `excel_open._SCOPE` (Cancel scope,
  Excel down).

**A new reader with thread-local state must register it.**

## What the user sees

- **Ledger:**
  - the failure summary counts them as "too slow / stalled the app (skipped
    until it changes)";
  - each file has a problem line "`name`: skipped: took longer than 180 s to
    read …".
- **Connections:** the unreadable list and its tooltip. The category
  "too slow / stalled" comes from `_categorize_unreadable_reason`. A skipped
  file is never cached in `doc_cache`, so a retry reads it.
- **Vault:**
  - the keyword index prints `[VaultIndex] name: skipped …`;
  - a data-index profile carries the reason as its error, with `indexed_at` 0
    so it is asked again (the list answers at once).
- **"Retry skipped files (N)"** (`council_qt/widgets/skipped_files.py`), beside
  Extract (Ledger), Rebuild graph (Connections) and the Vault's index buttons:
  - the tooltip lists the files and why;
  - a click empties the list;
  - it refreshes after each run.

## Tests

- `tests/test_file_guard.py`: the guard itself, including a crashed session
  and a killed frozen one, in child processes.
- `tests/test_file_guard_tabs.py`: each tab skips a hung read and goes on, the
  Connections graph skips what the Ledger skipped, and the Retry button.

## Not done

- **A read that eats memory is only left behind, not stopped.** A reader in a
  child process with a hard memory cap (`council_core/child_proc.py` already has
  the Windows Job Object) would free it at once, at the price of passing each
  file's result back as JSON.
- **A file-size gate** before reading.
