import pytest

import bot as B
import gui


# ----------------------------------------------------------------- strategy
def test_backtest_runs_and_accounts_for_fees(candles):
    equity, trades = B.backtest_df(candles)
    assert len(trades) > 0
    assert equity == pytest.approx(B.CFG.capital + sum(t["pnl"] for t in trades))
    assert {t["why"] for t in trades} <= {"stop", "tp", "time"}


def test_position_size_never_exceeds_capital_slice():
    B.CFG.max_positions = 3
    qty = B.position_qty(equity=900, free_cash=900, entry=100, stop=99.99)    # tiny stop would imply a huge size
    assert qty * 100 <= 900 / 3 + 1e-9


def test_position_size_respects_risk_budget():
    B.CFG.risk_per_trade = 0.005
    qty = B.position_qty(equity=1000, free_cash=1000, entry=100, stop=90)      # 10 USDT stop distance, 5 USDT risk
    assert qty * (100 - 90) <= 1000 * 0.005 + 1e-9


def test_entry_rejected_when_fees_eat_the_edge(candles):
    df = B.add_indicators(candles)
    df["atr"] = 0.001                                                           # absurdly small target
    assert all(B.entry_signal(df, i) is None for i in range(60, 400))


# -------------------------------------------------------------- paper trading
def test_paper_buy_then_take_profit(paper_bot):
    paper_bot.open("BTC/USDT", 100.0, 98.0, 104.0)
    assert "BTC/USDT" in paper_bot.state["positions"]
    paper_bot.ex.px = 104.5
    paper_bot.manage_exits()
    snap = paper_bot.snapshot()
    assert snap["positions"] == [] and snap["total_trades"] == 1
    assert snap["history"][-1]["why"] == "tp" and snap["realized"] > 0


def test_paper_buy_then_stop_loss(paper_bot):
    paper_bot.open("BTC/USDT", 100.0, 98.0, 104.0)
    paper_bot.ex.px = 97.0
    paper_bot.manage_exits()
    assert paper_bot.snapshot()["history"][-1]["why"] == "stop" and paper_bot.state["realized"] < 0


def test_daily_loss_limit_blocks_new_entries(paper_bot):
    assert paper_bot.can_enter()
    paper_bot.state["day_pnl"] = -B.CFG.daily_loss_limit * B.CFG.capital - 1
    assert not paper_bot.can_enter()


def test_state_survives_restart(paper_bot, monkeypatch):
    paper_bot.open("ETH/USDT", 100.0, 98.0, 104.0)
    again = B.Bot()
    assert "ETH/USDT" in again.state["positions"]


def test_live_mode_requires_confirmation_and_keys(monkeypatch):
    monkeypatch.setattr(B.ccxt, "binance", lambda *a, **k: None)
    B.CFG.mode = "live"
    monkeypatch.delenv("CONFIRM_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="confirmation"):
        B.Bot()
    with pytest.raises(RuntimeError, match="keys"):
        B.Bot(confirm_live=True)


# ---------------------------------------------------------------------- gui
@pytest.fixture
def client():
    return gui.app.test_client()


GOOD = {"mode": "paper", "symbols": "BTC/USDT,ETH/USDT", "timeframe": "15m", "capital": 500, "risk_pct": 0.5,
        "max_positions": 3, "max_trades": 20, "daily_loss_pct": 3, "rsi_entry": 45, "atr_stop": 1.5, "atr_tp": 2.5}


def test_gui_blocks_requests_without_token_or_from_other_hosts(client):
    assert client.get("/api/status").status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/status", headers={"X-Token": gui.TOKEN}).status_code == 200


@pytest.mark.parametrize("patch", [{"capital": 1}, {"mode": "yolo"}, {"symbols": "nonsense"}, {"timeframe": "2m"},
                                   {"risk_pct": 50}, {"atr_tp": 1.0, "atr_stop": 2.0}])
def test_gui_rejects_invalid_settings(patch):
    with pytest.raises(ValueError):
        gui.apply_settings({**GOOD, **patch}, save=False)


def test_gui_accepts_valid_settings_and_applies_them():
    gui.apply_settings({**GOOD, "capital": 750, "risk_pct": 1}, save=False)
    assert B.CFG.capital == 750 and B.CFG.risk_per_trade == pytest.approx(0.01)


def test_gui_live_start_needs_typed_confirmation(client, monkeypatch):
    monkeypatch.setattr(B.ccxt, "binance", lambda *a, **k: None)
    B.CFG.mode, B.CFG.api_key, B.CFG.api_secret = "live", "k", "s"
    monkeypatch.delenv("CONFIRM_LIVE", raising=False)
    r = client.post("/api/start", json={}, headers={"X-Token": gui.TOKEN})
    assert r.status_code == 400 and "confirmation" in r.get_json()["error"]
