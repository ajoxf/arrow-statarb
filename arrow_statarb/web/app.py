"""Flask + SocketIO web application for the Arrow StatArb spread trader.

Two pages:
  /            setup   — connect Arrow, pick instruments, assign the 2 legs
  /dashboard   trade   — live prices, server signal (z), position+P&L, the
                         DRY-RUN/LIVE badge, the lot-mismatch warning, and the
                         Algorithm Active toggle that drives the auto-trader.

One active broker (chosen from config) via the registry. One SignalEngine is the
single source of truth for the spread/z — the dashboard reads it (``/api/signal``)
and the algo reads it too, so their z is always identical. The manual buttons and
the algo share ONE order path (``_spread_order``), segment-aware, lot-multiplied,
mpp market orders, gated by the dry-run/live mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from loguru import logger

from arrow_statarb.config.config import Config, CONFIG_DIR, LEG_ASSIGNMENTS_FILE, PROJECT_ROOT
from arrow_statarb.brokers.registry import ActiveBroker, create_broker
from arrow_statarb.brokers.sim_broker import SimBroker
from arrow_statarb.core.signal import SignalEngine
from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.executor import SpreadExecutor, LegOrder
from arrow_statarb.core.execution_log import ExecutionLog
from arrow_statarb.core.trade_log import TradeLog
from arrow_statarb.core.untracked_ledger import UntrackedLedger
from arrow_statarb.core.reconcile import ReconcileGuard
from arrow_statarb.core import costs
from arrow_statarb.core import fairvalue, sizing, performance, scenarios
from arrow_statarb.core.signals import ZSignalGenerator
from arrow_statarb.core.exits import ExitLadder
from arrow_statarb.core.clip_executor import ClipExecutor
from arrow_statarb.core.arrow_leg import ArrowLeg
from arrow_statarb.core.whatif_shadow import ShadowTracker
from arrow_statarb.core.health import Heartbeat, health_verdict
from arrow_statarb.core.telegram import TelegramNotifier
from arrow_statarb.models.probability_filter import ProbabilityFilter

_MODE_STATUS = {"dry_run": "DRY-RUN", "live_sim": "LIVE-SIM", "live": "LIVE"}

_IST = timezone(timedelta(hours=5, minutes=30))

# Trade-log location. Module-level so tests can redirect it to a temp file and
# never write into the shipped data/trades.json.
TRADES_FILE = PROJECT_ROOT / "data" / "trades.json"

# Cached Arrow session token (valid ~24h). Persisted locally and gitignored so a
# restart within the validity window reuses it instead of running 2FA again.
# Module-level so tests can redirect it away from a real session file.
SESSION_FILE = PROJECT_ROOT / "data" / "arrow_session.json"
SIGNAL_WINDOW_FILE = PROJECT_ROOT / "data" / "signal_window.json"
UNTRACKED_FILE = PROJECT_ROOT / "data" / "untracked_closes.json"
SHADOW_FILE = PROJECT_ROOT / "data" / "whatif_shadow.json"
HEARTBEAT_FILE = PROJECT_ROOT / "data" / "heartbeat.txt"


def _save_session_token(app_id: str, token: str) -> None:
    """Persist the live session token for reuse on the next start. Best-effort —
    a failure here never blocks a connection."""
    if not app_id or not token:
        return
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(
            {"app_id": app_id, "token": token, "saved_at": time.time()}))
    except Exception as exc:
        logger.debug("session token save failed — {}", exc)


def _load_session_token(app_id: str, max_age_h: float = 23.0) -> str:
    """Return a saved token for this app_id if present and younger than
    ``max_age_h`` hours (Arrow tokens last ~24h), else ""."""
    if not app_id:
        return ""
    try:
        data = json.loads(SESSION_FILE.read_text())
    except Exception:
        return ""
    if str(data.get("app_id", "")) != str(app_id):
        return ""
    if (time.time() - float(data.get("saved_at", 0))) > max_age_h * 3600:
        return ""
    return str(data.get("token", "") or "")


def _clear_session_token() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.debug("session token clear failed — {}", exc)


def create_app(config: Optional[Config] = None) -> Tuple[Flask, SocketIO]:
    cfg = config or Config()
    templates_dir = Path(__file__).resolve().parent / "templates"
    app = Flask(__name__, template_folder=str(templates_dir))
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    active = ActiveBroker()
    _ltp_cache: Dict[str, Any] = {}
    trade_log = TradeLog(TRADES_FILE,
                         brokerage_per_lot=float(cfg.get("filters.brokerage_per_lot", 20)))
    untracked_ledger = UntrackedLedger(UNTRACKED_FILE)
    shadow = ShadowTracker(SHADOW_FILE,
                           window_sec=float(cfg.get("exits.whatif_window_min", 60) or 60) * 60.0)

    def _telegram_params() -> Dict:
        t = cfg.section("telegram")
        return {"enabled": bool(t.get("enabled", False)),
                "chat_id": str(t.get("chat_id", "") or ""),
                "notify_trades": bool(t.get("notify_trades", True)),
                "notify_health": bool(t.get("notify_health", False)),
                "notify_errors": bool(t.get("notify_errors", True))}
    telegram = TelegramNotifier(params_provider=_telegram_params)  # token from env only

    execution_log = ExecutionLog()

    # ── broker-read cache (positions + funds) ─────────────────────────────────
    # Arrow's positions/limits API can be slow (10s read-timeouts) and flaky
    # (502s). A background refresher polls it on a timer and stores last-good
    # values; every endpoint (and the pre-entry flat check) reads the cache
    # instead of calling Arrow directly — so a slow API never piles up request
    # threads or blanks the dashboard, and the last good funds value sticks.
    _broker_cache: Dict[str, Dict] = {
        "positions": {"ts": 0.0, "val": [], "ok": False},
        "funds": {"ts": 0.0, "val": {}, "ok": False},
    }
    _broker_cache_lock = threading.Lock()

    def _cache_get(key: str, max_age: float):
        with _broker_cache_lock:
            c = _broker_cache[key]
            fresh = c["ok"] and (time.time() - c["ts"]) <= max_age
            return (list(c["val"]) if isinstance(c["val"], list) else dict(c["val"])), fresh

    def _refresh_broker_cache() -> None:
        b = active.get()
        if not b:
            return
        try:
            pos = b.get_positions() or []
            with _broker_cache_lock:
                _broker_cache["positions"] = {"ts": time.time(), "val": pos, "ok": True}
        except Exception as exc:
            logger.debug("positions cache refresh failed (keeping last-good) — {}", exc)
        if hasattr(b, "get_funds"):
            try:
                f = b.get_funds() or {}
                with _broker_cache_lock:
                    _broker_cache["funds"] = {"ts": time.time(), "val": f, "ok": True}
            except Exception as exc:
                logger.debug("funds cache refresh failed (keeping last-good) — {}", exc)

    def _positions_cached() -> list:
        val, _ = _cache_get("positions", max_age=1e9)   # last-good; never blocks
        return val

    def _funds_cached() -> Dict:
        val, _ = _cache_get("funds", max_age=1e9)
        return val

    def _positions_for_verify():
        """Positions for the executor's pre-entry flat check — returns the cached
        list only if a refresh succeeded recently, else None (→ fail-safe block)."""
        ttl = float(cfg.get("broker.cache_ttl_sec", 2.0)) or 2.0
        val, fresh = _cache_get("positions", max_age=max(6.0, ttl * 4))
        return val if fresh else None

    def _start_cache_refresher() -> None:
        ttl = max(0.5, float(cfg.get("broker.cache_ttl_sec", 2.0) or 2.0))

        def _loop():
            while True:
                try:
                    _refresh_broker_cache()
                except Exception:
                    pass
                time.sleep(ttl)
        threading.Thread(target=_loop, daemon=True, name="BrokerCache").start()

    _start_cache_refresher()

    # ── config helpers ───────────────────────────────────────────────────────
    def _is_dry_run() -> bool:
        try:
            cfg.reload()
        except Exception:
            return True
        return cfg.is_dry_run

    def _mode() -> str:
        """Current order-routing mode: ``dry_run`` | ``live`` | ``live_sim``."""
        try:
            cfg.reload()
        except Exception:
            return "dry_run"
        m = str(cfg.mode).lower()
        return m if m in ("live", "live_sim") else "dry_run"

    def _seg_to_exch_seg() -> Dict[str, str]:
        return {k: str(v).upper() for k, v in (cfg.get("broker.segments") or {}).items()}

    def _read_legs() -> Dict[str, Dict]:
        """{'leg_a': {'segment','symbol','ratio'}, 'leg_b': {...}} from
        leg_assignments.yaml, falling back to settings.yaml instruments."""
        legs: Dict[str, Dict] = {}
        if LEG_ASSIGNMENTS_FILE.exists():
            with open(LEG_ASSIGNMENTS_FILE) as f:
                assigns = yaml.safe_load(f) or {}
            for lk in ("leg_a", "leg_b"):
                entry = assigns.get(lk) or {}
                mid = entry.get("mapping_id", "") if isinstance(entry, dict) else ""
                if "|" in mid:
                    seg, sym = mid.split("|", 1)
                    legs[lk] = {"segment": seg.strip(), "symbol": sym.strip(),
                                "ratio": float(entry.get("ratio", 1) or 1),
                                "cost_key": (str(entry.get("cost_key")).strip()
                                             if entry.get("cost_key") else "")}
        if "leg_a" in legs and "leg_b" in legs:
            return legs
        # Fall back to config defaults
        for lk in ("leg_a", "leg_b"):
            d = cfg.get(f"instruments.{lk}") or {}
            if d.get("segment") and d.get("symbol"):
                legs[lk] = {"segment": str(d["segment"]).strip(),
                            "symbol": str(d["symbol"]).strip(),
                            "ratio": float(d.get("ratio", 1) or 1),
                            "cost_key": (str(d.get("cost_key")).strip()
                                         if d.get("cost_key") else "")}
        return legs

    def _have_both_legs(legs: Dict) -> bool:
        return "leg_a" in legs and "leg_b" in legs

    # ── shared order path (manual + algo) ────────────────────────────────────
    def _order_legs(direction: str, lots: int):
        """Return [(seg, sym, side, lots_for_leg), ...] for a direction.

        Non-1:1 hedge sizing: leg_b is the contract leg (qty in lots); leg_a is
        sized so both legs carry EQUAL rupee exposure —
            units_a = units_b × hedge_ratio  (units_b = lots × lot_size_b)
        so leg_a's lot count = round(units_a ÷ lot_size_a). With hedge_ratio = 1
        and equal lot sizes (a same-instrument calendar) this reduces to the
        original equal-lots behaviour exactly."""
        legs = _read_legs()
        if not _have_both_legs(legs):
            raise ValueError("Both legs must be assigned in Setup")
        k = float(cfg.get("signal.hedge_ratio", 1) or 1)
        sa, ya = legs["leg_a"]["segment"], legs["leg_a"]["symbol"]
        sb, yb = legs["leg_b"]["segment"], legs["leg_b"]["symbol"]
        ls_a = _resolve_lot_size(sa, ya)
        ls_b = _resolve_lot_size(sb, yb)
        qb = max(1, round(lots * float(legs["leg_b"].get("ratio", 1) or 1)))
        units_a = qb * ls_b * k                       # match leg_b exposure × k
        qa = max(1, round(units_a / ls_a)) if ls_a > 0 else max(1, round(units_a))
        # LONG_SPREAD = buy A / sell B ; SHORT_SPREAD = sell A / buy B
        if direction == "LONG_SPREAD":
            return [(sa, ya, "buy", qa), (sb, yb, "sell", qb)]
        return [(sa, ya, "sell", qa), (sb, yb, "buy", qb)]

    def _spread_order(legs, label: str, verify_flat: bool = False) -> Dict:
        """Execute both legs according to the current mode:
          dry_run   → simulated, nothing leaves the process;
          live_sim  → real SpreadExecutor against a SimBroker (no real orders);
          live      → real SpreadExecutor against the connected broker.
        Shared by the manual endpoints AND the auto-trader."""
        mode = _mode()
        broker = active.get()

        if mode == "dry_run":
            if not broker:
                return {"success": False, "error": "No broker connected"}
            results = []
            for seg, sym, side, qty in legs:
                lot_size = broker.resolve_lot_size(seg, sym)
                actual_qty = qty * lot_size
                logger.info("[DRY-RUN] {}: would {} {}lot(s)×{}={} {}/{} — NOT transmitted",
                            label, side, qty, lot_size, actual_qty, sym, seg)
                results.append({"order_id": f"DRYRUN-{side}-{sym}", "status": "submitted",
                                "symbol": sym, "side": side, "quantity": actual_qty,
                                "dry_run": True})
            ids = [r["order_id"] for r in results]
            return {"success": True, "message": f"[DRY-RUN] Simulated: {', '.join(ids)}",
                    "results": results, "dry_run": True}

        if mode == "live" and not broker:
            return {"success": False, "error": "No broker connected"}

        # live / live_sim → run the real executor (against the sim broker for sim).
        executor = sim_executor if mode == "live_sim" else spread_executor
        resolver = sim_broker if mode == "live_sim" else broker
        leg_orders = []
        for seg, sym, side, qty in legs:
            lot_size = resolver.resolve_lot_size(seg, sym)
            tok = resolver.resolve_token(seg, sym)
            leg_orders.append(LegOrder(segment=seg, symbol=sym, side=side,
                                       units=qty * lot_size, token=tok))
        res = executor.execute(leg_orders, label=label, verify_flat=verify_flat)
        execution_log.record(res, label, mode)        # telemetry
        return res

    def _current_spread() -> Optional[float]:
        return signal_engine.get_signal().get("spread")

    def _trade_meta(res: Dict) -> Dict:
        """Capture per-trade journal detail: the contract lot size, the EXECUTED
        per-leg prices (live: actual fills from the executor; dry-run: current
        LTP proxy), the resulting spread, the live z-score, and the trade name."""
        legs = _read_legs()
        broker = sim_broker if _mode() == "live_sim" else active.get()
        meta: Dict = {"lot_size": 1, "leg_a_price": None, "leg_b_price": None,
                      "spread": _current_spread(), "zscore": None, "name": ""}
        if not _have_both_legs(legs):
            return meta
        meta["name"] = f'{legs["leg_a"]["symbol"]} − {legs["leg_b"]["symbol"]}'
        try:
            # Contract leg (leg_b) drives the σ→₹ / P&L multiplier in the
            # hedge-scaled spread; for a same-lot calendar this equals leg_a.
            meta["lot_size"] = int(broker.resolve_lot_size(
                legs["leg_b"]["segment"], legs["leg_b"]["symbol"]))
        except Exception:
            pass
        # Executed fill prices from the live executor results, keyed by symbol.
        by_sym: Dict[str, float] = {}
        for r in (res.get("results") or []):
            ap = r.get("avg_price")
            if ap:
                by_sym[str(r.get("symbol", "")).upper()] = float(ap)
        a = by_sym.get(legs["leg_a"]["symbol"].upper())
        b = by_sym.get(legs["leg_b"]["symbol"].upper())
        if a is None or b is None:               # dry-run / no fill price → live LTP
            la, lb = _leg_prices()
            a = a if a is not None else la
            b = b if b is not None else lb
        meta["leg_a_price"] = a
        meta["leg_b_price"] = b
        if a is not None and b is not None:
            # Fill spread on the SAME hedge-scaled basis as the signal, so the
            # algo's live-P&L reference matches its z-score world.
            k = float(cfg.get("signal.hedge_ratio", 1) or 1)
            meta["spread"] = round(k * a - b, 4)
        meta["zscore"] = signal_engine.get_signal().get("zscore")
        return meta

    def _unwind_cost(res: Dict, legs: Dict, lots: int) -> float:
        """Estimated ₹ lost on a slippage-aborted entry: brokerage + STT + other
        charges on the four leg fills (entry 2 + unwind 2), plus the ACTUAL
        realized adverse slippage measured on the entry fills. Fees follow the
        same convention as the algo's round-trip cost (2 sells for STT, 4 leg
        turnovers for 'other'), so the ledger reconciles with the cost audit.
        The unwind-side (market) slippage isn't separately measured."""
        f = cfg.section("filters")
        by_sym = {str(r.get("symbol", "")).upper(): r for r in (res.get("results") or [])}
        ra = by_sym.get(str(legs["leg_a"]["symbol"]).upper(), {})
        ref_a = float(ra.get("ref_price") or ra.get("avg_price") or 0)
        filled_a = int(ra.get("filled") or 0) or (lots * (ra.get("units") or 0))
        notional = ref_a * filled_a                       # one-leg notional (algo convention)
        brk = float(f.get("brokerage_per_lot", 20) or 0) * max(1, lots) * 4.0
        stt = 2.0 * (float(f.get("stt_pct", 0) or 0) / 100.0) * notional
        other = 4.0 * (float(f.get("other_cost_pct", 0) or 0) / 100.0) * notional
        adverse = 0.0                                     # actual entry slippage (₹)
        for r in (res.get("results") or []):
            ref = float(r.get("ref_price") or 0)
            avg = float(r.get("avg_price") or 0)
            filled = int(r.get("filled") or 0)
            if ref > 0 and avg > 0 and filled > 0:
                sign = 1.0 if r.get("side") == "buy" else -1.0
                adverse += max(0.0, sign * (avg - ref)) * filled
        return round(brk + stt + other + adverse, 2)

    def _engine_mode() -> str:
        return str(cfg.get("execution.engine_mode", "legacy") or "legacy").lower()

    def _clip_spread_order(legs, label: str) -> Dict:
        """Place the SAME Arrow-sized legs through the ported clip engine —
        slicing + limit-first repeg + cross-on-timeout — instead of the legacy
        SpreadExecutor. Arrow's sizing/sides/hedge are unchanged (no convention
        risk); only HOW the orders are placed differs. Atomic: if a leg
        under-fills, the already-filled legs are unwound at market. Returns the
        same {success, results:[{symbol, side, avg_price}], error} shape."""
        mode = _mode()
        broker = sim_broker if mode == "live_sim" else active.get()
        if not broker:
            return {"success": False, "error": "No broker connected"}
        ex = cfg.section("execution")
        ex_cfg = {
            "SLIPPAGE_TOLERANCE": 1.0, "PEG_OFFSET_POINTS": 0.0,
            "ORDER_POLL_SEC": float(ex.get("poll_interval_sec", 0.4) or 0.4),
            "REPEG_INTERVAL_SEC": float(ex.get("amend_interval_sec", 2.0) or 2.0),
            "LIMIT_TIMEOUT_SEC": float(ex.get("fill_timeout_sec", 8) or 8),
            "ON_TIMEOUT": "cross" if bool(ex.get("limit_to_market", True)) else "abort",
        }
        seg_by_sym = {sym: seg for seg, sym, _s, _q in legs}
        leg = ArrowLeg(broker, seg_by_sym, product=str(ex.get("product", "NRML")))
        ce = ClipExecutor(ex_cfg, leg, leg,
                          slice_lots=float(ex.get("slice_lots", 0) or 0))
        style = "limit" if bool(ex.get("use_limit_orders", True)) else "market"
        _opp = {"buy": "sell", "sell": "buy"}
        results, filled_legs = [], []
        try:
            ce.sweep_stale_orders([(leg, sym) for _s, sym, _sd, _q in legs])
            for seg, sym, side, lots_leg in legs:
                f, vwap = ce._send_sliced(leg, sym, side.upper(), lots_leg,
                                          f"{label}", style=style,
                                          timeout=ex_cfg["LIMIT_TIMEOUT_SEC"])
                results.append({"symbol": sym, "side": side, "avg_price": vwap,
                                "filled": f, "lots": lots_leg})
                if f >= lots_leg - 1e-9:
                    filled_legs.append((seg, sym, side, f))
                else:                                    # atomic unwind of filled legs
                    for fseg, fsym, fside, ff in filled_legs:
                        ce._send_sliced(leg, fsym, _opp[fside], ff, "unwind", style="market")
                    return {"success": False, "results": results,
                            "error": f"clip: {sym} filled {f}/{lots_leg} — legs unwound"}
            return {"success": True, "results": results}
        except Exception as exc:                         # never crash the money path
            logger.error("clip execution failed — {}", exc)
            for fseg, fsym, fside, ff in filled_legs:
                try:
                    ce._send_sliced(leg, fsym, _opp[fside], ff, "unwind", style="market")
                except Exception:
                    logger.critical("clip: could not unwind %s after error", fsym)
            return {"success": False, "results": results, "error": str(exc)}

    def _place(legs, label: str, verify_flat: bool = False) -> Dict:
        """Route to the clip engine or the legacy SpreadExecutor per settings."""
        if _engine_mode() == "clip":
            return _clip_spread_order(legs, label)
        return _spread_order(legs, label, verify_flat=verify_flat)

    def _spread_execute(direction: str, lots: int, source: str = "manual",
                        z: Optional[float] = None, spread: Optional[float] = None) -> Dict:
        mode = _mode()
        if mode != "live_sim" and not active.get():
            return {"success": False, "error": "No broker connected"}
        try:
            legs = _order_legs(direction, lots)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        res = _place(legs, "Order", verify_flat=True)
        if res.get("success"):
            m = _trade_meta(res)
            # Hand the actual executed fill spread back to the caller (the algo
            # stores it as the live-P&L reference for its dollar-stop / target).
            res["fill_spread"] = m["spread"]
            res["leg_a_fill"] = m["leg_a_price"]
            res["leg_b_fill"] = m["leg_b_price"]
            # P&L MUST come from the actual executed fill spread (m["spread"],
            # built from the executor's avg_price), NOT the algo's decision
            # spread — otherwise slippage is invisible and a slipped trade can
            # show a paper "win" while the broker books a real loss. The
            # decision z is still recorded (below) as the signal indicator.
            trade_log.record(action="OPEN", direction=direction, lots=lots,
                             spread=m["spread"],
                             decision_spread=(spread if spread is not None else _current_spread()),
                             dry_run=(mode != "live"),
                             status=_MODE_STATUS.get(mode, "DRY-RUN"), source=source,
                             lot_size=m["lot_size"],
                             zscore=(z if z is not None else m["zscore"]),
                             leg_a_price=m["leg_a_price"], leg_b_price=m["leg_b_price"],
                             stt_pct=float(cfg.get("filters.stt_pct", 0.02) or 0),
                             stt_a_pct=(float(cfg.get("filters.stt_a_pct"))
                                        if cfg.get("filters.stt_a_pct") is not None else None),
                             stt_b_pct=(float(cfg.get("filters.stt_b_pct"))
                                        if cfg.get("filters.stt_b_pct") is not None else None),
                             other_cost_pct=float(cfg.get("filters.other_cost_pct", 0) or 0),
                             capital_gains_pct=float(cfg.get("filters.capital_gains_pct", 0) or 0),
                             name=m["name"])
            _zt = z if z is not None else m["zscore"]
            _zx = f"{float(_zt):+.2f}" if _zt is not None else "n/a"
            telegram.notify(
                f"🟢 ENTRY {direction.replace('_SPREAD','')} · {m['name']} · "
                f"z={_zx} · {lots} lot(s) · {mode}", "trade")
        elif res.get("slippage_abort"):
            # The entry filled but slipped past the budget and was unwound — no
            # position results, yet real money moved (fees on 4 fills + the
            # realized slippage). Book it to the untracked ledger so it counts
            # against max_daily_loss (day_cost is subtracted from day P&L).
            la = _read_legs()
            est = _unwind_cost(res, la, lots)
            untracked_ledger.record(
                reason="slippage_abort",
                symbol=f"{la['leg_a']['symbol']}/{la['leg_b']['symbol']}",
                qty=lots, est_cost=est,
                detail=res.get("error", "entry unwound — slippage over budget"))
        return res

    def _spread_close(direction: str, lots: int, source: str = "manual",
                      reason: str = "", z: Optional[float] = None,
                      spread: Optional[float] = None,
                      peak_pnl: Optional[float] = None,
                      trough_pnl: Optional[float] = None,
                      peak_min: Optional[float] = None,
                      trough_min: Optional[float] = None) -> Dict:
        mode = _mode()
        if mode != "live_sim" and not active.get():
            return {"success": False, "error": "No broker connected"}
        close_dir = "SHORT_SPREAD" if direction == "LONG_SPREAD" else "LONG_SPREAD"
        try:
            legs = _order_legs(close_dir, lots)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        res = _place(legs, "Close")
        if res.get("success"):
            m = _trade_meta(res)
            rec = trade_log.record(action="CLOSE", direction=direction, lots=lots,
                             spread=m["spread"],  # actual fill spread — see OPEN note
                             decision_spread=(spread if spread is not None else _current_spread()),
                             dry_run=(mode != "live"),
                             status=_MODE_STATUS.get(mode, "DRY-RUN"), source=source,
                             lot_size=m["lot_size"],
                             zscore=(z if z is not None else m["zscore"]),
                             leg_a_price=m["leg_a_price"], leg_b_price=m["leg_b_price"],
                             stt_pct=float(cfg.get("filters.stt_pct", 0.02) or 0),
                             other_cost_pct=float(cfg.get("filters.other_cost_pct", 0) or 0),
                             capital_gains_pct=float(cfg.get("filters.capital_gains_pct", 0) or 0),
                             peak_pnl=peak_pnl, trough_pnl=trough_pnl,
                             peak_min=peak_min, trough_min=trough_min,
                             name=m["name"], exit_reason=reason)
            # Arm the what-if-held shadow (skips a clean target hit internally) so
            # premature exits become logged data, not an argument.
            if rec.get("entry_spread") is not None:
                try:
                    cost = float(rec.get("spread_pnl", 0) or 0) - float(rec.get("net_pnl", 0) or 0)
                    shadow.arm(direction=direction, entry_spread=float(rec["entry_spread"]),
                               lots=lots, lot_mult=float(rec.get("lot_size", 1) or 1),
                               cost_inr=cost, exit_reason=reason or "",
                               target_net=rec.get("target"))
                except Exception:                        # noqa: BLE001 — never block a close
                    pass
            _np = float(rec.get("net_pnl", 0) or 0)
            _emoji = "🟢" if _np > 0 else ("🔴" if _np < 0 else "⚪")
            telegram.notify(
                f"{_emoji} EXIT {reason or 'manual'} · {m['name']} · net ₹{_np:.0f}",
                "trade")
        return res

    # ── live leg prices (stream first, REST fallback) ────────────────────────
    def _leg_prices() -> Tuple[Optional[float], Optional[float]]:
        broker = active.get()
        legs = _read_legs()
        if not broker or not _have_both_legs(legs):
            return (None, None)
        syms = {lk: legs[lk]["symbol"] for lk in ("leg_a", "leg_b")}
        px: Dict[str, float] = {}
        if hasattr(broker, "get_streamed_ltp"):
            try:
                broker.start_price_stream([syms["leg_a"], syms["leg_b"]])  # idempotent
                px = broker.get_streamed_ltp([syms["leg_a"], syms["leg_b"]]) or {}
            except Exception:
                px = {}
        need = [lk for lk in ("leg_a", "leg_b") if syms[lk].upper() not in px]
        if need:
            try:
                instruments = [{"exchange_segment": legs[lk]["segment"],
                                "instrument_token": syms[lk]} for lk in need]
                px.update(broker.get_ltp(instruments) or {})
            except Exception:
                pass
        return (px.get(syms["leg_a"].upper()), px.get(syms["leg_b"].upper()))

    # ── live executor wiring (limit orders, fills, orphan recovery) ──────────
    def _one_ltp(seg: str, sym: str) -> Optional[float]:
        """Latest price for a single leg — stream cache first, REST fallback.
        Used by the executor to price limit orders and amendments."""
        broker = active.get()
        if not broker:
            return None
        px: Dict[str, float] = {}
        if hasattr(broker, "get_streamed_ltp"):
            try:
                broker.start_price_stream([sym])  # idempotent
                px = broker.get_streamed_ltp([sym]) or {}
            except Exception:
                px = {}
        if sym.upper() not in px:
            try:
                px.update(broker.get_ltp(
                    [{"exchange_segment": seg, "instrument_token": sym}]) or {})
            except Exception:
                pass
        return px.get(sym.upper())

    def _exec_params() -> Dict:
        """Execution params for the SpreadExecutor, read live from config."""
        e = cfg.section("execution")
        r = cfg.section("risk")
        return {
            "use_limit_orders": bool(e.get("use_limit_orders", True)),
            "limit_offset_pct": float(e.get("limit_offset_pct", 0.05)),
            "amend_step_pct": float(e.get("amend_step_pct", 0.05)),
            "fill_timeout_sec": float(e.get("fill_timeout_sec", 5.0)),
            "amend_interval_sec": float(e.get("amend_interval_sec", 1.5)),
            "poll_interval_sec": float(e.get("poll_interval_sec", 0.4)),
            "limit_to_market": bool(e.get("limit_to_market", True)),
            "verify_flat_before_entry": bool(e.get("verify_flat_before_entry", True)),
            # On a positions-read failure: False (default) BLOCKS the entry
            # (fail-safe for LIVE); True allows it through.
            "verify_flat_fail_open": bool(e.get("verify_flat_fail_open", False)),
            "unknown_status_grace_polls": int(e.get("unknown_status_grace_polls", 3)),
            "assume_fill_on_unknown": bool(e.get("assume_fill_on_unknown", False)),
            "product": str(e.get("product", "NRML")),
            "max_slippage_pct": float(r.get("max_slippage_pct", 0.5)),
            # Fallback price tick when the broker master exposes no tick size.
            # Arrow rejects off-tick limit prices (NIFTY fut = 0.10).
            "price_tick_size": float(e.get("price_tick_size", 0.05)),
        }

    spread_executor = SpreadExecutor(broker_fn=active.get, price_fn=_one_ltp,
                                     params_fn=_exec_params,
                                     positions_fn=_positions_for_verify)

    # ── live-sim: the real executor against a simulated broker (no real orders) ─
    _sim = cfg.section("execution")

    def _sim_lot_size(seg: str, sym: str) -> int:
        b = active.get()
        try:
            return int(b.resolve_lot_size(seg, sym)) if b else 0
        except Exception:
            return 0

    def _sim_quote(seg: str, sym: str):
        """Real top-of-book bid/ask from the connected broker (for realistic
        live-sim fills); None when unavailable → SimBroker uses a synthetic spread."""
        b = active.get()
        if b and hasattr(b, "get_quote"):
            try:
                return b.get_quote(seg, sym)
            except Exception:
                return None
        return None

    sim_broker = SimBroker(
        real_price_fn=_one_ltp, quote_fn=_sim_quote, lot_size_fn=_sim_lot_size,
        tick_size=float(_sim.get("sim_tick_size", 0.05)),
        spread_ticks=float(_sim.get("sim_spread_ticks", 2.0)),
        extra_slip_ticks=float(_sim.get("sim_extra_slip_ticks", 0.0)),
        slow_prob=float(_sim.get("sim_slow_prob", 0.25)),
        reject_prob=float(_sim.get("sim_reject_prob", 0.0)),
        orphan_prob=float(_sim.get("sim_orphan_prob", 0.0)),
        orphan_symbols=set(_sim.get("sim_orphan_symbols", []) or []),
        default_lot_size=int(_sim.get("sim_default_lot_size", 75)),
    )
    sim_executor = SpreadExecutor(
        broker_fn=lambda: sim_broker,
        price_fn=lambda seg, sym: sim_broker.get_streamed_ltp([sym]).get(sym.upper()),
        params_fn=_exec_params)

    # ── signal + algo wiring ─────────────────────────────────────────────────
    def _signal_params() -> Dict:
        s = cfg.section("signal")
        rg = cfg.section("regime")
        return {
            "window_minutes": float(s.get("window_minutes", 120)),
            "sample_interval_sec": float(s.get("sample_interval_sec", 0.5)),
            "min_signal_minutes": float(s.get("min_signal_minutes", 10)),
            "entry_zscore": float(s.get("entry_zscore", 2.0)),
            "exit_zscore": float(s.get("exit_zscore", 0.0)),
            "stop_zscore": float(s.get("stop_zscore", 4.0)),
            # non-1:1 pairs: spread = hedge_ratio × leg_a − leg_b (1 = same scale)
            "hedge_ratio": float(s.get("hedge_ratio", 1) or 1),
            "stats_update_interval_sec": float(s.get("stats_update_interval_sec", 0) or 0),
            # window persistence (resume warm-up across a quick restart)
            "persist_window": bool(s.get("persist_window", True)),
            "resume_max_gap_min": float(s.get("resume_max_gap_min", 10) or 0),
            "persist_interval_sec": float(s.get("persist_interval_sec", 30) or 30),
            # regime detection thresholds (read by SignalEngine.regime())
            "regime_window_samples": int(rg.get("window_samples", 120) or 120),
            "regime_efficiency_ratio_max": float(rg.get("efficiency_ratio_max", 0.6) or 0.6),
            "regime_min_zero_crossings": int(rg.get("min_zero_crossings", 4) or 4),
            "regime_vr_lag": int(rg.get("vr_lag", 5) or 5),
        }

    def _series_key() -> str:
        """Current leg pair as 'leg_a|leg_b' symbols — tags the persisted window
        so it is only resumed for the same contracts."""
        legs = _read_legs()
        a = (legs.get("leg_a") or {}).get("symbol", "")
        b = (legs.get("leg_b") or {}).get("symbol", "")
        return f"{a}|{b}" if a and b else ""

    signal_engine = SignalEngine(prices_provider=_leg_prices, params_provider=_signal_params,
                                 persist_path=SIGNAL_WINDOW_FILE, series_key_provider=_series_key)
    # Auto-start so the live signal + z-score chart always collect whenever prices
    # are available — independent of connecting the broker or arming the algo. It
    # simply no-ops while no prices are returned, so this is safe at startup.
    signal_engine.start()

    _algo_lots = {"lots": int(cfg.get("execution.default_lots", 1))}

    def _resolve_lot_size(seg: str, sym: str) -> float:
        """Units-per-lot for a leg from the active resolver (sim broker in
        live_sim, else the live broker). 1.0 when nothing can resolve it yet."""
        resolver = sim_broker if _mode() == "live_sim" else active.get()
        if not resolver:
            return 1.0
        try:
            return float(resolver.resolve_lot_size(seg, sym)) or 1.0
        except Exception:
            return 1.0

    def _lot_multiplier() -> float:
        """Spread σ→₹ multiplier = the CONTRACT leg's (leg_b) lot size. The
        non-1:1 spread is denominated in leg_b price units, and a position holds
        lots × lot_size_b of it, so P&L = lots × lot_size_b × Δspread. For a
        same-lot calendar (leg_a lot == leg_b lot) this is unchanged."""
        legs = _read_legs()
        if "leg_b" not in legs:
            return 1.0
        return _resolve_lot_size(legs["leg_b"]["segment"], legs["leg_b"]["symbol"])

    def _algo_params() -> Dict:
        s = cfg.section("signal")
        f = cfg.section("filters")
        r = cfg.section("risk")
        xo = cfg.section("exits")          # dollar-P&L exit overrides (Phase 1)
        co = cfg.section("costs")          # per-segment Indian cost model (opt-in)
        cap = int(r.get("max_contracts_per_leg", 0) or 0)
        lots = int(_algo_lots["lots"])
        if cap > 0:
            lots = min(lots, cap)            # hard position cap per leg
        # Per-segment cost rates for each leg — resolved from its segment (or an
        # explicit cost_key override), only when the segment model is enabled.
        use_seg = bool(co.get("use_segment_costs", False))
        seg_overrides = co.get("segments") or {}
        _clegs = _read_legs() if use_seg else {}
        def _cost_key(lk):
            e = _clegs.get(lk) or {}
            return e.get("cost_key") or e.get("segment") or ""
        return {
            "entry_zscore": float(s.get("entry_zscore", 2.0)),
            "exit_zscore": float(s.get("exit_zscore", 0.0)),
            "stop_zscore": float(s.get("stop_zscore", 4.0)),
            "confirmation_ticks": int(s.get("confirmation_ticks", 1)),
            "max_entry_z_divergence": float(s.get("max_entry_z_divergence", 0) or 0),
            "max_entry_spread_divergence": float(s.get("max_entry_spread_divergence", 0) or 0),
            "max_entry_zscore": float(s.get("max_entry_zscore", 0) or 0),
            "tick_interval": float(s.get("sample_interval_sec", 0.5)),
            "min_hold_sec": float(s.get("min_hold_sec", 0) or 0),
            # ── dollar-P&L exit overrides (priority above the z-score exit) ──
            "dollar_stop_inr": float(xo.get("dollar_stop_inr", 0) or 0),
            "profit_target_inr": float(xo.get("profit_target_inr", 0) or 0),
            # ── Phase 2: max-hold upgrade + trailing stop ──
            "max_hold_silent_when_losing": bool(xo.get("max_hold_silent_when_losing", False)),
            "max_hold_z_progress_min": float(xo.get("max_hold_z_progress_min", 0) or 0),
            "trailing_stop_pct": float(xo.get("trailing_stop_pct", 0) or 0),
            "trailing_stop_floor_pct": float(xo.get("trailing_stop_floor_pct", 0) or 0),
            # ── Tier A: gated reversion + z-reset + stop-cooldown + loss-streak ──
            # reversion gate ON by default — never book below break-even (BE
            # includes STT below); set reversion_gate_inr for a BE+profit floor.
            "reversion_require_profit": bool(xo.get("reversion_require_profit", True)),
            "reversion_gate_inr": float(xo.get("reversion_gate_inr", 0) or 0),
            "stt_pct": float(f.get("stt_pct", 0.02) or 0),   # STT %-of-notional, sell-side
            # per-leg STT for a non-1:1 pair (ETF ≈ 0.001 / future ≈ 0.02); each
            # falls back to stt_pct so a same-instrument spread is unchanged.
            "stt_a_pct": (float(f["stt_a_pct"]) if f.get("stt_a_pct") is not None else None),
            "stt_b_pct": (float(f["stt_b_pct"]) if f.get("stt_b_pct") is not None else None),
            # ── per-segment Indian cost model (STT/CTT+txn+GST+SEBI+stamp) ──
            "use_segment_costs": use_seg,
            "gst_pct": float(co.get("gst_pct", costs.GST_PCT) or costs.GST_PCT),
            "cost_rates_a": (costs.resolve_segment_costs(_cost_key("leg_a"), seg_overrides)
                             if use_seg else None),
            "cost_rates_b": (costs.resolve_segment_costs(_cost_key("leg_b"), seg_overrides)
                             if use_seg else None),
            "no_entry_days_before_expiry": float(co.get("no_entry_days_before_expiry", 0) or 0),
            "hedge_ratio": float(cfg.get("signal.hedge_ratio", 1) or 1),
            # ── Spec v2: z-stop demotion, hard time-stop, edge filter ──
            "z_stop_exit_enabled": bool(xo.get("z_stop_exit_enabled", True)),
            "hard_time_stop_mult": float(xo.get("hard_time_stop_mult", 0) or 0),
            "min_edge_multiple": float(f.get("min_edge_multiple", 0) or 0),
            "half_life_min_sec": float(f.get("half_life_min_sec", 0) or 0),
            "half_life_max_sec": float(f.get("half_life_max_sec", 0) or 0),
            "other_cost_pct": float(f.get("other_cost_pct", 0) or 0),
            "capital_gains_pct": float(f.get("capital_gains_pct", 0) or 0),
            # ── Tier B: scale-invariant exit levels ──
            "profit_target_sigma_frac": float(xo.get("profit_target_sigma_frac", 0) or 0),
            "tp_capital_pct": float(xo.get("tp_capital_pct", 0) or 0),
            "cost_floor_mult": float(xo.get("cost_floor_mult", 0) or 0),
            "stop_capital_pct": float(xo.get("stop_capital_pct", 0) or 0),
            "stop_rr": float(xo.get("stop_rr", 0) or 0),
            "capital_at_risk_inr": float(r.get("capital_at_risk_inr", 0) or 0),
            "stop_cooldown": float(cfg.get("execution.stop_cooldown_sec", 0) or 0),
            "z_reset_after_stop": bool(cfg.section("execution").get("z_reset_after_stop", False)),
            "loss_streak": int(trade_log.loss_streak()),
            "loss_streak_reduce_at": int(r.get("loss_streak_reduce_at", 0) or 0),
            "loss_streak_reduce_pct": float(r.get("loss_streak_reduce_pct", 0) or 0),
            "loss_streak_pause_at": int(r.get("loss_streak_pause_at", 0) or 0),
            # ── Tier A: regime / trend-day guard (state comes from the signal) ──
            "regime_enabled": bool(cfg.section("regime").get("enabled", False)),
            "regime_halt_on_trending": bool(cfg.section("regime").get("halt_on_trending", True)),
            "regime_trend_direction_filter": bool(cfg.section("regime").get("trend_direction_filter", False)),
            "cooldown": float(cfg.get("execution.cooldown_sec", 300)),
            "max_exit_failures": int(cfg.get("execution.max_exit_failures", 0) or 0),
            "exit_retry_backoff": float(cfg.get("execution.exit_retry_backoff_sec", 0) or 0),
            "exit_retry_backoff_max": float(cfg.get("execution.exit_retry_backoff_max_sec", 60) or 60),
            "lots": lots,
            "lot_multiplier": _lot_multiplier(),
            "max_daily_loss": float(r.get("max_daily_loss", 0) or 0),
            # untracked cleanup costs count against the daily-loss limit
            "day_pnl": round(trade_log.day_pnl() - untracked_ledger.day_cost(), 2),
            "enable_probability_filter": bool(f.get("enable_probability_filter", True)),
            "commission_basis": str(f.get("commission_basis", "per_lot")),
            "min_win_probability": float(f.get("min_win_probability", 0.60)),
            "min_expected_value": float(f.get("min_expected_value", 0.0)),
            "brokerage_per_lot": float(f.get("brokerage_per_lot", 20.0)),
            "slippage_per_lot": float(f.get("slippage_per_lot", 5.0)),
            "time_stop_half_lives": float(f.get("time_stop_half_lives", 3.0)),
            "trading_hours": cfg.section("trading_hours"),
            # no new entries within this many minutes of the exchange close
            "no_entry_buffer_min": float(cfg.get("trading_hours.no_entry_buffer_min", 20)),
        }

    arrow_algo = ArrowAutoTrader(
        signal_provider=signal_engine.get_signal,
        params_provider=_algo_params,
        execute_fn=_spread_execute,
        close_fn=_spread_close,
        prices_provider=_leg_prices,        # fresh leg prices for the entry guard
    )

    # Position recovery on restart: re-adopt any open trade from the log so the
    # engine manages (and can exit) a position it didn't open this run.
    try:
        _open = trade_log.open_position()
        if _open:
            arrow_algo.restore_position(_open)
    except Exception as exc:        # never block startup on recovery
        logger.warning("Position recovery skipped — {}", exc)

    # Cooldown recovery: re-arm the entry cooldown from the last algo close so a
    # stop right before shutdown doesn't allow an instant re-entry on restart.
    try:
        _cdsec = float(cfg.get("execution.cooldown_sec", 300) or 0)
        _last_close = trade_log.last_close_time(source="algo")
        if _cdsec > 0 and _last_close:
            arrow_algo.restore_cooldown(_last_close + _cdsec)
    except Exception as exc:
        logger.warning("Cooldown recovery skipped — {}", exc)

    def _reconcile() -> Dict:
        """Compare engine belief (algo position) against the broker's actual
        leg positions. Powers the dashboard mismatch banner so a divergence
        (rejected leg, manual close, orphan) is surfaced immediately."""
        st = arrow_algo.get_state()
        engine_open = bool(st.get("in_position"))
        legs = _read_legs()
        out = {"engine_open": engine_open, "engine": st.get("position"),
               "exchange": [], "mismatch": False, "message": "", "checked": False}
        broker = active.get()
        if not broker or not _have_both_legs(legs):
            return out
        try:
            positions = {str(p.get("symbol", "")).upper(): int(p.get("net_quantity", 0) or 0)
                         for p in (_positions_cached() or [])}
        except Exception as exc:
            out["message"] = f"could not read positions: {exc}"
            return out
        out["checked"] = True
        leg_syms = {lk: legs[lk]["symbol"] for lk in ("leg_a", "leg_b")}
        out["exchange"] = [{"leg": lk, "symbol": leg_syms[lk],
                            "net_quantity": positions.get(leg_syms[lk].upper(), 0)}
                           for lk in ("leg_a", "leg_b")]
        exch_open = any(e["net_quantity"] != 0 for e in out["exchange"])
        if engine_open and not exch_open:
            out["mismatch"] = True
            out["message"] = ("Engine holds a position but the exchange shows both legs "
                              "flat — it may have been closed/rejected outside the engine.")
        elif exch_open and not engine_open:
            out["mismatch"] = True
            out["message"] = ("Exchange shows an open leg position but the engine is flat — "
                              "possible orphaned leg or manual trade. Review positions.")
        return out

    # ── self-healing reconciliation (Tier B) ─────────────────────────────────
    def _bot_leg_positions() -> list:
        """Exchange net positions on the BOT'S legs ONLY (never other symbols)."""
        legs = _read_legs()
        if not _have_both_legs(legs):
            return []
        seg_of = {legs[lk]["symbol"].upper(): legs[lk]["segment"] for lk in ("leg_a", "leg_b")}
        out = []
        for p in (_positions_cached() or []):
            sym = str(p.get("symbol", "")).upper()
            if sym in seg_of:
                out.append({"symbol": sym, "segment": seg_of[sym],
                            "net_quantity": int(p.get("net_quantity", 0) or 0),
                            "ltp": float(p.get("ltp", 0) or 0)})
        return out

    def _flatten_bot_leg(p: Dict) -> None:
        """Market-close one orphaned bot leg; book an estimated cost to the ledger."""
        broker = active.get()
        qty = int(p.get("net_quantity", 0) or 0)
        if not broker or qty == 0:
            return
        side = "sell" if qty > 0 else "buy"
        broker.submit_order(symbol=p["symbol"], side=side, quantity=abs(qty),
                            order_type="market", price=None,
                            exchange_segment=p["segment"],
                            product=str(cfg.get("execution.product", "NRML")))
        est = round(abs(qty) * float(p.get("ltp") or 0) * 0.0003, 2)   # ~3bps market-close slip
        untracked_ledger.record(reason="orphan_auto_close", symbol=p["symbol"],
                                qty=abs(qty), est_cost=est,
                                detail=f"reconcile flattened {side} {abs(qty)} @market")

    _reconcile_guard = ReconcileGuard(
        engine_in_trade=lambda: bool(arrow_algo.get_state().get("in_position")),
        exchange_bot_positions=_bot_leg_positions,
        clear_engine=lambda: arrow_algo.clear_position("reconcile: exchange flat"),
        flatten_leg=_flatten_bot_leg,
        threshold=int(cfg.get("reconcile.mismatch_threshold", 3) or 3))

    def _start_reconcile_loop() -> None:
        def loop():
            while True:
                try:
                    rc = cfg.section("reconcile")
                    if bool(rc.get("enabled", False)):
                        _reconcile_guard._auto_close = bool(rc.get("auto_close", False))
                        res = _reconcile_guard.check()
                        if res.get("acted") == "cleared_engine":
                            untracked_ledger.record(reason="engine_ghost_cleared",
                                                    detail="exchange FLAT; engine state force-cleared")
                except Exception:
                    pass
                time.sleep(max(5.0, float(cfg.get("reconcile.interval_sec", 20) or 20)))
        threading.Thread(target=loop, daemon=True, name="Reconcile").start()

    _start_reconcile_loop()

    def _start_shadow_loop() -> None:
        """Mark active what-if-held watches against the live spread; finalize any
        whose window elapsed (incl. during downtime, on the first tick)."""
        def loop():
            while True:
                try:
                    shadow.update(signal_engine.get_signal().get("spread"))
                except Exception:                        # noqa: BLE001
                    pass
                time.sleep(max(2.0, float(cfg.get("exits.whatif_update_sec", 5) or 5)))
        threading.Thread(target=loop, daemon=True, name="ShadowWatch").start()

    _start_shadow_loop()

    # Liveness heartbeat — proves the process loop is spinning (independent of the
    # data feed), so an external watchdog can catch a FREEZE a crash-only
    # supervisor never sees. Data staleness is reported separately in /api/health.
    _heartbeat = Heartbeat(HEARTBEAT_FILE,
                           interval_sec=float(cfg.get("execution.heartbeat_sec", 10) or 10))
    _heartbeat.start()

    @app.route("/api/health", methods=["GET"])
    def api_health():
        """Loop liveness (heartbeat age) + feed liveness (last-tick age), reported
        separately: a frozen loop vs a stalled feed need different fixes."""
        hb_age = Heartbeat.age(HEARTBEAT_FILE)
        bars = signal_engine.export_bars()
        tick_age = (time.time() - bars[-1][0]) if bars else None
        v = health_verdict(hb_age, tick_age,
                           max_heartbeat_sec=float(cfg.get("execution.health_max_heartbeat_sec", 60) or 60),
                           max_tick_sec=float(cfg.get("execution.health_max_tick_sec", 120) or 120))
        return jsonify({
            "heartbeat_age_sec": round(hb_age, 1) if hb_age is not None else None,
            "last_tick_age_sec": round(tick_age, 1) if tick_age is not None else None,
            "running": bool(arrow_algo.get_state().get("running")),
            **v,
        })

    @app.route("/api/telegram/status", methods=["GET"])
    def api_telegram_status():
        """Whether the bot token (env) and chat id are set — the token is never
        returned, only whether it exists."""
        return jsonify({"token_set": telegram.token_set(),
                        "configured": telegram.configured(),
                        **_telegram_params()})

    @app.route("/api/telegram/test", methods=["POST"])
    def api_telegram_test():
        """Send a test message to the configured chat."""
        ok, msg = telegram.test()
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/shadow", methods=["GET"])
    def api_shadow():
        """What-if-held shadow: active watches + reversion stats (did the spread
        revert to break-even / target after we exited)."""
        return jsonify(shadow.summary())

    @app.route("/api/cost-audit", methods=["GET"])
    def api_cost_audit():
        """Realized round-trip cost vs the MODELED cost, with a miscalibration
        alarm (either side ≥ 2× the other) — the model must track real fills or
        the edge filter blocks good trades / lets through bad ones."""
        realized = trade_log.cost_audit()
        lots = int(_algo_lots["lots"]); lot_m = _lot_multiplier()
        brk = float(cfg.get("filters.brokerage_per_lot", 20) or 0) * lots * 4
        slp = float(cfg.get("filters.slippage_per_lot", 5) or 0) * lots * 4
        stt_pct = float(cfg.get("filters.stt_pct", 0) or 0) / 100.0
        other_pct = float(cfg.get("filters.other_cost_pct", 0) or 0) / 100.0
        la, _ = _leg_prices()
        notional = float(la) * lots * lot_m if la else 0.0
        stt = 2 * stt_pct * notional
        other = 4 * other_pct * notional
        modeled = round(brk + slp + stt + other, 2)
        rc = realized.get("avg_realized_cost", 0.0)
        alarm = bool(rc > 0 and (modeled >= 2 * rc or rc >= 2 * modeled))
        return jsonify({"modeled_cost": modeled, "realized": realized, "alarm": alarm})

    @app.route("/api/untracked", methods=["GET"])
    def api_untracked():
        return jsonify({"events": untracked_ledger.all(), "total": untracked_ledger.total(),
                        "day_cost": untracked_ledger.day_cost(),
                        "reconcile": _reconcile_guard.last})

    # ── pages ────────────────────────────────────────────────────────────────
    @app.route("/")
    def setup():
        return render_template("setup.html", broker_name=cfg.get("broker.name", "arrow"))

    @app.route("/dashboard")
    def dashboard():
        legs = _read_legs()
        return render_template(
            "dashboard.html",
            broker_name=cfg.get("broker.name", "arrow"),
            leg_a=legs.get("leg_a", {}).get("symbol", "Leg A"),
            leg_b=legs.get("leg_b", {}).get("symbol", "Leg B"),
            display_refresh_ms=int(cfg.get("signal.display_refresh_ms", 50)),
            entry_zscore=float(cfg.get("signal.entry_zscore", 2.0)),
            stop_zscore=float(cfg.get("signal.stop_zscore", 4.0)),
            window_minutes=int(cfg.get("signal.window_minutes", 120)),
        )

    # ── Arrow connection ─────────────────────────────────────────────────────
    @app.route("/api/arrow/connect", methods=["POST"])
    def api_arrow_connect():
        data = request.get_json(silent=True) or {}
        # Credentials: request body first, then environment.
        env = Config.arrow_credentials()
        creds = {k: (str(data.get(k, "")).strip() or env.get(k, ""))
                 for k in ("app_id", "user_id", "password", "api_secret", "totp_secret")}
        missing = [k for k, v in creds.items() if not v]
        if missing:
            return jsonify({"success": False,
                            "error": f"Missing credentials: {', '.join(missing)} "
                                     f"(send in form or set ARROW_* env vars)"})
        creds["lot_sizes"] = cfg.get("broker.lot_overrides") or {}
        # Reuse a still-valid session token from a previous run so a restart
        # skips the 2FA login (the broker validates it and re-logs in if dead).
        persist = bool(cfg.get("broker.persist_session", True))
        if persist and not str(data.get("token", "")).strip():
            saved = _load_session_token(creds["app_id"])
            if saved:
                creds["token"] = saved
        try:
            broker = create_broker(cfg.get("broker.name", "arrow"), creds)
            ok = broker.connect()
            if ok:
                active.set(broker)
                signal_engine.reset()
                signal_engine.start()
                if persist and hasattr(broker, "get_session_token"):
                    _save_session_token(creds["app_id"], broker.get_session_token())
            return jsonify({
                "success": ok,
                "message": "Connected" if ok else "Login failed",
                "error": "" if ok else (getattr(broker, "last_error", "") or "Login failed — check credentials"),
                "token": broker.get_session_token() if ok and hasattr(broker, "get_session_token") else "",
            })
        except Exception as exc:
            logger.error("Arrow connect error: {}", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/arrow/disconnect", methods=["POST"])
    def api_arrow_disconnect():
        arrow_algo.stop()
        signal_engine.stop()
        # An explicit disconnect ends the session — drop the cached token so the
        # next connect logs in fresh. active.clear() invalidates the live session.
        _clear_session_token()
        active.clear()
        return jsonify({"success": True})

    @app.route("/api/arrow/status", methods=["GET"])
    def api_arrow_status():
        broker = active.get()
        if not broker:
            return jsonify({"connected": False, "instruments_ready": False, "instrument_count": 0})
        ready = broker._instruments_ready.is_set() if hasattr(broker, "_instruments_ready") else True
        return jsonify({
            "connected": True,
            "instruments_ready": ready,
            "instrument_count": len(getattr(broker, "_instruments", [])),
            "lot_size_count": len(getattr(broker, "_lot_sizes", {})),
        })

    # ── order-test scenario suite (Setup page) ────────────────────────────────
    @app.route("/api/scenario-catalogue", methods=["GET"])
    def api_scenario_catalogue():
        """The 40 round-trip order scenarios (read-only). The runner is unit-
        tested; live execution (real min-lot orders) activates with the
        multi-asset execution adapter."""
        return jsonify(scenarios.CATALOGUE)

    @app.route("/api/scenario-test", methods=["POST"])
    def api_scenario_test():
        """Run ONE scenario at minimum lot via the Arrow leg adapter. SAFE in
        live_sim (simulated broker, no real orders); in live it places REAL
        min-lot round trips. Gated: broker connected, both legs set, and the
        algo NOT running (never interfere with a live position)."""
        data = request.get_json(silent=True) or {}
        try:
            sid = int(data.get("id"))
            scen = scenarios.CATALOGUE[sid]
        except (TypeError, ValueError, IndexError):
            return jsonify({"ok": False, "error": "unknown scenario id"}), 400

        if _mode() == "dry_run":
            return jsonify({"ok": False, "error": "dry_run places no orders — "
                            "switch to live_sim to exercise the plumbing safely"})
        broker = sim_broker if _mode() == "live_sim" else active.get()
        if not broker:
            return jsonify({"ok": False, "error": "connect a broker first"})
        if arrow_algo.get_state().get("running"):
            return jsonify({"ok": False, "error": "stop the algo (and flatten) "
                            "before running order tests"})
        legs = _read_legs()
        if not _have_both_legs(legs):
            return jsonify({"ok": False, "error": "set both legs on this page first"})

        from arrow_statarb.core.arrow_leg import ArrowLeg
        from arrow_statarb.core.scenarios import ScenarioRunner
        seg_map = {legs["leg_a"]["symbol"]: legs["leg_a"]["segment"],
                   legs["leg_b"]["symbol"]: legs["leg_b"]["segment"]}
        leg = ArrowLeg(broker, seg_map,
                       product=str(cfg.get("execution.product", "NRML")))
        runner = ScenarioRunner(leg, leg, legs["leg_a"]["symbol"],
                                legs["leg_b"]["symbol"])
        try:
            result = runner.run(scen["type"], scen["mode"], scen["variant"])
        except Exception as exc:                       # never let a test crash the app
            logger.warning("scenario {} failed — {}", sid, exc)
            return jsonify({"ok": False, "error": str(exc), "id": sid})
        result["id"] = sid
        result["name"] = scen["name"]
        return jsonify(result)

    # ── instrument picker (segment → underlying → contract) ──────────────────
    def _kind(ctype: str, exch_seg: str) -> Optional[str]:
        if ctype == "options":
            return "option"
        if ctype == "futures":
            return "cash" if exch_seg.endswith("CM") else "future"
        return None

    @app.route("/api/arrow/commodities", methods=["GET"])
    def api_arrow_commodities():
        broker = active.get()
        if not broker or (hasattr(broker, "_instruments_ready") and not broker._instruments_ready.is_set()):
            return jsonify([])
        exch = _seg_to_exch_seg().get(request.args.get("segment", "nse_fo").lower())
        kind = _kind(request.args.get("type", "futures"), exch or "")
        if not exch or not kind:
            return jsonify([])
        return jsonify(broker.list_underlyings(exch, kind))

    @app.route("/api/arrow/contracts", methods=["GET"])
    def api_arrow_contracts():
        broker = active.get()
        if not broker or (hasattr(broker, "_instruments_ready") and not broker._instruments_ready.is_set()):
            return jsonify([])
        exch = _seg_to_exch_seg().get(request.args.get("segment", "nse_fo").lower())
        commodity = request.args.get("commodity", "").strip()
        kind = _kind(request.args.get("type", "futures"), exch or "")
        if not exch or not commodity or not kind:
            return jsonify([])
        rows = broker.list_contracts(exch, kind, commodity)
        return jsonify([{"trading_symbol": r["trading_symbol"], "token": r["token"],
                         "expiry": r["expiry"], "lot_size": r["lot_size"]} for r in rows])

    # ── leg assignments ──────────────────────────────────────────────────────
    @app.route("/api/leg-assignments", methods=["GET", "POST"])
    def api_leg_assignments():
        if request.method == "GET":
            if LEG_ASSIGNMENTS_FILE.exists():
                with open(LEG_ASSIGNMENTS_FILE) as f:
                    return jsonify(yaml.safe_load(f) or {})
            return jsonify({})
        data = request.get_json(force=True) or {}
        try:
            assigns = {}
            if LEG_ASSIGNMENTS_FILE.exists():
                with open(LEG_ASSIGNMENTS_FILE) as f:
                    assigns = yaml.safe_load(f) or {}
            for lk in ("leg_a", "leg_b"):
                if lk in data and (data[lk] or {}).get("mapping_id"):
                    assigns[lk] = {"mapping_id": data[lk]["mapping_id"],
                                   "ratio": float(data[lk].get("ratio", 1.0) or 1.0)}
            LEG_ASSIGNMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LEG_ASSIGNMENTS_FILE, "w") as f:
                yaml.dump(assigns, f, default_flow_style=False)
            signal_engine.reset()   # legs changed → start the window fresh
            logger.info("Leg assignments saved: {}", assigns)
            return jsonify({"success": True})
        except Exception as exc:
            logger.error("Error saving leg assignments: {}", exc)
            return jsonify({"success": False, "error": str(exc)})

    @app.route("/api/leg-info", methods=["GET"])
    def api_leg_info():
        """Per-leg symbol + lot size with a lot-size mismatch flag. NSE revises
        lot sizes between expiries, so a calendar spread can have unequal lots
        (e.g. 550 vs 650) — 1 lot each is then NOT share-neutral."""
        out = {"connected": False, "legs": {}, "lot_mismatch": False, "message": ""}
        broker = active.get()
        if not broker:
            return jsonify(out)
        out["connected"] = True
        legs = _read_legs()
        lots = {}
        today_ist = datetime.now(_IST).date()
        for lk in ("leg_a", "leg_b"):
            if lk not in legs:
                continue
            try:
                ls = int(broker.resolve_lot_size(legs[lk]["segment"], legs[lk]["symbol"]))
            except Exception:
                ls = 0
            # Days-to-expiry — INFO ONLY. Purely a dashboard readout; it does not
            # gate entries or execution (the algo never reads this).
            dte = None
            if hasattr(broker, "resolve_expiry_ymd"):
                try:
                    ymd = broker.resolve_expiry_ymd(legs[lk]["symbol"])
                    if ymd and ymd != (9999, 99, 99):
                        y, m, d = ymd
                        dte = (date(y, m, d) - today_ist).days
                except Exception:
                    dte = None
            out["legs"][lk] = {"symbol": legs[lk]["symbol"], "lot_size": ls,
                               "days_to_expiry": dte}
            lots[lk] = ls
        la, lb = lots.get("leg_a"), lots.get("leg_b")
        if la and lb and la != lb:
            out["lot_mismatch"] = True
            out["message"] = (f"⚠ Lot sizes differ — Leg A {la} vs Leg B {lb}. "
                              f"1 lot each is NOT share-neutral ({la} vs {lb} units). "
                              f"Use same-expiry-tier legs or adjust ratios.")
        return jsonify(out)

    @app.route("/api/pair-analytics", methods=["GET"])
    def api_pair_analytics():
        """Read-only pair analytics for the new dashboard cards (Phase 6 merge):
        cost-of-carry fair value (display only), contract-aware sizing (the k =
        spread_units multiplier, notionals, hedge, imbalance, min-notional), and
        the live-vs-configured hedge-ratio drift. Never touches the algo."""
        out: Dict[str, Any] = {"ok": False}
        legs = _read_legs()
        if not _have_both_legs(legs):
            return jsonify(out)
        broker = active.get() or (sim_broker if _mode() == "live_sim" else None)
        la, lb = _leg_prices()
        sig = signal_engine.get_signal()
        spread = sig.get("spread")
        hedge_ratio = float(cfg.get("signal.hedge_ratio", 1) or 1)

        def _cs(lk):
            try:
                return float(broker.resolve_lot_size(legs[lk]["segment"],
                                                     legs[lk]["symbol"]))
            except Exception:
                return 1.0

        def _exp_iso(sym):
            if broker is not None and hasattr(broker, "resolve_expiry_ymd"):
                try:
                    ymd = broker.resolve_expiry_ymd(sym)
                    if ymd and ymd != (9999, 99, 99):
                        return f"{ymd[0]:04d}-{ymd[1]:02d}-{(ymd[2] or 1):02d}"
                except Exception:
                    pass
            return None

        contract_a, contract_b = _cs("leg_a"), _cs("leg_b")
        pairs = cfg.section("pairs")
        asset_cfg = {
            "pair_type": pairs.get("pair_type", "SPOT_FUTURE"),
            "risk_free_rate": pairs.get("risk_free_rate", 0.0425),
            "futures_expiry": _exp_iso(legs["leg_b"]["symbol"]),
            "spot_expiry": _exp_iso(legs["leg_a"]["symbol"]),
        }
        fv = size = None
        if la and lb:
            # Arrow spread = hedge_ratio*leg_a − leg_b; use it when the signal
            # window isn't warm yet so the card still reads.
            eff_spread = spread if spread is not None else (hedge_ratio * la - lb)
            fv = fairvalue.fair_value_block(asset_cfg, la, lb, eff_spread, hedge_ratio)
            # fairvalue uses the reference's futures−spot convention (leg_b−leg_a);
            # Arrow's spread is leg_a−leg_b (the exact negative). Flip the sign so
            # the card's fair value and gap are in the SAME convention as the
            # live spread shown beside them.
            if fv and fv.get("fair_value") is not None:
                fv["fair_value"] = -fv["fair_value"]
                fv["fair_gap"] = eff_spread - fv["fair_value"]
            params = {
                "HEDGE_RATIO": hedge_ratio,
                "SIZING_MODE": str(pairs.get("sizing_mode", "lots")),
                "NOTIONAL_PER_LEG_INR": float(pairs.get("notional_per_leg_inr", 0) or 0),
                "CLIP_LOTS": float(cfg.get("risk.lots_per_trade", 1) or 1),
                "HEDGE_MODE": str(pairs.get("hedge_mode", "units")),
            }
            size = sizing.plan(params, contract_a, contract_b, la, lb)
        out.update(ok=True, fair_value=fv, sizing=size, contract_a=contract_a,
                   contract_b=contract_b, hedge_ratio=hedge_ratio,
                   leg_a_price=la, leg_b_price=lb, spread=spread,
                   pair_type=asset_cfg["pair_type"],
                   leg_a=legs["leg_a"]["symbol"], leg_b=legs["leg_b"]["symbol"])
        return jsonify(out)

    def _engine_cfgs():
        """Map Arrow settings → the ported engine's SIGNALS/EXITS cfg dicts, for
        the shadow preview. Cooldowns/trend are stateless here (a one-shot
        preview), so they're neutralised."""
        s, xo, f = cfg.section("signal"), cfg.section("exits"), cfg.section("filters")
        signals_cfg = {
            "ENTRY_Z": float(s.get("entry_zscore", 2) or 2),
            "EXIT_Z": float(s.get("exit_zscore", 0) or 0),
            "STOP_Z": float(s.get("stop_zscore", 4) or 4),
            "MAX_ENTRY_Z": float(s.get("max_entry_zscore", 0) or s.get("stop_zscore", 4)),
            "TREND_FILTER": False, "ENTRY_COOLDOWN_SEC": 0, "STOP_COOLDOWN_SEC": 0,
        }
        frac = float(xo.get("profit_target_sigma_frac", 0) or 0)
        exits_cfg = {
            "USE_SIGMA_TARGET": frac > 0,
            "TP_CAPITAL_PCT": float(xo.get("tp_capital_pct", 0) or 0),
            "TP_INR_PER_LOT": float(xo.get("profit_target_inr", 0) or 0),
            "COST_FLOOR_MULT": float(xo.get("cost_floor_mult", 0) or 0),
            "STOP_INR_PER_LOT": float(xo.get("dollar_stop_inr", 0) or 0),
            "STOP_CAPITAL_PCT": float(xo.get("stop_capital_pct", 0) or 0),
            "RR": float(xo.get("stop_rr", 0) or 0),
            "GATE_FLOOR_INR": float(xo.get("reversion_gate_inr", 0) or 0),
            "MAX_HOLD_HALF_LIVES": float(f.get("time_stop_half_lives", 3) or 3),
            "MAX_HOLD_FALLBACK_MIN": 240,
            "HARD_TIME_STOP_MULT": float(xo.get("hard_time_stop_mult", 0) or 0),
            "HARD_MAX_HOLD_MIN": 0,
            "Z_STOP_EXIT_ENABLED": bool(xo.get("z_stop_exit_enabled", True)),
            "MAX_HOLD_PROGRESS_SUPPRESS": float(xo.get("max_hold_z_progress_min", 0.5) or 0.5),
        }
        return signals_cfg, exits_cfg, frac

    @app.route("/api/engine/shadow", methods=["GET"])
    def api_engine_shadow():
        """SHADOW: what the ported multi-asset engine WOULD do on the current
        pair right now — entry gate, exit-ladder levels, clip sizing — computed
        from the live signal + your settings. Places NO orders."""
        out: Dict[str, Any] = {"ready": False, "shadow": True}
        legs = _read_legs()
        sig = signal_engine.get_signal()
        z, spread, std = sig.get("zscore"), sig.get("spread"), sig.get("std")
        if not _have_both_legs(legs) or not sig.get("ready") or z is None or not std:
            out["reason"] = "warming up, or legs not set"
            return jsonify(out)

        signals_cfg, exits_cfg, frac = _engine_cfgs()
        slope = float((sig.get("regime_detail") or {}).get("slope", 0.0) or 0.0)

        class _Shim:                                   # SpreadStats-shaped preview
            warm = True
            def __init__(s):
                s.z, s.sigma = z, std
                s.half_life_sec = sig.get("half_life_sec")
            def trend_slope(s):
                return slope

        gen = ZSignalGenerator(signals_cfg, clock=time.time)
        direction = gen.entry_signal("shadow", _Shim(), sig, {}, 1, 1)
        gate = gen._blocking.get("shadow")

        # contract-aware sizing (same path as /api/pair-analytics)
        broker = active.get() or (sim_broker if _mode() == "live_sim" else None)
        la, lb = _leg_prices()
        hedge_ratio = float(cfg.get("signal.hedge_ratio", 1) or 1)
        def _cs(lk):
            try:
                return float(broker.resolve_lot_size(legs[lk]["segment"], legs[lk]["symbol"]))
            except Exception:
                return 1.0
        contract_a, contract_b = _cs("leg_a"), _cs("leg_b")
        pairs = cfg.section("pairs")
        size = None
        if la and lb:
            params = {"HEDGE_RATIO": hedge_ratio,
                      "SIZING_MODE": str(pairs.get("sizing_mode", "lots")),
                      "NOTIONAL_PER_LEG_INR": float(pairs.get("notional_per_leg_inr", 0) or 0),
                      "CLIP_LOTS": float(cfg.get("risk.lots_per_trade", 1) or 1),
                      "HEDGE_MODE": str(pairs.get("hedge_mode", "units"))}
            size = sizing.plan(params, contract_a, contract_b, la, lb)
        lots_b = float((size or {}).get("leg_b_lots") or 0)
        k = float((size or {}).get("spread_units") or 0)

        # legacy round-trip cost (₹) on leg-B notional — matches the algo's model
        f = cfg.section("filters")
        notional_b = (lb or 0) * lots_b * contract_b
        rt_cost = (float(f.get("brokerage_per_lot", 20) or 0) * max(1, lots_b) * 4.0
                   + 2.0 * (float(f.get("stt_pct", 0) or 0) / 100.0) * notional_b
                   + 4.0 * (float(f.get("other_cost_pct", 0) or 0) / 100.0) * notional_b)
        capital = float(cfg.get("risk.capital_at_risk_inr", 0) or 0) or None

        ladder = ExitLadder(exits_cfg, signals_cfg, target_fraction=frac or 0.5)
        levels = plan = None
        viable = True
        if direction and lots_b > 0 and k > 0:
            plan = ladder.build_plan(lots=lots_b, contract_size=contract_b,
                                     entry_z=z, sigma=std,
                                     half_life_sec=sig.get("half_life_sec"),
                                     rt_cost=rt_cost, capital=capital,
                                     entry_mu=sig.get("mean"))
            if plan is None:
                viable = False
            else:
                levels = ExitLadder.spread_levels(plan, spread, k, direction)

        out.update(ready=True, z=z, spread=spread, would_enter=direction,
                   blocking_gate=gate, viable=viable, levels=levels,
                   tp_inr=(plan or {}).get("tp_inr"),
                   stop_inr=(plan or {}).get("stop_inr"),
                   rt_cost_inr=round(rt_cost, 2), k=k, lots_b=lots_b)
        return jsonify(out)

    # ── prices / positions / signal ──────────────────────────────────────────
    @app.route("/api/prices", methods=["GET"])
    def api_prices():
        broker = active.get()
        legs = _read_legs()
        if not broker:
            return jsonify({"leg_a": None, "leg_b": None, "connected": False})
        if not _have_both_legs(legs):
            return jsonify({"leg_a": None, "leg_b": None, "connected": True,
                            "error": "No leg assignments"})
        syms = {lk: legs[lk]["symbol"] for lk in ("leg_a", "leg_b")}
        # Stream first
        if hasattr(broker, "start_price_stream"):
            try:
                broker.start_price_stream(list(syms.values()))
                streamed = broker.get_streamed_ltp(list(syms.values()))
            except Exception:
                streamed = {}
            if streamed:
                result = {"leg_a": None, "leg_b": None, "connected": True, "source": "stream"}
                have_all = True
                for lk, sym in syms.items():
                    v = streamed.get(sym.upper())
                    result[lk] = v
                    if v is None:
                        have_all = False
                if have_all:
                    _ltp_cache.update({k: result.get(k) for k in ("leg_a", "leg_b")})
                    return jsonify(result)
        # REST fallback
        instruments, sym_map = [], {}
        for lk, sym in syms.items():
            instruments.append({"exchange_segment": legs[lk]["segment"], "instrument_token": sym})
            sym_map[sym.upper()] = lk
        try:
            raw = broker.get_ltp(instruments)
        except Exception as exc:
            logger.warning("LTP fetch failed (cached): {}", exc)
            cached = dict(_ltp_cache); cached.update({"connected": True, "stale": True})
            return jsonify(cached)
        result = {"leg_a": None, "leg_b": None, "connected": True, "source": "rest"}
        for key, ltp in (raw or {}).items():
            lk = sym_map.get(str(key).upper())
            if lk:
                result[lk] = ltp
        _ltp_cache.update({k: result.get(k) for k in ("leg_a", "leg_b")})
        return jsonify(result)

    @app.route("/api/signal", methods=["GET"])
    def api_signal():
        """The server-side signal — the SINGLE source of truth the dashboard
        displays so its z matches the algo's exactly."""
        sig = signal_engine.get_signal()
        sig["dry_run"] = _is_dry_run()
        return jsonify(sig)

    @app.route("/api/signal/history", methods=["GET"])
    def api_signal_history():
        """Recent spread + z series for the dashboard's Spread History and
        Z-Score charts (server-side, so the charts match the algo's z)."""
        try:
            n = min(1000, max(20, int(request.args.get("points", 200))))
        except (ValueError, TypeError):
            n = 200
        return jsonify(signal_engine.get_series(n))

    @app.route("/api/signal/excursions", methods=["GET"])
    def api_signal_excursions():
        """Session-cumulative z-score excursion counts (±2σ / ±3σ touches and
        mean reversions) for the Analysis page."""
        return jsonify(signal_engine.get_excursions())

    @app.route("/api/signal/excursions/reset", methods=["POST"])
    def api_signal_excursions_reset():
        signal_engine.reset_excursions()
        return jsonify({"success": True})

    @app.route("/api/positions", methods=["GET"])
    def api_positions():
        out = {"connected": False, "direction": "FLAT", "leg_a": 0, "leg_b": 0,
               "unrealized_pnl": 0.0}
        broker = active.get()
        if not broker:
            return jsonify(out)
        out["connected"] = True
        legs = _read_legs()
        leg_syms = {lk: legs[lk]["symbol"].upper() for lk in ("leg_a", "leg_b") if lk in legs}
        try:
            positions = _positions_cached() or []
        except Exception as exc:
            out["error"] = str(exc)
            return jsonify(out)
        by_sym = {str(p.get("symbol", "")).upper(): p for p in positions if p.get("symbol")}
        streamed = {}
        if hasattr(broker, "get_streamed_ltp") and leg_syms:
            try:
                streamed = broker.get_streamed_ltp(list(leg_syms.values()))
            except Exception:
                streamed = {}
        total_pnl, nets = 0.0, {}
        for lk, sym in leg_syms.items():
            p = by_sym.get(sym)
            if not p:
                continue
            nq = int(p.get("net_quantity") or 0)
            avg = float(p.get("average_price") or 0)
            nets[lk] = nq
            out[lk] = nq
            ltp = streamed.get(sym) or p.get("ltp") or 0
            if ltp and avg and nq:
                total_pnl += (float(ltp) - avg) * nq
            elif p.get("pnl"):
                total_pnl += float(p.get("pnl"))
        out["unrealized_pnl"] = round(total_pnl, 2)
        a, b = nets.get("leg_a", 0), nets.get("leg_b", 0)
        if a > 0 and b < 0:
            out["direction"] = "LONG_SPREAD"
        elif a < 0 and b > 0:
            out["direction"] = "SHORT_SPREAD"
        elif a or b:
            out["direction"] = "OPEN"
        return jsonify(out)

    # ── manual trade ─────────────────────────────────────────────────────────
    @app.route("/api/manual-trade/activate", methods=["POST"])
    def api_manual_trade_activate():
        if not active.get():
            return jsonify({"success": False, "error": "No broker connected"})
        data = request.get_json(silent=True) or {}
        logger.info("Manual trade armed: dir={} lots={}", data.get("direction"), data.get("lots"))
        return jsonify({"success": True})

    @app.route("/api/manual-trade/cancel", methods=["POST"])
    def api_manual_trade_cancel():
        logger.info("Manual trade cancelled (watching)")
        return jsonify({"success": True})

    @app.route("/api/manual-trade/execute", methods=["POST"])
    def api_manual_trade_execute():
        data = request.get_json(force=True) or {}
        return jsonify(_spread_execute(data.get("direction", "LONG_SPREAD"), int(data.get("lots", 1))))

    @app.route("/api/manual-trade/close", methods=["POST"])
    def api_manual_trade_close():
        data = request.get_json(force=True) or {}
        return jsonify(_spread_close(data.get("direction", "LONG_SPREAD"), int(data.get("lots", 1))))

    # ── algo control ─────────────────────────────────────────────────────────
    @app.route("/api/algo/start", methods=["POST"])
    def api_algo_start():
        if not active.get():
            return jsonify({"success": False, "error": "No broker connected — connect Arrow first"})
        if not _have_both_legs(_read_legs()):
            return jsonify({"success": False, "error": "Both legs must be assigned in Setup"})
        data = request.get_json(silent=True) or {}
        try:
            _algo_lots["lots"] = max(1, int(data.get("lots", _algo_lots["lots"])))
        except (ValueError, TypeError):
            pass
        signal_engine.start()   # ensure the signal is running
        ok = arrow_algo.start()
        logger.info("Algo start requested (lots={}, mode={})",
                    _algo_lots["lots"], "DRY-RUN" if _is_dry_run() else "LIVE")
        return jsonify({"success": True, "running": True, "already_running": not ok,
                        "lots": _algo_lots["lots"], "dry_run": _is_dry_run()})

    @app.route("/api/algo/stop", methods=["POST"])
    def api_algo_stop():
        arrow_algo.stop()
        return jsonify({"success": True, "running": False})

    @app.route("/api/algo/state", methods=["GET"])
    def api_algo_state():
        st = arrow_algo.get_state()
        st["dry_run"] = _is_dry_run()
        return jsonify(st)

    @app.route("/api/reconcile", methods=["GET"])
    def api_reconcile():
        return jsonify(_reconcile())

    # ── mode (dry-run / live) ────────────────────────────────────────────────
    @app.route("/api/config", methods=["GET"])
    def api_config():
        return jsonify({"mode": cfg.mode, "dry_run": _is_dry_run(),
                        "broker": cfg.get("broker.name", "arrow")})

    @app.route("/api/preflight", methods=["GET"])
    def api_preflight():
        """Aggregated go-live readiness checks for the dashboard checklist."""
        broker = active.get()
        legs = _read_legs()
        sig = signal_engine.get_signal()
        r = cfg.section("risk")
        checks: list = []

        def add(key, label, status, detail):
            checks.append({"key": key, "label": label, "status": status, "detail": detail})

        # 1 — broker connected (+ instruments loaded)
        if broker:
            ev = getattr(broker, "_instruments_ready", None)
            instr_ok = ev.is_set() if (ev is not None and hasattr(ev, "is_set")) else True
            add("connected", "Arrow connected", "ok" if instr_ok else "warn",
                "Connected" if instr_ok else "Connected — loading instruments…")
        else:
            add("connected", "Arrow connected", "fail", "Not connected — Setup → Connect")

        # 2 — both legs assigned & share-neutral
        both = _have_both_legs(legs)
        la = lb = None
        if both and broker:
            try:
                la = int(broker.resolve_lot_size(legs["leg_a"]["segment"], legs["leg_a"]["symbol"]))
                lb = int(broker.resolve_lot_size(legs["leg_b"]["segment"], legs["leg_b"]["symbol"]))
            except Exception:
                pass
        if not both:
            add("legs", "Both legs assigned", "fail", "Assign Leg A and Leg B in Setup")
        elif la and lb and la != lb:
            add("legs", "Both legs assigned", "warn", f"Lot mismatch {la} vs {lb} — not share-neutral")
        else:
            add("legs", "Both legs assigned", "ok",
                f"{legs['leg_a']['symbol']} − {legs['leg_b']['symbol']}")

        # 3 — account funded
        funds = {}
        try:
            funds = _funds_cached()
        except Exception:
            funds = {}
        avail = funds.get("available")
        min_margin = float(r.get("min_live_margin", 0) or 0)
        if not broker:
            add("funds", "Account funded", "fail", "Connect to read funds")
        elif avail is None:
            add("funds", "Account funded", "warn", "Broker did not report funds — verify margin manually")
        elif avail <= 0:
            add("funds", "Account funded", "fail", "No available margin")
        elif min_margin > 0 and avail < min_margin:
            # Funds detected, but below the configured per-trade margin floor —
            # a live order would likely be rejected for insufficient margin.
            add("funds", "Account funded", "warn",
                f"Available ₹{avail:,.0f} below required ₹{min_margin:,.0f} — top up before live")
        else:
            tail = (f" (≥ ₹{min_margin:,.0f} required)" if min_margin > 0
                    else " — verify ≥ position margin")
            add("funds", "Account funded", "ok", f"Available ₹{avail:,.0f}{tail}")

        # 4 — risk caps set (advisory)
        cap = int(r.get("max_contracts_per_leg", 0) or 0)
        mdl = float(r.get("max_daily_loss", 0) or 0)
        lots = int(r.get("lots_per_trade", _algo_lots["lots"]))
        add("caps", "Risk caps set", "ok" if (cap > 0 and mdl > 0) else "warn",
            f"{lots} lot(s) · per-leg cap {cap or '∞'} · daily loss {('₹%.0f' % mdl) if mdl else 'off'}")

        # 5 — signal ready to trade
        if sig.get("ready"):
            add("signal", "Signal ready", "ok", f"z = {sig.get('zscore')}")
        elif sig.get("spread") is None:
            add("signal", "Signal ready", "fail", "No price data yet")
        else:
            add("signal", "Signal ready", "warn",
                f"collecting {float(sig.get('span_minutes', 0)):.1f}/"
                f"{float(sig.get('min_signal_minutes', 0)):.0f} min")

        # 6 — fill confirmation verified on a real (live) fill
        live_evs = [e for e in execution_log.all() if e.get("mode") == "live"]
        if not live_evs:
            add("sdk", "Fill confirmation (live)", "pending", "Confirmed on your first live fill")
        elif any(l.get("unconfirmed") for e in live_evs for l in e.get("legs", [])):
            add("sdk", "Fill confirmation (live)", "fail",
                "Broker returned UNKNOWN — get_order_status not wired; orphan detection blind")
        else:
            add("sdk", "Fill confirmation (live)", "ok", "get_order_status returning real fills")

        # 7 — entry guards armed (advisory): regime-shift cap, stale-signal
        # divergence, and confirmation ticks all configured.
        s = cfg.section("signal")
        zcap = float(s.get("max_entry_zscore", 0) or 0)
        zdiv = float(s.get("max_entry_z_divergence", 0) or 0)
        conf = int(s.get("confirmation_ticks", 0) or 0)
        gbits = [f"|z| cap {zcap:.1f}" if zcap > 0 else "no |z| cap",
                 f"divergence {zdiv:.1f}" if zdiv > 0 else "no divergence guard",
                 f"{conf} confirm tick(s)" if conf > 0 else "no confirmation"]
        add("entry_guards", "Entry guards armed",
            "ok" if (zcap > 0 and zdiv > 0 and conf > 0) else "warn", " · ".join(gbits))

        # 8 — exit safety armed (advisory): MARKET fallback on + a failure ceiling.
        ex = cfg.section("execution")
        l2m = bool(ex.get("limit_to_market", True))
        ceil_n = int(ex.get("max_exit_failures", 0) or 0)
        ebits = ["MARKET fallback on" if l2m else "MARKET fallback OFF",
                 f"exit ceiling {ceil_n}" if ceil_n > 0 else "no exit ceiling"]
        add("exit_safety", "Exit safety armed",
            "ok" if (l2m and ceil_n > 0) else "warn", " · ".join(ebits))

        # Advisory checks (entry_guards, exit_safety, caps, sdk) inform but do not
        # block go-live; only the critical four gate `ready`.
        critical = {"connected", "legs", "funds", "signal"}
        ready = all(c["status"] == "ok" for c in checks if c["key"] in critical)
        return jsonify({"mode": _mode(), "ready": ready, "checks": checks})

    @app.route("/api/trading-mode", methods=["POST"])
    def api_trading_mode():
        data = request.get_json(force=True) or {}
        m = str(data.get("mode", "")).lower()
        mode = m if m in ("live", "live_sim") else "dry_run"
        cfg.set_mode(mode)
        logger.warning("Trading mode set to {}", mode.upper())
        return jsonify({"success": True, "mode": mode, "dry_run": _is_dry_run()})

    @app.route("/api/execution", methods=["GET"])
    def api_execution():
        """Execution telemetry — recent live/live-sim executions with per-leg
        fill vs reference, slippage, amendments, escalation and orphan recovery."""
        return jsonify({"events": execution_log.all(), "stats": execution_log.stats()})

    @app.route("/api/execution/clear", methods=["POST"])
    def api_execution_clear():
        execution_log.clear()
        return jsonify({"success": True})

    @app.route("/api/exchange-orders", methods=["GET"])
    def api_exchange_orders():
        """The broker's own order book straight from the exchange (source of
        truth), for the Exchange Order Log. Live broker only — the sim broker
        has no exchange book."""
        broker = active.get()
        name = str(cfg.get("broker.name", "arrow"))
        if not broker or not hasattr(broker, "get_order_book"):
            return jsonify({"orders": [], "broker": name,
                            "note": "connect the broker to load the exchange order book"})
        try:
            orders = broker.get_order_book() or []
        except Exception as exc:                      # noqa: BLE001
            logger.warning("api_exchange_orders failed — {}", exc)
            return jsonify({"orders": [], "broker": name, "note": "could not read order book"})
        return jsonify({"orders": orders, "broker": name, "count": len(orders)})

    # ── settings page + API ──────────────────────────────────────────────────
    @app.route("/settings")
    def settings_page():
        return render_template("settings.html", broker_name=cfg.get("broker.name", "arrow"))

    @app.route("/analysis")
    def analysis_page():
        return render_template("analysis.html")

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        cfg.reload()
        if request.method == "GET":
            s, f, r, th = (cfg.section("signal"), cfg.section("filters"),
                           cfg.section("risk"), cfg.section("trading_hours"))
            ex = cfg.section("execution")
            xo = cfg.section("exits")
            rg = cfg.section("regime")
            rc = cfg.section("reconcile")
            bk = cfg.section("broker")
            co = cfg.section("costs")
            pa = cfg.section("pairs")
            _cseg = co.get("segments") or {}
            def _segrate(seg, rate):
                v = (_cseg.get(seg) or {}).get(rate)
                return v if v is not None else None
            return jsonify({
                "costs": {
                    "use_segment_costs": bool(co.get("use_segment_costs", False)),
                    "gst_pct": co.get("gst_pct", 18.0),
                    "no_entry_days_before_expiry": co.get("no_entry_days_before_expiry", 0),
                    # dominant per-segment STT/CTT overrides (blank = default)
                    "stt_etf": _segrate("etf", "stt_sell_pct"),
                    "stt_nse_fo": _segrate("nse_fo", "stt_sell_pct"),
                    "ctt_mcx_fo": _segrate("mcx_fo", "stt_sell_pct"),
                    "stt_nse_cm": _segrate("nse_cm", "stt_sell_pct"),
                },
                "execution": {
                    "product": ex.get("product", "NRML"),
                    "engine_mode": ex.get("engine_mode", "legacy"),
                    "slice_lots": ex.get("slice_lots", 0),
                    "cooldown_sec": ex.get("cooldown_sec", 300),
                    "use_limit_orders": bool(ex.get("use_limit_orders", True)),
                    "limit_offset_pct": ex.get("limit_offset_pct", 0.05),
                    "amend_step_pct": ex.get("amend_step_pct", 0.05),
                    "amend_interval_sec": ex.get("amend_interval_sec", 1.5),
                    "fill_timeout_sec": ex.get("fill_timeout_sec", 5),
                    "poll_interval_sec": ex.get("poll_interval_sec", 0.4),
                    "price_tick_size": ex.get("price_tick_size", 0.10),
                    "limit_to_market": bool(ex.get("limit_to_market", True)),
                    "unknown_status_grace_polls": ex.get("unknown_status_grace_polls", 3),
                    "assume_fill_on_unknown": bool(ex.get("assume_fill_on_unknown", False)),
                    "max_exit_failures": ex.get("max_exit_failures", 0),
                    "exit_retry_backoff_sec": ex.get("exit_retry_backoff_sec", 5),
                    "exit_retry_backoff_max_sec": ex.get("exit_retry_backoff_max_sec", 60),
                    "verify_flat_before_entry": bool(ex.get("verify_flat_before_entry", True)),
                    "verify_flat_fail_open": bool(ex.get("verify_flat_fail_open", False)),
                    "stop_cooldown_sec": ex.get("stop_cooldown_sec", 0),
                    "z_reset_after_stop": bool(ex.get("z_reset_after_stop", False)),
                    "sim_tick_size": ex.get("sim_tick_size", 0.10),
                    "sim_spread_ticks": ex.get("sim_spread_ticks", 2.0),
                    "sim_extra_slip_ticks": ex.get("sim_extra_slip_ticks", 0.0),
                    "sim_slow_prob": ex.get("sim_slow_prob", 0.25),
                    "sim_reject_prob": ex.get("sim_reject_prob", 0.0),
                    "sim_orphan_prob": ex.get("sim_orphan_prob", 0.0),
                    "sim_default_lot_size": ex.get("sim_default_lot_size", 75),
                },
                "broker": {
                    "persist_session": bool(bk.get("persist_session", True)),
                    "cache_ttl_sec": bk.get("cache_ttl_sec", 2),
                },
                "telegram": {
                    "enabled": bool(cfg.section("telegram").get("enabled", False)),
                    "chat_id": str(cfg.section("telegram").get("chat_id", "") or ""),
                    "notify_trades": bool(cfg.section("telegram").get("notify_trades", True)),
                    "notify_health": bool(cfg.section("telegram").get("notify_health", False)),
                    "notify_errors": bool(cfg.section("telegram").get("notify_errors", True)),
                    "token_set": bool(os.environ.get("ARROW_TELEGRAM_BOT_TOKEN", "")),
                },
                "exits": {
                    "dollar_stop_inr": xo.get("dollar_stop_inr", 0),
                    "profit_target_inr": xo.get("profit_target_inr", 0),
                    "max_hold_silent_when_losing": bool(xo.get("max_hold_silent_when_losing", False)),
                    "max_hold_z_progress_min": xo.get("max_hold_z_progress_min", 0),
                    "trailing_stop_pct": xo.get("trailing_stop_pct", 0),
                    "trailing_stop_floor_pct": xo.get("trailing_stop_floor_pct", 0),
                    "reversion_require_profit": bool(xo.get("reversion_require_profit", True)),
                    "reversion_gate_inr": xo.get("reversion_gate_inr", 0),
                    "profit_target_sigma_frac": xo.get("profit_target_sigma_frac", 0),
                    "tp_capital_pct": xo.get("tp_capital_pct", 0),
                    "cost_floor_mult": xo.get("cost_floor_mult", 0),
                    "stop_capital_pct": xo.get("stop_capital_pct", 0),
                    "stop_rr": xo.get("stop_rr", 0),
                    "z_stop_exit_enabled": bool(xo.get("z_stop_exit_enabled", True)),
                    "hard_time_stop_mult": xo.get("hard_time_stop_mult", 0),
                },
                "regime": {
                    "enabled": bool(rg.get("enabled", False)),
                    "halt_on_trending": bool(rg.get("halt_on_trending", True)),
                    "trend_direction_filter": bool(rg.get("trend_direction_filter", False)),
                    "efficiency_ratio_max": rg.get("efficiency_ratio_max", 0.6),
                    "min_zero_crossings": rg.get("min_zero_crossings", 4),
                    "window_samples": rg.get("window_samples", 120),
                    "vr_lag": rg.get("vr_lag", 5),
                },
                "reconcile": {
                    "enabled": bool(rc.get("enabled", False)),
                    "auto_close": bool(rc.get("auto_close", False)),
                    "mismatch_threshold": rc.get("mismatch_threshold", 3),
                    "interval_sec": rc.get("interval_sec", 20),
                },
                "signal": {
                    "window_minutes": s.get("window_minutes", 120),
                    "min_signal_minutes": s.get("min_signal_minutes", 10),
                    "sample_interval_sec": s.get("sample_interval_sec", 0.5),
                    "stats_update_interval_sec": s.get("stats_update_interval_sec", 0),
                    "hedge_ratio": s.get("hedge_ratio", 1.0),
                    "display_refresh_ms": s.get("display_refresh_ms", 500),
                    "persist_window": bool(s.get("persist_window", True)),
                    "resume_max_gap_min": s.get("resume_max_gap_min", 10),
                    "persist_interval_sec": s.get("persist_interval_sec", 30),
                    "min_hold_sec": s.get("min_hold_sec", 0),
                    "entry_zscore": s.get("entry_zscore", 2.0),
                    "exit_zscore": s.get("exit_zscore", 0.0),
                    "stop_zscore": s.get("stop_zscore", 4.0),
                    "confirmation_ticks": s.get("confirmation_ticks", 3),
                    "max_entry_z_divergence": s.get("max_entry_z_divergence", 0),
                    "max_entry_spread_divergence": s.get("max_entry_spread_divergence", 0),
                    "max_entry_zscore": s.get("max_entry_zscore", 0),
                },
                "pairs": {
                    "pair_type": pa.get("pair_type", "SPOT_FUTURE"),
                    "risk_free_rate": pa.get("risk_free_rate", 0.0425),
                    "sizing_mode": pa.get("sizing_mode", "lots"),
                    "hedge_mode": pa.get("hedge_mode", "units"),
                    "notional_per_leg_inr": pa.get("notional_per_leg_inr", 0),
                },
                "risk": {
                    "lots_per_trade": r.get("lots_per_trade", 1),
                    "max_contracts_per_leg": r.get("max_contracts_per_leg", 5),
                    "max_slippage_pct": r.get("max_slippage_pct", 0.5),
                    "max_daily_loss": r.get("max_daily_loss", 0),
                    "min_live_margin": r.get("min_live_margin", 0),
                    "loss_streak_reduce_at": r.get("loss_streak_reduce_at", 0),
                    "loss_streak_reduce_pct": r.get("loss_streak_reduce_pct", 20),
                    "loss_streak_pause_at": r.get("loss_streak_pause_at", 0),
                    "capital_at_risk_inr": r.get("capital_at_risk_inr", 0),
                },
                "trading_hours": {
                    "enabled": bool(th.get("enabled", False)),
                    "start_hour": th.get("start_hour", 9), "start_min": th.get("start_min", 15),
                    "end_hour": th.get("end_hour", 15), "end_min": th.get("end_min", 30),
                    "close_hour": th.get("close_hour", 15), "close_min": th.get("close_min", 30),
                    "no_entry_buffer_min": th.get("no_entry_buffer_min", 0),
                },
                "filters": {
                    "enable_probability_filter": bool(f.get("enable_probability_filter", True)),
                    "commission_basis": f.get("commission_basis", "per_lot"),
                    "brokerage_per_lot": f.get("brokerage_per_lot", 20),
                    "slippage_per_lot": f.get("slippage_per_lot", 5),
                    "stt_pct": f.get("stt_pct", 0.02),
                    "stt_a_pct": f.get("stt_a_pct"),        # None = fall back to stt_pct
                    "stt_b_pct": f.get("stt_b_pct"),
                    "other_cost_pct": f.get("other_cost_pct", 0.005),
                    "capital_gains_pct": f.get("capital_gains_pct", 0),
                    "min_edge_multiple": f.get("min_edge_multiple", 0),
                    "min_win_probability": f.get("min_win_probability", 0.60),
                    "min_expected_value": f.get("min_expected_value", 0),
                    "time_stop_half_lives": f.get("time_stop_half_lives", 3.0),
                    "half_life_min_sec": f.get("half_life_min_sec", 0),
                    "half_life_max_sec": f.get("half_life_max_sec", 0),
                },
                "mode": {"paper_trading": cfg.is_dry_run},
            })

        data = request.get_json(force=True) or {}

        def _num(v, d):
            try:
                return type(d)(v)
            except (ValueError, TypeError):
                return d

        raw = cfg.raw
        sig = raw.setdefault("signal", {})
        sd = data.get("signal") or {}
        if "persist_window" in sd:
            sig["persist_window"] = bool(sd["persist_window"])
        for k, d in (("window_minutes", 120.0), ("min_signal_minutes", 10.0),
                     ("sample_interval_sec", 0.5), ("display_refresh_ms", 500),
                     ("resume_max_gap_min", 10), ("persist_interval_sec", 30),
                     ("min_hold_sec", 0.0), ("hedge_ratio", 1.0),
                     ("stats_update_interval_sec", 0.0),
                     ("entry_zscore", 2.0), ("exit_zscore", 0.0), ("stop_zscore", 4.0),
                     ("confirmation_ticks", 3), ("max_entry_z_divergence", 0.0),
                     ("max_entry_spread_divergence", 0.0), ("max_entry_zscore", 0.0)):
            if k in sd:
                sig[k] = _num(sd[k], d)

        rk = raw.setdefault("risk", {})
        for k, d in (("lots_per_trade", 1), ("max_contracts_per_leg", 5),
                     ("max_slippage_pct", 0.5), ("max_daily_loss", 0),
                     ("min_live_margin", 0), ("loss_streak_reduce_at", 0),
                     ("loss_streak_reduce_pct", 20), ("loss_streak_pause_at", 0),
                     ("capital_at_risk_inr", 0)):
            if k in (data.get("risk") or {}):
                rk[k] = _num(data["risk"][k], d)

        th = raw.setdefault("trading_hours", {})
        td = data.get("trading_hours") or {}
        if "enabled" in td:
            th["enabled"] = bool(td["enabled"])
        for k, d in (("start_hour", 9), ("start_min", 15), ("end_hour", 15), ("end_min", 30),
                     ("close_hour", 15), ("close_min", 30), ("no_entry_buffer_min", 0)):
            if k in td:
                th[k] = _num(td[k], d)

        fl = raw.setdefault("filters", {})
        fd = data.get("filters") or {}
        if "enable_probability_filter" in fd:
            fl["enable_probability_filter"] = bool(fd["enable_probability_filter"])
        if "commission_basis" in fd:
            fl["commission_basis"] = "per_order" if fd["commission_basis"] == "per_order" else "per_lot"
        # Per-leg STT: empty/None clears the override (falls back to stt_pct).
        for k in ("stt_a_pct", "stt_b_pct"):
            if k in fd:
                v = fd[k]
                if v is None or v == "":
                    fl.pop(k, None)
                else:
                    fl[k] = _num(v, 0.0)
        for k, d in (("brokerage_per_lot", 20.0), ("slippage_per_lot", 5.0),
                     ("stt_pct", 0.02), ("other_cost_pct", 0.005),
                     ("capital_gains_pct", 0.0), ("min_edge_multiple", 0.0),
                     ("min_win_probability", 0.60), ("min_expected_value", 0.0),
                     ("time_stop_half_lives", 3.0), ("half_life_min_sec", 0.0),
                     ("half_life_max_sec", 0.0)):
            if k in fd:
                fl[k] = _num(fd[k], d)

        ex = raw.setdefault("execution", {})
        ed = data.get("execution") or {}
        for k in ("z_reset_after_stop", "use_limit_orders", "limit_to_market",
                  "assume_fill_on_unknown", "verify_flat_before_entry",
                  "verify_flat_fail_open"):
            if k in ed:
                ex[k] = bool(ed[k])
        if "product" in ed:
            ex["product"] = str(ed["product"] or "NRML")
        if "engine_mode" in ed:
            em = str(ed["engine_mode"] or "legacy").lower()
            ex["engine_mode"] = em if em in ("legacy", "clip") else "legacy"
        if "slice_lots" in ed:
            ex["slice_lots"] = _num(ed["slice_lots"], 0)
        for k, d in (("limit_offset_pct", 0.05), ("amend_step_pct", 0.05),
                     ("amend_interval_sec", 1.5), ("fill_timeout_sec", 5.0),
                     ("poll_interval_sec", 0.4), ("price_tick_size", 0.10),
                     ("unknown_status_grace_polls", 3), ("cooldown_sec", 300),
                     ("exit_retry_backoff_sec", 5), ("exit_retry_backoff_max_sec", 60),
                     ("max_exit_failures", 0), ("stop_cooldown_sec", 0.0),
                     ("sim_tick_size", 0.10), ("sim_spread_ticks", 2.0),
                     ("sim_extra_slip_ticks", 0.0), ("sim_slow_prob", 0.25),
                     ("sim_reject_prob", 0.0), ("sim_orphan_prob", 0.0),
                     ("sim_default_lot_size", 75)):
            if k in ed:
                ex[k] = _num(ed[k], d)

        xo = raw.setdefault("exits", {})
        xd = data.get("exits") or {}
        if "max_hold_silent_when_losing" in xd:
            xo["max_hold_silent_when_losing"] = bool(xd["max_hold_silent_when_losing"])
        if "reversion_require_profit" in xd:
            xo["reversion_require_profit"] = bool(xd["reversion_require_profit"])
        if "z_stop_exit_enabled" in xd:
            xo["z_stop_exit_enabled"] = bool(xd["z_stop_exit_enabled"])
        for k, d in (("dollar_stop_inr", 0.0), ("profit_target_inr", 0.0),
                     ("max_hold_z_progress_min", 0.0), ("trailing_stop_pct", 0.0),
                     ("trailing_stop_floor_pct", 0.0), ("reversion_gate_inr", 0.0),
                     ("profit_target_sigma_frac", 0.0), ("tp_capital_pct", 0.0),
                     ("cost_floor_mult", 0.0), ("stop_capital_pct", 0.0), ("stop_rr", 0.0),
                     ("hard_time_stop_mult", 0.0)):
            if k in xd:
                xo[k] = _num(xd[k], d)

        rg = raw.setdefault("regime", {})
        rgd = data.get("regime") or {}
        for k in ("enabled", "halt_on_trending", "trend_direction_filter"):
            if k in rgd:
                rg[k] = bool(rgd[k])
        for k, d in (("efficiency_ratio_max", 0.6), ("min_zero_crossings", 4),
                     ("window_samples", 120), ("vr_lag", 5)):
            if k in rgd:
                rg[k] = _num(rgd[k], d)

        rc = raw.setdefault("reconcile", {})
        rcd = data.get("reconcile") or {}
        for k in ("enabled", "auto_close"):
            if k in rcd:
                rc[k] = bool(rcd[k])
        for k, d in (("mismatch_threshold", 3), ("interval_sec", 20)):
            if k in rcd:
                rc[k] = _num(rcd[k], d)

        bk = raw.setdefault("broker", {})
        bkd = data.get("broker") or {}
        if "persist_session" in bkd:
            bk["persist_session"] = bool(bkd["persist_session"])
        if "cache_ttl_sec" in bkd:
            bk["cache_ttl_sec"] = _num(bkd["cache_ttl_sec"], 2)

        co = raw.setdefault("costs", {})
        cod = data.get("costs") or {}
        if "use_segment_costs" in cod:
            co["use_segment_costs"] = bool(cod["use_segment_costs"])
        for k, d in (("gst_pct", 18.0), ("no_entry_days_before_expiry", 0)):
            if k in cod:
                co[k] = _num(cod[k], d)
        # Dominant per-segment STT/CTT overrides → costs.segments.{seg}.stt_sell_pct.
        # Blank/None clears the override (falls back to the core/costs.py default).
        _seg = co.setdefault("segments", {})
        for field, seg in (("stt_etf", "etf"), ("stt_nse_fo", "nse_fo"),
                           ("ctt_mcx_fo", "mcx_fo"), ("stt_nse_cm", "nse_cm")):
            if field in cod:
                v = cod[field]
                if v is None or v == "":
                    (_seg.get(seg) or {}).pop("stt_sell_pct", None)
                    if seg in _seg and not _seg[seg]:
                        _seg.pop(seg, None)
                else:
                    _seg.setdefault(seg, {})["stt_sell_pct"] = _num(v, 0.0)

        pa = raw.setdefault("pairs", {})
        pad = data.get("pairs") or {}
        if "pair_type" in pad:
            pt = str(pad["pair_type"] or "SPOT_FUTURE").upper()
            pa["pair_type"] = pt if pt in ("SPOT_FUTURE", "FUTURE_FUTURE", "RELATED") else "SPOT_FUTURE"
        for k in ("sizing_mode", "hedge_mode"):
            if k in pad:
                pa[k] = str(pad[k] or "")
        for k, d in (("risk_free_rate", 0.0425), ("notional_per_leg_inr", 0.0)):
            if k in pad:
                pa[k] = _num(pad[k], d)

        tg = raw.setdefault("telegram", {})
        tgd = data.get("telegram") or {}
        for k in ("enabled", "notify_trades", "notify_health", "notify_errors"):
            if k in tgd:
                tg[k] = bool(tgd[k])
        if "chat_id" in tgd:
            tg["chat_id"] = str(tgd["chat_id"] or "")        # non-secret; token stays in env

        md = data.get("mode") or {}
        if "paper_trading" in md:
            raw["mode"] = "dry_run" if md["paper_trading"] else "live"

        cfg.save()
        # keep the running lot size in sync with lots_per_trade
        _algo_lots["lots"] = int(rk.get("lots_per_trade", _algo_lots["lots"]))
        logger.info("Settings saved")
        return jsonify({"success": True})

    @app.route("/api/hedge-ratio/derive", methods=["GET"])
    def api_hedge_ratio_derive():
        """Suggest the hedge ratio k = leg_b_price ÷ leg_a_price from live prices,
        so the ETF/near leg is scaled to the future/contract leg. The UI fills the
        field with this; the operator can accept or pin their own."""
        la, lb = _leg_prices()
        if not la or not lb or la <= 0:
            return jsonify({"ok": False, "error": "live prices for both legs unavailable"})
        return jsonify({"ok": True, "hedge_ratio": round(float(lb) / float(la), 4),
                        "leg_a": la, "leg_b": lb})

    # ── signal quality (win-prob / EV / breakeven / half-life gate) ──────────
    @app.route("/api/signal/quality", methods=["GET"])
    def api_signal_quality():
        sig = signal_engine.get_signal()
        f = cfg.section("filters")
        z = sig.get("zscore")
        std = sig.get("std")
        hl_sec = sig.get("half_life_sec", 0.0) or 0.0
        out = {"verdict": "WAITING", "ready": bool(sig.get("ready")),
               "win_probability": None, "expected_value": None, "breakeven_z": None,
               "round_trip_cost": None, "half_life_sec": hl_sec,
               "allowed_min_sec": float(f.get("half_life_min_sec", 0) or 0),
               "allowed_max_sec": float(f.get("half_life_max_sec", 0) or 0),
               "time_stop_sec": round(float(f.get("time_stop_half_lives", 3.0)) * hl_sec, 1),
               "entry_zscore": sig.get("entry_zscore", 2.0)}
        if z is None or std is None or not sig.get("ready"):
            return jsonify(out)
        pf = ProbabilityFilter(
            commission_per_lot=float(f.get("brokerage_per_lot", 20)) * 2.0,
            slippage_per_lot=float(f.get("slippage_per_lot", 5)) * 2.0,
            commission_basis=str(f.get("commission_basis", "per_lot")),
            lot_multiplier=_lot_multiplier(),
            min_win_probability=float(f.get("min_win_probability", 0.60)),
            min_expected_value=float(f.get("min_expected_value", 0)),
            exit_zscore=float(cfg.get("signal.exit_zscore", 0.0)),
            stop_zscore=float(cfg.get("signal.stop_zscore", 4.0)),
            enabled=bool(f.get("enable_probability_filter", True)),
        )
        m = pf._compute_metrics(z, std, int(_algo_lots["lots"]))
        out.update(win_probability=round(m["win_probability"], 4),
                   expected_value=round(m["expected_value"], 2),
                   breakeven_z=round(m["breakeven_z"], 3),
                   round_trip_cost=round(m["round_trip_cost"], 2))
        entry_z = float(sig.get("entry_zscore", 2.0))
        if abs(z) < entry_z:
            out["verdict"] = "WATCHING"
        else:
            allow, _reason, _m = pf.check_entry(z, std, hl_sec, int(_algo_lots["lots"]))
            out["verdict"] = "ALLOW" if allow else "BLOCKED"
        return jsonify(out)

    # ── risk metrics (leg notionals + max daily loss) ────────────────────────
    @app.route("/api/risk", methods=["GET"])
    def api_risk():
        mdl = float(cfg.get("risk.max_daily_loss", 0) or 0)
        day = trade_log.day_pnl()
        out = {"leg_a_notional": 0.0, "leg_b_notional": 0.0,
               "max_daily_loss": mdl, "day_pnl": day,
               "daily_loss_hit": bool(mdl > 0 and day <= -mdl)}
        broker = active.get()
        legs = _read_legs()
        if not broker or not _have_both_legs(legs):
            return jsonify(out)
        syms = {lk: legs[lk]["symbol"] for lk in ("leg_a", "leg_b")}
        prices = {}
        try:
            prices = broker.get_streamed_ltp(list(syms.values())) or {}
        except Exception:
            prices = {}
        positions = {}
        try:
            positions = {str(p.get("symbol", "")).upper(): p for p in (_positions_cached() or [])}
        except Exception:
            positions = {}
        for lk, sym in syms.items():
            ltp = prices.get(sym.upper()) or 0.0
            p = positions.get(sym.upper())
            qty = abs(int(p.get("net_quantity") or 0)) if p else 0
            if qty == 0:
                # No position → show prospective notional for the configured lots.
                try:
                    qty = broker.resolve_lot_size(legs[lk]["segment"], sym) * int(_algo_lots["lots"])
                except Exception:
                    qty = 0
            out[f"{lk}_notional"] = round(float(ltp) * qty, 2)
        return jsonify(out)

    # ── trades table ─────────────────────────────────────────────────────────
    @app.route("/api/funds", methods=["GET"])
    def api_funds():
        """Funds / margin / notional for the dashboard's Funds & Margin card.
        Margin utilized, available and equity come straight from the broker
        (Arrow sets the real SPAN+exposure margin); notional comes from the open
        exchange position, or a prospective estimate (LTP × lot × lots) when flat."""
        out = {"connected": False, "currency": "INR",
               "available": None, "used": None, "equity": None, "cash": None,
               "margin_ratio": None, "lots": int(_algo_lots["lots"]),
               "leg_a_notional": 0.0, "leg_b_notional": 0.0, "notional": 0.0,
               "notional_prospective": True}
        broker = active.get()
        if not broker:
            return jsonify(out)
        out["connected"] = True
        try:
            f = _funds_cached()
        except Exception:
            f = {}
        for k in ("available", "used", "equity", "cash"):
            out[k] = f.get(k)
        # Surface the raw broker payload so a funds field-name mismatch is
        # diagnosable from /api/funds (Arrow's keys vary by build).
        out["raw"] = f.get("raw")
        if out["used"] is not None and out["equity"]:
            try:
                out["margin_ratio"] = round(100.0 * out["used"] / out["equity"], 2)
            except ZeroDivisionError:
                pass
        legs = _read_legs()
        if _have_both_legs(legs):
            positions = {}
            try:
                positions = {str(p.get("symbol", "")).upper(): p
                             for p in (_positions_cached() or [])}
            except Exception:
                positions = {}
            la, lb = _leg_prices()
            any_open = False
            for lk, ltp in (("leg_a", la), ("leg_b", lb)):
                sym = legs[lk]["symbol"]
                p = positions.get(sym.upper())
                qty = abs(int(p.get("net_quantity") or 0)) if p else 0
                if qty:
                    any_open = True
                    price = float(p.get("ltp") or ltp or 0)
                else:                      # flat → prospective size for the configured lots
                    try:
                        qty = int(broker.resolve_lot_size(legs[lk]["segment"], sym)) * int(_algo_lots["lots"])
                    except Exception:
                        qty = 0
                    price = float(ltp or 0)
                out[f"{lk}_notional"] = round(price * qty, 2)
            out["notional"] = round(out["leg_a_notional"] + out["leg_b_notional"], 2)
            out["notional_prospective"] = not any_open
        return jsonify(out)

    @app.route("/api/trades", methods=["GET"])
    def api_trades():
        return jsonify({"trades": trade_log.all(), "stats": trade_log.stats()})

    @app.route("/api/trades/journal", methods=["GET"])
    def api_trades_journal():
        """Round-trip trades with full entry/exit detail for the Trade Journal."""
        return jsonify(trade_log.round_trips())

    @app.route("/api/expectancy", methods=["GET"])
    def api_expectancy():
        """The book on one sheet, in R: win rate, R:R, PF, break-even WR, EV/R."""
        return jsonify(trade_log.expectancy())

    @app.route("/api/drawdown", methods=["GET"])
    def api_drawdown():
        """Equity-curve drawdown tiles + per-trade adverse-excursion (MAE/MFE)
        for the Analysis page (Phase-8 merge). Computed from closed trades."""
        rows = [t for t in trade_log.all()
                if str(t.get("action")) == "CLOSE" and t.get("net_pnl") is not None]
        return jsonify({
            "drawdown": performance.drawdown_block(rows, newest_first=False),
            "excursion": performance.excursion_rows(rows),
        })

    @app.route("/api/calibration", methods=["GET"])
    def api_calibration():
        """Take/hold calibration from measured lifecycle extremes — the peak
        distribution and data-driven take-profit / max-hold suggestions."""
        return jsonify(trade_log.take_hold_calibration())

    @app.route("/api/backtest", methods=["POST"])
    def api_backtest():
        """Replay the CURRENT collected signal window (real data) through the live
        strategy + Indian cost model, and return the metrics + expectancy sheet.
        No fabricated data — only what's actually been collected this session."""
        from arrow_statarb.core.backtest import Backtester
        bars = signal_engine.export_bars()
        if len(bars) < 2:
            return jsonify({"error": "not enough collected data yet — let the "
                                     "signal window fill first"})
        sp, al, f = _signal_params(), _algo_params(), cfg.section("filters")
        bt = Backtester(
            signal_params=sp, strategy_params=al,
            lot_size=int(_lot_multiplier() or 1),
            brokerage_per_lot=float(f.get("brokerage_per_lot", 20)),
            slippage_per_lot=float(f.get("slippage_per_lot", 5)),
            lots=int(_algo_lots["lots"]),
            capital=float(cfg.get("risk.capital_at_risk_inr", 0) or 0) or None,
            hedge_ratio=float(cfg.get("signal.hedge_ratio", 1) or 1),
            stt_pct=float(f.get("stt_pct", 0.02) or 0),
            other_cost_pct=float(f.get("other_cost_pct", 0) or 0),
            capital_gains_pct=float(f.get("capital_gains_pct", 0) or 0),
            stt_a_pct=(float(f["stt_a_pct"]) if f.get("stt_a_pct") is not None else None),
            stt_b_pct=(float(f["stt_b_pct"]) if f.get("stt_b_pct") is not None else None))
        return jsonify(bt.run(bars))

    @app.route("/api/trades/clear", methods=["POST"])
    def api_trades_clear():
        trade_log.clear()
        return jsonify({"success": True})

    # ── markets bar (session clock) ──────────────────────────────────────────
    @app.route("/api/markets", methods=["GET"])
    def api_markets():
        now = datetime.now(_IST)
        ist = now.strftime("%H:%M IST")
        legs = _read_legs()
        pair = ""
        if _have_both_legs(legs):
            pair = f"{legs['leg_a']['symbol']} ↔ {legs['leg_b']['symbol']}"
        # Session windows differ by venue: NSE/BSE equity & F&O run 09:15–15:30;
        # MCX commodities run ~09:00–23:30 IST (evening session). Flag each
        # segment against its own hours so the bar isn't wrong after 15:30.
        hm = (now.hour, now.minute)
        nse_open = (9, 15) <= hm <= (15, 30)
        mcx_open = (9, 0) <= hm <= (23, 30)
        segs = ([{"name": "MCX", "time": ist, "open": mcx_open}]
                + [{"name": n, "time": ist, "open": nse_open}
                   for n in ("NSE F&O", "NSE Cash", "BSE")])
        return jsonify({"segments": segs, "pair": pair})

    # ── system tests ─────────────────────────────────────────────────────────
    @app.route("/api/run-tests", methods=["POST"])
    def api_run_tests():
        try:
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                                  cwd=str(PROJECT_ROOT), capture_output=True,
                                  text=True, timeout=300)
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
            return jsonify({"success": proc.returncode == 0,
                            "returncode": proc.returncode, "output": tail})
        except Exception as exc:
            return jsonify({"success": False, "output": str(exc)}), 500

    # ── socketio ─────────────────────────────────────────────────────────────
    @socketio.on("connect")
    def _on_connect():
        emit("status", {"connected": active.get() is not None})

    # Expose internals for tests (inject a fake broker, inspect the engines).
    app.extensions["arrow"] = {"active": active, "signal": signal_engine, "algo": arrow_algo}

    return app, socketio
