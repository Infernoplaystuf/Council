"""
analyst_helpers — domain-specific analytic helpers for the Council's
sandboxed analyst.

The functions in this package are registered into the analyst sandbox's
globals_dict by `register_helpers(globals_dict)`. The model's generated
pandas code can then call them by name — same convention as the older
helpers that live directly in `vault_analyst.py` (csv_inventory,
folder_data_summary, etc.).

Modules:

    spc          — Statistical Process Control. Capability indices,
                   control-chart limits, Western Electric / Nelson
                   rule detection. (Gage R&R exists but is NOT
                   registered yet — see spc.py header.)

    engineering  — (Gate B) units, dimensional checking, tolerance
                   stack-ups, FFT, regression with diagnostics.

    stats        — (Gate B) rigorous descriptive stats, test-selection
                   helper, multiple-comparison correction, bootstrap.

Sandbox-safety constraints (do not violate):

  • No filesystem writes outside `data_out/` / `workspace/`. None of
    the helpers here write at all; outputs are returned objects.
  • No network. No subprocess. No `eval` / `exec` / `compile`.
  • Imports limited to what the analyst's _safe_import allowlist
    permits: pandas, numpy, math, re, json, statistics, collections,
    scipy. (scipy was added to the allowlist in Gate A; the helpers
    here rely on scipy.stats / scipy.fft.)
  • Functions take Series / DataFrame / array-like / file path inputs
    and return dict / DataFrame / scalar — never print, never write.
"""
from __future__ import annotations

from typing import Any, Dict


def register_helpers(globals_dict: Dict[str, Any]) -> None:
    """Inject every public, sandbox-safe helper into the analyst's
    `globals_dict`. Called by `vault_analyst.execute_pandas_code`
    just before `exec(code, globals_dict)`.

    Imports are local so a sandbox load failure for one module
    (e.g. scipy not installed) doesn't take the others down.

    NOT registered (implemented but hidden by project policy):
      • spc.gage_rr            — ANOVA Gage R&R study
      • spc.western_electric_rules — Nelson / WE rule detector

    Both functions exist in spc.py and are fully working; they're
    just kept out of the sandbox namespace + analyst prompt so the
    model doesn't suggest them yet. To enable, add the matching
    name to the SPC block below AND update the analyst prompt
    (vault_analyst.build_pandas_code_prompt) AND extend the smoke-
    test sandbox-registration assertion in lockstep.
    """
    # ── Phase 1a — SPC ──────────────────────────────────────────────
    try:
        from . import spc as _spc
        globals_dict.update({
            "process_capability":   _spc.process_capability,
            "control_chart_limits": _spc.control_chart_limits,
            # NOTE: western_electric_rules and gage_rr are
            # intentionally NOT registered. See module docstring.
        })
    except Exception as exc:
        # Best-effort registration. If scipy is missing on a CPU-only
        # bundle that didn't install the optional analytic deps, the
        # remaining helpers in the sandbox (csv_inventory, etc.) keep
        # working — we just lose the SPC capabilities. A clear
        # ImportError will surface when the model tries to call them.
        import sys as _sys
        print(f"[analyst_helpers] SPC helpers not registered: {exc!r}",
              file=_sys.stderr)

    # ── Phase 1b/1c — engineering + stats (Gate B placeholders) ─────
    # Wired in at Gate B. Keeping the import sites here so the
    # register call's shape doesn't churn between gates.
    # try:
    #     from . import engineering as _eng
    #     globals_dict.update({...})
    # except Exception as exc: ...
    # try:
    #     from . import stats as _stats
    #     globals_dict.update({...})
    # except Exception as exc: ...
