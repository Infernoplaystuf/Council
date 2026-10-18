"""
council_core.plugin_manifest — a Typhon plugin's plugin.json, read as data and
checked, without running a line of the plugin.

WHAT A MANIFEST IS (docs/typhon_plugins_design.md 3.1 and 3.11)
Every plugin is a folder holding `plugin.json`: its id, version, the plugin
API it needs, its kind, what it needs and provides, and where it attaches.
Typhon reads every manifest at start-up WITHOUT importing any plugin code, so
it can list plugins, grey out features and refuse an incompatible plugin
before anything of it runs. This module is that reader. It imports nothing
but the standard library, and never the plugin's own files.

JSON, NOT TOML, AND NO PIP ENTRY POINTS
Every other Typhon file is JSON. Entry points would let any package installed
into a shared conda environment register itself without approval; one folder
the user can see is the whole of a plugin (design 3.1, question 7).

A CAMERA PLUGIN SAYS WHAT ITS CAMERAS CAN HAVE (`capabilities`, API 0.2)
Its capabilities (design 3.4: frames, events, software_trigger, ...) are
in the manifest, so the screen can offer features before any of its code
runs; each camera it opens confirms its own. Only a camera plugin declares
them (a feature says what it `needs`); a name Typhon does not know is kept
as written and ignored, as an offered capability Typhon does not know is.

STRICT, BECAUSE A MANIFEST IS A PROMISE
A field this schema does not know is refused, not ignored — a misspelt
"contributes" would otherwise quietly attach nothing. So are duplicate keys
(the second would silently win), NaN and Infinity, a file over 64 KB, text
that is not UTF-8, and anything but one JSON object. Each refusal says which
field and why; the plugin is then listed as incompatible with that reason
and its data are kept untouched (design 3.10, 4.3).

WHAT IS CHECKED HERE, AND WHAT IS NOT
One manifest on its own: the fields, their types and their forms, the API
version, the rules that set built-in and third-party plugins apart (`typhon.`
ids, `record_gate` and `frame_stage`, file names). Rules ACROSS plugins —
two plugins claiming one short name or one file suffix, a folder name that
does not match its id, a folder among the built-ins that is not on the
built-in list — are the registry's (council_core.plugin_host).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: The manifest's file name in a plugin's folder.
MANIFEST_FILE = "plugin.json"

#: The manifest's own format (the `schema` field). A manifest of another
#: schema is refused, named, never guessed at.
SCHEMA = 1

#: Far bigger than any manifest (a few hundred bytes); something else.
MAX_MANIFEST_BYTES = 64 * 1024

#: The plugin API this Typhon offers (typhon_plugin_api.API_VERSION, restated
#: so reading a manifest imports nothing else). 0.x on the branch: any
#: change is allowed until it is frozen as 1.0 at stage P5 (design 3.2).
API_VERSION = "0.2"

#: Ids: reverse-domain, lower case, at least one dot ("acme.thermal").
ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
#: The prefix kept for the plugins that ship with Typhon.
BUILTIN_PREFIX = "typhon."
#: A short name: in a third-party plugin's file names (`<run>_<short>_...`).
SHORT_RE = re.compile(r"^[a-z0-9]{2,12}$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
API_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")
#: `module:object` — a module inside the plugin's own package (dotted for a
#: sub-module, never relative, never leaving it) and an attribute of it.
ENTRY_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
#: A capability name ("frames", "hardware_trigger"); `a|b` means either.
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: A run-file suffix a plugin writes ("_capture_triggers.csv").
SUFFIX_RE = re.compile(r"^_[a-z0-9]+(?:_[a-z0-9]+)*\.[a-z0-9]{1,8}$")
#: A requirement on the project's interpreter: an import name, optionally
#: with version bounds ("numpy>=1.24", "sklearn", "pypylon>=4,<5").
REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?P<spec>(?:(?:===|==|!=|<=|>=|~=|<|>)\s*[0-9][0-9A-Za-z.*+!-]*\s*,?\s*)*)$")
SPEC_RE = re.compile(r"(===|==|!=|<=|>=|~=|<|>)\s*([0-9][0-9A-Za-z.*+!-]*)")

KINDS = ("camera", "source", "feature")
SCOPES = ("camera", "rig")
NETWORK = ("none", "camera-link", "internet")

#: The places a plugin may attach to (design 3.7, 3.8, 3.11). "presets"
#: (stage P3 row 4): values of its own in each camera's presets, as
#: "<id>/<key>" (design 3.9) — asked with preset_values, given back with
#: apply_preset_values.
CONTRIBUTIONS = ("panel", "toolbar", "status", "run_hooks", "analyse",
                 "review_source", "setup", "overlay", "record_gate",
                 "frame_stage", "presets")
#: Inside the data-safety rules: built-in plugins only (design 3.11).
BUILTIN_ONLY = frozenset({"record_gate", "frame_stage"})
#: Camera plugins only: how the slider shows one of its run files; and a
#: set-up — the family's check box in the set-up wizard, with its guide,
#: checks and scan (design 2.3, 2.4 item 15; stage P2 row 4).
CAMERA_ONLY = frozenset({"review_source", "setup"})
#: Named now so later plugins fit, not built yet (design 11.5): refused.
RESERVED = frozenset({"overlay"})

#: Run-file endings the core writes itself (council_core.capture's
#: RUN_MARKERS, less the run files of today's features that built-in
#: plugins take over, BUILTIN_FILES): no plugin may claim one.
CORE_SUFFIXES = ("_frame_", "_frames.csv", "_camera.json",
                 "_run_end.json", "_rig.json", "_rig_end.json")
#: The triggers CSV (capture.TRIGGERS_SUFFIX), a run marker too: the
#: built-in triggering plugin claims it; no third-party file may hold it.
TRIGGERS_FILE = "_capture_triggers.csv"
#: The raw file an event camera records beside its PNGs, a run marker too:
#: the built-in Prophesee plugin claims it (stage P3: with the run-file
#: suffix registry it moved from the core's endings to that plugin's).
RAW_FILE = "_events.raw"
#: Run files of today's Typhon that built-in plugins claim, each the name
#: its design fixed (design 3.9): no third-party file may hold one, as no
#: plugin may hold a core ending (the registry also refuses a second claim
#: of any run file, council_core.plugin_host.discover).
BUILTIN_FILES = (RAW_FILE, TRIGGERS_FILE)

#: Every field of schema 1, and which plugins must give it.
REQUIRED = ("schema", "id", "short", "name", "version", "api", "kind",
            "entry")
THIRD_PARTY_REQUIRED = ("author", "description", "licence")
OPTIONAL = ("scope", "needs", "python", "contributes", "files", "uses",
            "network", "budget_ms", "author", "description", "licence",
            "capabilities")
FIELDS = frozenset(REQUIRED) | frozenset(OPTIONAL)

#: Live calls' default time budget (design 3.11).
DEFAULT_BUDGET_MS = 5.0
MAX_BUDGET_MS = 1000.0
#: Text fields are labels, not documents.
MAX_TEXT = 200
MAX_DESCRIPTION = 2000
MAX_LIST = 64


class ManifestError(ValueError):
    """A manifest Typhon will not use. `field` names the field at fault
    ("" for the file as a whole); the message says why, in words for the
    Plugins window."""

    def __init__(self, message: str, field_name: str = ""):
        super().__init__(message)
        self.field = field_name


@dataclass(frozen=True)
class Use:
    """Another plugin this one works with (design 11.5)."""
    id: str
    required: bool = False


@dataclass(frozen=True)
class Manifest:
    """A checked plugin.json. Plain data: nothing here was imported."""
    id: str
    short: str
    name: str
    version: str
    api: str
    kind: str
    entry: str
    scope: str = "rig"
    needs: Tuple[str, ...] = ()
    python: Tuple[str, ...] = ()
    contributes: Tuple[str, ...] = ()
    files: Tuple[str, ...] = ()
    uses: Tuple[Use, ...] = ()
    network: str = "none"
    budget_ms: float = DEFAULT_BUDGET_MS
    author: str = ""
    description: str = ""
    licence: str = ""
    #: A camera plugin's: what its cameras can have (design 3.4), read
    #: before any of its code runs; each opened camera confirms its own.
    capabilities: Tuple[str, ...] = ()
    schema: int = SCHEMA
    #: Where it was read from, and whether it was read as a built-in.
    folder: Optional[Path] = field(default=None, compare=False)
    builtin: bool = False

    @property
    def module(self) -> str:
        """The entry's module, inside the plugin's package."""
        return self.entry.partition(":")[0]

    @property
    def attribute(self) -> str:
        """The entry's object in that module."""
        return self.entry.partition(":")[2]

    def as_dict(self) -> Dict[str, Any]:
        """The manifest as JSON data again (for the Plugins window, and a
        run's record of what made it)."""
        out: Dict[str, Any] = {
            "schema": self.schema, "id": self.id, "short": self.short,
            "name": self.name, "version": self.version, "api": self.api,
            "kind": self.kind, "entry": self.entry, "scope": self.scope,
            "needs": list(self.needs), "python": list(self.python),
            "contributes": list(self.contributes), "files": list(self.files),
            "uses": [{"id": u.id, "required": u.required} for u in self.uses],
            "network": self.network, "budget_ms": self.budget_ms}
        if self.capabilities:
            out["capabilities"] = list(self.capabilities)
        for name in THIRD_PARTY_REQUIRED:
            value = getattr(self, name)
            if value:
                out[name] = value
        return out


