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

import math
import os
import struct
import sys
import tempfile
import traceback
from pathlib import Path

# Force UTF-8 on stdout/stderr so the box-drawing characters used in
# section headers don't crash on Windows cp1252 consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


def _raises(exc_type, callable_obj) -> bool:
    """True iff `callable_obj()` raises an instance of `exc_type`.
    Used by assertions like _check('foo raises', _raises(ValueError,
    lambda: helper(bad_input)))."""
    try:
        callable_obj()
    except exc_type:
        return True
    except Exception:
        return False
    return False


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


def test_spc_dataframe_column_kwarg() -> None:
    """Multi-column DataFrame input must work via column='<name>',
    matching the convention used by csv_inventory / numeric_summary_
    per_csv. The old _coerce_series raised on multi-column DataFrame
    — this test pins the new behaviour."""
    import pandas as pd
    from analyst_helpers.spc import process_capability
    df = pd.DataFrame({
        "Diameter": [10.0, 10.5, 9.8, 10.2, 10.1, 9.9, 10.3, 10.0,
                     10.2, 9.7, 10.4, 10.1],
        "Operator": ["A"] * 12,
    })
    # column kwarg picks the numeric column by case-insensitive name
    out = process_capability(df, lsl=9.0, usl=11.0, column="diameter")
    _check("multi-column DataFrame + column= works (Ppk defined)",
           out["Ppk"] is not None)
    _check("missing column raises with a helpful message",
           _raises(ValueError,
                   lambda: process_capability(df, lsl=9.0, usl=11.0,
                                                column="nonexistent")))
    _check("multi-column DataFrame WITHOUT column= raises",
           _raises(ValueError,
                   lambda: process_capability(df, lsl=9.0, usl=11.0)))


def test_spc_sandbox_registration() -> None:
    """Asserts both the registered and the intentionally-hidden
    helpers across the package. Future maintainers flipping
    visibility on western_electric_rules or gage_rr have to
    update this test in lockstep with __init__.py."""
    import analyst_helpers
    gd: dict = {}
    analyst_helpers.register_helpers(gd)
    # SPC
    for name in ("process_capability", "control_chart_limits"):
        _check(f"sandbox has {name}", name in gd)
    for name in ("western_electric_rules", "gage_rr"):
        _check(f"{name} is NOT registered (per project policy)",
               name not in gd)
    # Engineering (Gate B)
    for name in ("units_convert", "dimensional_check",
                 "tolerance_stackup", "fft_spectrum",
                 "linear_regression_with_diagnostics"):
        _check(f"sandbox has {name}", name in gd)
    # Stats (Gate B)
    for name in ("descriptive_stats_rigorous", "compare_groups",
                 "multiple_comparison_correction",
                 "correlation_with_significance", "bootstrap_ci"):
        _check(f"sandbox has {name}", name in gd)


# ─── Gate B — engineering helpers ───────────────────────────────────

def test_engineering_units_convert() -> None:
    """Scalar and Series round-trips through pint. Skipped (with a
    PASS) when pint isn't installed — units_convert raises a clear
    ImportError that the helper documentation tells the user how
    to resolve."""
    try:
        from analyst_helpers.engineering import units_convert
        v = units_convert(25.4, "mm", "inch")
        _check(f"25.4 mm → 1 inch (got {v:.4f})", abs(v - 1.0) < 1e-6)
        v2 = units_convert(100.0, "psi", "kPa")
        _check(f"100 psi → ~689.5 kPa (got {v2:.2f})", abs(v2 - 689.5) < 1.0)
    except ImportError as exc:
        # pint is optional — the test "passes" by demonstrating the
        # documented behaviour: the helper raises with a clear hint.
        msg = str(exc)
        _check("ImportError mentions `pip install pint`",
               "pint" in msg.lower() and "install" in msg.lower())


