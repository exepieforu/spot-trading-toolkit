#!/usr/bin/env python3
"""
Spot signal predictor v2 (SIGNALS ONLY: never trades, needs no API keys).

Scans Binance spot coins in real time with 23 voting indicators and tells you:
  which coin, when to BUY, the STOP LOSS, TAKE PROFIT 1 / 2, and when to SELL.

Run:  python predictor.py   ->  http://127.0.0.1:8001
"""
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("predictor")

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
HTF = {"5m": "1h", "15m": "1h", "30m": "4h", "1h": "4h"}
FEE = 0.001            # Binance spot taker fee per side
ATR_STOP = 1.5         # stop distance in ATRs (= 1R); TP1 = 1R, TP2 = 2R
MAX_BARS = 24          # time exit
COOLDOWN_BARS = 4
MIN_NET = 0.003        # TP2 must clear fees by 0.3%
MIN_TRADES = 8         # minimum historical sample before the stats are trusted
HIST_DAYS = 30
POLL = 10
STATE_FILE = "predictor_state.json"
SETTINGS_FILE = "predictor_settings.json"
SETUPS = ("Pullback", "Breakout", "Squeeze", "Momentum")
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "AEUR", "EUR", "EURI", "XUSD", "UST", "PYUSD"}

SET = {
    "symbols": "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,DOGE/USDT,AVAX/USDT,LINK/USDT,DOT/USDT,LTC/USDT,"
               "NEAR/USDT,TRX/USDT,ATOM/USDT,UNI/USDT,ETC/USDT,FIL/USDT,APT/USDT,ARB/USDT,OP/USDT,INJ/USDT,SUI/USDT,AAVE/USDT,BCH/USDT",
    "auto_n": 30,            # scan the top N USDT coins by 24h volume (0 = use the custom list above)
    "timeframe": "15m",
    "min_score": 50,
    "min_conf": 50,          # minimum indicator confluence (0-100)
    "require_edge": True,
}


def symbols():
    return [s.strip() for s in SET["symbols"].split(",") if s.strip()]


def fin(x):
    try:
        x = float(x)
        return x if np.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def fp(x):
    return f"{x:,.2f}" if x >= 100 else f"{x:.3f}" if x >= 1 else f"{x:.5f}"


def g(x):
    x = fin(x)
    return "–" if x is None else f"{x:,.2f}" if abs(x) >= 100 else f"{x:.4g}"


