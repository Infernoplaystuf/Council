"""
diff_view.py — side-by-side diff dialog for reviewing AI-generated
file edits before keeping or reverting them.

Used by the Godot Workspace tab after ``GodotCoder.run`` succeeds:
the file is already validated on disk, but the user gets a chance
to see exactly what changed and either keep the edit (Apply, no-op
since the disk state is already correct) or roll it back to the
backup bytes (Reject, calls ``GodotCoder._atomic_restore``).

UI shape:

    ┌── /path/to/file.gd ────────────────────────  [Reject] [Apply] [×]
    │
    │ ┌── Before ────────────┐ ┌── After ─────────────┐
    │ │ untouched line       │ │ untouched line       │
    │ │ removed line   (red) │ │                      │
    │ │                      │ │ added line   (green) │
    │ │ untouched            │ │ untouched            │
    │ └──────────────────────┘ └──────────────────────┘
    │
    │ 5 lines changed (3 added, 2 removed)

Implementation notes:
  • Diff is computed via ``difflib.SequenceMatcher.get_opcodes()``
    on the line lists. Per-line tagging is more legible than
    character-level for code review.
  • Both Text widgets share a y-scroll callback so the two sides
    stay aligned. Mouse-wheel events on either widget drive both.
  • Equal / delete / insert / replace opcodes get distinct line-
    background tags. A "gutter" tk.Text on the far left shows
    "+/-/~" markers per line and is wheel-synced to the other two.
  • Apply / Reject / Close-as-Apply are the only exits. The dialog
    is modal (``transient``) — the user can't drift back to the
    main window mid-decision.
"""

from __future__ import annotations

import difflib
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable, List, Optional, Sequence, Tuple


# ============================================================
# Diff computation
# ============================================================

def _split_lines(text: str) -> List[str]:
    """Split with keepends=False so we can rejoin with deterministic
    newlines and avoid trailing-blank surprises across platforms."""
    if not text:
        return []
    # splitlines() handles CR / CRLF / LF uniformly
    return text.splitlines()


def _build_aligned_diff(
    before: Sequence[str],
    after: Sequence[str],
) -> Tuple[List[Tuple[str, str, str]], int, int, int]:
    """Align the two line lists into a flat list of rows for side-by-
    side rendering.

    Returns ``(rows, added, removed, modified)`` where each row is
    ``(tag, before_line, after_line)`` and ``tag`` is one of:
      "equal"   — both sides match
      "delete"  — only on the left
      "insert"  — only on the right
      "replace" — different content on both sides

    For "delete" rows ``after_line`` is "" and a gap is inserted on
    the right at render time so line numbers line up. Same idea on
    "insert" rows.

    The counters are line totals (lines removed, lines added,
    lines modified — replace contributes to both add+remove via
    ``modified``, never inflated as both).
    """
    sm = difflib.SequenceMatcher(a=list(before), b=list(after))
    rows: List[Tuple[str, str, str]] = []
    added = removed = modified = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(("equal", before[i1 + k], after[j1 + k]))
        elif tag == "delete":
            for k in range(i2 - i1):
                rows.append(("delete", before[i1 + k], ""))
            removed += i2 - i1
        elif tag == "insert":
            for k in range(j2 - j1):
                rows.append(("insert", "", after[j1 + k]))
            added += j2 - j1
        elif tag == "replace":
            # Pair up replace lines 1:1 as far as possible; pad the
            # shorter side with empties so the user can still scan
            # line-for-line.
            ldel = i2 - i1
            lins = j2 - j1
            pairs = max(ldel, lins)
            for k in range(pairs):
                bl = before[i1 + k] if k < ldel else ""
                al = after[j1 + k] if k < lins else ""
                rows.append(("replace", bl, al))
            modified += pairs
    return rows, added, removed, modified


# ============================================================
# Dialog
# ============================================================

# Theme colours — kept inline rather than importing branding so this
# module stays usable in isolation (e.g. from a unit-test harness).
_BG       = "#0f0c0c"
_PANEL_BG = "#1a1414"
_FG       = "#d4d4d4"
_MUTED    = "#7a7575"
_EQUAL_BG = "#0f0c0c"        # same as panel — invisible "no change"
_DEL_BG   = "#3a1818"        # deep red wash
_INS_BG   = "#1b3318"        # deep green wash
_REP_BG   = "#3a2a14"        # amber wash for replace
_GUTTER_BG = "#0a0808"


