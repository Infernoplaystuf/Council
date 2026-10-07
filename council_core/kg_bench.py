"""KG0 — which local model can find links in free text, measured on labelled
snippets, so the extractor model is chosen by evidence, not by reputation.

The 30 snippets use the synthetic Ironbridge world (council_core/kg_corpus.py)
and are written to hit what the laptop measured going wrong: direction
(SUPERSEDES), labels (WORKS_ON vs LEADS), part->project links (no model found
one), plus negation, hedging, passive voice, several links in one sentence,
'Last, First', an ambiguous initial, and distractors with no link at all.
Each snippet's ``tests`` says what it probes, so a miss can be read.

Scoring is on the links that SURVIVE the Council's checks (kg_extract.check):
that is what would reach the user. Precision = surviving links that are right
/ all surviving links; recall = right links / all gold links. Also reported:
how many raw model links the checks rejected or corrected, the JSON failure
count, and seconds per snippet.

    python -m council_core.kg_bench llama3.1:8b granite3.3:8b ...
writes a JSON + Markdown report to <app dir>/kg_bench/ (not the vault).
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from council_core import kg_corpus
from council_core import kg_extract as kx

Gold = Tuple[str, str, str]

EXTRA = {
    "ines": ("PERSON", "Inés Moreau", ["Moreau, Inés"]),
    "raj": ("PERSON", "Raj Patel", []),
    "vlv_c": ("PART", "VLV-210/C", []),
    "vlv_d": ("PART", "VLV-210/D", []),
    "kestrel": ("PROJECT", "PRJ-0952", ["Kestrel Pump Line", "Kestrel"]),
}


def known() -> List[kx.Known]:
    ents = dict(kg_corpus.ENTITIES)
    ents.update(EXTRA)
    return [kx.Known(k, t, n, list(a)) for k, (t, n, a) in ents.items()]


@dataclass
class Snippet:
    id: str
    tests: str
    text: str
    gold: List[Gold]


S = Snippet
SNIPPETS: List[Snippet] = [
    S("s01", "plain LEADS", "Dana Whitfield leads the Helios Turbine Upgrade.",
      [("dana", "LEADS", "helios")]),
    S("s02", "passive part->project", "The BRG-7720 bearing pack will be fitted to the Helios rig for the endurance run.",
      [("helios", "USES_PART", "brg")]),
    S("s03", "supersedes, natural order", "PN-1234/B supersedes PN-1234/A on all builds.",
      [("shim_b", "SUPERSEDES", "shim_a")]),
    S("s04", "supersedes, reversed wording", "PN-1234/A has been replaced by PN-1234/B.",
      [("shim_b", "SUPERSEDES", "shim_a")]),
    S("s05", "negation — no link", "Carol Lee no longer works on Helios; she moved to other duties.",
      []),
    S("s06", "two people, one project", "Tomás Echeverría and Bob Smith attended the Northwind Retrofit design review.",
      [("tomas", "WORKS_ON", "northwind"), ("bob", "WORKS_ON", "northwind")]),
    S("s07", "Last, First + contact", "Point of contact for the SL-0450 seal: Raman, Priya.",
      [("priya", "CONTACT_FOR", "seal")]),
    S("s08", "hedge — no link", "Marcus Oyelaran is considering whether HX-3301 could suit the Bearings & Seals Program.",
      []),
    S("s09", "distractor — co-mention only", "Hana Kowalski and Dan Whitfield had lunch near the Atlas lab.",
      []),
    S("s10", "OWNS via 'sources'", "Bob Smith will source the PN-0088 inlet gaskets.",
      [("bob", "OWNS", "gasket")]),
    S("s11", "part->project + owner", "The CTL-5005 controller board on the Atlas Test Rig is Hana Kowalski's responsibility.",
      [("atlas", "USES_PART", "ctl"), ("hana", "OWNS", "ctl")]),
    S("s12", "lead vs works on", "Inés Moreau now runs the Kestrel Pump Line; Raj Patel supports her on it.",
      [("ines", "LEADS", "kestrel"), ("raj", "WORKS_ON", "kestrel")]),
    S("s13", "project code only", "PRJ-0952 will use valve VLV-210/D from May.",
      [("kestrel", "USES_PART", "vlv_d")]),
    S("s14", "revision letters, verb 'retires'", "VLV-210/D retires VLV-210/C across the Kestrel line.",
      [("vlv_d", "SUPERSEDES", "vlv_c"), ("kestrel", "USES_PART", "vlv_d")]),
    S("s15", "nothing at all", "The quarterly fire drill is on Friday at 10:00.",
      []),
    S("s16", "test rig uses part", "PN-1234/B will be proven on the Atlas Test Rig before release.",
      [("atlas", "USES_PART", "shim_b")]),
    S("s17", "approval = works on", "ECN-0915-014 was approved by Carol Lee and Hana Kowalski for the Helios Turbine Upgrade.",
      [("carol", "WORKS_ON", "helios"), ("hana", "WORKS_ON", "helios")]),
    S("s18", "past tense lead", "Marcus Oyelaran led the Bearings & Seals Program through its first audit.",
      [("marcus", "LEADS", "bands")]),
    S("s19", "contact for project", "Questions about Northwind should go to Tomás Echeverría, the programme contact.",
      [("tomas", "CONTACT_FOR", "northwind")]),
    S("s20", "two parts, one project", "Northwind uses both the HX-3301 core and the PN-0088 gasket.",
      [("northwind", "USES_PART", "hx"), ("northwind", "USES_PART", "gasket")]),
    S("s21", "alias name of project", "The seal SL-0450 is standard on the Bearings & Seals Program.",
      [("bands", "USES_PART", "seal")]),
    S("s22", "negated part use", "Helios will not use the SL-0450 seal.",
      []),
    S("s23", "pronoun", "Priya Raman owns the SL-0450 seal. She is also the contact for it.",
      [("priya", "OWNS", "seal"), ("priya", "CONTACT_FOR", "seal")]),
    S("s24", "list of three", "Dana Whitfield, Carol Lee and Hana Kowalski all work on Helios.",
      [("dana", "WORKS_ON", "helios"), ("carol", "WORKS_ON", "helios"),
       ("hana", "WORKS_ON", "helios")]),
    S("s25", "ambiguous initial — skip", "D. Whitfield reviewed the shim drawings for Helios.",
      []),
    S("s26", "short name alias", "Atlas runs on the CTL-5005 board.",
      [("atlas", "USES_PART", "ctl")]),
    S("s27", "planned future use counts", "Kestrel is scheduled to adopt the BRG-7720 bearing pack next quarter.",
      [("kestrel", "USES_PART", "brg")]),
    S("s28", "comparison — no link", "The VLV-210/C valve is cheaper than the HX-3301 core.",
      []),
    S("s29", "owner + supersedes", "Dana Whitfield owns PN-1234/B, which replaces the A revision PN-1234/A.",
      [("dana", "OWNS", "shim_b"), ("shim_b", "SUPERSEDES", "shim_a")]),
    S("s30", "former lead — current only", "Raj Patel took over the Atlas Test Rig from Hana Kowalski, who left the project.",
      [("raj", "LEADS", "atlas")]),
]


@dataclass
class SnippetResult:
    id: str
    tests: str
    gold: List[Gold]
    got: List[Gold]
    rejected: int
    fixed: int
    error: str
    seconds: float


@dataclass
class ModelReport:
    model: str
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    correct: int = 0
    proposed: int = 0
    gold: int = 0
    rejected: int = 0
    fixed: int = 0
    errors: int = 0
    seconds_per_snippet: float = 0.0
    part_project_recall: float = 0.0
    no_link_false_positives: int = 0
    snippets: List[SnippetResult] = field(default_factory=list)


def score(model: str, results: List[SnippetResult]) -> ModelReport:
    rep = ModelReport(model=model, snippets=results)
    pp_gold = pp_hit = 0
    for r in results:
        g, got = set(r.gold), set(r.got)
        rep.correct += len(g & got)
        rep.proposed += len(got)
        rep.gold += len(g)
        rep.rejected += r.rejected
        rep.fixed += r.fixed
        rep.errors += bool(r.error)
        if not g:
            rep.no_link_false_positives += len(got)
        for t in g:
            if t[1] == "USES_PART":
                pp_gold += 1
                pp_hit += t in got
    rep.precision = rep.correct / rep.proposed if rep.proposed else 0.0
    rep.recall = rep.correct / rep.gold if rep.gold else 0.0
    rep.f1 = (2 * rep.precision * rep.recall / (rep.precision + rep.recall)
              if rep.precision + rep.recall else 0.0)
    rep.part_project_recall = pp_hit / pp_gold if pp_gold else 0.0
    rep.seconds_per_snippet = (sum(r.seconds for r in results) / len(results)) if results else 0.0
    return rep


def run_model(model: str, chat=None, snippets: Sequence[Snippet] = SNIPPETS,
              on_progress=None) -> ModelReport:
    chat = chat or kx.ollama_chat(model)
    ents = known()
    results = []
    for i, s in enumerate(snippets):
        t0 = time.time()
        res = kx.extract(s.text, ents, chat)
        dt = time.time() - t0
        results.append(SnippetResult(
            s.id, s.tests, list(s.gold), [(l.subject, l.predicate, l.object) for l in res.links],
            len(res.rejected), sum(bool(l.fixed) for l in res.links), res.error, round(dt, 2)))
        if on_progress:
            on_progress(model, i + 1, len(snippets))
    return score(model, results)


def report_markdown(reports: List[ModelReport]) -> str:
    lines = ["| model | precision | recall | F1 | part→project recall | false links on no-link snippets "
             "| rejected by checks | corrected | JSON errors | s / snippet |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(reports, key=lambda r: -r.f1):
        lines.append(f"| {r.model} | {r.precision:.2f} | {r.recall:.2f} | {r.f1:.2f} | "
                     f"{r.part_project_recall:.2f} | {r.no_link_false_positives} | {r.rejected} | "
                     f"{r.fixed} | {r.errors} | {r.seconds_per_snippet:.1f} |")
    return "\n".join(lines)


def out_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "Council" if base else Path.home() / ".council_app") / "kg_bench"


def main(models: Sequence[str]) -> int:
    reports = []
    for m in models:
        print(f"== {m}", flush=True)
        rep = run_model(m, on_progress=lambda mm, i, n: print(f"  {i}/{n}", end="\r", flush=True))
        print(f"  P={rep.precision:.2f} R={rep.recall:.2f} F1={rep.f1:.2f} "
              f"{rep.seconds_per_snippet:.1f}s/snippet errors={rep.errors}", flush=True)
        reports.append(rep)
    d = out_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (d / f"kg0-{stamp}.json").write_text(json.dumps([asdict(r) for r in reports], indent=1),
                                         encoding="utf-8")
    md = report_markdown(reports)
    (d / f"kg0-{stamp}.md").write_text(md + "\n", encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
