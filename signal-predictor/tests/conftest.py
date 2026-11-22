import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import predictor as P  # noqa: E402

COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "NEAR", "TRX", "ATOM", "UNI",
         "ETC", "FIL", "APT", "ARB", "OP", "INJ", "SUI", "AAVE", "BCH", "USDC", "BTCUP", "PEPE", "SHIB", "TON", "HBAR"]


class FakeExchange:
    """Offline stand-in for ccxt.binance with deterministic random-walk candles."""

    def __init__(self):
        self.markets = {f"{c}/USDT": {"spot": True, "active": True} for c in COINS}
        self._done = set()

    def load_markets(self):
        pass

    def milliseconds(self):
        return int(time.time() * 1000)

    def series(self, sym, tf, n, seed=None):
        ms = P.TF_MS[tf]
        end = (int(time.time() * 1000) // ms) * ms
        ts = end - ms * np.arange(n - 1, -1, -1)
        rng = np.random.default_rng(seed if seed is not None else sum(map(ord, sym)) + (tf == "1h"))
        c = 100 * np.exp(np.cumsum(rng.normal(0.0007, 0.004 * (ms / 9e5) ** 0.5, n)))
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.002, n)))
        lo = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.002, n)))
        v = rng.lognormal(0, 0.6, n) * 100
        return [[int(a), b, x, y, z, w] for a, b, x, y, z, w in zip(ts, o, h, lo, c, v)]

    def fetch_ohlcv(self, sym, tf, since=None, limit=300):
        if since is not None:                       # paginated history: one page, then empty
            if (sym, tf) in self._done:
                return []
            self._done.add((sym, tf))
            return self.series(sym, tf, 2900 if tf == "15m" else 1000)
        return self.series(sym, tf, limit)

    def fetch_tickers(self, syms=None):
        keys = syms or list(self.markets)
        return {k: {"last": 100.0, "quoteVolume": 1e9 - i * 1e7} for i, k in enumerate(keys)}


@pytest.fixture
def ex():
    return FakeExchange()


@pytest.fixture
def scanner(ex, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                      # state files go to a temp dir
    saved = dict(P.SET)
    yield P.Scanner(ex)
    P.SET.clear()
    P.SET.update(saved)
