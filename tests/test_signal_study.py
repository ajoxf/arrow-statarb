"""Signal study: a PAPER copy of the live algo on the same signal, settled in
money — and nothing in it that can reach a real order or the real log."""

import inspect
import re

import pytest

from arrow_statarb.core import signal_study as ss
from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.signal_study import SignalStudy, summarize

MULT = 75.0


def _params(**over):
    p = {"entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1,
         "tick_interval": 0.5, "cooldown": 0, "lot_multiplier": MULT,
         "enable_probability_filter": False, "brokerage_per_lot": 10.0,
         "slippage_per_lot": 5.0, "time_stop_half_lives": 3.0,
         "confirmation_ticks": 3, "entry_mode": "reentry", "reentry_back": 0.5,
         "reentry_window_pct": 50.0}
    p.update(over)
    return p


class World:
    """A controllable signal + clock shared by the study (and a live algo)."""

    def __init__(self):
        self.t = 1_000_000.0
        self.z = 0.0
        self.spread = 100.0

    def sig(self):
        return {"zscore": self.z, "std": 1.0, "ready": True, "leg_a": 200.0 + self.spread,
                "leg_b": 200.0, "spread": self.spread, "mean": 100.0, "samples": 300,
                "half_life": 0.0, "half_life_sec": 0.0, "sample_interval_sec": 0.5,
                "entry_zscore": 2.0, "exit_zscore": 0.0, "stop_zscore": 4.0,
                "min_signal_minutes": 1.0, "span_minutes": 5.0, "regime": None,
                "regime_detail": {}}

    def clock(self):
        return self.t


def _study(world, path=None, **over):
    params = _params(**over)
    return SignalStudy(path, world.sig, lambda: params, clock=world.clock), params


def _drive(world, study, path_zs, algo=None, step=1.0):
    """z → spread 100 + 10·z. Returns the live algo's (entries, exits)."""
    for z in path_zs:
        world.z, world.spread = z, 100.0 + 10.0 * z
        study.tick()
        if algo is not None:
            algo._tick()
        world.t += step


def _rows(study):
    return study.report("all", now=2e9)["rows"]


# ── the paper algo IS the algo ───────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["reentry", "touch"])
def test_paper_and_live_algo_enter_and_exit_at_the_same_moments(mode):
    w = World()
    st, params = _study(w, entry_mode=mode)
    live = {"in": [], "out": []}
    algo = ArrowAutoTrader(
        signal_provider=w.sig, params_provider=lambda: params, clock=w.clock,
        execute_fn=lambda d, n: live["in"].append((w.t, d)) or {"success": True, "results": []},
        close_fn=lambda d, n, reason="": live["out"].append((w.t, reason)) or {"success": True, "results": []})
    path = [0.5, 2.5, 2.6, 2.7, 2.9, 2.1, 1.4, 0.9, -0.2, 0.3,
            -2.4, -2.6, -2.8, -3.1, -1.4, -0.4, 0.2, 2.2, 2.4, 2.6, 4.3]
    _drive(w, st, path, algo)
    rows = [r for r in reversed(_rows(st)) if r["status"] in ("open", "closed")]
    assert live["in"] and [(r["entry_ts"], "LONG_SPREAD" if r["side"] == "LONG" else "SHORT_SPREAD")
                           for r in rows] == live["in"]
    assert [(r["exit_ts"], r["exit_reason"]) for r in rows if r["status"] == "closed"] == live["out"]


# ── money ────────────────────────────────────────────────────────────────────

def test_a_short_back_to_the_mean_is_settled_in_money_with_the_algos_costs():
    w = World()
    st, params = _study(w)
    _drive(w, st, [2.5, 2.6, 2.7, 1.4, 0.6, -0.1])
    (r,) = _rows(st)
    assert r["status"] == "closed" and r["exit_reason"] == "target"
    assert r["entry_spread"] == 114.0 and r["exit_spread"] == 99.0
    assert r["gross"] == (114.0 - 99.0) * MULT
    expect_cost = st._algo._round_trip_cost(params, lots=1, ref_price=r["entry_leg_a"],
                                            ref_b=r["entry_leg_b"])
    assert r["costs"] == round(expect_cost, 2) and r["net"] == round(r["gross"] - expect_cost, 2)
    assert r["result"] == "won" and r["armed_z"] == 2.7 and r["entry_z"] == 1.4


