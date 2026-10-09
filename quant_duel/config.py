"""Configuration: the shared ``config.yaml`` plus one node file.

Both nodes must be identical except for news. ``load`` merges the node file
into the shared settings and refuses a node file that tries to change
anything else, so a fair comparison is enforced by code, not by care.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
NODE_KEYS = {"node_id", "news_enabled"}


class ConfigError(ValueError):
    """A configuration that would make the experiment unfair or invalid."""


@dataclass(frozen=True)
class Config:
    """The merged settings for one node."""

    raw: Dict[str, Any]
    node_id: str
    news_enabled: bool
    root: Path

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def tickers(self) -> list:
        return list(dict.fromkeys(self.raw["universe"]["tickers"]))

    @property
    def data_dir(self) -> Path:
        return self.root / self.raw["data"]["dir"]

    @property
    def shared_dir(self) -> Path:
        """Prices and features, shared by both nodes (same bytes for both)."""
        return self.data_dir / "shared"

    @property
    def node_dir(self) -> Path:
        """This node's own ledger, change log, models and headlines."""
        return self.data_dir / self.node_id


def _read(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a mapping")
    return data


def load(node: Optional[str] = None, *, root: Path = ROOT,
         config_file: str = "config.yaml") -> Config:
    """The settings for ``node`` ("A" or "B"); ``None`` reads no node file
    and returns the shared settings with news off (for tools that do not
    belong to a node, such as ``compare``)."""
    raw = _read(root / config_file)
    node_id, news = "shared", False
    if node is not None:
        node_raw = _read(root / "nodes" / f"{node}.yaml")
        extra = set(node_raw) - NODE_KEYS
        if extra:
            raise ConfigError(
                f"nodes/{node}.yaml may only set {sorted(NODE_KEYS)}; it also "
                f"sets {sorted(extra)} — every other setting must be shared")
        node_id = str(node_raw.get("node_id", node))
        if node_id != node:
            raise ConfigError(f"nodes/{node}.yaml says node_id {node_id!r}")
        news = bool(node_raw.get("news_enabled", False))
    return Config(copy.deepcopy(raw), node_id, news, root)


def check_identical(a: Config, b: Config) -> None:
    """Raise unless ``a`` and ``b`` differ only in node id and news."""
    if a.raw != b.raw:
        raise ConfigError("the two nodes' shared settings differ")
    if a.news_enabled == b.news_enabled:
        raise ConfigError("exactly one node should have news enabled")
