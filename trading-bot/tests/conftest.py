import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot as B  # noqa: E402


class FakeBinance:
    """Offline stand-in for ccxt.binance."""

    def __init__(self, *a, **k):
        self.px = 100.0

    def set_sandbox_mode(self, flag):
        pass

    def load_markets(self):
        pass

    def market(self, sym):
        return {"base": sym.split("/")[0], "limits": {"cost": {"min": 5}, "amount": {"min": 0.0001}}}

    def amount_to_precision(self, sym, q):
        return f"{q:.5f}"

    def fetch_tickers(self, syms):
        return {s: {"last": self.px} for s in syms}

    def fetch_ticker(self, sym):
        return {"last": self.px}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Temp working dir for state files, and restore the global CFG after each test."""
    monkeypatch.chdir(tmp_path)
    saved = dict(vars(B.CFG))
    yield
    vars(B.CFG).update(saved)


@pytest.fixture
def paper_bot(monkeypatch):
    monkeypatch.setattr(B.ccxt, "binance", FakeBinance)
    B.CFG.mode = "paper"
    return B.Bot()


@pytest.fixture
def candles():
    rng = np.random.default_rng(1)
    n = 6000
    close = 30000 * np.exp(np.cumsum(rng.normal(0.00005, 0.003, n)))
    open_ = np.r_[close[0], close[:-1]]
    hi = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.0015, n)))
    lo = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.0015, n)))
    return pd.DataFrame({"ts": range(n), "open": open_, "high": hi, "low": lo, "close": close, "volume": 1.0})