def test_engineering_tolerance_stackup() -> None:
    """Worst-case vs RSS — known values: three ±0.1 components.
    Worst-case ± = 0.3; RSS ± = sqrt(3)/3 × 0.3 / 1 ≈ 0.1732 / 3 × 3."""
    from analyst_helpers.engineering import tolerance_stackup
    nominals   = [10.0, 20.0, 30.0]
    tolerances = [0.1,  0.1,  0.1]
    wc = tolerance_stackup(nominals, tolerances, method="worst_case")
    _check(f"worst-case nominal = 60.0 (got {wc['nominal']})",
           abs(wc["nominal"] - 60.0) < 1e-9)
    _check(f"worst-case tolerance = 0.3 (got {wc['tolerance']})",
           abs(wc["tolerance"] - 0.3) < 1e-9)
    _check("worst-case expected_std is None",
           wc["expected_std"] is None)
    rss = tolerance_stackup(nominals, tolerances, method="rss")
    _check(f"RSS nominal = 60.0 (got {rss['nominal']})",
           abs(rss["nominal"] - 60.0) < 1e-9)
    # RSS: σ_i = 0.1/3 each, σ_stack = sqrt(3) * (0.1/3) ≈ 0.0577,
    # ±3σ stack = ~0.1732.
    _check(f"RSS tolerance ≈ 0.173 (got {rss['tolerance']:.4f})",
           abs(rss["tolerance"] - 0.173205) < 0.001)


def test_engineering_tolerance_stackup_rejects_bad_input() -> None:
    """Length mismatch, negative tolerance, and bad method must
    all raise ValueError — not silently produce a wrong number."""
    from analyst_helpers.engineering import tolerance_stackup
    _check("length mismatch raises",
           _raises(ValueError,
                   lambda: tolerance_stackup([1.0, 2.0], [0.1])))
    _check("negative tolerance raises",
           _raises(ValueError,
                   lambda: tolerance_stackup([1.0], [-0.1])))
    _check("unknown method raises",
           _raises(ValueError,
                   lambda: tolerance_stackup([1.0], [0.1], method="bogus")))


def test_engineering_fft_spectrum_known_peak() -> None:
    """Synthesise a 50 Hz sine sampled at 1000 Hz; the magnitude
    peak should land within 1 bin of 50 Hz."""
    import numpy as np
    from analyst_helpers.engineering import fft_spectrum
    fs = 1000.0
    t = np.arange(0, 2.0, 1.0 / fs)
    signal = np.sin(2 * np.pi * 50.0 * t)
    spec = fft_spectrum(signal, sample_rate_hz=fs)
    peak_idx = int(spec["magnitude"].idxmax())
    peak_freq = float(spec["frequency_hz"].iloc[peak_idx])
    _check(f"50 Hz sine → peak ≈ 50 Hz (got {peak_freq:.2f})",
           abs(peak_freq - 50.0) < 1.0)
    _check("frequency_hz column starts at 0 (DC)",
           abs(float(spec["frequency_hz"].iloc[0])) < 1e-9)


