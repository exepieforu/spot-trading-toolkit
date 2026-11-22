# Trading Bot (Binance spot, with dashboard)

An automated long-only spot bot with hard risk limits, a backtester, paper and testnet modes,
and a local web dashboard. **Use paper mode first. Trading can lose money.**

## Setup (Python 3.11+)
    pip install -r requirements.txt
    cp .env.example .env      # then edit it (paper mode needs no keys)

## Dashboard (GUI)
    python gui.py
Opens http://127.0.0.1:8000 with Start/Stop, live equity, open positions, trade
history, a settings panel, one-click backtests and a live log. It listens on
localhost only and never sends your API keys to the browser (keys stay in `.env`).
Settings you save are stored in `settings.json`.

## Recommended order (do not skip steps)
1. **Backtest:**  `python bot.py backtest --days 90`
2. **Paper trade** (MODE=paper) for 2-4 weeks: `python bot.py run`
3. **Testnet** (MODE=testnet): keys from https://testnet.binance.vision
4. **Live with a small amount** (MODE=live, CONFIRM_LIVE=YES, CAPITAL_USDT small)

## API key safety
- Permissions: **Enable Spot & Margin Trading only**. Never enable withdrawals.
- Restrict the key to the IP of the machine running the bot.
- Keep `.env` private; never commit it.

## Controls
- `CAPITAL_USDT` is a hard cap; the bot never deploys more than that.
- Daily loss limit pauses new entries until the next UTC day.
- Create a file named `STOP` in the bot folder to close all positions and exit.
- Each mode keeps its own history in `state_paper.json` / `state_testnet.json` / `state_live.json`, logs in `bot.log`.

## Known limitations
- Stops/targets are monitored by the bot (polling every 15s), not placed as
  exchange orders. If the bot or your internet goes down, positions are
  unprotected until it restarts. Run it on a reliable server.
- Fees in PnL are estimated; Binance trade history is the source of truth.
- Spot long-only: it cannot profit from falling markets.
