# The Ledger's Extract in helper processes (2026-10-30)

Andy asked whether any of the app uses multithreading and then for a proposal.
Every tab runs its slow work on one background thread. Nothing ran in parallel
inside that work, and Python runs only one thread's code at a time, so more
threads would not have made reading or matching faster. These decisions were
agreed with Andy:

- results come back from the helpers as JSON (no pickle);
- the default is half the CPU cores, at most 4 helpers;
- files Excel opens stay one at a time, in the Ledger's own process.

## What was measured first

A Ledger Extract of the 83 synthetic workbooks (cloud container, 4 cores,
PR #56 applied) took 57 s:

| Phase | Time |
|---|---|
| Reading the files | 4 s |
| Matching values to the template, per file | 23 s |
| Loading the review workbook back in | 17 s (loaded twice) |
| Writing the review and filled templates | 12 s |

So reading in parallel on its own would have saved about 3 s. This work does
two things:

1. **The review is loaded once** (`ledger_ingest._extract`), and each value's
   source is found in a name-to-path map (`ledger_review._by_name`). This took
   the run from 57.3 s to 46.3 s.
2. **Helpers read and match whole files.** Each helper runs
   `source_extract._extract_one` on its own file. The run then went from
   47.5 s to 34.3 s with 2 helpers and 30.5 s with 4. Its floor is the largest
   file, `EC_master_log`, at 10.8 s. The review is identical with helpers or
   without.

## How it works (`council_core/ledger_workers.py`)

- `source_extract.extract` builds a config from what the Matcher is made of:
  the schema, aliases, ignore list, taught labels, explanations, pattern and
  part. `_extract_all` starts a `Pool` and puts each file's result into the run
  in file order (`ledger_workers.put`), exactly where `_extract_one` would have
  put it.
- A helper rebuilds the Matcher from the config and runs `_extract_one` on a
  fresh `Extraction` for each file. It sends back the file's results as JSON
  (`run_json`), plus the progress steps, which the Ledger repeats.
- The Ledger reads a file itself, as before, in three cases:
  - **Excel opens it.** That means `.xls`, `.xlsb`, a workbook that is not a
    zip (rights-protected), or any workbook openpyxl could not open. This is
    `suits()`, and in a helper `_grids_from_excel` raises `NeedsLedger`.
  - **Council's model is to be asked which part the file is.** The model is
    loaded in the Ledger's process.
  - **Its helper stopped.** The file is put on the skip list first, so the
    Ledger then reports it as skipped instead of reading it.
- Helpers are started as `python -c …`. In the built app they are
  `DatasInferno.exe --ledger-worker`, which `council_qt.py` handles before any
  Qt starts. They connect back over a local pipe
  (`multiprocessing.connection`, using `send_bytes` with JSON). The address and
  key are passed in the environment.
- `COUNCIL_LEDGER_WORKERS` sets the number of helpers: 0 or 1 means none. With
  the default, helpers start only when at least 4 files can go to them. They
  read up to `AHEAD` (64) files past the one the Ledger is waiting for.
- **Cancel** stops every helper. `file_guard.forget_pid` crosses out what they
  were reading, so those files are not put on the skip list.
- **A helper that left a slow read behind** is replaced, so the abandoned
  thread does not keep using a core.

## The skip list across processes (`council_core/file_guard.py`)

The helpers write `skipped_files.json` and `reading_now.json` too. Every
read-modify-write of those files now takes `_locked()`: this process's lock
plus a lock file (`file_guard.lock`, using msvcrt or fcntl). Without it, two
processes adding entries at once lost some of them, which a test shows. On
Windows, a write or read that meets the other process's replace tries again
for up to about 1 s.

## Tests

- `tests/test_ledger_workers.py` covers:
  - the same `Extraction` and the same review workbook with and without
    helpers;
  - the model asked in the Ledger's process;
  - cancel;
  - a helper that crashes;
  - helpers that cannot start;
  - the JSON round trip;
  - two processes writing the skip list at once;
  - `--ledger-worker` starting without Qt.
- `tests/test_ledger_review_load_once.py` covers loading the review once.
- `tests/conftest.py` sets `COUNCIL_LEDGER_WORKERS=0` for every other test,
  because a test that monkeypatches `source_extract` or `Path` would not reach
  a helper's copy of them.

## Not done yet

- Connections rebuild reading in helpers. It's the same idea; the database
  writes would stay on one thread.
- Tesseract reads of pictures run 2 to 4 at a time.
- Vault indexing.
- A first real run on Andy's PC, and on the offline test PC: Windows named
  pipes, the lock file and `CREATE_NO_WINDOW` are covered only by Windows CI.
