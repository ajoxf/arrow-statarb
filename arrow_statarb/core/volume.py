"""Traded-volume (turnover) tracker — day / week / month, in IST.

Replaces the reference system's crypto "VIP volume" tile with something an
Indian F&O desk actually cares about: how much notional you've turned over
today, this week and this month, plus the spread-lots and execution count.

Turnover is counted per EXECUTION (each OPEN and each CLOSE is a real fill on
BOTH legs and pays brokerage), so a one-lot round trip books turnover twice —
once on entry, once on exit — which is exactly how brokerage and exchange
turnover accrue. Rejected/again un-filled records contribute nothing.

Pure and side-effect free: it reads a list of trade-log records and a clock,
and returns a summary. Periods are calendar boundaries in IST (the exchange's
local zone): "today" since IST midnight, "this week" since Monday, "this
month" since the 1st.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

# Exchange-local zone — the boundary every period is cut on (matches trade_log).
_IST = timezone(timedelta(hours=5, minutes=30))

_ACTIONS = ("OPEN", "CLOSE")


def turnover_of(rec: Dict) -> float:
    """₹ notional traded by one execution record — both legs at their fill
    prices: lots × lot_size × (|leg_a_price| + |leg_b_price|). 0 if the record
    carries no usable price (e.g. a rejection)."""
    lots = float(rec.get("lots", 0) or 0)
    mult = float(rec.get("lot_size", 0) or 0) or 1.0
    pa = abs(float(rec.get("leg_a_price", 0) or 0))
    pb = abs(float(rec.get("leg_b_price", 0) or 0))
    return lots * mult * (pa + pb)


def _counts(rec: Dict) -> "tuple[float, float, int]":
    """(turnover_inr, spread_lots, executions) contributed by one record."""
    return turnover_of(rec), float(rec.get("lots", 0) or 0), 1


def _is_fill(rec: Dict) -> bool:
    """A record that actually moved contracts. Skips rejections and anything
    that is not an entry/exit execution."""
    if rec.get("action") not in _ACTIONS:
        return False
    if str(rec.get("status", "")).lower() == "rejected":
        return False
    return float(rec.get("lots", 0) or 0) > 0 and turnover_of(rec) > 0


def _ist_midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket() -> Dict:
    return {"turnover_inr": 0.0, "lots": 0.0, "trades": 0}


def _add(bucket: Dict, turn: float, lots: float, execs: int) -> None:
    bucket["turnover_inr"] += turn
    bucket["lots"] += lots
    bucket["trades"] += execs


def _round(bucket: Dict) -> Dict:
    return {"turnover_inr": round(bucket["turnover_inr"], 2),
            "lots": round(bucket["lots"], 4),
            "trades": int(bucket["trades"])}


def volume_summary(trades: Sequence[Dict], now_ts: Optional[float] = None,
                   recent_days: int = 14) -> Dict:
    """Turnover/lots/trades for today, this ISO week (from Monday) and this
    calendar month, all in IST, plus a per-day breakdown of the last
    ``recent_days`` days (oldest → newest) for a small history strip.

    ``trades`` is a list of trade-log records (``TradeLog.all()``); each may
    carry ts, action, status, lots, lot_size, leg_a_price, leg_b_price.
    """
    now_ts = time.time() if now_ts is None else float(now_ts)
    now = datetime.fromtimestamp(now_ts, _IST)
    day_start = _ist_midnight(now)
    week_start = day_start - timedelta(days=now.weekday())          # Monday
    month_start = day_start.replace(day=1)
    day_s = day_start.timestamp()
    week_s = week_start.timestamp()
    month_s = month_start.timestamp()

    day, week, month, all_time = _bucket(), _bucket(), _bucket(), _bucket()

    # Per-IST-date buckets for the recent-days strip.
    per_date: Dict[str, Dict] = {}

    for rec in trades:
        if not _is_fill(rec):
            continue
        ts = float(rec.get("ts", 0) or 0)
        turn, lots, execs = _counts(rec)
        _add(all_time, turn, lots, execs)
        if ts >= month_s:
            _add(month, turn, lots, execs)
        if ts >= week_s:
            _add(week, turn, lots, execs)
        if ts >= day_s:
            _add(day, turn, lots, execs)
        d = datetime.fromtimestamp(ts, _IST).strftime("%Y-%m-%d")
        _add(per_date.setdefault(d, _bucket()), turn, lots, execs)

    # Contiguous last-N-day strip (fills in zero-volume days so a sparkline is
    # continuous), oldest → newest.
    recent: List[Dict] = []
    for i in range(recent_days - 1, -1, -1):
        d = (day_start - timedelta(days=i)).strftime("%Y-%m-%d")
        b = per_date.get(d, _bucket())
        recent.append({"date": d, **_round(b)})

    return {
        "day": _round(day),
        "week": _round(week),
        "month": _round(month),
        "all_time": _round(all_time),
        "recent_days": recent,
        "week_start": week_start.strftime("%Y-%m-%d"),
        "month_start": month_start.strftime("%Y-%m-%d"),
        "as_of": now.strftime("%Y-%m-%d %H:%M IST"),
    }
