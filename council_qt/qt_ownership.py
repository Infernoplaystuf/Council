"""
council_qt.qt_ownership — one owner for every widget a layout gives Qt.

PYSIDE6 6.12 LEAVES A WIDGET OWNED TWICE when the layout holding it is added
to a layout that is already on a widget:

    row = QHBoxLayout()
    row.addWidget(button)          # row has no widget yet
    outer.addLayout(row)           # outer is on the view: Qt reparents button

Qt gives the button a parent, but PySide goes on counting it as Python's own.
A view freed by Python's garbage collector (any closure over ``self`` is a
cycle) is then taken apart in no fixed order: when the parent's C++ side goes
first it deletes the button, and then the button's wrapper deletes it again -
a segfault in QObject::~QObject at whichever garbage collection comes next.
Seen as crashes of the Qt tests under 6.12 only (2026-10-21), in a test far
from the view that left the garbage; 6.10 hands such a widget to its parent.
``QWidget.setLayout`` and ``addWidget`` on an attached layout are not affected.

Every view in the app builds rows that way, so this is not fixed view by view:
``install`` wraps the calls that add a layout to a layout (QBoxLayout's
addLayout and insertLayout, QGridLayout.addLayout, QFormLayout's addRow,
insertRow and setLayout) so that afterwards each widget and layout of the one
added that has a Qt parent is handed to it. ``setParent`` with the parent it
already has does that: Qt itself returns early (nothing is hidden or moved),
PySide records the new owner. Installed when ``council_qt`` is imported.
"""
from __future__ import annotations

import functools

#: (class name, method) wrapped by install()
_ATTACHES = (("QBoxLayout", "addLayout"), ("QBoxLayout", "insertLayout"),
             ("QGridLayout", "addLayout"),
             ("QFormLayout", "addRow"), ("QFormLayout", "insertRow"),
             ("QFormLayout", "setLayout"))

_installed = False


def hand_to_parent(obj) -> None:
    """Make ``obj`` owned by its Qt parent, if Python owns it as well."""
    import shiboken6
    parent = obj.parent()
    if parent is not None and shiboken6.ownedByPython(obj):
        obj.setParent(parent)


def adopt(layout) -> None:
    """Hand ``layout``, and every widget and layout inside it, to its Qt parent."""
    hand_to_parent(layout)
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if widget is not None:
            hand_to_parent(widget)
        inner = item.layout()
        if inner is not None:
            adopt(inner)


def _adopting(method):
    from PySide6.QtWidgets import QLayout

    @functools.wraps(method)
    def call(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        for arg in (*args, *kwargs.values()):
            if isinstance(arg, QLayout):
                adopt(arg)
        return result
    call._council_adopts = True
    return call


def install() -> bool:
    """Wrap the layout-into-layout calls (once). False without PySide6."""
    global _installed
    if _installed:
        return True
    try:
        from PySide6 import QtWidgets
        import shiboken6  # noqa: F401
    except ImportError:
        return False
    for cls_name, name in _ATTACHES:
        cls = getattr(QtWidgets, cls_name)
        method = cls.__dict__.get(name)
        if method is not None and not getattr(method, "_council_adopts", False):
            setattr(cls, name, _adopting(method))
    _installed = True
    return True
