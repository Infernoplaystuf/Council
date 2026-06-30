"""
question_history.py — a small per-vault log of the questions asked in the
Council tab, so the user can browse past questions and re-ask one with a
click (cheap + correct now that derived results are reused).

Storage: <vault>/question_history.json — a flat list of {"q", "ts"} in the
order asked. Reads/writes are best-effort and never raise into the UI. The
log de-duplicates an immediately repeated question and is capped so it can't
grow without bound.

Pure + dependency-light (json / pathlib / time), so it's unit-testable with
an explicit timestamp.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_STORE_NAME = "question_history.json"
_LOCK = threading.Lock()
_CAP = 500


def _vault_root(vault_dir: Optional[Any] = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).expanduser().resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


class QuestionHistory:
    """Per-vault store of Council questions (question_history.json)."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self.path = _vault_root(vault_dir) / _STORE_NAME

    def _read(self) -> List[Dict[str, Any]]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return d if isinstance(d, list) else []
        except Exception:
            pass
        return []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def add(self, question: str, ts: Optional[float] = None) -> None:
        """Append a question. Skips blanks and an immediate duplicate of the
        previous entry; caps the log length."""
        q = (question or "").strip()
        if not q:
            return
        with _LOCK:
            items = self._read()
            if items and str(items[-1].get("q", "")).strip() == q:
                return
            items.append({"q": q, "ts": ts if ts is not None else time.time()})
            if len(items) > _CAP:
                items = items[-_CAP:]
            self._write(items)

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Most-recent-first list of {"q", "ts"} (up to ``limit``)."""
        items = self._read()
        items.reverse()
        return items[:max(0, int(limit))]

    def all(self) -> List[Dict[str, Any]]:
        return self._read()

    def clear(self) -> None:
        with _LOCK:
            self._write([])
