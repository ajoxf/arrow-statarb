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
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from loguru import logger

from arrow_statarb.models.probability_filter import ProbabilityFilter

# India Standard Time (UTC+5:30) — trading-hours window is evaluated in IST.
_IST = timezone(timedelta(hours=5, minutes=30))


def _within_trading_hours(p: Dict) -> bool:
    th = p.get("trading_hours") or {}
    if not th.get("enabled"):
        return True
    now = datetime.now(_IST)
    start = now.replace(hour=int(th.get("start_hour", 9)), minute=int(th.get("start_min", 15)),
                        second=0, microsecond=0)
    end = now.replace(hour=int(th.get("end_hour", 15)), minute=int(th.get("end_min", 30)),
                      second=0, microsecond=0)
    return start <= now <= end


class ArrowAutoTrader:
    def __init__(
        self,
        *,
        signal_provider: Callable[[], Dict],
        params_provider: Callable[[], Dict],
        execute_fn: Callable[[str, int], Dict],
        close_fn: Callable[[str, int], Dict],
        clock: Optional[Callable[[], float]] = None,
    ):
        self._signal = signal_provider
        self._params = params_provider
        self._execute = execute_fn
        self._close = close_fn
        # Injectable wall-clock — overridden by the backtester so historical
        # timestamps drive cooldown / time-stop / holding time. Defaults to live.
        self._clock = clock or time.time

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()

        self._pos: Optional[Dict] = None          # open position, or None
        self._cooldown_until = 0.0
        self._consec_above = 0                     # consecutive ticks z ≥ +entry
        self._consec_below = 0                     # consecutive ticks z ≤ -entry
        self._exit_failures = 0                    # consecutive failed exit attempts
        self._exit_halted = False                  # ceiling hit → stop auto-exit retries
        self._exit_retry_at = 0.0                  # don't re-attempt an exit before this (backoff)
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

    def restore_position(self, pos: Optional[Dict]) -> bool:
        """Re-adopt an open position after a process restart so the running
        engine manages (and can exit/stop) a trade it didn't itself open.

        ``pos`` comes from the trade log: ``{direction, lots, entry_spread, ts,
        dry_run}``. Only the SIGN of ``entry_z`` matters to the exit logic
        (which side of the mean we entered), so it is reconstructed from the
        direction. No-op if a position is already held or ``pos`` is empty."""
        if not pos or not pos.get("direction"):
            return False
        with self._lock:
            if self._pos is not None:
                return False
            direction = pos["direction"]
            self._pos = {
                "direction": direction,
                "lots": max(1, int(pos.get("lots", 1))),
                "entry_z": -1.0 if direction == "LONG_SPREAD" else 1.0,
                "entry_spread": pos.get("entry_spread"),
                "entry_time": float(pos.get("ts") or self._clock()),
                "order_ids": [],
                "dry_run": bool(pos.get("dry_run", False)),
                "restored": True,
            }
        logger.warning("ArrowAlgo: restored open {} position ({} lot(s)) from trade log",
                       direction, self._pos["lots"])
        return True

    def restore_cooldown(self, until_ts: float) -> bool:
        """Re-arm the entry cooldown after a restart so a stop/exit right before
        shutdown doesn't allow an immediate re-entry. ``until_ts`` is an absolute
        epoch time (last close + cooldown_sec); ignored if already in the past."""
        try:
            until = float(until_ts)
        except (TypeError, ValueError):
            return False
        if until <= self._clock():
            return False
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, until)
        logger.info("ArrowAlgo: restored cooldown — {:.0f}s remaining",
                    self._cooldown_until - self._clock())
        return True

    def get_state(self) -> Dict:
        with self._lock:
            return {
                **self._snap,
                "running": self.running,
                "in_position": self._pos is not None,
                "position": dict(self._pos) if self._pos else None,
                "cooldown_s": max(0.0, round(self._cooldown_until - self._clock(), 1)),
                "exit_failures": self._exit_failures,
                "exit_halted": self._exit_halted,
                "exit_retry_s": max(0.0, round(self._exit_retry_at - self._clock(), 1)),
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
            commission_per_lot=float(p.get("brokerage_per_lot", 20.0)) * 2.0,
            slippage_per_lot=float(p.get("slippage_per_lot", 5.0)) * 2.0,
            commission_basis=str(p.get("commission_basis", "per_lot")),
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
        snap: Dict = {"ts": self._clock(),
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
        # Upper bound on entry |z|: an extremely deep z usually signals a regime
        # shift, not a reversion opportunity. 0 = disabled.
        max_entry_z = float(p.get("max_entry_zscore", 0) or 0)
        lots    = max(1, int(p.get("lots", 1)))
        half_life = float(sig.get("half_life", 0.0))
        sample_interval = float(sig.get("sample_interval_sec", 0.5))
        now = self._clock()

        # Confirmation ticks: require N consecutive ticks beyond the threshold
        # before an entry fires (filters out single-tick spikes).
        confirm = max(1, int(p.get("confirmation_ticks", 1)))
        if z >= entry_z:
            self._consec_above += 1; self._consec_below = 0
        elif z <= -entry_z:
            self._consec_below += 1; self._consec_above = 0
        else:
            self._consec_above = self._consec_below = 0
        confirmed_long = self._consec_below >= confirm   # z ≤ -entry
        confirmed_short = self._consec_above >= confirm   # z ≥ +entry

        if self._pos is None:
            mdl = float(p.get("max_daily_loss", 0) or 0)
            day_pnl = float(p.get("day_pnl", 0.0))
            if mdl > 0 and day_pnl <= -mdl:
                snap["status"] = f"daily loss limit reached (₹{day_pnl:.0f} ≤ −₹{mdl:.0f}) — entries halted"
            elif now < self._cooldown_until:
                snap["status"] = "cooldown"
            elif not _within_trading_hours(p):
                snap["status"] = "outside trading hours"
            elif (abs(z) >= entry_z and (confirmed_long or confirmed_short)
                  and max_entry_z > 0 and abs(z) > max_entry_z):
                snap["status"] = (f"blocked: |z|={abs(z):.2f} exceeds entry cap "
                                  f"{max_entry_z:.2f} (regime-shift guard)")
            elif abs(z) >= entry_z and (confirmed_long or confirmed_short):
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
                    refused = self._enter(direction, lots, z, sig.get("spread", 0.0))
                    snap["status"] = refused or f"ENTRY {direction} (z={z:.2f})"
            elif abs(z) >= entry_z:
                c = max(self._consec_above, self._consec_below)
                snap["status"] = f"confirming {c}/{confirm} (z={z:.2f})"
            else:
                snap["status"] = "flat — watching"
        else:
            entry_z_sign = self._pos["entry_z"]
            # revert through exit_z back toward the mean
            reverted = (z >= exit_z) if entry_z_sign < 0 else (z <= exit_z)
            held_sec = now - self._pos["entry_time"]
            max_hold_sec = (float(p.get("time_stop_half_lives", 3.0))
                            * half_life * sample_interval) if half_life > 0 else 0.0
            spread_now = sig.get("spread")
            if abs(z) >= stop_z:
                exit_reason = "stop"
            elif reverted:
                exit_reason = "target"
            elif max_hold_sec > 0 and held_sec >= max_hold_sec:
                exit_reason = "time_stop"
            else:
                exit_reason = None

            if exit_reason and self._exit_halted:
                # Ceiling reached — stop hammering the broker; demand attention.
                snap["status"] = (f"EXIT HALTED ({exit_reason}, z={z:.2f}) — "
                                  f"{self._exit_failures} consecutive failures; "
                                  f"close manually")
            elif exit_reason and now < self._exit_retry_at:
                # Backoff window after a failed exit — wait before re-attempting.
                wait = self._exit_retry_at - now
                snap["status"] = (f"exit retry in {wait:.0f}s (backoff after "
                                  f"{self._exit_failures} failure(s); {exit_reason}, z={z:.2f})")
            elif exit_reason == "stop":
                snap["status"] = f"STOP (z={z:.2f})"
                self._exit("stop", z, spread_now)
            elif exit_reason == "target":
                snap["status"] = f"EXIT target (z={z:.2f})"
                self._exit("target", z, spread_now)
            elif exit_reason == "time_stop":
                snap["status"] = f"TIME-STOP ({held_sec:.0f}s ≥ {max_hold_sec:.0f}s)"
                self._exit("time_stop", z, spread_now)
            else:
                snap["status"] = f"holding {self._pos['direction']} (z={z:.2f})"

        self._set_snap(snap)

    # ── actions (reuse the proven Arrow order path) ──────────────────────────
    def _enter(self, direction: str, lots: int, z: float, spread: float) -> Optional[str]:
        # Stale-signal guard: re-sample the live signal at the moment of entry
        # and refuse if the fill-time z has diverged from the DECISION z beyond
        # a configurable threshold (so a live entry can't fire on a signal that
        # has already snapped back). 0/absent ⇒ disabled. Returns a status string
        # when the entry is refused (for the snapshot), else None.
        p = self._params()
        max_div = float(p.get("max_entry_z_divergence", 0) or 0)
        if max_div > 0:
            fill_z = (self._signal() or {}).get("zscore")
            if fill_z is None or abs(float(fill_z) - z) > max_div:
                shown = "n/a" if fill_z is None else f"{float(fill_z):.2f}"
                msg = (f"stale signal: decision z={z:.2f} vs fill z={shown} "
                       f"(Δ>{max_div:.2f}) — entry refused")
                self._consec_above = self._consec_below = 0
                self._cooldown_until = self._clock() + float(p.get("cooldown", 300))
                logger.warning("ArrowAlgo: {}", msg)
                return msg

        # Pass the algo's DECISION z/spread (and source) so the journal records
        # the exact signal it acted on, not a re-sampled value at order time.
        try:
            res = self._execute(direction, lots, source="algo",
                                z=round(z, 4), spread=round(spread, 4)) or {}
        except TypeError:                        # execute_fn without the kwargs (tests)
            res = self._execute(direction, lots) or {}
        if res.get("success"):
            self._pos = {
                "direction": direction, "lots": lots,
                "entry_z": z, "entry_spread": round(spread, 2),
                "entry_time": self._clock(),
                "order_ids": [r.get("order_id") for r in res.get("results", [])],
                "dry_run": bool(res.get("dry_run")),
            }
            self._consec_above = self._consec_below = 0
            self._exit_failures = 0                 # fresh position → clean exit slate
            self._exit_halted = False
            self._exit_retry_at = 0.0
            logger.info("ArrowAlgo: ENTER {} {} lot(s) z={:.2f} → {}", direction, lots, z, res.get("message"))
        else:
            self.last_error = f"entry failed: {res.get('error')}"
            logger.error("ArrowAlgo: ENTER failed — {}", res.get("error"))

    def _exit(self, reason: str, z: float, spread: Optional[float] = None) -> None:
        if not self._pos:
            return
        kw = {"source": "algo", "reason": reason, "z": round(z, 4)}
        if spread is not None:
            kw["spread"] = round(spread, 4)
        try:
            res = self._close(self._pos["direction"], self._pos["lots"], **kw) or {}
        except TypeError:                       # close_fn without the extra kwargs (tests)
            res = self._close(self._pos["direction"], self._pos["lots"]) or {}
        if res.get("success"):
            logger.info("ArrowAlgo: EXIT ({}) {} z={:.2f} → {}", reason, self._pos["direction"], z, res.get("message"))
            self._pos = None
            self._exit_failures = 0
            self._exit_halted = False
            self._exit_retry_at = 0.0
            cooldown = float(self._params().get("cooldown", 300))
            self._cooldown_until = self._clock() + cooldown
        else:
            # Track consecutive failures; after the ceiling, halt auto-exit retries
            # so we alert the human instead of cancel-spamming the broker forever.
            self._exit_failures += 1
            p = self._params()
            ceiling = int(p.get("max_exit_failures", 0) or 0)
            if ceiling > 0 and self._exit_failures >= ceiling:
                self._exit_halted = True
            # Exponential backoff before the next attempt: base × 2^(n-1), capped.
            base = float(p.get("exit_retry_backoff", 0) or 0)
            delay = 0.0
            if base > 0 and not self._exit_halted:
                cap = float(p.get("exit_retry_backoff_max", 60) or 60)
                delay = min(base * (2 ** (self._exit_failures - 1)), cap)
                self._exit_retry_at = self._clock() + delay
            self.last_error = f"exit failed (x{self._exit_failures}): {res.get('error')}"
            if self._exit_halted:
                plan = "HALTING auto-exit — manual intervention required"
            elif delay > 0:
                plan = f"retry in {delay:.0f}s (backoff; position still open)"
            else:
                plan = "will retry next tick (position still open)"
            logger.error(
                "ArrowAlgo: EXIT FAILED — reason={} attempt={} ceiling={} orphan={} "
                "recovered={} err={} → {}",
                reason, self._exit_failures, ceiling or "∞",
                res.get("orphan"), res.get("recovered"), res.get("error"), plan)

    def _set_snap(self, snap: Dict) -> None:
        with self._lock:
            self._snap = snap
