"""
hardware_detect.py — best-effort hardware probe for the setup wizard.

Returns a structured snapshot of the user's machine so the wizard can
recommend a model size + CUDA wheel tier without making the user read
the GPU mapping table. Designed to be cheap, non-fatal, and dependency-
light — everything except the optional `torch.cuda` probe runs from
the standard library.

The probe NEVER raises. Missing fields come back as None / empty string
and the wizard surfaces them as "unknown" rather than failing.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional


# ============================================================
# Public API
# ============================================================

def detect() -> Dict[str, Any]:
    """One-shot snapshot. Returns a dict that's safe to JSON-serialize
    and stash in the wizard state.

    Keys:
        os:                "windows" | "linux" | "macos" | "wsl" | "unknown"
        os_version:        platform-specific version string
        python:            "3.11.7" style version
        cpu_brand:         e.g. "Intel(R) Core(TM) i7-14700K"
        cpu_cores:         physical core count (logical if physical unknown)
        ram_gb:            total system RAM in GB (float)
        has_avx2:          bool
        has_f16c:          bool
        gpu_vendor:        "nvidia" | "amd" | "intel" | "apple" | None
        gpu_name:          full GPU model string (or None)
        vram_gb:           VRAM in GB on the primary GPU (or None)
        cuda_max:          max CUDA version the driver supports, e.g. 12.4 (or None)
        vram_free_gb:      free VRAM right now (nvidia-smi), or None
        gpu_mem_clock_mhz: the GPU's max memory clock (nvidia-smi), or None
        cpu_p_cores:       performance cores on a hybrid CPU (Windows), or None
        cpu_e_cores:       efficiency cores on a hybrid CPU, or None
        cpu_logical:       logical processors
        ram_speed_mts:     DIMM speed in MT/s (Windows), or None
        recommended:       sub-dict with "model_tier", "cuda_tier", "n_ctx_max"
        notes:             list of human-readable observations
    """
    split = cpu_core_split()
    # The RAM-speed query is a PowerShell CIM call (~1.5-2 s measured; wmic
    # is gone on this Windows 11). Run it beside the other probes instead of
    # after them: detect() measured 3.1 s before it, 5.0 s run serially.
    import threading
    speed: Dict[str, Optional[int]] = {}
    speed_thread = threading.Thread(
        target=lambda: speed.__setitem__("mts", _ram_speed_mts()),
        name="ram-speed-probe", daemon=True)
    speed_thread.start()
    info: Dict[str, Any] = {
        "os":           _detect_os(),
        "os_version":   _os_version(),
        "python":       platform.python_version(),
        "cpu_brand":    _cpu_brand(),
        "cpu_cores":    _cpu_cores(),
        "cpu_p_cores":  split.get("p_cores"),
        "cpu_e_cores":  split.get("e_cores"),
        "cpu_logical":  split.get("logical"),
        "ram_gb":       _ram_gb(),
        "ram_speed_mts": None,
        "has_avx2":     False,
        "has_f16c":     False,
        "gpu_vendor":   None,
        "gpu_name":     None,
        "vram_gb":      None,
        "vram_free_gb": None,
        "gpu_mem_clock_mhz": None,
        "cuda_max":     None,
        "notes":        [],
    }
    _fill_cpu_features(info)
    _fill_gpu(info)
    info["recommended"] = _recommend(info)
    speed_thread.join(timeout=7)
    info["ram_speed_mts"] = speed.get("mts")
    return info


# ============================================================
# P-cores / E-cores (hybrid Intel CPUs)
# ============================================================

def cpu_core_split() -> Dict[str, Optional[int]]:
    """{"p_cores", "e_cores", "physical", "logical"} — stdlib only.

    WHY: llama.cpp splits each matrix evenly across its threads, so on a
    hybrid CPU one E-core thread holds the P-core threads up every token.
    psutil cannot tell the two apart (it reports 20 "physical" cores on the
    i7-14700HX: 8 P + 12 E). Windows can: GetLogicalProcessorInformationEx
    gives each core an EfficiencyClass, and the P-cores are the highest.
    On a CPU with one class (most AMD and pre-12th-gen Intel) p_cores is
    every physical core and e_cores is 0. Never raises; unknowns are None.
    """
    out: Dict[str, Optional[int]] = {"p_cores": None, "e_cores": None,
                                     "physical": None,
                                     "logical": os.cpu_count()}
    if sys.platform.startswith("win"):
        try:
            classes, logical = _win_core_classes()
            if classes:
                top = max(classes)
                out["physical"] = sum(classes.values())
                out["p_cores"] = classes[top]
                out["e_cores"] = out["physical"] - classes[top]
                out["logical"] = logical or out["logical"]
                return out
        except Exception:
            pass
    phys = _cpu_cores()
    out["physical"] = phys
    out["p_cores"] = phys
    out["e_cores"] = 0 if phys else None
    return out


def _win_core_classes():
    """({efficiency class: core count}, logical processors) from
    GetLogicalProcessorInformationEx(RelationProcessorCore).

    Record layout (winnt.h): DWORD Relationship, DWORD Size, then a
    PROCESSOR_RELATIONSHIP — BYTE Flags, BYTE EfficiencyClass,
    BYTE Reserved[20], WORD GroupCount, then GROUP_AFFINITY entries
    (KAFFINITY Mask, WORD Group, WORD Reserved[3]) aligned to 8.
    """
    import ctypes
    import struct
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = k32.GetLogicalProcessorInformationEx
    fn.argtypes = [ctypes.c_int, ctypes.c_void_p,
                   ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.BOOL
    relation_processor_core = 0
    size = wintypes.DWORD(0)
    fn(relation_processor_core, None, ctypes.byref(size))
    if not size.value:
        return {}, 0
    buf = ctypes.create_string_buffer(size.value)
    if not fn(relation_processor_core, buf, ctypes.byref(size)):
        return {}, 0
    raw = buf.raw[:size.value]
    ptr_size = ctypes.sizeof(ctypes.c_void_p)
    classes: Dict[int, int] = {}
    logical = 0
    off = 0
    while off + 8 <= len(raw):
        rel, rec_size = struct.unpack_from("<II", raw, off)
        if rec_size <= 0:
            break
        if rel == relation_processor_core:
            eff = raw[off + 9]
            groups = struct.unpack_from("<H", raw, off + 8 + 22)[0]
            mask_off = off + 8 + 24
            for g in range(max(1, groups)):
                fmt = "<Q" if ptr_size == 8 else "<I"
                mask = struct.unpack_from(fmt, raw,
                                          mask_off + g * (ptr_size + 8))[0]
                logical += bin(mask).count("1")
            classes[eff] = classes.get(eff, 0) + 1
        off += rec_size
    return classes, logical


def quick_memory():
    """(total VRAM GB, total RAM GB) without the rest of detect() — what the
    engine needs to pick a default model that fits. One nvidia-smi call."""
    vram = None
    try:
        exe = shutil.which("nvidia-smi")
        if exe:
            r = subprocess.run(
                [exe, "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            first = (r.stdout or "").strip().splitlines()
            if first and first[0].strip().isdigit():
                vram = round(int(first[0].strip()) / 1024, 1)
    except Exception:
        vram = None
    return vram, _ram_gb()


def _ram_speed_mts() -> Optional[int]:
    """Configured DIMM speed (MT/s) — the number that bounds CPU-side token
    generation (DDR5-5600 dual channel ≈ 89.6 GB/s). Windows only; one
    PowerShell CIM query, so detect() stays on a worker."""
    if not sys.platform.startswith("win"):
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_PhysicalMemory | "
             "Measure-Object -Property ConfiguredClockSpeed -Maximum)"
             ".Maximum"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        v = (r.stdout or "").strip().splitlines()
        return int(v[-1]) if v and v[-1].strip().isdigit() else None
    except Exception:
        return None


# ============================================================
# OS detection
# ============================================================

def _detect_os() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        # WSL leaves "microsoft" in /proc/version
        try:
            with open("/proc/version", "r", encoding="utf-8",
                      errors="ignore") as fh:
                if "microsoft" in fh.read().lower():
                    return "wsl"
        except Exception:
            pass
        return "linux"
    return "unknown"


def resolve_embed_device() -> Optional[str]:
    """Device for the sentence-transformers embedder, or None to auto-pick.

    Honors COUNCIL_EMBED_DEVICE when set. Otherwise defaults to 'cpu' on
    WSL: loading the embedder on the SAME GPU as a fully-offloaded GGUF
    model exhausts VRAM there and causes a CUDA error / NATIVE crash (core
    dump, no Python traceback). This makes that safe default apply no matter
    how the app is launched (not only via run-wsl.sh). Other platforms
    return None so sentence-transformers picks GPU when one is present."""
    ov = os.environ.get("COUNCIL_EMBED_DEVICE", "").strip()
    if ov:
        return ov
    try:
        if _detect_os() == "wsl":
            return "cpu"
    except Exception:
        pass
    return None


def _os_version() -> str:
    try:
        if sys.platform.startswith("win"):
            return f"Windows {platform.release()} ({platform.version()})"
        if sys.platform == "darwin":
            return f"macOS {platform.mac_ver()[0]}"
        # Linux / WSL
        rel = platform.release()
        try:
            with open("/etc/os-release", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PRETTY_NAME="):
                        return f"{line.split('=', 1)[1].strip().strip(chr(34))} ({rel})"
        except Exception:
            pass
        return rel
    except Exception:
        return platform.platform()


# ============================================================
# CPU
# ============================================================

def _cpu_brand() -> Optional[str]:
    # psutil-free path. /proc/cpuinfo on Linux/WSL, registry / wmic on
    # Windows, sysctl on macOS. All fail-soft to None.
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", "r", encoding="utf-8",
                      errors="ignore") as fh:
                for line in fh:
                    if "model name" in line:
                        return line.split(":", 1)[1].strip()
        elif sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                return r.stdout.strip()
        elif sys.platform.startswith("win"):
            # `wmic` is deprecated on Win 11 but still present. Fall back to
            # `powershell -Command (Get-CimInstance Win32_Processor).Name`
            # when wmic isn't available.
            try:
                r = subprocess.run(
                    ["wmic", "cpu", "get", "name"],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
                    if len(lines) >= 2:
                        return lines[1]
            except Exception:
                pass
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_Processor).Name"],
                    capture_output=True, text=True, timeout=4,
                )
                if r.returncode == 0:
                    return r.stdout.strip()
            except Exception:
                pass
    except Exception:
        pass
    # Last resort
    return platform.processor() or None


def _cpu_cores() -> Optional[int]:
    # Prefer physical cores; fall back to logical count.
    try:
        import psutil  # type: ignore[import]
        n = psutil.cpu_count(logical=False)
        if n and n >= 1:
            return int(n)
    except Exception:
        pass
    n = os.cpu_count()
    return int(n) if n else None


def _fill_cpu_features(info: Dict[str, Any]) -> None:
    flags: list = []
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", "r", encoding="utf-8",
                      errors="ignore") as fh:
                for line in fh:
                    if line.startswith("flags") or line.startswith("Features"):
                        flags = line.split(":", 1)[1].strip().split()
                        break
        elif sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.features"],
                                capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                flags = [f.lower() for f in r.stdout.strip().split()]
            r2 = subprocess.run(["sysctl", "-n", "machdep.cpu.leaf7_features"],
                                 capture_output=True, text=True, timeout=2)
            if r2.returncode == 0:
                flags.extend(f.lower() for f in r2.stdout.strip().split())
        elif sys.platform.startswith("win"):
            # Windows doesn't expose feature flags through a clean stdlib API.
            # AVX2 is present on every CPU released since 2013 (Haswell+);
            # for shipping purposes we assume yes unless we can prove
            # otherwise via cpuid. Skip the probe — the engine-side guard
            # in council_engine catches SIGILL at runtime if AVX2 is
            # actually missing.
            info["notes"].append(
                "CPU feature flags not probed on Windows — assuming AVX2/F16C "
                "present (every Intel/AMD chip from 2013+ has them).")
            info["has_avx2"] = True
            info["has_f16c"] = True
            return
    except Exception:
        pass
    info["has_avx2"] = "avx2" in flags
    info["has_f16c"] = "f16c" in flags
    if flags and not info["has_avx2"]:
        info["notes"].append(
            "CPU is missing AVX2 — prebuilt llama-cpp-python wheels will "
            "crash with 'illegal instruction'. Build from source with "
            "CMAKE_ARGS=\"-DGGML_AVX2=OFF -DGGML_F16C=OFF\".")


# ============================================================
# RAM
# ============================================================

def _ram_gb() -> Optional[float]:
    try:
        import psutil  # type: ignore[import]
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        pass
    # psutil-free path
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 ** 2), 1)
        elif sys.platform.startswith("win"):
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint),
                    ("dwMemoryLoad", ctypes.c_uint),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / (1024 ** 3), 1)
        elif sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                return round(int(r.stdout.strip()) / (1024 ** 3), 1)
    except Exception:
        pass
    return None


# ============================================================
# GPU
# ============================================================

def _fill_gpu(info: Dict[str, Any]) -> None:
    # Path 1 — nvidia-smi. Works on Windows + Linux + WSL, no torch dep.
    if _try_nvidia_smi(info):
        return
    # Path 2 — torch.cuda. Slower (imports torch) but more reliable on
    # weird Windows installs where nvidia-smi isn't on PATH.
    if _try_torch_cuda(info):
        return
    # Path 3 — Apple silicon
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=4,
            )
            if r.returncode == 0 and "Apple" in r.stdout:
                info["gpu_vendor"] = "apple"
                # Best-effort name extraction
                for line in r.stdout.splitlines():
                    s = line.strip()
                    if s.startswith("Chipset Model"):
                        info["gpu_name"] = s.split(":", 1)[1].strip()
                        break
                info["notes"].append(
                    "Apple Silicon detected — llama-cpp-python uses Metal "
                    "via the standard PyPI wheel (no CUDA tier needed).")
                return
        except Exception:
            pass


def _try_nvidia_smi(info: Dict[str, Any]) -> bool:
    if not shutil.which("nvidia-smi"):
        return False
    try:
        # Query GPU name + total memory in MB
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,driver_version,memory.free,"
             "clocks.max.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode != 0:
            return False
        first = r.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in first.split(",")]
        if len(parts) >= 2:
            info["gpu_vendor"] = "nvidia"
            info["gpu_name"]   = parts[0]
            try:
                info["vram_gb"] = round(float(parts[1]) / 1024, 1)
            except Exception:
                pass
        # Free VRAM (what a model can actually have now) and the memory
        # clock (decode speed is bounded by memory bandwidth). "[N/A]" on
        # GPUs that do not report one — left as None.
        if len(parts) >= 4:
            try:
                info["vram_free_gb"] = round(float(parts[3]) / 1024, 1)
            except Exception:
                pass
        if len(parts) >= 5:
            try:
                info["gpu_mem_clock_mhz"] = int(float(parts[4]))
            except Exception:
                pass
        # Parse "CUDA Version: 12.4" from the smi header
        r2 = subprocess.run(["nvidia-smi"], capture_output=True,
                             text=True, timeout=4)
        if r2.returncode == 0:
            import re as _re
            m = _re.search(r"CUDA Version:\s*([0-9]+\.[0-9]+)", r2.stdout)
            if m:
                try:
                    info["cuda_max"] = float(m.group(1))
                except Exception:
                    pass
        return True
    except Exception:
        return False


def _try_torch_cuda(info: Dict[str, Any]) -> bool:
    try:
        import torch  # type: ignore[import]
        if not torch.cuda.is_available():
            return False
        info["gpu_vendor"] = "nvidia"
        info["gpu_name"]   = torch.cuda.get_device_name(0)
        total = torch.cuda.get_device_properties(0).total_memory
        info["vram_gb"]    = round(total / (1024 ** 3), 1)
        # torch.version.cuda is what THIS wheel was built against, not
        # what the driver supports — useful as a floor estimate.
        try:
            info["cuda_max"] = float(torch.version.cuda)
        except Exception:
            pass
        return True
    except Exception:
        return False


# ============================================================
# Recommendation
# ============================================================

def _recommend(info: Dict[str, Any]) -> Dict[str, Any]:
    """Map the hardware snapshot to a recommended model size + CUDA wheel
    tier + n_ctx ceiling. The wizard surfaces this; the user can still
    override every part.
    """
    vram = info.get("vram_gb") or 0
    ram  = info.get("ram_gb") or 0
    has_gpu = info.get("gpu_vendor") == "nvidia"
    cuda_max = info.get("cuda_max") or 0.0

    # CUDA wheel tier ladder. Conservative — older wheels work on newer
    # drivers but not vice-versa.
    if not has_gpu:
        cuda_tier = "cpu"
    elif cuda_max >= 12.8:
        cuda_tier = "cu128"
    elif cuda_max >= 12.4:
        cuda_tier = "cu124"
    elif cuda_max >= 12.0:
        cuda_tier = "cu121"
    else:
        cuda_tier = "cpu"

    # Model-size tier. The numbers come from real KV-cache + weights
    # measurements on Q4_K_M quants; we leave a 1-2 GB margin for OS +
    # driver overhead.
    if vram >= 22:
        model_tier = "large"      # 30-70B class
    elif vram >= 12:
        model_tier = "medium"     # 14B class
    elif vram >= 7:
        model_tier = "small"      # 7-9B class
    elif vram >= 4 or ram >= 16:
        model_tier = "tiny"       # 3B class
    else:
        model_tier = "cpu_only"
    model_pick = _catalog_pick(vram if has_gpu else 0)

    # n_ctx ceiling — proportional to VRAM after subtracting model
    # weights. Powers of two so it lines up with the engine's
    # VRAM-aware ladder.
    if vram >= 22:
        n_ctx_max = 32768
    elif vram >= 12:
        n_ctx_max = 16384
    elif vram >= 7:
        n_ctx_max = 8192
    elif vram >= 4:
        n_ctx_max = 4096
    else:
        n_ctx_max = 4096

    return {
        "cuda_tier":   cuda_tier,
        "model_tier":  model_tier,
        "model_pick":  model_pick,
        "n_ctx_max":   n_ctx_max,
    }


def _catalog_pick(vram_gb: float) -> str:
    """The pick, from model_catalog.fits — the SAME rule the Models tab ranks
    with. It used to be a hard-coded string per tier, and for 8 GB it named
    "Llama 3.1 8B Q5_K_M (or Gemma 2 9B)" — both of which the catalog rejects
    on 8 GB (7.0 and 7.5 GB + the 1.5 GB margin). Now the first two general
    models that fit, defaults first; with no usable GPU, the smallest."""
    try:
        import model_catalog as mc
    except Exception:
        return "Llama 3.2 3B Instruct Q4_K_M"
    fitting = mc.for_vram(vram_gb, role="general") if vram_gb else []
    if fitting:
        names = [m.name for m in fitting[:2]]
        return names[0] + (f"  (or {names[1]})" if len(names) > 1 else "")
    small = sorted((m for m in mc.MODELS if m.role == "general"),
                   key=lambda m: m.size_gb)
    return (small[0].name + "  (CPU inference)") if small else \
        "Llama 3.2 3B Instruct Q4_K_M  (CPU inference)"


# ============================================================
# CLI for ad-hoc inspection
# ============================================================

if __name__ == "__main__":
    import json as _json
    print(_json.dumps(detect(), indent=2, default=str))
