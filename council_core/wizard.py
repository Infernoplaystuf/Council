"""
council_core.wizard — the Designer's guided on-ramp, minus the widgets.

The Designer is a freeform canvas: powerful once you know it, blank and
unhelpful the first time you open it. This asks five short questions and hands
the answers to that same canvas as ordinary shapes, which the user then edits
normally.

IT IS AN ON-RAMP, NOT A SECOND DESIGNER
One scene model, one snap engine, one undo stack, and Generate keeps reading
the canvas it always read. Everything the wizard produces is a plain Shape
list, which is why `Scene.add_shapes` — not `load` — is the right way to
receive it: the user may already have drawn something.

WHAT IS HERE AND WHAT IS NOT
The layout arithmetic was already pure, in `gui_templates`, and shape assembly
was already pure, in `gui_wizard.build_shapes`. What was NOT was the part that
reads the answers: the step order, what makes each step invalid, and how the
answers become options. All three are decisions and none needs a toolkit.

THE VALIDATION MESSAGES ARE THE PRODUCT
"A minimum window smaller than 200x200 will not be usable" is the difference
between a wizard that stops you and one that hands you a broken app and lets
you find out later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from gui_wizard import (DEFAULT_MIN_H, DEFAULT_MIN_W, MAIN_KINDS,  # noqa: F401
                        SIDE_KINDS, STEPS, WizardResult, build_shapes)

#: A window smaller than this cannot hold a usable layout.
MIN_USABLE = 200

#: Reserved frames smaller than this are not worth holding open.
MIN_RESERVED = 24


@dataclass
class Answers:
    """Everything the five steps collect. Strings, as they come off controls.

    Strings rather than typed fields because that is what a text box gives you,
    and because the coercion is where the interesting behaviour is: a bad
    number becomes a REFUSAL here, not a silent zero, since unlike a property
    panel this runs once and creates a project.
    """
    name: str = ""
    mode: str = "linked"
    title: str = ""
    min_w: str = str(DEFAULT_MIN_W)
    min_h: str = str(DEFAULT_MIN_H)
    template: str = "form"
    fields: str = "3"
    labels: str = ""
    buttons: str = "OK, Cancel"
    main_kind: str = MAIN_KINDS[0]
    left_kind: str = SIDE_KINDS[0]
    right_kind: str = "frame"
    reserve: str = "0"
    reserve_w: str = "240"
    reserve_h: str = "160"


def as_int(raw: Any, default: int) -> int:
    """A control's value as a number, or `default`.

    The default is chosen by the CALLER per use: `validate` passes -1 so an
    unparseable count fails its "zero or more" check, while the builders pass a
    real default so a blank box still produces something.
    """
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def validate(step: str, answers: Answers,
             existing: Sequence[str] = ()) -> str:
    """Why this step cannot be left, or "" if it can.

    Checked per step rather than at the end so the user finds out on the screen
    that caused it — an error on Finish about a name typed four screens ago is
    an error you have to go looking for.
    """
    if step == "basics":
        name = (answers.name or "").strip()
        if not name:
            return "Give the project a name."
        if name.lower() in {str(e).lower() for e in existing}:
            return f"There is already a project called {name!r}."
        if (as_int(answers.min_w, 0) < MIN_USABLE
                or as_int(answers.min_h, 0) < MIN_USABLE):
            return (f"A minimum window smaller than {MIN_USABLE}x{MIN_USABLE} "
                    f"will not be usable.")
    if step == "contents" and answers.template == "form":
        if as_int(answers.fields, -1) < 0:
            return "Number of fields must be zero or more."
    if step == "reserve":
        if as_int(answers.reserve, -1) < 0:
            return "Number of reserved spaces must be zero or more."
    return ""


def template_options(answers: Answers) -> Dict[str, Any]:
    """The answers, as the keyword arguments the template builder takes."""
    if answers.template == "form":
        labels = _split(answers.labels)
        buttons = _split(answers.buttons)
        return {"n_fields": max(0, as_int(answers.fields, 3)),
                # None, not [], so the template generates its own labels —
                # an empty list would produce a form of nameless fields.
                "labels": labels or None,
                "buttons": buttons or ("OK",),
                "title": (answers.title or "").strip() or "Details"}
    if answers.template == "toolbar_main_status":
        return {"main_kind": answers.main_kind}
    if answers.template == "split_view":
        return {"left_kind": answers.left_kind, "right_kind": answers.right_kind}
    return {}


def _split(raw: str) -> List[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def shapes_for(answers: Answers) -> List[Any]:
    """The wireframe these answers describe."""
    return build_shapes(
        answers.template, template_options(answers),
        reserve_n=max(0, as_int(answers.reserve, 0)),
        reserve_w=max(MIN_RESERVED, as_int(answers.reserve_w, 240)),
        reserve_h=max(MIN_RESERVED, as_int(answers.reserve_h, 160)))


def to_result(answers: Answers) -> WizardResult:
    """The finished answers, as the host takes them.

    The window minimums are clamped rather than rejected here: `validate`
    already refused anything below MIN_USABLE on the way past, and a Finish
    that threw on a value the user could no longer see would be unfixable.
    """
    name = (answers.name or "").strip()
    return WizardResult(
        name=name,
        mode=answers.mode,
        # The window title falls back to the project name. A window called
        # "Untitled" when the user named the project is a detail nobody
        # remembers to go back and fix.
        title=(answers.title or "").strip() or name,
        min_w=max(MIN_USABLE, as_int(answers.min_w, DEFAULT_MIN_W)),
        min_h=max(MIN_USABLE, as_int(answers.min_h, DEFAULT_MIN_H)),
        template=answers.template,
        shapes=shapes_for(answers))


def summary(answers: Answers) -> List[str]:
    """The review step, in words — what Finish is about to make."""
    shapes = shapes_for(answers)
    reserved = max(0, as_int(answers.reserve, 0))
    lines = [f"Project: {(answers.name or '').strip()} ({answers.mode})",
             f"Window: {max(MIN_USABLE, as_int(answers.min_w, DEFAULT_MIN_W))}"
             f"x{max(MIN_USABLE, as_int(answers.min_h, DEFAULT_MIN_H))}",
             f"Layout: {answers.template}",
             f"{len(shapes)} shape(s) will be placed on the canvas"]
    if reserved:
        lines.append(f"including {reserved} reserved space(s), held open at "
                     f"the size you chose and generated with no model call")
    return lines
