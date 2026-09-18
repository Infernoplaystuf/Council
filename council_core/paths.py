"""
council_core.paths — where the app's data lives. One answer, for both builds.

THE DEFECT THIS EXISTS TO FIX
Ten Qt tabs defaulted `vault_dir` to `~/council_vault`. The Vault tab defaulted
to `~/.council/vault`. The engine's actual answer is neither literal: it is
`$COUNCIL_VAULT_ROOT`, or `APP_DIR/vault` where APP_DIR is `$COUNCIL_APP_DIR`
or `~/.council`.

So on this machine the real vault — GUI projects, conversation logs, datasets,
the Dream3D docs — is at `~/.council/vault`, and the Qt build was reading and
writing `~/council_vault`, a directory it had created itself. Ten tabs saw an
empty vault and reported it as one. Nothing raised, nothing warned: a vault
with nothing in it looks exactly like a fresh install, which is why this
survived the whole port.

It also meant neither environment variable worked. A user who sets
COUNCIL_VAULT_ROOT to put the vault on another drive got it honoured by the Tk
build and ignored by the Qt one.

NOTHING HERE CREATES A DIRECTORY
The engine mkdirs at import; asking a module where something lives should not
make it so. `ensure()` is separate and explicit, and a launch calls it once.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: The engine's default when COUNCIL_APP_DIR is unset.
DEFAULT_APP_DIR = Path.home() / ".council"


def app_dir() -> Path:
    """Where the app keeps everything. `$COUNCIL_APP_DIR`, or ~/.council."""
    override = os.environ.get("COUNCIL_APP_DIR", "")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_APP_DIR


def vault_dir() -> Path:
    """The vault.

    `$COUNCIL_VAULT_ROOT` wins outright — it is how the vault gets put on
    another drive — otherwise it sits under the app directory. Read live rather
    than cached at import, because a test that sets the variable and a launcher
    that sets it are the same case and neither controls import order.
    """
    override = os.environ.get("COUNCIL_VAULT_ROOT", "")
    if override:
        return Path(override).expanduser().resolve()
    engine = sys.modules.get("council_gui_engine")
    from_engine = getattr(engine, "VAULT_DIR", None) if engine else None
    # If the engine is already loaded, its answer is authoritative: it is the
    # one that ran at import, and disagreeing with it would split the two
    # builds in the same process.
    if from_engine:
        return Path(str(from_engine))
    return app_dir() / "vault"


def ensure(path: Path) -> Path:
    """Create it if it is not there, and hand it back.

    Separate from the resolvers on purpose. A function that answers "where is
    the vault" and silently creates one is how a typo in an environment
    variable becomes a new empty vault rather than an error.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path
