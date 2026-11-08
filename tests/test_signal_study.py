"""Signal study: arrow's algo rules replayed as a reading — entries, exits,
outcomes in money — and nothing that can reach a decision."""

import inspect
import json

import pytest

from arrow_statarb.core import signal_study as ss
from arrow_statarb.core.signal_study import SignalStudy, summarize

LOT = 75.0


def _p(**over):
    p = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0,
         "confirmation_ticks": 3, "time_stop_half_lives": 3.0, "cooldown": 300.0,
         "lots": 1, "lot_multiplier": LOT, "brokerage_per_lot": 10.0,
         "enable_probability_filter": False}
    p.update(over)
    return p


def _study(path=None, **over):
    params = _p(**over)
    return SignalStudy(path, lambda: params), params


def _feed(study, zs_spreads, t0=0.0, step=1.0, half_life=0.0):
    t = t0
    for z, spread in zs_spreads:
        study.observe({"ts": t, "z": z, "spread": spread, "leg_a": 100.0 + spread,
                       "leg_b": 100.0, "std": 1.0, "half_life": half_life,
                       "sample_interval": 0.5})
        t += step
    return t


def _rows(study):
    return study.report("all", now=1e12)["rows"]


# ── entry ────────────────────────────────────────────────────────────────────

def test_a_short_enters_after_the_confirmation_ticks_at_the_confirming_sample():
    st, _ = _study()
    _feed(st, [(2.1, 10.0), (2.3, 11.0), (2.4, 12.0)])
    (r,) = _rows(st)
    assert r["side"] == "SHORT" and r["status"] == "open"
    assert r["entry_ts"] == 2.0 and r["entry_spread"] == 12.0 and r["entry_z"] == 2.4


def test_control_fewer_samples_than_confirmation_record_nothing():
    st, _ = _study()
    _feed(st, [(2.1, 10.0), (2.3, 11.0), (1.0, 5.0)])
    assert _rows(st) == []
    st1, _ = _study(confirmation_ticks=1)
    _feed(st1, [(2.1, 10.0)])
    assert len(_rows(st1)) == 1


# ── exits and money ──────────────────────────────────────────────────────────

def test_a_short_reverting_to_the_mean_is_won_in_money():
    st, _ = _study()
    _feed(st, [(2.5, 12.0)] * 3 + [(1.0, 8.0), (-0.1, 4.0)])
    (r,) = _rows(st)
    assert r["exit_reason"] == "target" and r["exit_spread"] == 4.0
    assert r["gross"] == (12.0 - 4.0) * LOT                  # a short gains as it falls
    assert r["costs"] == 10.0 * 1 * 4
    assert r["net"] == r["gross"] - r["costs"] and r["result"] == "won"


def test_a_long_reverting_up_is_won_in_money():
    st, _ = _study()
    _feed(st, [(-2.5, -12.0)] * 3 + [(-1.0, -8.0), (0.1, -4.0)])
    (r,) = _rows(st)
    assert r["side"] == "LONG" and r["exit_reason"] == "target"
    assert r["gross"] == (-4.0 - -12.0) * LOT and r["result"] == "won"


def test_a_shorts_stop_sits_above_and_does_not_fire_on_its_first_poll():
    st, _ = _study()
    _feed(st, [(2.5, 12.0)] * 3 + [(2.6, 12.5), (3.0, 13.0)])
    assert _rows(st)[0]["status"] == "open"                  # still holding
    _feed(st, [(4.2, 18.0)], t0=10)
    (r,) = _rows(st)
    assert r["exit_reason"] == "stop" and r["result"] == "lost"
    assert r["net"] == (12.0 - 18.0) * LOT - 40.0


def test_a_longs_stop_sits_below():
    st, _ = _study()
    _feed(st, [(-2.5, -12.0)] * 3 + [(-4.2, -18.0)])
    (r,) = _rows(st)
    assert r["exit_reason"] == "stop" and r["result"] == "lost"


def test_stop_is_checked_before_target_but_the_result_is_decided_in_money():
    # one sample: a short's z leaps from +3 to −4.5 — reverted AND |z| ≥ stop.
    # The algo checks STOP first, so the exit is booked as a stop; the spread
    # fell, so in money it is a WIN. Booking it "target" would be the happier
    # reading; calling it "lost" because a stop fired would be the wrong count.
    st, _ = _study()
    _feed(st, [(2.5, 12.0)] * 3 + [(-4.5, 2.0)])
    (r,) = _rows(st)
    assert r["exit_reason"] == "stop"
    assert r["net"] > 0 and r["result"] == "won"


