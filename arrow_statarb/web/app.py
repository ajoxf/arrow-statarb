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

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from loguru import logger

from arrow_statarb.config.config import Config, CONFIG_DIR, LEG_ASSIGNMENTS_FILE
from arrow_statarb.brokers.registry import ActiveBroker, create_broker
from arrow_statarb.core.signal import SignalEngine
from arrow_statarb.core.algo import ArrowAutoTrader


def create_app(config: Optional[Config] = None) -> Tuple[Flask, SocketIO]:
    cfg = config or Config()
    templates_dir = Path(__file__).resolve().parent / "templates"
    app = Flask(__name__, template_folder=str(templates_dir))
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    active = ActiveBroker()
    _ltp_cache: Dict[str, Any] = {}

    # ── config helpers ───────────────────────────────────────────────────────
    def _is_dry_run() -> bool:
        try:
            cfg.reload()
        except Exception:
            return True
        return cfg.is_dry_run

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

    def _spread_order(legs, dry_run: bool, label: str) -> Dict:
        """Place each leg in parallel through the active broker (lot-multiplied,
        segment-aware, mpp market). Simulated when dry_run. Shared by manual
        endpoints AND the auto-trader."""
        broker = active.get()
        if not broker:
            return {"success": False, "error": "No broker connected"}

        def _submit_leg(leg):
            seg, sym, side, qty = leg
            lot_size = broker.resolve_lot_size(seg, sym)
            actual_qty = qty * lot_size
            if dry_run:
                logger.info("[DRY-RUN] {}: would {} {}lot(s)×{}={} {}/{} — NOT transmitted",
                            label, side, qty, lot_size, actual_qty, sym, seg)
                return {"order_id": f"DRYRUN-{side}-{sym}", "status": "submitted",
                        "symbol": sym, "side": side, "quantity": actual_qty, "dry_run": True}
            tok = broker.resolve_token(seg, sym)
            res = broker.submit_order(symbol=sym, side=side, quantity=actual_qty,
                                      order_type="market", exchange_segment=seg,
                                      product=str(cfg.get("execution.product", "NRML")), token=tok)
            if res.get("status") == "error":
                logger.error("{}: {} {}×{}={} {}/{} FAILED — {}",
                             label, side, qty, lot_size, actual_qty, sym, seg, res.get("message"))
            else:
                logger.info("{}: {} {}×{}={} {}/{} → {} (id={})",
                            label, side, qty, lot_size, actual_qty, sym, seg,
                            res.get("status"), res.get("order_id"))
            return res

        with ThreadPoolExecutor(max_workers=len(legs)) as ex:
            results = [f.result() for f in [ex.submit(_submit_leg, leg) for leg in legs]]
        errors = [r for r in results if r.get("status") == "error"]
        if errors:
            return {"success": False,
                    "error": "; ".join(r.get("message", "order rejected") for r in errors),
                    "results": results, "dry_run": dry_run}
        ids = [r.get("order_id", "?") for r in results]
        verb = "Simulated" if dry_run else "Placed"
        tag = "DRY-RUN" if dry_run else "LIVE"
        return {"success": True, "message": f"[{tag}] {verb}: {', '.join(ids)}",
                "results": results, "dry_run": dry_run}

    def _spread_execute(direction: str, lots: int) -> Dict:
        if not active.get():
            return {"success": False, "error": "No broker connected"}
        try:
            legs = _order_legs(direction, lots)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _spread_order(legs, _is_dry_run(), "Order")

    def _spread_close(direction: str, lots: int) -> Dict:
        if not active.get():
            return {"success": False, "error": "No broker connected"}
        close_dir = "SHORT_SPREAD" if direction == "LONG_SPREAD" else "LONG_SPREAD"
        try:
            legs = _order_legs(close_dir, lots)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _spread_order(legs, _is_dry_run(), "Close")

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
        return {
            "entry_zscore": float(s.get("entry_zscore", 2.0)),
            "exit_zscore": float(s.get("exit_zscore", 0.0)),
            "stop_zscore": float(s.get("stop_zscore", 4.0)),
            "tick_interval": float(s.get("sample_interval_sec", 0.5)),
            "cooldown": float(cfg.get("execution.cooldown_sec", 300)),
            "lots": int(_algo_lots["lots"]),
            "lot_multiplier": _lot_multiplier(),
            "enable_probability_filter": bool(f.get("enable_probability_filter", True)),
            "min_win_probability": float(f.get("min_win_probability", 0.60)),
            "min_expected_value": float(f.get("min_expected_value", 0.0)),
            "brokerage_per_lot": float(f.get("brokerage_per_lot", 10.0)),
            "slippage_per_lot": float(f.get("slippage_per_lot", 5.0)),
            "time_stop_half_lives": float(f.get("time_stop_half_lives", 3.0)),
        }

    arrow_algo = ArrowAutoTrader(
        signal_provider=signal_engine.get_signal,
        params_provider=_algo_params,
        execute_fn=_spread_execute,
        close_fn=_spread_close,
    )

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
        try:
            broker = create_broker(cfg.get("broker.name", "arrow"), creds)
            ok = broker.connect()
            if ok:
                active.set(broker)
                signal_engine.reset()
                signal_engine.start()
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
            positions = broker.get_positions() or []
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

    # ── mode (dry-run / live) ────────────────────────────────────────────────
    @app.route("/api/config", methods=["GET"])
    def api_config():
        return jsonify({"mode": cfg.mode, "dry_run": _is_dry_run(),
                        "broker": cfg.get("broker.name", "arrow")})

    @app.route("/api/trading-mode", methods=["POST"])
    def api_trading_mode():
        data = request.get_json(force=True) or {}
        mode = "live" if str(data.get("mode", "")).lower() == "live" else "dry_run"
        cfg.set_mode(mode)
        logger.warning("Trading mode set to {}", mode.upper())
        return jsonify({"success": True, "mode": mode, "dry_run": _is_dry_run()})

    # ── socketio ─────────────────────────────────────────────────────────────
    @socketio.on("connect")
    def _on_connect():
        emit("status", {"connected": active.get() is not None})

    # Expose internals for tests (inject a fake broker, inspect the engines).
    app.extensions["arrow"] = {"active": active, "signal": signal_engine, "algo": arrow_algo}

    return app, socketio
