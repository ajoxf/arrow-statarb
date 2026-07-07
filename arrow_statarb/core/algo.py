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
from typing import Callable, Dict, Optional, Tuple

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


def _entry_cutoff_reached(p: Dict) -> bool:
    """True if we are within ``no_entry_buffer_min`` minutes of the exchange
    close — block NEW entries near the close (exits stay allowed) so we don't
    open a position we can't manage before EOD. Applies on NSE/BSE regardless of
    whether the optional trading-hours window is enabled."""
    th = p.get("trading_hours") or {}
    # Default 0 (disabled) when unset — production enables it via config
    # (_algo_params passes trading_hours.no_entry_buffer_min, default 20).
    buf = float(p.get("no_entry_buffer_min", th.get("no_entry_buffer_min", 0)) or 0)
    if buf <= 0:
        return False
    now = datetime.now(_IST)
    close_mod = (int(th.get("close_hour", th.get("end_hour", 15))) * 60
                 + int(th.get("close_min", th.get("end_min", 30))))
    now_mod = now.hour * 60 + now.minute + now.second / 60.0
    # Minutes until the NEXT close, wrap-safe (mod 1440) so a close time that
    # lands on the other side of midnight never inverts the comparison the way
    # now.replace(hour=…) would. Block only inside the buffer BEFORE the close.
    minutes_to_close = (close_mod - now_mod) % 1440
    return minutes_to_close <= buf


