"""
Shared fixtures for inferno_local's tests.

The same protections as tests/: a throwaway app folder and vault for the
session (never the user's ~/.council), re-asserted after every test, and the
desktop guard — nothing a test does may open a window on the user's desktop,
and Qt runs offscreen. tests/conftest.py does not reach this folder, so both
are imported here too.
"""
from tests.sandbox_vault import sandbox_env  # noqa: F401  (first: before anything finds the vault)
from tests.desktop_guard import desktop_openers  # noqa: F401
