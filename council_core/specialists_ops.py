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
