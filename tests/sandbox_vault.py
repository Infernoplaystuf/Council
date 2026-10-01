"""
tests.sandbox_vault — every test session gets a throwaway app folder and vault.

The app finds its state through $COUNCIL_APP_DIR (else ~/.council) and its
vault through $COUNCIL_VAULT_ROOT (else <app dir>/vault) — the user's real
ones: documents, conversation logs, role memory, model settings. Tests that
set neither fell through to them. MEASURED on 2026-09-29: one run of
tests/smoke_test.py rewrote vault_index.json, data_in/.vault_col_index.json
and data_in/data_index_cache.pickle in the real ~/.council/vault, and
test_task_chain / smoke_test read (and cached for the rest of the process) the
user's real model_slots.json through local_chat -> model_slots.current().

BOTH are redirected, and the Tk engine's path migration is switched off. A
review of the first version (vault only) found council_gui_engine, on
import, shutil.move()s legacy files from the app dir (node_registry.json,
personality_backends.json, .chromadb, ...) and <repo>/vault/dream3d_docs into
the vault — which here is deleted when the session ends. Measured in a
scratch home: the files were moved into the throwaway vault and gone after
the run. With the app dir sandboxed there is nothing of the user's to move,
and COUNCIL_SKIP_PATH_MIGRATION stops the repo-relative one too.

This runs at IMPORT, from the top of tests/conftest.py and
inferno_local/tests/conftest.py — before any test module is collected.
council_gui_engine computes APP_DIR and VAULT_DIR when it is imported, which
happens during collection, before a session fixture could run.

The override is unconditional: a variable exported in the shell that runs
pytest (say, to the real vault) must not decide where tests write. A test
that needs another vault sets it with monkeypatch, which still wins;
tests/test_paths.py clears both to test the default resolution; and
restore() puts them back after any test that changed them by hand.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

ROOT = tempfile.mkdtemp(prefix="council_test_app_")
VAULT = str(Path(ROOT) / "vault")
Path(VAULT).mkdir()

SANDBOX_ENV = {
    "COUNCIL_APP_DIR": ROOT,
    "COUNCIL_VAULT_ROOT": VAULT,
    "COUNCIL_SKIP_PATH_MIGRATION": "1",
    # The engine answers from a localhost Ollama when no GGUF can load
    # (council_engine._route_chat). On a developer PC that server is real and
    # has real models, so a test that expects "no model configured" — e.g.
    # test_council_tab's real-engine turn — would instead run a real
    # generation. Tests of the Ollama backend turn this back on with
    # monkeypatch and point COUNCIL_OLLAMA_HOST at a fake server.
    "COUNCIL_OLLAMA_FALLBACK": "0",
}
os.environ.update(SANDBOX_ENV)
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)


def restore() -> list:
    """Put the sandbox variables back; return the names that had changed."""
    changed = [k for k, v in SANDBOX_ENV.items() if os.environ.get(k) != v]
    os.environ.update(SANDBOX_ENV)
    return changed


class SandboxEscaped(UserWarning):
    """A test left the sandbox variables changed after it finished."""


try:
    import pytest
except ImportError:                      # script mode (smoke_test's main())
    pytest = None

if pytest is not None:
    @pytest.fixture(autouse=True)
    def sandbox_env():
        """After every test, the sandbox is still in force.

        A test that changed a variable by hand and did not put it back —
        MEASURED: smoke_test's analyst-stats test popped COUNCIL_VAULT_ROOT,
        and every later test in the session resolved the real vault — is
        named in a warning and the sandbox restored, so one careless test
        cannot switch it off for the rest of the run. monkeypatch changes
        are already undone by the time this runs (it is set up first)."""
        yield
        changed = restore()
        if changed:
            import warnings
            warnings.warn(SandboxEscaped(
                f"{', '.join(changed)} changed by this test and not restored; "
                f"the test sandbox has been put back."), stacklevel=2)
