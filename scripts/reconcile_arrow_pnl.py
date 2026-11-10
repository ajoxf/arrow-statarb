#!/usr/bin/env python3
"""Compare Arrow's P&L report with the Nexus trade journal — find the fills
Nexus never journaled.

Download the report from Arrow (Profit & Loss → segment → date range →
Download; CSV, or XLSX with openpyxl installed), then:

    python scripts/reconcile_arrow_pnl.py --report ~/Downloads/pnl.csv \\
        --from 2026-10-01 --to 2026-11-09

Per contract it prints Arrow's realised P&L, what the journal's LIVE trades
imply, and the remainder: the buys/sells (quantity @ average price) Arrow
holds that the journal does not, and the ₹ they carry. Gross, before charges.
Run scripts/fix_mcx_trade_pnl.py --apply first if the journal predates the
MCX multiplier fix, or MCX rows count at 1/10th of their size.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arrow_statarb.config.config import PROJECT_ROOT                    # noqa: E402
from arrow_statarb.core import fill_check                               # noqa: E402
from arrow_statarb.core.contracts import mcx_multiplier                 # noqa: E402
from arrow_statarb.core.trade_log import TradeLog                       # noqa: E402


def _fmt_side(units, avg):
    if not units or units <= 0:
        return "—"
    return f"{units:g} @ {avg:,.2f}" if avg is not None else f"{units:g}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, help="Arrow P&L report (CSV or XLSX)")
    ap.add_argument("--from", dest="date_from", help="first day, YYYY-MM-DD (IST)")
    ap.add_argument("--to", dest="date_to", help="last day, YYYY-MM-DD (IST)")
    ap.add_argument("--journal", default=str(PROJECT_ROOT / "data" / "trades.json"))
    args = ap.parse_args()

    path = Path(args.report).expanduser()
    try:
        rows = fill_check.read_pnl_report(path.read_bytes(), path.name)
    except (OSError, ValueError) as exc:
        print(f"Could not read {path}: {exc}")
        return 1
    trades = TradeLog(Path(args.journal)).all()
    journal = fill_check.journal_fills(
        trades, args.date_from, args.date_to,
        units_per_lot=lambda sym, rec: mcx_multiplier(sym) or rec.get("lot_size"))
    r = fill_check.reconcile(rows, journal)

    print(f"{'Contract':<26}{'Arrow ₹':>12}{'Journal ₹':>12}   "
          f"{'not journaled: bought':>24}{'sold':>20}{'open':>7}{'₹':>11}")
    for c in r["contracts"]:
        res = c["residual"]
        flag = "  (journal > Arrow)" if res["journal_has_extra"] else ""
        print(f"{c['symbol'][:25]:<26}{c['arrow']['realised']:>12,.2f}{c['journal']['realised']:>12,.2f}   "
              f"{_fmt_side(res['buy_units'], res['buy_avg']):>24}{_fmt_side(res['sell_units'], res['sell_avg']):>20}"
              f"{(res['open_units'] or 0):>7g}{c['untracked_pnl']:>11,.2f}{flag}")
    t = r["totals"]
    print(f"\n{'Total':<26}{t['arrow']:>12,.2f}{t['journal']:>12,.2f}{'':>54}{t['untracked']:>11,.2f}")
    charges = sum(x.get("charges") or 0 for x in rows)
    if charges:
        print(f"Arrow charges in the report: ₹{charges:,.2f}")
    print("\nAll fills are in the journal." if r["matches"] else
          "\n'journal > Arrow' means the journal holds fills Arrow does not show — an exit Nexus\n"
          "estimated ('closed outside Nexus'), or a trade outside the report's dates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
