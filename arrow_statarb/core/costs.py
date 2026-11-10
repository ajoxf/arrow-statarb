"""Indian statutory + broker transaction costs, per exchange segment.

Replaces the crypto maker/taker/funding model. On NSE/MCX the round-trip cost is
a stack of statutory charges that differ by SEGMENT and by SIDE:

  • STT (equity/F&O) or CTT (MCX commodities) — % of notional, sell-side
    (equity DELIVERY is charged on both buy and sell);
  • exchange transaction charge — % of notional, every turnover;
  • SEBI turnover fee — % of notional, every turnover;
  • stamp duty — % of notional, BUY side only;
  • brokerage — flat ₹ per lot per order (Arrow);
  • GST — 18% on (brokerage + txn + SEBI), NOT on notional.

A round trip trades each leg exactly twice (one buy + one sell), so per leg:
STT/CTT hits its one sell (+ its one buy for equity delivery), stamp hits its one
buy, txn/SEBI/brokerage hit both turnovers, and GST rides on the brokerage+txn+SEBI.

RATES ARE DEFAULTS FOR CALIBRATION — verify against a real Arrow contract note.
An inflated cost estimate breaks the edge filter and the cost floor (the reference
system lost real money to exactly this), so calibrate, don't trust the rate card.
"""

from __future__ import annotations

from typing import Dict, Optional

GST_PCT = 18.0  # GST on (brokerage + exchange txn + SEBI), not on notional.

# Per-segment / per-instrument-kind rates, % of notional unless noted.
# Keys: broker segment keys (nse_fo, mcx_fo, …) plus instrument-kind overrides
# (etf, nse_cm_delivery, nse_cm_intraday) selected via a leg's cost_key.
DEFAULT_SEGMENT_COSTS: Dict[str, Dict[str, float]] = {
    # NSE equity FUTURES (F&O): STT 0.02% sell (2024 rate — verify).
    "nse_fo": {"stt_sell_pct": 0.02, "stt_buy_pct": 0.0, "txn_pct": 0.0019,
               "sebi_pct": 0.0001, "stamp_buy_pct": 0.002},
    # BSE F&O.
    "bse_fo": {"stt_sell_pct": 0.02, "stt_buy_pct": 0.0, "txn_pct": 0.0,
               "sebi_pct": 0.0001, "stamp_buy_pct": 0.002},
    # MCX commodity futures (non-agri): CTT 0.01% sell (agri = 0, set per commodity).
    "mcx_fo": {"stt_sell_pct": 0.01, "stt_buy_pct": 0.0, "txn_pct": 0.0021,
               "sebi_pct": 0.0001, "stamp_buy_pct": 0.002},
    # Equity ETF (delivery): STT 0.001% sell — the low-cost spot leg.
    "etf": {"stt_sell_pct": 0.001, "stt_buy_pct": 0.0, "txn_pct": 0.00297,
            "sebi_pct": 0.0001, "stamp_buy_pct": 0.015},
    # Equity cash DELIVERY: STT 0.1% both sides.
    "nse_cm_delivery": {"stt_sell_pct": 0.1, "stt_buy_pct": 0.1, "txn_pct": 0.00297,
                        "sebi_pct": 0.0001, "stamp_buy_pct": 0.015},
    # Equity cash INTRADAY: STT 0.025% sell only.
    "nse_cm_intraday": {"stt_sell_pct": 0.025, "stt_buy_pct": 0.0, "txn_pct": 0.00297,
                        "sebi_pct": 0.0001, "stamp_buy_pct": 0.003},
    # Bare segment fallback for cash (delivery assumed — the safer, higher cost).
    "nse_cm": {"stt_sell_pct": 0.1, "stt_buy_pct": 0.1, "txn_pct": 0.00297,
               "sebi_pct": 0.0001, "stamp_buy_pct": 0.015},
    "bse_cm": {"stt_sell_pct": 0.1, "stt_buy_pct": 0.1, "txn_pct": 0.00375,
               "sebi_pct": 0.0001, "stamp_buy_pct": 0.015},
}


def resolve_segment_costs(cost_key: str, overrides: Optional[Dict] = None) -> Dict[str, float]:
    """Rates for a leg. ``cost_key`` is the leg's segment key or an instrument-kind
    override (etf / nse_cm_delivery / …). ``overrides`` (from config) shadow the
    defaults key-by-key, so an operator can calibrate one rate without restating
    the whole table. Unknown keys resolve to all-zero (no phantom cost)."""
    base = dict(DEFAULT_SEGMENT_COSTS.get(cost_key, {}))
    if overrides and cost_key in overrides and isinstance(overrides[cost_key], dict):
        base.update({k: float(v) for k, v in overrides[cost_key].items() if v is not None})
    return base