def test_engineering_linear_regression_diagnostics() -> None:
    """Synthetic y = 2*x + noise. Slope estimate should be ~2,
    R² should be high, and the per-coefficient p-value for x
    should be small."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.engineering import linear_regression_with_diagnostics
    rng = np.random.default_rng(123)
    x = rng.uniform(0, 10, size=200)
    y = 2.0 * x + rng.normal(0, 0.5, size=200)
    df = pd.DataFrame({"x": x, "y": y})
    out = linear_regression_with_diagnostics(df, x_cols="x", y_col="y")
    coef = out["coefficients"]
    slope = float(coef.loc[coef["term"] == "x", "estimate"].iloc[0])
    _check(f"slope ≈ 2 (got {slope:.3f})", 1.95 <= slope <= 2.05)
    _check(f"r2 > 0.95 on clean linear data (got {out['r2']:.3f})",
           out["r2"] > 0.95)
    slope_p = float(coef.loc[coef["term"] == "x", "p_value"].iloc[0])
    _check(f"slope p-value tiny (got {slope_p:.2e})", slope_p < 1e-9)
    # VIF is NaN for single predictor — assert that
    slope_vif = float(coef.loc[coef["term"] == "x", "vif"].iloc[0])
    _check("VIF is NaN for single-predictor model",
           math.isnan(slope_vif))


def test_engineering_linear_regression_collinearity_flagged() -> None:
    """Highly correlated predictors → VIF warning surfaces."""
    import math
    import numpy as np
    import pandas as pd
    from analyst_helpers.engineering import linear_regression_with_diagnostics
    rng = np.random.default_rng(0)
    x1 = rng.uniform(0, 10, size=200)
    x2 = x1 + rng.normal(0, 0.1, size=200)   # ~perfectly correlated with x1
    y  = 3.0 * x1 + rng.normal(0, 0.5, size=200)
    df = pd.DataFrame({"x1": x1, "x2": x2, "y": y})
    out = linear_regression_with_diagnostics(
        df, x_cols=["x1", "x2"], y_col="y")
    _check("warning surfaces multicollinearity",
           any("multicollinearity" in w.lower() for w in out["warnings"]))
    coef = out["coefficients"]
    # Max VIF excluding intercept
    vifs = [float(v) for v in coef.loc[coef["term"] != "(Intercept)", "vif"]
             if not math.isnan(float(v))]
    _check(f"max VIF > 10 on near-collinear data (got {max(vifs):.1f})",
           max(vifs) > 10.0)


# ─── Gate B — stats helpers ─────────────────────────────────────────

def test_stats_descriptive() -> None:
    """Hand-built sample with known mean / SD / CI bounds."""
    import math
    from analyst_helpers.stats import descriptive_stats_rigorous
    out = descriptive_stats_rigorous([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    _check("n == 10", out["n"] == 10)
    _check("mean = 5.5", abs(out["mean"] - 5.5) < 1e-9)
    _check("median = 5.5", abs(out["median"] - 5.5) < 1e-9)
    _check("std ≈ 3.028", abs(out["std"] - 3.0277) < 0.001)
    _check("ci95 brackets mean",
           out["ci95_lower"] < out["mean"] < out["ci95_upper"])


def test_stats_compare_groups_picks_ttest() -> None:
    """Two normal groups, equal variance → independent t-test."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.stats import compare_groups
    rng = np.random.default_rng(42)
    a = rng.normal(10.0, 1.0, 100)
    b = rng.normal(10.6, 1.0, 100)
    df = pd.DataFrame({
        "value": np.concatenate([a, b]),
        "group": ["A"] * 100 + ["B"] * 100,
    })
    out = compare_groups(df, value_col="value", group_col="group")
    _check("test_used mentions t-test",
           "t-test" in out["test_used"].lower())
    _check("rationale string non-empty", bool(out["rationale"]))
    _check(f"detected mean diff effect (Cohen's d ~ -0.6, got {out['effect_size']:.3f})",
           abs(out["effect_size"] + 0.6) < 0.3)


