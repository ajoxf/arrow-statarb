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

    # Resolve the two legs from config (segment, symbol, lot size, LTP).
    legs = []
    for lk in ("leg_a", "leg_b"):
        seg = cfg.get(f"instruments.{lk}.segment", "nse_fo")
        sym = cfg.get(f"instruments.{lk}.symbol", "")
        side = "buy" if lk == "leg_a" else "sell"          # LONG_SPREAD
        lot = int(broker.resolve_lot_size(seg, sym))
        ltp = broker.get_ltp([{"segment": seg, "symbol": sym}]).get(sym, 0.0)
        legs.append({"seg": seg, "sym": sym, "side": side, "qty": lot, "ltp": float(ltp or 0.0)})
        print(f"  {lk}: {sym} {side} qty={lot} ltp={ltp}")

    from pyarrow_client import Exchange, OrderType, ProductType, TransactionType
    from arrow_statarb.brokers.arrow_broker import _SEGMENT_MAP

    def _enums(leg):
        ex = Exchange(_SEGMENT_MAP.get(leg["seg"].lower(), leg["seg"].upper()))
        tt = TransactionType.BUY if leg["side"] == "buy" else TransactionType.SELL
        return ex, tt

    # ── 1. order_margin per leg (fully typed signature — guaranteed callable) ──
    for leg in legs:
        ex, tt = _enums(leg)
        _show(
            f"order_margin  {leg['sym']} {leg['side']} x{leg['qty']} @ {leg['ltp']}",
            lambda ex=ex, tt=tt, leg=leg: client.order_margin(
                exchange=ex, symbol=leg["sym"], quantity=leg["qty"],
                product=ProductType.NRML, order_type=OrderType.LIMIT,
                transaction_type=tt, price=leg["ltp"], include_positions=False,
            ),
        )

    # ── 2. basket_margin — shape unknown; try a few plausible dict encodings ──
    def basket(encode):
        return lambda: client.basket_margin([encode(leg) for leg in legs])

    # 2a: enum objects, keys mirroring order_margin params
    def enc_enum(leg):
        ex, tt = _enums(leg)
        return {"exchange": ex, "symbol": leg["sym"], "quantity": leg["qty"],
                "product": ProductType.NRML, "order_type": OrderType.LIMIT,
                "transaction_type": tt, "price": leg["ltp"]}

    # 2b: plain string values
    def enc_str(leg):
        return {"exchange": _SEGMENT_MAP.get(leg["seg"].lower(), leg["seg"].upper()),
                "symbol": leg["sym"], "quantity": leg["qty"],
                "product": "NRML", "order_type": "LIMIT",
                "transaction_type": "BUY" if leg["side"] == "buy" else "SELL",
                "price": leg["ltp"]}

    # 2c: enum .value strings
    def enc_val(leg):
        ex, tt = _enums(leg)
        return {"exchange": ex.value, "symbol": leg["sym"], "quantity": leg["qty"],
                "product": ProductType.NRML.value, "order_type": OrderType.LIMIT.value,
                "transaction_type": tt.value, "price": leg["ltp"]}

    _show("basket_margin  [enum objects]", basket(enc_enum))
    _show("basket_margin  [string values]", basket(enc_str))
    _show("basket_margin  [enum .value]", basket(enc_val))

    print("\nDone. Paste the whole output back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
