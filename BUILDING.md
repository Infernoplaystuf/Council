# Building a standalone Datas Inferno bundle

This document covers how to package the Council app into a single
folder the end user can run **without installing Python or any
dependencies**. The output is `dist/DatasInferno/DatasInferno.exe`
(Windows) or `dist/DatasInferno/DatasInferno` (mac / Linux), plus a
few hundred MB of bundled libraries.

The packaging tool is **PyInstaller**. We pin no version — anything
≥ 6.0 works.

---

## 1. Prerequisites on the build machine

You need a working Python 3.11 environment with **every runtime
dependency installed**. PyInstaller looks at what your interpreter
can actually import, so if a library is missing locally it will be
missing in the bundle.

```cmd
:: Recommended: build inside the same conda env you develop in.
::   conda activate council        (or whatever you named yours)

:: Sanity-check the version:
python --version
:: -> Python 3.11.x

:: Install everything the app needs:
pip install -r requirements.txt

:: Install the packager itself:
pip install pyinstaller
```

If you've never set up the dev environment, follow `installs.txt`
first. It walks through the four CUDA variants of `llama-cpp-python`
plus the conda + pip mix that avoids ABI breakage on numpy / pandas.

### Optional: enable CUDA in the bundled llama-cpp

Whatever variant of `llama-cpp-python` you installed (CPU, cu121,
cu124, or nightly cu128) is what the bundle ships. The CUDA DLLs
are pulled in automatically via `collect_all('llama_cpp')`. There is
no extra step.

---

## 2. Build

### Windows

```cmd
build.bat
```

### macOS / Linux

```bash
chmod +x build.sh
./build.sh
```

Both scripts:

1. Wipe `build/` and `dist/`.
2. Sanity-check that Python + PyInstaller are on PATH.
3. Probe-import the critical deps (`llama_cpp`, `pandas`, `numpy`,
   `openpyxl`, `tkinter`, `yaml`, `requests`) — fails fast if any
   are missing, so you don't wait 10 minutes for PyInstaller to
   discover the same thing.
4. Run `pyinstaller council.spec --noconfirm --clean`.
5. Report the final bundle size.

Typical build time: **5–15 minutes** depending on disk + CPU.
Bundle size: **~1.2–1.5 GB on disk**, **~600 MB zipped**.

---

## 3. What ends up in the bundle

```
dist/DatasInferno/
├── DatasInferno.exe              ← entry point
├── _internal/                    ← PyInstaller's bundled Python + libs
│   ├── python311.dll
│   ├── llama_cpp/                ← native llama.dll + CUDA helpers
│   ├── pandas/                   ← Python files + pandas C extensions
│   ├── numpy/, openpyxl/, ...    ← every dep from requirements.txt
│   ├── tcl/, tk/                 ← Tkinter UI libraries
│   └── ...
├── assets/                       ← icons, splash, sample CSVs
├── personality_config.yaml
├── personality_backends.json
├── README.md
├── USER_GUIDE.md
└── BUILDING.md                   ← this file
```

### What is NOT bundled, and why

