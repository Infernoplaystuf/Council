"""
image_index.py — CLIP-based semantic image search for the Council vault.

Uses sentence-transformers' `clip-ViT-B-32` to project both images and
short text queries into a shared 512-dim embedding space. Cosine
similarity ranks images by visual match for a text query like
"defect on solder joint" or "machine nameplate close-up".

Persists per-image vectors to ``vault/image_index.json`` keyed by
``(path, mtime, size)`` so an unchanged image isn't re-encoded on every
rebuild. First-time encode of ~1000 images on an RTX 4080 takes <30s.

Public API:

    idx = ImageIndex(vault_dir)
    idx.rebuild(on_progress=print)               # incremental encode
    hits = idx.search("control panel HMI screen", k=5)
    # hits = [(score: float, path: Path), ...]

Safe import: this module never raises at import time. If
sentence-transformers or PIL isn't installed, ``ImageIndex`` initialises
but ``rebuild()`` and ``search()`` no-op with a clear message.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


_INDEX_FILENAME = "image_index.json"
_MODEL_NAME = os.environ.get("COUNCIL_CLIP_MODEL", "clip-ViT-B-32")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _key(p: Path) -> Tuple[str, int, int]:
    try:
        st = p.stat()
        return (str(p), int(st.st_mtime), st.st_size)
    except Exception:
        return (str(p), 0, 0)


@dataclass
class _Vec:
    path: str
    mtime: int
    size: int
    vec: List[float]


class ImageIndex:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self.index_path = self.vault_dir / _INDEX_FILENAME
        self._model = None        # lazy
        self._vecs: Dict[str, _Vec] = {}
        self._available: bool = True
        self._unavailable_reason: str = ""
        self._load()

    # ── persistence ─────────────────────────────────────────
    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            blob = json.loads(self.index_path.read_text(encoding="utf-8"))
            for rec in blob.get("entries", []):
                v = _Vec(
                    path=rec["path"], mtime=int(rec.get("mtime", 0)),
                    size=int(rec.get("size", 0)), vec=list(rec.get("vec", [])),
                )
                self._vecs[v.path] = v
        except Exception:
            self._vecs = {}

    def _save(self) -> None:
        try:
            self.index_path.write_text(
                json.dumps({
                    "model": _MODEL_NAME,
                    "entries": [
                        {"path": v.path, "mtime": v.mtime,
                         "size": v.size, "vec": v.vec}
                        for v in self._vecs.values()
                    ],
                }),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── model lazy-load ─────────────────────────────────────
    def _get_model(self):
        if self._model is not None or not self._available:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:
            self._available = False
            self._unavailable_reason = f"sentence-transformers missing: {e!r}"
            return None
        try:
            # CPU is the safe default — CLIP is small (~150 MB) and on
            # a 5080 with cu124 PTX-fallback torch the GPU path can be
            # flaky. Operators who know they're on native sm_89 (4080)
            # can set COUNCIL_CLIP_DEVICE=cuda.
            device = os.environ.get("COUNCIL_CLIP_DEVICE", "cpu")
            self._model = SentenceTransformer(_MODEL_NAME, device=device)
        except Exception as e:
            self._available = False
            self._unavailable_reason = f"CLIP load failed: {e!r}"
            return None
        return self._model

    # ── walk vault for images ───────────────────────────────
    def _collect_images(self) -> List[Path]:
        out: List[Path] = []
        for p in self.vault_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            # Skip hidden dirs same as vault_rag
            if any(part in {".git", "__pycache__", ".chromadb", "node_modules"}
                   for part in p.parts):
                continue
            out.append(p)
        return sorted(out)

    # ── public API ──────────────────────────────────────────
    def rebuild(self, on_progress: Optional[Callable[[str], None]] = None) -> int:
        model = self._get_model()
        if model is None:
            if on_progress:
                on_progress(f"[image_index] disabled — {self._unavailable_reason}")
            return 0
        try:
            from PIL import Image as _PILImage
        except Exception as e:
            self._available = False
            self._unavailable_reason = f"Pillow missing: {e!r}"
            if on_progress:
                on_progress(f"[image_index] disabled — {self._unavailable_reason}")
            return 0

        images = self._collect_images()
        new_or_changed: List[Tuple[Path, Tuple[str, int, int]]] = []
        for p in images:
            k = _key(p)
            existing = self._vecs.get(str(p))
            if existing and (existing.mtime, existing.size) == (k[1], k[2]):
                continue
            new_or_changed.append((p, k))

        if not new_or_changed:
            if on_progress:
                on_progress(f"[image_index] {len(images)} images, all up to date")
            return 0

        if on_progress:
            on_progress(f"[image_index] encoding {len(new_or_changed)} image(s)...")

        # Batch encode (CLIP handles PIL images directly).
        BATCH = 8
        encoded = 0
        for i in range(0, len(new_or_changed), BATCH):
            batch = new_or_changed[i:i+BATCH]
            try:
                pil_imgs = []
                for p, _ in batch:
                    try:
                        pil_imgs.append(_PILImage.open(p).convert("RGB"))
                    except Exception:
                        pil_imgs.append(None)
                vecs = model.encode(
                    [im for im in pil_imgs if im is not None],
                    convert_to_numpy=True, show_progress_bar=False,
                )
                # Re-zip results back to original batch ordering
                v_iter = iter(vecs)
                for (p, k), im in zip(batch, pil_imgs):
                    if im is None:
                        continue
                    vec = next(v_iter).tolist()
                    self._vecs[str(p)] = _Vec(path=str(p), mtime=k[1],
                                              size=k[2], vec=vec)
                    encoded += 1
            except Exception as e:
                if on_progress:
                    on_progress(f"[image_index] batch failed: {e!r}")
                continue
        # Drop entries for images that no longer exist
        live = {str(p) for p in images}
        for key in list(self._vecs.keys()):
            if key not in live:
                self._vecs.pop(key, None)
        self._save()
        if on_progress:
            on_progress(f"[image_index] encoded {encoded} new, "
                        f"index now holds {len(self._vecs)}")
        return encoded

    def search(self, query: str, k: int = 5) -> List[Tuple[float, Path]]:
        model = self._get_model()
        if model is None or not self._vecs:
            return []
        try:
            import numpy as np
            qv = model.encode([query], convert_to_numpy=True,
                              show_progress_bar=False)[0]
            qn = qv / (np.linalg.norm(qv) + 1e-9)
            scored: List[Tuple[float, Path]] = []
            for v in self._vecs.values():
                vec = np.asarray(v.vec, dtype=np.float32)
                vn = vec / (np.linalg.norm(vec) + 1e-9)
                score = float(qn @ vn)
                scored.append((score, Path(v.path)))
            scored.sort(key=lambda r: -r[0])
            return scored[:k]
        except Exception:
            return []

    def status(self) -> Dict[str, object]:
        return {
            "available": bool(self._available),
            "reason": self._unavailable_reason,
            "indexed_images": len(self._vecs),
            "index_path": str(self.index_path),
            "model": _MODEL_NAME,
        }
