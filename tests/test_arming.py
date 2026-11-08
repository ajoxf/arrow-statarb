"""Armed entries (entry_mode = reentry): a stretch to ±entry ARMS; the entry
comes on the way back inside the band, within the re-entry window — in the
live algo and in the Analysis study alike, from one rule (core/arming.py)."""

import pytest

from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.arming import Arming, reentry_levels
from arrow_statarb.core.signal_study import SignalStudy, summarize

RE = {"entry_zscore": 2.0, "confirmation_ticks": 3, "entry_mode": "reentry",
      "reentry_back": 0.5, "reentry_window_pct": 50.0}


def _steps(arm, zs, p=RE):
    return [arm.step(z, float(i), p) for i, z in enumerate(zs)]


# ── the rule ─────────────────────────────────────────────────────────────────

def test_levels_are_arrows_entry_minus_back_and_the_window_floor():
    assert reentry_levels(RE) == (1.5, 0.75)
    assert reentry_levels(dict(RE, reentry_window_pct=100)) == (1.5, 0.0)


def test_a_stretch_arms_and_the_way_back_triggers_short():
    out = _steps(Arming(), [2.5, 2.6, 2.7, 2.2, 1.8, 1.4])
    assert [o["event"] for o in out][:3] == [None, None, "armed"]
    assert [o["trigger"] for o in out] == [None] * 5 + ["SHORT"]


def test_long_side_mirrors():
    out = _steps(Arming(), [-2.5, -2.6, -2.7, -1.6, -1.45])
    assert out[-1]["trigger"] == "LONG" and out[-2]["trigger"] is None


def test_a_trend_riding_the_band_never_enters():
    out = _steps(Arming(), [2.5, 2.6, 2.7] + [2.8 + 0.1 * i for i in range(50)])
    assert all(o["trigger"] is None for o in out)


def test_through_the_floor_without_an_entry_is_lost():
    a = Arming()
    out = _steps(a, [2.5, 2.6, 2.7, 0.5])                 # 2.7 → 0.5: past 0.75
    assert out[-1]["trigger"] is None and out[-1]["event"] == "lost"
    assert out[-1]["lost"]["peak_z"] == 2.7 and a.armed is None


def test_arming_needs_the_confirmation_ticks():
    out = _steps(Arming(), [2.5, 2.6, 1.4])
    assert all(o["trigger"] is None for o in out)


def test_control_one_confirmation_tick_arms_at_once():
    out = _steps(Arming(), [2.5, 1.4], dict(RE, confirmation_ticks=1))
    assert out[-1]["trigger"] == "SHORT"


def test_control_touch_mode_enters_at_the_stretch():
    out = _steps(Arming(), [2.5, 2.6, 2.7], dict(RE, entry_mode="touch"))
    assert out[-1]["trigger"] == "SHORT"


# ── the live algo ────────────────────────────────────────────────────────────

def _algo(**over):
    calls = {"execute": [], "close": []}
    state = {"z": 0.0}
    params = {"exit_zscore": 0.0, "stop_zscore": 4.0, "lots": 1, "tick_interval": 0.5,
              "cooldown": 0, "lot_multiplier": 75.0, "enable_probability_filter": False,
              "brokerage_per_lot": 10.0, "time_stop_half_lives": 3.0, **RE}
    params.update(over)

    def sig():
        return {"zscore": state["z"], "std": 1.0, "ready": True, "leg_a": 110.0,
                "leg_b": 10.0, "spread": 100.0, "mean": 100.0, "samples": 300,
                "half_life": 0.0, "sample_interval_sec": 0.5, "entry_zscore": 2.0,
                "exit_zscore": 0.0, "stop_zscore": 4.0, "min_signal_minutes": 1.0,
                "span_minutes": 5.0}

    algo = ArrowAutoTrader(
        signal_provider=sig, params_provider=lambda: params,
        execute_fn=lambda d, n, **kw: calls["execute"].append(d) or
        {"success": True, "dry_run": True, "results": []},
        close_fn=lambda d, n, **kw: calls["close"].append(d) or {"success": True})
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
    assert calls["execute"] == []                       # 1.4 came after the arming was lost


def test_live_a_blocked_reentry_stays_armed_and_enters_inside_the_window():
    algo, state, calls, params = _algo(max_daily_loss=100.0, day_pnl=-500.0)
    _run(algo, state, [2.5, 2.6, 2.7, 1.4])
    assert calls["execute"] == [] and "daily loss" in algo.get_state()["status"]
    params["day_pnl"] = 0.0
    _run(algo, state, [1.3])
    assert calls["execute"] == ["SHORT_SPREAD"]


