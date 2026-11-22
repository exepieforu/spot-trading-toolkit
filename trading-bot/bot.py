#!/usr/bin/env python3
"""
Binance spot trading bot: trend-pullback strategy with hard risk limits.

Modes (MODE in .env):
  paper   - real market data, simulated fills, no keys needed   (default)
  testnet - Binance spot testnet, fake money, needs testnet keys
  live    - real money (also needs CONFIRM_LIVE=YES)

Usage:
  python bot.py backtest --symbol BTC/USDT --days 60
  python bot.py run
"""
import argparse
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


def _f(name, default):
    return float(os.getenv(name, default))


@dataclass
class Config:
    mode: str = os.getenv("MODE", "paper").lower()
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    symbols: tuple = tuple(s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT").split(","))
    timeframe: str = os.getenv("TIMEFRAME", "15m")
    capital: float = _f("CAPITAL_USDT", 500)            # HARD CAP the bot may ever use
    risk_per_trade: float = _f("RISK_PER_TRADE_PCT", 0.5) / 100
    max_positions: int = int(_f("MAX_OPEN_POSITIONS", 3))
    max_trades_per_day: int = int(_f("MAX_TRADES_PER_DAY", 20))
    daily_loss_limit: float = _f("DAILY_LOSS_LIMIT_PCT", 3) / 100
    fee: float = _f("FEE_RATE", 0.001)                   # per side; 0.00075 with BNB discount
    slippage: float = _f("SLIPPAGE", 0.0005)
    # strategy
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_len: int = 14
    rsi_entry: float = _f("RSI_ENTRY", 45)
    atr_len: int = 14
    atr_stop: float = _f("ATR_STOP", 1.5)
    atr_tp: float = _f("ATR_TP", 2.5)
    min_net_edge: float = 0.002                          # TP must beat fees by >=0.2%
    max_hold_bars: int = int(_f("MAX_HOLD_BARS", 24))
    poll_seconds: int = int(_f("POLL_SECONDS", 15))


CFG = Config()
log = logging.getLogger("bot")
LOG_BUFFER = deque(maxlen=200)


class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(f"{datetime.fromtimestamp(record.created):%H:%M:%S} {record.levelname:<7} {record.getMessage()}")
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}


# ----------------------------------------------------------------- strategy
def add_indicators(df, c=CFG):
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=c.ema_fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=c.ema_slow, adjust=False).mean()
    d = df["close"].diff()
    gain = d.clip(lower=0).ewm(alpha=1 / c.rsi_len, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / c.rsi_len, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    pc = df["close"].shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / c.atr_len, adjust=False).mean()
    return df


def entry_signal(df, i, c=CFG):
    """Long-only: buy a pullback inside an uptrend once momentum turns up.
    Returns (entry, stop, take_profit) or None."""
    if i < c.ema_slow + 5:
        return None
    r, p = df.iloc[i], df.iloc[i - 1]
    if np.isnan(r.atr) or np.isnan(r.rsi):
        return None
    uptrend = r.ema_fast > r.ema_slow and r.close > r.ema_slow
    pullback = p.rsi < c.rsi_entry and r.rsi > p.rsi and r.close > p.close
    if not (uptrend and pullback):
        return None
    entry = r.close
    stop = entry - c.atr_stop * r.atr
    tp = entry + c.atr_tp * r.atr
    if (tp - entry) / entry - 2 * c.fee < c.min_net_edge:   # fees would eat the edge
        return None
    return entry, stop, tp


def position_qty(equity, free_cash, entry, stop, c=CFG):
    risk_qty = equity * c.risk_per_trade / (entry - stop)
    cap_qty = min(equity / c.max_positions, free_cash) / entry
    return min(risk_qty, cap_qty)


# ----------------------------------------------------------------- backtest
def backtest_df(df, c=CFG):
    df = add_indicators(df, c)
    equity, pos, trades = c.capital, None, []
    for i in range(len(df)):
        row = df.iloc[i]
        if pos:
            exit_px, why = None, None
            if row.low <= pos["stop"]:
                exit_px, why = pos["stop"] * (1 - c.slippage), "stop"
            elif row.high >= pos["tp"]:
                exit_px, why = pos["tp"], "tp"
            elif i - pos["i"] >= c.max_hold_bars:
                exit_px, why = row.close * (1 - c.slippage), "time"
            if exit_px:
                proceeds = pos["qty"] * exit_px
                pnl = proceeds - pos["cost"] - c.fee * (pos["cost"] + proceeds)
                equity += pnl
                trades.append({"pnl": pnl, "why": why})
                pos = None
        if not pos:
            sig = entry_signal(df, i, c)
            if sig:
                entry, stop, tp = sig
                fill = entry * (1 + c.slippage)
                qty = position_qty(equity, equity, fill, stop, c)
                if qty * fill > 10:
                    pos = {"qty": qty, "cost": qty * fill, "stop": stop, "tp": tp, "i": i}
    return equity, trades


