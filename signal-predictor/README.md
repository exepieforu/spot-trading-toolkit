# Spot Signal Predictor v2 (signals only)

Scans Binance spot coins live and tells you **which coin, when to BUY, the STOP LOSS,
TAKE PROFIT 1 / 2, and when to SELL**. It never trades and needs **no API keys**.

## Run
    pip install -r requirements.txt
    python predictor.py          # opens http://127.0.0.1:8001

Keep it running. When a setup appears you get a sound, a browser notification
(click "Enable alerts") and optionally a Telegram message (see `.env.example`).

## Coins
By default it auto-scans the **top 30 USDT coins by 24h volume** (stablecoins and
leveraged tokens excluded), refreshed hourly. Set "Auto-scan" to 0 in Settings to use
your own list instead (up to 60 coins; 24 are pre-filled).

## The 23 indicators (each votes bullish / bearish / neutral)
| Family | Indicators |
|---|---|
| Trend (8) | EMA 20/50 stack, price vs EMA 200, MACD, ADX with +DI/-DI, Supertrend, Parabolic SAR, Ichimoku cloud, Aroon |
| Momentum (8) | RSI, Stochastic, Stochastic RSI, CCI, Williams %R, Rate of Change, Money Flow Index, Awesome Oscillator |
| Range (3) | Bollinger %B, Keltner Channel, Donchian position |
| Volume (4) | On-Balance Volume, Chaikin Money Flow, rolling VWAP, volume pressure |

Also tracked: ATR, Bollinger width (squeeze), 50-candle support/resistance.
Each family gets a 0-100 score; their average is the **confluence** (0-100).
Click any coin in the Market scan to see every indicator reading and vote.

## How a signal fires
1. Higher timeframe (1h for 15m signals) AND signal timeframe both in an uptrend.
2. Confluence at or above your minimum (default 50).
3. One of four setups: **Pullback** (RSI dip then turn up), **Breakout** (20-candle
   high on 1.5x volume), **Squeeze** (Bollinger squeeze release), **Momentum**
   (MACD cross with ADX and Supertrend confirming).
4. The target clears Binance fees (0.1%/side) by a margin.
5. **Proof check:** the same rules (including confluence) are replayed over 30 days of
   that coin. By default, a setup only fires if it was profitable after fees with at
   least 8 past trades.
6. Score >= your minimum (default 50). Score blends confluence, higher-timeframe
   strength, setup quality, ADX and historical edge. It ranks signals; it is NOT a
   win probability. Resistance close to TP1 costs 10 points and shows a warning.

Levels: stop = 1.5 x ATR below entry (1R); TP1 = +1R (sell half, move stop to entry);
TP2 = +2R (sell the rest); time exit after 24 candles.

## Using signals safely
- After buying, **place the stop and take-profit as real orders on Binance** (OCO).
  The dashboard checks prices about every 10 seconds and can miss fast wicks.
- Use the position-size box (account size + risk %). Risking 0.5-1% per trade is typical.
- More indicators does not mean more accuracy; they mostly filter out weak trades.
  The **Signal track record** panel shows the tool's real, live results. Trust that
  over any backtest, and follow signals on paper for a few weeks first.
- Long-only spot: no signals in downtrends. Often there will be none for hours.
- First start takes a few minutes while it downloads and analyses 30 days per coin.

Signals are estimates, not guarantees, and many will lose. Not financial advice.
