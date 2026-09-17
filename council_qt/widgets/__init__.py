"""
council_qt.widgets — Qt widgets that are not tabs.

A widget lands here when more than one tab needs it, or when it is complicated
enough to deserve its own tests. The transcript is both: the Council tab and
the Dream3D tab each show one, and between them they carry every rich-text
mechanism in the app.
"""
from __future__ import annotations

__all__ = ["transcript"]
