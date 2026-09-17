"""
council_qt.dialogs — tkinter's dialog API, backed by Qt.

The engine calls messagebox/filedialog/simpledialog at 73 sites (55/11/7). Every
one of them is a line of otherwise toolkit-neutral logic saying "ask the user
something". Rewriting all 73 into idiomatic Qt would be 73 opportunities to
change behaviour by accident, for no gain — so this module keeps tkinter's
names, argument order and RETURN CONVENTIONS, and a ported call site changes
its import rather than its code.

The return conventions are the part worth being careful about:

    askyesno        -> True / False
    askyesnocancel  -> True / False / None      (three states; two sites branch
                                                 on the None, so a bool would
                                                 silently take the wrong path)
    askstring       -> str or None on cancel
    askopenfilename -> "" on cancel, NOT None   (tkinter returns an empty string
                                                 and callers test falsiness)

`parent=` is accepted everywhere because tkinter takes it; Qt uses it to centre
and to decide modality, which is the same job.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

_Btn = QMessageBox.StandardButton


def _box(icon, title: str, message: str, parent=None) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(str(title))
    box.setText(str(message))
    return box


# ---- messagebox -----------------------------------------------------------

def showinfo(title: str = "", message: str = "", parent=None, **_kw) -> str:
    _box(QMessageBox.Icon.Information, title, message, parent).exec()
    return "ok"


def showwarning(title: str = "", message: str = "", parent=None, **_kw) -> str:
    _box(QMessageBox.Icon.Warning, title, message, parent).exec()
    return "ok"


def showerror(title: str = "", message: str = "", parent=None, **_kw) -> str:
    _box(QMessageBox.Icon.Critical, title, message, parent).exec()
    return "ok"


def askyesno(title: str = "", message: str = "", parent=None, **_kw) -> bool:
    box = _box(QMessageBox.Icon.Question, title, message, parent)
    box.setStandardButtons(_Btn.Yes | _Btn.No)
    return box.exec() == _Btn.Yes


def askokcancel(title: str = "", message: str = "", parent=None, **_kw) -> bool:
    box = _box(QMessageBox.Icon.Question, title, message, parent)
    box.setStandardButtons(_Btn.Ok | _Btn.Cancel)
    return box.exec() == _Btn.Ok


def askyesnocancel(title: str = "", message: str = "", parent=None,
                   **_kw) -> Optional[bool]:
    """True / False / None. The None is load-bearing — a caller that treats
    cancel as "no" saves when the user meant to back out."""
    box = _box(QMessageBox.Icon.Question, title, message, parent)
    box.setStandardButtons(_Btn.Yes | _Btn.No | _Btn.Cancel)
    answer = box.exec()
    if answer == _Btn.Yes:
        return True
    if answer == _Btn.No:
        return False
    return None


def askretrycancel(title: str = "", message: str = "", parent=None,
                   **_kw) -> bool:
    box = _box(QMessageBox.Icon.Warning, title, message, parent)
    box.setStandardButtons(_Btn.Retry | _Btn.Cancel)
    return box.exec() == _Btn.Retry


# ---- simpledialog ---------------------------------------------------------

def askstring(title: str = "", prompt: str = "", initialvalue: str = "",
              parent=None, show: str = "", **_kw) -> Optional[str]:
    from PySide6.QtWidgets import QLineEdit
    mode = (QLineEdit.EchoMode.Password if show
            else QLineEdit.EchoMode.Normal)
    text, ok = QInputDialog.getText(parent, str(title), str(prompt), mode,
                                    str(initialvalue or ""))
    return text if ok else None


def askinteger(title: str = "", prompt: str = "", initialvalue: int = 0,
               minvalue: int = -2147483647, maxvalue: int = 2147483647,
               parent=None, **_kw) -> Optional[int]:
    value, ok = QInputDialog.getInt(parent, str(title), str(prompt),
                                    int(initialvalue or 0),
                                    int(minvalue), int(maxvalue))
    return value if ok else None


# ---- filedialog -----------------------------------------------------------

def _filters(filetypes: Optional[Sequence[Tuple[str, Any]]]) -> str:
    """tkinter's [("Images", "*.png *.jpg"), ...] as Qt's "Images (*.png *.jpg)".

    tkinter also accepts a tuple of patterns for one entry, so both shapes are
    handled; an empty list becomes "All files", which is what tkinter shows.
    """
    out: List[str] = []
    for entry in filetypes or ():
        try:
            label, patterns = entry
        except (TypeError, ValueError):
            continue
        if isinstance(patterns, (list, tuple)):
            patterns = " ".join(str(p) for p in patterns)
        out.append(f"{label} ({patterns})")
    out.append("All files (*.*)")
    return ";;".join(out)


def askopenfilename(title: str = "Open", initialdir: str = "",
                    filetypes=None, parent=None, **_kw) -> str:
    path, _ = QFileDialog.getOpenFileName(parent, str(title),
                                          str(initialdir or ""),
                                          _filters(filetypes))
    return path or ""          # "" not None — callers test falsiness


def askopenfilenames(title: str = "Open", initialdir: str = "",
                     filetypes=None, parent=None, **_kw) -> List[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, str(title),
                                            str(initialdir or ""),
                                            _filters(filetypes))
    return list(paths or [])


def asksaveasfilename(title: str = "Save as", initialdir: str = "",
                      initialfile: str = "", defaultextension: str = "",
                      filetypes=None, parent=None, **_kw) -> str:
    start = str(initialdir or "")
    if initialfile:
        start = f"{start}/{initialfile}" if start else str(initialfile)
    path, _ = QFileDialog.getSaveFileName(parent, str(title), start,
                                          _filters(filetypes))
    if path and defaultextension and "." not in path.rsplit("/", 1)[-1]:
        path += defaultextension
    return path or ""


def askdirectory(title: str = "Choose a folder", initialdir: str = "",
                 parent=None, **_kw) -> str:
    path = QFileDialog.getExistingDirectory(parent, str(title),
                                            str(initialdir or ""))
    return path or ""
