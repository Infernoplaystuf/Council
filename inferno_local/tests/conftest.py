"""
Shared fixtures for inferno_local's tests.

The same protections as tests/: a throwaway app folder and vault for the
session (never the user's ~/.council), re-asserted after every test, the
desktop guard — nothing a test does may open a window on the user's desktop,
and Qt runs offscreen — and the Tk guard: the Tk GUIs are deprecated, and
any Tk root or Toplevel a test makes, here or in a python child, raises.
tests/conftest.py does not reach this folder, so all three are imported here
too.
"""
from tests.sandbox_vault import sandbox_env  # noqa: F401  (first: before anything finds the vault)
from tests.desktop_guard import desktop_openers  # noqa: F401
from tests.no_tk_guard import no_tk_windows  # noqa: F401
