"""Candle band mode: TradingView BB with an EMA basis on spread candles."""

import math
from datetime import datetime

import pytest

from arrow_statarb.core.candles import (IST, SpreadCandles, align_legs, bucket_start,
                                        pine_ema, pine_stdev)
from arrow_statarb.core.signal import SignalEngine
from arrow_statarb.core.algo import ArrowAutoTrader


def _ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()


# ── TradingView's own formulas ────────────────────────────────────────────────
def test_pine_ema_is_sma_seeded_then_recursive():
    xs = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0]
    n, a = 3, 2.0 / 4.0
    e = (10 + 11 + 12) / 3                      # seed at bar 3 = SMA
    for x in xs[3:]:
        e = a * x + (1 - a) * e
    assert pine_ema(xs, n) == pytest.approx(e)
    assert pine_ema(xs[:3], n) == pytest.approx(11.0)
    assert pine_ema(xs[:2], n) is None


def test_pine_stdev_is_population_sigma_of_last_n():
    xs = [1.0, 2.0, 4.0, 4.0, 5.0, 7.0, 9.0]
    w = xs[-4:]
    m = sum(w) / 4
    assert pine_stdev(xs, 4) == pytest.approx(math.sqrt(sum((x - m) ** 2 for x in w) / 4))


# ── the candle grid lines up with the exchange session ───────────────────────
def test_mcx_buckets_start_at_0900_ist():
    t = _ist(2026, 10, 5, 14, 7)
    assert bucket_start(t, 900, 9 * 60) == _ist(2026, 10, 5, 14, 0)
    assert bucket_start(t, 3600, 9 * 60) == _ist(2026, 10, 5, 14, 0)
    # 4 H on MCX: 09:00, 13:00, 17:00, 21:00
    assert bucket_start(t, 14400, 9 * 60) == _ist(2026, 10, 5, 13, 0)
    assert bucket_start(_ist(2026, 10, 5, 22, 59), 14400, 9 * 60) == _ist(2026, 10, 5, 21, 0)


def test_nse_hourly_candles_start_at_0915():
    assert bucket_start(_ist(2026, 10, 5, 10, 20), 3600, 9 * 60 + 15) == _ist(2026, 10, 5, 10, 15)


def test_align_legs_carries_a_quiet_leg_forward():
    a = [(_ist(2026, 10, 5, 9, 0), 100.0), (_ist(2026, 10, 5, 9, 15), 101.0),
         (_ist(2026, 10, 5, 9, 30), 102.0)]
    b = [(_ist(2026, 10, 5, 9, 0), 90.0), (_ist(2026, 10, 5, 9, 30), 93.0)]   # no 09:15 trade
    rows = align_legs(a, b, 900, 540)
    assert [(ca, cb) for _, ca, cb in rows] == [(100.0, 90.0), (101.0, 90.0), (102.0, 93.0)]


# ── the store: live candles, current candle included, persistence ────────────
def test_bands_include_the_current_candle(tmp_path):
    c = SpreadCandles(persist_path=tmp_path / "c.json", key_provider=lambda: "A|B")
    t0 = _ist(2026, 10, 5, 9, 0)
    spreads = [10, 12, 11, 13, 12, 14]
    for i, sp in enumerate(spreads):
        c.update(t0 + i * 900 + 60, 100.0 + sp, 100.0)       # one tick per 15-min candle
    b = c.bands("15m", 3, 1.0)
    assert b["ready"] and b["count"] == 6
    assert b["mean"] == pytest.approx(pine_ema([float(x) for x in spreads], 3))
    assert b["std"] == pytest.approx(pine_stdev([float(x) for x in spreads], 3))
    # a new tick in the SAME candle moves the bands (it is the current candle's close)
    c.update(t0 + 5 * 900 + 400, 100.0 + 20, 100.0)
    b2 = c.bands("15m", 3, 1.0)
    assert b2["count"] == 6
    assert b2["mean"] == pytest.approx(pine_ema([10.0, 12, 11, 13, 12, 20], 3))


