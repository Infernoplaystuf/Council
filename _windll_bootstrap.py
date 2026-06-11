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
"""
from __future__ import annotations

import os
import sys


def _bootstrap() -> None:
    if sys.platform != "win32":
        return
    if not hasattr(os, "add_dll_directory"):
        return
    # Locate torch WITHOUT importing it. We only need the path to
    # torch/lib — importlib.util.find_spec finds the package on disk in
    # milliseconds, while `import torch` executes its __init__ (CUDA
    # context probing, ~3.8 s measured) at every app launch. llama-cpp
    # only needs the DLL directory on the search path; torch itself
    # loads later, if and when something actually uses it.
    try:
        import importlib.util
        spec = importlib.util.find_spec("torch")
    except Exception:
        return
    if spec is None or not spec.origin:
        return
    torch_lib = os.path.join(os.path.dirname(spec.origin), "lib")
    if os.path.isdir(torch_lib):
        try:
            os.add_dll_directory(torch_lib)
        except OSError:
            pass


_bootstrap()