def fetch_history(ex, symbol, tf, days):
    since, out = ex.milliseconds() - days * 86_400_000, []
    while True:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        out += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    return pd.DataFrame(out, columns=["ts", "open", "high", "low", "close", "volume"])


def run_backtest(symbol, days, c=CFG):
    ex = ccxt.binance({"enableRateLimit": True})
    df = fetch_history(ex, symbol, c.timeframe, days)
    equity, trades = backtest_df(df, c)
    n = len(trades)
    wins = sum(t["pnl"] > 0 for t in trades)
    curve, run = [], c.capital
    for t in trades:
        run += t["pnl"]
        curve.append(round(run, 2))
    return {"symbol": symbol, "days": days, "timeframe": c.timeframe, "candles": len(df),
            "trades": n, "win_rate": round(wins / n * 100) if n else 0,
            "start": c.capital, "end": round(equity, 2),
            "return_pct": round((equity / c.capital - 1) * 100, 2), "curve": curve}


def cmd_backtest(args):
    for s in ([args.symbol] if args.symbol else CFG.symbols):
        r = run_backtest(s, args.days)
        print(f"\n{s}  {r['timeframe']}  {r['days']}d  ({r['candles']} candles)")
        print(f"  trades: {r['trades']}  win rate: {r['win_rate']}%" if r["trades"] else "  no trades")
        print(f"  start {r['start']:.2f} -> end {r['end']:.2f} USDT  ({r['return_pct']:+.2f}%)  (after fees + slippage)")
    print("\nPast performance does not predict future results. "
          "Run paper mode for a few weeks before risking real money.")