# ======================================================================
# The API version
# ======================================================================
def _api_parts(text: str) -> Tuple[int, int]:
    match = API_RE.match(text)
    if not match:
        raise ValueError(f"{text!r} is not an API version like \"1.0\"")
    return int(match.group(1)), int(match.group(2))


def api_problem(wanted: str, have: str = API_VERSION) -> str:
    """Why a plugin needing API `wanted` cannot run on API `have`, or "".

    From 1.0 (design 3.2, Spyder's rule): the same major version, and a
    minor no later than ours — "1.2" means 1.2 or later, before 2.0. Before
    1.0 every change is allowed, so a 0.x plugin must name exactly this
    0.x. The message names both numbers, as Micro-Manager's does."""
    try:
        want_major, want_minor = _api_parts(str(wanted))
        have_major, have_minor = _api_parts(str(have))
    except ValueError as exc:
        return str(exc)
    if want_major != have_major:
        return (f"it was written for plugin API {wanted}, and this Typhon "
                f"has plugin API {have} — a different major version")
    if have_major == 0 and want_minor != have_minor:
        return (f"it was written for plugin API {wanted}, and this Typhon "
                f"has plugin API {have}; before 1.0 a plugin must name the "
                f"exact 0.x it was written for")
    if want_minor > have_minor:
        return (f"it needs plugin API {wanted} or later, and this Typhon has "
                f"plugin API {have}")
    return ""


