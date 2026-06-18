#!/usr/bin/env python3
"""Controlled live-trade tester for the Arrow broker.

Run this on the server whose static IP is registered with Arrow. It walks the
order pipeline in safe, explicit stages so you can validate live trading before
trusting the dashboard's dual-market-order execute path.

Credentials (env vars preferred — keeps secrets out of shell history):

    export ARROW_APP_ID=...
    export ARROW_USER_ID=...
    export ARROW_PASSWORD=...
    export ARROW_API_SECRET=...
    export ARROW_TOTP_SECRET=...      # base32 secret, NOT the 6-digit code

Stages (each opt-in — default run is read-only):

    # read-only: connect → account → LTP
    python scripts/arrow_test_trade.py --symbol NIFTY30JUN26F --segment nse_fo

    # place a resting LIMIT order away from market, then cancel it
    python scripts/arrow_test_trade.py --symbol NIFTY30JUN26F --segment nse_fo --place-limit

    # fire a REAL market order (guarded by --yes)
    python scripts/arrow_test_trade.py --symbol NIFTY30JUN26F --segment nse_fo \
        --side buy --place-market --yes
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Allow running from the repo root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger  # noqa: E402

from arrow_statarb.brokers.arrow_broker import ArrowBroker  # noqa: E402


def _build_config(args) -> dict:
    cfg = {
        "app_id":      args.app_id      or os.environ.get("ARROW_APP_ID", ""),
        "user_id":     args.user_id     or os.environ.get("ARROW_USER_ID", ""),
        "password":    args.password    or os.environ.get("ARROW_PASSWORD", ""),
        "api_secret":  args.api_secret  or os.environ.get("ARROW_API_SECRET", ""),
        "totp_secret": args.totp_secret or os.environ.get("ARROW_TOTP_SECRET", ""),
    }
    if args.lot_size:
        sym, _, val = args.lot_size.partition("=")
        if sym and val:
            cfg["lot_sizes"] = {sym.strip().upper(): int(val)}
    missing = [k for k in ("app_id", "user_id", "password", "api_secret", "totp_secret") if not cfg[k]]
    if missing:
        logger.error("Missing credentials: {} (set ARROW_* env vars or pass flags)", ", ".join(missing))
        sys.exit(2)
    return cfg


def _get_ltp(broker: ArrowBroker, segment: str, symbol: str):
    quotes = broker.get_ltp([{"exchange_segment": segment, "instrument_token": symbol}])
    return quotes.get(symbol.upper())


def main() -> int:
    p = argparse.ArgumentParser(description="Arrow broker controlled live-trade tester")
    p.add_argument("--symbol",  required=True, help="Trading symbol, e.g. NIFTY30JUN26F")
    p.add_argument("--segment", default="nse_fo",
                   help="Exchange segment: nse_fo, nse_cm, bse_fo, bse_cm (default nse_fo)")
    p.add_argument("--side",    default="buy", choices=["buy", "sell"], help="Order side (default buy)")
    p.add_argument("--lots",    type=int, default=1, help="Number of lots (default 1)")
    p.add_argument("--product", default="NRML", choices=["NRML", "MIS", "CNC"], help="Product (default NRML)")
    p.add_argument("--offset",  type=float, default=3.0,
                   help="Limit price offset %% away from LTP so it rests un-filled (default 3.0)")
    p.add_argument("--place-limit",  action="store_true", help="Place a resting (non-filling) limit order")
    p.add_argument("--place-market", action="store_true", help="Place a REAL market order (needs --yes)")
    p.add_argument("--no-cancel",    action="store_true", help="Leave the limit order resting (don't cancel)")
    p.add_argument("--yes",          action="store_true", help="Confirm a real market order")
    p.add_argument("--app-id");      p.add_argument("--user-id");     p.add_argument("--password")
    p.add_argument("--api-secret");  p.add_argument("--totp-secret"); p.add_argument("--lot-size")
    args = p.parse_args()

    cfg = _build_config(args)
    broker = ArrowBroker(config=cfg)

    logger.info("Connecting to Arrow…")
    if not broker.connect():
        logger.error("Connect failed — check credentials and that this host's IP is registered with Arrow.")
        return 1
    logger.success("Connected. Session token: {}…", broker.get_session_token()[:12])

    try:
        info = broker.get_account_info()
        logger.info("Account info: {}", info or "(none returned)")

        positions = broker.get_positions()
        logger.info("Open positions: {}", len(positions))
        for pos in positions:
            logger.info("  {} {} net={} avg={}", pos["exchange"], pos["symbol"],
                        pos["net_quantity"], pos["average_price"])

        # Give the instrument master a moment to load (lot sizes come from it)
        broker._instruments_ready.wait(timeout=10)
        lot_size = broker.resolve_lot_size(args.segment, args.symbol)
        qty = args.lots * lot_size
        logger.info("Lot size for {}: {} → {} lot(s) = {} units", args.symbol, lot_size, args.lots, qty)

        ltp = _get_ltp(broker, args.segment, args.symbol)
        if ltp is None:
            logger.warning("No LTP returned for {}/{} — check the symbol/segment.", args.segment, args.symbol)
        else:
            logger.success("LTP {}/{} = {}", args.segment, args.symbol, ltp)

        if not args.place_limit and not args.place_market:
            logger.info("Read-only run complete. Add --place-limit or --place-market to send an order.")
            return 0

        if args.place_limit:
            if ltp is None:
                logger.error("Cannot place a relative limit order without an LTP.")
                return 1
            factor = (1 - args.offset / 100) if args.side == "buy" else (1 + args.offset / 100)
            limit_price = round(ltp * factor, 2)
            logger.warning("Placing {} LIMIT {} {} @ {} ({}% {} LTP {}) — should rest un-filled",
                           args.side.upper(), qty, args.symbol, limit_price,
                           args.offset, "below" if args.side == "buy" else "above", ltp)
            res = broker.submit_order(symbol=args.symbol, side=args.side, quantity=qty,
                                      order_type="limit", price=limit_price,
                                      exchange_segment=args.segment, product=args.product)
            logger.info("Order result: {}", res)
            order_id = res.get("order_id")
            if res.get("status") == "error" or not order_id:
                logger.error("Limit order rejected — see message above.")
                return 1
            logger.success("Limit order accepted: id={}", order_id)
            if args.no_cancel:
                logger.warning("Leaving order {} resting (--no-cancel). Cancel it manually when done.", order_id)
            else:
                time.sleep(2)
                if broker.cancel_order(order_id):
                    logger.success("Cancelled order {}", order_id)
                else:
                    logger.error("Cancel failed for {} — CHECK AND CANCEL MANUALLY.", order_id)
                    return 1
            return 0

        if args.place_market:
            if not args.yes:
                logger.error("--place-market needs --yes (it fires a REAL fillable order). Aborting.")
                return 2
            logger.warning("Placing REAL MARKET {} {} {} @ market — this WILL fill.",
                           args.side.upper(), qty, args.symbol)
            res = broker.submit_order(symbol=args.symbol, side=args.side, quantity=qty,
                                      order_type="market", exchange_segment=args.segment, product=args.product)
            logger.info("Order result: {}", res)
            if res.get("status") == "error":
                logger.error("Market order rejected — see message above.")
                return 1
            logger.success("Market order placed: id={}", res.get("order_id"))
            time.sleep(2)
            for pos in broker.get_positions():
                logger.info("  position: {} {} net={} avg={}", pos["exchange"], pos["symbol"],
                            pos["net_quantity"], pos["average_price"])
            return 0

    finally:
        broker.disconnect()
        logger.info("Disconnected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