# ------------------------------------------------------------- indicators
def _rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def _supertrend(h, l, c, tr, mult=3.0, n=10):
    atr = _rma(tr, n)
    hl2 = (h + l) / 2
    ub, lb, cv = (hl2 + mult * atr).values, (hl2 - mult * atr).values, c.values
    fu, fl, d = ub.copy(), lb.copy(), np.ones(len(cv))
    for i in range(1, len(cv)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or cv[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or cv[i - 1] < fl[i - 1]) else fl[i - 1]
        d[i] = (-1 if cv[i] < fl[i] else 1) if d[i - 1] == 1 else (1 if cv[i] > fu[i] else -1)
    return d


def _psar(h, l, af0=0.02, afmax=0.2):
    h, l = h.values, l.values
    n = len(h)
    sar, bull, af, ep = np.zeros(n), True, af0, h[0]
    sar[0] = l[0]
    for i in range(1, n):
        sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
        if bull:
            sar[i] = min(sar[i], l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar[i]:
                bull, sar[i], ep, af = False, ep, l[i], af0
            elif h[i] > ep:
                ep, af = h[i], min(af + af0, afmax)
        else:
            sar[i] = max(sar[i], h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar[i]:
                bull, sar[i], ep, af = True, ep, h[i], af0
            elif l[i] < ep:
                ep, af = l[i], min(af + af0, afmax)
    return sar


def _aroon(h, l, n=25):
    up = h.rolling(n + 1).apply(lambda x: x.argmax(), raw=True)
    dn = l.rolling(n + 1).apply(lambda x: x.argmin(), raw=True)
    return 100 * up / n, 100 * dn / n


VOTE_FAMILIES = {
    "Trend": ["v_ema", "v_e200", "v_macd", "v_adx", "v_st", "v_sar", "v_ich", "v_aroon"],
    "Momentum": ["v_rsi", "v_stoch", "v_srsi", "v_cci", "v_wr", "v_roc", "v_mfi", "v_ao"],
    "Range": ["v_bb", "v_kc", "v_dc"],
    "Volume": ["v_obv", "v_cmf", "v_vwap", "v_vol"],
}

# (vote column, label, family, reading)
IND = [
    ("v_ema", "EMA 20 / 50 stack", "Trend", lambda r: f"{g(r['ema_f'])} / {g(r['ema_s'])}"),
    ("v_e200", "Price vs EMA 200", "Trend", lambda r: g(r["ema200"])),
    ("v_macd", "MACD (12,26,9)", "Trend", lambda r: f"hist {g(r['macd_h'])}"),
    ("v_adx", "ADX / DI (14)", "Trend", lambda r: f"ADX {g(r['adx'])}  +DI {g(r['pdi'])}  -DI {g(r['mdi'])}"),
    ("v_st", "Supertrend (10,3)", "Trend", lambda r: "uptrend" if r["st_dir"] > 0 else "downtrend"),
    ("v_sar", "Parabolic SAR", "Trend", lambda r: g(r["sar"])),
    ("v_ich", "Ichimoku cloud", "Trend", lambda r: f"cloud {g(r['ich_bot'])} to {g(r['ich_top'])}"),
    ("v_aroon", "Aroon (25)", "Trend", lambda r: f"up {g(r['aro_up'])}  down {g(r['aro_dn'])}"),
    ("v_rsi", "RSI (14)", "Momentum", lambda r: g(r["rsi"])),
    ("v_stoch", "Stochastic (14,3,3)", "Momentum", lambda r: f"%K {g(r['sk'])}  %D {g(r['sd'])}"),
    ("v_srsi", "Stochastic RSI", "Momentum", lambda r: f"%K {g(r['srsi_k'])}  %D {g(r['srsi_d'])}"),
    ("v_cci", "CCI (20)", "Momentum", lambda r: g(r["cci"])),
    ("v_wr", "Williams %R (14)", "Momentum", lambda r: g(r["wr"])),
    ("v_roc", "Rate of change (12)", "Momentum", lambda r: g(r["roc"]) + "%"),
    ("v_mfi", "Money Flow Index (14)", "Momentum", lambda r: g(r["mfi"])),
    ("v_ao", "Awesome Oscillator", "Momentum", lambda r: g(r["ao"])),
    ("v_bb", "Bollinger %B (20,2)", "Range", lambda r: f"{g(r['pctb'])}  {'squeeze' if r['squeeze'] else 'normal width'}"),
    ("v_kc", "Keltner Channel", "Range", lambda r: f"upper {g(r['kc_up'])}"),
    ("v_dc", "Donchian position (20)", "Range", lambda r: f"{g(r['dpos'] * 100)}% of range"),
    ("v_obv", "On-Balance Volume", "Volume", lambda r: "above its EMA" if r["obv"] > r["obv_ma"] else "below its EMA"),
    ("v_cmf", "Chaikin Money Flow (20)", "Volume", lambda r: g(r["cmf"])),
    ("v_vwap", "Rolling VWAP (24h)", "Volume", lambda r: g(r["vwap"])),
    ("v_vol", "Volume pressure", "Volume", lambda r: f"{g(r['vr'])}x average"),
]


def add_ind(df, tf="15m"):
    d = df.copy().reset_index(drop=True)
    h, l, c, v = d["high"], d["low"], d["close"], d["volume"]
    nan = np.nan
    d["ema_f"], d["ema_s"] = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()
    delta = c.diff()
    d["rsi"] = 100 - 100 / (1 + _rma(delta.clip(lower=0), 14) / _rma((-delta.clip(upper=0)), 14).replace(0, nan))
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = _rma(tr, 14)
    d["vol_ma"] = v.rolling(20).mean()
    d["vr"] = v / d["vol_ma"].replace(0, nan)
    d["hh"] = h.rolling(20).max().shift(1)
    d["res50"], d["sup50"] = h.rolling(50).max().shift(1), l.rolling(50).min().shift(1)
    # trend
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    d["macd"], d["macd_s"] = macd, macd.ewm(span=9, adjust=False).mean()
    d["macd_h"] = d["macd"] - d["macd_s"]
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index)
    d["pdi"], d["mdi"] = 100 * _rma(plus, 14) / d["atr"], 100 * _rma(minus, 14) / d["atr"]
    dx = 100 * (d["pdi"] - d["mdi"]).abs() / (d["pdi"] + d["mdi"]).replace(0, nan)
    d["adx"] = _rma(dx.fillna(0), 14)
    d["st_dir"] = _supertrend(h, l, c, tr)
    d["sar"] = _psar(h, l)
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    d["tenkan"], d["kijun"] = tenkan, kijun
    d["ich_top"], d["ich_bot"] = np.fmax(span_a, span_b), np.fmin(span_a, span_b)
    d["aro_up"], d["aro_dn"] = _aroon(h, l)
    # momentum
    ll14, hh14 = l.rolling(14).min(), h.rolling(14).max()
    rng14 = (hh14 - ll14).replace(0, nan)
    d["sk"] = (100 * (c - ll14) / rng14).rolling(3).mean()
    d["sd"] = d["sk"].rolling(3).mean()
    rmin, rmax = d["rsi"].rolling(14).min(), d["rsi"].rolling(14).max()
    d["srsi_k"] = (100 * (d["rsi"] - rmin) / (rmax - rmin).replace(0, nan)).rolling(3).mean()
    d["srsi_d"] = d["srsi_k"].rolling(3).mean()
    tp = (h + l + c) / 3
    mad = (tp - tp.rolling(20).mean()).abs().rolling(20).mean().replace(0, nan)
    d["cci"] = (tp - tp.rolling(20).mean()) / (0.015 * mad)
    d["wr"] = -100 * (hh14 - c) / rng14
    d["roc"] = c.pct_change(12) * 100
    rmf = tp * v
    pos = rmf.where(tp > tp.shift(), 0).rolling(14).sum()
    neg = rmf.where(tp < tp.shift(), 0).rolling(14).sum().replace(0, nan)
    d["mfi"] = 100 - 100 / (1 + pos / neg)
    med = (h + l) / 2
    d["ao"] = med.rolling(5).mean() - med.rolling(34).mean()
    # range / volatility
    mid, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    d["bb_up"], d["bb_lo"] = mid + 2 * sd, mid - 2 * sd
    d["pctb"] = (c - d["bb_lo"]) / (d["bb_up"] - d["bb_lo"]).replace(0, nan)
    bw = (d["bb_up"] - d["bb_lo"]) / mid
    d["bw"], d["squeeze"] = bw, (bw < bw.rolling(50).mean() * 0.75)
    d["kc_up"] = d["ema_f"] + 1.5 * d["atr"]
    dh, dl = h.rolling(20).max(), l.rolling(20).min()
    d["dpos"] = (c - dl) / (dh - dl).replace(0, nan)
    # volume
    d["obv"] = (np.sign(c.diff().fillna(0)) * v).cumsum()
    d["obv_ma"] = d["obv"].ewm(span=20, adjust=False).mean()
    mfm = ((c - l) - (h - c)) / (h - l).replace(0, nan)
    d["cmf"] = (mfm * v).rolling(20).sum() / v.rolling(20).sum().replace(0, nan)
    win = max(20, int(86_400_000 / TF_MS.get(tf, 900_000)))
    d["vwap"] = (tp * v).rolling(win, min_periods=10).sum() / v.rolling(win, min_periods=10).sum().replace(0, nan)
    return add_votes(d)


def add_votes(d):
    V = lambda up, dn: up.astype(int) - dn.astype(int)
    c, o = d["close"], d["open"]
    d["v_ema"] = V((c > d.ema_f) & (d.ema_f > d.ema_s), (c < d.ema_s) & (d.ema_f < d.ema_s))
    d["v_e200"] = V(c > d.ema200, c < d.ema200)
    d["v_macd"] = V(d.macd > d.macd_s, d.macd < d.macd_s)
    d["v_adx"] = V((d.adx > 20) & (d.pdi > d.mdi), (d.adx > 20) & (d.mdi > d.pdi))
    d["v_st"] = d.st_dir.astype(int)
    d["v_sar"] = V(c > d.sar, c < d.sar)
    d["v_ich"] = V((c > d.ich_top) & (d.tenkan > d.kijun), (c < d.ich_bot) & (d.tenkan < d.kijun))
    d["v_aroon"] = V((d.aro_up > d.aro_dn) & (d.aro_up > 70), (d.aro_dn > d.aro_up) & (d.aro_dn > 70))
    d["v_rsi"] = V((d.rsi > 50) & (d.rsi < 75), d.rsi >= 75)
    d["v_stoch"] = V((d.sk > d.sd) & (d.sk < 85), d.sk < d.sd)
    d["v_srsi"] = V((d.srsi_k > d.srsi_d) & (d.srsi_k < 85), d.srsi_k < d.srsi_d)
    d["v_cci"] = V((d.cci > 0) & (d.cci < 200), d.cci < 0)
    d["v_wr"] = V((d.wr > -50) & (d.wr <= -20), d.wr < -50)
    d["v_roc"] = V(d.roc > 0, d.roc < 0)
    d["v_mfi"] = V((d.mfi > 50) & (d.mfi < 80), (d.mfi < 40) | (d.mfi >= 85))
    d["v_ao"] = V((d.ao > 0) & (d.ao > d.ao.shift()), (d.ao < 0) & (d.ao < d.ao.shift()))
    d["v_bb"] = V((d.pctb > 0.5) & (d.pctb < 1.0), d.pctb < 0.5)
    d["v_kc"] = V((c > d.ema_f) & (c <= d.kc_up), c < d.ema_f)
    d["v_dc"] = V(d.dpos > 0.5, d.dpos < 0.5)
    d["v_obv"] = V(d.obv > d.obv_ma, d.obv < d.obv_ma)
    d["v_cmf"] = V(d.cmf > 0.05, d.cmf < -0.05)
    d["v_vwap"] = V(c > d.vwap, c < d.vwap)
    d["v_vol"] = V((d.vr >= 1.2) & (c > o), (d.vr >= 1.2) & (c < o))
    for fam, cols in VOTE_FAMILIES.items():
        d["fam_" + fam] = (d[cols].mean(axis=1) + 1) * 50
    d["conf"] = d[["fam_" + f for f in VOTE_FAMILIES]].mean(axis=1)
    return d


def fam_of(r):
    return {f: int(round(r["fam_" + f])) for f in VOTE_FAMILIES}


def inds_of(r):
    return [{"name": n, "fam": f, "v": int(r[col]), "val": fn(r)} for col, n, f, fn in IND]


def with_htf(df, hdf, tf, htf):
    """Attach higher-timeframe trend using only HTF candles that had CLOSED (no lookahead)."""
    h = add_ind(hdf, htf)
    h["htf_ok"] = (h.close > h.ema_s) & (h.ema_f > h.ema_s)
    h["htf_spread"] = (h.ema_f - h.ema_s) / h.ema_s
    h["avail"] = h["ts"] + TF_MS[htf]
    h = h[["avail", "htf_ok", "htf_spread"]].sort_values("avail")
    d = df.copy()
    d["close_time"] = d["ts"] + TF_MS[tf]
    d = pd.merge_asof(d.sort_values("close_time"), h, left_on="close_time", right_on="avail", direction="backward")
    d["htf_ok"] = d["htf_ok"].fillna(False).astype(bool)
    d["htf_spread"] = d["htf_spread"].fillna(0.0)
    return d.reset_index(drop=True)


# ------------------------------------------------------------------ setups
def detect(R, i):
    """Long-only setups, gated by indicator confluence. R = list of row dicts."""
    if i < 60:
        return None
    r, p = R[i], R[i - 1]
    if any(pd.isna(x) for x in (r["atr"], r["rsi"], r["vol_ma"], r["hh"], p["rsi"], r["conf"])):
        return None
    if r["conf"] < SET["min_conf"] or not (r["ema_f"] > r["ema_s"] and r["close"] > r["ema_s"]):
        return None
    vr = r["vr"] if not pd.isna(r["vr"]) else 0
    if r["close"] > r["hh"] and vr >= 1.5 and r["rsi"] < 75:
        return {"setup": "Breakout", "vr": vr}
    if p["squeeze"] and r["close"] > r["bb_up"] and vr >= 1.2 and r["rsi"] < 75:
        return {"setup": "Squeeze", "vr": vr}
    if p["macd_h"] <= 0 < r["macd_h"] and r["adx"] > 20 and r["st_dir"] > 0 and 50 <= r["rsi"] < 70:
        return {"setup": "Momentum", "vr": vr}
    if p["rsi"] < 45 and r["rsi"] > p["rsi"] and r["close"] > p["close"]:
        return {"setup": "Pullback", "vr": vr}
    return None


def levels(r):
    entry = r["close"]
    risk = ATR_STOP * r["atr"]
    if risk <= 0 or (2 * risk) / entry - 2 * FEE < MIN_NET:      # fees would eat the edge
        return None
    return {"entry": entry, "risk": risk, "stop": entry - risk, "stop_now": entry - risk,
            "tp1": entry + risk, "tp2": entry + 2 * risk}


def step(st, high, low):
    """Trade state machine shared by the history simulator and live tracking.
    Sell 50% at TP1 (stop moves to entry), the rest at TP2. Stop is checked first (conservative)."""
    ev = []
    if st["status"] != "ACTIVE":
        return ev
    if not st["tp1_hit"]:
        if low <= st["stop"]:
            st.update(status="STOPPED", r_gross=-1.0)
            return ["STOP"]
        if high >= st["tp1"]:
            st.update(tp1_hit=True, stop_now=st["entry"])
            ev.append("TP1")
    if st["tp1_hit"]:
        if high >= st["tp2"]:
            st.update(status="WIN", r_gross=1.5)
            ev.append("TP2")
        elif low <= st["entry"] and "TP1" not in ev:
            st.update(status="BREAKEVEN", r_gross=0.5)
            ev.append("BE")
    return ev


def expire(st, price):
    r = (price - st["entry"]) / st["risk"]
    st.update(status="EXPIRED", r_gross=(0.5 + 0.5 * r) if st["tp1_hit"] else r)


def r_net(st):
    return st["r_gross"] - 2 * FEE * st["entry"] / st["risk"]


def compute_stats(df):
    """Replay the exact same rules over history, one trade at a time."""
    R = df.to_dict("records")
    by, i = {k: [] for k in SETUPS}, 60
    while i < len(R) - 1:
        cand = detect(R, i)
        lv = levels(R[i]) if (cand and R[i]["htf_ok"]) else None
        if not lv:
            i += 1
            continue
        st = {**lv, "tp1_hit": False, "status": "ACTIVE", "r_gross": None}
        j = i + 1
        while j < len(R):
            step(st, R[j]["high"], R[j]["low"])
            if st["status"] == "ACTIVE" and j - i >= MAX_BARS:
                expire(st, R[j]["close"])
            if st["status"] != "ACTIVE":
                break
            j += 1
        if st["status"] == "ACTIVE":
            break
        by[cand["setup"]].append(r_net(st))
        i = j + 1
    return {k: {"n": len(v), "win": round(100 * sum(x > 0 for x in v) / len(v)) if v else 0,
                "avg_r": round(float(np.mean(v)), 2) if v else 0.0} for k, v in by.items()}


def score(r, p, cand, stats):
    clip = lambda x, a, b: max(a, min(b, x))
    conf = clip((r["conf"] - 50) / 35, 0, 1) * 30
    htf = clip(r["htf_spread"] / 0.01, 0, 1) * 15
    q = 5 + (clip((45 - p["rsi"]) / 20, 0, 1) if cand["setup"] == "Pullback" else clip((cand["vr"] - 1) / 2, 0, 1)) * 15
    adx = clip(((0 if pd.isna(r["adx"]) else r["adx"]) - 15) / 25, 0, 1) * 10
    s = (stats or {}).get(cand["setup"])
    hist = clip(s["avg_r"] / 0.4, 0, 1) * 25 if s and s["n"] >= MIN_TRADES else 5
    return int(round(conf + htf + q + adx + hist))


# ------------------------------------------------------------------ scanner
def telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=6)
    except Exception as e:
        log.warning("Telegram failed: %s", e)


class Scanner:
    def __init__(self, ex=None):
        self.ex = ex or ccxt.binance({"enableRateLimit": True})
        self.ex.load_markets()
        self.lock = threading.RLock()
        self.sigs, self.events, self.eid = [], [], 0
        self.market, self.stats = {}, {}
        self.last_candle, self.next_fetch = {}, {}
        self.auto, self.auto_t = [], 0
        self.paused, self.error, self.last_scan = False, "", None
        self._load()

    # persistence -------------------------------------------------------
    def _load(self):
        if os.path.exists(STATE_FILE):
            try:
                d = json.load(open(STATE_FILE))
                self.sigs, self.events, self.eid = d["signals"], d["events"], d["eid"]
            except Exception as e:
                log.warning("Could not read %s: %s", STATE_FILE, e)

    def _save(self):
        json.dump({"signals": self.sigs[-300:], "events": self.events[-200:], "eid": self.eid}, open(STATE_FILE, "w"))

    def emit(self, typ, sig, text):
        self.eid += 1
        self.events.append({"id": self.eid, "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "type": typ, "symbol": sig["symbol"], "text": text})
        log.info("%s %s: %s", typ, sig["symbol"], text)
        threading.Thread(target=telegram, args=(f"{typ} {sig['symbol']}\n{text}",), daemon=True).start()

    # universe ------------------------------------------------------------
    def refresh_auto(self):
        tk = self.ex.fetch_tickers()
        rows = []
        for sym, t in tk.items():
            m = self.ex.markets.get(sym) or {}
            base = sym.split("/")[0]
            if (not sym.endswith("/USDT") or not m.get("spot") or m.get("active") is False or base in STABLES
                    or any(base.endswith(x) for x in ("UP", "DOWN", "BULL", "BEAR"))):
                continue
            rows.append((t.get("quoteVolume") or 0, sym))
        self.auto = [s for _, s in sorted(rows, reverse=True)]
        self.auto_t = time.time()
        log.info("Auto universe refreshed: %s ...", self.auto[:8])

    def universe(self):
        if SET["auto_n"] > 0:
            if not self.auto or time.time() - self.auto_t > 3600:
                try:
                    self.refresh_auto()
                except Exception as e:
                    log.warning("Auto universe failed: %s", e)
            if self.auto:
                return self.auto[:SET["auto_n"]]
        return [s for s in symbols() if s in self.ex.markets]

    # data ----------------------------------------------------------------
    def history(self, sym, tf, days):
        since, out = self.ex.milliseconds() - days * 86_400_000, []
        while True:
            b = self.ex.fetch_ohlcv(sym, tf, since=since, limit=1000)
            if not b:
                break
            out += b
            since = b[-1][0] + 1
            if len(b) < 1000:
                break
        return pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"]).iloc[:-1]

    def refresh_stats(self, sym):
        tf = SET["timeframe"]
        try:
            df = add_ind(self.history(sym, tf, HIST_DAYS), tf)
            h = self.history(sym, HTF[tf], HIST_DAYS + 12)
            self.stats[sym] = {"t": time.time(), "data": compute_stats(with_htf(df, h, tf, HTF[tf]))}
        except Exception as e:
            log.warning("Stats failed for %s: %s", sym, e)
            self.stats[sym] = {"t": time.time() - 6600, "data": None}      # retry in ~10 min

    # live tracking -------------------------------------------------------
    def update_active(self, prices):
        now = int(time.time() * 1000)
        for s in self.sigs:
            if s["status"] != "ACTIVE" or s["symbol"] not in prices:
                continue
            p, sym = prices[s["symbol"]], s["symbol"]
            for ev in step(s, p, p):
                if ev == "TP1":
                    self.emit("TP1", s, f"SELL 50% of {sym} at about {fp(s['tp1'])} and MOVE YOUR STOP TO ENTRY ({fp(s['entry'])}).")
                elif ev == "TP2":
                    self.emit("TP2", s, f"SELL the remaining {sym} at about {fp(s['tp2'])}. Target reached, trade complete.")
                elif ev == "STOP":
                    self.emit("STOP", s, f"STOP LOSS hit: SELL ALL {sym} at market (about {fp(s['stop'])}).")
                elif ev == "BE":
                    self.emit("EXIT", s, f"Price fell back to entry after TP1: SELL the remaining {sym}. Closed with a small profit.")
            if s["status"] == "ACTIVE" and now - s["created"] > MAX_BARS * TF_MS[s["tf"]]:
                expire(s, p)
                self.emit("EXIT", s, f"Time exit: setup did not play out. SELL {sym} at market (about {fp(p)}).")
            if s["status"] != "ACTIVE":
                s["closed_at"], s["r_net"] = now, round(r_net(s), 2)
        self._save()

    # scanning ------------------------------------------------------------
    def scan(self, sym):
        tf = SET["timeframe"]
        cols = ["ts", "open", "high", "low", "close", "volume"]
        raw = self.ex.fetch_ohlcv(sym, tf, limit=300)
        hraw = self.ex.fetch_ohlcv(sym, HTF[tf], limit=150)
        df = with_htf(add_ind(pd.DataFrame(raw, columns=cols).iloc[:-1], tf), pd.DataFrame(hraw, columns=cols), tf, HTF[tf])
        R = df.to_dict("records")
        i = len(R) - 1
        r, p = R[i], R[i - 1]
        ts = int(r["ts"])
        self.next_fetch[sym] = ts + 2 * TF_MS[tf] + 3000
        stats = (self.stats.get(sym) or {}).get("data")
        up = r["ema_f"] > r["ema_s"] and r["close"] > r["ema_s"]
        m = self.market.setdefault(sym, {})
        m.update(trend="Up" if up else "Down/Flat", htf="Up" if r["htf_ok"] else "Down/Flat", rsi=fin(r["rsi"]),
                 adx=fin(r["adx"]), conf=round(float(r["conf"]), 1), fam=fam_of(r), inds=inds_of(r),
                 stats=stats, status="Watching")
        if self.last_candle.get(sym) == ts:
            return
        self.last_candle[sym] = ts
        if any(s["symbol"] == sym and s["status"] == "ACTIVE" for s in self.sigs):
            m["status"] = "Signal active"
            return
        recent = [s for s in self.sigs if s["symbol"] == sym and s["closed_at"]]
        if recent and int(time.time() * 1000) - recent[-1]["closed_at"] < COOLDOWN_BARS * TF_MS[tf]:
            m["status"] = "Cooling down"
            return
        cand = detect(R, i)
        if not cand:
            return
        name = cand["setup"]
        if not r["htf_ok"]:
            m["status"] = f"{name} found, skipped (higher timeframe not in uptrend)"
            return
        lv = levels(r)
        if not lv:
            m["status"] = f"{name} found, skipped (move too small to beat fees)"
            return
        st = (stats or {}).get(name)
        has_edge = bool(st and st["n"] >= MIN_TRADES and st["avg_r"] > 0)
        if SET["require_edge"] and not has_edge:
            m["status"] = f"{name} found, skipped (no proven edge on this coin yet)"
            return
        sc = score(r, p, cand, stats)
        warn = None
        if name != "Breakout" and not pd.isna(r["res50"]) and (r["res50"] - lv["entry"]) / lv["risk"] < 1.0:
            sc -= 10
            warn = f"Resistance near {fp(r['res50'])} is close to TP1, so there is limited room to run"
        if sc < SET["min_score"]:
            m["status"] = f"{name} found, skipped (score {sc} below {SET['min_score']})"
            return
        votes = [r[c] for cols_ in VOTE_FAMILIES.values() for c in cols_]
        why = {"Pullback": f"RSI turned up from {p['rsi']:.0f} (pullback buy in an uptrend)",
               "Breakout": f"Broke the 20-candle high on {cand['vr']:.1f}x average volume",
               "Squeeze": f"Volatility squeeze released: closed above the upper Bollinger Band on {cand['vr']:.1f}x volume",
               "Momentum": f"MACD histogram crossed above zero with ADX {r['adx']:.0f} and Supertrend up"}[name]
        reasons = [f"{HTF[tf]} trend is up (EMA20 above EMA50)", why,
                   f"Indicator confluence {r['conf']:.0f}/100: {sum(v > 0 for v in votes)} of {len(votes)} indicators bullish, "
                   f"{sum(v < 0 for v in votes)} bearish",
                   (f"History: {st['win']}% wins, avg {st['avg_r']:+.2f}R over {st['n']} similar setups (30d, after fees)"
                    if st and st["n"] else "History: not enough similar setups to judge")]
        sig = {"id": f"{sym}-{ts}", "symbol": sym, "setup": name, "score": sc, "tf": tf, "created": int(time.time() * 1000),
               **lv, "buy_below": lv["entry"] + 0.25 * r["atr"], "tp1_hit": False, "status": "ACTIVE",
               "r_gross": None, "r_net": None, "closed_at": None, "stats": st, "has_edge": has_edge,
               "reasons": reasons, "warn": warn, "conf": round(float(r["conf"]), 1), "fam": fam_of(r), "inds": inds_of(r)}
        self.sigs.append(sig)
        m["status"] = "SIGNAL!"
        self.emit("BUY", sig, f"BUY {sym} at market (about {fp(lv['entry'])}, don't chase above {fp(sig['buy_below'])}). "
                              f"STOP LOSS {fp(lv['stop'])} | TAKE PROFIT 1 {fp(lv['tp1'])} (sell half) | "
                              f"TAKE PROFIT 2 {fp(lv['tp2'])} | {name}, score {sc}, confluence {r['conf']:.0f}/100. "
                              f"Place the stop and targets as orders on Binance right away.")
        self._save()

    def tick(self):
        syms = self.universe()
        prices = {k: v["last"] for k, v in self.ex.fetch_tickers(syms).items() if v.get("last")}
        with self.lock:
            for k, v in prices.items():
                self.market.setdefault(k, {})["price"] = v
            self.update_active(prices)
        for s in [s for s in syms if time.time() - (self.stats.get(s) or {"t": 0})["t"] > 7200][:3]:
            self.refresh_stats(s)
        now = int(time.time() * 1000)
        for s in syms:
            if now >= self.next_fetch.get(s, 0):
                try:
                    with self.lock:
                        self.scan(s)
                except ccxt.BadSymbol:
                    log.warning("Skipping unavailable symbol %s", s)
                    self.next_fetch[s] = now + 3_600_000
        self.last_scan, self.error = time.time(), ""

    def loop(self):
        while True:
            try:
                if not self.paused:
                    self.tick()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                log.warning("Tick failed: %s", self.error)
            time.sleep(POLL)

    def snapshot(self):
        with self.lock:
            closed = [s for s in self.sigs if s["status"] != "ACTIVE"]
            rs = [s["r_net"] for s in closed if s["r_net"] is not None]
            syms = self.universe()
            mk = [{"symbol": s, **self.market.get(s, {})} for s in syms]
            return {"paused": self.paused, "error": self.error, "last_scan": self.last_scan, "settings": SET,
                    "warming": sum(1 for s in syms if s not in self.stats), "universe": len(syms),
                    "active": [s for s in self.sigs if s["status"] == "ACTIVE"],
                    "closed": [{k: v for k, v in s.items() if k not in ("inds",)} for s in closed[-40:][::-1]],
                    "events": self.events[-40:], "market": mk,
                    "record": {"n": len(rs), "wins": sum(x > 0 for x in rs),
                               "avg_r": round(float(np.mean(rs)), 2) if rs else 0, "sum_r": round(sum(rs), 2)},
                    "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))}


# ---------------------------------------------------------------------- web
app = Flask(__name__)
TOKEN = secrets.token_hex(16)
scanner = None


@app.before_request
def guard():
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        return jsonify(error="forbidden"), 403
    if request.path.startswith("/api/") and request.headers.get("X-Token") != TOKEN:
        return jsonify(error="forbidden"), 403


@app.get("/api/status")
def status():
    return jsonify(scanner.snapshot())


@app.post("/api/settings")
def settings():
    d = request.get_json()
    try:
        syms = [s.strip().upper() for s in str(d["symbols"]).split(",") if s.strip()]
        bad = [s for s in syms if s not in scanner.ex.markets]
        auto_n, sc, conf = int(d["auto_n"]), int(d["min_score"]), int(d["min_conf"])
        if not 0 <= auto_n <= 60:
            raise ValueError("Auto-scan count must be 0-60")
        if auto_n == 0 and (not syms or len(syms) > 60):
            raise ValueError("Enter 1-60 coins when auto-scan is off")
        if bad:
            raise ValueError(f"Unknown symbols: {', '.join(bad)}")
        if d["timeframe"] not in HTF:
            raise ValueError("Timeframe must be 5m, 15m, 30m or 1h")
        if not (0 <= sc <= 100 and 0 <= conf <= 100):
            raise ValueError("Score and confluence must be 0-100")
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    with scanner.lock:
        if d["timeframe"] != SET["timeframe"] or conf != SET["min_conf"]:
            scanner.stats.clear()
        scanner.last_candle.clear()
        scanner.next_fetch.clear()
        SET.update(symbols=",".join(syms), auto_n=auto_n, timeframe=d["timeframe"], min_score=sc, min_conf=conf,
                   require_edge=bool(d["require_edge"]))
    json.dump(SET, open(SETTINGS_FILE, "w"))
    return jsonify(ok=True)


@app.post("/api/pause")
def pause():
    scanner.paused = bool(request.get_json().get("paused"))
    return jsonify(ok=True)


@app.get("/")
def index():
    return Response(PAGE.replace("__TOKEN__", TOKEN), mimetype="text/html")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spot Signal Predictor</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#14171a;--muted:#66707a;--line:#e3e6ea;--accent:#2563eb;--pos:#0a8a4a;--neg:#d12c3a;--warn:#b7791f;--posbg:#e7f6ee;--negbg:#fdecee}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#181c22;--text:#e8ebef;--muted:#8b95a1;--line:#2a3038;--accent:#5b8dff;--pos:#33c47a;--neg:#ff6370;--warn:#e0a84a;--posbg:#14301f;--negbg:#3a1a1e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
h1{font-size:17px;margin:0;flex:1}main{max-width:1150px;margin:0 auto;padding:18px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 10px}
button{font:inherit;padding:7px 13px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block;margin-right:6px}.dot.on{background:var(--pos)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.note{color:var(--muted);font-size:12px}.warn{color:var(--warn)}
.sig{border:2px solid var(--pos);border-radius:12px;padding:14px;background:var(--card)}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.coin{font-size:20px;font-weight:700}
.pill{padding:2px 9px;border-radius:99px;border:1px solid var(--line);font-size:12px;color:var(--muted)}
.buy{margin:10px 0;padding:10px 12px;border-radius:8px;background:var(--posbg);color:var(--pos);font-size:17px;font-weight:700}
.buy small{font-weight:400;color:var(--muted);font-size:12px}
.lv{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px}
.lv div{border:1px solid var(--line);border-radius:8px;padding:8px 10px}.lv b{font-size:16px;font-variant-numeric:tabular-nums}
.lv .s{background:var(--negbg)}.lv .t{background:var(--posbg)}.lv span{display:block;font-size:12px;color:var(--muted)}
.bar{position:relative;height:10px;background:var(--line);border-radius:6px;margin:16px 4px 8px}
.bar i{position:absolute;top:-3px;width:2px;height:16px;background:var(--muted)}.bar u{position:absolute;top:-4px;width:6px;height:18px;border-radius:3px;background:var(--accent);transform:translateX(-3px)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:500;font-size:12px}.scroll{overflow-x:auto;max-height:360px;overflow-y:auto}
tr.click{cursor:pointer}tr.click:hover{background:var(--bg)}
.ev{padding:7px 0;border-bottom:1px solid var(--line)}.ev b{padding:1px 8px;border-radius:5px;font-size:12px;margin-right:6px}
.b-BUY,.b-TP1,.b-TP2{background:var(--posbg);color:var(--pos)}.b-STOP{background:var(--negbg);color:var(--neg)}.b-EXIT{background:var(--line);color:var(--warn)}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}label{display:grid;gap:3px;font-size:12px;color:var(--muted)}
input,select{font:inherit;padding:7px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--text);width:100%}
#toast{position:fixed;bottom:18px;right:18px;background:var(--text);color:var(--bg);padding:12px 16px;border-radius:8px;display:none;max-width:380px;z-index:9;font-weight:600}
ul{margin:6px 0 0;padding-left:18px;color:var(--muted)}details{margin-top:8px}summary{cursor:pointer;color:var(--accent)}
.fams{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0}.fams span{border:1px solid var(--line);border-radius:8px;padding:4px 10px}
.conf{font-weight:700}
</style></head><body>
<header><h1>Spot Signal Predictor</h1>
 <span><span id="dot" class="dot"></span><span id="state">Connecting…</span></span>
 <button id="notifBtn">Enable alerts</button><button id="soundBtn">Sound: on</button><button id="pauseBtn">Pause</button></header>
<main>
 <div id="err" class="neg"></div>
 <div class="card"><h2>Your position sizing <span class="note">(used to suggest how much to buy)</span></h2>
  <div class="form"><label>Account size (USDT)<input id="acct" type="number" value="1000"></label><label>Risk per trade (%)<input id="riskp" type="number" step="0.1" value="1"></label></div></div>
 <div id="active"></div>
 <div class="card"><h2>Alerts</h2><div id="events" class="scroll"></div></div>
 <div class="card"><h2>Signal track record (live, since you started)</h2><div id="record" class="note"></div><div class="scroll"><table id="closedTbl"></table></div></div>
 <div class="card"><h2>Market scan <span class="note">(click a coin for its full indicator readout)</span></h2><div class="scroll" style="max-height:480px"><table id="mkt"></table></div></div>
 <div class="card"><h2>Indicator readout</h2><label style="max-width:260px">Coin<select id="indSel"></select></label><div id="indOut"></div></div>
 <div class="card"><h2>Settings</h2><div class="form">
  <label>Auto-scan top N coins by volume (0 = use my list)<input id="f_auto_n" type="number"></label>
  <label>My coin list (used when auto-scan is 0)<input id="f_symbols"></label>
  <label>Signal timeframe<select id="f_timeframe"><option>5m</option><option>15m</option><option>30m</option><option>1h</option></select></label>
  <label>Minimum score (0-100)<input id="f_min_score" type="number"></label>
  <label>Minimum indicator confluence (0-100)<input id="f_min_conf" type="number"></label>
  <label>Only coins with proven edge<select id="f_require_edge"><option value="1">Yes (recommended)</option><option value="0">No (more signals, less filtered)</option></select></label></div>
  <p><button id="saveBtn" class="primary">Save settings</button> <span id="tg" class="note"></span></p></div>
 <p class="note">Signals are rule-based estimates, not predictions or guarantees, and many will lose. Backtested stats come from only about 30 days of data. Never risk money you can't afford to lose. Not financial advice.</p>
</main><div id="toast"></div>
<script>
const TOKEN="__TOKEN__",$=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fp=x=>x==null?'–':x>=100?x.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):x>=1?x.toFixed(3):x.toFixed(5);
const pct=(a,b)=>((a/b-1)*100).toFixed(2)+'%';
const cc=v=>v>=60?'pos':v<=40?'neg':'';
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:b?JSON.stringify(b):undefined});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j;}
let sound=true,lastEv=null,paused=false,formLoaded=false,data=null,selKey='';
const openSet=new Set();
document.addEventListener('toggle',e=>{const d=e.target;if(d.dataset&&d.dataset.k){d.open?openSet.add(d.dataset.k):openSet.delete(d.dataset.k);}},true);
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',9000);}
function beep(){if(!sound)return;try{const c=new (window.AudioContext||window.webkitAudioContext)(),o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.frequency.value=880;g.gain.value=.15;o.start();o.stop(c.currentTime+.25);}catch(e){}}
function notify(e){const title=(e.type==='BUY'?'BUY ':'SELL ')+e.symbol;toast(title+': '+e.text);beep();if('Notification' in window&&Notification.permission==='granted')new Notification(title,{body:e.text});}
$('notifBtn').onclick=async()=>{if(!('Notification' in window))return toast('Notifications not supported');const p=await Notification.requestPermission();$('notifBtn').textContent=p==='granted'?'Alerts enabled':'Alerts blocked';};
$('soundBtn').onclick=()=>{sound=!sound;$('soundBtn').textContent='Sound: '+(sound?'on':'off');if(sound)beep();};
$('pauseBtn').onclick=async()=>{await api('/api/pause',{paused:!paused});refresh();};
$('saveBtn').onclick=async()=>{try{await api('/api/settings',{symbols:$('f_symbols').value,auto_n:$('f_auto_n').value,timeframe:$('f_timeframe').value,min_score:$('f_min_score').value,min_conf:$('f_min_conf').value,require_edge:$('f_require_edge').value==='1'});toast('Saved. Re-analysing coins…');refresh();}catch(e){toast(e.message);}};
const ago=ms=>{const m=Math.floor((Date.now()-ms)/60000);return m<1?'just now':m<60?m+'m ago':Math.floor(m/60)+'h '+m%60+'m ago';};

