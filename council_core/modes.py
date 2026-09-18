"""
council_core.modes — which build this is.

ADVANCED MODE ADDS TABS, IT DOES NOT UNLOCK A SETTING
Six tabs exist only under it: Librarian, Nodes, Apothecary and their
neighbours. They are the ones that commit the whole vault to git, SSH into a
Raspberry Pi, or rewrite the model registry — operations that are fine when
you went looking for them and alarming when you did not.

So a Qt build that registers them unconditionally is not a port; it is a
behaviour change that puts "git commit my entire vault" in front of everyone.

READ LIVE, NOT CACHED AT IMPORT
The Tk build evaluates this once at module import, which is fine for a process
that never changes it. A test that sets the variable and a launcher that sets
it are the same case, and neither controls import order.
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

#: What the environment variable accepts, matching the Tk build exactly.
TRUE_VALUES = ("1", "true", "yes")

ENV_VAR = "COUNCIL_ADVANCED"
FLAG = "--advanced"


def advanced(argv: Optional[Sequence[str]] = None) -> bool:
    """Whether this build shows the advanced tabs.

    Either `COUNCIL_ADVANCED=1` in the environment or `--advanced` on the
    command line — the same two the Tk shell honours.
    """
    if os.environ.get(ENV_VAR, "").strip().lower() in TRUE_VALUES:
        return True
    return FLAG in list(argv if argv is not None else sys.argv)
