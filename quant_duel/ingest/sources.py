"""Where daily prices come from, behind one interface.

``YFinanceSource`` is the real one (yfinance is unofficial and can break —
which is why it sits behind ``DataSource``). ``SyntheticSource`` produces
deterministic prices with no predictable signal, for tests, replays and
machines without internet.

Every source returns the same frame: one row per trading day, indexed by
date (``datetime.date``), columns ``open high low close adj_close volume``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

from ..market_calendar import trading_days

COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]


class DataSourceError(RuntimeError):
    pass


class DataSource(ABC):
    name = "abstract"

    @abstractmethod
    def fetch(self, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        """Daily bars for ``ticker`` from ``start`` to ``end`` inclusive."""


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.Index([], name="date"))


def system_ca_bundle():
    """A PEM of the operating system's trusted roots plus certifi's.

    yfinance talks through curl_cffi, which trusts only certifi's bundle.
    Where antivirus scans HTTPS with its own root (Norton, Avast...), that
    root is in the OS store but not in certifi, so every download fails.
    Python's ``ssl`` loads the OS store on Windows; export it once."""
    import ssl
    import tempfile
    from pathlib import Path
    try:
        ctx = ssl.create_default_context()
        pems = [ssl.DER_cert_to_PEM_cert(d)
                for d in ctx.get_ca_certs(binary_form=True)]
        try:
            import certifi
            pems.append(Path(certifi.where()).read_text(encoding="ascii"))
        except Exception:                                 # noqa: BLE001
            pass
        path = Path(tempfile.gettempdir()) / "quant_duel_ca.pem"
        path.write_text("".join(pems), encoding="ascii")
        return str(path)
    except Exception:                                     # noqa: BLE001
        return None


class YFinanceSource(DataSource):
    name = "yfinance"

    def __init__(self):
        self._session = None

    def session(self):
        if self._session is None:
            try:
                from curl_cffi import requests as creq
                self._session = creq.Session(
                    impersonate="chrome", verify=system_ca_bundle() or True)
            except ImportError:
                self._session = False
        return self._session or None

    def fetch(self, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise DataSourceError("yfinance is not installed "
                                  "(pip install yfinance)") from exc
        try:
            raw = yf.download(ticker, start=start.isoformat(),
                              end=(end + dt.timedelta(days=1)).isoformat(),
                              auto_adjust=False, progress=False,
                              threads=False, session=self.session())
        except Exception as exc:                          # noqa: BLE001
            raise DataSourceError(f"yfinance failed for {ticker}: {exc}") from exc
        if raw is None or raw.empty:
            return _empty()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        out = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                  "Close": "close", "Adj Close": "adj_close",
                                  "Volume": "volume"})
        if "adj_close" not in out:
            out["adj_close"] = out["close"]
        out.index = pd.Index(pd.to_datetime(out.index).date, name="date")
        return out[COLUMNS].astype(float)


class SyntheticSource(DataSource):
    """Deterministic random-walk prices: a shared market factor plus noise,
    no information about tomorrow in anything up to today. Same seed, same
    prices — on every machine."""

    name = "synthetic"

    def __init__(self, seed: int = 0, start: dt.date = dt.date(2010, 1, 4),
                 drift: float = 0.0003):
        self.seed = seed
        self.origin = start
        self.drift = drift

    def _rng(self, key: str) -> np.random.Generator:
        h = int(hashlib.sha256(f"{self.seed}:{key}".encode()).hexdigest()[:16], 16)
        return np.random.default_rng(h)

    def _series(self, ticker: str, end: dt.date) -> pd.DataFrame:
        # One generator per component: a later end date only appends draws,
        # so the history already fetched never changes.
        days = trading_days(self.origin, end)
        n = len(days)
        market = self._rng("__market__").normal(0, 0.009, n)
        params = self._rng(ticker + ":params")
        beta = 0.6 + params.random() * 0.8
        idio_sd = 0.008 + params.random() * 0.012
        level = 50.0 * (1 + params.random() * 4)
        idio = self._rng(ticker + ":idio").normal(0, idio_sd, n)
        rets = self.drift + beta * market + idio
        if ticker in ("SPY", "QQQ"):
            rets = self.drift + market * (1.0 if ticker == "SPY" else 1.2)
        close = level * np.exp(np.cumsum(rets))
        gap = self._rng(ticker + ":gap").normal(0, 0.003, n)
        open_ = close / np.exp(rets) * np.exp(gap)
        wick = self._rng(ticker + ":wick")
        hi = np.abs(wick.normal(0, 0.004, (n, 2)))
        high = np.maximum(open_, close) * (1 + hi[:, 0])
        low = np.minimum(open_, close) * (1 - hi[:, 1])
        volume = np.round(1e6 * np.exp(self._rng(ticker + ":vol")
                                       .normal(0, 0.3, n)))
        return pd.DataFrame({"open": open_, "high": high, "low": low,
                             "close": close, "adj_close": close,
                             "volume": volume},
                            index=pd.Index(days, name="date"))

    def fetch(self, ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        full = self._series(ticker, end)
        return full[full.index >= start]


def make_source(name: str, seed: int = 0) -> DataSource:
    if name == "yfinance":
        return YFinanceSource()
    if name == "synthetic":
        return SyntheticSource(seed)
    raise DataSourceError(f"unknown data source {name!r}")
