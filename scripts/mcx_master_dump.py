"""What Arrow's instrument master ACTUALLY says about one commodity.

    python scripts/mcx_master_dump.py CRUDEOIL

Logs in the same way run_arrow.py does (credentials from .env, the saved
session token reused), waits for the instrument master, and prints every
row whose trading symbol or underlying contains the search term:

  * counts by ExchSeg and by how this build classifies them
  * every row it calls a FUTURE, oldest expiry first
  * the RAW rows (Arrow's own field names and values) for the next few
    expiry months, whatever they were classified as — so a contract that
    is missing from the picker can be seen for what it really is

Read-only: it places no orders and never prints a credential. Safe to run
during market hours, alongside the dashboard.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:                                                        # mirror run_arrow.py
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
except Exception:
    pass

from arrow_statarb.config.config import Config              # noqa: E402
from arrow_statarb.brokers.registry import create_broker    # noqa: E402

SESSION_FILE = PROJECT_ROOT / "data" / "arrow_session.json"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _load_token(app_id: str) -> str:
    try:
        data = json.loads(SESSION_FILE.read_text())
    except Exception:
        return ""
    return str(data.get("token", "")) if str(data.get("app_id", "")) == str(app_id) else ""


def _lower(row):
    return {str(k).lower(): v for k, v in row.items()}


def _kind(broker, g):
    exch = str(g.get("exchseg") or g.get("exch_seg") or "").upper()
    ot = str(g.get("optiontype") or g.get("option_type") or "").upper()
    tsym = str(g.get("tradingsymbol") or g.get("trading_symbol") or "")
    strike = g.get("strikeprice") or g.get("strike")
    if exch.endswith("CM"):
        return "cash"
    if ot in ("CE", "PE", "C", "P", "CALL", "PUT") or broker._is_option_row(strike, tsym):
        return "option"
    return "future"


def _next_month_tags(count):
    """['OCT26', 'NOV26', ...] starting from the current month."""
    today = datetime.date.today()
    year, month = today.year, today.month
    tags = []
    for _ in range(count):
        tags.append(f"{MONTHS[month - 1]}{year % 100:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return tags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("needle", nargs="?", default="CRUDEOIL")
    parser.add_argument("--months", type=int, default=4,
                        help="how many months ahead to dump raw rows for (default 4)")
    parser.add_argument("--rows", type=int, default=6,
                        help="raw rows to print per month (default 6)")
    args = parser.parse_args()
    needle = args.needle.upper()

    cfg = Config()
    creds = dict(Config.arrow_credentials())
    creds["lot_sizes"] = cfg.get("broker.lot_overrides") or {}
    tok = _load_token(creds.get("app_id", ""))
    if tok:
        creds["token"] = tok

    broker = create_broker(cfg.get("broker.name", "arrow"), creds)
    print("connecting…")
    if not broker.connect():
        print("connect failed:", getattr(broker, "last_error", "?"))
        return 1
    print("waiting for the instrument master (~10-20 s)…")
    if not broker._instruments_ready.wait(timeout=180):
        print("the instrument master did not arrive within 3 minutes")
        return 1

    rows = [r for r in broker._instruments if isinstance(r, dict)]
    print(f"master: {len(rows):,} rows")

    matches = []
    for row in rows:
        g = _lower(row)
        tsym = str(g.get("tradingsymbol") or g.get("trading_symbol") or "").upper()
        und = str(g.get("symbol") or g.get("underlying") or "").upper()
        if needle in tsym or needle in und:
            matches.append((row, g, tsym, _kind(broker, g)))
    print(f"\n{len(matches):,} rows match {needle!r}")

    counts = {}
    for _row, g, _tsym, kind in matches:
        key = (str(g.get("exchseg") or g.get("exch_seg") or "?").upper(), kind)
        counts[key] = counts.get(key, 0) + 1
    print("   by ExchSeg and kind:")
    for (exch, kind), n in sorted(counts.items()):
        print(f"      {exch:<8} {kind:<7} {n:>7,}")

    futures = [(g, tsym) for _row, g, tsym, kind in matches if kind == "future"]

    def _exp_key(item):
        g, tsym = item
        return broker._parse_date_tuple(str(g.get("expiry") or "")) \
            or broker._parse_date_tuple(tsym) or (9999, 99, 99)

    futures.sort(key=_exp_key)
    print(f"\nFUTURES ({len(futures)}), oldest expiry first:")
    for g, tsym in futures:
        print(f"   {tsym:<24} exch={g.get('exchseg')!s:<7} und={g.get('symbol')!s:<10} "
              f"expiry={g.get('expiry')!s:<12} lot={g.get('lotsize')!s:<5} "
              f"token={g.get('token')}")

    print(f"\nRAW rows for the next {args.months} months (whatever they were classified as):")
    for tag in _next_month_tags(args.months):
        in_month = [(row, tsym, kind) for row, _g, tsym, kind in matches if tag in tsym]
        # futures-looking rows first, then the rest
        in_month.sort(key=lambda x: (x[2] != "future", len(x[1]), x[1]))
        print(f"\n  {tag}: {len(in_month)} rows "
              f"({sum(1 for x in in_month if x[2] == 'future')} classified future)")
        for row, tsym, kind in in_month[:args.rows]:
            print(f"   [{kind}] {json.dumps(row, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
