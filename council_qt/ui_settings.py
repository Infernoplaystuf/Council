"""
council_qt.ui_settings — the "ui" section of the user's settings file.

What this reads lives at ``app_dir() / "ui_settings.json"``: a small file next
to the vault, deliberately separate from backend_settings.json (which the
engine owns and uses to pick a model). The shape is the one the spec names:

    {
      "ui": {
        "profile": "demo" | "developer" | "full" | "custom",
        "custom": {
          "base_profile": "demo" | "developer" | "full",
          "advanced_mode": true | false,
          "visible_tabs": ["council", ...]
        }
      },
      "advanced_mode": true | false      # the retired flag, kept one release
    }

Reads are cheap and tolerant: a missing file, a malformed file, a missing key
each return a sensible default rather than raising. The caller is a GUI loading
at startup; crashing because `~/.council/ui_settings.json` has a stray comma is
worse than any default the file might have held.

MIGRATION, HERE RATHER THAN IN A WIZARD
`load_or_migrate()` turns the legacy world (no `ui` section, maybe an
`advanced_mode` flag) into the new one on first launch. True maps to Developer,
False or absent to Demo — exactly what the spec names. A truly fresh install
(no `advanced_mode` either) becomes Full with no notice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from council_core import paths

#: Where this file lives, by default. Overridable with the env var so a test
#: can point the writer at a tmp_path without touching ~/.council.
ENV_VAR = "COUNCIL_UI_SETTINGS"
FILENAME = "ui_settings.json"

PRESET_NAMES = ("demo", "developer", "full")
PROFILE_NAMES = PRESET_NAMES + ("custom",)

#: What a profile with no `ui` and no `advanced_mode` gets. Per spec §4.
DEFAULT_FIRST_RUN_PROFILE = "full"


def settings_path() -> Path:
    override = os.environ.get(ENV_VAR, "")
    if override:
        return Path(override).expanduser()
    return paths.app_dir() / FILENAME


def _read_raw() -> Dict[str, Any]:
    try:
        text = settings_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_raw(data: Dict[str, Any]) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # A GUI that refuses to open because it cannot persist a preference is
        # worse than one that forgets it. Reported, but not fatal.
        print(f"[ui_settings] could not write {path}: {exc!r}", flush=True)


def default_ui() -> Dict[str, Any]:
    """What the `ui` section looks like for a fresh install."""
    return {
        "profile": DEFAULT_FIRST_RUN_PROFILE,
        "custom": {
            "base_profile": "demo",
            "advanced_mode": False,
            "visible_tabs": [],
        },
    }


def _coerce_ui(section: Any) -> Dict[str, Any]:
    """Shape ``section`` to the schema; drop keys that are the wrong type."""
    out = default_ui()
    if not isinstance(section, dict):
        return out
    profile = section.get("profile")
    if isinstance(profile, str) and profile.lower() in PROFILE_NAMES:
        out["profile"] = profile.lower()
    custom = section.get("custom")
    if isinstance(custom, dict):
        base = custom.get("base_profile")
        if isinstance(base, str) and base.lower() in PRESET_NAMES:
            out["custom"]["base_profile"] = base.lower()
        advanced = custom.get("advanced_mode")
        if isinstance(advanced, bool):
            out["custom"]["advanced_mode"] = advanced
        visible = custom.get("visible_tabs")
        if isinstance(visible, list):
            out["custom"]["visible_tabs"] = [
                str(t) for t in visible if isinstance(t, str)]
    return out


def load_or_migrate() -> Tuple[Dict[str, Any], Optional[str]]:
    """Return the current `ui` section, migrating a legacy file if needed.

    The second value is a one-line notice to show the user — the status-bar
    nudge from spec §6 — or None when nothing migrated.
    """
    raw = _read_raw()
    if isinstance(raw.get("ui"), dict):
        return _coerce_ui(raw["ui"]), None

    legacy_advanced = raw.get("advanced_mode")
    if isinstance(legacy_advanced, bool):
        new_ui = default_ui()
        new_ui["profile"] = "developer" if legacy_advanced else "demo"
        new_ui["custom"]["base_profile"] = new_ui["profile"]
        new_ui["custom"]["advanced_mode"] = legacy_advanced
        raw["ui"] = new_ui
        _write_raw(raw)
        return new_ui, (
            "Advanced mode is now part of your profile. "
            "See Profile menu.")

    new_ui = default_ui()
    raw["ui"] = new_ui
    _write_raw(raw)
    return new_ui, None


def save_ui(section: Dict[str, Any]) -> None:
    """Persist `section` as the file's `ui`. Other keys are preserved."""
    raw = _read_raw()
    raw["ui"] = _coerce_ui(section)
    _write_raw(raw)


def current_advanced_mode(section: Dict[str, Any]) -> bool:
    """Advanced-mode-under-the-active-profile, per spec §5."""
    profile = str(section.get("profile", DEFAULT_FIRST_RUN_PROFILE)).lower()
    if profile == "demo":
        return False
    if profile in ("developer", "full"):
        return True
    if profile == "custom":
        custom = section.get("custom") or {}
        value = custom.get("advanced_mode")
        if isinstance(value, bool):
            return value
        base = str(custom.get("base_profile", "demo")).lower()
        return base in ("developer", "full")
    return False