def leg_round_trip_cost(rates: Dict[str, float], notional: float,
                        brokerage_leg: float, gst_pct: float = GST_PCT) -> float:
    """Cost (₹) for ONE leg over a full round trip (one buy + one sell).
    ``brokerage_leg`` is the brokerage for this leg's TWO orders (entry + exit)."""
    n = max(0.0, float(notional))
    txn = float(rates.get("txn_pct", 0) or 0) / 100.0 * n * 2.0        # both turnovers
    sebi = float(rates.get("sebi_pct", 0) or 0) / 100.0 * n * 2.0
    stamp = float(rates.get("stamp_buy_pct", 0) or 0) / 100.0 * n       # buy side only
    stt = ((float(rates.get("stt_sell_pct", 0) or 0)                    # the one sell
            + float(rates.get("stt_buy_pct", 0) or 0))                  # +buy if delivery
           / 100.0 * n)
    gst = float(gst_pct) / 100.0 * (brokerage_leg + txn + sebi)         # GST on charges only
    return brokerage_leg + txn + sebi + stamp + stt + gst


def round_trip_cost(rates_a: Dict[str, float], notional_a: float,
                    rates_b: Dict[str, float], notional_b: float,
                    brokerage_per_lot: float, lots: int,
                    slippage_per_lot: float = 0.0,
                    gst_pct: float = GST_PCT) -> float:
    """Full two-leg round-trip transaction cost (₹). Brokerage/slippage are flat
    ₹ per lot per order; each leg has 2 orders (entry+exit). Excludes CGT (a
    haircut on profit, applied in the P&L, not a transaction cost)."""
    lots = max(1, int(lots))
    brokerage_leg = float(brokerage_per_lot or 0) * lots * 2.0          # 2 orders / leg
    slippage = float(slippage_per_lot or 0) * lots * 4.0               # 2 legs × 2 sides
    return (leg_round_trip_cost(rates_a, notional_a, brokerage_leg, gst_pct)
            + leg_round_trip_cost(rates_b, notional_b, brokerage_leg, gst_pct)
            + slippage)


#: Exchange names in Arrow's tradebook → the cost table's segment keys.
EXCHANGE_COST_KEYS = {"MCX": "mcx_fo", "MCXFO": "mcx_fo", "NFO": "nse_fo", "NSEFO": "nse_fo",
                      "BFO": "bse_fo", "BSEFO": "bse_fo", "NSE": "nse_cm", "BSE": "bse_cm"}


def fills_charges(fills, rates_for, brokerage_per_order: float = 20.0,
                  gst_pct: float = GST_PCT) -> Dict[str, float]:
    """Arrow's charges on ACTUAL fills, line by line as its contract note
    and P&L report charge them:

      brokerage  flat ₹ per EXECUTED ORDER (one order of 20 qty is charged
                 once), shared pro rata when one order is split across trades;
      txn, SEBI  % of every turnover;  CTT/STT  % of sells (+buys if delivery);
      stamp      % of buys;  GST  on brokerage + txn + SEBI.

    ``fills``: dicts with side ('buy'/'sell'), units, price, and optionally
    symbol, order_id and order_units (the order's total, for the brokerage
    share; default = this fill is the whole order). ``rates_for(fill)`` gives
    that fill's rate dict (see DEFAULT_SEGMENT_COSTS)."""
    out = {"brokerage": 0.0, "txn": 0.0, "sebi": 0.0, "stt": 0.0, "stamp": 0.0, "gst": 0.0}
    for f in fills or []:
        r = rates_for(f) or {}
        units, px = float(f.get("units") or 0), float(f.get("price") or 0)
        turnover = abs(units * px)
        whole = float(f.get("order_units") or units or 1)
        out["brokerage"] += float(brokerage_per_order or 0) * (units / whole if whole else 1.0)
        out["txn"] += float(r.get("txn_pct", 0) or 0) / 100.0 * turnover
        out["sebi"] += float(r.get("sebi_pct", 0) or 0) / 100.0 * turnover
        # stt_factor / stamp_factor: the contract note rounds CTT/STT and stamp
        # to whole rupees per day; a factor spreads that day's rounding back
        # over its fills so per-trade charges add up to the statement.
        sf, mf = float(f.get("stt_factor", 1.0)), float(f.get("stamp_factor", 1.0))
        if f.get("side") == "sell":
            out["stt"] += float(r.get("stt_sell_pct", 0) or 0) / 100.0 * turnover * sf
        else:
            out["stt"] += float(r.get("stt_buy_pct", 0) or 0) / 100.0 * turnover * sf
            out["stamp"] += float(r.get("stamp_buy_pct", 0) or 0) / 100.0 * turnover * mf
    out["gst"] = float(gst_pct) / 100.0 * (out["brokerage"] + out["txn"] + out["sebi"])
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(sum(out.values()), 2)
    return out
