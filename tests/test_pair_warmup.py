"""After an INSTRUMENT CHANGE, candle bands are ready the moment the new
pair's history loads — the new pair must still collect the warm-up of live
data ("Warm-up before trading") before the signal is ready. A restart of the
same pair is not held back; a restart part-way through a pair warm-up is."""

import json

import pytest

from arrow_statarb.core.signal import SignalEngine

STEP = 2.0


class _Bands:
    """Candle bands that are always ready (history already loaded)."""
    def bands(self, tf, n, k=1.0):
        return {"ready": True, "mean": 1000.0, "std": 1.0, "count": 600}

    def update(self, *a, **kw):
        pass


def _engine(tmp_path, key="NEW-A|NEW-B", **over):
    p = {"window_minutes": 90.0, "sample_interval_sec": 0.5, "min_signal_minutes": 60.0,
         "band_source": "candles", "band_timeframe": "15m", "band_length": 20}
    p.update(over)
    keys = {"k": key}
    eng = SignalEngine(lambda: (None, None), lambda: p, candles=_Bands(),
                       series_key_provider=lambda: keys["k"],
                       pair_warmup_path=tmp_path / "pair_warmup.json")
    return eng, keys


def _feed(eng, minutes, t0=0.0):
    n = int(minutes * 60 / STEP)
    for i in range(n):
        eng.push(2000.0 + (i % 5) * 0.1, 1000.0, ts=t0 + i * STEP)   # a little movement: σ > 0
    return t0 + n * STEP


def test_a_restart_on_the_same_pair_is_ready_once_candles_load(tmp_path):
    eng, _ = _engine(tmp_path)
    _feed(eng, 1)
    s = eng.get_signal()
    assert s["ready"] is True and s["pair_warmup"] is None


def test_an_instrument_change_waits_for_the_warmup_of_live_data(tmp_path):
    eng, _ = _engine(tmp_path)
    eng.begin_pair_warmup(now=0.0)
    t = _feed(eng, 15)                             # the 15 min the user saw
    s = eng.get_signal()
    assert s["ready"] is False
    assert s["pair_warmup"]["need_sec"] == 3600.0
    assert s["pair_warmup"]["have_sec"] == pytest.approx(15 * 60 - STEP, abs=STEP)
    _feed(eng, 46, t0=t)                           # past 60 min of live data
    s = eng.get_signal()
    assert s["ready"] is True and s["pair_warmup"] is None
    assert json.loads((tmp_path / "pair_warmup.json").read_text())["active"] is False


def test_no_touches_are_counted_while_the_new_pair_warms_up(tmp_path):
    eng, _ = _engine(tmp_path, min_signal_minutes=10.0)
    eng.begin_pair_warmup(now=0.0)
    for i in range(int(5 * 60 / STEP)):            # z = +3 throughout, 5 of 10 min
        eng.push(2003.0, 1000.0, ts=i * STEP)
    assert eng.get_excursions()["touch_2_up"] == 0


def test_a_restart_part_way_through_keeps_the_pair_warmup(tmp_path):
    eng, _ = _engine(tmp_path)
    eng.begin_pair_warmup(now=0.0)
    _feed(eng, 10)
    again, _ = _engine(tmp_path)                   # Nexus restarted: same pair
    _feed(again, 1, t0=10_000.0)
    s = again.get_signal()
    assert s["ready"] is False and s["pair_warmup"] is not None


def test_a_marker_for_another_pair_is_ignored(tmp_path):
    eng, _ = _engine(tmp_path, key="OLD-A|OLD-B")
    eng.begin_pair_warmup(now=0.0)
    other, _ = _engine(tmp_path, key="NEW-A|NEW-B")
    _feed(other, 1)
    assert other.get_signal()["ready"] is True


def test_the_setting_off_restores_the_old_behaviour(tmp_path):
    eng, _ = _engine(tmp_path, warmup_after_pair_change=False)
    eng.begin_pair_warmup(now=0.0)
    _feed(eng, 1)
    assert eng.get_signal()["ready"] is True


def test_tick_mode_is_unchanged(tmp_path):
    eng, _ = _engine(tmp_path, band_source="ticks", min_signal_minutes=10.0)
    eng.begin_pair_warmup(now=0.0)
    t = _feed(eng, 5)
    assert eng.get_signal()["ready"] is False      # its own warm-up, as before
    _feed(eng, 6, t0=t)
    assert eng.get_signal()["ready"] is True


def test_the_algo_says_why_it_waits():
    from arrow_statarb.core.algo import ArrowAutoTrader
    sig = {"zscore": 3.0, "std": 1.0, "ready": False, "band_source": "candles",
           "candles_have": 600, "candles_need": 20, "band_timeframe": "15m",
           "pair_warmup": {"have_sec": 900.0, "need_sec": 3600.0}}
    algo = ArrowAutoTrader(signal_provider=lambda: sig, params_provider=lambda: {"lots": 1},
                           execute_fn=lambda *a, **k: {"success": True},
                           close_fn=lambda *a, **k: {"success": True})
    algo._tick()
    assert algo.get_state()["status"] == ("new pair warm-up: collecting live data "
                                          "(15.0/60 min) — candle bands ready")