def test_time_stop_after_the_set_number_of_half_lives():
    st, _ = _study()
    # half-life 20 samples × 0.5 s = 10 s; 3 half-lives = 30 s
    _feed(st, [(2.5, 12.0)] * 3 + [(1.5, 11.0)] * 40, half_life=20.0)
    (r,) = _rows(st)
    assert r["exit_reason"] == "time_stop" and r["held_sec"] == 30.0


def test_mae_and_mfe_are_in_the_positions_own_direction():
    st, _ = _study()
    _feed(st, [(2.5, 12.0)] * 3 + [(3.0, 14.0), (1.0, 9.0), (-0.1, 10.0)])
    (r,) = _rows(st)
    assert r["mae_pts"] == -2.0 and r["mfe_pts"] == 3.0       # short: up hurts
    assert r["mae_inr"] == -2.0 * LOT and r["mfe_inr"] == 3.0 * LOT


# ── gates, each with a control ───────────────────────────────────────────────

def test_cooldown_after_an_exit_filters_the_next_run_once():
    st, _ = _study()
    t = _feed(st, [(2.5, 12.0)] * 3 + [(-0.1, 4.0)])
    _feed(st, [(2.5, 12.0)] * 6, t0=t)
    rows = _rows(st)
    assert [r["status"] for r in rows] == ["filtered", "closed"]
    assert rows[0]["filter_reason"] == "cooldown" and rows[0]["blocked_samples"] == 4
    assert rows[0]["net"] is None


def test_control_without_cooldown_the_next_run_is_taken():
    st, _ = _study(cooldown=0.0)
    t = _feed(st, [(2.5, 12.0)] * 3 + [(-0.1, 4.0)])
    _feed(st, [(2.5, 12.0)] * 3, t0=t)
    assert [r["status"] for r in _rows(st)] == ["open", "closed"]


def test_the_probability_filter_blocks_with_its_reason():
    st, _ = _study(enable_probability_filter=True, min_win_probability=0.999)
    _feed(st, [(2.5, 12.0)] * 3)
    (r,) = _rows(st)
    assert r["status"] == "filtered" and r["filter_reason"]


def test_control_with_the_filter_off_the_same_signal_is_taken():
    st, _ = _study(enable_probability_filter=False, min_win_probability=0.999)
    _feed(st, [(2.5, 12.0)] * 3)
    assert _rows(st)[0]["status"] == "open"


def test_one_position_at_a_time():
    st, _ = _study()
    _feed(st, [(2.5, 12.0)] * 3 + [(3.0, 13.0)] * 10)     # confirmed again and again
    assert len(_rows(st)) == 1                            # the open short is the only row


# ── unmeasured is not zero ───────────────────────────────────────────────────

def test_unknown_lot_size_settles_with_no_money_and_counts_in_no_tally():
    st, _ = _study(lot_multiplier=None)
    _feed(st, [(2.5, 12.0)] * 3 + [(-0.1, 4.0)])
    (r,) = _rows(st)
    assert r["status"] == "closed" and r["net"] is None and r["result"] is None
    s = summarize(_rows(st))
    assert s["won"] == s["lost"] == 0 and s["unmeasured"] == 1
    assert s["win_rate"] is None and s["net"] is None and s["expectancy"] is None


def test_an_empty_study_measures_nothing():
    s = summarize([])
    for k in ("win_rate", "net", "avg_win", "avg_loss", "profit_factor",
              "expectancy", "avg_hold_sec", "max_drawdown"):
        assert s[k] is None, k


def test_summary_in_money():
    st, _ = _study(cooldown=0.0)
    t = _feed(st, [(2.5, 12.0)] * 3 + [(-0.1, 4.0)])            # +600 − 40 = +560
    _feed(st, [(2.5, 12.0)] * 3 + [(4.5, 16.0)], t0=t)          # −300 − 40 = −340
    s = summarize(_rows(st))
    assert (s["won"], s["lost"]) == (1, 1) and s["win_rate"] == 50.0
    assert s["net"] == 220.0 and s["profit_factor"] == round(560 / 340, 2)
    assert s["max_drawdown"] == 340.0 and s["expectancy"] == 110.0
    assert s["by_exit"]["stop"]["lost"] == 1 and s["by_exit"]["target"]["won"] == 1


# ── survives a restart ───────────────────────────────────────────────────────