def test_stats_compare_groups_picks_mannwhitney() -> None:
    """Skewed data → falls to Mann-Whitney."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.stats import compare_groups
    rng = np.random.default_rng(7)
    a = rng.exponential(scale=1.0, size=80)
    b = rng.exponential(scale=2.0, size=80)
    df = pd.DataFrame({
        "value": np.concatenate([a, b]),
        "group": ["A"] * 80 + ["B"] * 80,
    })
    out = compare_groups(df, value_col="value", group_col="group")
    _check("test_used is Mann-Whitney U",
           "mann-whitney" in out["test_used"].lower())
    _check("rationale references normality failure",
           "normality" in out["rationale"].lower())


def test_stats_multiple_comparison_correction() -> None:
    """Known input: BH on [0.01, 0.04, 0.03, 0.005] at α=0.05 → all
    four reject (their BH-adjusted p's stay below 0.05)."""
    from analyst_helpers.stats import multiple_comparison_correction
    raw = [0.01, 0.04, 0.03, 0.005]
    out = multiple_comparison_correction(raw, method="fdr_bh", alpha=0.05)
    _check(f"BH adjusts all four below α (rejects={out['reject_null'].tolist()})",
           bool(out["reject_null"].all()))
    out_bonf = multiple_comparison_correction(
        raw, method="bonferroni", alpha=0.05)
    # Bonferroni multiplies by m=4: 0.04*4=0.16, 0.03*4=0.12 → not rejected
    _check("Bonferroni rejects fewer than BH",
           int(out_bonf["reject_null"].sum())
           <= int(out["reject_null"].sum()))


def test_stats_bootstrap_ci_reproducible() -> None:
    """random_state seed → identical CI across calls."""
    from analyst_helpers.stats import bootstrap_ci
    data = list(range(1, 101))
    a = bootstrap_ci(data, n_boot=2000, random_state=99)
    b = bootstrap_ci(data, n_boot=2000, random_state=99)
    _check("identical CIs from identical seed", a == b)
    # CI of the mean of 1..100 should bracket 50.5
    _check(f"CI brackets the true mean (50.5) — got [{a[1]:.2f}, {a[2]:.2f}]",
           a[1] < 50.5 < a[2])


def test_stats_correlation_with_significance() -> None:
    """Strong linear correlation should be flagged significant
    after FDR correction; unrelated columns should not."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.stats import correlation_with_significance
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 10, 200)
    df = pd.DataFrame({
        "x":       x,
        "y_linear": 2 * x + rng.normal(0, 0.5, 200),
        "y_noise":  rng.normal(0, 1, 200),
    })
    out = correlation_with_significance(df)
    _check("x vs y_linear correlation > 0.9",
           out["corr"].loc["x", "y_linear"] > 0.9)
    _check("x vs y_linear is FDR-significant",
           bool(out["significant_fdr"].loc["x", "y_linear"]))
    _check("x vs y_noise correlation small",
           abs(out["corr"].loc["x", "y_noise"]) < 0.2)


def test_db_sql_validator_accepts_reads() -> None:
    """The SQL read-only validator must accept the canonical read
    patterns we promise users in the documentation."""
    from db_connections import _validate_select_only
    OK = [
        "SELECT * FROM orders LIMIT 10",
        "select 1",
        "WITH t AS (SELECT * FROM o) SELECT * FROM t",
        "SELECT * FROM /* comment with WORDS */ orders",
        "EXPLAIN SELECT * FROM orders",
        "SHOW TABLES",
        "DESCRIBE orders",
        # trailing semicolons are fine
        "SELECT 1;",
        # case insensitivity
        "Select Count(*) From Orders Where x > 1",
    ]
    for sql in OK:
        try:
            _validate_select_only(sql)
            _check(f"validator accepts: {sql!r}", True)
        except Exception as exc:
            _check(f"validator accepts: {sql!r}", False,
                   detail=f"raised {exc!r}")


def test_db_sql_validator_rejects_writes() -> None:
    """Every documented rejection must actually raise."""
    from db_connections import _validate_select_only, ReadOnlyViolation
    BAD = [
        ("DROP TABLE orders",                       "drop"),
        ("DELETE FROM orders WHERE 1=1",            "delete"),
        ("INSERT INTO orders VALUES (1, 'x')",      "insert"),
        ("UPDATE orders SET total = 0",             "update"),
        ("TRUNCATE TABLE orders",                   "truncate"),
        ("ALTER TABLE orders ADD COLUMN x INT",     "alter"),
        ("CREATE TABLE x (id INT)",                 "create"),
        ("GRANT SELECT ON orders TO public",        "grant"),
        ("MERGE INTO target USING source ON ...",   "merge"),
        # multi-statement
        ("SELECT 1; DROP TABLE orders",             "multi-statement"),
        # comment-cloaked DDL
        ("/* SELECT */ DROP TABLE orders",          "comment cloak"),
        ("-- SELECT \n DROP TABLE orders",          "line-comment cloak"),
        # stored proc that could hide writes
        ("EXEC sp_some_proc",                       "exec sproc"),
        # SET ROLE escalation
        ("SET ROLE admin",                          "SET keyword"),
        # empty / comment-only
        ("",                                         "empty"),
        ("-- only a comment",                       "comment only"),
        ("/* only */ /* comments */",               "block comments only"),
    ]
    for sql, label in BAD:
        ok = _raises(ReadOnlyViolation, lambda s=sql: _validate_select_only(s))
        _check(f"validator rejects {label}: {sql!r}", ok)


def test_db_mongo_pipeline_validator() -> None:
    """Mongo aggregation pipelines with write or server-side-JS
    stages must be rejected. Read-only stages must pass."""
    from db_connections import (_validate_mongo_pipeline,
                                 MongoPipelineViolation)
    OK_PIPELINES = [
        [{"$match": {"level": "error"}}],
        [{"$group": {"_id": "$service", "n": {"$sum": 1}}},
         {"$sort": {"n": -1}},
         {"$limit": 100}],
        [{"$project": {"name": 1, "_id": 0}}],
        [],   # empty pipeline is technically valid for find-like reads
    ]
    for p in OK_PIPELINES:
        try:
            _validate_mongo_pipeline(p)
            _check(f"pipeline accepted: {p}", True)
        except Exception as exc:
            _check(f"pipeline accepted: {p}", False, detail=repr(exc))

    BAD_PIPELINES = [
        ("$out",         [{"$match": {}}, {"$out": "errors_archive"}]),
        ("$merge",       [{"$match": {}}, {"$merge": {"into": "x"}}]),
        ("$function",    [{"$project": {"x": {"$function":
                            {"body": "fn", "args": [], "lang": "js"}}}}]),
        ("$accumulator", [{"$group": {"_id": None,
                            "acc": {"$accumulator": {"init": "f"}}}}]),
        ("$where",       [{"$match": {"$where": "this.x > 1"}}]),
    ]
    for label, pipeline in BAD_PIPELINES:
        ok = _raises(MongoPipelineViolation,
                     lambda p=pipeline: _validate_mongo_pipeline(p))
        _check(f"pipeline with {label} rejected", ok)

    # Non-list payloads
    _check("non-list pipeline rejected",
           _raises(MongoPipelineViolation,
                   lambda: _validate_mongo_pipeline("not a list")))
    _check("non-dict stage rejected",
           _raises(MongoPipelineViolation,
                   lambda: _validate_mongo_pipeline([1, 2, 3])))


def test_db_unresolved_env_var() -> None:
    """An unset ${ENV_VAR} in a URL must surface as UnresolvedEnvVarError
    with the missing names listed — NOT silently pass through to the
    driver and yield an opaque auth failure."""
    import db_connections as _db
    # Make sure the test env var is really not set
    os.environ.pop("COUNCIL_TEST_MISSING_VAR", None)
    os.environ.pop("COUNCIL_TEST_PRESENT_VAR", None)

    raised = False
    try:
        _db._resolve_url("postgresql://u:${COUNCIL_TEST_MISSING_VAR}@h/d")
    except _db.UnresolvedEnvVarError as exc:
        raised = True
        _check("UnresolvedEnvVarError lists the missing name",
               "COUNCIL_TEST_MISSING_VAR" in exc.missing)
    _check("missing env var raises UnresolvedEnvVarError", raised)

    # Multiple missing → all listed
    raised = False
    try:
        _db._resolve_url(
            "mongodb://${COUNCIL_TEST_MISSING_VAR}:${COUNCIL_TEST_OTHER}@h/d")
    except _db.UnresolvedEnvVarError as exc:
        raised = True
        names = set(exc.missing)
        _check("both missing env vars listed",
               "COUNCIL_TEST_MISSING_VAR" in names
               and "COUNCIL_TEST_OTHER" in names)
    _check("multi-missing also raises", raised)

    # When the var IS set, _resolve_url succeeds
    os.environ["COUNCIL_TEST_PRESENT_VAR"] = "secret"
    try:
        out = _db._resolve_url(
            "postgresql://u:${COUNCIL_TEST_PRESENT_VAR}@h/d")
        _check("present env var substituted",
               out == "postgresql://u:secret@h/d")
    finally:
        os.environ.pop("COUNCIL_TEST_PRESENT_VAR", None)


def test_db_connection_storage_roundtrip() -> None:
    """Saved connections round-trip through the JSON files; passwords
    with ${ENV_VAR} placeholders pass through unmolested."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        _db.save_sql_connection(
            vault, "sales_db",
            "postgresql://readonly_user:${PG_PASS}@host:5432/sales")
        _db.save_mongo_connection(
            vault, "logs",
            "mongodb://readonly_user:${MONGO_PASS}@host:27017/")

        sql_map   = _db.list_sql_connections(vault)
        mongo_map = _db.list_mongo_connections(vault)
        _check("SQL connection round-trips",
               sql_map.get("sales_db", "").endswith("/sales")
               and "${PG_PASS}" in sql_map["sales_db"])
        _check("Mongo connection round-trips",
               mongo_map.get("logs", "").endswith(":27017/")
               and "${MONGO_PASS}" in mongo_map["logs"])

        # Removal
        _check("remove SQL returns True", _db.remove_sql_connection(vault, "sales_db"))
        _check("remove SQL again returns False",
               not _db.remove_sql_connection(vault, "sales_db"))
        _check("after remove, list is empty",
               _db.list_sql_connections(vault) == {})


