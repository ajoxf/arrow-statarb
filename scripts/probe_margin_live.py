"""Live probe: discover Arrow's order_margin / basket_margin request + response shape.

Arrow's SDK ships no docstrings for these, so we call them against the REAL
session (reusing the persisted token — no 2FA, no orders placed) and print
whatever comes back. Read the output and we wire ArrowBroker.get_order_margin()
to the exact keys.

Run from the project root with the venv active:

    python scripts/probe_margin_live.py

Nothing is transmitted to the exchange — margin endpoints are read-only.
"""
from __future__ import annotations

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


def _load_token(app_id: str) -> str:
    try:
        data = json.loads(SESSION_FILE.read_text())
    except Exception:
        return ""
    return str(data.get("token", "")) if str(data.get("app_id", "")) == str(app_id) else ""


def _show(label, fn):
    """Call fn(), pretty-printing the result or the exception type+message."""
    print("\n" + "=" * 70)
    print(label)
    print("-" * 70)
    try:
        out = fn()
        print(type(out).__name__, "→")
        try:
            print(json.dumps(out, indent=2, default=str))
        except Exception:
            print(repr(out))
    except Exception as exc:
        print(f"!! {type(exc).__name__}: {exc}")


def main() -> int:
    cfg = Config()
    creds = dict(Config.arrow_credentials())
    creds["lot_sizes"] = cfg.get("broker.lot_overrides") or {}
    tok = _load_token(creds.get("app_id", ""))
    if tok:
        creds["token"] = tok

    broker = create_broker(cfg.get("broker.name", "arrow"), creds)
    if not broker.connect():
        print("connect failed:", getattr(broker, "last_error", "?"))
        return 1
    client = broker._client
    print("connected. client:", type(client).__name__)

    # connect() fetches the instrument master on a background thread; wait for it
    # so resolve_lot_size() returns the real 65 (not the fallback 1).
    import time
    for _ in range(60):
        if getattr(broker, "_lot_sizes", None):
            break
        time.sleep(0.5)
    print("instrument master entries:", len(getattr(broker, "_lot_sizes", {})))

    # Resolve the two legs from config (segment, symbol, lot size, LTP). When the
    # market is shut LTP is 0 and we fall back to a sane price just so the margin
    # endpoint has a value to chew on. qty is forced to a multiple of the lot.
    FALLBACK_PRICE = 24000.0
    legs = []
    for lk in ("leg_a", "leg_b"):
        seg = cfg.get(f"instruments.{lk}.segment", "nse_fo")
        sym = cfg.get(f"instruments.{lk}.symbol", "")
        side = "buy" if lk == "leg_a" else "sell"          # LONG_SPREAD
        lot = int(broker.resolve_lot_size(seg, sym))
        qty = lot if lot > 1 else 65                        # Arrow told us 65
        ltp = broker.get_ltp([{"exchange_segment": seg, "instrument_token": sym}]).get(sym.upper(), 0.0)
        px = float(ltp) if ltp else FALLBACK_PRICE
        legs.append({"seg": seg, "sym": sym, "side": side, "qty": qty, "ltp": px})
        print(f"  {lk}: {sym} {side} qty={qty} px={px} (raw_lot={lot}, raw_ltp={ltp})")

    from pyarrow_client import Exchange, OrderType, ProductType, TransactionType
    from arrow_statarb.brokers.arrow_broker import _SEGMENT_MAP

    def _enums(leg):
        ex = Exchange(_SEGMENT_MAP.get(leg["seg"].lower(), leg["seg"].upper()))
        tt = TransactionType.BUY if leg["side"] == "buy" else TransactionType.SELL
        return ex, tt

    # ── 1. order_margin per leg (fully typed signature — guaranteed callable) ──
    for leg in legs:
        ex, tt = _enums(leg)
        # 1a: MARKET — no price required
        _show(
            f"order_margin MKT  {leg['sym']} {leg['side']} x{leg['qty']}",
            lambda ex=ex, tt=tt, leg=leg: client.order_margin(
                exchange=ex, symbol=leg["sym"], quantity=leg["qty"],
                product=ProductType.NRML, order_type=OrderType.MARKET,
                transaction_type=tt, price=0.0, include_positions=False,
            ),
        )
        # 1b: LIMIT — with the (possibly fallback) price
        _show(
            f"order_margin LMT  {leg['sym']} {leg['side']} x{leg['qty']} @ {leg['ltp']}",
            lambda ex=ex, tt=tt, leg=leg: client.order_margin(
                exchange=ex, symbol=leg["sym"], quantity=leg["qty"],
                product=ProductType.NRML, order_type=OrderType.LIMIT,
                transaction_type=tt, price=leg["ltp"], include_positions=False,
            ),
        )

    # ── 2. basket_margin — quantity-as-string is accepted; the only remaining
    # rejection is "invalid order type". Dump the enum wire-values, then brute
    # force the order_type/product string the basket endpoint wants. Stop on the
    # first success (that's the netted spread margin WITH calendar benefit).
    print("\n" + "=" * 70)
    print("ENUM WIRE VALUES")
    print("-" * 70)
    for E in (Exchange, OrderType, ProductType, TransactionType):
        try:
            print(E.__name__, "→", {m.name: m.value for m in E})
        except Exception as exc:
            print(E.__name__, "?", exc)

    def basket(orders):
        return lambda: client.basket_margin(orders)

    def enc(leg, order_type, product, price):
        ex, tt = _enums(leg)
        return {"exchange": ex.value, "symbol": leg["sym"], "quantity": str(leg["qty"]),
                "product": product, "order_type": order_type,
                "transaction_type": tt.value, "price": str(price)}

    # order_type candidates (wire spellings Arrow uses elsewhere) × product
    # spellings ("NRML" enum value vs "M" wire code).
    ot_candidates = ["LMT", "LIMIT", "L", "MKT", "MARKET", "M"]
    prod_candidates = [ProductType.NRML.value, "NRML", "M"]
    got_basket = False
    for prod in prod_candidates:
        for ot in ot_candidates:
            price = "0" if ot in ("MKT", "MARKET", "M") else str(legs[0]["ltp"])
            orders = [enc(leg, ot, prod, price) for leg in legs]
            label = f"basket_margin  order_type={ot!r} product={prod!r}"
            _show(label, basket(orders))
            # crude success probe: re-run and inspect (the _show already printed);
            # we keep going so every combo's error is visible, but flag once a
            # dict with a margin field comes back.
            try:
                out = client.basket_margin(orders)
                if isinstance(out, dict) and any(k.lower().startswith("margin") or
                        "margin" in k.lower() or "required" in k.lower() for k in out):
                    print(f">>> SUCCESS with order_type={ot!r} product={prod!r}")
                    got_basket = True
                    break
            except Exception:
                pass
        if got_basket:
            break

    print("\nDone. Paste the whole output back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