# ======================================================================
# Reading
# ======================================================================
def _no_duplicates(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ManifestError(f"the key {key!r} appears twice", key)
        out[key] = value
    return out


def _no_constant(name: str) -> Any:
    raise ManifestError(f"{name} is not a number JSON allows")


def load(path: Any) -> Dict[str, Any]:
    """The JSON object in `path`, as data, or ManifestError."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise ManifestError(f"it has no {MANIFEST_FILE}") from exc
    except OSError as exc:
        raise ManifestError(f"its {MANIFEST_FILE} cannot be read: "
                            f"{exc}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"its {MANIFEST_FILE} is {size:,} bytes — over "
                            f"the {MAX_MANIFEST_BYTES:,} a manifest may be")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"its {MANIFEST_FILE} is not UTF-8 text") from exc
    except OSError as exc:
        raise ManifestError(f"its {MANIFEST_FILE} cannot be read: "
                            f"{exc}") from exc
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates,
                         parse_constant=_no_constant)
    except ManifestError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ManifestError(f"its {MANIFEST_FILE} is not valid JSON: "
                            f"{str(exc)[:120]}") from exc
    if not isinstance(doc, dict):
        raise ManifestError(f"its {MANIFEST_FILE} is not one JSON object")
    return doc


def read(folder: Any, *, builtin: bool) -> Manifest:
    """The checked manifest of the plugin in `folder`, read as a built-in or
    a third-party plugin. Raises ManifestError with the reason."""
    folder = Path(folder)
    doc = load(folder / MANIFEST_FILE)
    return check(doc, builtin=builtin, folder=folder)


def check(doc: Mapping[str, Any], *, builtin: bool,
          folder: Optional[Path] = None) -> Manifest:
    """`doc` (a manifest as data) checked field by field; a Manifest, or
    ManifestError naming the first field at fault."""
    if not isinstance(doc, Mapping):
        raise ManifestError("a manifest is one JSON object")
    unknown = sorted(set(doc) - FIELDS)
    if unknown:
        raise ManifestError(
            f"it has field{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(u) for u in unknown)}, which schema {SCHEMA} "
            f"does not have", unknown[0])
    needed = REQUIRED + (() if builtin else THIRD_PARTY_REQUIRED)
    for name in needed:
        if name not in doc:
            raise ManifestError(f"it has no {name!r}", name)

    schema = doc["schema"]
    if not _whole(schema) or schema != SCHEMA:
        raise ManifestError(f"its schema is {schema!r}; this Typhon reads "
                            f"schema {SCHEMA}", "schema")

    ident = _text(doc, "id")
    if not ID_RE.match(ident):
        raise ManifestError(f"its id {ident!r} is not reverse-domain lower "
                            f"case with dots, like \"acme.thermal\"", "id")
    if builtin and not ident.startswith(BUILTIN_PREFIX):
        raise ManifestError(f"a built-in plugin's id starts "
                            f"{BUILTIN_PREFIX!r}; this one is {ident!r}", "id")
    if not builtin and ident.startswith(BUILTIN_PREFIX):
        raise ManifestError(f"ids starting {BUILTIN_PREFIX!r} are kept for "
                            f"the plugins that ship with Typhon", "id")

    short = _text(doc, "short")
    if not SHORT_RE.match(short):
        raise ManifestError(f"its short name {short!r} is not 2-12 lower-case "
                            f"letters or digits", "short")
    name = _text(doc, "name", limit=MAX_TEXT)
    if not name.strip():
        raise ManifestError("its name is empty", "name")
    version = _text(doc, "version")
    if not VERSION_RE.match(version):
        raise ManifestError(f"its version {version!r} is not "
                            f"major.minor.patch", "version")
    api = _text(doc, "api")
    if not API_RE.match(api):
        raise ManifestError(f"its api {api!r} is not a plugin API version "
                            f"like \"1.0\"", "api")
    kind = _text(doc, "kind")
    if kind not in KINDS:
        raise ManifestError(f"its kind {kind!r} is not one of "
                            f"{', '.join(KINDS)}", "kind")
    if "scope" in doc:
        scope = _text(doc, "scope")
        if scope not in SCOPES:
            raise ManifestError(f"its scope {scope!r} is not camera or rig",
                                "scope")
        if kind != "feature" and scope != "rig":
            raise ManifestError(f"a {kind} plugin has one instance per "
                                f"window: its scope can only be rig", "scope")
    elif kind == "feature":
        raise ManifestError("a feature plugin says its scope: camera (one per "
                            "camera) or rig (one per window)", "scope")
    else:
        scope = "rig"
    entry = _text(doc, "entry")
    if not ENTRY_RE.match(entry):
        raise ManifestError(f"its entry {entry!r} is not module:object "
                            f"inside the plugin", "entry")

    needs = _strings(doc, "needs")
    for need in needs:
        if not all(CAPABILITY_RE.match(part) for part in need.split("|")):
            raise ManifestError(f"{need!r} in needs is not a capability name "
                                f"(or a|b)", "needs")
    python = _strings(doc, "python")
    for requirement in python:
        if not REQUIREMENT_RE.match(requirement):
            raise ManifestError(f"{requirement!r} in python is not an import "
                                f"name with optional version bounds, like "
                                f"\"numpy>=1.24\"", "python")
    contributes = _strings(doc, "contributes")
    for point in contributes:
        if point not in CONTRIBUTIONS:
            raise ManifestError(f"{point!r} in contributes is not a place a "
                                f"plugin attaches to "
                                f"({', '.join(CONTRIBUTIONS)})", "contributes")
        if point in RESERVED:
            raise ManifestError(f"{point!r} is named for later plugins and "
                                f"not built yet", "contributes")
        if point in BUILTIN_ONLY and not builtin:
            raise ManifestError(f"{point!r} sits inside the data-safety rules: "
                                f"built-in plugins only", "contributes")
        if point in CAMERA_ONLY and kind != "camera":
            raise ManifestError(f"{point!r} is for camera plugins only",
                                "contributes")
    if len(set(contributes)) != len(contributes):
        raise ManifestError("a place is named twice in contributes",
                            "contributes")
    files = _strings(doc, "files")
    for suffix in files:
        if not SUFFIX_RE.match(suffix):
            raise ManifestError(f"{suffix!r} in files is not a run-file "
                                f"ending like \"_{short}_tracks.csv\"", "files")
        # ANYWHERE in it, not only at its start: capture.taken_runs splits a
        # file name at the first core marker it holds, so a sidecar
        # "<run>_ab_frame_1.png" would read as a frame of a run "<run>_ab".
        markers = CORE_SUFFIXES + (() if builtin else BUILTIN_FILES)
        held = [core for core in markers if core in suffix]
        if held:
            raise ManifestError(f"{suffix!r} is a run file Typhon's core "
                                f"writes itself, or holds the ending of one "
                                f"({held[0]!r}), so Typhon would take it for "
                                f"a run of its own", "files")
        if not builtin and not suffix.startswith(f"_{short}_"):
            raise ManifestError(f"a third-party plugin's run files are named "
                                f"<run>_{short}_<what>; {suffix!r} is not",
                                "files")
    if len(set(files)) != len(files):
        raise ManifestError("a run file is named twice in files", "files")
    uses = _uses(doc)
    capabilities = _strings(doc, "capabilities")
    for capability in capabilities:
        if not CAPABILITY_RE.match(capability):
            raise ManifestError(f"{capability!r} in capabilities is not a "
                                f"capability name", "capabilities")
    if capabilities and kind != "camera":
        raise ManifestError("only a camera plugin says what its cameras can "
                            "do (capabilities); a feature says what it "
                            "needs", "capabilities")
    if len(set(capabilities)) != len(capabilities):
        raise ManifestError("a capability is named twice in capabilities",
                            "capabilities")
    network = _text(doc, "network") if "network" in doc else "none"
    if network not in NETWORK:
        raise ManifestError(f"its network {network!r} is not one of "
                            f"{', '.join(NETWORK)}", "network")
    budget = doc.get("budget_ms", DEFAULT_BUDGET_MS)
    if (isinstance(budget, bool) or not isinstance(budget, (int, float))
            or not math.isfinite(budget) or not 0 < budget <= MAX_BUDGET_MS):
        raise ManifestError(f"its budget_ms {budget!r} is not a time in ms "
                            f"above 0 and at most {MAX_BUDGET_MS:g}",
                            "budget_ms")
    texts = {k: _text(doc, k, limit=MAX_DESCRIPTION if k == "description"
                      else MAX_TEXT) if k in doc else ""
             for k in THIRD_PARTY_REQUIRED}
    if not builtin:
        for key, value in texts.items():
            if not value.strip():
                raise ManifestError(f"its {key} is empty", key)
    return Manifest(
        id=ident, short=short, name=name.strip(), version=version, api=api,
        kind=kind, entry=entry, scope=scope, needs=tuple(needs),
        python=tuple(python), contributes=tuple(contributes),
        files=tuple(files), uses=uses, network=network,
        budget_ms=float(budget), capabilities=tuple(capabilities),
        schema=SCHEMA, folder=folder, builtin=builtin, **texts)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(doc: Mapping[str, Any], key: str, limit: int = MAX_TEXT) -> str:
    value = doc[key]
    if not isinstance(value, str):
        raise ManifestError(f"its {key} is not text", key)
    if len(value) > limit:
        raise ManifestError(f"its {key} is {len(value)} characters — over "
                            f"the {limit} it may be", key)
    return value


def _strings(doc: Mapping[str, Any], key: str) -> List[str]:
    value = doc.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str)
                                              for v in value):
        raise ManifestError(f"its {key} is not a list of text", key)
    if len(value) > MAX_LIST:
        raise ManifestError(f"its {key} lists {len(value)} — over the "
                            f"{MAX_LIST} it may", key)
    return list(value)


def _uses(doc: Mapping[str, Any]) -> Tuple[Use, ...]:
    value = doc.get("uses", [])
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise ManifestError("its uses is not a list of {id, required}",
                            "uses")
    out = []
    for item in value:
        if (not isinstance(item, dict) or set(item) - {"id", "required"}
                or not isinstance(item.get("id"), str)
                or not ID_RE.match(item["id"])
                or not isinstance(item.get("required", False), bool)):
            raise ManifestError("each entry of uses is {\"id\": a plugin id, "
                                "\"required\": true or false}", "uses")
        out.append(Use(item["id"], bool(item.get("required", False))))
    return tuple(out)


# ======================================================================
# Requirements on the interpreter
# ======================================================================
def requirement_parts(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """("numpy", [(">=", "1.24")]) from "numpy>=1.24"."""
    match = REQUIREMENT_RE.match(text)
    if not match:
        raise ValueError(f"{text!r} is not a requirement")
    return match.group("name"), SPEC_RE.findall(match.group("spec") or "")


def _release(version: str) -> Tuple[int, ...]:
    """The leading release numbers of a version ("1.24.0rc1" -> (1, 24, 0))."""
    numbers = []
    for part in str(version).split("."):
        digits = re.match(r"\d+", part)
        if not digits:
            break
        numbers.append(int(digits.group(0)))
        if digits.group(0) != part:
            break
    return tuple(numbers)


def version_ok(version: str, specs: List[Tuple[str, str]]) -> bool:
    """Whether `version` meets every (operator, version) in `specs`,
    comparing release numbers only ("1.24.0rc1" counts as 1.24.0). Enough
    for what a manifest asks; never used to install anything."""
    have = _release(version)
    for op, wanted in specs:
        if op == "===":
            if str(version) != wanted:
                return False
            continue
        star = wanted.endswith(".*")
        want = _release(wanted[:-2] if star else wanted)
        width = max(len(have), len(want))
        a = have + (0,) * (width - len(have))
        b = want + (0,) * (width - len(want))
        if star and op in ("==", "!="):
            same = have[:len(want)] == want
            if (op == "==") != same:
                return False
            continue
        if op == "==" and a != b:
            return False
        if op == "!=" and a == b:
            return False
        if op == ">=" and a < b:
            return False
        if op == "<=" and a > b:
            return False
        if op == ">" and a <= b:
            return False
        if op == "<" and a >= b:
            return False
        if op == "~=":
            if a < b or len(want) < 2:
                return False
            if have[:len(want) - 1] != want[:len(want) - 1]:
                return False
    return True
