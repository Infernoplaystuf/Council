"""
council_core.forge_jobs — describe a tool, get a tool.

The local model writes Python from a plain-English description, the analyst
sandbox validates it (read-only: no delete, no write, no network, no shell) and
test-runs it, and it is saved into <vault>/App_Built_tools/ — UNREVIEWED.

THAT WORD IS THE FEATURE, NOT A DISCLAIMER
A model wrote this code and nobody has read it. Every message this module
produces says so, because the one thing a user must not conclude is that
something validated it for them. The sandbox proves it does not delete, write,
reach the network or shell out; it does not prove the tool is correct, and
those are very different assurances.

WHAT MOVED AND WHY
`tool_forge` and `app_built_tools` were already toolkit-free and already
returned (ok, message, ...) tuples. What was NOT shared was the assembly: which
model call to make, what to do with a failure, and the exact wording the user
sees. That wording is most of the safety here, so it lives in one place.

ONE DEFECT CARRIED OVER DELIBERATELY, ONE FIXED
Fixed: `_forge_save` runs the save synchronously on the GUI thread — it is the
only forge action that does. Saving writes a file and re-validates, so it is
a worker's job.
Kept: generation saves the tool whether or not the user has looked at it. That
is the design — the tool is on disk and marked unreviewed — and changing it
would be a product decision, not a port.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Model settings for generation, exactly as the Tk shell uses them.
TEMPERATURE, NUM_PREDICT, TIMEOUT = 0.2, 700, 120

#: The framing that must survive any rewording.
UNREVIEWED = ("The tool is saved but UNREVIEWED — review the code on the left. "
              "Select it in the list and press 'Run Selected' to try it.")


@dataclass
class ForgeResult:
    ok: bool
    status: str                   # the one-line status bar text
    body: str = ""                # the output pane text
    name: Optional[str] = None
    code: str = ""
    error: Optional[BaseException] = None


@dataclass
class ToolListResult:
    ok: bool
    message: str
    rows: List[Tuple[str, str]] = field(default_factory=list)   # (name, blurb)
    names: List[str] = field(default_factory=list)
    error: Optional[BaseException] = None


def default_model_call(prompt: str) -> str:
    """The generation call. Separated so a test never reaches a model."""
    import council_engine
    return council_engine.local_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=TEMPERATURE, num_predict=NUM_PREDICT, timeout=TIMEOUT)


def list_tools(vault_dir: Any) -> ToolListResult:
    """Every tool the app has built, as rows for a list.

    The Tk version swallows any failure into an empty list, so a broken tools
    directory looks exactly like an empty one.
    """
    try:
        import app_built_tools
        tools = app_built_tools.list_tools(vault_dir=Path(vault_dir)) or []
    except Exception as exc:                              # noqa: BLE001
        return ToolListResult(False, f"Could not list the tools: {exc!r}",
                              error=exc)
    rows, names = [], []
    for tool in tools:
        name = tool.get("name") or "(unnamed)"
        blurb = (tool.get("description") or "")[:50]
        rows.append((name, blurb))
        names.append(name)
    return ToolListResult(
        True,
        (f"{len(rows)} tool(s)." if rows else
         "No tools yet. Describe one above and press Generate."),
        rows=rows, names=names)


def forge(task: str, vault_dir: Any, *,
          model_call: Optional[Callable[[str], str]] = None) -> ForgeResult:
    """Write, validate and save a tool from a description.

    Blocking — call it from a worker. The Tk version does; only its save does
    not.
    """
    task = (task or "").strip()
    if not task:
        return ForgeResult(False, "Describe a tool first.")

    try:
        import tool_forge
        ok, message, name, code = tool_forge.generate_tool(
            task, model_call or default_model_call,
            author="council", vault_dir=Path(vault_dir))
    except Exception as exc:                              # noqa: BLE001
        return ForgeResult(False, f"⚠ {exc!r}"[:80],
                           body=f"{exc!r}\n\nYou can edit the code on the left "
                                "and press 'Save Edited Code'.",
                           error=exc)

    if ok:
        return ForgeResult(
            True, f"✓ Saved '{name}' — UNREVIEWED. Select it and Run to test.",
            body=f"{message}\n\n{UNREVIEWED}", name=name, code=code or "")
    return ForgeResult(
        False, "⚠ " + str(message)[:80],
        body=f"{message}\n\nYou can edit the code on the left and press "
             "'Save Edited Code'.",
        code=code or "")


def save_edited(code: str, vault_dir: Any) -> ForgeResult:
    """Save hand-edited code as a tool.

    Blocking — this writes a file and re-validates, so it belongs on a worker.
    The Tk version runs it on the GUI thread, which is the only forge action
    that does.
    """
    code = (code or "").strip()
    if not code:
        return ForgeResult(False,
                           "Nothing to save — generate or paste code first.")
    try:
        import tool_forge
        ok, message, name = tool_forge.save_edited_tool(
            code, description="", author="user", vault_dir=Path(vault_dir))
    except Exception as exc:                              # noqa: BLE001
        return ForgeResult(False, f"⚠ {exc!r}"[:80], body=repr(exc), error=exc)
    return ForgeResult(ok,
                       f"✓ Saved '{name}'." if ok else "⚠ " + str(message)[:80],
                       body=str(message), name=name, code=code)


def run_tool(name: str, vault_dir: Any) -> ForgeResult:
    """Run a saved tool against the vault's input folder.

    Blocking. The tool goes through the analyst sandbox, so it can read and
    compute and cannot delete, write, reach the network or shell out.
    """
    if not name:
        return ForgeResult(False, "Select a tool in the list to run.")
    try:
        import app_built_tools
        frame, message = app_built_tools.run_tool(name,
                                                  vault_dir=Path(vault_dir))
    except Exception as exc:                              # noqa: BLE001
        return ForgeResult(False, f"⚠ {name}: {exc!r}"[:80], body=repr(exc),
                           error=exc)

    body = str(message)
    if frame is not None:
        try:
            body = f"{message}\n\n{frame.to_string(max_rows=50)}"
        except Exception:                                 # noqa: BLE001
            pass                     # a frame that will not render is not fatal
    return ForgeResult(True, f"Ran '{name}'.", body=body, name=name)
