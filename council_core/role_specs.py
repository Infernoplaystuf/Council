"""
council_core.role_specs — "system requirements" for each council role.

Like a game's spec sheet: for every role, the MINIMUM model that does the job,
the RECOMMENDED one, and the BEST worth having, each with the graphics-card
memory (VRAM) it needs to run fast and the system RAM it needs on the CPU
alone. Then, against this PC and the models the roles use now, whether each
role meets its minimum or its recommendation — and where an upgrade pays off
first. The Council Map tab shows it ("Role specs").

WHERE THE NUMBERS COME FROM
  * Model sizes: model_catalog (vram_gb_q4: the VRAM at Q4 with a 4k
    context; size_gb on disk). RAM for the CPU path is the file size plus
    CPU_OVERHEAD_GB — an estimate, labelled as one.
  * What a role needs: the rules the Models tab already applies
    (local_models.TOOL_ROLES need tool calling, CODE_ROLES need 8k+
    context) and what each role does in a turn (council_core/deliberation.py).
  * Calls per round: counted from the deliberation — each member answers,
    rates its confidence, rebuts and takes two cross-fire turns (≈5 calls);
    the Peasant questions every candidate and joins cross-fire; the Judge
    ranks and critiques (2); the Writer writes once.
  * The minimum / recommended / best picks are the Council's guidance from
    the catalog's own tags (tools, writing, reasoning, code), not a
    benchmark. "Check this PC" in the Models tab measures real speeds.

DUPLICATES
A duplicate of a role — a second copy answering at the same time — only
helps when two calls to that role can run at once: in the Fan-out tab, or
with 'Parallel members' on when the members' models sit on different
machines. The Council tab still runs one question at a time. Each card says
what a duplicate would buy and what it costs: with Ollama
one loaded model can serve two requests at once (OLLAMA_NUM_PARALLEL, extra
context memory only); an in-app .gguf model serves one call at a time, so a
duplicate there is a second copy of the weights.

No Qt, no network. `assess` takes plain numbers so a test can hand it any PC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Added to a model's file size for the CPU path: the context cache and the
#: runtime. An estimate — a long context needs more.
CPU_OVERHEAD_GB = 2.0

#: Order: who decides quality, then who does the most work.
PRIORITY_LABELS = {1: "Highest", 2: "High", 3: "Medium", 4: "Low"}


@dataclass(frozen=True)
class Tier:
    model: str                 # an Ollama tag in model_catalog
    why: str


@dataclass(frozen=True)
class RoleSpec:
    role: str
    label: str
    job: str
    priority: int              # 1 = upgrade first
    calls: str                 # model calls per round, in words
    needs: str                 # what the model must be able to do
    minimum: Tier
    recommended: Tier
    best: Tier
    tips: Tuple[str, ...] = ()
    duplicate: str = ""        # what a second copy would buy


SPECS: Tuple[RoleSpec, ...] = (
    RoleSpec(
        "writer", "Writer", "Writes the one answer you read, from every "
        "candidate, argument and the Judge's ranking.",
        1, "1 per round, with the longest prompt of the turn",
        "Long context (it reads the whole debate) and good prose.",
        Tier("llama3.1:8b", "128k context; the smallest that synthesises "
             "a long debate reliably"),
        Tier("gemma3:12b", "tagged for writing, 128k context"),
        Tier("gpt-oss:20b", "the strongest US model in the catalog; an MoE, "
             "so it runs well even when part of it spills to RAM"),
        ("Every answer you see is the Writer's: this is the role where a "
         "better model shows most.",
         "Give it a large context window (n_ctx) in model_slots.json — its "
         "prompt grows with every member on the panel."),
        "Would let two questions be answered at once (two Council turns, or "
        "a turn plus a background job). Within one question the Writer runs "
        "once, so a duplicate does not make a single answer faster."),
    RoleSpec(
        "judge", "Judge", "Picks the panel, ranks the answers, checks the "
        "final one — and, as controller, reviews placement weekly.",
        1, "2 per round (rank, critique); short replies, strict JSON",
        "Reliable structured (JSON) output and careful reasoning.",
        Tier("llama3.1:8b", "native tool calling and dependable JSON"),
        Tier("phi4:14b", "tagged for reasoning; fits a 12 GB card"),
        Tier("gpt-oss:20b", "reasoning + tools; the best verdicts in the "
             "catalog"),
        ("A weak judge picks the wrong winner and passes weak answers: "
         "quality here matters more than speed.",
         "Keep it on a different model from the members if you can, so it "
         "is not grading its own writing style."),
        "Little: it makes two calls per round, after everyone else. Not "
        "worth duplicating."),
    RoleSpec(
        "coder", "Coder", "Writes code and GUIs; with Tools on, runs Python "
        "and searches the vault mid-answer.",
        2, "≈5 per round, plus 1–3 more per tool use",
        "Code ability, tool calling, and 8k+ context.",
        Tier("llama3.1:8b", "tools, 128k context; good enough for scripts"),
        Tier("phi4:14b", "tagged for code and reasoning"),
        Tier("gpt-oss:20b", "code, docs and tools in one model"),
        ("If you use the council mostly for code, move the Coder up to "
         "Highest.",
         "granite3-dense:8b is a code-specialised alternative at the same "
         "size as Llama 3.1 8B."),
        "Already used by the 🧩 Fan-out tab: several Coder workers run at "
        "once on different parts of a bigger change, one per machine. More "
        "machines with the Coder's model (Machines & roles) means more units "
        "in parallel. In a council question, Parallel members helps most "
        "when the Coder has its own machine: it is the slowest member, so "
        "one at a time it holds the others up most."),
    RoleSpec(
        "docs", "Docs", "Answers from documentation servers in the Docs tab "
        "and writes code from what it read.",
        2, "Several per question, each a tool call",
        "Tool calling is required (local_models.TOOL_ROLES).",
        Tier("llama3.1:8b", "the smallest catalog model with native tool "
             "calling"),
        Tier("gpt-oss:20b", "tools + code + docs"),
        Tier("gpt-oss:20b", "same — nothing larger in the catalog"),
        ("A model without tool support cannot do this role at all.",),
        "Not on the council panel; a duplicate only helps if you ask the "
        "Docs tab two things at once."),
    RoleSpec(
        "strategist", "Strategist", "Plans: steps, order, trade-offs.",
        3, "≈5 per round",
        "Reasoning over several steps.",
        Tier("llama3.1:8b", "solid general model"),
        Tier("phi4:14b", "tagged for reasoning"),
        Tier("gpt-oss:20b", "reasoning"),
        ("Shares a model with the Judge happily: different job, same "
         "strength.",),
        "Helps with Parallel members on, when it has its own machine."),
    RoleSpec(
        "sage", "Sage", "Long-view answers: context, history, consequences.",
        3, "≈5 per round",
        "Breadth of knowledge; a larger model knows more.",
        Tier("llama3.1:8b", "solid general model"),
        Tier("gemma3:12b", "larger, tagged for writing"),
        Tier("gpt-oss:20b", "broadest knowledge in the catalog"),
        (), "Helps with Parallel members on, when it has its own machine."),
    RoleSpec(
        "skeptic", "Skeptic", "Attacks the question and the answers.",
        3, "≈5 per round, short replies",
        "Short critical replies; no vault, no history.",
        Tier("llama3.2:3b", "short replies are fine from a small model"),
        Tier("llama3.1:8b", "sharper objections"),
        Tier("phi4:14b", "reasoning: finds the real flaws"),
        ("A different model from the Writer gives genuinely different "
         "objections.",),
        "Helps with Parallel members on, when it has its own machine."),
    RoleSpec(
        "artist", "Artist", "Creative answers and alternatives.",
        4, "≈5 per round",
        "Fluent, varied writing.",
        Tier("llama3.2:3b", "fast and fluent"),
        Tier("gemma3:12b", "tagged for writing"),
        Tier("gemma3:12b", "same"),
        (), "Helps with Parallel members on, when it has its own machine."),
    RoleSpec(
        "intern", "Intern", "Fast first drafts; with Tools on, can run code "
        "and search the vault.",
        4, "≈5 per round, plus tool calls",
        "Speed over depth.",
        Tier("llama3.2:3b", "fast; the role is meant to be quick"),
        Tier("llama3.1:8b", "better drafts, still fast on a GPU"),
        Tier("llama3.1:8b", "more is wasted on first drafts"),
        ("A good role for a small, fast model — or a second machine once "
         "roles can be pinned to one.",),
        "Helps with Parallel members on, when it has its own machine."),
    RoleSpec(
        "peasant", "Peasant", "Asks two plain questions about every answer.",
        4, "1–2 per candidate, plus cross-fire questions — the most calls of "
        "any role",
        "Speed: it is called the most, with short prompts and replies.",
        Tier("llama3.2:1b", "plain questions need little; runs anywhere"),
        Tier("llama3.2:3b", "better questions, still very fast"),
        Tier("llama3.2:3b", "a bigger model mostly adds waiting"),
        ("The cheapest way to speed up a whole turn: give the Peasant a "
         "small model of its own so it never waits behind the big one.",),
        "The best candidate for a duplicate once calls can overlap: it is "
        "called the most and its calls are short."),
)


# ============================================================
# Model sizes, from the catalog
# ============================================================

@dataclass(frozen=True)
class ModelSize:
    tag: str
    name: str
    params: str
    vram_gb: float             # to run fully on the graphics card
    ram_gb: float              # to run on the CPU alone (estimate)
    effective_b: float         # for "is the current model at least this?"


def _norm(tag: str) -> str:
    t = str(tag or "").strip().lower()
    if t.startswith("ollama:"):
        t = t[len("ollama:"):]
    return t[:-len(":latest")] if t.endswith(":latest") else t


def model_size(tag: str) -> Optional[ModelSize]:
    """The catalog's numbers for an Ollama tag, or None if it is not there."""
    try:
        import model_catalog
    except Exception:                                     # noqa: BLE001
        return None
    want = _norm(tag)
    for m in model_catalog.MODELS:
        if m.ollama and _norm(m.ollama) == want:
            params = (f"{m.params_b:g}B ({m.active_params_b:g}B active)"
                      if m.is_moe else f"{m.params_b:g}B")
            return ModelSize(m.ollama, m.name, params, float(m.vram_gb_q4),
                             round(float(m.size_gb) + CPU_OVERHEAD_GB, 1),
                             float(m.effective_params_b))
    return None


