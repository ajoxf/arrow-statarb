"""Armed entries (entry_mode = reentry): a stretch to ±entry ARMS; the entry
comes on the way back inside the band, within the re-entry window — in the
live algo and in the Analysis study's paper copy of it, from one rule
(core/arming.py), each side on its own executable z."""

import pytest

from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.arming import Arming, reentry_levels

RE = {"entry_zscore": 2.0, "confirmation_ticks": 3, "entry_mode": "reentry",
      "reentry_back": 0.5, "reentry_window_pct": 50.0}


def _steps(arm, zs, p=RE):
    return [arm.step(z, float(i), p) for i, z in enumerate(zs)]


# ── the rule ─────────────────────────────────────────────────────────────────

def test_levels_are_entry_minus_back_and_the_window_floor():
    assert reentry_levels(RE) == (1.5, 0.75)
    assert reentry_levels(dict(RE, reentry_window_pct=100)) == (1.5, 0.0)


def test_a_stretch_arms_and_the_way_back_triggers_short():
    out = _steps(Arming(), [2.5, 2.6, 2.7, 2.2, 1.8, 1.4])
    assert [o["event"] for o in out][:3] == [None, None, "armed"]
    assert [o["trigger"] for o in out] == [None] * 5 + ["SHORT"]


def test_long_side_mirrors():
    out = _steps(Arming(), [-2.5, -2.6, -2.7, -1.6, -1.45])
    assert out[-1]["trigger"] == "LONG" and out[-2]["trigger"] is None


def test_each_side_reads_its_own_executable_z():
    a, p = Arming(), dict(RE, confirmation_ticks=1)
    # the SELL side is stretched, the BUY side is not: a SHORT arms
    assert a.step(2.4, 0.0, p, z_long=1.9)["event"] == "armed" and a.armed == "SHORT"
    # the SELL side comes back to 1.4 — the BUY side's 1.0 is not what decides
    assert a.step(1.4, 1.0, p, z_long=1.0)["trigger"] == "SHORT"
    # a side that cannot be traded (None) neither arms nor triggers
    b = Arming()
    assert b.step(None, 0.0, p, z_long=-2.5)["event"] == "armed" and b.armed == "LONG"


def test_a_trend_riding_the_band_never_enters():
    out = _steps(Arming(), [2.5, 2.6, 2.7] + [2.8 + 0.1 * i for i in range(50)])
    assert all(o["trigger"] is None for o in out)


def test_through_the_floor_without_an_entry_is_lost():
    a = Arming()
    out = _steps(a, [2.5, 2.6, 2.7, 0.5])
    assert out[-1]["trigger"] is None and out[-1]["event"] == "lost"
    assert out[-1]["lost"]["peak_z"] == 2.7 and a.armed is None


def test_arming_needs_the_confirmation_ticks():
    assert all(o["trigger"] is None for o in _steps(Arming(), [2.5, 2.6, 1.4]))


def test_control_touch_mode_enters_at_the_stretch():
    out = _steps(Arming(), [2.5, 2.6, 2.7], dict(RE, entry_mode="touch"))
    assert out[-1]["trigger"] == "SHORT"


# ── the live algo ────────────────────────────────────────────────────────────

def _algo(**over):
    calls = {"execute": [], "close": []}
    state = {"z": 0.0, "z_sell": None, "z_buy": None}
    params = {"exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1, "tick_interval": 0.5,
              "cooldown": 0, "lot_multiplier": 75.0, "enable_probability_filter": False,
              "brokerage_per_lot": 10.0, "slippage_per_lot": 5.0,
              "time_stop_half_lives": 3.0, **RE}
    params.update(over)

    def sig():
        d = {"zscore": state["z"], "std": 1.0, "ready": True, "leg_a": 110.0, "leg_b": 10.0,
             "spread": 100.0, "mean": 100.0, "samples": 300, "half_life": 0.0,
             "half_life_sec": 0.0, "sample_interval_sec": 0.5, "entry_zscore": 2.0,
             "exit_zscore": 0.0, "stop_zscore": 4.0, "min_signal_minutes": 1.0,
             "span_minutes": 5.0, "regime": None, "regime_detail": {}}
        if state["z_sell"] is not None:
            d.update(z_sell=state["z_sell"], z_buy=state["z_buy"], bid_a=1.0, ask_a=1.0,
                     bid_b=1.0, ask_b=1.0, sell_spread=100.0, buy_spread=100.0)
        return d

    algo = ArrowAutoTrader(
        signal_provider=sig, params_provider=lambda: params,
        execute_fn=lambda d, n: calls["execute"].append(d) or
        {"success": True, "dry_run": True, "results": []},
        close_fn=lambda d, n: calls["close"].append(d) or {"success": True, "results": []})
    return algo, state, calls, params


