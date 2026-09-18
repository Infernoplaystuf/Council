"""
council_core.specialists_ops — the specialist registry, for both front ends.

A specialist is a named LENS on the shared vault: an id, an icon, a name, some
domain keywords and a system-prompt overlay. It owns no data — there is exactly
one knowledge pool — and at query time the council either honours a manual pin
or keyword-matches up to three of them and composes their overlays into the
extra context the base personality answers with.

WHY THIS MODULE EXISTS: THE PIN WAS BROKEN IN THE PORT AND SAID NOTHING
The Qt Council tab's "Ask:" dropdown has been empty since it was written, in
three independent ways, all silent:

  1. It called `SpecialistRegistry()` with no argument. The constructor takes
     `vault_dir`, so every call raised TypeError — and a bare
     `except Exception: return []` turned that into an empty list. A dropdown
     that is empty because of a swallowed TypeError looks exactly like a
     dropdown that is empty because you have no specialists.
  2. Its labels were bare names; the Tk shell's are `f"{icon} {name}"`. Two
     front ends offering different text for the same registry is precisely
     what `council_options.specialist_choices` warns about in its own comment.
  3. Its label→id map was `{name: name}`, so even with entries it would have
     pinned a NAME where the resolver wants an ID.

One label format, built once, resolved by the same code that built it. The
label is for a human; `id` is the address — the same rule
`council_core.grapher_files` had to impose on the chart file dropdown, for the
same reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: What the dropdown shows when nothing is pinned.
AUTO_LABEL = "Auto"


@dataclass
class SpecialistList:
    """The registry as a front end needs it: labels to show, ids to act on."""
    ok: bool
    message: str
    labels: List[str] = field(default_factory=list)
    by_label: Dict[str, str] = field(default_factory=dict)   # label -> id
    specialists: List[Any] = field(default_factory=list)
    error: Optional[BaseException] = None


def pin_label(specialist: Any) -> str:
    """How a specialist is named in the Council tab's pin.

    `f"{icon} {name}"`, matching the Tk shell exactly. The Specialists tab's
    own list uses a third format (doubled spaces and an enabled tag) and that
    one is genuinely different — it is a management view, not a chooser.
    """
    icon = getattr(specialist, "icon", "") or ""
    name = getattr(specialist, "name", "") or ""
    return f"{icon} {name}".strip()


def load(vault_dir: Any, *, enabled_only: bool = True) -> SpecialistList:
    """Every specialist, in the registry's own order.

    Not sorted: the order is meaningful, and re-sorting it in a view is how two
    front ends end up offering different lists.

    A failure is REPORTED rather than flattened to an empty list, because
    "there are none" and "the registry would not load" need different words in
    front of a user.
    """
    try:
        import specialists
        registry = specialists.SpecialistRegistry(Path(vault_dir))
        items = list(registry.all())
    except Exception as exc:                              # noqa: BLE001
        return SpecialistList(False,
                              f"Could not load the specialists: {exc!r}",
                              error=exc)

    if enabled_only:
        items = [s for s in items if getattr(s, "enabled", True)]

    labels, by_label = [], {}
    for specialist in items:
        label = pin_label(specialist)
        labels.append(label)
        by_label[label] = getattr(specialist, "id", label)

    message = (f"{len(items)} specialist(s)." if items else
               "No specialists yet. Add one in the Specialists tab to give "
               "the council a lens on a topic.")
    return SpecialistList(True, message, labels=labels, by_label=by_label,
                          specialists=items)


def choices(listing: SpecialistList) -> List[str]:
    """Dropdown entries: automatic first, then the specialists."""
    return [AUTO_LABEL] + list(listing.labels)


def pinned_id(choice: str, listing: SpecialistList) -> Optional[str]:
    """The specialist id a selection means, or None for automatic.

    Resolved through the map the labels were BUILT from, so there is no string
    arithmetic here to get wrong — and "Auto" is not a specialist, which a
    front end that forgets pins onto every query.
    """
    if not choice or choice == AUTO_LABEL:
        return None
    return listing.by_label.get(choice)


# ============================================================
# Editing one
# ============================================================
# The Specialists tab is CRUD over the registry. What lives here is the part
# that must not differ between front ends: what a valid specialist is, what a
# slug is, and what the defaults are for a new one.

#: Which existing personality can wear a lens. Exactly the Tk dropdown's list,
#: in its order — a user picks by position.
BASE_PERSONALITIES = ("writer", "sage", "strategist", "intern", "coder",
                      "content")

DEFAULT_BASE = "writer"
DEFAULT_ICON = "🎓"


def slugify(name: str) -> str:
    """A url-safe id from a display name.

    Stable and lowercase, because the id is the address: it is what the Council
    tab's pin resolves to and what the registry stores under. Renaming a
    specialist must not change it.
    """
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug[:40] or "specialist"


def parse_keywords(text: str) -> List[str]:
    """The comma-separated keyword box, as a list.

    Empties dropped, whitespace stripped, order kept — the order is the user's
    and re-sorting it would make the box appear to rewrite itself on save.
    """
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def format_keywords(keywords: Sequence[str]) -> str:
    return ", ".join(keywords or [])


def check_specialist(name: str, keywords: Sequence[str]) -> Optional[str]:
    """Why this specialist cannot be saved, or None."""
    if not (name or "").strip():
        return "Give the specialist a name."
    if not keywords:
        return ("Add at least one domain keyword — they are how the council "
                "knows when to summon it.")
    return None


@dataclass
class SpecialistDraft:
    """An edited specialist, before it is saved.

    A plain value, so the view can hold one and compare it against what is
    stored to know whether there are unsaved edits. The Tk form has no such
    notion: selecting another specialist destroys the form and the edits with
    it, silently.
    """
    id: str = ""
    name: str = ""
    icon: str = DEFAULT_ICON
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    overlay: str = ""
    base: str = DEFAULT_BASE
    enabled: bool = True

    @classmethod
    def of(cls, specialist: Any) -> "SpecialistDraft":
        return cls(
            id=getattr(specialist, "id", ""),
            name=getattr(specialist, "name", ""),
            icon=getattr(specialist, "icon", DEFAULT_ICON) or DEFAULT_ICON,
            description=getattr(specialist, "description", "") or "",
            keywords=list(getattr(specialist, "domain_keywords", []) or []),
            overlay=getattr(specialist, "system_prompt_overlay", "") or "",
            base=getattr(specialist, "base_personality", DEFAULT_BASE)
            or DEFAULT_BASE,
            enabled=bool(getattr(specialist, "enabled", True)))


@dataclass
class OpResult:
    ok: bool
    message: str
    error: Optional[BaseException] = None


def save_specialist(vault_dir: Any, draft: SpecialistDraft) -> OpResult:
    """Create or update one specialist."""
    problem = check_specialist(draft.name, draft.keywords)
    if problem:
        return OpResult(False, problem)
    try:
        import specialists
        registry = specialists.SpecialistRegistry(Path(vault_dir))
        specialist = specialists.Specialist(
            id=draft.id or slugify(draft.name),
            name=draft.name.strip(),
            icon=draft.icon or DEFAULT_ICON,
            description=draft.description.strip(),
            domain_keywords=list(draft.keywords),
            system_prompt_overlay=draft.overlay,
            base_personality=(draft.base or DEFAULT_BASE),
            enabled=bool(draft.enabled))
        registry.add(specialist)
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Could not save: {exc!r}", error=exc)
    return OpResult(True, f"Saved “{draft.name.strip()}”.")


def set_enabled(vault_dir: Any, specialist_id: str, enabled: bool) -> OpResult:
    """Turn one specialist on or off, without touching anything else.

    A separate operation from saving, because the Tk checkbox saves the WHOLE
    specialist as the form currently shows it — so toggling Enabled commits
    whatever half-typed edits are in the boxes.
    """
    if not specialist_id:
        return OpResult(False, "Select a specialist first.")
    try:
        import specialists
        registry = specialists.SpecialistRegistry(Path(vault_dir))
        specialist = registry.get(specialist_id)
        if specialist is None:
            return OpResult(False, "That specialist no longer exists.")
        specialist.enabled = bool(enabled)
        registry.add(specialist)
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Could not update: {exc!r}", error=exc)
    return OpResult(True, "Enabled." if enabled else "Disabled.")


def confirm_delete_text(name: str) -> str:
    """What to ask before deleting. Names what is NOT lost, because a user who
    thinks their data is at stake will not press it."""
    return (f"Delete the specialist “{name}”?\n"
            "(Your files are NOT touched — a specialist is only a lens.)")


def delete_specialist(vault_dir: Any, specialist_id: str) -> OpResult:
    if not specialist_id:
        return OpResult(False, "Select a specialist first.")
    try:
        import specialists
        specialists.SpecialistRegistry(Path(vault_dir)).remove(specialist_id)
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Could not delete: {exc!r}", error=exc)
    return OpResult(True, "Deleted.")


def list_label(specialist: Any) -> str:
    """The management list's own format — icon, name, and whether it is on.

    Deliberately different from `pin_label`: this list is where a user turns
    specialists on and off, so the state belongs in the row. The pin is a
    chooser and has no use for it.
    """
    icon = getattr(specialist, "icon", "") or ""
    name = getattr(specialist, "name", "") or ""
    tag = "✓" if getattr(specialist, "enabled", True) else "(off)"
    return f"{icon}  {name}  {tag}"