def tier_line(tier: Tier) -> str:
    size = model_size(tier.model)
    if size is None:
        return f"{tier.model} — {tier.why}"
    return (f"{size.name} ({size.tag}, {size.params}) — GPU: "
            f"{size.vram_gb:g} GB VRAM · CPU only: {size.ram_gb:g} GB RAM "
            f"(slower)\n      {tier.why}")


# ============================================================
# Against this PC and the current setup
# ============================================================

@dataclass
class Assessment:
    spec: RoleSpec
    current: str = ""                  # the model the role uses now
    model_verdict: str = ""            # "Recommended" | "Minimum" | ...
    hardware_verdict: str = ""
    notes: List[str] = field(default_factory=list)
    calls_7d: int = 0
    seconds_7d: float = 0.0


def _hardware_fit(size: Optional[ModelSize], vram_gb: Optional[float],
                  ram_gb: Optional[float]) -> str:
    if size is None:
        return "unknown"
    if vram_gb and vram_gb >= size.vram_gb:
        return "gpu"
    if ram_gb and ram_gb >= size.ram_gb:
        return "cpu"
    if vram_gb is None and ram_gb is None:
        return "unknown"
    return "no"


def _model_verdict(current: str, spec: RoleSpec) -> str:
    size = model_size(current) if current else None
    if not current:
        return "Not set"
    if size is None:
        try:
            from .local_models import maker_and_origin
            if maker_and_origin(current)[1] == "non-US":
                return "Not US-made"
        except Exception:                                 # noqa: BLE001
            pass
        return "Not in catalog"
    # A model the spec names gets that tier, whatever its size: gpt-oss-20b
    # is an MoE whose effective size (≈8.7B) is below a dense 12B, yet it
    # is the Writer's BEST pick.
    for verdict, tier in (("Best", spec.best),
                          ("Recommended", spec.recommended),
                          ("Minimum", spec.minimum)):
        if _norm(current) == _norm(tier.model):
            return verdict
    rec = model_size(spec.recommended.model)
    low = model_size(spec.minimum.model)
    if rec and size.effective_b >= rec.effective_b:
        return "Recommended"
    if low and size.effective_b >= low.effective_b:
        return "Minimum"
    return "Below minimum"