def test_an_open_study_position_survives_a_restart_and_is_followed(tmp_path):
    path = tmp_path / "study.json"
    a, params = _study(path)
    _feed(a, [(2.5, 12.0)] * 3)
    b = SignalStudy(path, lambda: params)
    _feed(b, [(-0.1, 4.0)], t0=100)
    (r,) = _rows(b)
    assert r["status"] == "closed" and r["result"] == "won"
    assert r["crossed_restart"] is True                       # z restarted with the window


def test_restart_does_not_duplicate_a_filtered_run(tmp_path):
    path = tmp_path / "study.json"
    a, params = _study(path, enable_probability_filter=True, min_win_probability=0.999)
    _feed(a, [(2.5, 12.0)] * 3)
    b = SignalStudy(path, lambda: params)
    _feed(b, [(2.5, 12.0)] * 3, t0=10)
    rows = _rows(b)
    # one row, not two; confirmation starts again after a restart (the
    # arming never outlives the z window), so only the confirmed sample adds
    assert len(rows) == 1 and rows[0]["blocked_samples"] == 2


# ── date ranges ──────────────────────────────────────────────────────────────

def test_ranges_filter_by_entry_time():
    st, _ = _study(cooldown=0.0)
    day = 86400.0
    t = _feed(st, [(2.5, 12.0)] * 3 + [(-0.1, 4.0)], t0=0.0)
    _feed(st, [(2.5, 12.0)] * 3 + [(-0.1, 4.0)], t0=20 * day)
    now = 20 * day + 100
    assert len(st.report("1d", now=now)["rows"]) == 1
    assert len(st.report("30d", now=now)["rows"]) == 2
    assert len(st.report("all", now=now)["rows"]) == 2
    assert st.report("bogus", now=now)["range"] == "1d"


# ── a reading must not reach a decision ──────────────────────────────────────

def test_the_study_holds_no_order_path():
    import re
    src = inspect.getsource(ss)
    for word in ("submit_order", "cancel_order", "execute_fn", "close_fn",
                 "_execute", "_close", "broker", "trade_log", "TradeLog", "ArrowAutoTrader"):
        assert not re.search(r"\b%s\b" % word, src), word


def test_the_algo_and_the_order_path_never_read_the_study():
    from arrow_statarb.core import algo, executor
    for mod in (algo, executor):
        src = inspect.getsource(mod)
        assert "signal_study" not in src and "SignalStudy" not in src


def test_an_observer_cannot_change_or_break_the_engine():
    from arrow_statarb.core.signal import SignalEngine
    params = {"window_minutes": 120.0, "sample_interval_sec": 0.5, "min_signal_minutes": 0.0}
    plain = SignalEngine(lambda: (None, None), lambda: params)
    watched = SignalEngine(lambda: (None, None), lambda: params)

    def vandal(sample):
        sample["z"] = 99.0; sample["spread"] = -1.0
        raise RuntimeError("an observer blowing up")
    watched.add_observer(vandal)
    for i in range(200):
        s = (i % 17) - 8.0
        plain.push(100.0 + s, 100.0, ts=float(i))
        watched.push(100.0 + s, 100.0, ts=float(i))
    assert watched.get_signal() == plain.get_signal()
    assert watched.get_excursions()["touch_2_up"] == plain.get_excursions()["touch_2_up"]


def test_the_app_feeds_the_study_and_serves_it(tmp_path):
    import arrow_statarb.web.app as appmod
    from arrow_statarb.config.config import Config
    (tmp_path / "s.yaml").write_text(
        "mode: dry_run\nbroker:\n  name: arrow\nsignal:\n  window_minutes: 120\n"
        "  sample_interval_sec: 0.5\n  min_signal_minutes: 0\n  confirmation_ticks: 1\n"
        "filters:\n  enable_probability_filter: false\n")
    appmod.TRADES_FILE = tmp_path / "trades.json"
    app, _ = appmod.create_app(Config(tmp_path / "s.yaml"))
    eng = app.extensions["arrow"]["signal"]
    eng.stop()
    for i in range(300):
        eng.push(100.0 + (i % 10) * 0.1, 100.0, ts=1_000_000.0 + i)
    eng.push(130.0, 100.0, ts=1_000_400.0)                 # a stretch
    d = app.test_client().get("/api/analysis/signals?range=all").get_json()
    assert d["summary"]["signals"] >= 1 and d["rows"][0]["side"] == "SHORT"
    assert d["rules"]["entry_zscore"] == 2.0 and d["rules"]["window_minutes"] == 120
    # no broker → lot size unknown → no money, not ₹ at ×1
    assert d["rows"][0]["lot_size"] is None
