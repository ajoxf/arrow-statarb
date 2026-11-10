#!/usr/bin/env python3
"""Bring the trade journal in line with Arrow's tradebook.

Download the tradebook from Arrow (Orders → Tradebook → date range →
Download), then — with Nexus stopped, since it holds the journal in memory:

    python scripts/import_arrow_tradebook.py --file ~/Downloads/Tradebook.csv            # preview
    python scripts/import_arrow_tradebook.py --file ~/Downloads/Tradebook.csv --apply    # write

It matches every LIVE journal leg to the Arrow order that filled it, corrects
fills Nexus estimated, re-costs the round trips on Arrow's rate card (₹ per
order brokerage, CTT/STT, txn, stamp, SEBI, GST), and adds the trades placed
outside Nexus as rows marked 'arrow'. A backup is kept; running it again on
the same file changes nothing. The same import is on the Analysis page
(Arrow check → choose the tradebook → Compare → Update the journal).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arrow_statarb.config.config import Config, PROJECT_ROOT          # noqa: E402
from arrow_statarb.core import costs, fill_check, journal_sync        # noqa: E402
from arrow_statarb.core.contracts import mcx_multiplier               # noqa: E402
from arrow_statarb.core.trade_log import TradeLog                     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, help="Arrow tradebook (CSV or XLSX)")
    ap.add_argument("--journal", default=str(PROJECT_ROOT / "data" / "trades.json"))
    ap.add_argument("--apply", action="store_true", help="write the changes (default: preview)")
    args = ap.parse_args()

    path = Path(args.file).expanduser()
    try:
        fills = fill_check.read_tradebook(path.read_bytes(), path.name)
    except (OSError, ValueError) as exc:
        print(f"Could not read {path}: {exc}")
        return 1
    cfg = Config()
    co = cfg.section("costs") or {}
    override = float(cfg.get("pairs.multiplier_b", 0) or 0)
    log = TradeLog(Path(args.journal))
    mult = lambda s: (override or None) if override and mcx_multiplier(s) else mcx_multiplier(s)  # noqa: E731
    rates = journal_sync.default_rates_for(lambda s: None, co.get("segments") or {})
    bpo = float(co.get("brokerage_per_order", cfg.get("filters.brokerage_per_lot", 20)) or 0)
    gst = float(co.get("gst_pct", costs.GST_PCT) or costs.GST_PCT)
    cgt = float(cfg.get("filters.capital_gains_pct", 0) or 0)
    if args.apply:
        # the same repair Nexus runs on start (×1 MCX rows, rate-card costs)
        journal_sync.repair_on_start(log, mult, rates, bpo, gst, cgt)

    def make_plan():
        return journal_sync.plan_import([dict(r) for r in log._trades], fills,
                                        multiplier_for=mult, rates_for=rates,
                                        brokerage_per_order=bpo, gst_pct=gst, cgt_pct=cgt)
    plan = make_plan()

    a, b, af = plan["arrow"], plan["journal_before"], plan["journal_after"]
    print(f"Tradebook {a['date_from']} → {a['date_to']}: {a['fills']} fills in {a['orders']} orders\n")
    print(f"{'':22}{'gross':>12}{'charges':>12}{'net':>12}")
    for lbl, x in (("Arrow", a), ("Journal now", b), ("Journal after", af)):
        print(f"{lbl:22}{x['gross']:>12,.2f}{x['charges']:>12,.2f}{x['net']:>12,.2f}")
    for c in plan["corrections"]:
        print(f"\n• correct {c['action']} of {c['when']}: " + "; ".join(c["changes"]))
    for t in plan["new_trips"]:
        print(f"• add {t['direction']} {t['name']} {t['opened']} → {t['closed']}: gross "
              f"{t['gross']:,.2f} − {t['charges']:,.2f} = {t['net']:,.2f}"
              + ("" if t["balanced"] else
                 f"  (unbalanced: leg A {t['leg_a']['lots']:g} / leg B {t['leg_b']['lots']:g} lots)"))
    for u in plan["unmatched_journal"]:
        print(f"! journal {u['action']} {u['when']} ({u['side']} {u['units']:g} @ {u['price']}) "
              "has no matching Arrow fill — left as journaled")
    if plan["still_open"]:
        print("! still open at the end of the tradebook (not added): "
              + " · ".join(f"{f['when']} {f['side']} {f['units']:g} {f['symbol']} @ {f['price']}"
                           for f in plan["still_open"]))
    if not plan["changes"]:
        print("\nThe journal already matches this tradebook.")
        return 0
    if not args.apply:
        print("\nPreview — nothing written. Stop Nexus, then re-run with --apply.")
        return 0
    res = journal_sync.apply_import(log, plan)
    print(f"\nJournal updated: {res['updated']} row(s) corrected, {res['added']} trade(s) added "
          f"(backup: {res['backup']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
