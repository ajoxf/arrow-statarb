#!/usr/bin/env python3
"""Run a spread backtest.

    # synthetic mean-reverting data (plumbing check / demo — NOT real results):
    python scripts/backtest.py --synthetic --capital 200000

    # your own data: a CSV with columns  ts,leg_a,leg_b  (ts = epoch seconds):
    python scripts/backtest.py --csv data/history.csv --entry 2.5 --exit 0 --stop 4 \
        --time-stop 10 --lot-size 65 --capital 200000

Returns are MEASURED on the data you provide — feed real Arrow/NSE history for
meaningful numbers. Synthetic mode only checks the harness works.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arrow_statarb.core.backtest import Backtester, synthetic_ou


def _load_csv(path: str):
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bars.append((float(row["ts"]), float(row["leg_a"]), float(row["leg_b"])))
    return bars


def main() -> int:
    p = argparse.ArgumentParser(description="Spread backtest")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="CSV with columns ts,leg_a,leg_b")
    src.add_argument("--synthetic", action="store_true", help="generate OU test data")
    p.add_argument("--bars", type=int, default=5000, help="synthetic bar count")
    p.add_argument("--entry", type=float, default=2.0)
    p.add_argument("--exit", type=float, default=0.0)
    p.add_argument("--stop", type=float, default=4.0)
    p.add_argument("--time-stop", type=float, default=3.0, help="time_stop_half_lives")
    p.add_argument("--confirm", type=int, default=1, help="confirmation ticks")
    p.add_argument("--window-min", type=float, default=120.0)
    p.add_argument("--min-history-min", type=float, default=10.0)
    p.add_argument("--sample-sec", type=float, default=0.5)
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--lot-size", type=int, default=75)
    p.add_argument("--brokerage", type=float, default=10.0, help="₹/lot/leg one-way")
    p.add_argument("--slippage", type=float, default=5.0, help="₹/lot/leg one-way")
    p.add_argument("--no-filter", action="store_true", help="disable EV/probability gate")
    p.add_argument("--capital", type=float, default=None, help="₹ capital for ROI")
    args = p.parse_args()

    bars = synthetic_ou(args.bars, seed=42) if args.synthetic else _load_csv(args.csv)

    signal_params = {"window_minutes": args.window_min, "sample_interval_sec": args.sample_sec,
                     "min_signal_minutes": args.min_history_min, "entry_zscore": args.entry,
                     "exit_zscore": args.exit, "stop_zscore": args.stop}
    strategy_params = {"entry_zscore": args.entry, "exit_zscore": args.exit,
                       "stop_zscore": args.stop, "confirmation_ticks": args.confirm,
                       "time_stop_half_lives": args.time_stop, "cooldown": 0.0,
                       "enable_probability_filter": not args.no_filter,
                       "lot_multiplier": float(args.lot_size),
                       "brokerage_per_lot": args.brokerage, "slippage_per_lot": args.slippage}

    bt = Backtester(signal_params=signal_params, strategy_params=strategy_params,
                    lot_size=args.lot_size, brokerage_per_lot=args.brokerage,
                    slippage_per_lot=args.slippage, lots=args.lots, capital=args.capital)
    m = bt.run(bars)
    m.pop("equity_curve", None)   # keep the printout compact

    print("\n=== Backtest result"
          + (" (SYNTHETIC — not real data)" if args.synthetic else "") + " ===")
    print(json.dumps(m, indent=2))
    if args.capital:
        print(f"\nROI on ₹{args.capital:,.0f}: {m.get('roi_pct')}%  "
              f"→ annualized ~{m.get('annualized_roi_pct')}% over {m.get('span_days')} days")
    print("\nNote: returns are only as meaningful as the data + slippage you supplied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