| Thing | Why excluded | Where it lives instead |
|---|---|---|
| **GGUF model** (5–15 GB) | Too big; users pick their own (Phi-4, Llama 3.1, Granite, …) | Wherever the user downloads it; `COUNCIL_GGUF_PATH` env var or in-app Browse button points at it |
| **Vault folder** | User data — must persist across reinstalls | `%USERPROFILE%\.council\vault\` (Windows) / `~/.council/vault/` (Unix) |
| **Sentence-Transformer weights** (~80 MB) | Downloaded on demand by `huggingface_hub` | `~/.cache/huggingface/` on first "build embeddings" command |
| **CUDA toolkit** | End user only needs the runtime, not the full SDK | NVIDIA driver bundles the runtime; if user has the matching driver, GPU offload works |

---

## 4. First-run setup on a fresh target machine

1. Copy the entire `dist/DatasInferno/` folder anywhere (e.g.
   `C:\Programs\DatasInferno\`). Make sure the user has write
   permission to `%USERPROFILE%\.council\` (default on Windows).
2. Download a GGUF model. Recommended for the typical workload:
   * **Phi-4 14B Q4_K_M** (~9 GB) — best reasoning at 16 GB VRAM
   * **Llama 3.1 8B Q5_K_M** (~6 GB) — longest context (128K)
   * **Granite 3.1 8B** — IBM, the current baseline
3. Run `DatasInferno.exe`. On first launch the app:
   * creates `~/.council/vault/` and its `data_in/`, `logs/`,
     `workspace/`, etc. subfolders
   * prompts for the model path if `COUNCIL_GGUF_PATH` is unset
4. (Recommended) Before launching, raise the context window in
   the environment so big folder dumps don't get clipped:

   ```cmd
   :: Windows cmd
   set COUNCIL_GGUF_N_CTX=16384

   :: PowerShell
   $env:COUNCIL_GGUF_N_CTX = "16384"
   ```

   Phi-4 supports 16K natively; Llama 3.1 and Granite 3.x go up to
   128K. KV-cache RAM roughly doubles each time you double `n_ctx`.

---

## 5. Distribution

The simplest distribution is a **zip of `dist/DatasInferno/`** with
a short README pointing at the model-download step. Variations:

* **Inno Setup / NSIS installer (Windows)** — registers a Start
  Menu shortcut, optionally adds an uninstaller, and sets up the
  Windows AppUserModelID so taskbar pinning works. Use the
  `assets/icon.ico` we already bundle as the installer icon.
* **DMG (macOS)** — `hdiutil create` against the bundle folder, or
  `create-dmg` for a fancy installer window.
* **AppImage (Linux)** — `appimagetool` against the bundle folder
  plus a `.desktop` file that points at `DatasInferno`.

### Windows code signing

Unsigned `.exe`s from new publishers will trigger SmartScreen
"Unrecognized app" warnings on first run. To avoid that:

1. Buy a code-signing cert from a CA Microsoft trusts (DigiCert,
   SSL.com, Sectigo). EV certs are recognized immediately;
   standard OV certs gain trust gradually as more users run them.
2. Sign with `signtool sign /fd SHA256 /a /tr <timestamp-url> /td SHA256
   DatasInferno.exe` after the build.

Without a signed `.exe`, advise users to click "More info → Run
anyway" on the SmartScreen dialog the first time.

---

## 6. Troubleshooting

### "PyInstaller failed: ModuleNotFoundError: No module named X"

Add `X` to the `hidden` list in `council.spec`, then rebuild.
PyInstaller's static analyser can't see modules imported from
inside `try/except`, `importlib.import_module(...)`, or any
indirect mechanism.

### Bundle launches but crashes immediately, no window appears

Edit `council.spec`, change `console=False` to `console=True`,
rebuild, and run from a terminal. The console will show the
Python traceback. Common causes:

* Missing native DLL (set `COUNCIL_GGUF_PATH=...` to a real file
  first; the app shouldn't crash but a stray import might).
* `llama_cpp` not pulled in — confirm `collect_all('llama_cpp')`
  ran cleanly and the spec wasn't edited locally.

### Bundle is huge (> 2 GB)

Likely Torch sneaked in. Verify:

```cmd
pip uninstall torch torchvision torchaudio
```

…unless you actually need PyTorch in your env for another tool.
The Council app doesn't use it directly; `sentence_transformers`
will fall back to a smaller Sentence-Transformer backend if Torch
is missing.

### Antivirus / Windows Defender flags `DatasInferno.exe`

PyInstaller bundles trip heuristic AV scanners regularly. Two
mitigations:

1. **Don't enable UPX compression** — already disabled in the
   spec (`upx=False`). UPX is the single biggest AV-flag cause.
2. **Code-sign the .exe** (see §5). Signed binaries get whitelisted
   much faster.

If a specific scanner flags it, submit a false-positive report to
the vendor with a sample of the bundle. They typically clear it
within a few business days for legitimate signed software.

### "Failed to load Python DLL" on the target machine

PyInstaller bundles Python — the target machine should NOT need
Python installed. If this error appears, the bundle is incomplete:
re-build with `--clean` and verify `_internal/python311.dll` is
present in `dist/DatasInferno/`.

---

## 7. CI / automated builds

The build is fully scriptable. A minimal GitHub Actions job:

```yaml
- uses: actions/setup-python@v5
  with: { python-version: "3.11" }
- run: pip install -r requirements.txt pyinstaller
- run: pyinstaller council.spec --noconfirm --clean
- uses: actions/upload-artifact@v4
  with:
    name: DatasInferno-${{ runner.os }}
    path: dist/DatasInferno/
```

Use the `windows-latest`, `macos-latest`, `ubuntu-latest` runners
to produce all three platform bundles in parallel. CUDA support
on Linux/Windows runners requires either a self-hosted runner with
a GPU **or** building the CPU variant of `llama-cpp-python` for
distribution and letting end users replace it locally.