def test_candles_survive_a_restart_and_a_pair_change_starts_afresh(tmp_path):
    key = {"k": "A|B"}
    p = tmp_path / "c.json"
    c = SpreadCandles(persist_path=p, key_provider=lambda: key["k"])
    t0 = _ist(2026, 10, 5, 9, 0)
    for i in range(25):
        c.update(t0 + i * 900, 105.0 + i % 3, 100.0)
    c.save()
    again = SpreadCandles(persist_path=p, key_provider=lambda: key["k"])
    assert again.counts()["15m"] == 25
    assert again.bands("15m", 20)["mean"] == pytest.approx(c.bands("15m", 20)["mean"])
    key["k"] = "C|D"
    other = SpreadCandles(persist_path=p, key_provider=lambda: key["k"])
    assert other.counts()["15m"] == 0                       # someone else's candles


def test_backfill_fills_history_and_the_live_candle_keeps_live_prices(tmp_path):
    now = _ist(2026, 10, 5, 12, 5)
    hist_a = [(_ist(2026, 10, 5, 9, 0) + i * 900, 200.0 + i) for i in range(13)]
    hist_b = [(t, 100.0) for t, _ in hist_a]
    calls = []

    def history(tf, frm, to):
        calls.append(tf)
        return {"a": hist_a, "b": hist_b} if tf == "15m" else None

    c = SpreadCandles(persist_path=tmp_path / "c.json", history_provider=history,
                      key_provider=lambda: "A|B", clock=lambda: now)
    c.update(now, 300.0, 100.0)                              # the live 12:00 candle
    c._backfill(force=True)
    closes = [x for _, x in c.closes("15m")]
    assert len(closes) == 13
    assert closes[-1] == 200.0                              # 12:00 candle: live price kept
    assert closes[0] == 100.0 and closes[-2] == 111.0
    assert c.status["15m"]["state"] == "ok"
    assert c.status["5m"]["state"] == "failed"              # said plainly, not hidden
    # 1 H / 4 H are BUILT from the 15-min history (one fetch), not fetched
    assert calls.count("15m") == 1 and "1h" not in calls and "4h" not in calls
    assert c.status["1h"]["state"] == "ok" and "15-min" in c.status["1h"]["detail"]
    # 15-min 09:00 … 12:00 → hours 09, 10, 11, 12 (12:00 is the live candle)
    assert [t for t, _ in c.closes("1h")] == [_ist(2026, 10, 5, h, 0) for h in (9, 10, 11, 12)]
    assert [x for _, x in c.closes("1h")][:3] == [103.0, 107.0, 111.0]   # each hour's last 15-min close


# ── the signal engine in candle mode ─────────────────────────────────────────
def _engine(tmp_path, params, book=None):
    c = SpreadCandles(persist_path=tmp_path / "c.json", key_provider=lambda: "A|B")
    eng = SignalEngine(prices_provider=lambda: (None, None), params_provider=lambda: params,
                       book_provider=(lambda: book) if book else None, candles=c)
    return eng, c


def test_candle_mode_uses_ema_and_sigma_and_waits_for_n_candles(tmp_path):
    params = {"band_source": "candles", "band_timeframe": "15m", "band_length": 5,
              "window_minutes": 600, "min_signal_minutes": 999}
    eng, c = _engine(tmp_path, params)
    t0 = _ist(2026, 10, 5, 9, 0)
    for i in range(4):
        c.update(t0 + i * 900, 110.0 + i, 100.0)
        eng.push(110.0 + i, 100.0, ts=t0 + i * 900)
    s = eng.get_signal()
    assert s["band_source"] == "candles" and not s["ready"]          # 4 of 5 candles
    assert s["candles_have"] == 4 and s["candles_need"] == 5
    c.update(t0 + 4 * 900, 116.0, 100.0)
    eng.push(116.0, 100.0, ts=t0 + 4 * 900)
    s = eng.get_signal()
    closes = [10.0, 11.0, 12.0, 13.0, 16.0]
    assert s["ready"]                                   # the tick warm-up does not gate it
    assert s["mean"] == pytest.approx(pine_ema(closes, 5), abs=1e-3)
    assert s["std"] == pytest.approx(pine_stdev(closes, 5), abs=1e-5)
    assert s["zscore"] == pytest.approx((16.0 - s["mean"]) / s["std"], abs=1e-3)
    assert set(s["bands"]) == {"5m", "15m", "1h", "4h"}


