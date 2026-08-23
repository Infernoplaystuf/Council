"""
art_reference.py — local image generation for REFERENCE ONLY.

Scope, deliberately narrow
--------------------------
This module exists so the artist can look at a generated mood/reference
image while hand-painting in the Pixel Art tab. It is **not** an asset
pipeline:

  * Output lands in ``vault/art_reference/`` — never in a game project,
    never in ``vault/sprites/``, never in a built project's assets.
  * Nothing here writes into a Godot project, and the Pixel Art tab
    deliberately offers no "load this into the canvas" action for these
    images (the procedural generator in ``pixel_gen`` is the thing you
    are meant to draw over — it is pure maths with no training-data
    provenance).
  * Every file is written with a ``REFERENCE_`` name prefix so a
    generated image can never be mistaken for hand-made work later.

That separation is the whole point: it keeps generated imagery on the
reference board and hand-made pixels in the game.

Backends are local only — ComfyUI (:8188) or A1111 (:7860), whichever
is running, via ``image_engine``. If neither is up, ``generate``
returns None and the caller shows a "start a local backend" message.
Nothing is ever sent to a hosted service.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Optional

try:
    import image_engine as _ie
    ENGINE_OK = True
except Exception:                                    # pragma: no cover
    _ie = None
    ENGINE_OK = False


#: Subfolder under the vault where reference images live.
REFERENCE_SUBDIR = "art_reference"
#: Filename prefix — makes generated images self-identifying on disk.
REFERENCE_PREFIX = "REFERENCE_"

# Pixel-art shaping. Diffusion models need to be pushed hard toward
# sprite-like output or they return smooth digital paintings, which are
# useless as pixel reference.
_STYLE_SUFFIX = (
    ", pixel art, 16-bit sprite, limited palette, crisp hard edges, "
    "clean pixel clusters, orthographic, game asset reference sheet, "
    "flat lighting, no anti-aliasing"
)
_NEGATIVE = (
    "photorealistic, 3d render, blurry, soft gradients, anti-aliased, "
    "smooth shading, watermark, text, signature, jpeg artifacts"
)


def shape_prompt(subject: str) -> str:
    """Turn a plain subject into a pixel-art-leaning image prompt."""
    s = (subject or "").strip()
    for prefix in ("pixel art of", "pixel art:", "sprite of", "draw"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):].strip(" :")
            break
    if len(s) > 300:
        s = s[:300]
    return s + _STYLE_SUFFIX


class ReferenceGenerator:
    """Generate reference images into ``vault/art_reference/``.

    Usage::

        gen = ReferenceGenerator(VAULT_DIR)
        if gen.available:
            path = gen.generate("a mossy stone gatehouse")
    """

    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self.out_dir = self.vault_dir / REFERENCE_SUBDIR
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._gen = None
        if ENGINE_OK:
            try:
                # Reuse image_engine's backend detection + HTTP plumbing,
                # then redirect its output at our reference folder.
                self._gen = _ie.ThumbnailGenerator(self.vault_dir)
                self._gen.images_dir = self.out_dir
            except Exception:
                self._gen = None

    # ── Backend status ────────────────────────────────────────

    @property
    def available(self) -> bool:
        if self._gen is None:
            return False
        try:
            return self._gen.available
        except Exception:
            return False

    @property
    def backend_label(self) -> str:
        if self._gen is None:
            return "image_engine unavailable"
        try:
            return self._gen.backend_label
        except Exception:
            return "not available"

    def status_text(self) -> str:
        """A line the UI can show verbatim."""
        if not ENGINE_OK:
            return "Reference generation unavailable (image_engine missing)."
        if self.available:
            return f"Local backend: {self.backend_label} — reference only."
        return ("No local image backend running. Start ComfyUI "
                "(localhost:8188) or A1111 (localhost:7860) to generate "
                "reference images. Nothing is ever sent off this machine.")

    # ── Generation ────────────────────────────────────────────

    def generate(self, subject: str, *, size: int = 512) -> Optional[Path]:
        """Generate one reference image. Returns its path, or None when
        no local backend is running or generation failed."""
        if self._gen is None or not subject.strip():
            return None
        run_id = REFERENCE_PREFIX + uuid.uuid4().hex[:8]
        try:
            return self._gen.generate(
                shape_prompt(subject), run_id,
                width=int(size), height=int(size),
            )
        except Exception:
            return None

    def list_references(self) -> List[Path]:
        """Every reference image on disk, newest first."""
        try:
            files = [p for p in self.out_dir.glob("*.png") if p.is_file()]
        except Exception:
            return []
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files
