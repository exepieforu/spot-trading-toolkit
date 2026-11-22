import pandas as pd
import pytest

import predictor as P

COLS = ["ts", "open", "high", "low", "close", "volume"]


def frame(ex, n=600, tf="15m"):
    return pd.DataFrame(ex.series("BTC/USDT", tf, n), columns=COLS)


# ---------------------------------------------------------------- indicators
def test_all_23_indicators_vote_and_read(ex):
    df = P.add_ind(frame(ex), "15m")
    r = df.iloc[-1].to_dict()
    inds = P.inds_of(r)
    assert len(inds) == 23
    assert {i["fam"] for i in inds} == {"Trend", "Momentum", "Range", "Volume"}
    assert all(i["v"] in (-1, 0, 1) for i in inds)
    assert all(i["val"] != "–" for i in inds), "an indicator has no reading on 600 candles"


def test_confluence_is_bounded_and_matches_families(ex):
    df = P.add_ind(frame(ex), "15m")
    assert df["conf"].between(0, 100).all()
    r = df.iloc[-1]
    fams = [r["fam_" + f] for f in P.VOTE_FAMILIES]
    assert r["conf"] == pytest.approx(sum(fams) / 4)


def test_htf_has_no_lookahead(ex):
    ltf = P.add_ind(frame(ex, 800, "15m"), "15m")
    hraw = pd.DataFrame(ex.series("BTC/USDT", "1h", 300), columns=COLS)
    merged = P.with_htf(ltf, hraw, "15m", "1h")
    h = P.add_ind(hraw, "1h")
    h["avail"] = h["ts"] + P.TF_MS["1h"]
    h["spread"] = (h.ema_f - h.ema_s) / h.ema_s
    for idx in (200, 450, 700):
        row = merged.iloc[idx]
        closed = h[h["avail"] <= row["close_time"]]
        assert row["htf_spread"] == pytest.approx(closed.iloc[-1]["spread"])


# -------------------------------------------------------------- trade logic
def new_trade():
    return {"entry": 100.0, "risk": 2.0, "stop": 98.0, "stop_now": 98.0, "tp1": 102.0, "tp2": 104.0,
            "tp1_hit": False, "status": "ACTIVE", "r_gross": None}


def test_full_win_path():
    st = new_trade()
    assert P.step(st, 102.5, 100.5) == ["TP1"] and st["stop_now"] == 100.0
    assert P.step(st, 104.1, 103.0) == ["TP2"]
    assert st["status"] == "WIN" and st["r_gross"] == 1.5


def test_stop_hit_is_minus_one_r():
    st = new_trade()
    assert P.step(st, 99.0, 97.9) == ["STOP"]
    assert st["status"] == "STOPPED" and st["r_gross"] == -1.0


def test_breakeven_after_tp1():
    st = new_trade()
    P.step(st, 102.2, 101.0)
    assert P.step(st, 101.0, 99.9) == ["BE"]
    assert st["r_gross"] == 0.5


def test_stop_is_checked_before_target_in_same_candle():
    st = new_trade()
    assert P.step(st, 105.0, 97.0) == ["STOP"]       # conservative assumption


def test_fees_reduce_r():
    st = new_trade()
    P.step(st, 102.5, 100.5)
    P.step(st, 104.1, 103.0)
    assert P.r_net(st) == pytest.approx(1.5 - 2 * P.FEE * 100 / 2)


def test_expire_after_tp1_blends_partial_profit():
    st = new_trade()
    P.step(st, 102.5, 100.5)
    P.expire(st, 101.0)
    assert st["status"] == "EXPIRED" and st["r_gross"] == pytest.approx(0.5 + 0.5 * 0.5)


def test_levels_reject_moves_too_small_to_beat_fees():
    assert P.levels({"close": 100.0, "atr": 0.05}) is None
    lv = P.levels({"close": 100.0, "atr": 1.0})
    assert lv["stop"] == pytest.approx(98.5) and lv["tp1"] == pytest.approx(101.5) and lv["tp2"] == pytest.approx(103.0)


def test_compute_stats_shape(ex):
    df = P.with_htf(P.add_ind(frame(ex, 2900), "15m"), pd.DataFrame(ex.series("BTC/USDT", "1h", 800), columns=COLS), "15m", "1h")
    stats = P.compute_stats(df)
    assert set(stats) == set(P.SETUPS)
    for s in stats.values():
        assert s["n"] >= 0 and 0 <= s["win"] <= 100


# ------------------------------------------------------------------ scanner
def test_auto_universe_excludes_stables_and_leveraged(scanner):
    P.SET["auto_n"] = 30
    u = scanner.universe()
    assert "USDC/USDT" not in u and "BTCUP/USDT" not in u
    assert len(u) == 28 and len(u) >= 20


def test_custom_universe_when_auto_off(scanner):
    P.SET["auto_n"] = 0
    assert len(scanner.universe()) == 24


def test_scan_emits_buy_signal_with_levels_and_indicators(scanner, monkeypatch):
    P.SET.update(min_score=0, min_conf=0, require_edge=False)
    monkeypatch.setattr(P, "detect", lambda R, i: {"setup": "Squeeze", "vr": 2.0})
    orig = P.with_htf
    monkeypatch.setattr(P, "with_htf", lambda *a, **k: orig(*a, **k).assign(htf_ok=True))
    scanner.scan("ETH/USDT")
    sig = scanner.sigs[-1]
    assert sig["stop"] < sig["entry"] < sig["tp1"] < sig["tp2"]
    assert len(sig["inds"]) == 23 and len(sig["reasons"]) == 4
    assert scanner.events[-1]["type"] == "BUY"


def test_live_tracking_emits_tp1_then_tp2(scanner):
    sig = {"id": "x", "symbol": "BTC/USDT", "setup": "Pullback", "score": 70, "tf": "15m", "created": int(__import__("time").time() * 1000),
           **{k: v for k, v in new_trade().items()}, "buy_below": 100.5, "r_net": None, "closed_at": None}
    scanner.sigs.append(sig)
    scanner.update_active({"BTC/USDT": 102.5})
    scanner.update_active({"BTC/USDT": 104.2})
    assert [e["type"] for e in scanner.events] == ["TP1", "TP2"]
    assert sig["status"] == "WIN" and sig["r_net"] == pytest.approx(1.4)


# ---------------------------------------------------------------------- web
@pytest.fixture
def client(scanner):
    P.scanner = scanner
    return P.app.test_client()


def test_api_requires_token_and_localhost(client):
    assert client.get("/api/status").status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/status", headers={"X-Token": P.TOKEN}).status_code == 200


def test_settings_validation(client):
    h = {"X-Token": P.TOKEN}
    base = {"symbols": "BTC/USDT", "auto_n": 0, "timeframe": "15m", "min_score": 50, "min_conf": 50, "require_edge": True}
    assert client.post("/api/settings", json=base, headers=h).status_code == 200
    assert client.post("/api/settings", json={**base, "symbols": "FOO/USDT"}, headers=h).status_code == 400
    assert client.post("/api/settings", json={**base, "timeframe": "7m"}, headers=h).status_code == 400
    assert client.post("/api/settings", json={**base, "min_score": 150}, headers=h).status_code == 400