def _run(algo, state, zs):
    for z in zs:
        state["z"] = z
        algo._tick()


def test_live_reentry_waits_at_the_touch_and_enters_on_the_way_back():
    algo, state, calls, _ = _algo()
    _run(algo, state, [2.5, 2.6, 2.7, 2.2])
    assert calls["execute"] == []
    st = algo.get_state()["status"]
    assert st.startswith("ARMED SHORT") and "1.50" in st and "0.75" in st
    _run(algo, state, [1.4])
    assert calls["execute"] == ["SHORT_SPREAD"]
    assert "on re-entry" in algo.get_state()["status"]


def test_control_live_touch_mode_enters_at_the_touch():
    algo, state, calls, _ = _algo(entry_mode="touch")
    _run(algo, state, [2.5, 2.6, 2.7])
    assert calls["execute"] == ["SHORT_SPREAD"]


def test_live_a_trend_and_a_gap_through_the_floor_never_trade():
    algo, state, calls, _ = _algo()
    _run(algo, state, [2.5, 2.6, 2.7] + [3.0] * 20 + [0.3, 1.4])
    assert calls["execute"] == []


def test_live_with_a_book_a_short_arms_and_triggers_on_the_sell_side():
    algo, state, calls, _ = _algo(confirmation_ticks=1)
    for zs, zb in ((2.4, 1.8), (1.4, 0.9)):
        state.update(z=(zs + zb) / 2, z_sell=zs, z_buy=zb)
        algo._tick()
    assert calls["execute"] == ["SHORT_SPREAD"]


def test_live_sell_only_never_arms_a_long():
    algo, state, calls, _ = _algo(trade_direction="sell_only")
    _run(algo, state, [-2.5, -2.6, -2.7, -1.4])
    assert calls["execute"] == [] and algo.get_state().get("armed") is None


def test_live_a_blocked_reentry_stays_armed_and_enters_inside_the_window():
    algo, state, calls, params = _algo(max_daily_loss=100.0, day_pnl=-500.0)
    _run(algo, state, [2.5, 2.6, 2.7, 1.4])
    assert calls["execute"] == [] and "daily loss" in algo.get_state()["status"]
    params["day_pnl"] = 0.0
    _run(algo, state, [1.3])
    assert calls["execute"] == ["SHORT_SPREAD"]


def test_live_the_entry_cap_reads_the_depth_of_the_stretch():
    algo, state, calls, _ = _algo(max_entry_zscore=3.0)
    _run(algo, state, [2.5, 2.6, 3.4, 1.4])                 # peaked at 3.4 > cap 3.0
    assert calls["execute"] == [] and "entry cap" in algo.get_state()["status"]


def test_control_under_the_cap_it_enters():
    algo, state, calls, _ = _algo(max_entry_zscore=3.0)
    _run(algo, state, [2.5, 2.6, 2.8, 1.4])
    assert calls["execute"] == ["SHORT_SPREAD"]


def test_live_arming_does_not_survive_a_restart():
    a, state, calls, _ = _algo()
    _run(a, state, [2.5, 2.6, 2.7])
    restarted, state2, calls2, _ = _algo()
    _run(restarted, state2, [1.4])
    assert calls2["execute"] == []


def test_control_the_same_process_does_enter():
    a, state, calls, _ = _algo()
    _run(a, state, [2.5, 2.6, 2.7, 1.4])
    assert calls["execute"] == ["SHORT_SPREAD"]


def test_settings_default_to_armed_and_switch_to_touch(tmp_path):
    import arrow_statarb.web.app as appmod
    from arrow_statarb.config.config import Config
    (tmp_path / "s.yaml").write_text("mode: dry_run\nbroker:\n  name: arrow\n")
    appmod.TRADES_FILE = tmp_path / "trades.json"
    app, _ = appmod.create_app(Config(tmp_path / "s.yaml"))
    app.extensions["arrow"]["signal"].stop()
    c = app.test_client()
    sig = c.get("/api/settings").get_json()["signal"]
    assert (sig["entry_mode"], sig["reentry_back"], sig["reentry_window_pct"]) == ("reentry", 0.5, 50.0)
    c.post("/api/settings", json={"signal": {"entry_mode": "touch", "reentry_back": 0.3}})
    sig = c.get("/api/settings").get_json()["signal"]
    assert sig["entry_mode"] == "touch" and sig["reentry_back"] == 0.3
    c.post("/api/settings", json={"signal": {"entry_mode": "bogus"}})
    assert c.get("/api/settings").get_json()["signal"]["entry_mode"] == "touch"
