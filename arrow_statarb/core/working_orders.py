"""Working (resting) spread orders — good-till-cancelled, watch-and-fire.

A spread has no order book of its own, so a resting order here is a price
the app WATCHES: the live executable spreads are checked several times a
second, and the moment one reaches the order's price both legs are sent
through the normal manual order path (the same lock, journal and fills
record as a click on BUY / SELL).

    SELL at P fires when the Sell spread (k·bid A − ask B) ≥ P
    BUY  at P fires when the Buy spread  (k·ask A − bid B) ≤ P

so a SELL above the market waits for the spread to rise, a BUY below it
waits for it to fall, and one placed at or through the market fires at
once. The fire is a market order on both legs: the order's price is the
TRIGGER, the fill can slip (the Fills window records by how much).

Rules (one position at a time, as everywhere else in the app):
  * flat                      → the order OPENS a position
  * opposite side, same lots  → the order CLOSES the open position
  * anything else             → the order is HELD (kept, shown as held,
                                fires once the position no longer stops it)
  * a broker/engine refusal   → the order is REJECTED with that reason and
                                never retried — an order is sent at most once
Orders live until cancelled (GTC) and survive restarts (saved to disk).
They never fire while quotes are stale or the market is closed.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

#: (sell_spread, buy_spread, fresh) — fresh = both legs ticking & market open
QuotesFn = Callable[[], Tuple[Optional[float], Optional[float], bool]]
#: order → (allowed, "open" | "close" | reason held)
GateFn = Callable[[Dict], Tuple[bool, str]]
#: (order, action "open"/"close") → result {success, error, message}
FireFn = Callable[[Dict, str], Dict]

ACTIVE = ("working", "held")
#: Refusals that are not about the ORDER but about a moment: retried later.
TRANSIENT = ("still executing",)
HISTORY = 50


class WorkingOrders:
    def __init__(self, path: Optional[Path], quotes_fn: QuotesFn, gate_fn: GateFn,
                 fire_fn: FireFn, clock: Callable[[], float] = time.time,
                 interval: float = 0.25):
        self._path = Path(path) if path else None
        self._quotes, self._gate, self._fire = quotes_fn, gate_fn, fire_fn
        self._clock = clock
        self._interval = interval
        self._lock = threading.RLock()
        self._orders: List[Dict] = []
        self._thread: Optional[threading.Thread] = None
        self._load()

    # ── book ──────────────────────────────────────────────────────────────────
    def place(self, side: str, price: float, lots: int) -> Dict:
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        lots = int(lots)
        if lots < 1:
            raise ValueError("lots must be at least 1")
        o = {"id": uuid.uuid4().hex[:8], "side": side, "price": round(float(price), 4),
             "lots": lots, "status": "working", "note": "", "created": self._clock(),
             "updated": self._clock(), "source": "manual"}
        with self._lock:
            self._orders.append(o)
            self._save()
        logger.info("Working order placed: {} {} lot(s) @ {} (id {})", side, lots, o["price"], o["id"])
        return dict(o)

    def cancel(self, oid: str, reason: str = "cancelled") -> Optional[Dict]:
        with self._lock:
            for o in self._orders:
                if o["id"] == oid and o["status"] in ACTIVE:
                    self._set(o, "cancelled", reason)
                    logger.info("Working order cancelled: {} {} @ {} — {}", o["side"], o["lots"],
                                o["price"], reason)
                    self._save()
                    return dict(o)
        return None

    def cancel_all(self, reason: str = "cancelled") -> int:
        n = 0
        with self._lock:
            for o in self._orders:
                if o["status"] in ACTIVE:
                    self._set(o, "cancelled", reason)
                    n += 1
            if n:
                logger.info("Working orders cancelled: {} — {}", n, reason)
                self._save()
        return n

    def active(self) -> List[Dict]:
        with self._lock:
            return [dict(o) for o in self._orders if o["status"] in ACTIVE]

    def all(self) -> List[Dict]:
        with self._lock:
            return [dict(o) for o in self._orders]

    # ── the watcher ───────────────────────────────────────────────────────────
    def check_once(self) -> List[Dict]:
        """One pass: fire every order whose price the market has reached.
        Returns the orders that were sent (for tests and logging)."""
        try:
            sell, buy, fresh = self._quotes()
        except Exception:                             # noqa: BLE001
            return []
        if not fresh:
            return []
        sent = []
        with self._lock:
            pending = [o for o in self._orders if o["status"] in ACTIVE]
        for o in sorted(pending, key=lambda x: x["created"]):
            if o["side"] == "SELL":
                hit = sell is not None and sell >= o["price"]
            else:
                hit = buy is not None and buy <= o["price"]
            if not hit:
                if o["status"] == "held":                 # the price moved away again
                    with self._lock:
                        self._set(o, "working", "")
                continue
            try:
                ok, how = self._gate(o)
            except Exception as exc:                  # noqa: BLE001
                ok, how = False, f"check failed: {exc}"
            if not ok:
                with self._lock:
                    if o["status"] != "held" or o["note"] != how:
                        self._set(o, "held", how)
                        self._save()
                continue
            with self._lock:
                if o["status"] not in ACTIVE:             # cancelled meanwhile
                    continue
                self._set(o, "firing", f"{how} at Sell {sell} / Buy {buy}")
            logger.info("Working order FIRING: {} {} lot(s) @ {} ({}; Sell {} / Buy {})",
                        o["side"], o["lots"], o["price"], how, sell, buy)
            try:
                res = self._fire(o, how) or {}
            except Exception as exc:                  # noqa: BLE001
                res = {"success": False, "error": str(exc)}
            with self._lock:
                if res.get("success"):
                    self._set(o, "filled", res.get("message") or how)
                    o["fill_spread"] = res.get("fill_spread")
                    o["filled_at"] = self._clock()
                    logger.info("Working order FILLED: {} {} lot(s) @ {} — {}", o["side"],
                                o["lots"], o["price"], res.get("message", ""))
                else:
                    err = str(res.get("error") or "refused")
                    if any(t in err for t in TRANSIENT):
                        self._set(o, "working", "")       # a moment's clash, not the order
                    else:
                        self._set(o, "rejected", err)
                        logger.warning("Working order REJECTED (not retried): {} {} @ {} — {}",
                                       o["side"], o["lots"], o["price"], err)
                self._save()
            sent.append(dict(o))
        return sent

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                try:
                    self.check_once()
                except Exception as exc:              # noqa: BLE001 — keep watching
                    logger.warning("Working orders: check failed — {}", exc)
                time.sleep(self._interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="WorkingOrders")
        self._thread.start()

    # ── internals ─────────────────────────────────────────────────────────────
    def _set(self, o: Dict, status: str, note: str) -> None:
        o["status"], o["note"], o["updated"] = status, note, self._clock()

    def _save(self) -> None:
        if not self._path:
            return
        with self._lock:
            live = [o for o in self._orders if o["status"] in ACTIVE + ("firing",)]
            done = [o for o in self._orders if o not in live][-HISTORY:]
            self._orders = done + live
            data = {"orders": self._orders}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._path)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Working orders: could not save {}: {}", self._path, exc)

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Working orders: could not read {}: {}", self._path, exc)
            return
        self._orders = list(data.get("orders") or [])
        for o in self._orders:
            if o.get("status") == "firing":
                # The app stopped while an order was being sent: whether it
                # reached the exchange is unknown — never resend it blindly.
                self._set(o, "rejected", "the app stopped while this order was being sent — "
                                         "check the Order Log and positions")
        n = sum(1 for o in self._orders if o.get("status") in ACTIVE)
        if n:
            logger.info("Working orders restored from disk: {} active (GTC)", n)
