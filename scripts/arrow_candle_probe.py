"""What Arrow's candle history returns for your pair — READ-ONLY.

    python scripts/arrow_candle_probe.py
    python scripts/arrow_candle_probe.py CRUDEOILM19OCT26F CRUDEOILM18DEC26F

Logs in the same way run_arrow.py does (credentials from .env, the saved
session token reused), then for each leg and each timeframe (5 min, 15 min,
1 hour, 4 hours) asks Arrow's historical host for candles and prints how many
came back, the first and last candle, and the price scale it detected. It
then builds the SPREAD's candles and the TradingView BB(20, EMA basis) on
15 min — compare those numbers with your TradingView chart.

It places no orders and never prints a credential. Safe to run while the
dashboard is running. With no symbols it uses the pair assigned in Setup.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:                                                        # mirror run_arrow.py
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
except Exception:
    pass

import yaml                                                  # noqa: E402

from arrow_statarb.config.config import Config               # noqa: E402
from arrow_statarb.brokers.registry import create_broker     # noqa: E402
from arrow_statarb.core import sessions                      # noqa: E402
from arrow_statarb.core.candles import (BACKFILL_DAYS, BUILT_FROM, IST, TF_LABELS, TIMEFRAMES,  # noqa: E402
                                        align_legs, pine_ema, pine_stdev)

SESSION_FILE = PROJECT_ROOT / "data" / "arrow_session.json"
LEGS_FILE = PROJECT_ROOT / "config" / "leg_assignments.yaml"


def _load_token(app_id: str) -> str:
    try:
        data = json.loads(SESSION_FILE.read_text())
    except Exception:
        return ""
    return str(data.get("token", "")) if str(data.get("app_id", "")) == str(app_id) else ""


def _pair_from_setup():
    try:
        legs = yaml.safe_load(LEGS_FILE.read_text()) or {}
    except Exception:
        return None
    out = []
    for lk in ("leg_a", "leg_b"):
        mid = str((legs.get(lk) or {}).get("mapping_id") or "")
        if "|" not in mid:
            return None
        seg, sym = mid.split("|", 1)
        out.append((seg, sym))
    return out


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d-%b %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="leg A and leg B trading symbols")
    ap.add_argument("--segment", default="mcx_fo", help="segment for symbols given (default mcx_fo)")
    ap.add_argument("--n", type=int, default=20, help="BB length (default 20)")
    args = ap.parse_args()

    if len(args.symbols) == 2:
        pair = [(args.segment, args.symbols[0].upper()), (args.segment, args.symbols[1].upper())]
    else:
        pair = _pair_from_setup()
        if not pair:
            print("No pair given and none assigned in Setup. Usage: "
                  "python scripts/arrow_candle_probe.py LEG_A_SYMBOL LEG_B_SYMBOL")
            return 1
    print("pair:", " − ".join(s for _, s in pair))

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
    try:
        broker.start_price_stream([s for _, s in pair])     # for the paise/rupee check
        time.sleep(3)
    except Exception:
        pass

    open_min = (sessions.pair_session([seg for seg, _ in pair],
                                      datetime.now(IST).date()) or (540, 0))[0]
    now = time.time()
    got, raw = {}, {}
    for tf in TIMEFRAMES:
        print(f"\n── {TF_LABELS[tf]} ─────────────────────────────")
        src = BUILT_FROM.get(tf)
        if src:
            # 1 H / 4 H are built from 15-min candles on the 09:00 grid, as on TradingView
            if src in raw and all(raw[src]):
                got[tf] = align_legs(raw[src][0], raw[src][1], TIMEFRAMES[tf], open_min)
                r = got[tf]
                print(f"  built from {TF_LABELS[src]}: {len(r)} candles  {_t(r[0][0])} → {_t(r[-1][0])}")
            else:
                print(f"  cannot build: {TF_LABELS[src]} history failed")
            continue
        legs = []
        for seg, sym in pair:
            try:
                rows = broker.get_candles(seg, sym, tf, now - BACKFILL_DAYS[tf] * 86400.0, now)
            except Exception as exc:
                print(f"  {sym}: FAILED — {exc}")
                legs.append(None)
                continue
            live = (broker.get_streamed_ltp([sym]) or {}).get(sym.upper())
            print(f"  {sym}: {len(rows)} candles  {_t(rows[0][0])} → {_t(rows[-1][0])}  "
                  f"last close ₹{rows[-1][1]:.2f}  (live ₹{live if live else '—'})")
            legs.append(rows)
        route = getattr(broker, "_candle_routes", {})
        if route:
            print("  exchange used:", {k: str(v) for k, v in route.items()})
        raw[tf] = legs
        if all(legs):
            got[tf] = align_legs(legs[0], legs[1], TIMEFRAMES[tf], open_min)

    k = float(cfg.get("signal.hedge_ratio", 1) or 1)
    rows = got.get("15m")
    if rows:
        closes = [k * a - b for _, a, b in rows]
        n = args.n
        ema, sd = pine_ema(closes, n), pine_stdev(closes, n)
        e = float(cfg.get("signal.entry_zscore", 2.0) or 2.0)
        print(f"\nSpread 15 min — {len(closes)} candles, last {_t(rows[-1][0])}: close {closes[-1]:.2f}")
        if ema is not None:
            print(f"BB({n}, EMA basis): basis {ema:.2f} · σ {sd:.3f} · "
                  f"upper {ema + e * sd:.2f} · lower {ema - e * sd:.2f}  (±{e:g}σ)")
            print("Compare with TradingView on the same spread, 15 min, BB length "
                  f"{n}, basis MA type EMA, StdDev {e:g}.")
    print("\nRead-only: no orders were placed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