class ArrowAutoTrader:
    def __init__(
        self,
        *,
        signal_provider: Callable[[], Dict],
        params_provider: Callable[[], Dict],
        execute_fn: Callable[[str, int], Dict],
        close_fn: Callable[[str, int], Dict],
        clock: Optional[Callable[[], float]] = None,
        prices_provider: Optional[Callable[[], Tuple[Optional[float], Optional[float]]]] = None,
    ):
        self._signal = signal_provider
        self._params = params_provider
        self._execute = execute_fn
        self._close = close_fn
        # Fresh (leg_a, leg_b) reader for the entry spread-divergence guard. Reads
        # the live price cache at ORDER time, which is fresher than the (sampled)
        # signal — so it catches a stale far-leg quote the z-guard can't. Optional.
        self._prices = prices_provider
        # Injectable wall-clock — overridden by the backtester so historical
        # timestamps drive cooldown / time-stop / holding time. Defaults to live.
        self._clock = clock or time.time

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()

        self._pos: Optional[Dict] = None          # open position, or None
        self._cooldown_until = 0.0
        # After a STOP, block same-direction re-entry until z re-enters the exit
        # band (z-reset gate) — stops chasing a runaway trend back in. None = clear.
        self._stop_block_dir: Optional[str] = None
        # Regime guard: once a day is flagged TRENDING, latch a halt on new
        # entries until the next IST day (auto-rearm). Stores the halted day index.
        self._regime_halt_day: Optional[int] = None
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
                # the trade-log OPEN records the FILL spread, so it is the right
                # P&L reference for a position re-adopted after a restart
                "entry_fill_spread": pos.get("entry_spread"),
                "entry_time": float(pos.get("ts") or self._clock()),
                "peak_pnl": 0.0,                 # trailing-stop high-water mark (fresh)
                "order_ids": [],
                "dry_run": bool(pos.get("dry_run", False)),
                "restored": True,
            }
        logger.warning("ArrowAlgo: restored open {} position ({} lot(s)) from trade log",
                       direction, self._pos["lots"])
        return True

    def clear_position(self, reason: str = "reconcile") -> bool:
        """Force-drop the engine's belief in an open position (used by the
        reconciler when the exchange shows FLAT but the engine thinks it is
        in-trade). Safe — touches only in-memory state, places no orders."""
        with self._lock:
            if self._pos is None:
                return False
            direction = self._pos.get("direction")
            self._pos = None
            self._exit_failures = 0
            self._exit_halted = False
            self._exit_retry_at = 0.0
        logger.warning("ArrowAlgo: force-cleared engine position ({}) — {}", direction, reason)
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

    def _live_net_pnl(self, cur_spread: Optional[float], p: Dict) -> Optional[float]:
        """Live mark-to-market net P&L (₹) of the open position, or None when it
        cannot be computed yet (flat, or no current price).

        gross = Δspread × lots × lot_size — the spread is in ₹/unit so it must be
        scaled by the contract's unit count. The reference is the actual ENTRY
        FILL spread (which already embeds entry slippage); the current side is
        the live MID spread, so the EXIT's own slippage is NOT pre-deducted —
        the live figure is a touch rosier than the eventual realized P&L, and ₹
        targets should be set with a little margin. Fees are the flat round-trip
        brokerage (2 legs × entry+exit), matching the trade-log convention.
        LONG profits when the spread rises; SHORT when it falls."""
        pos = self._pos
        if not pos or cur_spread is None:
            return None
        entry = pos.get("entry_fill_spread")
        if entry is None:
            entry = pos.get("entry_spread")
        if entry is None:
            return None
        lots = max(1, int(pos.get("lots", 1)))
        lot_mult = float(p.get("lot_multiplier", 1.0) or 1.0)
        change = ((cur_spread - entry) if pos["direction"] == "LONG_SPREAD"
                  else (entry - cur_spread))
        gross = change * lots * lot_mult
        rt_fees = float(p.get("brokerage_per_lot", 20.0) or 0) * lots * 4.0
        # STT (Securities Transaction Tax): sell-side % of notional. A calendar
        # round trip has TWO sells (one leg at entry, the other at exit), each on
        # ~one leg's notional (price × lots × lot_size). Configurable, 0 = off.
        stt_pct = float(p.get("stt_pct", 0) or 0) / 100.0
        if stt_pct > 0:
            ref = pos.get("entry_leg_a") or pos.get("entry_leg_b")
            if ref:
                rt_fees += 2.0 * stt_pct * float(ref) * lots * lot_mult
        return gross - rt_fees

    def _reversion_allowed(self, net_pnl: Optional[float], p: Dict) -> bool:
        """Gate the z-reversion ("target") exit on P&L — never book a losing
        profit-take. When ``reversion_require_profit`` is on, the reversion exit
        may fire only if net ≥ ``reversion_gate_inr`` (0 = break-even). Fail-open
        when P&L can't be priced (net None) so a missing price never traps a
        position. Off (default) ⇒ unchanged behaviour."""
        if not bool(p.get("reversion_require_profit", False)):
            return True
        if net_pnl is None:
            return True                              # fail-open
        return net_pnl >= float(p.get("reversion_gate_inr", 0) or 0)

    def _round_trip_cost(self, p: Dict) -> float:
        """Estimated round-trip cost in ₹: brokerage + slippage (both ×2 legs
        ×2 in/out) + STT (2 sells × %-of-notional). Uses the entry leg price as
        the notional basis."""
        pos = self._pos or {}
        lots = int(pos.get("lots", p.get("lots", 1)) or 1)
        lot_m = float(p.get("lot_multiplier", 1.0) or 1.0)
        cost = float(p.get("brokerage_per_lot", 20.0) or 0) * lots * 4.0
        cost += float(p.get("slippage_per_lot", 5.0) or 0) * lots * 4.0
        stt_pct = float(p.get("stt_pct", 0) or 0) / 100.0
        ref = pos.get("entry_leg_a") or pos.get("entry_leg_b")
        if stt_pct > 0 and ref:
            cost += 2.0 * stt_pct * float(ref) * lots * lot_m
        return cost

    def _effective_exit_levels(self, p: Dict) -> Tuple[float, float]:
        """Resolve the ₹ (dollar_stop, profit_target) with scale-invariant
        precedence, so the levels survive resizing/re-vol:
          target: σ-fraction > %-of-capital > fixed-₹, then raised to a cost floor;
          stop:   min(target/RR, %-of-capital × capital_at_risk) — the TIGHTER
                  binds — with fixed-₹ as the fallback.
        Fixed-₹ fields are used only when their scale-invariant twin is unset."""
        pos = self._pos or {}
        lots = int(pos.get("lots", p.get("lots", 1)) or 1)
        lot_m = float(p.get("lot_multiplier", 1.0) or 1.0)
        car = float(p.get("capital_at_risk_inr", 0) or 0)
        # ── target ──
        target = float(p.get("profit_target_inr", 0) or 0)
        sfrac = float(p.get("profit_target_sigma_frac", 0) or 0)
        std0 = float(pos.get("entry_std", 0) or 0)
        absz = abs(float(pos.get("entry_z", 0) or 0))
        tp_cap = float(p.get("tp_capital_pct", 0) or 0)
        if sfrac > 0 and std0 > 0 and absz > 0:
            target = sfrac * absz * std0 * lots * lot_m
        elif tp_cap > 0 and car > 0:
            target = (tp_cap / 100.0) * car
        cost_mult = float(p.get("cost_floor_mult", 0) or 0)
        if cost_mult > 0 and target > 0:
            target = max(target, cost_mult * self._round_trip_cost(p))
        # ── stop ──
        stop = float(p.get("dollar_stop_inr", 0) or 0)
        cands = []
        rr = float(p.get("stop_rr", 0) or 0)
        if rr > 0 and target > 0:
            cands.append(target / rr)
        scap = float(p.get("stop_capital_pct", 0) or 0)
        if scap > 0 and car > 0:
            cands.append((scap / 100.0) * car)
        if cands:
            stop = min(cands)
        return round(stop, 2), round(target, 2)

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
            want_dir = "LONG_SPREAD" if z < 0 else "SHORT_SPREAD"
            # z-reset: clear a post-stop block once z has recovered toward the
            # mean — a LONG block (entered deep-negative) clears when z ≥ −exit_z;
            # a SHORT block when z ≤ +exit_z (robust when exit_z = 0).
            if self._stop_block_dir == "LONG_SPREAD" and z >= -exit_z:
                self._stop_block_dir = None
            elif self._stop_block_dir == "SHORT_SPREAD" and z <= exit_z:
                self._stop_block_dir = None
            streak = int(p.get("loss_streak", 0) or 0)
            pause_at = int(p.get("loss_streak_pause_at", 0) or 0)
            # Regime guard: latch a day-long halt once the spread is flagged
            # TRENDING (auto-rearm next IST day); optional trend-direction filter.
            reg_enabled = bool(p.get("regime_enabled", False))
            reg_state = sig.get("regime")
            reg_slope = float((sig.get("regime_detail") or {}).get("slope", 0.0) or 0.0)
            today = int((self._clock() + 5.5 * 3600) // 86400)
            if self._regime_halt_day is not None and self._regime_halt_day != today:
                self._regime_halt_day = None                     # auto-rearm next day
            if (reg_enabled and bool(p.get("regime_halt_on_trending", True))
                    and reg_state == "TRENDING"):
                self._regime_halt_day = today                    # latch for the day
            regime_halted = reg_enabled and self._regime_halt_day == today
            trend_blocks = (reg_enabled and bool(p.get("regime_trend_direction_filter", False))
                            and ((reg_slope > 0 and want_dir == "LONG_SPREAD")
                                 or (reg_slope < 0 and want_dir == "SHORT_SPREAD")))
            if mdl > 0 and day_pnl <= -mdl:
                snap["status"] = f"daily loss limit reached (₹{day_pnl:.0f} ≤ −₹{mdl:.0f}) — entries halted"
            elif pause_at > 0 and streak >= pause_at:
                snap["status"] = f"paused: {streak}-loss streak (≥ {pause_at}) — entries halted"
            elif regime_halted:
                snap["status"] = "regime TRENDING — new entries halted for the day (auto-rearm tomorrow)"
            elif now < self._cooldown_until:
                snap["status"] = "cooldown"
            elif not _within_trading_hours(p):
                snap["status"] = "outside trading hours"
            elif _entry_cutoff_reached(p):
                buf = float(p.get("no_entry_buffer_min", 0) or 0)
                snap["status"] = f"no new entries — within {buf:.0f} min of close"
            elif (abs(z) >= entry_z and (confirmed_long or confirmed_short) and trend_blocks):
                snap["status"] = (f"trend filter: {'SHORT' if reg_slope > 0 else 'LONG'}-only "
                                  f"(S {'rising' if reg_slope > 0 else 'falling'})")
            elif (abs(z) >= entry_z and (confirmed_long or confirmed_short)
                  and self._stop_block_dir == want_dir):
                snap["status"] = (f"z-reset: blocking {want_dir.replace('_SPREAD','')} "
                                  f"re-entry until z re-enters ±{exit_z:.1f} band (z={z:.2f})")
            elif (abs(z) >= entry_z and (confirmed_long or confirmed_short)
                  and max_entry_z > 0 and abs(z) > max_entry_z):
                snap["status"] = (f"blocked: |z|={abs(z):.2f} exceeds entry cap "
                                  f"{max_entry_z:.2f} (regime-shift guard)")
            elif abs(z) >= entry_z and (confirmed_long or confirmed_short):
                direction = want_dir
                # Loss-streak size reducer: shrink size on a losing run.
                eff_lots = lots
                reduce_at = int(p.get("loss_streak_reduce_at", 0) or 0)
                if reduce_at > 0 and streak >= reduce_at:
                    pct = float(p.get("loss_streak_reduce_pct", 0) or 0) / 100.0
                    eff_lots = max(1, int(round(lots * (1.0 - pct))))
                pf = self._build_filter(p)
                allow, reason, metrics = pf.check_entry(z, std, half_life, contracts=eff_lots)
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
                    refused = self._enter(direction, eff_lots, z, sig.get("spread", 0.0))
                    snap["status"] = refused or f"ENTRY {direction} (z={z:.2f}, {eff_lots} lot(s))"
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
            min_hold_sec = float(p.get("min_hold_sec", 0.0))
            max_hold_sec = (float(p.get("time_stop_half_lives", 3.0))
                            * half_life * sample_interval) if half_life > 0 else 0.0
            spread_now = sig.get("spread")

            # ── live mark-to-market net P&L on the open position (₹) ──────────
            net_pnl = self._live_net_pnl(spread_now, p)
            dollar_stop, profit_target = self._effective_exit_levels(p)
            snap["net_pnl"] = round(net_pnl, 2) if net_pnl is not None else None
            snap["dollar_stop"] = -dollar_stop if dollar_stop > 0 else None
            snap["profit_target"] = profit_target if profit_target > 0 else None
            _pnl_txt = f"₹{net_pnl:.0f}" if net_pnl is not None else "n/a"

            # ── position detail for the Signal & Position card ───────────────
            entry_ref = self._pos.get("entry_fill_spread")
            if entry_ref is None:
                entry_ref = self._pos.get("entry_spread")
            snap["entry_spread"] = entry_ref
            snap["held_sec"] = round(held_sec, 1)
            snap["max_hold_sec"] = round(max_hold_sec, 1) if max_hold_sec > 0 else None
            snap["remaining_sec"] = (round(max(0.0, max_hold_sec - held_sec), 1)
                                     if max_hold_sec > 0 else None)
            snap["delta_spread"] = (round(spread_now - entry_ref, 4)
                                    if spread_now is not None and entry_ref is not None else None)
            _lot_mult = float(p.get("lot_multiplier", 1.0) or 1.0)
            _la = sig.get("leg_a")
            snap["notional"] = (round(self._pos["lots"] * _lot_mult * float(_la))
                                if _la else None)

            # Minimum hold: suppress the reversion/target exit until the trade has
            # lived long enough that REAL reversion — not a single noisy tick —
            # decides the outcome. Risk overrides (dollar stop, z-stop) are NEVER
            # suppressed; the time-stop is a maximum so it is always beyond it.
            hold_gated = reverted and min_hold_sec > 0 and held_sec < min_hold_sec

            # ── Phase 2: peak tracking, max-hold upgrade, trailing stop ──────
            # Peak P&L high-water mark (drives the trailing stop); reset at entry.
            peak = float(self._pos.get("peak_pnl", 0.0) or 0.0)
            if net_pnl is not None and net_pnl > peak:
                peak = net_pnl
                self._pos["peak_pnl"] = peak
            snap["peak_pnl"] = round(peak, 2) if net_pnl is not None else None

            # Max-hold (the time-stop), upgraded:
            #  • silent when losing — but ONLY when a ₹ stop is armed to catch the
            #    loss; without that backstop it still fires (no stuck losers);
            #  • z-progress gate — a WINNING trade that has reverted ≥ the gate
            #    fraction of the way from entry |z| to exit |z| is let run.
            max_hold_due = max_hold_sec > 0 and held_sec >= max_hold_sec
            max_hold_expired = max_hold_due
            if max_hold_due:
                silent = bool(p.get("max_hold_silent_when_losing", False)) and dollar_stop > 0
                if silent and net_pnl is not None and net_pnl <= 0:
                    max_hold_due = False
                z_prog_min = float(p.get("max_hold_z_progress_min", 0) or 0)
                if max_hold_due and z_prog_min > 0 and net_pnl is not None and net_pnl > 0:
                    entry_abs, exit_abs = abs(self._pos["entry_z"]), abs(exit_z)
                    journey = entry_abs - exit_abs
                    z_prog = ((entry_abs - abs(z)) / journey) if journey > 1e-9 else 1.0
                    if z_prog >= z_prog_min:
                        max_hold_due = False        # winning & reverting → run to target
            snap["max_hold_expired"] = bool(max_hold_expired)

            # Trailing stop: arm once the peak clears the floor (a % of the profit
            # target, or simply 'in profit' when no target/floor is set), then
            # fire when P&L pulls back trail_pct from the peak.
            trail_pct = float(p.get("trailing_stop_pct", 0) or 0)
            floor_pct = float(p.get("trailing_stop_floor_pct", 0) or 0)
            trailing_fire = trailing_armed = False
            if trail_pct > 0 and net_pnl is not None and peak > 0:
                trailing_armed = (peak >= (floor_pct / 100.0) * profit_target
                                  if floor_pct > 0 and profit_target > 0 else True)
                if trailing_armed and net_pnl < peak * (1.0 - trail_pct / 100.0):
                    trailing_fire = True
            snap["trailing_armed"] = trailing_armed

            # Exit priority (first match wins) — RISK BEFORE REWARD:
            #   1 dollar stop · 2 profit target · 3 max-hold (silent/gated) ·
            #   4 trailing stop · 5 emergency z-stop · 6 z-target (min-hold gated)
            if dollar_stop > 0 and net_pnl is not None and net_pnl <= -dollar_stop:
                exit_reason = "dollar_stop"
            elif profit_target > 0 and net_pnl is not None and net_pnl >= profit_target:
                exit_reason = "profit_target"
            elif max_hold_due:
                exit_reason = "time_stop"
            elif trailing_fire:
                exit_reason = "trailing_stop"
            elif abs(z) >= stop_z:
                exit_reason = "stop"
            elif reverted and not hold_gated and self._reversion_allowed(net_pnl, p):
                exit_reason = "target"
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
            elif exit_reason == "dollar_stop":
                snap["status"] = f"STOP PRICE (net {_pnl_txt} ≤ −₹{dollar_stop:.0f})"
                self._exit("dollar_stop", z, spread_now)
            elif exit_reason == "profit_target":
                snap["status"] = f"PROFIT TARGET (net {_pnl_txt} ≥ ₹{profit_target:.0f})"
                self._exit("profit_target", z, spread_now)
            elif exit_reason == "time_stop":
                snap["status"] = f"TIME-STOP ({held_sec:.0f}s ≥ {max_hold_sec:.0f}s, net {_pnl_txt})"
                self._exit("time_stop", z, spread_now)
            elif exit_reason == "trailing_stop":
                snap["status"] = f"TRAILING STOP (net {_pnl_txt}, peak ₹{peak:.0f})"
                self._exit("trailing_stop", z, spread_now)
            elif exit_reason == "stop":
                snap["status"] = f"STOP (z={z:.2f})"
                self._exit("stop", z, spread_now)
            elif exit_reason == "target":
                snap["status"] = f"EXIT target (z={z:.2f})"
                self._exit("target", z, spread_now)
            elif hold_gated:
                snap["status"] = (f"min-hold {held_sec:.0f}s/{min_hold_sec:.0f}s — "
                                  f"reverted but holding (z={z:.2f}, net {_pnl_txt})")
            else:
                _extra = " · max-hold EXPIRED (gated)" if max_hold_expired else ""
                snap["status"] = (f"holding {self._pos['direction']} "
                                  f"(z={z:.2f}, net {_pnl_txt}){_extra}")

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

        # Fresh-price spread guard: the z-guard above compares signal-to-signal,
        # so it can't see a STALE far-leg quote (a phantom z). Here we read the
        # live leg prices at ORDER time (fresher than the sampled signal) and
        # refuse if the real, executable spread has diverged from the DECISION
        # spread by more than the threshold (in spread points). 0/absent ⇒ off.
        max_sdiv = float(p.get("max_entry_spread_divergence", 0) or 0)
        if max_sdiv > 0 and self._prices is not None:
            try:
                la, lb = self._prices()
            except Exception:
                la = lb = None
            live_spread = (float(la) - float(lb)) if (la is not None and lb is not None) else None
            if live_spread is None or abs(live_spread - spread) > max_sdiv:
                shown = "n/a" if live_spread is None else f"{live_spread:.1f}"
                msg = (f"stale signal: decision spread={spread:.1f} vs live={shown} "
                       f"(Δ>{max_sdiv:.1f}) — entry refused")
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
            # entry_fill_spread = the ACTUAL executed spread (avg fills), used for
            # live ₹ P&L; falls back to the decision spread when the execute path
            # can't report fills (dry-run stubs / tests).
            fill = res.get("fill_spread")
            self._pos = {
                "direction": direction, "lots": lots,
                "entry_z": z, "entry_spread": round(spread, 2),
                "entry_std": (self._signal() or {}).get("std"),   # σ frozen at entry
                "entry_fill_spread": (float(fill) if fill is not None else round(spread, 2)),
                "entry_leg_a": res.get("leg_a_fill"),
                "entry_leg_b": res.get("leg_b_fill"),
                "entry_time": self._clock(),
                "peak_pnl": 0.0,                 # trailing-stop high-water mark
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
            closed_dir = self._pos["direction"]
            logger.info("ArrowAlgo: EXIT ({}) {} z={:.2f} → {}", reason, closed_dir, z, res.get("message"))
            self._pos = None
            self._exit_failures = 0
            self._exit_halted = False
            self._exit_retry_at = 0.0
            p = self._params()
            cooldown = float(p.get("cooldown", 300))
            # A STOP earns a longer cooldown and (optionally) arms the z-reset
            # gate so we don't re-enter the same direction into a runaway move.
            if reason in ("stop", "dollar_stop"):
                cooldown = max(cooldown, float(p.get("stop_cooldown", 0) or 0))
                if bool(p.get("z_reset_after_stop", False)):
                    self._stop_block_dir = closed_dir
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
