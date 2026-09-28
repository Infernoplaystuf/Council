"""
council_core.apothecary — the Apothecary's toolkit-free half, for both shells.

The node registry, SSH engine, health monitor and provisioning wizard already
live toolkit-free in `apothecary_engine.py` (lines 1-940) and the Tk console
still imports them from there, so they are re-exported here rather than moved:
one copy, with the engine's own defects fixed in place for both front ends.

What is NEW here is everything the Tk console did by string surgery on widget
text — and got wrong (docs/qt_migration/remaining_tabs_requirements.md
§apothecary, defects numbered as in its table):

   6  `ram_gb` was parsed out of the combobox label with replace/split and
      raised ValueError for both "+ AI HAT+" models, so Save did nothing.
      `ram_gb_for` reads the number with a regex.
   7  The selected node was recovered by splitting the list row on spaces, so
      "living room pi" resolved to "living". A Qt row carries its node's NAME
      as item data; `row_label` is display only and is never parsed.
  12  Re-running Discover for a known node replaced its record with a bare
      8-field NodeEntry, wiping hardware metadata and model history.
      `merge_discovered` starts from the existing record.
  13  Renaming in the Edit dialog appended a duplicate, because the registry
      upserts by name. `merge_form` reports the previous name so the caller can
      delete it (`Apothecary.save_node`).

STORED PASSWORDS
`STORE_PASSWORDS = True` is carried over from the Tk build unchanged: SSH
passwords are written in plain text to vault/node_registry.json. That is a
decision for the owner, not something a port should change silently.
"""
from __future__ import annotations

import re
import socket
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import apothecary_engine as _ae
from apothecary_engine import (PI_COUNCIL_ROLE, PI_MODEL_RECOMMENDATIONS,  # noqa: F401
                               STATUS_ICON, Apothecary, NodeEntry,
                               confirm_and_get_real_ip, discover_pi, now_iso)

render_steps = _ae._render_steps

#: Carried over from council_gui_engine.STORE_PASSWORDS — see the docstring.
STORE_PASSWORDS = True

PI_MODELS: Tuple[str, ...] = tuple(PI_MODEL_RECOMMENDATIONS)
COUNCIL_ROLES = ("heavy", "fast", "unassigned")
AUTH_METHODS = ("password", "key")
AI_HAT_TOPS = 26.0
DEFAULT_MODEL = "qwen2.5:3b"

#: status -> theme token. The Tk console hard-coded Catppuccin hex values.
STATUS_TOKEN = {"online": "success", "offline": "error",
                "degraded": "warning", "unknown": "muted_fg"}


def registry_path(vault_dir: Path) -> Path:
    """Where the Tk build keeps it (council_gui_engine.REGISTRY_PATH)."""
    return Path(vault_dir) / "node_registry.json"


def status_icon(status: str) -> str:
    return STATUS_ICON.get(status, "?")


# ============================================================
# Rows and details — display only, never parsed back
# ============================================================

def row_label(node: NodeEntry) -> str:
    label = (f"{status_icon(node.status)} {node.name:<16} "
             f"{node.username}@{node.host}:{node.port}")
    if node.model:
        label += f"  [{node.model}]"
    if node.last_seen:
        label += f"  seen:{node.last_seen[:10]}"
    return label


def detail_line(node: NodeEntry) -> str:
    # The two "+ AI HAT+" model names already say so; Tk said it twice.
    hat = ("  AI HAT+" if node.has_ai_hat
           and "AI HAT" not in (node.pi_model or "") else "")
    role = f"  role:{node.council_role}" if node.council_role else ""
    installed = (f"  models:{len(node.installed_models)}"
                 if node.installed_models else "")
    return (f"{node.name}  |  {node.pi_model or 'unknown hardware'}{hat}{role}"
            f"  |  {node.username}@{node.host}:{node.port}  |  "
            f"status:{node.status}{installed}  |  "
            f"active:{node.active_model or '—'}")


def ollama_url(node: NodeEntry) -> str:
    return f"http://{node.host}:{node.ollama_port}"


def log_tag(msg: str, error: bool = False) -> str:
    """ok / err / warn / info — the Tk console's rule. ✓ wins over the flag,
    as it does in Tk."""
    if "✓" in msg:
        return "ok"
    if "✗" in msg or error:
        return "err"
    if "⚠" in msg:
        return "warn"
    return "info"