class _DiffDialog:
    """The actual Toplevel. Built once per show_diff_dialog call."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        file_path: Path,
        original_text: str,
        proposed_text: str,
        on_apply: Callable[[], None],
        on_reject: Callable[[], None],
        is_new_file: bool,
    ):
        self.parent = parent
        self.file_path = file_path
        self.on_apply = on_apply
        self.on_reject = on_reject
        self.is_new_file = is_new_file

        before_lines = _split_lines(original_text)
        after_lines  = _split_lines(proposed_text)
        rows, added, removed, modified = _build_aligned_diff(
            before_lines, after_lines,
        )

        # ── Window setup ────────────────────────────────────────
        win = tk.Toplevel(parent)
        self.win = win
        verb = "New file" if is_new_file else "Review change"
        win.title(f"{verb} — {file_path.name}")
        win.configure(bg=_PANEL_BG)
        win.geometry("980x620")
        try:
            win.transient(parent.winfo_toplevel())
        except Exception:
            pass
        # Treat the window close button (X) the same as Apply — the
        # disk already has the proposed change; the safe default is
        # "keep what's there".
        win.protocol("WM_DELETE_WINDOW", self._do_apply)
        win.bind("<Escape>", lambda e: self._do_apply())

        # ── Header strip ────────────────────────────────────────
        hdr = tk.Frame(win, bg=_PANEL_BG)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        rel_label = str(file_path)
        if is_new_file:
            rel_label += "   (new file)"
        tk.Label(hdr, text=rel_label, bg=_PANEL_BG, fg=_FG,
                 font=("Consolas", 10)).pack(side="left")

        # Right-aligned buttons. Apply is green, Reject is red.
        # Tk widgets accept colour args; ttk Buttons need a Style.
        # Plain tk.Button gives the colour we want with one line.
        reject_label = (
            "🗑 Reject (delete new file)" if is_new_file
            else "↶ Reject (restore original)"
        )
        tk.Button(
            hdr, text=reject_label, command=self._do_reject,
            bg="#3a1818", fg="#ffe0e0",
            activebackground="#552020", activeforeground="#ffe0e0",
            relief="flat", borderwidth=0, padx=10, pady=4,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="right")
        tk.Button(
            hdr, text="✓ Apply (keep change)", command=self._do_apply,
            bg="#1b3318", fg="#dfffd0",
            activebackground="#2a4d24", activeforeground="#dfffd0",
            relief="flat", borderwidth=0, padx=10, pady=4,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
        ).pack(side="right", padx=(0, 6))

        # ── Summary strip ───────────────────────────────────────
        if is_new_file:
            summary = f"{len(after_lines)} line(s) in new file"
        elif not rows:
            summary = "(empty diff — files are identical)"
        else:
            summary = (
                f"{added + removed + modified} line(s) changed   —   "
                f"+{added} added, −{removed} removed"
                + (f", ~{modified} modified" if modified else "")
            )
        tk.Label(win, text=summary, bg=_PANEL_BG, fg=_MUTED,
                 font=("Segoe UI", 9), anchor="w",
                 ).pack(fill="x", padx=8, pady=(0, 4))

        # ── Body — three text widgets side-by-side ──────────────
        body = tk.Frame(win, bg=_PANEL_BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Gutter (left edge) — narrow strip showing +/-/~ markers
        self.gutter = tk.Text(
            body, width=3, wrap="none",
            bg=_GUTTER_BG, fg=_MUTED, font=("Consolas", 10),
            relief="flat", borderwidth=0, padx=2, pady=2,
            cursor="arrow", takefocus=0,
        )
        self.gutter.pack(side="left", fill="y")

        # Right side — a horizontal frame holding before/after + scrollbar
        right = tk.Frame(body, bg=_PANEL_BG)
        right.pack(side="left", fill="both", expand=True)

        # Single shared scrollbar drives all three text widgets
        self.sb = ttk.Scrollbar(right, orient="vertical",
                                 command=self._on_scrollbar)
        self.sb.pack(side="right", fill="y")

        # "Before" pane
        before_frame = tk.Frame(right, bg=_PANEL_BG)
        before_frame.pack(side="left", fill="both", expand=True,
                          padx=(0, 4))
        tk.Label(before_frame,
                 text="Before" if not is_new_file else "Before (empty)",
                 bg=_PANEL_BG, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x")
        self.before_text = tk.Text(
            before_frame, wrap="none",
            bg=_BG, fg=_FG, font=("Consolas", 10),
            relief="flat", borderwidth=0, padx=4, pady=2,
            cursor="arrow", takefocus=0,
        )
        self.before_text.pack(fill="both", expand=True)

        # "After" pane
        after_frame = tk.Frame(right, bg=_PANEL_BG)
        after_frame.pack(side="left", fill="both", expand=True)
        tk.Label(after_frame, text="After",
                 bg=_PANEL_BG, fg=_MUTED,
                 font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x")
        self.after_text = tk.Text(
            after_frame, wrap="none",
            bg=_BG, fg=_FG, font=("Consolas", 10),
            relief="flat", borderwidth=0, padx=4, pady=2,
            cursor="arrow", takefocus=0,
        )
        self.after_text.pack(fill="both", expand=True)

        # ── Configure colour tags on both panes ────────────────
        for t in (self.before_text, self.after_text, self.gutter):
            t.tag_configure("equal",   background=_EQUAL_BG)
            t.tag_configure("delete",  background=_DEL_BG)
            t.tag_configure("insert",  background=_INS_BG)
            t.tag_configure("replace", background=_REP_BG)
            t.tag_configure("gap",     foreground=_MUTED)

        # ── Render the rows ────────────────────────────────────
        self._render_rows(rows)

        # ── Wire shared scrolling ──────────────────────────────
        for t in (self.before_text, self.after_text, self.gutter):
            t.configure(yscrollcommand=self._on_textscroll)
            # Mouse wheel: bind to each widget so wheel works from
            # any pane. Windows / mac / linux use different events.
            t.bind("<MouseWheel>",      self._on_mousewheel)
            t.bind("<Button-4>",        self._on_mousewheel)  # X11 up
            t.bind("<Button-5>",        self._on_mousewheel)  # X11 down

        # Disable editing — the diff dialog is read-only by design
        for t in (self.before_text, self.after_text, self.gutter):
            t.configure(state="disabled")

        # Focus the Apply button so Enter accepts by default
        win.after(50, lambda: self._set_focus())

    # ── Rendering ──────────────────────────────────────────────

    def _render_rows(self, rows: List[Tuple[str, str, str]]) -> None:
        """Write each row into the three text widgets and tag the
        appropriate background."""
        if not rows:
            return
        for (tag, bl, al) in rows:
            # Map opcode → gutter marker
            marker = {
                "equal":   "  ",
                "delete":  "- ",
                "insert":  "+ ",
                "replace": "~ ",
            }.get(tag, "  ")
            self.gutter.insert("end", marker + "\n", (tag,))
            # For delete rows, the After side is blank but we still
            # need a newline-occupying line so the rows align.
            if tag == "delete":
                self.before_text.insert("end", bl + "\n", (tag,))
                self.after_text.insert("end", "\n", ("gap",))
            elif tag == "insert":
                self.before_text.insert("end", "\n", ("gap",))
                self.after_text.insert("end", al + "\n", (tag,))
            else:
                self.before_text.insert("end", bl + "\n", (tag,))
                self.after_text.insert("end", al + "\n", (tag,))

    # ── Synchronised scrolling ─────────────────────────────────
    #
    # We have three Text widgets and one shared Scrollbar. Tk's
    # yscrollcommand fires whenever any widget scrolls; we forward
    # to the others. The Scrollbar's command drives all three via
    # ``_on_scrollbar``. Mouse-wheel events directly call yview
    # on all three.

    def _on_textscroll(self, first: str, last: str) -> None:
        # One Text widget scrolled (cursor key, keyboard); update
        # the scrollbar and keep the other two in sync.
        self.sb.set(first, last)
        try:
            f = float(first)
        except Exception:
            return
        for t in (self.before_text, self.after_text, self.gutter):
            try:
                t.yview_moveto(f)
            except Exception:
                pass

    def _on_scrollbar(self, *args) -> None:
        for t in (self.before_text, self.after_text, self.gutter):
            try:
                t.yview(*args)
            except Exception:
                pass

    def _on_mousewheel(self, event) -> str:
        # Cross-platform delta normalisation
        if event.num == 4:        # X11 wheel up
            delta = -1
        elif event.num == 5:      # X11 wheel down
            delta = 1
        else:
            # Windows / macOS — event.delta is +120 / -120 (Win)
            # or smaller increments (mac). Normalise to ±1 per
            # notch.
            delta = -1 if event.delta > 0 else 1
        for t in (self.before_text, self.after_text, self.gutter):
            try:
                t.yview_scroll(delta, "units")
            except Exception:
                pass
        return "break"

    # ── Decisions ──────────────────────────────────────────────

    def _set_focus(self) -> None:
        try:
            self.win.lift()
            self.win.focus_force()
        except Exception:
            pass

    def _do_apply(self) -> None:
        try:
            self.on_apply()
        except Exception as exc:
            print(f"[diff_view] on_apply crashed: {exc!r}")
        try:
            self.win.destroy()
        except Exception:
            pass

    def _do_reject(self) -> None:
        try:
            self.on_reject()
        except Exception as exc:
            print(f"[diff_view] on_reject crashed: {exc!r}")
        try:
            self.win.destroy()
        except Exception:
            pass


# ============================================================
# Public entry point
# ============================================================

def show_diff_dialog(
    parent: Any,
    *,
    file_path: Any,
    original_text: str,
    proposed_text: str,
    on_apply: Optional[Callable[[], None]] = None,
    on_reject: Optional[Callable[[], None]] = None,
    is_new_file: bool = False,
) -> None:
    """Pop a modal diff review dialog. Returns immediately — the
    callbacks fire when the user makes a decision.

    Contract:
      • ``on_apply`` is also called when the user closes the window
        (X / Escape). Default = "the disk state was kept, no further
        action needed."
      • ``on_reject`` is what the caller wires up to revert: for an
        existing file, restore from the original bytes; for a new
        file (``is_new_file=True``), delete the file silently.
      • ``original_text`` may be empty when ``is_new_file=True``.
    """
    apply_cb = on_apply or (lambda: None)
    reject_cb = on_reject or (lambda: None)
    _DiffDialog(
        parent,
        file_path=Path(file_path),
        original_text=original_text or "",
        proposed_text=proposed_text or "",
        on_apply=apply_cb,
        on_reject=reject_cb,
        is_new_file=is_new_file,
    )
