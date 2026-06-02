# Running Data's Inferno on WSL — step by step

This is the "from zero to chat" guide. Follow every step in order.
Total time on a clean Windows install: about 20-30 minutes (most of
that is the conda env + torch download).

You need a Windows 11 machine with an NVIDIA GPU. (Win 10 also
works but needs an X server — covered at the end.)

---

## 1. Make sure WSL2 is installed

Open **PowerShell as Administrator** on the Windows side and run:

```powershell
wsl --install -d Ubuntu-22.04
wsl --update
```

If WSL was already installed, only `wsl --update` is needed.

Reboot if prompted. Then open **Ubuntu** from the Start menu and
sign in to the Linux user you just created.

> Already have WSL with a different distro? You can use Ubuntu-24.04
> or Debian instead — anything based on Ubuntu 22.04+ will work.
> Avoid Alpine; the wheels we depend on don't ship for musl libc.

---

## 2. Verify the GPU is visible inside WSL

In the **Ubuntu** shell, run:

```bash
nvidia-smi
```

You should see a table showing your GPU and a "CUDA Version" entry
in the top right.

**If it says "command not found"** → your Windows NVIDIA driver is
either too old or wasn't installed with WSL support. Download the
latest **Game Ready Driver** for your card from
<https://www.nvidia.com/Download> on the Windows side, install it,
reboot, then run `wsl --shutdown` from PowerShell and reopen
Ubuntu. Try `nvidia-smi` again.

**If you don't have an NVIDIA GPU** — the setup script falls back
to CPU-only mode automatically. The app still works; just slower.

---

## 3. Get the repo into WSL

Pick **one** of the two options. Both end up with `~/Council-Demo/`
on the Linux side and you `cd` into it.

### Option A — fresh clone (cleanest, recommended)

```bash
cd ~
git clone https://github.com/Infernoplaystuf/Council.git Council-Demo
cd Council-Demo
git checkout Work-Build-App
```

### Option B — symlink an existing Windows copy

If you already have the repo at e.g.
`C:\Users\you\Downloads\Council-Demo\`:

```bash
ln -s "/mnt/c/Users/$USER/Downloads/Council-Demo/Council-Demo" ~/Council-Demo
cd ~/Council-Demo
```

This works but the **first vault index rebuild will be slower** —
the WSL filesystem bridge to `/mnt/c/...` is about 10× slower than
the native Linux side for many-small-file walks. If your vault has
1000s of files, prefer Option A and copy the vault contents over
once instead of leaving them on the Windows drive.

---

## 4. Run the setup script

```bash
./setup-wsl.sh
```

This does everything: apt installs (build tools, audio libraries),
Miniforge (conda), the `council` env, torch + llama-cpp-python with
the CUDA wheels matched to your GPU, and the rest of the Python
dependencies. **Takes about 15-25 minutes** the first time — most
of it is downloading the torch wheel (~2 GB).

The script is idempotent — if it fails partway through, just run
it again and it'll skip the parts that already worked.

If `./setup-wsl.sh` fails with **"Permission denied"**:

```bash
chmod +x setup-wsl.sh run-wsl.sh
./setup-wsl.sh
```

(This shouldn't normally happen — the scripts are committed with
the executable bit set — but Windows-side clones occasionally
strip permissions.)

### What the setup picks for you

The script reads `nvidia-smi`'s reported max CUDA version and picks
the matching wheel tier automatically:

| Your driver shows  | Script picks |
|--------------------|--------------|
| CUDA ≥ 12.8        | cu128 (RTX 50-series Blackwell) |
| CUDA 12.4 – 12.7   | cu124 (RTX 40-series / Ada)     |
| CUDA 12.0 – 12.3   | cu121 (RTX 20/30-series, A-series) |
| No NVIDIA GPU      | cpu  (works, just slower)       |

If you want to override the auto-pick, set `COUNCIL_CUDA_TIER` before
running:

```bash
COUNCIL_CUDA_TIER=cu121 ./setup-wsl.sh   # force older wheels
```

### Verification at the end

The last step of the script prints something like:

```
  CUDA available: True
  device:         NVIDIA GeForce RTX 4080 SUPER
  torch.cuda:     12.4
  all imports OK
