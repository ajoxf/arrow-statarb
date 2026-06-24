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
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
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
                                "ratio": float(entry.get("ratio", 1) or 1)}
        if "leg_a" in legs and "leg_b" in legs:
            return legs
        # Fall back to config defaults
        for lk in ("leg_a", "leg_b"):
            d = cfg.get(f"instruments.{lk}") or {}
            if d.get("segment") and d.get("symbol"):
                legs[lk] = {"segment": str(d["segment"]).strip(),
                            "symbol": str(d["symbol"]).strip(),
                            "ratio": float(d.get("ratio", 1) or 1)}
        return legs

    def _have_both_legs(legs: Dict) -> bool:
        return "leg_a" in legs and "leg_b" in legs

    # ── shared order path (manual + algo) ────────────────────────────────────
    def _order_legs(direction: str, lots: int):
        """Return [(seg, sym, side, lots_for_leg), ...] for a direction."""
        legs = _read_legs()
        if not _have_both_legs(legs):
            raise ValueError("Both legs must be assigned in Setup")

        def _leg(lk):
            seg = legs[lk]["segment"]
            sym = legs[lk]["symbol"]
            qty = max(1, round(lots * legs[lk]["ratio"]))
            return seg, sym, qty

        sa, ya, qa = _leg("leg_a")
        sb, yb, qb = _leg("leg_b")
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
            meta["lot_size"] = int(broker.resolve_lot_size(
                legs["leg_a"]["segment"], legs["leg_a"]["symbol"]))
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
            meta["spread"] = round(a - b, 4)
        meta["zscore"] = signal_engine.get_signal().get("zscore")
        return meta

    def _spread_execute(direction: str, lots: int, source: str = "manual",
                        z: Optional[float] = None, spread: Optional[float] = None) -> Dict:
        mode = _mode()
        if mode != "live_sim" and not active.get():
            return {"success": False, "error": "No broker connected"}
        try:
            legs = _order_legs(direction, lots)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        res = _spread_order(legs, "Order", verify_flat=True)
        if res.get("success"):
            m = _trade_meta(res)
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
                             name=m["name"])
        return res

    def _spread_close(direction: str, lots: int, source: str = "manual",
                      reason: str = "", z: Optional[float] = None,
                      spread: Optional[float] = None) -> Dict:
        mode = _mode()
        if mode != "live_sim" and not active.get():
            return {"success": False, "error": "No broker connected"}
        close_dir = "SHORT_SPREAD" if direction == "LONG_SPREAD" else "LONG_SPREAD"
        try:
            legs = _order_legs(close_dir, lots)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        res = _spread_order(legs, "Close")
        if res.get("success"):
            m = _trade_meta(res)
            trade_log.record(action="CLOSE", direction=direction, lots=lots,
                             spread=m["spread"],  # actual fill spread — see OPEN note
                             decision_spread=(spread if spread is not None else _current_spread()),
                             dry_run=(mode != "live"),
                             status=_MODE_STATUS.get(mode, "DRY-RUN"), source=source,
                             lot_size=m["lot_size"],
                             zscore=(z if z is not None else m["zscore"]),
                             leg_a_price=m["leg_a_price"], leg_b_price=m["leg_b_price"],
                             name=m["name"], exit_reason=reason)
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
        return {
            "window_minutes": float(s.get("window_minutes", 120)),
            "sample_interval_sec": float(s.get("sample_interval_sec", 0.5)),
            "min_signal_minutes": float(s.get("min_signal_minutes", 10)),
            "entry_zscore": float(s.get("entry_zscore", 2.0)),
            "exit_zscore": float(s.get("exit_zscore", 0.0)),
            "stop_zscore": float(s.get("stop_zscore", 4.0)),
        }

    signal_engine = SignalEngine(prices_provider=_leg_prices, params_provider=_signal_params)
    # Auto-start so the live signal + z-score chart always collect whenever prices
    # are available — independent of connecting the broker or arming the algo. It
    # simply no-ops while no prices are returned, so this is safe at startup.
    signal_engine.start()

    _algo_lots = {"lots": int(cfg.get("execution.default_lots", 1))}

    def _lot_multiplier() -> float:
        """Lot size (units per lot) of leg A — turns σ (₹/unit) into ₹ per lot
        for the EV gate. 1 if the broker/master isn't ready yet."""
        broker = active.get()
        legs = _read_legs()
        if not broker or "leg_a" not in legs:
            return 1.0
        try:
            return float(broker.resolve_lot_size(legs["leg_a"]["segment"], legs["leg_a"]["symbol"]))
        except Exception:
            return 1.0

    def _algo_params() -> Dict:
        s = cfg.section("signal")
        f = cfg.section("filters")
        r = cfg.section("risk")
        cap = int(r.get("max_contracts_per_leg", 0) or 0)
        lots = int(_algo_lots["lots"])
        if cap > 0:
            lots = min(lots, cap)            # hard position cap per leg
        return {
            "entry_zscore": float(s.get("entry_zscore", 2.0)),
            "exit_zscore": float(s.get("exit_zscore", 0.0)),
            "stop_zscore": float(s.get("stop_zscore", 4.0)),
            "confirmation_ticks": int(s.get("confirmation_ticks", 1)),
            "max_entry_z_divergence": float(s.get("max_entry_z_divergence", 0) or 0),
            "max_entry_zscore": float(s.get("max_entry_zscore", 0) or 0),
            "tick_interval": float(s.get("sample_interval_sec", 0.5)),
            "cooldown": float(cfg.get("execution.cooldown_sec", 300)),
            "max_exit_failures": int(cfg.get("execution.max_exit_failures", 0) or 0),
            "exit_retry_backoff": float(cfg.get("execution.exit_retry_backoff_sec", 0) or 0),
            "exit_retry_backoff_max": float(cfg.get("execution.exit_retry_backoff_max_sec", 60) or 60),
            "lots": lots,
            "lot_multiplier": _lot_multiplier(),
            "max_daily_loss": float(r.get("max_daily_loss", 0) or 0),
            "day_pnl": trade_log.day_pnl(),
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
        for lk in ("leg_a", "leg_b"):
            if lk not in legs:
                continue
            try:
                ls = int(broker.resolve_lot_size(legs[lk]["segment"], legs[lk]["symbol"]))
            except Exception:
                ls = 0
            out["legs"][lk] = {"symbol": legs[lk]["symbol"], "lot_size": ls}
            lots[lk] = ls
        la, lb = lots.get("leg_a"), lots.get("leg_b")
        if la and lb and la != lb:
            out["lot_mismatch"] = True
            out["message"] = (f"⚠ Lot sizes differ — Leg A {la} vs Leg B {lb}. "
                              f"1 lot each is NOT share-neutral ({la} vs {lb} units). "
                              f"Use same-expiry-tier legs or adjust ratios.")
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
            return jsonify({
                "signal": {
                    "window_minutes": s.get("window_minutes", 120),
                    "min_signal_minutes": s.get("min_signal_minutes", 10),
                    "sample_interval_sec": s.get("sample_interval_sec", 0.5),
                    "entry_zscore": s.get("entry_zscore", 2.0),
                    "exit_zscore": s.get("exit_zscore", 0.0),
                    "stop_zscore": s.get("stop_zscore", 4.0),
                    "confirmation_ticks": s.get("confirmation_ticks", 3),
                    "max_entry_z_divergence": s.get("max_entry_z_divergence", 0),
                    "max_entry_zscore": s.get("max_entry_zscore", 0),
                },
                "execution": {
                    "max_exit_failures": cfg.section("execution").get("max_exit_failures", 0),
                },
                "risk": {
                    "lots_per_trade": r.get("lots_per_trade", 1),
                    "max_contracts_per_leg": r.get("max_contracts_per_leg", 5),
                    "max_slippage_pct": r.get("max_slippage_pct", 0.5),
                    "max_daily_loss": r.get("max_daily_loss", 0),
                    "min_live_margin": r.get("min_live_margin", 0),
                },
                "trading_hours": {
                    "enabled": bool(th.get("enabled", False)),
                    "start_hour": th.get("start_hour", 9), "start_min": th.get("start_min", 15),
                    "end_hour": th.get("end_hour", 15), "end_min": th.get("end_min", 30),
                },
                "filters": {
                    "enable_probability_filter": bool(f.get("enable_probability_filter", True)),
                    "commission_basis": f.get("commission_basis", "per_lot"),
                    "brokerage_per_lot": f.get("brokerage_per_lot", 20),
                    "slippage_per_lot": f.get("slippage_per_lot", 5),
                    "min_win_probability": f.get("min_win_probability", 0.60),
                    "min_expected_value": f.get("min_expected_value", 0),
                    "time_stop_half_lives": f.get("time_stop_half_lives", 3.0),
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
        for k, d in (("window_minutes", 120.0), ("min_signal_minutes", 10.0),
                     ("sample_interval_sec", 0.5),
                     ("entry_zscore", 2.0), ("exit_zscore", 0.0), ("stop_zscore", 4.0),
                     ("confirmation_ticks", 3), ("max_entry_z_divergence", 0.0),
                     ("max_entry_zscore", 0.0)):
            if k in (data.get("signal") or {}):
                sig[k] = _num(data["signal"][k], d)

        ex = raw.setdefault("execution", {})
        if "max_exit_failures" in (data.get("execution") or {}):
            ex["max_exit_failures"] = _num(data["execution"]["max_exit_failures"], 0)

        rk = raw.setdefault("risk", {})
        for k, d in (("lots_per_trade", 1), ("max_contracts_per_leg", 5),
                     ("max_slippage_pct", 0.5), ("max_daily_loss", 0),
                     ("min_live_margin", 0)):
            if k in (data.get("risk") or {}):
                rk[k] = _num(data["risk"][k], d)

        th = raw.setdefault("trading_hours", {})
        td = data.get("trading_hours") or {}
        if "enabled" in td:
            th["enabled"] = bool(td["enabled"])
        for k, d in (("start_hour", 9), ("start_min", 15), ("end_hour", 15), ("end_min", 30)):
            if k in td:
                th[k] = _num(td[k], d)

        fl = raw.setdefault("filters", {})
        fd = data.get("filters") or {}
        if "enable_probability_filter" in fd:
            fl["enable_probability_filter"] = bool(fd["enable_probability_filter"])
        if "commission_basis" in fd:
            fl["commission_basis"] = "per_order" if fd["commission_basis"] == "per_order" else "per_lot"
        for k, d in (("brokerage_per_lot", 20.0), ("slippage_per_lot", 5.0),
                     ("min_win_probability", 0.60), ("min_expected_value", 0.0),
                     ("time_stop_half_lives", 3.0)):
            if k in fd:
                fl[k] = _num(fd[k], d)

        md = data.get("mode") or {}
        if "paper_trading" in md:
            raw["mode"] = "dry_run" if md["paper_trading"] else "live"

        cfg.save()
        # keep the running lot size in sync with lots_per_trade
        _algo_lots["lots"] = int(rk.get("lots_per_trade", _algo_lots["lots"]))
        logger.info("Settings saved")
        return jsonify({"success": True})

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
        # Equity/F&O regular session 09:15–15:30 IST.
        open_now = (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) <= (15, 30)
        segs = [{"name": n, "time": ist, "open": open_now}
                for n in ("MCX", "NSE F&O", "NSE Cash", "BSE")]
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