# ---------------------------------------------------------------------- bot
class Bot:
    STATE = "state.json"

    def __init__(self, confirm_live=False):
        self.c = CFG
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.prices, self.last_tick, self.last_error = {}, None, ""
        if self.c.mode == "live" and not (confirm_live or os.getenv("CONFIRM_LIVE") == "YES"):
            raise RuntimeError("Live mode needs explicit confirmation")
        if self.c.mode in ("testnet", "live") and not (self.c.api_key and self.c.api_secret):
            raise RuntimeError("API keys missing in .env")
        self.ex = ccxt.binance({
            "apiKey": self.c.api_key or None, "secret": self.c.api_secret or None,
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        })
        if self.c.mode == "testnet":
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()
        self.STATE = f"state_{self.c.mode}.json"      # separate history per mode
        self.state = self._load()
        self.last_seen = {}
        if self.c.mode != "paper":
            free = self.ex.fetch_balance()["USDT"]["free"]
            if free < self.c.capital:
                log.warning("Free USDT %.2f is below CAPITAL_USDT %.2f", free, self.c.capital)

    # state ---------------------------------------------------------------
    def _load(self):
        if os.path.exists(self.STATE):
            return json.load(open(self.STATE))
        return {"realized": 0.0, "day": "", "day_pnl": 0.0, "day_trades": 0, "positions": {}, "history": []}

    def _save(self):
        json.dump(self.state, open(self.STATE, "w"), indent=2)

    @property
    def equity(self):
        return self.c.capital + self.state["realized"]

    @property
    def deployed(self):
        return sum(p["cost"] for p in self.state["positions"].values())

    def _rollover(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state.update(day=today, day_pnl=0.0, day_trades=0)
            self._save()

    # trading -------------------------------------------------------------
    def _fees_in_base(self, order, base):
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        return sum(f["cost"] for f in fees if f and f.get("currency") == base)

    def open(self, sym, entry, stop, tp):
        c, m = self.c, self.ex.market(sym)
        qty = position_qty(self.equity, self.equity - self.deployed, entry, stop, c)
        qty = float(self.ex.amount_to_precision(sym, qty))
        min_cost = (m["limits"]["cost"]["min"] or 10)
        if qty <= 0 or qty * entry < min_cost or qty < (m["limits"]["amount"]["min"] or 0):
            return
        if c.mode == "paper":
            fill, net_qty = entry * (1 + c.slippage), qty
        else:
            o = self.ex.create_market_buy_order(sym, qty)
            fill = o.get("average") or entry
            net_qty = float(self.ex.amount_to_precision(sym, o["filled"] - self._fees_in_base(o, m["base"])))
        self.state["positions"][sym] = {
            "qty": net_qty, "entry": fill, "cost": net_qty * fill, "stop": stop, "tp": tp,
            "opened": time.time()}
        self.state["day_trades"] += 1
        self._save()
        log.info("BUY  %s qty=%s @ %.4f  stop=%.4f tp=%.4f", sym, net_qty, fill, stop, tp)

    def close(self, sym, price, why):
        c, pos = self.c, self.state["positions"][sym]
        if c.mode == "paper":
            fill = price * (1 - c.slippage if why != "tp" else 1)
        else:
            o = self.ex.create_market_sell_order(sym, float(self.ex.amount_to_precision(sym, pos["qty"])))
            fill = o.get("average") or price
        proceeds = pos["qty"] * fill
        pnl = proceeds - pos["cost"] - c.fee * (pos["cost"] + proceeds)   # estimated fees
        s = self.state
        s["realized"] += pnl
        s["day_pnl"] += pnl
        s["history"].append({"sym": sym, "pnl": round(pnl, 4), "why": why,
                             "t": datetime.now(timezone.utc).isoformat()})
        del s["positions"][sym]
        self._save()
        log.info("SELL %s @ %.4f (%s)  pnl=%+.3f  equity=%.2f", sym, fill, why, pnl, self.equity)

    def manage_exits(self):
        for sym, pos in list(self.state["positions"].items()):
            px = self.ex.fetch_ticker(sym)["last"]
            held_bars = (time.time() - pos["opened"]) / TF_SECONDS.get(self.c.timeframe, 900)
            if px <= pos["stop"]:
                self.close(sym, px, "stop")
            elif px >= pos["tp"]:
                self.close(sym, px, "tp")
            elif held_bars >= self.c.max_hold_bars:
                self.close(sym, px, "time")

    def can_enter(self):
        s, c = self.state, self.c
        if s["day_pnl"] <= -c.daily_loss_limit * c.capital:
            return False
        return (s["day_trades"] < c.max_trades_per_day
                and len(s["positions"]) < c.max_positions
                and self.equity - self.deployed > 10)

    def scan_entries(self):
        for sym in self.c.symbols:
            if not self.can_enter():
                return
            if sym in self.state["positions"]:
                continue
            raw = self.ex.fetch_ohlcv(sym, self.c.timeframe, limit=300)
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"]).iloc[:-1]
            ts = int(df.ts.iloc[-1])
            if self.last_seen.get(sym) == ts:      # evaluate each closed candle once
                continue
            self.last_seen[sym] = ts
            df = add_indicators(df)
            sig = entry_signal(df, len(df) - 1)
            if sig:
                self.open(sym, *sig)

    def flatten(self):
        for sym in list(self.state["positions"]):
            self.close(sym, self.ex.fetch_ticker(sym)["last"], "flatten")

    def tick(self):
        with self.lock:
            if os.path.exists("STOP"):
                log.warning("STOP file found: flattening and stopping")
                self.flatten()
                self.stop_event.set()
                return
            self._rollover()
            tk = self.ex.fetch_tickers(list(self.c.symbols))
            self.prices = {k: v["last"] for k, v in tk.items()}
            self.manage_exits()
            self.scan_entries()
            self.last_tick, self.last_error = time.time(), ""

    def run(self):
        log.info("Starting in %s mode | capital cap %.2f USDT | %s %s",
                 self.c.mode.upper(), self.c.capital, self.c.symbols, self.c.timeframe)
        while not self.stop_event.is_set():
            try:
                self.tick()
            except KeyboardInterrupt:
                break
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                self.last_error = f"Network issue: {e}"
                log.warning(self.last_error)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Tick failed")
            self.stop_event.wait(self.c.poll_seconds)
        log.info("Stopped. Open positions remain in %s", self.STATE)

    def close_one(self, sym):
        with self.lock:
            if sym in self.state["positions"]:
                self.close(sym, self.ex.fetch_ticker(sym)["last"], "manual")

    def snapshot(self):
        with self.lock:
            s, pos, unreal = self.state, [], 0.0
            for sym, p in s["positions"].items():
                px = self.prices.get(sym, p["entry"])
                u = p["qty"] * px - p["cost"]
                unreal += u
                pos.append({**p, "sym": sym, "price": px, "unrealized": u,
                            "pct": (px / p["entry"] - 1) * 100})
            h = s["history"]
            return {"mode": self.c.mode, "equity": self.equity, "realized": s["realized"],
                    "unrealized": unreal, "day_pnl": s["day_pnl"], "day_trades": s["day_trades"],
                    "deployed": self.deployed, "capital": self.c.capital, "positions": pos,
                    "prices": self.prices, "history": h[-200:],
                    "wins": sum(x["pnl"] > 0 for x in h), "total_trades": len(h),
                    "halted": s["day_pnl"] <= -self.c.daily_loss_limit * self.c.capital,
                    "last_tick": self.last_tick, "last_error": self.last_error}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log"), BufferHandler()])
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--symbol")
    b.add_argument("--days", type=int, default=60)
    sub.add_parser("run")
    args = ap.parse_args()
    cmd_backtest(args) if args.cmd == "backtest" else Bot().run()