def test_tick_mode_is_unchanged_by_the_candle_store(tmp_path):
    params = {"band_source": "ticks", "window_minutes": 600, "min_signal_minutes": 0}
    eng, c = _engine(tmp_path, params)
    t0 = _ist(2026, 10, 5, 9, 0)
    xs = [110.0, 112.0, 111.0, 115.0]
    for i, a in enumerate(xs):
        c.update(t0 + i, a, 100.0)
        eng.push(a, 100.0, ts=t0 + i)
    s = eng.get_signal()
    sp = [x - 100.0 for x in xs]
    m = sum(sp) / len(sp)
    sd = math.sqrt(sum((x - m) ** 2 for x in sp) / (len(sp) - 1))
    assert s["band_source"] == "ticks"
    assert s["mean"] == pytest.approx(m) and s["std"] == pytest.approx(sd)


# ── the algo keeps a position's bands, and times it out in candles ───────────
def _sig(**kw):
    base = {"ready": True, "zscore": 0.0, "std": 1.0, "mean": 0.0, "spread": 0.0,
            "sample_interval_sec": 0.5, "half_life": 0.0, "entry_zscore": 2.0,
            "exit_zscore": 0.0, "stop_zscore": 9.0, "leg_a": 100.0, "leg_b": 100.0,
            "band_source": "candles", "band_timeframe": "15m",
            "bands": {"15m": {"mean": 0.0, "std": 1.0, "ready": True},
                      "1h": {"mean": 0.0, "std": 1.0, "ready": True}}}
    base.update(kw)
    return base


def _algo(state, params, closes):
    return ArrowAutoTrader(
        signal_provider=lambda: state["sig"], params_provider=lambda: params,
        execute_fn=lambda d, l, **k: {"success": True, "dry_run": True, "results": []},
        close_fn=lambda d, l, **k: (closes.append(k.get("reason")) or
                                    {"success": True, "results": []}),
        clock=lambda: state["now"])


def test_position_keeps_its_timeframe_after_the_setting_changes():
    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 9.0, "lots": 1,
              "cooldown": 0, "lot_multiplier": 10.0, "enable_probability_filter": False,
              "min_edge_multiple": 0}
    state, closes = {"now": 1000.0, "sig": _sig(zscore=-2.5, spread=-2.5, mean=0.0)}, []
    algo = _algo(state, params, closes)
    algo._tick()
    assert algo._pos and algo._pos["band_tf"] == "15m"
    # switch to 1 H whose mean says "reverted", while the 15-min band still says "not yet"
    state["now"] += 5
    state["sig"] = _sig(zscore=0.5, spread=-1.0, mean=-1.5, band_timeframe="1h",
                        bands={"15m": {"mean": 0.0, "std": 1.0, "ready": True},
                               "1h": {"mean": -1.5, "std": 1.0, "ready": True}})
    algo._tick()
    assert algo._pos is not None and closes == []          # still judged on 15-min bands
    state["now"] += 5
    state["sig"]["spread"] = 0.2                            # 15-min z = +0.2 ≥ exit 0
    algo._tick()
    assert algo._pos is None and closes                     # exits on ITS bands


def test_max_hold_is_counted_in_candles_of_the_entry_timeframe():
    params = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 9.0, "lots": 1,
              "cooldown": 0, "lot_multiplier": 10.0, "enable_probability_filter": False,
              "min_edge_multiple": 0, "max_hold_candles": 2, "time_stop_half_lives": 3.0}
    state, closes = {"now": 1000.0, "sig": _sig(zscore=-2.5, spread=-2.5)}, []
    algo = _algo(state, params, closes)
    algo._tick()
    assert algo._pos
    state["sig"] = _sig(zscore=-1.5, spread=-1.5)
    state["now"] += 2 * 900 - 10                            # just under two 15-min candles
    algo._tick()
    assert algo._pos is not None
    state["now"] += 20
    algo._tick()
    assert algo._pos is None and closes and "time" in str(closes[-1])