def assess(role_models: Dict[str, str], *, vram_gb: Optional[float] = None,
           ram_gb: Optional[float] = None,
           usage: Sequence[Dict[str, Any]] = ()) -> List[Assessment]:
    """Every role against this PC: does its current model meet the spec, can
    this PC run the recommended model, and how busy was it this week.

    `role_models` maps role → model tag (as the placement report builds it);
    `usage` is usage_log.summarise rows."""
    out = []
    for spec in SPECS:
        a = Assessment(spec, current=role_models.get(spec.role, ""))
        a.model_verdict = _model_verdict(a.current, spec)
        rec = model_size(spec.recommended.model)
        best = model_size(spec.best.model)
        fit = _hardware_fit(rec, vram_gb, ram_gb)
        a.hardware_verdict = {
            "gpu": "Runs the recommended model on the graphics card",
            "cpu": "Runs the recommended model on the CPU only (slow)",
            "no": "Too little memory for the recommended model",
            "unknown": "Hardware not checked",
        }[fit]
        if fit == "gpu" and best and _hardware_fit(
                best, vram_gb, ram_gb) == "gpu" and best.tag != rec.tag:
            a.hardware_verdict += "; the best one fits too"
        if a.model_verdict == "Below minimum":
            a.notes.append(f"Use at least {spec.minimum.model}.")
        elif a.model_verdict == "Not US-made":
            a.notes.append(f"Only US-made models are recommended: "
                           f"{spec.recommended.model} does this job.")
        elif a.model_verdict == "Minimum" and fit == "gpu":
            a.notes.append(f"This PC can run {spec.recommended.model} on "
                           f"its graphics card — an easy upgrade for the "
                           f"{spec.label}.")
        elif a.model_verdict == "Minimum" and fit == "cpu":
            a.notes.append(f"{spec.recommended.model} would run here only "
                           f"on the CPU (slow); the upgrade that pays off "
                           f"is a graphics card with "
                           f"{rec.vram_gb:g} GB or more.")
        for u in usage:
            if u.get("role") == spec.role:
                a.calls_7d += int(u.get("calls") or 0)
                a.seconds_7d += float(u.get("seconds") or 0.0)
        out.append(a)
    return out