def test_db_tls_posture_warnings() -> None:
    """check_tls_posture must warn on cleartext-creds-over-non-local
    URLs and stay silent on safe ones (localhost, RFC1918, ${ENV_VAR}-
    protected, explicit TLS hint)."""
    import db_connections as _db
    SHOULD_WARN = [
        ("postgresql://u:hunter2@db.example.com:5432/sales",       "remote PG plaintext"),
        ("mysql+pymysql://u:hunter2@db.example.com:3306/sales",    "remote MySQL plaintext"),
        ("mongodb://u:hunter2@mongo.example.com:27017/",           "remote Mongo plaintext"),
    ]
    SHOULD_NOT_WARN = [
        # ${ENV_VAR}-protected
        ("postgresql://u:${PG_PASS}@db.example.com/sales",         "env-var password"),
        # localhost / RFC1918
        ("postgresql://u:hunter2@localhost/sales",                 "localhost"),
        ("postgresql://u:hunter2@127.0.0.1/sales",                 "127.0.0.1"),
        ("postgresql://u:hunter2@10.1.2.3/sales",                  "10.x RFC1918"),
        ("postgresql://u:hunter2@192.168.1.5/sales",               "192.168.x RFC1918"),
        ("postgresql://u:hunter2@172.16.0.1/sales",                "172.16.x RFC1918"),
        # explicit TLS hint
        ("postgresql://u:p@db.example.com/sales?sslmode=require",  "sslmode=require"),
        ("mysql+pymysql://u:p@db.example.com/sales?ssl=true",      "ssl=true"),
        ("mssql+pyodbc://u:p@db.example.com/sales?encrypt=yes",    "encrypt=yes"),
        ("mongodb://u:p@mongo.example.com/?tls=true",              "tls=true"),
        ("mongodb+srv://u:p@cluster.example.com/sales",            "mongodb+srv (TLS by default)"),
        # File-based, no network
        ("sqlite:///C:/data/sales.db",                              "sqlite file"),
        ("duckdb:///C:/data/sales.duckdb",                          "duckdb file"),
        # No credentials at all
        ("postgresql://db.example.com/sales",                       "no credentials"),
    ]
    for url, label in SHOULD_WARN:
        warning = _db.check_tls_posture(url)
        _check(f"TLS warn on {label}", warning is not None,
               detail=f"got {warning!r} for {url!r}")
    for url, label in SHOULD_NOT_WARN:
        warning = _db.check_tls_posture(url)
        _check(f"TLS silent on {label}", warning is None,
               detail=f"got {warning!r} for {url!r}")

    # Env-var suppression
    try:
        os.environ["COUNCIL_DB_TLS_WARN"] = "0"
        _check("COUNCIL_DB_TLS_WARN=0 suppresses warnings",
               _db.check_tls_posture(
                   "postgresql://u:p@db.example.com/sales") is None)
    finally:
        os.environ.pop("COUNCIL_DB_TLS_WARN", None)


