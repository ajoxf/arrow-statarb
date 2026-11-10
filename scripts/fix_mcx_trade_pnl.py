#!/usr/bin/env python3
"""Repair MCX rows in data/trades.json journaled at ₹1 per point.

Until this fix the Trades journal took its multiplier from the broker's ORDER
lot, which Arrow reports as 1 for MCX. A CRUDEOILM spread move was therefore
valued at ₹1/point instead of ₹10 (CRUDEOIL ₹100, NATURALGAS ₹1250 …), so the
journal's P&L did not match Arrow's P&L report.

This rescales each affected row to the contract multiplier (``pairs.
multiplier_b`` from settings if set, else the MCX contract spec), keeping per-lot
brokerage as it was and recomputing STT, CGT and net.

    # stop Nexus first (it holds the journal in memory and would overwrite it)
    python scripts/fix_mcx_trade_pnl.py            # dry run: show what changes
    python scripts/fix_mcx_trade_pnl.py --apply    # write it (backup kept)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arrow_statarb.config.config import Config, PROJECT_ROOT          # noqa: E402
from arrow_statarb.core.contracts import mcx_multiplier                # noqa: E402
from arrow_statarb.core.trade_log import TradeLog                      # noqa: E402


def _leg_b_symbol(rec) -> str:
    """leg_b from the journaled pair name 'LEG_A − LEG_B'."""
    name = str(rec.get("name") or "")
    for sep in (" − ", " - "):
        if sep in name:
            return name.split(sep, 1)[1].strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(PROJECT_ROOT / "data" / "trades.json"))
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"No journal at {path}")
        return 1
    cfg = Config()
    override = float(cfg.get("pairs.multiplier_b", 0) or 0)
    cgt_pct = float(cfg.get("filters.capital_gains_pct", 0) or 0)

    def multiplier_for(rec):
        spec = mcx_multiplier(_leg_b_symbol(rec))
        if spec is None:
            return None                       # not a known MCX contract: leave it
        return override if override > 0 else spec

    log = TradeLog(path)
    changes = log.rescale_lot_size(multiplier_for, capital_gains_pct=cgt_pct, apply=False)
    if not changes:
        print("Nothing to repair — every MCX row already carries its contract multiplier.")
        return 0

    total_old = total_new = 0.0
    for c in changes:
        (lo, ln), (no, nn) = c["lot_size"], c["net_pnl"]
        day = time.strftime("%d %b %H:%M", time.localtime(c["ts"])) if c.get("ts") else c.get("time")
        line = f"{day:>13}  {c['action']:<5}  ×{lo:g} → ×{ln:g}"
        if c["action"] == "CLOSE":
            line += f"   net ₹{float(no or 0):>10,.2f} → ₹{float(nn or 0):>10,.2f}"
            total_old += float(no or 0)
            total_new += float(nn or 0)
        print(line + f"   {c['name']}")
    print(f"\nNet of repaired closes: ₹{total_old:,.2f} → ₹{total_new:,.2f}")

    if not args.apply:
        print("\nDry run — nothing written. Stop Nexus, then re-run with --apply.")
        return 0
    backup = path.with_name(f"{path.stem}.before-mcx-fix.{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
    shutil.copy2(path, backup)
    log.rescale_lot_size(multiplier_for, capital_gains_pct=cgt_pct, apply=True)
    print(f"\nWritten {path} (backup: {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
