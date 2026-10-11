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


class Boosting(Model):
    """Gradient-boosted trees. LightGBM when it is installed, otherwise
    scikit-learn's HistGradientBoostingClassifier — the same settings drive
    both (names follow scikit-learn), and both are run single-threaded and
    deterministic so the same data and seed give the same predictions.

    ``backend`` is ``auto`` (LightGBM if importable), ``lightgbm`` or
    ``sklearn``. Which one actually ran is in ``self.backend`` — the two
    give close but not identical numbers, so a comparison between machines
    should check it matches."""

    name = "boosting"

    def __init__(self, max_depth: int = 3, learning_rate: float = 0.05,
                 max_iter: int = 200, min_samples_leaf: int = 200,
                 l2_regularization: float = 1.0, backend: str = "auto",
                 seed: int = 0):
        self.max_depth, self.learning_rate = max_depth, learning_rate
        self.max_iter, self.min_samples_leaf = max_iter, min_samples_leaf
        self.l2_regularization, self.seed = l2_regularization, seed
        self.backend = resolve_backend(backend)

    def _make(self):
        if self.backend == "lightgbm":
            import lightgbm as lgb
            return lgb.LGBMClassifier(
                max_depth=self.max_depth,
                num_leaves=min(2 ** self.max_depth, 31),
                learning_rate=self.learning_rate,
                n_estimators=self.max_iter,
                min_child_samples=self.min_samples_leaf,
                reg_lambda=self.l2_regularization,
                random_state=self.seed, n_jobs=1, deterministic=True,
                force_col_wise=True, verbose=-1)
        from sklearn.ensemble import HistGradientBoostingClassifier
        return HistGradientBoostingClassifier(
            max_depth=self.max_depth, learning_rate=self.learning_rate,
            max_iter=self.max_iter, min_samples_leaf=self.min_samples_leaf,
            l2_regularization=self.l2_regularization,
            early_stopping=False, random_state=self.seed)

    def fit(self, X, y):
        self.features = list(X.columns)
        self.est = self._make()
        self.est.fit(X.to_numpy(dtype=float), np.asarray(y).astype(int))
        return self

    def predict_proba(self, X):
        p = self.est.predict_proba(X.to_numpy(dtype=float))[:, 1]
        return np.clip(p, EPS, 1 - EPS)

    def importances(self) -> pd.Series:
        """Split-count importance per feature (LightGBM); for scikit-learn,
        which has none built in, the number of splits on each feature."""
        if self.backend == "lightgbm":
            imp = np.asarray(self.est.feature_importances_, dtype=float)
        else:
            imp = np.zeros(len(self.features))
            for stage in self.est._predictors:
                for tree in stage:
                    nodes = tree.nodes
                    inner = nodes["is_leaf"] == 0
                    np.add.at(imp, nodes["feature_idx"][inner], 1)
        return pd.Series(imp, index=self.features).sort_values(
            ascending=False)


def lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return False
    return True


def resolve_backend(backend: str) -> str:
    if backend not in ("auto", "lightgbm", "sklearn"):
        raise ValueError(f"unknown boosting backend {backend!r}")
    if backend == "auto":
        return "lightgbm" if lightgbm_available() else "sklearn"
    if backend == "lightgbm" and not lightgbm_available():
        raise ValueError("boosting backend 'lightgbm' asked for but "
                         "lightgbm is not installed")
    return backend


def make_model(kind: str, params: Dict, seed: int = 0) -> Model:
    if kind == "always_up":
        return AlwaysUp()
    if kind == "persistence":
        return Persistence()
    if kind == "logistic":
        return Logistic(seed=seed, **params.get("logistic", {}))
    if kind == "boosting":
        return Boosting(seed=seed, **params.get("boosting", {}))
    raise ValueError(f"unknown model kind {kind!r}")


KINDS: Sequence[str] = ("always_up", "persistence", "logistic", "boosting")


BASELINES: Sequence[str] = ("always_up", "persistence")