def test_a_stop_is_lost_in_money():
    w = World()
    st, _ = _study(w)
    _drive(w, st, [2.5, 2.6, 2.7, 1.4, 4.2])
    (r,) = _rows(st)
    assert r["exit_reason"] == "stop" and r["result"] == "lost" and r["net"] < 0


def test_unknown_lot_multiplier_settles_with_no_money_and_no_result():
    w = World()
    st, _ = _study(w, lot_multiplier=0)            # the algo's "not known"
    _drive(w, st, [2.5, 2.6, 2.7, 1.4, -0.1])
    (r,) = _rows(st)
    assert r["status"] == "closed" and r["net"] is None and r["result"] is None
    s = summarize(_rows(st))
    assert s["unmeasured"] == 1 and s["win_rate"] is None and s["net"] is None


def test_mae_and_mfe_follow_the_closing_side():
    w = World()
    st, _ = _study(w)
    _drive(w, st, [2.5, 2.6, 2.7, 1.4, 1.9, 0.5, -0.1])
    (r,) = _rows(st)
    assert r["mae_pts"] == -5.0 and r["mfe_pts"] == 15.0      # short: up hurts


# ── armed, never back, filtered ──────────────────────────────────────────────

def test_a_stretch_that_never_came_back_is_a_row_without_money():
    w = World()
    st, _ = _study(w)
    _drive(w, st, [2.5, 2.6, 2.7, 3.2, 0.3])
    (r,) = _rows(st)
    assert r["status"] == "never_back" and r["peak_z"] == 3.2 and r["net"] is None
    s = summarize(_rows(st))
    assert s["never_back"] == 1 and s["won"] == s["lost"] == 0


def test_a_blocked_signal_carries_the_algos_own_words_once_per_run():
    w = World()
    st, _ = _study(w, entry_mode="touch", max_entry_zscore=2.5)
    _drive(w, st, [2.6, 2.7, 2.8, 2.9, 2.9, 2.9])
    (r,) = _rows(st)
    assert r["status"] == "filtered" and "entry cap" in r["filter_reason"]
    assert r["blocked_samples"] == 4


def test_the_paper_algo_halts_on_its_own_results_not_the_real_ones():
    w = World()
    st, _ = _study(w, entry_mode="touch", max_daily_loss=100.0, day_pnl=-1e6, loss_streak=99,
                   loss_streak_pause_at=2)
    _drive(w, st, [2.5, 2.6, 2.7])
    assert _rows(st)[0]["status"] == "open"     # the real day's loss does not halt it


# ── restart ──────────────────────────────────────────────────────────────────

def test_an_open_paper_position_survives_a_restart(tmp_path):
    path = tmp_path / "study.json"
    w = World()
    a, params = _study(w, path)
    _drive(w, a, [2.5, 2.6, 2.7, 1.4])
    b = SignalStudy(path, w.sig, lambda: params, clock=w.clock)
    _drive(w, b, [-0.1])
    (r,) = _rows(b)
    assert r["status"] == "closed" and r["exit_reason"] == "target"
    assert r["crossed_restart"] is True and r["result"] == "won"


def test_an_arming_does_not_survive_a_restart(tmp_path):
    path = tmp_path / "study.json"
    w = World()
    a, params = _study(w, path)
    _drive(w, a, [2.5, 2.6, 2.7])
    b = SignalStudy(path, w.sig, lambda: params, clock=w.clock)
    _drive(w, b, [1.4])
    (r,) = _rows(b)
    assert r["status"] == "never_back" and r["lost_reason"] == "restart"


