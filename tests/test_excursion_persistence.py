"""The z-score excursion tally survives a restart — counts AND the arming that
gates them, together — and counts only on a READY window.

arrow-statarb's z is a time window of samples (window_minutes, sample σ with
ddof=1, simple mean), and that window is NOT persisted: after a restart z is
measured against a fresh window. So "the same stretch" across a restart means
the stretch the restored arming says is still open; it re-arms only when a
ready window puts z back inside the band (z < 1 above, z > -1 below).
"""

import json
import math
import random

import pytest

from arrow_statarb.core.signal import SignalEngine

STEP = 2.0                       # seconds between samples in these tests


def _params(**over):
    p = {"window_minutes": 120.0, "sample_interval_sec": 0.5, "min_signal_minutes": 10.0}
    p.update(over)
    return lambda: p


def _engine(path=None, **over):
    return SignalEngine(lambda: (None, None), _params(**over), excursions_path=path)


def _feed(eng, spreads, t0):
    """Push spreads (leg_b fixed) at STEP-second intervals; return the next ts
    and the z the tally saw on each READY sample."""
    zs = []
    for i, s in enumerate(spreads):
        ts = t0 + i * STEP
        eng.push(1000.0 + s, 1000.0, ts=ts)
        sig = eng.get_signal()
        if sig["ready"]:
            zs.append(sig["zscore"])
    return t0 + len(spreads) * STEP, zs


def _noise(n, seed, level=0.0, sd=1.0):
    rnd = random.Random(seed)
    return [level + rnd.gauss(0, sd) for _ in range(n)]


def _climb(n, start, k=0.012):
    """A spread that keeps stretching: exponential, so the newest sample sits
    far above the mean of ANY window over it — z stays ≥ 1 throughout."""
    return [start + math.exp(k * i) for i in range(n)]


def _stretched_then_restarted(path):
    """Warm up on noise, stretch out past +2σ (counted once), restart while
    the stretch is still open. Returns the restarted engine and next ts."""
    a = _engine(path)
    t, _ = _feed(a, _noise(450, 1), 0.0)                # 15 min of noise
    t, _ = _feed(a, _climb(150, 0.0), t)                # stretch out
    before = a.get_excursions()
    assert before["touch_2_up"] >= 1 and a._arm_2u is False
    a.stop()                                            # the process ends
    return _engine(path), t, before


# ── counts + arming survive, together ────────────────────────────────────────

def test_counts_events_and_arming_survive_a_restart(tmp_path):
    path = tmp_path / "excursions.json"
    b, _t, before = _stretched_then_restarted(path)
    after = b.get_excursions()
    for k in ("touch_2_up", "touch_2_down", "touch_3_up", "touch_3_down", "reversions",
              "event_count", "since"):
        assert after[k] == before[k], k
    assert after["events"][:5] == before["events"][:5]
    assert b._arm_2u is False and b._active_up is True  # the stretch is still open


def test_restart_does_not_recount_a_stretch_that_is_still_open(tmp_path):
    path = tmp_path / "excursions.json"
    b, t, before = _stretched_then_restarted(path)
    # the spread keeps stretching after the restart; the new window warms up
    # and, once ready, still reads it as stretched (z never back below 1)
    _t, zs = _feed(b, _climb(600, 30.0), t)
    assert zs and min(zs) >= 1.0 and max(zs) >= 2.0     # the scenario holds
    assert b.get_excursions()["touch_2_up"] == before["touch_2_up"]


def test_control_without_the_restored_arming_the_same_stretch_counts_again(tmp_path):
    # identical data after the restart, but a tally that forgot its arming
    _b, t, _before = _stretched_then_restarted(tmp_path / "excursions.json")
    forgetful = _engine(None)
    _feed(forgetful, _climb(600, 30.0), t)
    assert forgetful.get_excursions()["touch_2_up"] >= 1


def test_control_a_genuine_second_stretch_still_counts(tmp_path):
    path = tmp_path / "excursions.json"
    b, t, before = _stretched_then_restarted(path)
    t, zs = _feed(b, _noise(600, 2, level=30.0, sd=0.5), t)   # back to quiet
    assert zs and min(zs) < 1.0                          # re-armed on a ready window
    _t, zs = _feed(b, _climb(150, 30.0), t)              # stretches out again
    assert max(zs) >= 2.0
    assert b.get_excursions()["touch_2_up"] > before["touch_2_up"]


