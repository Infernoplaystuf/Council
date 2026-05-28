# ============================================================
# image_engine.py  —  Local thumbnail image generation
# ============================================================
# Supports two fully-free, fully-local backends:
#   1. ComfyUI  — localhost:8188  (Flux.1-schnell recommended)
#   2. A1111    — localhost:7860  (SDXL / SD 1.5)
#
# Auto-detects whichever is running. If neither is available
# the generator returns None and the council continues with
# text-only thumbnail concepts.
#
# Images are saved to vault/idea_images/ which is covered by
# .gitignore — they never leave the local machine.
# ============================================================

from __future__ import annotations

import base64
import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


# ── Prompt shaping ──────────────────────────────────────────────────────

_QUALITY_SUFFIX = (
    ", YouTube thumbnail, high quality, sharp focus, "
    "vibrant saturated colors, professional photo, eye-catching, "
    "cinematic lighting, 4k"
)
_NEGATIVE_PROMPT = (
    "text, watermark, logo, signature, blurry, low quality, "
    "pixelated, distorted face, extra limbs, bad anatomy, "
    "multiple people unless intentional, boring, flat lighting"
)

THUMBNAIL_WIDTH  = 1280
THUMBNAIL_HEIGHT = 720


def enhance_prompt(concept: str) -> str:
    """
    Turn a pitcher's thumbnail concept description into an image-gen prompt.
    Strips any meta-commentary and appends quality modifiers.
    """
    c = concept.strip()
    for prefix in (
        "thumbnail concept:", "thumbnail:", "thumbnail —", "thumbnail -",
        "thumbnail image:", "concept:",
    ):
        if c.lower().startswith(prefix):
            c = c[len(prefix):].strip()
            break
    # Truncate if excessively long — most image models ignore tail tokens
    if len(c) > 400:
        c = c[:400]
    return c + _QUALITY_SUFFIX


# ── HTTP helpers ────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 4) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _post(url: str, payload: dict, timeout: int = 180) -> Optional[dict]:
    try:
        body = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ── Backend: ComfyUI ────────────────────────────────────────────────────

COMFYUI_HOST = "http://localhost:8188"

# Flux.1-schnell workflow for ComfyUI.
# Expected model files (drop into ComfyUI/models/):
#   unet/  → flux1-schnell.safetensors
#   clip/  → t5xxl_fp8_e4m3fn.safetensors, clip_l.safetensors
#   vae/   → ae.safetensors
# All four are free downloads from black-forest-labs on Hugging Face.
def _flux_workflow(prompt: str, width: int, height: int, steps: int, seed: int) -> dict:
    return {
        "1":  {"class_type": "UNETLoader",
               "inputs": {"unet_name": "flux1-schnell.safetensors",
                          "weight_dtype": "fp8_e4m3fn"}},
        "2":  {"class_type": "DualCLIPLoader",
               "inputs": {"clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
                          "clip_name2": "clip_l.safetensors",
                          "type": "flux"}},
        "3":  {"class_type": "VAELoader",
               "inputs": {"vae_name": "ae.safetensors"}},
        "4":  {"class_type": "CLIPTextEncode",
               "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5":  {"class_type": "EmptyLatentImage",
               "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6":  {"class_type": "KSamplerSelect",
               "inputs": {"sampler_name": "euler"}},
        "7":  {"class_type": "BasicScheduler",
               "inputs": {"scheduler": "simple", "steps": steps,
                          "denoise": 1.0, "model": ["1", 0]}},
        "8":  {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["9", 0], "guider": ["10", 0],
                          "sampler": ["6", 0], "sigmas": ["7", 0],
                          "latent_image": ["5", 0]}},
        "9":  {"class_type": "RandomNoise",
               "inputs": {"noise_seed": seed}},
        "10": {"class_type": "BasicGuider",
               "inputs": {"model": ["1", 0], "conditioning": ["4", 0]}},
        "11": {"class_type": "VAEDecode",
               "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "12": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "council_thumb",
                          "images": ["11", 0]}},
    }