def upgrade_order(assessments: Sequence[Assessment]) -> List[Assessment]:
    """Where to spend first: highest priority, then whoever falls furthest
    short, then whoever took the most model time this week."""
    short = {"Below minimum": 0, "Not US-made": 0, "Not set": 1,
             "Minimum": 2,
             "Not in catalog": 3, "Recommended": 4, "Best": 5}
    return sorted(assessments, key=lambda a: (
        a.spec.priority, short.get(a.model_verdict, 6), -a.seconds_7d))


def card_text(a: Assessment) -> str:
    """One role's spec card, as the tab shows it."""
    s = a.spec
    L = [f"{s.label} — upgrade priority: {PRIORITY_LABELS[s.priority]}",
         "", s.job, "",
         f"Calls per round: {s.calls}", f"Needs: {s.needs}", "",
         "MINIMUM", "  " + tier_line(s.minimum), "",
         "RECOMMENDED", "  " + tier_line(s.recommended), "",
         "BEST", "  " + tier_line(s.best), "",
         "YOUR SETUP",
         f"  Uses now: {a.current or '(not set — the main model)'}  "
         f"[{a.model_verdict}]",
         f"  This PC: {a.hardware_verdict}"]
    if a.calls_7d:
        L.append(f"  Last 7 days: {a.calls_7d} calls, "
                 f"{a.seconds_7d:.0f} s of model time")
    L += [f"  → {n}" for n in a.notes]
    if s.tips:
        L += ["", "TIPS"] + [f"  • {t}" for t in s.tips]
    if s.duplicate:
        L += ["", "A DUPLICATE (a second copy running at the same time)",
              f"  {s.duplicate}",
              "  Calls run side by side across machines (Machines & roles), "
              "in the Fan-out tab, and between members with 'Parallel "
              "members' on. Cost on one machine: with "
              "Ollama, one loaded model can serve two calls "
              "(OLLAMA_NUM_PARALLEL — extra context memory only); an in-app "
              ".gguf model needs a second copy of its weights."]
    return "\n".join(L)


