"""Live-safety integrity checks for the shipped presets.

These presets are copied straight over config/settings.yaml by a (possibly
non-technical) operator and then run against LIVE markets. A silent
inconsistency — e.g. the BE+% take-profit set but capital-at-risk 0 (so it's
inactive), or no stop armed while the z-stop is demoted — would ship a trader
with no profit target or no loss backstop. Assert the invariants here so a bad
preset fails CI, not in the market.
"""

import yaml
import pytest

from pathlib import Path

from arrow_statarb.core.algo import ArrowAutoTrader

PRESET_DIR = Path(__file__).resolve().parent.parent / "config" / "presets"
PRESETS = ["nifty_calendar.yaml", "mcx_crudeoil_calendar.yaml"]


def _load(name):
    return yaml.safe_load((PRESET_DIR / name).read_text())


def _algo_params(cfg):
    """Flatten the preset sections the exit-level resolver reads (mirrors how
    app.py assembles live params) so we can exercise the REAL algo logic."""
    p = {}
    for sec in ("signal", "filters", "risk", "exits", "costs"):
        p.update(cfg.get(sec) or {})
    p["lot_multiplier"] = (cfg.get("execution") or {}).get("sim_default_lot_size", 1)
    p["lots"] = 1
    return p


@pytest.mark.parametrize("name", PRESETS)
def test_preset_loads_and_is_not_live(name):
    cfg = _load(name)
    # First-run safety: never ship a preset defaulting to real-money `live`.
    assert cfg["mode"] == "live_sim", f"{name} must default to live_sim, not live"


@pytest.mark.parametrize("name", PRESETS)
def test_entry_band_is_sane(name):
    s = _load(name)["signal"]
    assert s["entry_zscore"] > 0
    # stop must sit beyond entry, and entry beyond exit — a well-ordered band.
    assert s["stop_zscore"] > s["entry_zscore"] >= s["exit_zscore"]
    assert "hedge_ratio" in s


@pytest.mark.parametrize("name", PRESETS)
def test_take_profit_be_plus_pct_is_active(name):
    # The user's exit design is "BE + designed profit %" via tp_capital_pct.
    # That form is INACTIVE unless capital_at_risk_inr > 0 — the exact footgun
    # ArrowAlgo warns about. If the preset uses it, it must be wired live.
    cfg = _load(name)
    tp = float(cfg["exits"].get("tp_capital_pct", 0) or 0)
    car = float(cfg["risk"].get("capital_at_risk_inr", 0) or 0)
    if tp > 0:
        assert car > 0, f"{name}: tp_capital_pct set but capital_at_risk_inr=0 (BE+% inactive)"


@pytest.mark.parametrize("name", PRESETS)
def test_a_stop_is_always_armed(name):
    # z_stop_exit_enabled is OFF in these presets, so in-trade risk is DOLLARS
    # only → a ₹/%-capital stop MUST exist, else a losing trade has no backstop.
    cfg = _load(name)
    x = cfg["exits"]
    car = float(cfg["risk"].get("capital_at_risk_inr", 0) or 0)
    has_pct_stop = float(x.get("stop_capital_pct", 0) or 0) > 0 and car > 0
    has_fixed_stop = float(x.get("dollar_stop_inr", 0) or 0) > 0
    if not x.get("z_stop_exit_enabled", True):
        assert has_pct_stop or has_fixed_stop, f"{name}: z-stop off and no ₹ stop armed"


@pytest.mark.parametrize("name", PRESETS)
def test_cost_is_modeled(name):
    # If per-segment costs are off (the consistent legacy model), a real STT/CTT
    # must be modeled — a 0 cost would make the edge filter accept losing trades.
    cfg = _load(name)
    if not cfg["costs"].get("use_segment_costs", False):
        assert float(cfg["filters"].get("stt_pct", 0) or 0) > 0, \
            f"{name}: legacy cost model but stt_pct=0 (no cost modeled)"


@pytest.mark.parametrize("name", PRESETS)
def test_leg_segments_are_mapped(name):
    cfg = _load(name)
    segs = set((cfg["broker"].get("segments") or {}).keys())
    for lk in ("leg_a", "leg_b"):
        seg = cfg["instruments"][lk]["segment"]
        assert seg in segs, f"{name}: leg segment {seg} not in broker.segments"


@pytest.mark.parametrize("name", PRESETS)
def test_effective_exit_levels_match_preset(name):
    # Drive the REAL exit-level resolver with the preset's params and confirm the
    # target = tp_capital_pct% × capital-at-risk (BE+%) and a positive stop binds.
    cfg = _load(name)
    p = _algo_params(cfg)
    algo = ArrowAutoTrader(signal_provider=lambda: None,
                           params_provider=lambda: p,
                           execute_fn=lambda *a: {"success": True},
                           close_fn=lambda *a: {"success": True})
    algo._pos = {"lots": 1, "entry_z": -3.0, "entry_std": 0.0}   # no σ → %-capital
    stop, target = algo._effective_exit_levels(p)
    tp = float(p.get("tp_capital_pct", 0) or 0)
    car = float(p.get("capital_at_risk_inr", 0) or 0)
    assert target == pytest.approx(tp / 100.0 * car)             # BE + designed %
    assert stop > 0                                              # a loss backstop exists


def test_mcx_preset_is_mcx_tuned():
    cfg = _load("mcx_crudeoil_calendar.yaml")
    for lk in ("leg_a", "leg_b"):
        assert cfg["instruments"][lk]["segment"] == "mcx_fo"
    # MCX crude: ₹1 tick, 100-barrel lot, CTT 0.01%, evening-session close.
    assert cfg["execution"]["price_tick_size"] == 1
    assert cfg["execution"]["sim_default_lot_size"] == 100
    assert cfg["filters"]["stt_pct"] == 0.01
    assert (cfg["trading_hours"]["close_hour"], cfg["trading_hours"]["close_min"]) == (23, 30)
    # Days-to-expiry is INFO ONLY per the design → no auto expiry block.
    assert cfg["costs"]["no_entry_days_before_expiry"] == 0
