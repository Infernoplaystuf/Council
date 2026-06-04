"""
tests/smoke_test.py — fast, no-network, no-dependency smoke tests
for the modules that are most likely to regress silently across
platforms.

Runs from any directory; bootstraps sys.path against the repo root.
Exits 0 on pass, non-zero on first failure. CI gates on the exit
code; users can run it manually after install via:

    python tests/smoke_test.py

What this covers (and doesn't):

  • hardware_detect.detect() returns the documented dict shape
    on whatever OS is hosting the test. Doesn't assert specific
    values — the GPU on CI is whatever the runner has.

  • previous_install_detect.detect() returns the documented dict
    shape against a temp dir. Verifies the structural contract.

  • Synthetic GGUF round-trip — writes a valid-magic minimal GGUF
    to a temp file and confirms three independent validators
    accept it:
        onboarding.gguf_file_status
        previous_install_detect._quick_gguf_validate
        council_engine.read_gguf_metadata

  • Negative cases — non-magic file, too-small file, missing file.
    Each validator must reject in the documented way.

What this DOES NOT cover (out of scope here):
  • llama-cpp Llama() construction — needs a real model, real
    compute, and a GPU on the runner; that belongs in a separate
    GPU-enabled integration test.
  • UI / Tkinter — headless CI has no display.
  • Vault indexing — covered by the existing `python -c \"import vault_index\"`
    smoke in installs.txt.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import traceback
from pathlib import Path


# ─── Path setup ─────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ─── Test runner ────────────────────────────────────────────────────
_FAILS: list = []
_PASSES: list = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSES.append(name)
        print(f"  ✓ {name}")
    else:
        _FAILS.append((name, detail))
        print(f"  ✗ {name}   {detail}")


def _run(label: str, fn) -> None:
    print(f"\n── {label} ──")
    try:
        fn()
    except Exception as exc:
        _FAILS.append((label, repr(exc)))
        print(f"  ✗ {label} raised: {exc!r}")
        traceback.print_exc()


# ─── Synthetic GGUF builder ─────────────────────────────────────────
def _make_synthetic_gguf(path: Path,
                          arch: str = "llama",
                          context_length: int = 8192) -> None:
    """Write a minimum-viable GGUF v3 header to ``path``.

    Layout per https://github.com/ggerganov/ggml/blob/master/docs/gguf.md:
        4 B  magic = b"GGUF"
        4 B  version (u32, LE) = 3
        8 B  tensor_count (u64) = 0
        8 B  metadata_kv count (u64)
        for each KV:
            8 B key_len (u64)
            <key_len> bytes — utf-8 key
            4 B value_type (u32)
            value bytes (variable per type)

    We emit two KVs: general.architecture (string type=8) and
    {arch}.context_length (u32 type=4). That's enough for
    read_gguf_metadata + _gguf_max_context_from_metadata to extract
    the context_length end-to-end.
    """
    arch_key = b"general.architecture"
    ctx_key  = f"{arch}.context_length".encode("utf-8")
    arch_val = arch.encode("utf-8")

    with open(path, "wb") as fh:
        # Header
        fh.write(b"GGUF")
        fh.write(struct.pack("<I", 3))      # version
        fh.write(struct.pack("<Q", 0))      # tensor_count
        fh.write(struct.pack("<Q", 2))      # kv_count

        # KV 1 — general.architecture : string
        fh.write(struct.pack("<Q", len(arch_key)))
        fh.write(arch_key)
        fh.write(struct.pack("<I", 8))      # type = string
        fh.write(struct.pack("<Q", len(arch_val)))
        fh.write(arch_val)

        # KV 2 — <arch>.context_length : u32
        fh.write(struct.pack("<Q", len(ctx_key)))
        fh.write(ctx_key)
        fh.write(struct.pack("<I", 4))      # type = u32
        fh.write(struct.pack("<I", context_length))

        # Pad to 1 KB so the size sanity-check in the validators
        # doesn't reject it as "suspiciously small".
        pad_target = 2048
        cur = fh.tell()
        if cur < pad_target:
            fh.write(b"\x00" * (pad_target - cur))


# ─── Tests ──────────────────────────────────────────────────────────
def test_hardware_detect() -> None:
    import hardware_detect as hd
    info = hd.detect()
    _check("returns a dict", isinstance(info, dict))
    expected_keys = {
        "os", "os_version", "python", "cpu_brand", "cpu_cores",
        "ram_gb", "has_avx2", "has_f16c", "gpu_vendor", "gpu_name",
        "vram_gb", "cuda_max", "recommended", "notes",
    }
    missing = expected_keys - set(info)
    _check(f"all documented keys present (missing={sorted(missing)})",
           not missing)
    _check("os value is one of {windows, linux, macos, wsl, unknown}",
           info.get("os") in ("windows", "linux", "macos", "wsl", "unknown"),
           detail=f"got {info.get('os')!r}")
    _check("recommended sub-dict has keys",
           isinstance(info.get("recommended"), dict)
           and {"cuda_tier", "model_tier", "model_pick", "n_ctx_max"}.issubset(
               set(info["recommended"]))
           )
    _check("cuda_tier is a known value",
           info["recommended"]["cuda_tier"] in ("cpu", "cu121", "cu124", "cu128"))


def test_previous_install_detect() -> None:
    import previous_install_detect as pid
    with tempfile.TemporaryDirectory() as td:
        app_dir   = Path(td) / "app"
        vault_dir = Path(td) / "vault"
        app_dir.mkdir(); vault_dir.mkdir()
        info = pid.detect(app_dir, vault_dir)
    _check("returns a dict", isinstance(info, dict))
    expected_keys = {
        "conda_env", "vault", "gguf_models",
        "previous_model", "prior_version", "notes",
    }
    missing = expected_keys - set(info)
    _check(f"all documented keys present (missing={sorted(missing)})",
           not missing)
    _check("vault.present matches the temp dir we created",
           info["vault"]["present"] is True)
    _check("data_in_files is 0 for empty vault",
           info["vault"]["data_in_files"] == 0)
    _check("previous_model is None on empty vault",
           info["previous_model"] is None)


def test_synthetic_gguf_accepted() -> None:
    """A valid-magic GGUF must pass every validator."""
    import council_engine as ce
    import onboarding
    import previous_install_detect as pid

    with tempfile.TemporaryDirectory() as td:
        gguf = Path(td) / "test.gguf"
        _make_synthetic_gguf(gguf, arch="llama", context_length=8192)

        # onboarding.gguf_file_status
        ok, msg = onboarding.gguf_file_status(str(gguf))
        _check(f"onboarding.gguf_file_status accepts synthetic GGUF "
               f"(msg={msg!r})", ok)

        # previous_install_detect._quick_gguf_validate
        _check("previous_install_detect._quick_gguf_validate accepts it",
               pid._quick_gguf_validate(gguf))

        # council_engine.read_gguf_metadata round-trip — must produce
        # a non-empty dict and surface context_length correctly.
        md = ce.read_gguf_metadata(gguf)
        _check("read_gguf_metadata returns non-empty dict",
               isinstance(md, dict) and len(md) > 0,
               detail=f"got {md!r}")
        ctx = ce._gguf_max_context_from_metadata(md)
        _check(f"context_length round-trips through metadata reader "
               f"(got {ctx})", ctx == 8192)


def test_synthetic_gguf_rejected_cases() -> None:
    """Negative cases — every validator must refuse to accept these."""
    import council_engine as ce
    import onboarding
    import previous_install_detect as pid

    with tempfile.TemporaryDirectory() as td:
        # Case 1 — non-GGUF magic
        notgguf = Path(td) / "not.gguf"
        notgguf.write_bytes(b"<!DOCTYPE html>\n<html>" + b"\0" * 4096)
        ok, _ = onboarding.gguf_file_status(str(notgguf))
        _check("non-GGUF magic rejected by onboarding.gguf_file_status",
               not ok)
        _check("non-GGUF magic rejected by _quick_gguf_validate",
               not pid._quick_gguf_validate(notgguf))

        # Case 2 — too small (200 bytes is below the size floor)
        tiny = Path(td) / "tiny.gguf"
        tiny.write_bytes(b"GGUF" + b"\0" * 196)
        ok, _ = onboarding.gguf_file_status(str(tiny))
        _check("too-small file rejected by onboarding.gguf_file_status",
               not ok)
        _check("too-small file rejected by _quick_gguf_validate",
               not pid._quick_gguf_validate(tiny))

        # Case 3 — missing file
        missing = Path(td) / "missing.gguf"
        ok, _ = onboarding.gguf_file_status(str(missing))
        _check("missing file rejected by onboarding.gguf_file_status",
               not ok)
        # read_gguf_metadata on missing file should return {}
        md = ce.read_gguf_metadata(missing)
        _check("read_gguf_metadata returns {} on missing file",
               md == {})


def test_data_summary_triggers() -> None:
    """Data-summary intents must route to the analyst — not freeform.
    These queries are what makes the Council tab actually capable of
    answering 'give me a true data summary of files in this subfolder'.
    """
    import vault_analyst as va
    cases = [
        "give me a true data summary of the files",
        "what's in the sales subfolder?",
        "describe the files in data_in/Q3/",
        "summarize the data in this folder",
        "overview of the files",
        "inventory of files in the vault",
        "data quality on these CSVs",
        "profile this dataset",
    ]
    for q in cases:
        _check(f"looks_computational matches: {q!r}",
               va.looks_computational(q),
               detail="trigger word missing from _COMPUTE_KEYWORDS")


def test_folder_data_summary_helper() -> None:
    """folder_data_summary must:
       * be importable
       * return a DataFrame on an empty folder
       * return one row per file with the documented schema on a
         folder containing a small CSV + a JSON + an image.
    """
    import pandas as pd
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Empty-folder case
        df_empty = va.folder_data_summary(td_path)
        _check("returns a DataFrame on empty folder",
               isinstance(df_empty, pd.DataFrame))
        _check("empty folder produces 0 rows", len(df_empty) == 0)

        # Mixed-content case
        (td_path / "orders.csv").write_text(
            "order_id,total,date\n1,99.5,2024-03-15\n2,150,2024-03-16\n"
        )
        (td_path / "config.json").write_text('{"setting": "value"}')
        (td_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 256)
        df = va.folder_data_summary(td_path)
        _check("returns a DataFrame", isinstance(df, pd.DataFrame))
        _check("documented columns present",
               {"file", "type", "rows", "columns", "size_kb",
                "missing_pct"}.issubset(set(df.columns)))
        types = set(df["type"].astype(str).tolist())
        _check(f"all three types detected (got {sorted(types)})",
               {"csv", "json", "image"}.issubset(types))
        csv_row = df[df["file"] == "orders.csv"]
        _check("CSV row count correct (2 rows)",
               not csv_row.empty
               and int(csv_row.iloc[0]["rows"]) == 2)
        _check("CSV column count correct (3 cols)",
               not csv_row.empty
               and int(csv_row.iloc[0]["columns"]) == 3)


def test_clip_path_persistence() -> None:
    """The vision (mmproj) path must round-trip through backend_settings
    .json without overwriting the GGUF path stored alongside it. This
    catches the regression I almost shipped — save_gguf_path() used to
    overwrite the whole file, which would have wiped clip_path on every
    model swap."""
    import onboarding
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        # Synthesise valid GGUF + mmproj files so the validators don't
        # reject the paths.
        gguf  = vault / "weights.gguf"
        clip  = vault / "mmproj.gguf"
        _make_synthetic_gguf(gguf, arch="llama", context_length=4096)
        _make_synthetic_gguf(clip, arch="llama", context_length=4096)

        # 1) Save the GGUF first.
        onboarding.save_gguf_path(vault, str(gguf))
        _check("after save_gguf_path, load_gguf_path returns the path",
               onboarding.load_gguf_path(vault) == str(gguf))

        # Save the clip path — this is the case that broke before the
        # _merge_backend_settings refactor.
        onboarding.save_clip_path(vault, str(clip))

        # 2) Both paths must now be readable from the SAME file.
        _check("load_clip_path round-trips after save_clip_path",
               onboarding.load_clip_path(vault) == str(clip))
        _check("save_clip_path did NOT clobber gguf_path",
               onboarding.load_gguf_path(vault) == str(gguf))

        # 3) Clearing the clip path (text-only mode) must NOT clobber
        #    the GGUF path either.
        onboarding.save_clip_path(vault, "")
        _check("clearing clip_path leaves gguf_path intact",
               onboarding.load_gguf_path(vault) == str(gguf))
        _check("clearing clip_path makes load_clip_path return empty",
               onboarding.load_clip_path(vault) == "")

        # 4) Env-var override beats persisted JSON.
        try:
            os.environ["COUNCIL_GGUF_CLIP_PATH"] = "/tmp/env-override.gguf"
            _check("env var COUNCIL_GGUF_CLIP_PATH wins over JSON",
                   onboarding.load_clip_path(vault) == "/tmp/env-override.gguf")
        finally:
            os.environ.pop("COUNCIL_GGUF_CLIP_PATH", None)


def test_spc_process_capability_known_values() -> None:
    """Cpk of a centered N(10, 0.5) sample with specs [8, 12] should
    land near 1.33 (process spread ±6 σ = ±3 fits comfortably inside
    [LSL, USL]). Synthetic data → deterministic check."""
    import numpy as np
    from analyst_helpers.spc import process_capability
    rng = np.random.default_rng(seed=42)
    series = rng.normal(loc=10.0, scale=0.5, size=2000)
    out = process_capability(series, lsl=8.0, usl=12.0)
    ppk = out.get("Ppk")
    _check(f"Ppk near 1.33 on centered N(10, 0.5) (got {ppk:.3f})",
           ppk is not None and 1.20 <= ppk <= 1.46)
    _check("normality_ok=True on a Gaussian sample",
           out["normality_ok"] is True)
    _check("n_dropped_nan == 0 on clean data",
           out["n_dropped_nan"] == 0)
    _check("Cp/Cpk are None without subgroup_size",
           out["Cp"] is None and out["Cpk"] is None)
    _check("warnings list mentions the missing subgroup_size",
           any("subgroup_size" in w for w in out["warnings"]))


def test_spc_process_capability_one_sided() -> None:
    """One-sided LSL → Pp must be None (no two-sided spread), Ppk
    reduces to Ppl."""
    from analyst_helpers.spc import process_capability
    out = process_capability(
        [10.0, 11.0, 9.0, 10.5, 9.8, 10.2, 10.1, 9.9, 10.3, 10.0],
        lsl=5.0,
    )
    _check("Pp is None for one-sided spec", out["Pp"] is None)
    _check("Ppk is defined and positive",
           out["Ppk"] is not None and out["Ppk"] > 0)


def test_spc_process_capability_nan_handling() -> None:
    """NaN must be DROPPED with the count surfaced, not filled."""
    import math
    from analyst_helpers.spc import process_capability
    arr = [10.0, 10.5, math.nan, 9.7, math.nan, 10.1, 9.9, 10.3, 10.2]
    out = process_capability(arr, lsl=8.0, usl=12.0)
    _check("n_dropped_nan reports the 2 NaNs", out["n_dropped_nan"] == 2)
    _check("n is the post-drop count", out["n"] == 7)


def test_spc_process_capability_subgroup_size_path() -> None:
    """With subgroup_size, Cp and Cpk should be computed from a
    short-term sigma estimate. Trailing partial subgroups are dropped
    with a warning per the agreed convention."""
    import numpy as np
    from analyst_helpers.spc import process_capability
    rng = np.random.default_rng(seed=7)
    # 102 values → 20 full subgroups of size 5, 2 trailing dropped
    series = rng.normal(loc=10.0, scale=0.5, size=102)
    out = process_capability(series, lsl=8.0, usl=12.0, subgroup_size=5)
    _check("Cp computed when subgroup_size given",
           out["Cp"] is not None and out["Cp"] > 0)
    _check("Cpk computed when subgroup_size given",
           out["Cpk"] is not None and out["Cpk"] > 0)
    _check("trailing-partial-subgroup drop warning present",
           any("trailing values" in w for w in out["warnings"]))


def test_spc_control_chart_limits_xbar() -> None:
    """X-bar chart on a calibrated dataset — center / UCL / LCL must
    bracket the data and match the constants-table arithmetic.
    Subgroup size = 5 → A2 = 0.577 → UCL = grand_mean + 0.577 * R_bar."""
    import numpy as np
    from analyst_helpers.spc import control_chart_limits, _A2_CONSTANTS
    rng = np.random.default_rng(seed=11)
    series = rng.normal(loc=50.0, scale=2.0, size=100)
    out = control_chart_limits(series, chart_type="xbar", subgroup_size=5)
    _check("center bracketed roughly by data mean ±0.5",
           abs(out["center"] - 50.0) < 1.0)
    _check("UCL > center > LCL",
           out["ucl"] > out["center"] > out["lcl"])
    _check("constants_used.A2 matches the table value",
           abs(out["constants_used"]["A2"] - _A2_CONSTANTS[5]) < 1e-9)
    _check("n_subgroups == 100 // 5", out["n_subgroups"] == 20)


def test_spc_control_chart_limits_unknown_chart_type() -> None:
    """Unknown chart_type → ValueError, not a silent fallback."""
    from analyst_helpers.spc import control_chart_limits
    try:
        control_chart_limits([1.0, 2.0, 3.0], chart_type="bogus")
        raised = False
    except ValueError:
        raised = True
    _check("unknown chart_type raises ValueError", raised)


def test_spc_western_electric_rule1() -> None:
    """Single point > 3σ from center → rule 1 fires."""
    from analyst_helpers.spc import western_electric_rules
    # center=10, sigma=1 (UCL=13, LCL=7). Single value at 15 → 5σ → rule 1.
    series = [10.0, 10.5, 9.5, 15.0, 10.2, 9.8]
    df = western_electric_rules(series, ucl=13.0, lcl=7.0, center=10.0)
    _check("rule 1 violation surfaced (n_violations > 0)", len(df) > 0)
    rule_1_hits = df[df["rule_number"] == 1]
    _check("the value at index 3 (15.0) is the rule-1 violator",
           len(rule_1_hits) == 1 and int(rule_1_hits.iloc[0]["index"]) == 3)


def test_spc_western_electric_rule4() -> None:
    """8 consecutive points on the same side of center → rule 4 fires
    no later than index 7."""
    from analyst_helpers.spc import western_electric_rules
    # All points slightly above center=10 (still within 1σ). 10 points.
    series = [10.3, 10.4, 10.2, 10.5, 10.3, 10.1, 10.4, 10.3,
              10.2, 10.5]
    df = western_electric_rules(series, ucl=13.0, lcl=7.0, center=10.0)
    rule_4 = df[df["rule_number"] == 4]
    _check("rule 4 violation surfaced", len(rule_4) > 0)
    _check("rule 4 fires by index 7 (8 consecutive same-side)",
           int(rule_4.iloc[0]["index"]) == 7)


def test_spc_western_electric_clean_data() -> None:
    """A perfectly random-looking sequence centered at the center with
    no consecutive runs should produce ZERO violations."""
    from analyst_helpers.spc import western_electric_rules
    # Alternates above/below center, all within 1σ → no rule trips.
    series = [10.3, 9.7, 10.4, 9.6, 10.5, 9.5, 10.2, 9.8,
              10.1, 9.9]
    df = western_electric_rules(series, ucl=13.0, lcl=7.0, center=10.0)
    _check("no violations on alternating in-control data", len(df) == 0)


def test_spc_sandbox_registration() -> None:
    """The SPC helpers must be registered into the analyst sandbox's
    globals_dict via analyst_helpers.register_helpers — that's the
    contract that makes them callable from model-generated code."""
    import analyst_helpers
    gd: dict = {}
    analyst_helpers.register_helpers(gd)
    for name in ("process_capability", "control_chart_limits",
                 "western_electric_rules"):
        _check(f"sandbox has {name}", name in gd)
    _check("gage_rr is NOT registered (per project policy)",
           "gage_rr" not in gd)


def test_vault_image_suffix_routing() -> None:
    """The image-file routing added in the ship-readiness pass must
    accept image extensions through _PARSEABLE and produce a record
    with type='image'. We don't need PIL — the no-PIL branch is what
    matters for first-run."""
    import vault_index
    _check(".png is in _PARSEABLE", ".png" in vault_index._PARSEABLE)
    _check(".jpg is in _PARSEABLE", ".jpg" in vault_index._PARSEABLE)
    _check(".gif is in _PARSEABLE", ".gif" in vault_index._PARSEABLE)
    # Synthetic empty image — the parser should NOT crash and should
    # still produce a filename record.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 256)
        rec = vault_index._parse_image(p, ".png")
    _check("_parse_image returns dict", isinstance(rec, dict))
    _check("_parse_image record type is 'image'",
           rec.get("type") == "image")


# ─── Main ───────────────────────────────────────────────────────────
def main() -> int:
    print("Council smoke tests")
    print("=" * 70)
    _run("hardware_detect.detect()",            test_hardware_detect)
    _run("previous_install_detect.detect()",    test_previous_install_detect)
    _run("synthetic GGUF — accept case",        test_synthetic_gguf_accepted)
    _run("synthetic GGUF — reject cases",       test_synthetic_gguf_rejected_cases)
    _run("data-summary trigger keywords",       test_data_summary_triggers)
    _run("folder_data_summary helper",          test_folder_data_summary_helper)
    _run("clip_path / GGUF path co-persistence", test_clip_path_persistence)
    _run("SPC — process_capability known-values", test_spc_process_capability_known_values)
    _run("SPC — process_capability one-sided",    test_spc_process_capability_one_sided)
    _run("SPC — process_capability NaN handling", test_spc_process_capability_nan_handling)
    _run("SPC — process_capability subgroup path", test_spc_process_capability_subgroup_size_path)
    _run("SPC — control_chart_limits X-bar",      test_spc_control_chart_limits_xbar)
    _run("SPC — control_chart unknown type",      test_spc_control_chart_limits_unknown_chart_type)
    _run("SPC — Western Electric rule 1",         test_spc_western_electric_rule1)
    _run("SPC — Western Electric rule 4",         test_spc_western_electric_rule4)
    _run("SPC — WE clean data, no violations",    test_spc_western_electric_clean_data)
    _run("SPC — sandbox registration contract",   test_spc_sandbox_registration)
    _run("vault image suffix routing",          test_vault_image_suffix_routing)
    print()
    print("=" * 70)
    print(f"PASSED {len(_PASSES)} · FAILED {len(_FAILS)}")
    if _FAILS:
        print("\nFailures:")
        for name, detail in _FAILS:
            print(f"  ✗ {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