def test_db_engine_cache_reuses_engines() -> None:
    """Repeated _sql_engine calls for the same connection must return
    the cached instance. A different env-var value for the same
    placeholder yields a NEW engine (because the cache key includes
    the resolved URL)."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        # SQLite in-memory engine via the file-mode bypass — no real
        # disk, no network. Skip if SQLAlchemy isn't installed.
        try:
            _db._import_sqlalchemy()
        except Exception:
            _check("SQLAlchemy not installed — engine cache test "
                   "skipped (expected on minimal builds)", True)
            return
        _db.save_sql_connection(vault, "test_db",
                                 "sqlite:///" + str(vault / "test.db"))
        try:
            eng_a = _db._sql_engine(vault, "test_db")
            eng_b = _db._sql_engine(vault, "test_db")
            _check("cached engine identity preserved", eng_a is eng_b)
        finally:
            _db.dispose_engines()


def test_db_audit_log_rotation() -> None:
    """When the audit log exceeds the size cap, it gets rotated to
    db_audit.log.1 and a fresh file is started. Set a tiny cap via
    env var so the test doesn't have to write 100 MB."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        try:
            os.environ["COUNCIL_DB_AUDIT_MAX_MB"] = "1"   # 1 MB cap
            os.environ["COUNCIL_DB_AUDIT_KEEP"]   = "3"
            # The cap check uses bytes — write enough records to
            # cross the MB boundary. Each record is ~120 bytes, so
            # ~10K records = ~1.2 MB.
            for i in range(11000):
                _db._audit(vault, kind="test", n=i, msg="x" * 50)
            # After the rotation fires (on the write that pushes the
            # CURRENT log past the cap), the current log should be
            # smaller than the cap and db_audit.log.1 should exist.
            cur = vault / "db_audit.log"
            rot = vault / "db_audit.log.1"
            _check("current log exists", cur.is_file())
            _check("rotated .1 file exists", rot.is_file())
            _check("rotated log carries records",
                   rot.stat().st_size > 0)
        finally:
            os.environ.pop("COUNCIL_DB_AUDIT_MAX_MB", None)
            os.environ.pop("COUNCIL_DB_AUDIT_KEEP", None)


