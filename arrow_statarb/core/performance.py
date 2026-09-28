"""Equity-curve drawdown + per-trade adverse-excursion (MAE/MFE).

Ported from the W3 basis system (INR). Computes the Analysis-page drawdown
tiles and the 'Max Drawdown by Trade' table from closed-trade rows. Works on
Arrow's trade-log rows (``net_pnl``) or the ported trade_review rows
(``realized_pnl``) — whichever key is present.
"""

from __future__ import annotations


def _pnl(row):
    v = row.get("net_pnl")
    if v is None:
        v = row.get("realized_pnl")
    return float(v) if v is not None else None


def drawdown_block(rows, newest_first=True):
    """Equity-curve drawdown summary. ``rows`` is a list of closed trades;
    ``newest_first`` replays them oldest→newest to build the running equity."""
    pnls = [_pnl(r) for r in rows if _pnl(r) is not None]
    seq = list(reversed(pnls)) if newest_first else pnls
    peak = run = max_dd = 0.0
    for pnl in seq:
        run += pnl
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)
    current = max(0.0, peak - run)
    return {
        "max_inr": round(max_dd, 2),
        "current_inr": round(current, 2),
        "peak_equity_inr": round(peak, 2),
        "max_pct": round(100 * max_dd / peak, 2) if peak else 0.0,
        "current_pct": round(100 * current / peak, 2) if peak else 0.0,
        "trades": len(pnls),
    }


def excursion_row(row):
    """One closed trade → its MAE/MFE excursion row. MAE is a positive
    magnitude (worst adverse ₹); MFE the best favourable ₹. Percent fields are
    None when the trade's notional isn't known."""
    pnl = _pnl(row) or 0.0
    notional = float(row.get("notional") or 0.0)
    peak = row.get("peak_pnl")
    trough = row.get("trough_pnl")
    target = row.get("capture_target_inr") or row.get("capture_target")
    mae = abs(min(float(trough or 0.0), 0.0))
    mfe = max(float(peak or 0.0), 0.0)
    return {
        "id": row.get("id") or row.get("position_id") or row.get("ts"),
        "position_type": ("SHORT" if (row.get("entry_zscore")
                                       or row.get("entry_z") or 0) > 0 else "LONG"),
        "exit_reason": row.get("outcome") or row.get("exit_reason"),
        "mae_inr": round(mae, 2), "mfe_inr": round(mfe, 2),
        "mae_pct": round(100 * mae / notional, 3) if notional else None,
        "mfe_pct": round(100 * mfe / notional, 3) if notional else None,
        "pnl_inr": round(pnl, 2),
        "pnl_pct": round(100 * pnl / notional, 3) if notional else None,
        "utilization_pct": round(100 * pnl / mfe, 1) if mfe else None,
        "peak_min": row.get("peak_min"), "trough_min": row.get("trough_min"),
        "target_inr": target,
        "hit_target": bool(target and pnl >= target),
        "hit_be": pnl >= 0,
    }


def excursion_rows(rows):
    """Excursion rows for all closed trades that carry lifecycle extremes."""
    return [excursion_row(r) for r in rows
            if r.get("peak_pnl") is not None or r.get("trough_pnl") is not None]
