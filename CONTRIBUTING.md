# Contributing

Thanks for helping out!

    python -m venv .venv && source .venv/bin/activate
    pip install -r signal-predictor/requirements.txt -r trading-bot/requirements.txt -r requirements-dev.txt
    ruff check .
    python -m pytest signal-predictor -q
    python -m pytest trading-bot -q

Guidelines
- Tests run fully offline against a fake exchange. Never add tests that call Binance or need keys.
- Changing strategy logic? Include a test, and say in the PR what the change does to backtest results.
- Never commit `.env`, state files, logs or anything containing keys.
- Keep changes focused. Open an issue first for large features.
