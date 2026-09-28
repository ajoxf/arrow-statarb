"""MultiAssetEngine — two assets concurrently on one connection."""

from arrow_statarb.core.engine import MultiAssetEngine
from arrow_statarb.core.exits import BUY_BASIS


PARAMS = {
    "SIGNALS": {"ENTRY_Z": 1.5, "MAX_ENTRY_Z": 8.0, "STOP_Z": 8.0, "EXIT_Z": 0.5,
                "TREND_FILTER": False, "ENTRY_COOLDOWN_SEC": 0,
                "STOP_COOLDOWN_SEC": 0, "LOOKBACK_SEC": 10000,
                "STATS_INTERVAL_SEC": 0, "MIN_SAMPLES": 8, "MIN_HISTORY_SEC": 0,
                "MIN_SIGMA": 0.0, "MAX_ABS_Z": 25.0, "TREND_WINDOW_SEC": 10000},
    "EXITS": {"USE_SIGMA_TARGET": True, "COST_FLOOR_MULT": 0.0,
              "STOP_INR_PER_LOT": 1000.0, "MAX_HOLD_HALF_LIVES": 4,
              "MAX_HOLD_FALLBACK_MIN": 240, "HARD_TIME_STOP_MULT": 0,
              "HARD_MAX_HOLD_MIN": 0, "GATE_FLOOR_INR": 0.0,
              "Z_STOP_EXIT_ENABLED": False, "MAX_HOLD_PROGRESS_SUPPRESS": 0.5},
    "TRADING": {"CLIP_LOTS": 1, "SIZING_MODE": "lots", "HEDGE_MODE": "units",
                "HEDGE_RATIO": 1.0},
    "COSTS": {"TARGET_FRACTION": 0.5},
}


def _build():
    t = [1000.0]
    MD = {"A": {"spread": 100.0, "price_a": 100.0, "price_b": 100.0, "quote_id": 0},
          "B": {"spread": 50.0, "price_a": 50.0, "price_b": 50.0, "quote_id": 0}}
    calls = {"exec": [], "close": []}

    def price_fn(key):
        return MD[key]

    def execute_fn(key, direction, size):
        calls["exec"].append((key, direction))
        return {"success": True, "entry_spread": MD[key]["spread"]}

    def close_fn(key, reason):
        calls["close"].append((key, reason))
        return {"success": True}

    eng = MultiAssetEngine(
        assets={"A": {"contract_a": 1, "contract_b": 1},
                "B": {"contract_a": 1, "contract_b": 1}},
        params_provider=lambda: PARAMS, price_fn=price_fn,
        execute_fn=execute_fn, close_fn=close_fn, clock=lambda: t[0])
    return eng, t, MD, calls


def _feed(eng, t, MD, key, value, qid):
    t[0] += 1
    MD[key]["spread"] = value
    MD[key]["quote_id"] = qid
    eng.tick()


def test_multi_asset_concurrent_entry_and_exit():
    eng, t, MD, calls = _build()
    eng.start()

    # Warm A with an oscillation (B held constant → degenerate, never trades).
    qid = 0
    for v in [99, 101, 99, 101, 99, 101, 99, 101, 99, 101, 99, 101]:
        qid += 1
        _feed(eng, t, MD, "A", v, qid)
        _feed(eng, t, MD, "B", 50.0, qid)      # constant → sigma 0 → no signal

    assert eng.assets["A"].position is None     # still resting (|z| small)
    assert eng.assets["B"].position is None

    # A dips well below the mean → |z| beyond ENTRY_Z → BUY_BASIS opens.
    qid += 1
    _feed(eng, t, MD, "A", 97.0, qid)
    assert eng.assets["A"].position is not None
    assert ("A", BUY_BASIS) in calls["exec"]
    assert eng.assets["B"].position is None     # B never traded

    # Spread rises back through the mean → take-profit closes A.
    qid += 1
    _feed(eng, t, MD, "A", 101.0, qid)
    assert eng.assets["A"].position is None
    assert calls["close"] and calls["close"][0][0] == "A"
    assert not any(c[0] == "B" for c in calls["close"])


def test_snapshot_reports_per_asset():
    eng, t, MD, calls = _build()
    snap = eng.snapshot()
    assert set(snap.keys()) == {"A", "B"}
    assert snap["A"]["position"] is None
