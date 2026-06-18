"""Arrow auto-trader — a focused, server-side mean-reversion executor.

Trades the SAME spread the dashboard shows, from the SAME server-side signal
(``core/signal.py``), through the SAME proven order path used by the manual
buttons. Dependency-injected so it has no web/circular imports and is testable:

  signal_provider()           -> the live signal dict (single source of truth)
  params_provider()           -> dict of strategy params (entry/exit/stop z, lots,
                                  cooldown, filter settings, lot_multiplier, …)
  execute_fn(direction, lots) -> result dict   (reuses the manual spread execute)
  close_fn(direction, lots)   -> result dict   (reuses the manual spread close)

The execute/close functions already honour dry-run/live mode, so the auto-trader
inherits dry-run safety automatically.

Signal convention (matches the dashboard / Arrow fact #10):
  spread = leg_a - leg_b ; z = (spread - mean) / std
  z <= -entry  → spread cheap  → LONG_SPREAD  (buy A / sell B)
  z >= +entry  → spread rich   → SHORT_SPREAD (sell A / buy B)
  exit when z reverts through exit_z (≈0); stop when |z| >= stop_z

Upgrades over the original port:
  * Reads the shared SignalEngine (server-side time window) instead of its own
    deque, so algo z == dashboard z exactly.
  * Probability/EV/cost gate before entry (OU gambler's-ruin model).
  * Time-stop: exit if held longer than time_stop_half_lives × half_life.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

from loguru import logger

from arrow_statarb.models.probability_filter import ProbabilityFilter


class ArrowAutoTrader:
    def __init__(
        self,
        *,
        signal_provider: Callable[[], Dict],
        params_provider: Callable[[], Dict],
        execute_fn: Callable[[str, int], Dict],
        close_fn: Callable[[str, int], Dict],
    ):
        self._signal = signal_provider
        self._params = params_provider
        self._execute = execute_fn
        self._close = close_fn

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()

        self._pos: Optional[Dict] = None          # open position, or None
        self._cooldown_until = 0.0
        self._snap: Dict = {"status": "stopped"}   # last snapshot for /state
        self.running = False
        self.last_error = ""

    # ── control ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop_evt.clear()
            self.running = True
            self.last_error = ""
            self._thread = threading.Thread(target=self._loop, daemon=True, name="ArrowAlgo")
            self._thread.start()
            logger.info("ArrowAlgo: started")
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        self.running = False
        logger.info("ArrowAlgo: stopped (open position, if any, is left untouched)")

    def get_state(self) -> Dict:
        with self._lock:
            return {
                **self._snap,
                "running": self.running,
                "in_position": self._pos is not None,
                "position": dict(self._pos) if self._pos else None,
                "cooldown_s": max(0.0, round(self._cooldown_until - time.time(), 1)),
                "last_error": self.last_error,
            }

    # ── loop ───────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception as exc:                       # never let the loop die
                self.last_error = str(exc)
                logger.exception("ArrowAlgo: tick error")
            interval = 0.5
            try:
                interval = max(0.05, float(self._params().get("tick_interval", 0.5)))
            except Exception:
                pass
            self._stop_evt.wait(interval)

    def _build_filter(self, p: Dict) -> ProbabilityFilter:
        """Construct the OU/EV gate from live params. brokerage/slippage are
        per lot PER LEG, one-way → ×2 legs (the filter's own ×2 covers
        entry+exit). lot_multiplier converts σ (₹/unit) to ₹ per lot."""
        return ProbabilityFilter(
            commission_per_lot=float(p.get("brokerage_per_lot", 10.0)) * 2.0,
            slippage_per_lot=float(p.get("slippage_per_lot", 5.0)) * 2.0,
            commission_basis="per_lot",
            lot_multiplier=float(p.get("lot_multiplier", 1.0)),
            min_win_probability=float(p.get("min_win_probability", 0.60)),
            min_expected_value=float(p.get("min_expected_value", 0.0)),
            max_half_lives=float(p.get("time_stop_half_lives", 3.0)),
            exit_zscore=float(p.get("exit_zscore", 0.0)),
            stop_zscore=float(p.get("stop_zscore", 4.0)),
            enabled=bool(p.get("enable_probability_filter", True)),
        )

    def _tick(self) -> None:
        p = self._params()
        sig = self._signal() or {}
        snap: Dict = {"ts": time.time(),
                      "leg_a": sig.get("leg_a"), "leg_b": sig.get("leg_b"),
                      "spread": sig.get("spread"), "mean": sig.get("mean"),
                      "std": sig.get("std"), "zscore": sig.get("zscore"),
                      "samples": sig.get("samples"), "half_life": sig.get("half_life")}

        z = sig.get("zscore")
        std = sig.get("std")
        if z is None or std is None:
            snap["status"] = "waiting for prices"
            self._set_snap(snap)
            return

        if not sig.get("ready"):
            need = sig.get("min_signal_minutes", 0)
            have = sig.get("span_minutes", 0)
            snap["status"] = f"collecting signal ({have:.1f}/{need:.0f} min)"
            self._set_snap(snap)
            return

        entry_z = float(p.get("entry_zscore", sig.get("entry_zscore", 2.0)))
        exit_z  = float(p.get("exit_zscore", sig.get("exit_zscore", 0.0)))
        stop_z  = float(p.get("stop_zscore", sig.get("stop_zscore", 4.0)))
        lots    = max(1, int(p.get("lots", 1)))
        half_life = float(sig.get("half_life", 0.0))
        sample_interval = float(sig.get("sample_interval_sec", 0.5))
        now = time.time()

        if self._pos is None:
            if now < self._cooldown_until:
                snap["status"] = "cooldown"
            elif abs(z) >= entry_z:
                direction = "LONG_SPREAD" if z < 0 else "SHORT_SPREAD"
                pf = self._build_filter(p)
                allow, reason, metrics = pf.check_entry(z, std, half_life, contracts=lots)
                snap["pf_reason"] = reason
                snap["pf_metrics"] = metrics
                if not allow:
                    wp = metrics.get("win_probability")
                    ev = metrics.get("expected_value")
                    extra = ""
                    if wp is not None:
                        extra = f" (P_win={wp*100:.0f}% EV=₹{ev:.0f})"
                    snap["status"] = f"blocked: {reason}{extra}"
                else:
                    snap["status"] = f"ENTRY {direction} (z={z:.2f})"
                    self._enter(direction, lots, z, sig.get("spread", 0.0))
            else:
                snap["status"] = "flat — watching"
        else:
            entry_z_sign = self._pos["entry_z"]
            # revert through exit_z back toward the mean
            reverted = (z >= exit_z) if entry_z_sign < 0 else (z <= exit_z)
            held_sec = now - self._pos["entry_time"]
            max_hold_sec = (float(p.get("time_stop_half_lives", 3.0))
                            * half_life * sample_interval) if half_life > 0 else 0.0
            if abs(z) >= stop_z:
                snap["status"] = f"STOP (z={z:.2f})"
                self._exit("stop", z)
            elif reverted:
                snap["status"] = f"EXIT target (z={z:.2f})"
                self._exit("target", z)
            elif max_hold_sec > 0 and held_sec >= max_hold_sec:
                snap["status"] = f"TIME-STOP ({held_sec:.0f}s ≥ {max_hold_sec:.0f}s)"
                self._exit("time_stop", z)
            else:
                snap["status"] = f"holding {self._pos['direction']} (z={z:.2f})"

        self._set_snap(snap)

    # ── actions (reuse the proven Arrow order path) ──────────────────────────
    def _enter(self, direction: str, lots: int, z: float, spread: float) -> None:
        res = self._execute(direction, lots) or {}
        if res.get("success"):
            self._pos = {
                "direction": direction, "lots": lots,
                "entry_z": z, "entry_spread": round(spread, 2),
                "entry_time": time.time(),
                "order_ids": [r.get("order_id") for r in res.get("results", [])],
                "dry_run": bool(res.get("dry_run")),
            }
            logger.info("ArrowAlgo: ENTER {} {} lot(s) z={:.2f} → {}", direction, lots, z, res.get("message"))
        else:
            self.last_error = f"entry failed: {res.get('error')}"
            logger.error("ArrowAlgo: ENTER failed — {}", res.get("error"))

    def _exit(self, reason: str, z: float) -> None:
        if not self._pos:
            return
        res = self._close(self._pos["direction"], self._pos["lots"]) or {}
        if res.get("success"):
            logger.info("ArrowAlgo: EXIT ({}) {} z={:.2f} → {}", reason, self._pos["direction"], z, res.get("message"))
            self._pos = None
            cooldown = float(self._params().get("cooldown", 300))
            self._cooldown_until = time.time() + cooldown
        else:
            self.last_error = f"exit failed: {res.get('error')}"
            logger.error("ArrowAlgo: EXIT failed — {} (position still open!)", res.get("error"))

    def _set_snap(self, snap: Dict) -> None:
        with self._lock:
            self._snap = snap
