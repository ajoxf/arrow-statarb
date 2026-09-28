"""Hedge-ratio (beta) drift monitor — display/monitoring only.

The monitor watches whether leg_b ≈ β·leg_a stays put. These tests build
synthetic price windows with a KNOWN, controllable relationship and assert
the state machine (WARMUP → STABLE → DRIFTING → STRUCTURAL_DRIFT →
DRIFTED_RETURNED) fires when — and only when — the hedge ratio actually moves.
"""

import random

from arrow_statarb.core import beta_monitor as bm


DT = 0.5   # sample spacing (s) — matches the engine's default cadence


def _make(n, beta_fn, *, dt=DT, a0=100.0, noise=0.0, seed=1, a_vol=0.05):
    rng = random.Random(seed)
    ts, a, b = [], [], []
    ai = a0
    for k in range(n):
        ai += rng.uniform(-a_vol, a_vol)        # leg_a wanders (gives x variance)
        beta = beta_fn(k / max(1, n - 1))
        bi = beta * ai + (rng.uniform(-noise, noise) if noise else 0.0)
        ts.append(k * dt)
        a.append(ai)
        b.append(bi)
    return list(zip(ts, a, b))


# Short windows so the tests build modest bars but still separate anchor/now.
KW = dict(beta_window_sec=30.0, anchor_window_sec=30.0, min_points=40,
          structural_min_sec=20.0, max_estimates=120)


def test_warmup_when_too_few_points():
    out = bm.beta_drift_block([(0, 100, 100), (1, 100, 100)], **KW)
    assert out["status"] == "WARMUP"
    assert out["z"] is None


def test_warmup_when_history_too_short():
    # Enough points, but the span is shorter than anchor+beta windows can split.
    bars = _make(80, lambda f: 1.0, dt=0.1)      # 80 * 0.1s = 8s span < 30s
    out = bm.beta_drift_block(bars, **KW)
    assert out["status"] == "WARMUP"


def test_stable_when_beta_constant():
    # Beta pinned at 1.5 the whole window → near-zero drift → STABLE, |z| small.
    bars = _make(600, lambda f: 1.5, noise=0.01)
    out = bm.beta_drift_block(bars, **KW)
    assert out["status"] == "STABLE"
    assert abs(out["z"]) < 1.0
    assert abs(out["anchor"] - 1.5) < 0.05
    assert abs(out["current_beta"] - 1.5) < 0.05
    assert out["minutes_beyond"] == 0


def test_flat_identical_legs_are_stable_not_drift():
    # Both legs pinned at the same value → ratio 1.0 exactly, dead flat. The
    # relationship is trivially stable, so this must read STABLE (β≈1, z≈0) —
    # never a drift alarm.
    bars = [(k * DT, 100.0, 100.0) for k in range(600)]
    out = bm.beta_drift_block(bars, **KW)
    assert out["status"] == "STABLE"
    assert abs(out["anchor"] - 1.0) < 1e-6
    assert abs(out["z"]) < 1.0


def test_structural_drift_on_sustained_ramp():
    # Beta ramps from 1.0 to 1.6 across the window and STAYS high at the end →
    # the current beta sits far from the (early) anchor for a sustained stretch.
    bars = _make(800, lambda f: 1.0 + 0.6 * f, noise=0.005)
    out = bm.beta_drift_block(bars, **KW)
    assert out["status"] == "STRUCTURAL_DRIFT"
    assert abs(out["z"]) >= 2.0
    assert out["current_beta"] > out["anchor"]
    assert out["minutes_beyond"] >= 1        # sustained beyond the 2σ band


def test_drifted_returned_after_transient_excursion():
    # Beta bulges out mid-window then comes back to baseline by the end:
    # a triangular excursion. Current |z| is small but the peak exceeded 2σ →
    # DRIFTED_RETURNED (transient, reverted — no action needed).
    def beta_fn(f):
        # 1.0 baseline, spike to ~1.5 at the middle, back to 1.0 by the end.
        return 1.0 + 0.5 * (1.0 - abs(2.0 * f - 1.0)) * (1.0 if 0.15 < f < 0.85 else 0.0)
    bars = _make(1000, beta_fn, noise=0.004)
    out = bm.beta_drift_block(bars, **KW)
    assert out["max_abs_z"] >= 2.0            # it did leave the band…
    assert abs(out["z"]) < 1.0               # …but is back home now
    assert out["status"] == "DRIFTED_RETURNED"


def test_sign_of_z_follows_direction():
    # Beta ends BELOW the anchor → current z must be negative.
    bars = _make(800, lambda f: 1.5 - 0.5 * f, noise=0.005)
    out = bm.beta_drift_block(bars, **KW)
    assert out["z"] < 0
    assert out["current_beta"] < out["anchor"]


def test_jitter_does_not_manufacture_drift():
    # A rock-flat beta with only tiny numerical noise must stay STABLE — the
    # std floor stops sub-0.1% jitter from blowing up into a spurious z.
    bars = _make(600, lambda f: 2.0, noise=0.0, a_vol=0.2, seed=7)
    out = bm.beta_drift_block(bars, **KW)
    assert out["status"] == "STABLE"
    assert abs(out["z"]) < 1.0


def test_diagnostics_present():
    bars = _make(600, lambda f: 1.0, noise=0.01)
    out = bm.beta_drift_block(bars, **KW)
    for key in ("anchor", "current_beta", "max_abs_z", "minutes_beyond",
                "beta_std", "n_samples", "span_min"):
        assert key in out
    assert out["n_samples"] == 600
