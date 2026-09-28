"""Per-trade review record — the numbers-first row the Analysis page reads.

Ported from the W3 basis system's ``trade_review`` table (INR). Assembles one
closed trade's full account from the pieces the engine already has: the entry
decision (z, sigma), the frozen exit plan (levels + cost), the realized P&L,
lifecycle extremes (peak/trough with minutes), and the entry/exit slippage
decompositions. Pure and additive — persistence + the Analysis page consume
this in later phases.
"""

from __future__ import annotations

from .exits import outcome_tag


def _get(report, key):
    return report.get(key) if isinstance(report, dict) else None


def build(*, entry_time, exit_time, asset, direction, entry_z, exit_z,
          entry_sigma, capture_target_inr, cost_est_inr, realized_pnl,
          exit_reason, peak_pnl=None, peak_min=None, trough_pnl=None,
          trough_min=None, entry_spread=None, exit_spread=None,
          spread_levels=None, notional_inr=None, entry_slip=None,
          exit_slip=None, exit_z_band=0.5):
    """Assemble one trade-review dict.

    ``spread_levels`` is the frozen ``ExitLadder.spread_levels`` block (be/ex/
    tp/sl). ``entry_slip``/``exit_slip`` are ``slippage.pair_report`` results.
    ``outcome`` is derived from the exit reason and whether z fully reverted."""
    z_reverted = exit_z is not None and abs(exit_z) <= exit_z_band
    lvl = spread_levels or {}
    return {
        "entry_time": entry_time,
        "exit_time": exit_time,
        "asset": asset,
        "direction": direction,
        "entry_z": entry_z,
        "exit_z": exit_z,
        "entry_sigma": entry_sigma,
        "capture_target_inr": capture_target_inr,
        "cost_est_inr": cost_est_inr,
        "realized_pnl": realized_pnl,
        "exit_reason": exit_reason,
        "outcome": outcome_tag(exit_reason, z_reverted),
        "peak_pnl": peak_pnl,
        "peak_min": peak_min,
        "trough_pnl": trough_pnl,
        "trough_min": trough_min,
        "entry_spread": entry_spread,
        "exit_spread": exit_spread,
        "be_spread": lvl.get("be"),
        "ex_spread": lvl.get("ex"),
        "tp_spread": lvl.get("tp"),
        "sl_spread": lvl.get("sl"),
        "notional_inr": notional_inr,
        # entry/exit execution decomposition (spread units + ₹)
        "entry_crossing_spread": _get(entry_slip, "crossing_spread"),
        "entry_crossing_inr": _get(entry_slip, "crossing_inr"),
        "entry_slippage_spread": _get(entry_slip, "slippage_spread"),
        "entry_slippage_inr": _get(entry_slip, "slippage_inr"),
        "exit_crossing_spread": _get(exit_slip, "crossing_spread"),
        "exit_crossing_inr": _get(exit_slip, "crossing_inr"),
        "exit_slippage_spread": _get(exit_slip, "slippage_spread"),
        "exit_slippage_inr": _get(exit_slip, "slippage_inr"),
    }