# ── Arrow's candle host: parsing, paise → rupees, exchange fallback ──────────
def test_broker_candles_parse_arrow_rows_in_paise_and_remember_the_exchange(arrow_broker):
    b = arrow_broker
    b._sym_token["CRUDEOILM19OCT26F"] = "9001"
    calls = []

    def candle_data(ex, token, interval, frm, to, oi=False):
        ex = str(getattr(ex, "value", ex))
        calls.append((ex, token, interval, frm))
        if ex == "MCX":
            raise Exception("invalid exchange")
        return {"data": {"candles": [
            ["2026-10-05T09:00:00+0530", 548000, 549000, 547000, 548500, 120],
            ["2026-10-05T09:15:00+0530", 548500, 550000, 548000, 549900, 80]]}}

    b._client.candle_data = candle_data
    b.get_streamed_ltp = lambda syms: {"CRUDEOILM19OCT26F": 5498.0}
    t_to = _ist(2026, 10, 5, 10, 0)
    rows = b.get_candles("mcx_fo", "CRUDEOILM19OCT26F", "15m", t_to - 3 * 3600, t_to)
    assert rows == [(_ist(2026, 10, 5, 9, 0), 5485.0), (_ist(2026, 10, 5, 9, 15), 5499.0)]
    assert calls[0][:3] == ("MCX", "9001", "15min") and calls[1][0] == "MCXFO"
    assert calls[1][3] == "2026-10-05T07:00:00"          # IST, yyyy-MM-ddTHH:mm:ss
    calls.clear()
    b.get_candles("mcx_fo", "CRUDEOILM19OCT26F", "1h", t_to - 3 * 3600, t_to)
    assert calls[0][0] == "MCXFO" and calls[0][2] == "hour"   # remembered, Arrow's name


def test_broker_candles_keep_rupees_when_arrow_already_sends_rupees(arrow_broker):
    b = arrow_broker
    b._sym_token["X"] = "1"
    b._client.candle_data = lambda *a, **k: [["2026-10-05T09:00:00+0530", 5480, 5490, 5470, 5485, 1]]
    b.get_streamed_ltp = lambda syms: {"X": 5490.0}
    t = _ist(2026, 10, 5, 10, 0)
    assert b.get_candles("mcx_fo", "X", "15m", t - 3600, t)[0][1] == 5485.0


def test_a_restored_position_keeps_its_candle_timeframe(tmp_path):
    from arrow_statarb.core.trade_log import TradeLog
    tl = TradeLog(path=tmp_path / "trades.json")
    tl.record(action="OPEN", direction="SHORT_SPREAD", lots=1, spread=12.0, dry_run=True,
              status="DRY-RUN", source="algo", band_source="candles", band_tf="4h")
    op = tl.open_position()
    assert op["band_source"] == "candles" and op["band_tf"] == "4h"
    algo = ArrowAutoTrader(signal_provider=lambda: None, params_provider=lambda: {},
                           execute_fn=lambda *a, **k: {}, close_fn=lambda *a, **k: {})
    assert algo.restore_position(op)
    assert algo._pos["band_source"] == "candles" and algo._pos["band_tf"] == "4h"


def test_hourly_and_4h_candles_start_at_0900_like_tradingview(tmp_path):
    """Arrow's own hourly candles start at :15; built from 15-min candles the
    hours sit on TradingView's MCX grid (09:00, 10:00 …; 4 H 09/13/17/21)."""
    t0 = _ist(2026, 10, 5, 9, 0)
    q15 = [(t0 + i * 900, 100.0 + i) for i in range(56)]            # 09:00 → 22:45
    c = SpreadCandles(persist_path=tmp_path / "c.json", key_provider=lambda: "A|B",
                      history_provider=lambda tf, f, t: {"a": q15, "b": [(x, 50.0) for x, _ in q15]},
                      clock=lambda: t0 + 56 * 900)
    c._backfill(force=True)
    hours = [datetime.fromtimestamp(t, IST).strftime("%H:%M") for t, _ in c.closes("1h")]
    assert hours[0] == "09:00" and hours[1] == "10:00" and hours[-1] == "22:00"
    four = [datetime.fromtimestamp(t, IST).strftime("%H:%M") for t, _ in c.closes("4h")]
    assert four == ["09:00", "13:00", "17:00", "21:00"]
    assert c.closes("4h")[0][1] == (100.0 + 15) - 50.0             # close of 12:45, the 4 H's last 15 min


def test_old_saved_hourly_candles_are_dropped_and_rebuilt(tmp_path):
    import json as _json
    p = tmp_path / "c.json"
    p.write_text(_json.dumps({"key": "A|B", "bars": {
        "15m": [[_ist(2026, 10, 5, 9, 0), 101.0, 100.0]],
        "1h": [[_ist(2026, 10, 5, 9, 0), 105.0, 100.0]]}}))       # no "grid": old file
    c = SpreadCandles(persist_path=p, key_provider=lambda: "A|B")
    assert c.counts()["15m"] == 1 and c.counts()["1h"] == 0
