"""
Windows DLL bootstrap — make llama-cpp-python's prebuilt CUDA wheel
find the CUDA runtime DLLs that ship inside the `torch` package.

On Windows, Python 3.8+ uses a strict DLL search path: PATH no longer
contributes to ctypes.CDLL lookups. llama-cpp's `ggml-cuda.dll` is
dynamically linked against `cudart64_12.dll` / `cublas64_12.dll` /
`cublasLt64_12.dll`, which are *not* bundled inside the llama-cpp wheel
but *are* bundled inside the torch wheel under `torch/lib/`.

We explicitly add torch's lib directory to the DLL search path before
llama-cpp is first imported. No-op on non-Windows platforms.

Import this module FIRST — before `council_engine`, `llama_cpp`, or any
module that transitively imports llama_cpp.

Second job — the winmode preload
--------------------------------
Adding the directory is necessary but NOT sufficient. llama-cpp-python
(>=0.3.x) loads its own DLLs with ``winmode=ctypes.RTLD_GLOBAL``, which
on Windows is passed straight to LoadLibraryExW as dwFlags. RTLD_GLOBAL
is 0x100, which Windows reads as LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR — and
specifying ANY LOAD_LIBRARY_SEARCH_* flag *replaces* the default search
order. The loader then looks only in the DLL's own folder: not the
directories added via os.add_dll_directory (0x400), and not even
System32 (0x800). So `ggml-cuda.dll` cannot find `cudart64_12.dll` in
torch/lib, nor `nvcuda.dll` in System32, and the import dies with a
misleading "Could not find module llama.dll".

The fix is to pre-load the chain ourselves using the DEFAULT search
order, bottom-up. Once a module is in the process, llama-cpp's later
LoadLibraryExW on the same full path just bumps the refcount and hands
back the existing handle without re-resolving imports — so its
restrictive winmode no longer matters.
"""
from __future__ import annotations

import os
import sys

# Bottom-up: each entry's dependencies are loaded before it.
_GGML_CHAIN = ("ggml-base", "ggml-cpu", "ggml-cuda", "ggml", "llama")


def _torch_lib_dir() -> str | None:
    """Path to torch/lib WITHOUT importing torch.

    importlib.util.find_spec finds the package on disk in milliseconds,
    while `import torch` executes its __init__ (CUDA context probing,
    ~3.8 s measured) at every app launch. llama-cpp only needs the DLL
    directory on the search path; torch itself loads later, if and when
    something actually uses it.
    """
    try:
        import importlib.util
        spec = importlib.util.find_spec("torch")
    except Exception:
        return None
    if spec is None or not spec.origin:
        return None
    path = os.path.join(os.path.dirname(spec.origin), "lib")
    return path if os.path.isdir(path) else None


def _llama_lib_dir() -> str | None:
    """Path to llama_cpp/lib WITHOUT importing llama_cpp (which is the
    very thing we are trying to make importable)."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("llama_cpp")
    except Exception:
        return None
    if spec is None or not spec.origin:
        return None
    path = os.path.join(os.path.dirname(spec.origin), "lib")
    return path if os.path.isdir(path) else None


def _bootstrap() -> None:
    if sys.platform != "win32":
        return
    if not hasattr(os, "add_dll_directory"):
        return

    llama_lib = _llama_lib_dir()

    for directory in (_torch_lib_dir(), llama_lib):
        if directory:
            try:
                os.add_dll_directory(directory)
            except OSError:
                pass

    # Pre-load the ggml/llama chain under the default search order so the
    # CUDA runtime and driver DLLs actually resolve. Best-effort: on a
    # CPU-only wheel ggml-cuda.dll simply isn't there, and on a genuinely
    # broken install we stay silent and let llama-cpp raise its own error
    # rather than masking it with one of ours.
    if not llama_lib:
        return
    import ctypes
    for name in _GGML_CHAIN:
        dll = os.path.join(llama_lib, f"{name}.dll")
        if not os.path.isfile(dll):
            continue
        try:
            ctypes.CDLL(dll)
        except OSError:
            pass


_bootstrap()
