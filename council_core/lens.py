"""
council_core.lens — parallel critique, with no council involved.

WHAT A LENS IS, AND WHAT IT IS NOT
Paste a draft, a spec, a chunk of code; tick which personalities should look at
it; get each one's independent critique side by side.

None of the council machinery runs. No judge, no route, no panel selection, no
debate, no synthesis, no confidence, no verdict. ONE FIXED PROMPT — the same
for every role — goes to each ticked personality in parallel, and the answers
are shown as they arrive. That is the whole feature, and it is worth stating
plainly because "ask several models the same thing at once" reads like a
cut-down deliberation and is not one: the point is that the roles DO NOT see
each other's answers.

THREE DEFECTS THE RECONNAISSANCE FOUND, FIXED HERE

1. ONE OF THE ELEVEN CHECKBOXES CANNOT EVER WORK.
   The Tk tab offers `musician`, and there is no musician personality slot
   anywhere — not in the required roles, not in the optional ones, not in
   `_unpack_personalities`. Ticking it gets "(Role not loaded)" every time.
   `available_roles()` reports which roles this build can actually run, so a
   front end can disable the checkbox and say why instead of offering a
   control that is guaranteed to disappoint.

2. "DONE — n ROLES RESPONDED" COUNTS THE ROLES ASKED.
   The Tk status line reports `len(selected_roles)`, so three roles that
   errored and one that answered still reads "Done — 4 roles responded". The
   count here is of roles that actually produced text.

3. EMPTY CONTENT DOES NOTHING, SILENTLY.
   `_lens_run` returns with no message when the box is empty, so the button
   appears dead. `check_request()` gives the front end something to say.

WHAT IS DELIBERATELY KEPT
A failing role becomes a paragraph, not an exception: `(Error: …)` in its own
panel, with the other roles unaffected. Running eleven models and losing all of
them because one raised would be a worse trade than any error handling buys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: (role, on by default), IN LAYOUT ORDER. The order is the Tk tab's and is
#: load-bearing: users find a checkbox by position.
LENS_ROLES: Tuple[Tuple[str, bool], ...] = (
    ("writer", True),
    ("coder", True),
    ("sage", True),
    ("peasant", True),
    ("strategist", True),
    ("director", True),
    ("artist", False),
    ("intern", False),
    ("skeptic", False),
    ("content", True),
    ("musician", False),
)

#: How much of the pasted content reaches the model. The Tk tab slices to this
#: silently; a front end should say so rather than let a user wonder why the
#: back half of their document was ignored.
CONTENT_LIMIT = 3000

#: Per-role answer length. Enough for the 150-250 words the prompt asks for.
MAX_TOKENS = 350

#: Four at a time. Eleven concurrent model calls would thrash a machine running
#: local GGUF weights; this is the Tk tab's number and it is a sensible one.
MAX_PARALLEL = 4


def build_prompt(content: str) -> str:
    """The one prompt every role gets.

    Identical for every role on purpose — the role's own system prompt is what
    makes the critique different, and adding role-specific wording here would
    quietly turn eleven independent readings into eleven nudged ones.
    """
    return (
        "Review the following content from your specific lens.\n"
        "Give your honest, role-specific critique — 150-250 words.\n"
        "Do NOT synthesise or defer to other roles.\n"
        "Lead with what you specifically notice, good or bad.\n\n"
        f"CONTENT:\n{content[:CONTENT_LIMIT]}"
    )


def available_roles(models: Any) -> Dict[str, bool]:
    """role -> whether this build has a model for it.

    `musician` is always False and always will be: the Tk tab offers the
    checkbox and no such personality exists. Reporting it lets a front end
    disable the control rather than offer one that cannot work.
    """
    return {role: getattr(models, role, None) is not None
            for role, _default in LENS_ROLES}


def check_request(content: str, roles: Sequence[str]) -> Optional[str]:
    """Why this run cannot start, or None.

    The Tk version returns silently on empty content, so the button looks
    broken rather than refusing.
    """
    if not (content or "").strip():
        return "Paste something to review first."
    if not roles:
        return "Select at least one role."
    return None


def truncation_note(content: str) -> str:
    """What to say when only part of the content will be read, or ""."""
    length = len(content or "")
    if length <= CONTENT_LIMIT:
        return ""
    return (f"Only the first {CONTENT_LIMIT:,} characters are being reviewed "
            f"({length:,} pasted).")


def format_critique(role: str, text: str) -> str:
    """One role's answer, as it appears in the output pane.

    The rule is separators BEFORE the text, so a long critique never runs into
    the next role's heading when the pane is scrolled.
    """
    bar = "─" * 40
    return f"\n{bar}\n🔍 {role.upper()}\n{bar}\n{text}\n"


@dataclass
class LensResult:
    ok: bool
    message: str
    results: Dict[str, str] = field(default_factory=dict)
    answered: int = 0
    failed: List[str] = field(default_factory=list)
    error: Optional[BaseException] = None


def _is_failure(text: str) -> bool:
    """Whether a role's "answer" is really a failure notice."""
    stripped = (text or "").strip()
    return stripped.startswith("(Error:") or stripped == "(Role not loaded)"


def run_lens(models: Any, content: str, roles: Sequence[str], *,
             on_result: Optional[Callable[[str, str], None]] = None,
             max_parallel: int = MAX_PARALLEL) -> LensResult:
    """Ask each role the same thing at once and collect the answers.

    ``on_result(role, text)`` fires as each one lands, so a front end can show
    the first critique while the rest are still running — which on local
    weights is the difference between a feature that feels alive and one that
    appears hung for a minute. The caller decides which thread that callback
    lands on.

    Never raises. A role that fails becomes a paragraph.
    """
    roles = list(roles)
    problem = check_request(content, roles)
    if problem:
        return LensResult(False, problem)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    prompt = build_prompt(content)
    results: Dict[str, str] = {}

    def run_role(role: str) -> Tuple[str, str]:
        model = getattr(models, role, None)
        if model is None:
            return role, "(Role not loaded)"
        try:
            return role, model.respond(prompt, max_tokens=MAX_TOKENS)
        except Exception as exc:                          # noqa: BLE001
            return role, f"(Error: {exc})"

    try:
        with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
            futures = [pool.submit(run_role, role) for role in roles]
            for future in as_completed(futures):
                role, text = future.result()
                results[role] = text
                if on_result is not None:
                    on_result(role, text)
    except Exception as exc:                              # noqa: BLE001
        return LensResult(False, f"The lens failed: {exc!r}",
                          results=results, error=exc)

    failed = sorted(role for role, text in results.items() if _is_failure(text))
    answered = len(results) - len(failed)

    # Counting what ANSWERED, not what was asked. The Tk line reports
    # len(selected_roles), so three failures and one answer still reads
    # "Done — 4 roles responded".
    if not answered:
        message = f"No role could answer ({len(failed)} failed)."
    elif failed:
        message = (f"Done — {answered} of {len(roles)} roles responded "
                   f"({', '.join(failed)} could not).")
    else:
        message = f"Done — {answered} role(s) responded."

    return LensResult(True, message, results=results, answered=answered,
                      failed=failed)
