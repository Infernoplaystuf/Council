"""
godot_workspace.py — open-Godot-project state + Run/Validate orchestration.

This module owns the *project-level* model that the Godot Workspace tab
binds to:

  • Which folder is the current Godot project (contains project.godot)
  • Which Godot binary to invoke
  • The Run / Validate subprocess lifecycle and stdout/stderr capture

It deliberately knows nothing about Tk or the council. The Tk tab
holds the references; the council subscribes to events emitted here
so a Godot stderr line can become a deliberation trigger.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


# ============================================================
# Environment / settings
# ============================================================

#: Override for the Godot binary path. When unset, GodotRunner falls
#: back to whichever ``godot`` is on PATH.
GODOT_BINARY_ENV = "ANVIL_GODOT_BINARY"


def get_godot_binary() -> str:
    """Return the configured Godot binary path.

    Order: ``ANVIL_GODOT_BINARY`` env override → autodetect (PATH +
    common install/download locations) → bare ``"godot"`` so the error
    message is still sensible when nothing is found.
    """
    env = os.environ.get(GODOT_BINARY_ENV)
    if env:
        return env
    found = detect_godot_binary()
    return found or "godot"


_DETECT_CACHE: List[Optional[str]] = []   # memoised detect result


def detect_godot_binary() -> Optional[str]:
    """Locate a Godot binary so onboarding / the sim runner can use it
    without the user hand-setting a path. Returns an absolute path or
    None. Result is memoised — the filesystem probe runs once per
    process.

    Probes, in order: PATH, then common Windows download/install
    locations. The official Windows build is a zip the user extracts
    to a ``Godot_*`` folder, so we look only a couple of levels deep
    (NOT a full recursive walk, which is slow over OneDrive/Documents).
    The ``_console.exe`` build is preferred because it pipes stdout
    reliably for headless runs.
    """
    if _DETECT_CACHE:
        return _DETECT_CACHE[0]
    result = _detect_godot_binary_uncached()
    _DETECT_CACHE.append(result)
    return result


def _detect_godot_binary_uncached() -> Optional[str]:
    for name in ("godot", "godot4", "Godot_v4", "Godot"):
        found = shutil.which(name)
        if found:
            return found
    roots: List[Path] = []
    home = Path.home()
    roots.extend([home / "Downloads", home / "Desktop", home / "Godot",
                  home / "Documents"])
    for env_key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env_key)
        if base:
            roots.append(Path(base))
    # Shallow patterns only: the exe sits at depth 0-2 under a root.
    patterns = ("Godot*.exe", "Godot*/Godot*.exe", "Godot*/*/Godot*.exe")
    candidates: List[Path] = []
    seen = set()
    for root in roots:
        try:
            rp = root.resolve()
        except Exception:
            continue
        if rp in seen or not rp.exists():
            continue
        seen.add(rp)
        for pat in patterns:
            try:
                for p in rp.glob(pat):
                    if p.is_file():
                        candidates.append(p)
            except Exception:
                continue
        if candidates:
            break  # first root with a hit wins
    if not candidates:
        return None
    console = [c for c in candidates if "_console" in c.name.lower()]
    pick_from = console or candidates
    pick_from.sort(key=lambda p: p.name, reverse=True)
    return str(pick_from[0])


# ============================================================
# Sprite placement (Pixel Art → game)
# ============================================================

def list_tscn_body_nodes(tscn_text: str) -> List[str]:
    """Return node names in a .tscn that are likely game entities — the
    bodies/areas a hand-painted sprite would be attached to (skips the
    root, HUD, Labels, ColorRects, collisions)."""
    out: List[str] = []
    skip_types = ("Label", "ColorRect", "CollisionShape2D", "Control",
                  "CanvasLayer", "Camera2D")
    for m in re.finditer(r'\[node name="([^"]+)" type="([^"]+)"'
                          r'(?:\s+parent="([^"]+)")?', tscn_text):
        name, ntype, parent = m.group(1), m.group(2), m.group(3)
        if parent is None:
            continue  # root
        if ntype in skip_types:
            continue
        if name not in out:
            out.append(name)
    return out


def place_sprite_in_tscn(tscn_text: str, node_name: str,
                          texture_res_path: str,
                          sprite_name: str = "Sprite") -> Tuple[str, bool]:
    """Return ``(new_tscn_text, ok)`` with a ``Sprite2D`` child carrying
    ``texture_res_path`` added under ``node_name``.

    Pure text transform (no Godot needed) so it's unit-testable: adds an
    ``[ext_resource type="Texture2D" ...]`` for the PNG, bumps
    ``load_steps``, and appends the Sprite2D node. ``ok`` is False when
    the target node isn't present.
    """
    # Confirm the target node exists.
    if not re.search(r'\[node name="' + re.escape(node_name) + r'"', tscn_text):
        return tscn_text, False

    # Allocate an ext_resource id not already used.
    used = set(re.findall(r'\[ext_resource [^\]]*id="([^"]+)"', tscn_text))
    n = 1
    while f"{n}_tex" in used or str(n) in used:
        n += 1
    rid = f"{n}_tex"

    lines = tscn_text.splitlines()
    out_lines: List[str] = []
    inserted_ext = False
    for ln in lines:
        out_lines.append(ln)
        if not inserted_ext and ln.startswith("[gd_scene"):
            # Bump load_steps in the header we just appended.
            m = re.search(r"load_steps=(\d+)", out_lines[-1])
            if m:
                out_lines[-1] = out_lines[-1].replace(
                    f"load_steps={m.group(1)}",
                    f"load_steps={int(m.group(1)) + 1}")
            out_lines.append(
                f'[ext_resource type="Texture2D" '
                f'path="{texture_res_path}" id="{rid}"]')
            inserted_ext = True
    if not inserted_ext:
        # No gd_scene header (shouldn't happen) — prepend one.
        out_lines.insert(0, '[gd_scene load_steps=2 format=3]')
        out_lines.insert(1, f'[ext_resource type="Texture2D" '
                            f'path="{texture_res_path}" id="{rid}"]')

    out_lines.append("")
    out_lines.append(f'[node name="{sprite_name}" type="Sprite2D" '
                     f'parent="{node_name}"]')
    out_lines.append(f'texture = ExtResource("{rid}")')
    out_lines.append("")
    return "\n".join(out_lines), True


# ============================================================
# Project model
# ============================================================

# project.godot is INI-shaped: [section]\nkey="value"\n... — Godot
# tolerates Python-style INI but the values are commonly quoted
# strings, paths (``"res://..."``), or PackedStringArray literals.
# We only need a few fields, so a hand parse is cheaper than a real
# INI library and avoids picking up a dependency.
_PROJECT_LINE_RE = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_/]*)\s*=\s*(.+?)\s*$'
)
_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]\s*$")


def _strip_godot_value(raw: str) -> str:
    """Unwrap a Godot manifest value (quoted string, path, or bare)."""
    s = raw.strip()
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    return s


@dataclass
class GodotProject:
    """Snapshot of an open Godot project."""
    root:        Path
    name:        str = ""
    main_scene:  str = ""           # ``res://...`` form, may be empty
    scripts:     List[Path] = field(default_factory=list)
    scenes:      List[Path] = field(default_factory=list)
    other_files: List[Path] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.root / "project.godot"

    def relpath(self, p: Any) -> str:
        """Return a path relative to the project root, or absolute if outside."""
        try:
            return str(Path(p).resolve().relative_to(self.root))
        except Exception:
            return str(p)


def _walk_project(root: Path, project: GodotProject) -> None:
    """Populate project.scripts / scenes / other_files from a recursive walk.

    Skips Godot's per-project caches (``.godot/`` and ``.import/``)
    so the file tree isn't dominated by binary import data.
    """
    SKIP_DIRS = {".godot", ".import", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk doesn't recurse into caches
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        d = Path(dirpath)
        for fn in filenames:
            p = d / fn
            suffix = p.suffix.lower()
            if suffix == ".gd":
                project.scripts.append(p)
            elif suffix == ".tscn":
                project.scenes.append(p)
            else:
                # Include only files Godot users typically edit; skip
                # binaries / imports / autogenerated stuff to keep the
                # tree readable.
                if suffix in {".tres", ".gdshader", ".import",
                              ".cfg", ".md", ".json", ".txt", ".gltf",
                              ".png", ".jpg", ".jpeg", ".webp", ".ogg",
                              ".wav", ".mp3"}:
                    project.other_files.append(p)


def open_project(path: Any) -> Optional[GodotProject]:
    """Probe ``path`` for a Godot 4.x project manifest and populate.

    Returns a populated ``GodotProject`` or ``None`` if the folder
    is not a Godot project. Failures during the walk (permission
    errors, vanished files) degrade to a partial project rather
    than raising — the user's first interaction is still useful.
    """
    p = Path(path).expanduser().resolve()
    manifest = p / "project.godot"
    if not manifest.exists():
        return None

    project = GodotProject(root=p, name=p.name)

    # Parse the few project.godot fields we care about
    try:
        section = ""
        with open(manifest, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\n")
                sec_m = _SECTION_RE.match(line)
                if sec_m:
                    section = sec_m.group(1)
                    continue
                m = _PROJECT_LINE_RE.match(line)
                if not m:
                    continue
                key, val = m.group(1), _strip_godot_value(m.group(2))
                if section == "application":
                    if key == "config/name" and val:
                        project.name = val
                    elif key == "run/main_scene" and val:
                        project.main_scene = val
    except Exception as exc:
        # Don't fail the whole open — just note via empty fields
        print(f"[godot_workspace] could not parse project.godot: {exc!r}")

    try:
        _walk_project(p, project)
    except Exception as exc:
        print(f"[godot_workspace] project walk failed: {exc!r}")

    project.scripts.sort()
    project.scenes.sort()
    project.other_files.sort()
    return project


# ============================================================
# Runner
# ============================================================

class GodotRunner:
    """Subprocess wrapper around the Godot binary.

      • ``run(project)`` — launch the project's main scene and stream
        stdout / stderr through ``on_line(stream, text)`` callbacks
        so the Workspace console + the council can both observe.
      • ``validate(project)`` — invoke ``godot --headless --check-only
        project.godot`` for a fast parse-only check.
      • ``stop()`` — terminate the running subprocess cleanly.

    Threading: stdout/stderr drainers run on daemon threads so the
    GUI thread is never blocked by a chatty Godot run. The exit
    callback fires once both streams have been fully drained AND
    the process has exited (a race we explicitly synchronise via
    a join on the drainer threads).
    """

    def __init__(
        self,
        on_line: Optional[Callable[[str, str], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ):
        self.on_line = on_line or (lambda stream, text: None)
        self.on_exit = on_exit or (lambda rc: None)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []

    # ----------------------------------------------------------------
    # Lifecycle helpers
    # ----------------------------------------------------------------

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """Terminate the running Godot subprocess if any. Polite first
        (terminate) — if it doesn't exit within 2s, escalates to kill."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as exc:
            print(f"[GodotRunner] stop failed: {exc!r}")

    # ----------------------------------------------------------------
    # Internal — spawn + drain
    # ----------------------------------------------------------------

    def _spawn(self, args: List[str], cwd: Path) -> None:
        """Launch ``godot <args>`` from ``cwd`` and pump stdout/stderr.

        Raises RuntimeError if a Godot run is already in flight (the
        UI should disable Run while one is in progress; this is a
        belt-and-braces guard).
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("Godot is already running — stop it first.")

        # On Windows, hide the console window that would otherwise pop
        # up alongside the Godot game window when launched from a GUI.
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            except Exception:
                startupinfo = None
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,        # line-buffered
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            # Godot binary not on PATH and not configured. Surface to
            # the console so the UI can show the user the fix.
            self.on_line(
                "stderr",
                f"[Anvil] Could not find Godot binary: {args[0]!r}. "
                f"Set {GODOT_BINARY_ENV} or pick the executable in "
                f"the Godot Workspace settings.",
            )
            self.on_exit(127)
            return
        except Exception as exc:
            self.on_line("stderr", f"[Anvil] Failed to launch Godot: {exc!r}")
            self.on_exit(-1)
            return

        with self._lock:
            self._proc = proc
            self._threads = []

        t_out = threading.Thread(
            target=self._drain, args=(proc.stdout, "stdout"),
            name="godot-stdout-drain", daemon=True,
        )
        t_err = threading.Thread(
            target=self._drain, args=(proc.stderr, "stderr"),
            name="godot-stderr-drain", daemon=True,
        )
        t_wait = threading.Thread(
            target=self._wait_and_emit, args=(proc, [t_out, t_err]),
            name="godot-wait", daemon=True,
        )
        with self._lock:
            self._threads = [t_out, t_err, t_wait]
        t_out.start()
        t_err.start()
        t_wait.start()

    def _drain(self, stream, label: str) -> None:
        """Pump a subprocess pipe line-by-line into ``self.on_line``."""
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                # Strip the trailing newline but keep internal whitespace
                self.on_line(label, line.rstrip("\r\n"))
        except Exception as exc:
            self.on_line("stderr", f"[Anvil] drain({label}) crashed: {exc!r}")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _wait_and_emit(
        self, proc: subprocess.Popen, drainers: List[threading.Thread],
    ) -> None:
        """Block until the proc exits AND the stream drainers finish,
        then emit the exit callback. Without joining the drainers we
        could fire on_exit before the last few stderr lines arrived."""
        try:
            rc = proc.wait()
        except Exception as exc:
            self.on_line("stderr", f"[Anvil] wait() crashed: {exc!r}")
            rc = -1
        for t in drainers:
            try:
                t.join(timeout=3.0)
            except Exception:
                pass
        with self._lock:
            self._proc = None
        try:
            self.on_exit(rc)
        except Exception as exc:
            print(f"[GodotRunner] on_exit raised: {exc!r}")

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def run(self, project: GodotProject) -> None:
        """Launch the project. Godot picks its own main scene from the
        manifest, so we don't have to pass one explicitly."""
        binary = get_godot_binary()
        self._spawn(
            [binary, "--path", str(project.root)],
            cwd=project.root,
        )

    def validate(self, project: GodotProject) -> None:
        """Headless parse-only validation. Fast (<1s on a small project)
        and produces script-level error reports to stderr."""
        binary = get_godot_binary()
        self._spawn(
            [binary, "--headless", "--path", str(project.root),
             "--check-only"],
            cwd=project.root,
        )