def system_requirements() -> List[Tuple[str, float, float]]:
    """Per tier: (name, VRAM for its largest model, VRAM to hold every
    distinct model the tier names at once). The first fits with models
    taking turns on the card (Ollama swaps them, with a load each time); the
    second keeps them all loaded."""
    out = []
    for name, pick in (("Minimum", lambda s: s.minimum),
                       ("Recommended", lambda s: s.recommended),
                       ("Best", lambda s: s.best)):
        sizes = {}
        for spec in SPECS:
            size = model_size(pick(spec).model)
            if size:
                sizes[size.tag] = size.vram_gb
        out.append((name, max(sizes.values()), round(sum(sizes.values()), 1)))
    return out


def summary_text(assessments: Sequence[Assessment],
                 hardware: str = "") -> str:
    """The overview: this PC, the whole council's requirements, and the
    order to upgrade in."""
    L = ["Role specs — what each agent needs, and where more power pays off",
         "", f"This PC: {hardware or 'not checked yet'}", "",
         "The whole council, on the graphics card:"]
    for name, largest, together in system_requirements():
        L.append(f"  {name:<12} {largest:g} GB VRAM for the largest model "
                 f"(models take turns) · {together:g} GB to keep them all "
                 f"loaded")
    L += ["  Fewer different models means less memory: roles can share one "
          "(the Models tab). A different model per role is a choice, not a "
          "requirement.", "", "Where to spend first:"]
    for i, a in enumerate(upgrade_order(assessments), start=1):
        L.append(f"  {i}. {a.spec.label} ({PRIORITY_LABELS[a.spec.priority]}"
                 f") — {a.current or 'main model'} [{a.model_verdict}]")
    L += ["", "Click a role in the table for its minimum, recommended and "
          "best model, the memory each needs, and tips.",
          "", "Sizes are from the Council's model catalog (4-bit, 4k "
          "context). RAM for CPU-only is an estimate. Measure real speeds "
          "with 'Check this PC' in the Models tab."]
    return "\n".join(L)


__all__ = ["CPU_OVERHEAD_GB", "PRIORITY_LABELS", "Tier", "RoleSpec", "SPECS",
           "ModelSize", "model_size", "tier_line", "Assessment", "assess",
           "upgrade_order", "card_text", "summary_text",
           "system_requirements"]
