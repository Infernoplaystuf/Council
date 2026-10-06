"""
Role specs: each role's minimum / recommended / best model, against a PC.

Pinned: every tier names a US model that is in the catalog; a model the spec
names gets that tier whatever its size (gpt-oss-20b is an MoE smaller than a
dense 12B by effective size, yet it is the Writer's best); an upgrade is
called easy only when it fits the graphics card; non-US models are flagged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import role_specs as rs  # noqa: E402
from council_core.local_models import maker_and_origin  # noqa: E402
from council_core.model_slots import COUNCIL_ROLES  # noqa: E402


def _a(assessments, role):
    return next(a for a in assessments if a.spec.role == role)


def test_every_role_has_a_spec_and_every_tier_is_a_us_catalog_model():
    assert {s.role for s in rs.SPECS} == set(COUNCIL_ROLES)
    for s in rs.SPECS:
        for tier in (s.minimum, s.recommended, s.best):
            size = rs.model_size(tier.model)
            assert size, (s.role, tier.model)
            assert maker_and_origin(tier.model)[1] == "US", tier.model
        low, rec, best = (rs.model_size(t.model).vram_gb
                          for t in (s.minimum, s.recommended, s.best))
        assert low <= rec <= best, s.role


def test_named_tiers_win_over_size():
    a = rs.assess({"writer": "gpt-oss:20b", "judge": "phi4:14b"})
    assert _a(a, "writer").model_verdict == "Best"
    assert _a(a, "judge").model_verdict == "Recommended"


def test_verdicts_by_size_and_origin():
    a = rs.assess({"skeptic": "llama3.2:1b", "artist": "qwen2.5:7b",
                   "sage": "olmo2:13b", "intern": "my-own-model"})
    assert _a(a, "skeptic").model_verdict == "Below minimum"
    assert "llama3.2:3b" in _a(a, "skeptic").notes[0]
    assert _a(a, "artist").model_verdict == "Not US-made"
    assert _a(a, "sage").model_verdict == "Recommended"   # 13.7B ≥ 12.2B
    assert _a(a, "intern").model_verdict == "Not in catalog"


def test_an_upgrade_is_easy_only_on_the_graphics_card():
    roles = {"intern": "llama3.2:3b", "coder": "granite3-dense:8b"}
    a = rs.assess(roles, vram_gb=8, ram_gb=32)
    assert "graphics card" in _a(a, "intern").notes[0]          # 8B: 6.2 GB
    assert "only on the CPU" in _a(a, "coder").notes[0]         # 14B: 11 GB
    assert _a(a, "coder").hardware_verdict.startswith(
        "Runs the recommended model on the CPU")
    big = rs.assess(roles, vram_gb=24, ram_gb=64)
    assert "the best one fits too" in _a(big, "coder").hardware_verdict
    tiny = rs.assess(roles, vram_gb=None, ram_gb=4)
    assert _a(tiny, "coder").hardware_verdict.startswith("Too little")
    assert _a(rs.assess(roles), "coder").hardware_verdict == \
        "Hardware not checked"


def test_upgrade_order_puts_quality_roles_and_shortfalls_first():
    a = rs.assess({"writer": "llama3.1:8b", "judge": "gpt-oss:20b",
                   "peasant": "llama3.2:1b"},
                  usage=[{"role": "peasant", "calls": 40, "seconds": 90.0}])
    order = [x.spec.role for x in rs.upgrade_order(a)]
    assert order.index("writer") < order.index("judge")   # Minimum < Best
    assert order[:2] == ["writer", "judge"]
    assert _a(a, "peasant").calls_7d == 40


def test_card_explains_tiers_memory_and_duplicates():
    a = rs.assess({"peasant": "llama3.2:1b"}, vram_gb=8, ram_gb=32)
    card = rs.card_text(_a(a, "peasant"))
    for must in ("MINIMUM", "RECOMMENDED", "BEST", "GB VRAM", "GB RAM",
                 "YOUR SETUP", "A DUPLICATE", "OLLAMA_NUM_PARALLEL",
                 "side by side"):
        assert must in card, must
    summary = rs.summary_text(a, "8 GB GPU")
    assert "Where to spend first" in summary and "8 GB GPU" in summary


def test_system_requirements_grow_by_tier():
    reqs = rs.system_requirements()
    assert [r[0] for r in reqs] == ["Minimum", "Recommended", "Best"]
    largest = [r[1] for r in reqs]
    assert largest == sorted(largest)
    for _name, big, together in reqs:
        assert together >= big
    assert "keep them all loaded" in rs.summary_text(rs.assess({}))