# ── no counting on a warming window ──────────────────────────────────────────

def test_a_warming_window_counts_nothing(tmp_path):
    eng = _engine(tmp_path / "excursions.json")
    _feed(eng, _noise(299, 7, level=8.0, sd=0.2), 0.0)    # < 10 minutes of samples
    x = eng.get_excursions()
    assert x["touch_2_up"] + x["touch_2_down"] + x["touch_3_up"] + x["touch_3_down"] == 0
    assert x["event_count"] == 0


def test_control_without_the_warm_up_gate_the_same_noise_counts(tmp_path):
    eng = _engine(tmp_path / "excursions.json", min_signal_minutes=0.0)
    _feed(eng, _noise(299, 7, level=8.0, sd=0.2), 0.0)
    x = eng.get_excursions()
    assert x["touch_2_up"] + x["touch_2_down"] >= 1


# ── the file ─────────────────────────────────────────────────────────────────

def test_state_file_holds_counts_and_arming_together(tmp_path):
    path = tmp_path / "excursions.json"
    _stretched_then_restarted(path)
    d = json.loads(path.read_text())
    assert set(d["arming"]) == {"_arm_2u", "_arm_3u", "_arm_2d", "_arm_3d",
                                "_active_up", "_active_dn"}
    assert d["exc"]["touch_2_up"] >= 1 and "z_prev" in d and d["events"]


def test_counts_without_arming_are_not_half_restored(tmp_path):
    path = tmp_path / "excursions.json"
    path.write_text(json.dumps({"version": 1, "exc": {"touch_2_up": 9}, "events": []}))
    eng = _engine(path)
    assert eng.get_excursions()["touch_2_up"] == 0       # fresh, not half-restored
    assert eng._arm_2u is True


def test_reset_button_clears_the_saved_state_too(tmp_path):
    path = tmp_path / "excursions.json"
    b, _t, _before = _stretched_then_restarted(path)
    b.reset_excursions()
    c = _engine(path)
    assert c.get_excursions()["touch_2_up"] == 0 and c._arm_2u is True


def test_in_memory_engine_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    eng = _engine(None)
    _feed(eng, _noise(450, 1) + _climb(150, 0.0), 0.0)
    assert eng.get_excursions()["touch_2_up"] >= 1
    assert list(tmp_path.iterdir()) == []


# ── a reading must not reach a decision ──────────────────────────────────────

def test_the_tally_cannot_change_the_signal_the_algo_trades_on(tmp_path):
    """The algo reads get_signal() only. Two engines fed the same samples —
    one carrying a restored tally with every band disarmed, one fresh — must
    hand the algo the identical signal."""
    path = tmp_path / "excursions.json"
    path.write_text(json.dumps({
        "version": 1,
        "exc": {"touch_2_up": 50, "touch_2_down": 50, "touch_3_up": 9, "touch_3_down": 9,
                "reversions": 40, "max_z": 7.0, "min_z": -7.0, "since": 1.0},
        "arming": {"_arm_2u": False, "_arm_3u": False, "_arm_2d": False, "_arm_3d": False,
                   "_active_up": True, "_active_dn": True},
        "z_prev": 3.0, "events": []}))
    loaded, fresh = _engine(path), _engine(None)
    data = _noise(450, 4) + _climb(150, 0.0) + _noise(300, 5)
    for i, s in enumerate(data):
        for eng in (loaded, fresh):
            eng.push(1000.0 + s, 1000.0, ts=i * STEP)
        if i % 50 == 0:
            assert loaded.get_signal() == fresh.get_signal()
    assert loaded.get_excursions()["touch_2_up"] != fresh.get_excursions()["touch_2_up"]


def test_the_algo_never_reads_the_tally():
    import inspect
    from arrow_statarb.core import algo
    src = inspect.getsource(algo)
    for name in ("get_excursions", "_exc", "_arm_2u", "_arm_2d", "_active_up",
                 "_active_dn", "excursions"):
        assert name not in src, name