def test_db_mongo_roundtrip_mongomock() -> None:
    """End-to-end Mongo helpers exercised against an in-memory mongomock
    instance. Skipped (with PASS) when mongomock isn't installed —
    the test is optional CI coverage, not a hard requirement.

    Install with: pip install mongomock
    """
    try:
        import mongomock  # type: ignore[import]
    except ImportError:
        _check("mongomock not installed — roundtrip test skipped "
               "(install with `pip install mongomock` for full Mongo "
               "coverage)", True)
        return
    import db_connections as _db

    # Monkey-patch _mongo_client to return a mongomock client. We
    # restore it on exit so other tests aren't affected.
    real_mongo_client = _db._mongo_client
    def _fake_client(vault_dir, conn_name, **kw):
        return mongomock.MongoClient()
    _db._mongo_client = _fake_client   # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            _db.save_mongo_connection(vault, "mock", "mongodb://localhost/")

            # Seed some data
            client = mongomock.MongoClient()
            client["testdb"]["events"].insert_many([
                {"level": "info",  "service": "api",  "n": 1},
                {"level": "error", "service": "api",  "n": 2},
                {"level": "error", "service": "db",   "n": 3},
                {"level": "warn",  "service": "auth", "n": 4},
            ])
            # Repoint our fake client constructor to this seeded client
            _db._mongo_client = (   # type: ignore[assignment]
                lambda vault_dir, conn_name, **kw: client)

            # list_mongo_collections
            cols = _db.list_mongo_collections(vault, "mock", "testdb")
            _check("list_mongo_collections sees the seeded collection",
                   "events" in cols)

            # read_mongo_collection
            df = _db.read_mongo_collection(
                vault, "mock", "testdb", "events",
                query={"level": "error"})
            _check("read_mongo_collection returns 2 error rows",
                   len(df) == 2)

            # mongo_count
            n = _db.mongo_count(vault, "mock", "testdb", "events",
                                 query={"service": "api"})
            _check(f"mongo_count returns 2 api rows (got {n})", n == 2)

            # mongo_distinct
            services = sorted(_db.mongo_distinct(
                vault, "mock", "testdb", "events", "service"))
            _check(f"mongo_distinct returns 3 services (got {services})",
                   services == ["api", "auth", "db"])

            # mongo_aggregate with a benign pipeline
            agg = _db.mongo_aggregate(
                vault, "mock", "testdb", "events",
                pipeline=[
                    {"$match": {"level": "error"}},
                    {"$group": {"_id": "$service", "n": {"$sum": 1}}},
                ])
            _check("aggregate returns 2 groups", len(agg) == 2)
    finally:
        _db._mongo_client = real_mongo_client   # type: ignore[assignment]