def _comfyui_generate(
    prompt: str, dest: Path,
    width: int, height: int, steps: int,
) -> Optional[Path]:
    seed     = int(uuid.uuid4().int % (2 ** 32))
    workflow = _flux_workflow(prompt, width, height, steps, seed)

    resp = _post(f"{COMFYUI_HOST}/prompt", {"prompt": workflow}, timeout=15)
    if not resp or "prompt_id" not in resp:
        return None
    prompt_id = resp["prompt_id"]

    # Poll history until the job completes (up to 4 minutes)
    for _ in range(240):
        time.sleep(1)
        raw = _get(f"{COMFYUI_HOST}/history/{prompt_id}", timeout=5)
        if not raw:
            continue
        data = json.loads(raw)
        if prompt_id not in data:
            continue
        for node_out in data[prompt_id].get("outputs", {}).values():
            for img in node_out.get("images", []):
                url = (
                    f"{COMFYUI_HOST}/view"
                    f"?filename={img['filename']}"
                    f"&subfolder={img.get('subfolder', '')}"
                    f"&type=output"
                )
                img_bytes = _get(url, timeout=15)
                if img_bytes:
                    dest.write_bytes(img_bytes)
                    return dest
    return None


# ── Backend: Automatic1111 ──────────────────────────────────────────────

A1111_HOST = "http://localhost:7860"


def _a1111_generate(
    prompt: str, dest: Path,
    width: int, height: int, steps: int,
) -> Optional[Path]:
    payload = {
        "prompt":          prompt,
        "negative_prompt": _NEGATIVE_PROMPT,
        "width":           width,
        "height":          height,
        "steps":           steps,
        "cfg_scale":       7.0,
        "sampler_name":    "DPM++ 2M Karras",
    }
    resp = _post(f"{A1111_HOST}/sdapi/v1/txt2img", payload, timeout=180)
    if not resp or not resp.get("images"):
        return None
    dest.write_bytes(base64.b64decode(resp["images"][0]))
    return dest


# ── ThumbnailGenerator ──────────────────────────────────────────────────

class ThumbnailGenerator:
    """
    Generates 1280×720 thumbnail images from a pitcher's thumbnail concept.

    Usage:
        gen = ThumbnailGenerator(vault_dir)
        path = gen.generate(concept_text, idea_id)   # returns Path or None

    Auto-detects ComfyUI (preferred) or A1111. Returns None immediately
    if neither is reachable so the ideation loop is never blocked.
    """

    # Steps — Flux needs only 4; A1111/SDXL needs ~25
    _FLUX_STEPS  = 4
    _A1111_STEPS = 25

    def __init__(self, vault_dir: Path):
        self.vault_dir  = Path(vault_dir)
        self.images_dir = self.vault_dir / "idea_images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._backend:    Optional[str] = None
        self._last_probe: float         = 0.0

    # ── Backend detection ─────────────────────────────────────────

    def probe(self, force: bool = False) -> Optional[str]:
        """
        Return 'comfyui', 'a1111', or None.
        Re-probes at most once every 30 s unless force=True.
        """
        if not force and time.monotonic() - self._last_probe < 30:
            return self._backend
        self._last_probe = time.monotonic()
        if _get(f"{COMFYUI_HOST}/system_stats", timeout=2):
            self._backend = "comfyui"
        elif _get(f"{A1111_HOST}/sdapi/v1/sd-models", timeout=2):
            self._backend = "a1111"
        else:
            self._backend = None
        return self._backend

    @property
    def available(self) -> bool:
        return self.probe() is not None

    @property
    def backend_label(self) -> str:
        b = self.probe()
        return {"comfyui": "ComfyUI (Flux)", "a1111": "A1111 (SDXL/SD)"}.get(
            b or "", "not available")

    # ── Generation ────────────────────────────────────────────────

    def generate(
        self,
        concept:  str,
        idea_id:  str,
        width:    int = THUMBNAIL_WIDTH,
        height:   int = THUMBNAIL_HEIGHT,
    ) -> Optional[Path]:
        """
        Generate a thumbnail image for the given concept.
        Saves to vault/idea_images/<idea_id>_<hex>.png.
        Returns the Path on success, None if generation fails or no backend.
        """
        backend = self.probe()
        if not backend or not concept.strip():
            return None

        prompt = enhance_prompt(concept)
        dest   = self.images_dir / f"{idea_id}_{uuid.uuid4().hex[:6]}.png"

        try:
            if backend == "comfyui":
                return _comfyui_generate(
                    prompt, dest, width, height, self._FLUX_STEPS)
            else:
                return _a1111_generate(
                    prompt, dest, width, height, self._A1111_STEPS)
        except Exception:
            return None

    def delete_for_idea(self, idea_id: str):
        """Remove all generated images for a given idea ID."""
        for f in self.images_dir.glob(f"{idea_id}_*.png"):
            try:
                f.unlink()
            except Exception:
                pass