```

If `CUDA available: False` appears on a machine that *has* a GPU,
your Windows driver is older than the CUDA wheel needs. Update the
driver via the link in Step 2, run `wsl --shutdown` from
PowerShell, reopen Ubuntu, and re-run `./setup-wsl.sh`.

---

## 5. Get a GGUF model onto the machine

The app needs a `.gguf` model file. If you don't have one, download
one from Hugging Face. Recommendations by GPU memory:

| VRAM         | Model file                                                | Size  |
|--------------|-----------------------------------------------------------|-------|
| 4-6 GB       | `bartowski/Llama-3.2-3B-Instruct-GGUF` (Q4_K_M)            | ~2 GB |
| 8 GB         | `bartowski/granite-3.0-8b-instruct-GGUF` (Q4_K_M)          | ~5 GB |
| 16 GB (4080) | `bartowski/phi-4-GGUF` (Q4_K_M)                            | ~9 GB |
| 24 GB+       | `bartowski/Qwen2.5-32B-Instruct-GGUF` (Q4_K_M)             | ~20 GB |

Easiest way to download once Hugging Face CLI is installed (it is —
`setup-wsl.sh` pulled `huggingface_hub`):

```bash
mkdir -p ~/models
cd ~/models
huggingface-cli download bartowski/phi-4-GGUF \
    phi-4-Q4_K_M.gguf --local-dir .
cd ~/Council-Demo
```

Or copy a model you already have on the Windows side:

```bash
mkdir -p ~/models
cp /mnt/c/Users/$USER/path/to/your-model.gguf ~/models/
```

> **Why copy instead of leaving it on /mnt/c?** Loading a 9 GB file
> across the WSL↔Windows bridge adds ~10 seconds to every launch.
> A native-Linux-side copy loads in 1-2 seconds.

---

## 6. Run the app

```bash
./run-wsl.sh
```

That's it. The script:

- Activates the conda env.
- Auto-finds your `.gguf` model in `~/models/`, `~/Downloads/`,
  `./models/`, or `/mnt/c/Users/<you>/models/`.
- Sets sensible WSL defaults (UI scale 1.5× because WSLg reports
  96 DPI even on 4K displays, GPU offload on, ladder-debug logging
  on so you can see the n_ctx choice).
- Launches the GUI.

On **Windows 11** the Tkinter window opens through WSLg
automatically. On **Windows 10** see Step 9 below.

### Pinning a specific model or settings

The script honors any env var you've already exported. Run-time
overrides:

```bash
# pick a specific GGUF
COUNCIL_GGUF_PATH=~/models/granite-3.0-8b.gguf ./run-wsl.sh

# force CPU only (sanity check when GPU launch crashes)
COUNCIL_GGUF_GPU_LAYERS=0 ./run-wsl.sh