function indTable(o){
  if(!o||!o.inds)return '<div class="note">Analysing…</div>';
  const f=o.fam||{};
  return `<div class="fams">${['Trend','Momentum','Range','Volume'].map(k=>`<span>${k} <b class="${cc(f[k])}">${f[k]??'–'}</b></span>`).join('')}<span>Confluence <b class="conf ${cc(o.conf)}">${o.conf??'–'}</b>/100</span></div>
  <div class="scroll"><table><tr><th>Indicator</th><th>Family</th><th>Reading</th><th>Vote</th></tr>${o.inds.map(i=>`<tr><td>${esc(i.name)}</td><td>${esc(i.fam)}</td><td>${esc(i.val)}</td><td class="${i.v>0?'pos':i.v<0?'neg':''}">${i.v>0?'▲ Bullish':i.v<0?'▼ Bearish':'– Neutral'}</td></tr>`).join('')}</table></div>`;
}
function card(s,price){
  const acct=+$('acct').value||0,rp=+$('riskp').value||0,risk=s.entry-s.stop;
  let qty=risk>0?(acct*rp/100)/risk:0;if(qty*s.entry>acct)qty=acct/s.entry;
  const base=s.symbol.split('/')[0],cur=price||s.entry,pl=(cur/s.entry-1)*100;
  const pos=Math.max(0,Math.min(100,(cur-s.stop)/(s.tp2-s.stop)*100));
  const late=!s.tp1_hit&&cur>s.buy_below;
  const live=s.tp1_hit?'TP1 reached: half sold, stop now at entry '+fp(s.entry):late?'Price already ran above the buy limit. Skip this one (don\'t chase).':'Waiting for targets';
  const k='sig-'+s.id;
  return `<div class="sig"><div class="top"><span class="coin">${esc(s.symbol)}</span><span class="pill">${esc(s.setup)}</span><span class="pill">Score ${s.score}</span>${s.conf!=null?`<span class="pill">Confluence ${s.conf}</span>`:''}<span class="note">${ago(s.created)}</span></div>
  <div class="buy">${s.tp1_hit?'IN TRADE':'BUY at market'} ≈ ${fp(s.entry)} <small>${s.tp1_hit?'':'(don\'t buy above '+fp(s.buy_below)+')'}</small></div>
  <div class="lv"><div class="s"><span>STOP LOSS: sell everything if hit</span><b>${fp(s.stop_now)}</b> <span>${pct(s.stop_now,s.entry)} from entry${s.tp1_hit?' (moved to breakeven)':''}</span></div>
   <div class="t"><span>TAKE PROFIT 1: sell 50%, move stop to entry</span><b>${fp(s.tp1)}</b> <span>+${pct(s.tp1,s.entry)}</span></div>
   <div class="t"><span>TAKE PROFIT 2: sell the rest</span><b>${fp(s.tp2)}</b> <span>+${pct(s.tp2,s.entry)}</span></div></div>
  <div class="bar"><i style="left:33%"></i><i style="left:67%"></i><u style="left:${pos}%"></u></div>
  <div class="note">Now ${fp(cur)} (<span class="${pl>=0?'pos':'neg'}">${pl>=0?'+':''}${pl.toFixed(2)}%</span>) · ${esc(live)}</div>
  <p style="margin:8px 0 0"><b>Suggested size:</b> ${qty>0?qty.toPrecision(3)+' '+esc(base)+' (≈ '+(qty*s.entry).toFixed(2)+' USDT), risking ≈ '+(qty*risk).toFixed(2)+' USDT if the stop hits':'enter your account size above'}</p>
  <ul>${s.reasons.map(r=>'<li>'+esc(r)+'</li>').join('')}${s.warn?'<li class="warn">'+esc(s.warn)+'</li>':''}${s.has_edge?'':'<li class="warn">No proven historical edge on this coin; treat with extra caution</li>'}</ul>
  ${s.inds?`<details data-k="${esc(k)}" ${openSet.has(k)?'open':''}><summary>All 23 indicators at signal time</summary>${indTable(s)}</details>`:''}</div>`;
}
function renderPanel(){
  if(!data)return;const m=data.market.find(x=>x.symbol===$('indSel').value);$('indOut').innerHTML=indTable(m);
}
$('indSel').onchange=renderPanel;
function histCell(st){
  if(!st)return '…';
  const rows=['Pullback','Breakout','Squeeze','Momentum'].filter(k=>st[k]&&st[k].n).map(k=>`${k.slice(0,4)} ${st[k].win}% / ${st[k].avg_r>0?'+':''}${st[k].avg_r}R (${st[k].n})`);
  return rows.length?rows.join('<br>'):'no setups';
}
function render(d){
  data=d;paused=d.paused;const st=d.settings;
  $('dot').className='dot'+(d.paused?'':' on');
  $('state').textContent=d.paused?'Paused':d.warming?`Analysing history (${d.warming} of ${d.universe} coins left)…`:'Scanning '+d.universe+' coins live';
  $('pauseBtn').textContent=d.paused?'Resume':'Pause';$('err').textContent=d.error||'';
  if(!formLoaded){$('f_symbols').value=st.symbols;$('f_auto_n').value=st.auto_n;$('f_timeframe').value=st.timeframe;$('f_min_score').value=st.min_score;$('f_min_conf').value=st.min_conf;$('f_require_edge').value=st.require_edge?'1':'0';formLoaded=true;}
  $('tg').textContent=d.telegram?'Telegram alerts: on':'Telegram alerts: off (see README)';
  const px={};d.market.forEach(m=>px[m.symbol]=m.price);
  $('active').innerHTML=d.active.length?d.active.map(s=>card(s,px[s.symbol])).join(''):
   `<div class="card"><b>No active signals.</b><div class="note">The scanner only alerts when a high-quality setup passes the trend, confluence, fee and history checks. Hours without a signal is normal. Keep this tab open (or enable Telegram) and you'll get a sound + notification the moment one fires.</div></div>`;
  $('events').innerHTML=[...d.events].reverse().map(e=>`<div class="ev"><b class="b-${e.type}">${e.type==='BUY'?'BUY':'SELL'}</b><b>${esc(e.symbol)}</b> ${esc(e.text)} <span class="note">${esc(e.t.slice(11,19))} UTC</span></div>`).join('')||'<div class="note">No alerts yet.</div>';
  if(d.events.length){const top=d.events[d.events.length-1].id;if(lastEv!==null)d.events.filter(e=>e.id>lastEv).forEach(notify);lastEv=top;}else if(lastEv===null)lastEv=0;
  const r=d.record;$('record').textContent=r.n?`${r.n} closed signals · ${Math.round(r.wins/r.n*100)}% profitable · average ${r.avg_r>0?'+':''}${r.avg_r}R per signal · total ${r.sum_r>0?'+':''}${r.sum_r}R (1R = the amount risked per trade, after fees)`:'No closed signals yet. This fills in as signals play out, so you can judge real accuracy.';
  $('closedTbl').innerHTML=d.closed.length?'<tr><th>Coin</th><th>Setup</th><th>Opened</th><th>Result</th><th>R (net)</th></tr>'+d.closed.map(s=>`<tr><td>${esc(s.symbol)}</td><td>${esc(s.setup)}</td><td>${ago(s.created)}</td><td>${esc(s.status)}</td><td class="${s.r_net>0?'pos':'neg'}">${s.r_net>0?'+':''}${s.r_net}</td></tr>`).join(''):'';
  const mk=[...d.market].sort((a,b)=>(b.conf??-1)-(a.conf??-1));
  $('mkt').innerHTML='<tr><th>Coin</th><th>Price</th><th>Confluence</th><th>Trend</th><th>Higher TF</th><th>RSI</th><th>ADX</th><th>Setup history (win% / avg R / trades)</th><th>Status</th></tr>'+mk.map(m=>
    `<tr class="click" onclick="pick('${esc(m.symbol)}')"><td>${esc(m.symbol)}</td><td>${fp(m.price)}</td><td class="conf ${cc(m.conf)}">${m.conf??'…'}</td><td class="${m.trend==='Up'?'pos':''}">${esc(m.trend||'…')}</td><td class="${m.htf==='Up'?'pos':''}">${esc(m.htf||'…')}</td><td>${m.rsi?m.rsi.toFixed(0):'–'}</td><td>${m.adx?m.adx.toFixed(0):'–'}</td><td style="text-align:left;font-size:12px">${histCell(m.stats)}</td><td style="text-align:left">${esc(m.status||'Waiting')}</td></tr>`).join('');
  const key=d.market.map(m=>m.symbol).join(',');
  if(key!==selKey){const cur=$('indSel').value;$('indSel').innerHTML=d.market.map(m=>`<option>${esc(m.symbol)}</option>`).join('');if(d.market.some(m=>m.symbol===cur))$('indSel').value=cur;selKey=key;}
  renderPanel();
}
window.pick=s=>{$('indSel').value=s;renderPanel();$('indSel').scrollIntoView({behavior:'smooth',block:'center'});};
async function refresh(){try{render(await api('/api/status'));}catch(e){$('state').textContent='Server unreachable';}}
refresh();setInterval(refresh,3000);
</script></body></html>"""


def main():
    global scanner
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("predictor.log")])
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if os.path.exists(SETTINGS_FILE):
        try:
            SET.update(json.load(open(SETTINGS_FILE)))
        except Exception:
            pass
    print("Connecting to Binance…")
    scanner = Scanner()
    threading.Thread(target=scanner.loop, daemon=True).start()
    url = "http://127.0.0.1:8001"
    print(f"Dashboard: {url}  (Ctrl+C to quit)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=8001, threaded=True)


if __name__ == "__main__":
    main()
