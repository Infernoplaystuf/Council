"""
Shared fixtures for inferno_local's tests.

The same desktop guard as tests/: nothing a test does may open a window on
the user's desktop, and Qt runs offscreen. tests/conftest.py does not reach
this folder, so the fixture is imported here too.
"""
from tests.desktop_guard import desktop_openers  # noqa: F401
