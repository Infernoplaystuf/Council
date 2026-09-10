"""
gui_examples.py — worked GUI Designer wireframes, as model context.

PURE. Stdlib only; reads .gspec files from examples/gui/. No Tk, no model.

WHY THIS EXISTS
---------------
Asked to design a GUI from a description, a local model produces JSON that is
syntactically fine and structurally wrong. Measured, on a real request for a
file picker, three numeric rows, an image panel and a slider:

  * it wrapped everything in a full-canvas Frame AND a full-canvas Notebook,
    so the generated app came up completely blank
  * it put one of three identical rows inside a container and left the other
    two outside, so the user drew three rows and saw two
  * it sized the image panel to exactly fill its own parent
  * it dropped the requested background colour entirely

Rules in a prompt did not fix that; the model followed the letter of each and
still produced the same shapes. A COMPLETE, CORRECT EXAMPLE is a stronger
signal than a list of prohibitions, because it shows the target rather than
the boundary.

These examples are not hand-written illustrations. They are exported from
projects that were actually built, generated, policy-checked and RUN, so
every coordinate, colour and declaration in them is known to work.

WHAT EACH ONE TEACHES
---------------------
  barbie_capture  the basics: a window colour, a font, labels ABOVE their
                  boxes, and transparent labels (a label with no bg of its
                  own inherits the window's, which is what "transparent"
                  means in Tk — there is no alpha). Its picker, canvas and
                  scrubber are WIRED: a widget that looks like it browses
                  frames has to actually browse them, or the example teaches
                  a form that does nothing.
  image_viewer    everything above plus the three declaration types that
                  connect a wireframe to real behaviour:
                    port   — the typed value a widget exposes
                    script — a Python function a button runs
                    drives — a slider stepping an image panel through a folder
  barbie_capture_v2
                  the current version, and the only one sent to a model by
                  default: all of the above, a readable font, and a region of
                  interest drawn on the live image — Apply zooms the view to
                  it, and Save writes every frame cropped to it.

VERSIONS ARE KEPT, NOT REPLACED
-------------------------------
barbie_capture and barbie_capture_v2 both stay on disk so the improvement is
visible side by side (python run_example_gui.py <name>). Only PROMPT_EXAMPLES
is sent as model context: three near-identical wireframes cost three times the
tokens to teach one thing, and the budget is what leaves room for the user's
own request.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples" / "gui"

# What each example is FOR. Kept beside the data rather than inside it,
# because a .gspec is a project file and should not carry teaching notes.
NOTES: Dict[str, str] = {
    "barbie_capture": (
        "A capture form that browses a folder of frames and reports which of "
        "them are mistimed. Shows: window bg + fg + font applied to the whole "
        "app; a file picker in FOLDER mode wired to an image canvas by a "
        "scrubber's `drives`, so picking a folder loads it and the slider "
        "steps through it; a button whose `script` runs a Python function and "
        "fills TWO readouts — a count and a list — from ONE call; three "
        "numeric rows with the label directly ABOVE its spinbox; labels with "
        "no bg of their own, which inherit the window colour and so read as "
        "transparent."
    ),
    "image_viewer": (
        "The same form plus behaviour. Shows: `port` naming the typed value "
        "each widget exposes; `script` linking a button to a Python function "
        "with several outputs; `drives` making a scrubber step an image "
        "canvas through the images in a folder port."
    ),
    "barbie_capture_v2": (
        "A capture form that browses a folder of frames, reports mistimed "
        "ones, and crops to a region of interest. Shows: window bg + fg + a "
        "readable font; a FOLDER picker driving an image canvas through a "
        "scrubber's `drives`; `roi: true` on the canvas (Draw / Apply / Clear "
        "ROI above the image) with `drives.roi` naming an entry that mirrors "
        "the box as 'x, y, w, h'; a button whose `script` fills TWO readouts "
        "from ONE call; a Save button whose `script` passes three ports to a "
        "function and shows its one-line result; labels ABOVE their boxes."
    ),
}

# The examples sent to a model when none is named. The newest version teaches
# everything the older ones do; sending all of them tripled the context for
# no new signal. Older versions remain loadable by name.
PROMPT_EXAMPLES = ("barbie_capture_v2",)

# The declarations a designing model most often gets wrong, stated once.
DECLARATION_HELP = """\
Beyond kind/label/x/y/w/h, a shape may declare:

  "bg" / "fg"   "#rrggbb". A widget with no bg inherits its container's, so
                omitting bg on a label over a coloured panel is how you get a
                transparent label. Notebook, Treeview, Combobox and
                Progressbar cannot take colour at all.
  "font"        a Tk font string, e.g. "Magneto 18 bold".
  "port"        {"name": "<identifier>"} — the typed value this widget
                exposes to code as self.ports.<name>.
  "script"      on a button: {"module","function","inputs":[port names],
                "outputs":{port: result_key}} — generation writes a working
                handler that calls it.
  "drives"      on a scrubber/scale: {"folder": <port>, "target": <port>} —
                steps the target widget through the files in the folder port,
                and sizes itself to the folder automatically. Add
                "roi": <entry port> to mirror a box drawn on the target
                (which needs props {"roi": true}) as "x, y, w, h".
