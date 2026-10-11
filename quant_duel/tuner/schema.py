"""What the tuner may change, and how far.

A proposal is ``{"changes": {...}, "reason": "..."}`` where ``changes``
may hold only:

* ``threshold`` — the long/flat P(up) cut-off;
* ``train_days`` — the training window (trading days, integer);
* ``model_params`` — hyperparameters of the CURRENT model kind;
* ``features_enabled`` — on/off per feature group;
* ``sentiment_weight`` — node A (news) only.

Every number has bounds in ``config.yaml`` (``tuner.bounds``), identical
for both nodes. Anything else — an unknown key, a wrong type, a value out
of bounds, a string where a number belongs, too many changes at once, or
no real change — rejects the whole proposal with the reasons listed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

from ..paper.books import GROUPS, BookConfig

INT_PARAMS = {"train_days", "max_depth", "max_iter", "min_samples_leaf"}
TOP_KEYS = {"threshold", "train_days", "model_params", "features_enabled",
            "sentiment_weight"}


class ProposalError(ValueError):
    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Checked:
    """A proposal that passed the schema: the new settings and what moved."""

    book: BookConfig
    changed: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)
    reason: str = ""


def _number(name: str, v: Any, lo: float, hi: float, errors: List[str],
            integer: bool = False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        errors.append(f"{name} must be a number, got {type(v).__name__}")
        return None
    if not math.isfinite(v):
        errors.append(f"{name} must be finite")
        return None
    if integer:
        if float(v) != int(v):
            errors.append(f"{name} must be a whole number")
            return None
        v = int(v)
    else:
        v = float(v)
    if not lo <= v <= hi:
        errors.append(f"{name}={v} is outside [{lo}, {hi}]")
        return None
    return v


def describe(book: BookConfig, bounds: Mapping[str, Any],
             news: bool) -> Dict[str, Any]:
    """The schema as the LLM sees it (compact)."""
    out: Dict[str, Any] = {
        "threshold": {"min": bounds["threshold"][0],
                      "max": bounds["threshold"][1]},
        "train_days": {"min": bounds["train_days"][0],
                       "max": bounds["train_days"][1], "integer": True},
        "model_params": {k: {"min": lo, "max": hi,
                             **({"integer": True} if k in INT_PARAMS else {})}
                         for k, (lo, hi) in
                         bounds.get(book.model_kind, {}).items()},
        "features_enabled": {"groups": [g for g, _ in GROUPS],
                             "values": "true|false",
                             "min_on": bounds.get("min_features_on", 2)},
    }
    if news:
        out["sentiment_weight"] = {"min": bounds["sentiment_weight"][0],
                                   "max": bounds["sentiment_weight"][1]}
    return out


def check(proposal: Any, current: BookConfig, bounds: Mapping[str, Any],
          news: bool, max_changes: int = 3) -> Checked:
    """Validate ``proposal`` against ``current``; raise ``ProposalError``."""
    errors: List[str] = []
    if not isinstance(proposal, dict):
        raise ProposalError(["proposal must be a JSON object"])
    extra = set(proposal) - {"changes", "reason"}
    if extra:
        errors.append(f"unknown top-level keys {sorted(extra)}")
    changes = proposal.get("changes")
    reason = proposal.get("reason", "")
    if not isinstance(reason, str):
        errors.append("reason must be a string")
        reason = ""
    if not isinstance(changes, dict):
        raise ProposalError(errors + ["'changes' must be an object"])
    unknown = set(changes) - TOP_KEYS
    if unknown:
        errors.append(f"cannot change {sorted(unknown)}; allowed: "
                      f"{sorted(TOP_KEYS)}")
    new = current.to_dict()
    if "threshold" in changes:
        lo, hi = bounds["threshold"]
        v = _number("threshold", changes["threshold"], lo, hi, errors)
        if v is not None:
            new["threshold"] = v
    if "train_days" in changes:
        lo, hi = bounds["train_days"]
        v = _number("train_days", changes["train_days"], lo, hi, errors,
                    integer=True)
        if v is not None:
            new["train_days"] = v
    if "sentiment_weight" in changes:
        if not news:
            errors.append("sentiment_weight is only for the news node")
        else:
            lo, hi = bounds["sentiment_weight"]
            v = _number("sentiment_weight", changes["sentiment_weight"],
                        lo, hi, errors)
            if v is not None:
                new["sentiment_weight"] = v
    if "model_params" in changes:
        mp = changes["model_params"]
        allowed = bounds.get(current.model_kind, {})
        if not isinstance(mp, dict):
            errors.append("model_params must be an object")
        else:
            params = dict(new["model_params"])
            for k, v in mp.items():
                if k not in allowed:
                    errors.append(f"model_params.{k} is not tunable for "
                                  f"{current.model_kind} (allowed: "
                                  f"{sorted(allowed)})")
                    continue
                lo, hi = allowed[k]
                v = _number(f"model_params.{k}", v, lo, hi, errors,
                            integer=k in INT_PARAMS)
                if v is not None:
                    params[k] = v
            new["model_params"] = params
    if "features_enabled" in changes:
        fe = changes["features_enabled"]
        groups = {g for g, _ in GROUPS}
        if not isinstance(fe, dict):
            errors.append("features_enabled must be an object")
        else:
            flags = dict(new["features_enabled"])
            for k, v in fe.items():
                if k not in groups:
                    errors.append(f"unknown feature group {k!r}")
                elif not isinstance(v, bool):
                    errors.append(f"features_enabled.{k} must be true or "
                                  "false")
                else:
                    flags[k] = v
            on = sum(bool(flags.get(g, True)) for g in groups)
            if on < bounds.get("min_features_on", 2):
                errors.append(f"only {on} feature groups left on; at least "
                              f"{bounds.get('min_features_on', 2)} required")
            new["features_enabled"] = flags
    if errors:
        raise ProposalError(errors)
    book = BookConfig.from_dict(new)
    changed = _diff(current, book)
    if not changed:
        raise ProposalError(["the proposal changes nothing"])
    if len(changed) > max_changes:
        raise ProposalError([f"{len(changed)} settings changed; at most "
                             f"{max_changes} per proposal"])
    return Checked(book, changed, reason.strip()[:500])


def _diff(a: BookConfig, b: BookConfig) -> Dict[str, Tuple[Any, Any]]:
    out: Dict[str, Tuple[Any, Any]] = {}
    da, db = a.to_dict(), b.to_dict()
    for k in ("threshold", "train_days", "sentiment_weight"):
        if da[k] != db[k]:
            out[k] = (da[k], db[k])
    for sub in ("model_params", "features_enabled"):
        for k in sorted(set(da[sub]) | set(db[sub])):
            if da[sub].get(k) != db[sub].get(k):
                out[f"{sub}.{k}"] = (da[sub].get(k), db[sub].get(k))
    return out