def wizard_tag(msg: str, error: bool = False) -> str:
    """The wizard log adds a header colour for "[ Task ]" and "===" lines."""
    if error:
        return "err"
    if "✓" in msg:
        return "ok"
    if "⚠" in msg:
        return "warn"
    stripped = msg.lstrip("\n")
    if stripped.startswith("[") or stripped.startswith("="):
        return "hdr"
    return "info"


# ============================================================
# The Add/Edit form
# ============================================================

_RAM_RE = re.compile(r"\((\d+)\s*GB\)", re.IGNORECASE)


def ram_gb_for(pi_model: str) -> int:
    """RAM in GB from a Pi-model label; 0 when it names none. Defect 6."""
    m = _RAM_RE.search(pi_model or "")
    return int(m.group(1)) if m else 0


def auto_role(pi_model: str) -> str:
    return PI_COUNCIL_ROLE.get(pi_model, "")


def recommendations(pi_model: str) -> List[str]:
    return list(PI_MODEL_RECOMMENDATIONS.get(pi_model, []))


class FormError(ValueError):
    pass


def merge_form(existing: Optional[NodeEntry],
               form: Dict[str, object]) -> Tuple[NodeEntry, Optional[str]]:
    """(entry to save, previous name if this is a rename).

    ``form`` holds the dialog's values as strings/bools. Everything the form
    does not show — model, installed_models, model_log, active_model, status,
    last_seen, created_at, last_status_check — is carried from ``existing``.
    Raises FormError with a message a dialog can show.
    """
    name = str(form.get("name", "")).strip()
    host = str(form.get("host", "")).strip()
    if not name or not host:
        raise FormError("Name and Host are required.")
    try:
        port = int(str(form.get("port", "") or 22))
        oll = int(str(form.get("ollama_port", "") or 11434))
    except ValueError:
        raise FormError("Ports must be whole numbers.") from None
    pi_model = str(form.get("pi_model", "") or "")
    has_hat = bool(form.get("has_ai_hat", False))
    base = existing if existing is not None else NodeEntry(
        name=name, host=host, created_at=now_iso())
    entry = replace(
        base,
        name=name, host=host, port=port,
        username=str(form.get("username", "")).strip() or "pi",
        auth_method=str(form.get("auth_method", "password")),
        password=str(form.get("password", "")),
        key_path=str(form.get("key_path", "")).strip(),
        ollama_port=oll,
        notes=str(form.get("notes", "")).strip(),
        pi_model=pi_model,
        has_ai_hat=has_hat,
        ai_hat_tops=AI_HAT_TOPS if has_hat else 0.0,
        ram_gb=ram_gb_for(pi_model),
        council_role=str(form.get("council_role", "") or "unassigned"),
        # Lists are copied so the saved entry never aliases the old record.
        installed_models=list(base.installed_models or []),
        model_log=list(base.model_log or []),
    )
    previous = existing.name if existing is not None \
        and existing.name != name else None
    return entry, previous


def merge_discovered(existing: Optional[NodeEntry], name: str, host: str,
                     username: str, password: str) -> NodeEntry:
    """The record Discover saves. Defect 12: starts from the existing one."""
    fields = dict(host=host, port=22, username=username or "pi",
                  auth_method="password", password=password,
                  status="online", last_seen=now_iso())
    if existing is None:
        return NodeEntry(name=name, **fields)
    return replace(existing, name=name, **fields)


# ============================================================
# The wizard and the Council registration
# ============================================================

def desktop_ip() -> str:
    """This machine's LAN address, or "" — the address the Pi keepalive pings.
    Opens no connection: a UDP connect only picks a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:                                     # noqa: BLE001
        return ""


def bat_candidates() -> List[Path]:
    return [Path.home() / "council_ai" / "launch_council.bat",
            Path.home() / "Desktop" / "launch_council.bat",
            Path("launch_council.bat")]


def find_launch_bat() -> Optional[Path]:
    return next((p for p in bat_candidates() if p.exists()), None)


def patch_launch_bat(text: str, url: str) -> str:
    """Set COUNCIL_PI_HOSTS in a launch_council.bat, Tk's rule verbatim.

    Replaces an existing line; otherwise inserts before
    OLLAMA_MAX_LOADED_MODELS. A file with neither is returned unchanged —
    the caller checks, because "Updated" on an unchanged file is a lie.
    """
    line = f"set COUNCIL_PI_HOSTS={url}"
    if "COUNCIL_PI_HOSTS" in text:
        return re.sub(r"set COUNCIL_PI_HOSTS=[^\r\n]*", lambda _m: line, text)
    return text.replace("set OLLAMA_MAX_LOADED_MODELS",
                        line + "\nset OLLAMA_MAX_LOADED_MODELS", 1)