def test_db_audit_log_writes() -> None:
    """The audit logger writes one JSONL record per call and never
    raises — even when the file can't be created (the audit log is
    forensic and must never break a query)."""
    import db_connections as _db
    import json as _json
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        _db._audit(vault, kind="test", note="hello", n=42)
        _db._audit(vault, kind="test", note="world", n=43)
        log_path = vault / "db_audit.log"
        _check("audit log file created", log_path.is_file())
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        _check("two records written", len(lines) == 2)
        rec = _json.loads(lines[0])
        _check("record has ts + kind + note", {"ts", "kind", "note"} <= set(rec))
        _check("record contents preserved",
               rec["kind"] == "test" and rec["note"] == "hello")


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
    _run("SPC — multi-col DataFrame + column=",   test_spc_dataframe_column_kwarg)
    _run("SPC — sandbox registration contract",   test_spc_sandbox_registration)
    # DB connectivity — read-only enforcement
    _run("DB — SQL validator accepts reads",      test_db_sql_validator_accepts_reads)
    _run("DB — SQL validator rejects writes",     test_db_sql_validator_rejects_writes)
    _run("DB — Mongo pipeline validator",         test_db_mongo_pipeline_validator)
    _run("DB — connection storage round-trip",    test_db_connection_storage_roundtrip)
    _run("DB — unresolved env-var error",         test_db_unresolved_env_var)
    _run("DB — TLS posture warnings",             test_db_tls_posture_warnings)
    _run("DB — engine cache reuse",               test_db_engine_cache_reuses_engines)
    _run("DB — audit log rotation",               test_db_audit_log_rotation)
    _run("DB — Mongo round-trip (mongomock)",     test_db_mongo_roundtrip_mongomock)
    _run("DB — audit log writes JSONL",           test_db_audit_log_writes)
    # Gate B — engineering
    _run("ENG — units_convert (or ImportError)",  test_engineering_units_convert)
    _run("ENG — tolerance_stackup known values",  test_engineering_tolerance_stackup)
    _run("ENG — tolerance_stackup bad input",     test_engineering_tolerance_stackup_rejects_bad_input)
    _run("ENG — fft_spectrum 50 Hz peak",         test_engineering_fft_spectrum_known_peak)
    _run("ENG — linear regression diagnostics",   test_engineering_linear_regression_diagnostics)
    _run("ENG — VIF / collinearity warning",      test_engineering_linear_regression_collinearity_flagged)
    # Gate B — stats
    _run("STAT — descriptive_stats_rigorous",     test_stats_descriptive)
    _run("STAT — compare_groups picks t-test",    test_stats_compare_groups_picks_ttest)
    _run("STAT — compare_groups picks MW-U",      test_stats_compare_groups_picks_mannwhitney)
    _run("STAT — MC correction (Bonf vs BH)",     test_stats_multiple_comparison_correction)
    _run("STAT — bootstrap CI reproducibility",   test_stats_bootstrap_ci_reproducible)
    _run("STAT — correlation_with_significance",  test_stats_correlation_with_significance)
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
