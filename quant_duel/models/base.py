"""Every model has the same interface: fit on a training fold, then give
P(up) for each row. Anything learned — scalers included — is learned in
``fit`` from the training fold only."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Sequence

import numpy as np
import pandas as pd

EPS = 1e-4


class Model(ABC):
    name = "model"

    @abstractmethod
    def fit(self, X: pd.DataFrame, y: np.ndarray) -> "Model":
        ...

    @abstractmethod
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """P(up) per row, clipped to [EPS, 1 - EPS]."""


class AlwaysUp(Model):
    """Always long. Its probability is the training fold's up-rate, so it
    has an honest log loss rather than an infinite one."""

    name = "always_up"

    def fit(self, X, y):
        self.p = float(np.clip(np.mean(y), EPS, 1 - EPS))
        return self

    def predict_proba(self, X):
        return np.full(len(X), max(self.p, 0.5 + EPS))


class Persistence(Model):
    """Tomorrow goes the way today went: P(up) is the training up-rate
    after an up day or after a down day."""

    name = "persistence"

    def fit(self, X, y):
        up = X["ret_1"].to_numpy() > 0
        y = np.asarray(y)
        self.p_up = float(np.clip(y[up].mean() if up.any() else 0.5, EPS, 1 - EPS))
        self.p_down = float(np.clip(y[~up].mean() if (~up).any() else 0.5,
                                    EPS, 1 - EPS))
        return self

    def predict_proba(self, X):
        up = X["ret_1"].to_numpy() > 0
        return np.where(up, self.p_up, self.p_down)


class Logistic(Model):
    """Standardised logistic regression; the scaler is part of the fitted
    pipeline, so it sees the training fold only."""

    name = "logistic"

    def __init__(self, C: float = 0.1, max_iter: int = 500, seed: int = 0):
        self.C, self.max_iter, self.seed = C, max_iter, seed

    def fit(self, X, y):
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        self.pipe = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=self.C, max_iter=self.max_iter,
                               random_state=self.seed))
        self.pipe.fit(X.to_numpy(dtype=float), np.asarray(y).astype(int))
        return self

    def predict_proba(self, X):
        p = self.pipe.predict_proba(X.to_numpy(dtype=float))[:, 1]
        return np.clip(p, EPS, 1 - EPS)


def make_model(kind: str, params: Dict, seed: int = 0) -> Model:
    if kind == "always_up":
        return AlwaysUp()
    if kind == "persistence":
        return Persistence()
    if kind == "logistic":
        return Logistic(seed=seed, **params.get("logistic", {}))
    raise ValueError(f"unknown model kind {kind!r} (boosting arrives in "
                     "phase 2)")


BASELINES: Sequence[str] = ("always_up", "persistence")
