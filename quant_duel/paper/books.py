"""What a book trades with: model, features, threshold, training window.

Three books run side by side on every node:

* ``live`` — the strategy the tuner may change (phase 4);
* ``control`` — the starting settings, frozen when the ledger is created,
  never tuned and never given news (identical on both nodes);
* ``spy`` — buy-and-hold of the benchmark.

A ``BookConfig`` is plain data, stored as canonical JSON, so two nodes that
start from the same ``config.yaml`` hold byte-identical book settings.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Sequence

MODEL_BOOKS = ("live", "control")
BENCHMARK_BOOK = "spy"
BOOKS = MODEL_BOOKS + (BENCHMARK_BOOK,)

# Feature column prefix per on/off group (spy_ is checked before ret_).
GROUPS = (("spy_context", "spy_"), ("returns", "ret_"),
          ("ma_ratio", "ma_ratio_"), ("volume_z", "volume_z"),
          ("volatility", "vol_"), ("rsi", "rsi"), ("day_of_week", "dow_"))


def group_of(column: str) -> str:
    for group, prefix in GROUPS:
        if column.startswith(prefix):
            return group
    raise KeyError(f"feature {column!r} belongs to no group")


@dataclass(frozen=True)
class BookConfig:
    model_kind: str
    model_params: Dict[str, Any]
    features_enabled: Dict[str, bool]
    threshold: float
    train_days: int
    refit_days: int
    sentiment_weight: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg) -> "BookConfig":
        kind = cfg["model"]["kind"]
        bt = cfg["backtest"]
        return cls(model_kind=kind,
                   model_params=dict(cfg["model"]["params"].get(kind) or {}),
                   features_enabled=dict(cfg["features"]["enabled"]),
                   threshold=float(bt["threshold"]),
                   train_days=int(bt["train_days"]),
                   refit_days=int(bt["test_days"]))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BookConfig":
        return cls(**d)

    def to_json(self) -> str:
        return canonical(self.to_dict())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def features(self, columns: Sequence[str]) -> List[str]:
        """The feature columns of the shared table this book uses."""
        return [c for c in columns
                if self.features_enabled.get(group_of(c), True)]


def canonical(obj: Any) -> str:
    """JSON with sorted keys and no whitespace: same data, same bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