def test_ranges_filter_by_entry_time():
    w = World()
    st, _ = _study(w, entry_mode="touch")
    _drive(w, st, [2.5, 2.6, 2.7, -0.1])
    w.t += 20 * 86400
    _drive(w, st, [2.5, 2.6, 2.7, -0.1])
    now = w.t
    assert len(st.report("1d", now=now)["rows"]) == 1
    assert len(st.report("30d", now=now)["rows"]) == 2
    assert st.report("bogus", now=now)["range"] == "1d"


# ── a reading must not reach a decision ──────────────────────────────────────

def test_the_study_holds_no_real_order_path():
    src = inspect.getsource(ss)
    for word in ("submit_order", "cancel_order", "_spread_execute", "_spread_close",
                 "trade_log", "TradeLog", "broker", "executor"):
        assert not re.search(r"\b%s\b" % word, src), word
    st, _ = _study(World())
    assert st._algo._execute == st._paper_open and st._algo._close == st._paper_close


def test_the_live_algo_and_order_path_never_read_the_study():
    from arrow_statarb.core import algo, executor
    for mod in (algo, executor):
        assert "signal_study" not in inspect.getsource(mod)


def test_the_paper_algo_never_writes_an_info_line_to_the_log():
    from loguru import logger
    seen = []
    hid = logger.add(lambda m: seen.append(m), level="INFO")
    try:
        w = World()
        st, _ = _study(w, entry_mode="touch")
        _drive(w, st, [2.5, 2.6, 2.7, -0.1])
    finally:
        logger.remove(hid)
    assert _rows(st)[0]["status"] == "closed"
    assert not [m for m in seen if "ArrowAlgo" in m]


def test_the_app_serves_the_study(tmp_path):
    import arrow_statarb.web.app as appmod
    from arrow_statarb.config.config import Config
    (tmp_path / "s.yaml").write_text("mode: dry_run\nbroker:\n  name: arrow\n")
    appmod.TRADES_FILE = tmp_path / "trades.json"
    app, _ = appmod.create_app(Config(tmp_path / "s.yaml"))
    app.extensions["arrow"]["signal"].stop()
    d = app.test_client().get("/api/analysis/signals?range=7d").get_json()
    assert d["range"] == "7d" and d["rows"] == [] and d["summary"]["win_rate"] is None
    assert d["rules"]["entry_mode"] == "reentry" and d["rules"]["band_source"] in ("ticks", "candles")


def test_a_blocked_run_keeps_its_first_moment_and_records_the_spread_and_peak():
    w = World()
    st, _ = _study(w, entry_mode="touch", max_entry_zscore=2.5)
    _drive(w, st, [2.6, 2.7, 2.8, 2.9, 3.1, 2.9])
    (r,) = _rows(st)
    # the first blocked sample (z 2.8 → spread 128) names the row ...
    assert r["entry_z"] == 2.8 and r["entry_spread"] == 128.0
    assert "2.80" in r["filter_reason"]            # ... and its reason says the same z
    assert r["peak_z"] == 3.1 and r["blocked_samples"] == 4


def test_an_off_side_signal_records_the_spread_it_would_have_traded():
    w = World()
    st, _ = _study(w, entry_mode="touch", trade_direction="sell_only")
    _drive(w, st, [-2.5, -2.6, -2.7])
    (r,) = _rows(st)
    assert r["status"] == "filtered" and r["side"] == "LONG"
    assert r["entry_z"] == -2.5 and r["entry_spread"] == 75.0
    assert "-2.50" in r["filter_reason"] and r["peak_z"] == -2.7


def test_filter_reasons_bucket_by_gate_not_by_z():
    rows = [{"status": "filtered", "filter_reason": f"buy spread at z {z:+.2f} — LONG entries are off (Sell spread only)"}
            for z in (-2.51, -2.58, -3.0)]
    assert summarize(rows)["filter_reasons"] == {"buy spread at z … — LONG entries are off": 3}