# bigger or smaller text
COUNCIL_UI_SCALE=1.8 ./run-wsl.sh
```

Make any of those stick across launches by adding to `~/.bashrc`:

```bash
echo 'export COUNCIL_GGUF_PATH=~/models/phi-4-Q4_K_M.gguf' >> ~/.bashrc
echo 'export COUNCIL_UI_SCALE=1.7'                         >> ~/.bashrc
source ~/.bashrc
```

---

## 7. Confirm the GPU is actually being used

On launch, look at the terminal output for these lines:

```
[GGUF] n_ctx = 32,768  (source: VRAM-aware ...)
[CPU] features include avx=True avx2=True f16c=True ...
[GGUF] Loading phi-4-Q4_K_M.gguf (n_ctx=32768, n_threads=8, n_gpu_layers=99  GPU=NVIDIA GeForce RTX 4080 SUPER (16.0 GB VRAM))
```

The Tkinter window title bar also shows the chosen `n_ctx`:

```
Data's Inferno  ·  n_ctx=32,768
```

If you see `n_gpu_layers=0` and `GPU=none detected` on a machine
that has a GPU, the CUDA wheels didn't take. Re-run setup with the
right tier (Step 4) or force the manual reinstall command from
the installs.txt block.

---

## 8. Common failures and fixes

### "Illegal instruction (core dumped)" right at model load

The prebuilt llama-cpp-python wheel needs AVX2 + F16C from your CPU.
A few configurations (old Xeons, some Hyper-V nested VMs, certain
corporate Windows installs) end up without one of those flags
exposed inside WSL.

The new launcher catches this automatically — when the app exits
with SIGILL (code 132), it retries once on CPU and points you at
the real fix. If the CPU retry works, you've confirmed it's a
GPU-path / CUDA-wheel issue rather than a CPU instruction issue.

For the actual fix — rebuilding llama-cpp-python without AVX2 —
see `installs.txt`, the "Illegal instruction (core dumped)" block.

### Tkinter window never appears (Windows 11)

WSLg might be disabled. From a Windows PowerShell:

```powershell
wsl --version
```

You need WSL version 1.0.0 or newer. If older:

```powershell
wsl --update
wsl --shutdown
```

Then reopen Ubuntu and try `./run-wsl.sh` again.

### Tkinter window never appears (Windows 10)

Windows 10 doesn't have WSLg. You need an X server on the Windows
side.

1. Install **VcXsrv** (free): <https://sourceforge.net/projects/vcxsrv/>
2. Start `XLaunch`. Pick "Multiple windows", "Display number 0",
   "Start no client", and **check** "Disable access control".
3. The `run-wsl.sh` script auto-detects Win 10 and sets `DISPLAY`
   to the right value, so you should be able to just launch:

   ```bash
   ./run-wsl.sh
   ```

If the window still doesn't appear, manually export `DISPLAY` and
retry:

```bash
export DISPLAY=$(grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'):0
./run-wsl.sh
```

### "torch.cuda.is_available() = False" but nvidia-smi works

Windows host NVIDIA driver is older than the CUDA wheel needs:

| Wheel    | Needs Windows driver |
|----------|----------------------|
| cu121    | 525+                 |
| cu124    | 545+                 |
| cu128    | 570+                 |

Update the driver, `wsl --shutdown` from PowerShell, reopen Ubuntu.
You do **not** need to re-run `./setup-wsl.sh` — only the driver
changed.

### "OSError: libcuda.so.1: cannot open shared object file"

Your WSL distro is older than 22.04 and doesn't expose libcuda
cleanly via the driver passthrough. Either upgrade in-place
(`sudo do-release-upgrade`) or set up a fresh Ubuntu-24.04
distro side-by-side:

```powershell
wsl --install -d Ubuntu-24.04
```

### "paramiko / cryptography build fails"

Missing build tools — `./setup-wsl.sh` Step 1 didn't run cleanly.
Re-run:

```bash
sudo apt install -y build-essential
./setup-wsl.sh
```

### App starts but the vault is empty

The app shows your `vault/` folder contents. By default it's
`~/Council-Demo/vault/`. Either drop files into `~/Council-Demo/vault/data_in/`
on the Linux side, or symlink an existing Windows-side vault:

```bash
mv ~/Council-Demo/vault ~/Council-Demo/vault.empty
ln -s "/mnt/c/Users/$USER/Documents/MyVault" ~/Council-Demo/vault
```

The first index rebuild over the `/mnt/c` bridge will be slow
(see the timing warning in Step 3). After that it's cached.

---

## 9. Where to go from here

- **The app's tabs and features** — see the rest of `README.md`.
- **Per-CUDA-tier install detail** — `installs.txt`, Section F
  (WSL) and Sections B/C/D (Windows native — same wheels).
- **Tuning n_ctx for your VRAM** — set `COUNCIL_GGUF_N_CTX_DEBUG=1`
  on a launch and look at the n_ctx ladder trace. The chosen
  value plus the considered-and-rejected ones tell you what your
  ceiling is.
- **Headed deeper into the data analyst features** — see the
  "Analyst" section in `README.md`. Same on WSL as on Windows.

If something breaks that isn't covered above, the engine's startup
log + the n_ctx ladder dump + the CPU feature line together cover
almost every failure mode. Capture them with:

```bash
COUNCIL_GGUF_N_CTX_DEBUG=1 ./run-wsl.sh 2>&1 | tee ~/council-launch.log
```

— then look at `~/council-launch.log` for the diagnostic.