def test_live_arming_does_not_survive_a_restart():
    a, state, calls, params = _algo()
    _run(a, state, [2.5, 2.6, 2.7])
    restarted, state2, calls2, _ = _algo()
    _run(restarted, state2, [1.4])
    assert calls2["execute"] == []                       # the new window owes it nothing


def test_control_the_same_process_does_enter():
    a, state, calls, _ = _algo()
    _run(a, state, [2.5, 2.6, 2.7, 1.4])
    assert calls["execute"] == ["SHORT_SPREAD"]


# ── the study, on the same rule ──────────────────────────────────────────────

def _study(**over):
    p = {"exit_zscore": 0.0, "stop_zscore": 4.0, "time_stop_half_lives": 3.0,
         "cooldown": 0.0, "lots": 1, "lot_multiplier": 75.0, "brokerage_per_lot": 10.0,
         "enable_probability_filter": False, **RE}
    p.update(over)
    return SignalStudy(None, lambda: p), p


def _feed(st, zs, t0=0.0):
    for i, z in enumerate(zs):
        st.observe({"ts": t0 + i, "z": z, "spread": 10.0 * z, "leg_a": 100.0, "leg_b": 90.0,
                    "std": 1.0, "half_life": 0.0, "sample_interval": 0.5})
    return t0 + len(zs)


def _rows(st):
    return st.report("all", now=1e12)["rows"]


def test_study_records_the_arming_and_enters_on_the_way_back():
    st, _ = _study()
    _feed(st, [2.5, 2.6, 2.8, 3.1, 2.2, 1.4, 0.8, -0.1])
    (r,) = _rows(st)
    # armed on the 3rd sample beyond 2.0 (confirmation 3), peaked later
    assert r["entry_mode"] == "reentry" and r["armed_ts"] == 2.0 and r["armed_z"] == 2.8
    assert r["peak_z"] == 3.1 and r["entry_z"] == 1.4 and r["entry_ts"] == 5.0
    assert r["status"] == "closed" and r["exit_reason"] == "target"
    assert r["gross"] == (14.0 - -1.0) * 75.0 and r["result"] == "won"


def test_study_a_stretch_that_never_came_back_has_a_row_and_no_money():
    st, _ = _study()
    _feed(st, [2.5, 2.6, 2.7, 3.0, 0.3])
    (r,) = _rows(st)
    assert r["status"] == "never_back" and r["net"] is None and r["result"] is None
    s = summarize(_rows(st))
    assert s["never_back"] == 1 and s["armed"] == 1 and s["won"] == s["lost"] == 0


def test_study_a_blocked_reentry_keeps_its_row_and_enters_later_in_the_window():
    st, p = _study(enable_probability_filter=True, min_win_probability=0.999)
    _feed(st, [2.5, 2.6, 2.7, 1.4])
    (r,) = _rows(st)
    assert r["status"] == "armed" and r["filter_reason"]
    p["enable_probability_filter"] = False
    _feed(st, [1.3], t0=4)
    (r,) = _rows(st)
    assert r["status"] == "open" and r["entry_z"] == 1.3 and r["filter_reason"] is None


def test_study_an_arming_open_at_a_restart_is_closed_as_never_back(tmp_path):
    path = tmp_path / "study.json"
    p = {"exit_zscore": 0.0, "stop_zscore": 4.0, "cooldown": 0.0, "lots": 1,
         "lot_multiplier": 75.0, "brokerage_per_lot": 10.0,
         "enable_probability_filter": False, **RE}
    a = SignalStudy(path, lambda: p)
    _feed(a, [2.5, 2.6, 2.7])
    b = SignalStudy(path, lambda: p)
    _feed(b, [1.4], t0=10)
    (r,) = _rows(b)
    assert r["status"] == "never_back" and r["lost_reason"] == "restart"


@pytest.mark.parametrize("mode", ["reentry", "touch"])
def test_the_study_and_the_live_algo_enter_on_the_same_sample(mode):
    zs = [0.5, 2.5, 2.6, 2.7, 2.9, 2.1, 1.6, 1.4, 0.9, -0.2, 0.1, -2.4, -2.5, -2.6, -1.9, -1.2]
    algo, state, calls, _ = _algo(entry_mode=mode)
    entered_at = []
    for i, z in enumerate(zs):
        state["z"] = z
        before = len(calls["execute"])
        algo._tick()
        if len(calls["execute"]) > before:
            entered_at.append((i, calls["execute"][-1].replace("_SPREAD", "")))
    st, _ = _study(entry_mode=mode)
    _feed(st, zs)
    study_at = sorted((int(r["entry_ts"]), r["side"]) for r in _rows(st)
                      if r["status"] in ("open", "closed"))
    assert entered_at and study_at == entered_at


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