"""


def names() -> List[str]:
    """Available example names, in a stable order."""
    if not EXAMPLES_DIR.is_dir():
        return []
    return sorted(p.stem for p in EXAMPLES_DIR.glob("*.gspec"))


def load(name: str) -> Dict[str, Any]:
    """One example as its raw .gspec dict."""
    p = EXAMPLES_DIR / f"{name}.gspec"
    if not p.is_file():
        raise KeyError(f"no such example: {name!r}; have {names()}")
    return json.loads(p.read_text(encoding="utf-8"))


def _compact(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Drop everything a model does not need to see.

    A raw .gspec carries ids, z-order, resize modes, min sizes and empty prop
    dicts. Feeding all of it wastes context and invites the model to copy
    fields it should not set — ids especially, which the app assigns."""
    win = dict(spec.get("window") or {})
    out: Dict[str, Any] = {
        "window": {k: v for k, v in win.items() if v not in ("", 0, None)},
        "shapes": [],
    }
    for s in spec.get("shapes") or []:
        keep: Dict[str, Any] = {"kind": s.get("kind")}
        if s.get("label"):
            keep["label"] = s["label"]
        for k in ("x", "y", "w", "h"):
            keep[k] = s.get(k, 0)
        for k in ("bg", "fg", "font"):
            if s.get(k):
                keep[k] = s[k]
        for k in ("port", "script", "drives"):
            if s.get(k):
                keep[k] = s[k]
        # Props that merely repeat the catalogue's default are noise, and
        # worse than noise: a model that sees "text": "" in every example
        # learns to emit it. Only props the author actually chose survive.
        props = _meaningful_props(str(s.get("kind") or ""), s.get("props"))
        if props:
            keep["props"] = props
        out["shapes"].append(keep)
    return out


def _meaningful_props(kind: str, props: Optional[Dict[str, Any]]
                      ) -> Dict[str, Any]:
    """Only the props whose value differs from the kind's declared default."""
    props = dict(props or {})
    if not props:
        return {}
    try:
        from gui_shapes import PALETTE            # pure; no Tk
        schema = (PALETTE.get(kind) or {}).get("prop_schema") or {}
    except Exception:
        schema = {}
    out: Dict[str, Any] = {}
    for k, v in props.items():
        if v in ("", None, [], {}):
            continue
        default = (schema.get(k) or {}).get("default")
        if default is not None and v == default:
            continue
        out[k] = v
    return out


def for_prompt(name: Optional[str] = None, *, include_help: bool = True) -> str:
    """The example(s) rendered as model context.

    Returns text meant to be pasted into a prompt: a one-line statement of
    what the example teaches, then the compact JSON a model should imitate.
    With no name, PROMPT_EXAMPLES — the current version — is sent; if none of
    those exist on disk, every example is, so the model is never shown
    nothing."""
    on_disk = names()
    if name:
        wanted = [name]
    else:
        wanted = [n for n in PROMPT_EXAMPLES if n in on_disk] or on_disk
    blocks: List[str] = []
    for n in wanted:
        try:
            spec = load(n)
        except KeyError:
            continue
        blocks.append(
            f"# EXAMPLE: {n}\n# {NOTES.get(n, '')}\n"
            + json.dumps(_compact(spec), indent=2))
    if not blocks:
        return ""
    head = ("Worked examples of correct wireframes. Each was built, generated "
            "and run, so the coordinates and declarations are known good. "
            "Imitate this shape.\n\n")
    tail = ("\n\n" + DECLARATION_HELP) if include_help else ""
    return head + "\n\n".join(blocks) + tail
