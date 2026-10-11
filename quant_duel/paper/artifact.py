"""Model artifacts without pickle.

A fitted model is recorded as a *spec*: the book settings, the feature
list, the seed, the training window and two hashes — one of the training
data and one of the model's predictions on it (the fingerprint). Every
model here is deterministic, so refitting from the spec on the same data
reproduces the model exactly, and the fingerprint proves it. Two nodes
that fit on the same shared prices produce byte-identical specs.

The training window matches the walk-forward backtest: the last
``train_days`` labelled days before the prediction day, minus one embargo
day (the newest labelled row, whose label is the prediction day's close).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from ..models.base import Model, make_model
from .books import BookConfig, canonical

EMBARGO = 1


class ArtifactMismatch(RuntimeError):
    """Refitting a spec did not reproduce the recorded model."""


def training_rows(table: pd.DataFrame, day: dt.date, train_days: int,
                  embargo: int = EMBARGO) -> pd.DataFrame:
    """Labelled rows usable to predict ``day`` (see module docstring)."""
    labelled = table[(table["date"] < day) & table["target"].notna()]
    dates = sorted(labelled["date"].unique())
    if embargo:
        dates = dates[:-embargo]
    keep = set(dates[-train_days:])
    return labelled[labelled["date"].isin(keep)]


def _hash_frame(X: pd.DataFrame, y: Optional[np.ndarray] = None) -> str:
    h = hashlib.sha256(",".join(X.columns).encode())
    h.update(np.round(X.to_numpy(dtype=float), 10).tobytes())
    if y is not None:
        h.update(np.asarray(y, dtype=float).tobytes())
    return h.hexdigest()[:16]


def _fingerprint(model: Model, X: pd.DataFrame) -> str:
    p = np.round(model.predict_proba(X), 8)
    return hashlib.sha256(p.tobytes()).hexdigest()[:16]


@dataclass(frozen=True)
class Spec:
    data: Dict[str, Any]

    @property
    def model_id(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return canonical(self.data)

    @property
    def book(self) -> BookConfig:
        return BookConfig.from_dict(self.data["book_config"])

    @property
    def first_day(self) -> dt.date:
        return dt.date.fromisoformat(self.data["first_day"])


def _fit(book: BookConfig, rows: pd.DataFrame, features, seed: int) -> Model:
    model = make_model(book.model_kind, {book.model_kind: book.model_params},
                       seed=seed)
    return model.fit(rows[features], rows["target"].to_numpy())


def fit(book: BookConfig, table: pd.DataFrame, day: dt.date, seed: int
        ) -> Tuple[Model, Spec]:
    """Fit ``book``'s model to predict from ``day`` on; returns the model
    and its spec. ``table`` must already be cut at ``day`` (``as_of``)."""
    from ..features.build import feature_columns, guard
    features = book.features(feature_columns(table))
    rows = training_rows(table, day, book.train_days)
    if rows["date"].nunique() < 2:
        raise ValueError(f"not enough labelled history before {day}")
    guard(rows, features)
    model = _fit(book, rows, features, seed)
    spec = Spec({
        "book_config": book.to_dict(), "features": features, "seed": seed,
        "first_day": day.isoformat(),
        "train_start": min(rows["date"]).isoformat(),
        "train_end": max(rows["date"]).isoformat(),
        "n_rows": int(len(rows)),
        "data_hash": _hash_frame(rows[features], rows["target"].to_numpy()),
        "backend": getattr(model, "backend", None),
        "fingerprint": _fingerprint(model, rows[features]),
    })
    return model, spec


def refit(spec: Spec, table: pd.DataFrame) -> Model:
    """Rebuild the model a spec describes; raise ``ArtifactMismatch`` if the
    training data or the resulting model differ from the recording (for
    example a dividend re-adjusted the price history)."""
    d = spec.data
    start = dt.date.fromisoformat(d["train_start"])
    end = dt.date.fromisoformat(d["train_end"])
    rows = table[(table["date"] >= start) & (table["date"] <= end)
                 & table["target"].notna()]
    feats = d["features"]
    if any(f not in rows.columns for f in feats):
        raise ArtifactMismatch("feature columns changed")
    if _hash_frame(rows[feats], rows["target"].to_numpy()) != d["data_hash"]:
        raise ArtifactMismatch("training data changed since the model was "
                               "fitted")
    model = _fit(spec.book, rows, feats, d["seed"])
    if getattr(model, "backend", None) != d["backend"]:
        raise ArtifactMismatch(f"backend {getattr(model, 'backend', None)} "
                               f"!= recorded {d['backend']}")
    if _fingerprint(model, rows[feats]) != d["fingerprint"]:
        raise ArtifactMismatch("refitted model does not reproduce the "
                               "recorded predictions")
    return model
