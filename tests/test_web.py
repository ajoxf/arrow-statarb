"""Web order path: the shared dry-run/live gate (Arrow fact #1) and that the
manual + algo routes go through one segment-aware, lot-multiplied, mpp path."""

import pytest

from arrow_statarb.config.config import Config


class FakeBroker:
    """Minimal broker for exercising the web order path offline."""
    def __init__(self):
        self.connected = True
        self.orders = []

    def resolve_lot_size(self, seg, sym):
        return 75

    def resolve_token(self, seg, sym):
        return sym

    def submit_order(self, **kw):
        self.orders.append(kw)
        oid = f"OID{len(self.orders)}"
        return {"order_id": oid, "status": "submitted", **kw}

    # the live SpreadExecutor confirms fills — report every order as filled
    def get_order_status(self, order_id):
        return {"order_id": order_id, "status": "COMPLETE", "filled_qty": 150,
                "pending_qty": 0, "avg_price": 100.0, "raw": {}}

    def amend_order(self, order_id, price=None, quantity=None, order_type=None):
        return True

    def cancel_order(self, order_id):
        return True

    # streaming is optional — return nothing so REST path is skipped in prices
    def start_price_stream(self, syms):
        return False

    def get_streamed_ltp(self, syms):
        return {}

    def get_positions(self):
        return []


def _app(tmp_path, mode="dry_run"):
    import arrow_statarb.web.app as appmod

    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"mode: {mode}\n"
        "broker:\n  name: arrow\n"
        "  segments: {nse_fo: NSEFO}\n"
        "execution:\n  product: NRML\n  default_lots: 1\n"
        "  use_limit_orders: false\n  verify_flat_before_entry: false\n"
        "signal:\n  window_minutes: 120\n  sample_interval_sec: 0.5\n"
    )
    legs = tmp_path / "leg_assignments.yaml"
    legs.write_text(
        "leg_a:\n  mapping_id: nse_fo|NIFTY30JUN26F\n  ratio: 1\n"
        "leg_b:\n  mapping_id: nse_fo|NIFTY28JUL26F\n  ratio: 1\n"
    )
    # Point the app's leg-assignments + trade-log paths at temp files so tests
    # never read or write the shipped config/data files.
    import pytest as _pt  # noqa
    appmod.LEG_ASSIGNMENTS_FILE = legs
    appmod.TRADES_FILE = tmp_path / "trades.json"
    appmod.SHADOW_FILE = tmp_path / "shadow.json"
    appmod.HEARTBEAT_FILE = tmp_path / "heartbeat.txt"
    appmod.SIGNAL_WINDOW_FILE = tmp_path / "signal_window.json"
    appmod.EXECUTION_LOG_FILE = tmp_path / "execution_log.jsonl"

    cfg = Config(settings)
    app, _sio = appmod.create_app(cfg)
    broker = FakeBroker()
    app.extensions["arrow"]["active"].set(broker)
    return app, broker


class SlippingBroker(FakeBroker):
    """Reports an LTP and fills every limit at its (crossed) submitted price, so
    a large limit offset produces measurable slippage. Recovery MARKET orders
    (price=None) fill at the LTP — confirming the unwind."""
    def __init__(self, ltp=100.0):
        super().__init__()
        self._ltp = ltp
        self._px = {}

    def start_price_stream(self, syms):
        return True

    def get_streamed_ltp(self, syms):
        return {str(s).upper(): self._ltp for s in syms}

    def submit_order(self, **kw):
        r = super().submit_order(**kw)
        self._px[r["order_id"]] = kw.get("price")
        return r

    def get_order_status(self, order_id):
        price = self._px.get(order_id)
        avg = float(price) if price else self._ltp        # market recovery → LTP
        return {"order_id": order_id, "status": "COMPLETE", "filled_qty": 150,
                "pending_qty": 0, "avg_price": avg, "raw": {}}


def test_slippage_abort_charged_to_untracked_ledger(tmp_path):
    # A live entry that slips past the budget is unwound; the real money spent
    # (fees on 4 fills + realized slippage) must be booked to the untracked
    # ledger so it counts against max_daily_loss.
    import arrow_statarb.web.app as appmod
    appmod.UNTRACKED_FILE = tmp_path / "untracked.json"
    app, _ = _app(tmp_path, mode="live")
    app.extensions["arrow"]["active"].set(SlippingBroker(ltp=100.0))
    client = app.test_client()
    # 1% limit offset ≫ 0.5% budget → the fill will breach and unwind
    client.post("/api/settings", json={
        "execution": {"use_limit_orders": True, "limit_offset_pct": 1.0},
        "risk": {"max_slippage_pct": 0.5},
    })
    res = client.post("/api/manual-trade/execute",
                      json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert res["success"] is False
    assert res["slippage_abort"] is True
    led = client.get("/api/untracked").get_json()
    assert led["day_cost"] > 0
    ev = led["events"][0]
    assert ev["reason"] == "slippage_abort"
    assert ev["est_cost"] > 0


class TwoLotBroker(FakeBroker):
    """Per-symbol lot sizes: an ETF (1 unit) vs a future (25/lot)."""
    def __init__(self, sizes):
        super().__init__()
        self._sizes = sizes
    def resolve_lot_size(self, seg, sym):
        return self._sizes.get(sym.upper(), 1)


def test_hedge_ratio_order_sizing(tmp_path):
    # NIFTYBEES (leg_a, lot 1) vs NIFTY future (leg_b, lot 25), hedge_ratio 87.
    # leg_b (contract): 1 lot × 25 = 25 units. leg_a: matched exposure =
    # 25 × 87 = 2175 units. Both legs must carry equal, opposite exposure.
    import arrow_statarb.web.app as appmod
    app, _ = _app(tmp_path, mode="live")
    # reassign legs to the ETF/future pair
    appmod.LEG_ASSIGNMENTS_FILE.write_text(
        "leg_a:\n  mapping_id: nse_cm|NIFTYBEES\n  ratio: 1\n"
        "leg_b:\n  mapping_id: nse_fo|NIFTY\n  ratio: 1\n")
    broker = TwoLotBroker({"NIFTYBEES": 1, "NIFTY": 25})
    app.extensions["arrow"]["active"].set(broker)
    client = app.test_client()
    client.post("/api/settings", json={"signal": {"hedge_ratio": 87.0}})
    res = client.post("/api/manual-trade/execute",
                      json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert res["success"] is True
    by_sym = {o["symbol"].upper(): o for o in broker.orders}
    assert by_sym["NIFTY"]["quantity"] == 25         # 1 lot × 25
    assert by_sym["NIFTY"]["side"] == "sell"         # LONG_SPREAD = sell future
    assert by_sym["NIFTYBEES"]["quantity"] == 2175   # 25 × 87 matched exposure
    assert by_sym["NIFTYBEES"]["side"] == "buy"      # buy the ETF


def test_telegram_status_and_settings(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    client = app.test_client()
    st = client.get("/api/telegram/status").get_json()
    assert "token_set" in st and "configured" in st
    # settings round-trip for the telegram section (token never posted)
    client.post("/api/settings", json={"telegram": {"enabled": True, "chat_id": "555",
                                                     "notify_health": True}})
    back = client.get("/api/settings").get_json()["telegram"]
    assert back["enabled"] is True and back["chat_id"] == "555"
    assert back["notify_health"] is True
    # test send without a token → graceful failure message
    res = client.post("/api/telegram/test").get_json()
    assert res["ok"] is False and "ARROW_TELEGRAM_BOT_TOKEN" in res["message"]


def test_health_endpoint(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    client = app.test_client()
    h = client.get("/api/health").get_json()
    # heartbeat was written at startup → fresh, and no feed staleness with no data
    assert h["heartbeat_age_sec"] is not None and h["heartbeat_age_sec"] < 60
    assert h["ok"] is True and "problems" in h


def test_backtest_endpoint(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    client = app.test_client()
    # no data collected yet → graceful error, not a crash
    assert "error" in client.post("/api/backtest").get_json()
    # feed the signal window some bars, then backtest runs on the real data
    eng = app.extensions["arrow"]["signal"]
    for i in range(400):
        eng.push(24000.0 + (i % 5) - 2, 24080.0, ts=1000.0 + i)
    m = client.post("/api/backtest").get_json()
    assert m.get("bars") == 400
    assert "expectancy" in m and "below_cost_pct" in m


def test_close_arms_whatif_shadow(tmp_path):
    # A non-target close arms a what-if-held watch (clean target hits don't).
    app, _ = _app(tmp_path, mode="live")
    app.extensions["arrow"]["active"].set(SlippingBroker(ltp=100.0))
    client = app.test_client()
    assert client.post("/api/manual-trade/execute",
                       json={"direction": "LONG_SPREAD", "lots": 1}).get_json()["success"]
    assert client.post("/api/manual-trade/close",
                       json={"direction": "LONG_SPREAD", "lots": 1}).get_json()["success"]
    sh = client.get("/api/shadow").get_json()
    assert sh["active"] == 1
    assert sh["watches"][0]["direction"] == "LONG_SPREAD"


def test_settings_expose_and_persist_all_knobs(tmp_path):
    # Every knob a non-technical user might set must be readable AND writable
    # through the Settings page — no .yaml editing required. GET → POST → GET
    # must round-trip the value for each newly-surfaced key.
    app, _ = _app(tmp_path)
    client = app.test_client()
    got = client.get("/api/settings").get_json()
    # newly-surfaced keys grouped by section, with a changed value to write
    changes = {
        "signal": {"display_refresh_ms": 750, "persist_window": False,
                   "resume_max_gap_min": 15, "persist_interval_sec": 45,
                   "hedge_ratio": 87.5},
        "filters": {"half_life_min_sec": 30, "half_life_max_sec": 900,
                    "stt_a_pct": 0.001, "stt_b_pct": 0.02},
        "regime": {"window_samples": 200, "vr_lag": 7},
        "trading_hours": {"close_hour": 15, "close_min": 25, "no_entry_buffer_min": 20},
        "execution": {"product": "MIS", "cooldown_sec": 120, "use_limit_orders": False,
                      "price_tick_size": 0.05, "poll_interval_sec": 0.6,
                      "limit_to_market": False, "unknown_status_grace_polls": 5,
                      "assume_fill_on_unknown": True, "exit_retry_backoff_sec": 8,
                      "exit_retry_backoff_max_sec": 90, "verify_flat_before_entry": False,
                      "verify_flat_fail_open": True, "sim_tick_size": 0.05,
                      "sim_spread_ticks": 3.0, "sim_extra_slip_ticks": 1.0,
                      "sim_slow_prob": 0.4, "sim_reject_prob": 0.1,
                      "sim_orphan_prob": 0.2, "sim_default_lot_size": 50},
        "broker": {"persist_session": False, "cache_ttl_sec": 5},
        "costs": {"use_segment_costs": True, "gst_pct": 18.0,
                  "no_entry_days_before_expiry": 3, "stt_etf": 0.001,
                  "stt_nse_fo": 0.02, "ctt_mcx_fo": 0.01, "stt_nse_cm": 0.1},
    }
    # every section+key must already be present in the GET payload
    for sec, kv in changes.items():
        assert sec in got, f"section {sec} missing from GET"
        for k in kv:
            assert k in got[sec], f"{sec}.{k} not exposed in GET"
    res = client.post("/api/settings", json=changes).get_json()
    assert res["success"] is True
    back = client.get("/api/settings").get_json()
    for sec, kv in changes.items():
        for k, v in kv.items():
            assert back[sec][k] == v, f"{sec}.{k} did not persist: {back[sec][k]!r} != {v!r}"


def test_dry_run_does_not_transmit(tmp_path):
    app, broker = _app(tmp_path, mode="dry_run")
    client = app.test_client()
    res = client.post("/api/manual-trade/execute",
                      json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert res["success"] is True
    assert res["dry_run"] is True
    assert broker.orders == []                 # nothing transmitted in dry-run


def test_live_transmits_with_mpp_and_units(tmp_path):
    app, broker = _app(tmp_path, mode="live")
    client = app.test_client()
    res = client.post("/api/manual-trade/execute",
                      json={"direction": "LONG_SPREAD", "lots": 2}).get_json()
    assert res["success"] is True
    assert res["dry_run"] is False
    assert len(broker.orders) == 2             # both legs fired
    # Quantity is lots × lot_size (2 × 75), market order via the broker.
    for o in broker.orders:
        assert o["quantity"] == 150
        assert o["order_type"] == "market"
        assert o["exchange_segment"] == "nse_fo"
    sides = sorted(o["side"] for o in broker.orders)
    assert sides == ["buy", "sell"]            # LONG_SPREAD = buy A / sell B


def test_close_reverses_direction(tmp_path):
    app, broker = _app(tmp_path, mode="live")
    client = app.test_client()
    assert client.post("/api/manual-trade/execute",
                       json={"direction": "LONG_SPREAD", "lots": 1}).get_json()["success"]
    broker.orders.clear()
    r = client.post("/api/manual-trade/close",
                    json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert r["success"] is True
    # Closing a LONG_SPREAD reverses to sell A / buy B.
    by_sym = {o["symbol"]: o["side"] for o in broker.orders}
    assert by_sym["NIFTY30JUN26F"] == "sell"
    assert by_sym["NIFTY28JUL26F"] == "buy"


def test_config_endpoint_reports_mode(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    client = app.test_client()
    c = client.get("/api/config").get_json()
    assert c["dry_run"] is True
    assert c["broker"] == "arrow"


def test_leg_info_lot_mismatch(tmp_path):
    app, broker = _app(tmp_path, mode="dry_run")

    # Make leg B a different lot size to trigger the warning.
    def _ls(seg, sym):
        return 75 if "JUN" in sym else 65
    broker.resolve_lot_size = _ls

    client = app.test_client()
    info = client.get("/api/leg-info").get_json()
    assert info["lot_mismatch"] is True
    assert "NOT share-neutral" in info["message"]


def test_leg_info_days_to_expiry_is_info_only(tmp_path):
    # Days-to-expiry surfaces on /api/leg-info for the dashboard. It is a pure
    # readout: a broker WITHOUT resolve_expiry_ymd yields None (no crash), and a
    # broker WITH it yields the day count — never gating anything.
    app, broker = _app(tmp_path, mode="dry_run")
    client = app.test_client()

    # No resolve_expiry_ymd on the fake broker → field present but None.
    info = client.get("/api/leg-info").get_json()
    assert info["legs"]["leg_a"]["days_to_expiry"] is None

    # Add the info-only resolver → a positive day count appears. Compute the
    # target against the SAME IST 'today' the endpoint uses (avoid TZ off-by-one).
    from datetime import datetime, timedelta
    from arrow_statarb.web.app import _IST
    future = datetime.now(_IST).date() + timedelta(days=12)
    broker.resolve_expiry_ymd = lambda sym: (future.year, future.month, future.day)
    info = client.get("/api/leg-info").get_json()
    assert info["legs"]["leg_a"]["days_to_expiry"] == 12
    assert info["legs"]["leg_b"]["days_to_expiry"] == 12


def test_scenario_catalogue_endpoint(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    c = app.test_client()
    cat = c.get("/api/scenario-catalogue").get_json()
    assert len(cat) == 40 and cat[0]["type"] == "BUY_SPOT"
    # dry_run places no orders → guarded with a switch-to-live_sim hint
    st = c.post("/api/scenario-test", json={"id": 0}).get_json()
    assert st["ok"] is False and "live_sim" in st["error"]


def test_scenario_runs_live_sim_through_adapter(tmp_path):
    # live_sim runs the ported ScenarioRunner over the Arrow leg adapter against
    # the SIM broker (no real orders) — proving the seam end-to-end in the app.
    app, _ = _app(tmp_path, mode="live_sim")
    c = app.test_client()
    r = c.post("/api/scenario-test", json={"id": 18}).get_json()   # MKT BUY_SPOT #1
    assert r["ok"] is True and "flat" in r["detail"]
    r2 = c.post("/api/scenario-test", json={"id": 36}).get_json()  # partial rollback
    assert r2["ok"] is True and any(s[0].startswith("rollback") for s in r2["steps"])


def test_engine_mode_setting_roundtrip(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    c = app.test_client()
    assert c.get("/api/settings").get_json()["execution"]["engine_mode"] == "legacy"
    c.post("/api/settings", json={"execution": {"engine_mode": "clip", "slice_lots": 5}})
    ex = c.get("/api/settings").get_json()["execution"]
    assert ex["engine_mode"] == "clip" and ex["slice_lots"] == 5
    # invalid engine_mode falls back to legacy
    c.post("/api/settings", json={"execution": {"engine_mode": "bogus"}})
    assert c.get("/api/settings").get_json()["execution"]["engine_mode"] == "legacy"


def test_clip_execution_places_and_closes_live_sim(tmp_path):
    # engine_mode=clip routes real order placement through the clip engine over
    # the SIM broker — correct Arrow sides (LONG_SPREAD = buy A / sell B), flat.
    app, _ = _app(tmp_path, mode="live_sim")
    c = app.test_client()
    c.post("/api/settings", json={"execution": {"engine_mode": "clip"}})
    r = c.post("/api/manual-trade/execute", json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert r["success"] is True
    sides = {x["symbol"]: x["side"] for x in r["results"]}
    assert sides["NIFTY30JUN26F"] == "buy" and sides["NIFTY28JUL26F"] == "sell"
    rc = c.post("/api/manual-trade/close", json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert rc["success"] is True


def test_engine_shadow_endpoint(tmp_path):
    # Shadow preview is read-only and degrades gracefully before the signal is
    # warm — and must never place an order (no broker order calls).
    app, broker = _app(tmp_path, mode="dry_run")
    r = app.test_client().get("/api/engine/shadow").get_json()
    assert r["shadow"] is True and r["ready"] is False
    assert broker.orders == []                       # nothing was traded


def test_drawdown_endpoint_shape(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    d = app.test_client().get("/api/drawdown").get_json()
    assert set(d.keys()) == {"drawdown", "excursion"}
    assert d["drawdown"]["max_inr"] == 0.0 and d["excursion"] == []


def test_settings_pairs_section_roundtrip(tmp_path):
    app, _ = _app(tmp_path, mode="dry_run")
    c = app.test_client()
    g = c.get("/api/settings").get_json()
    assert g["pairs"]["pair_type"] == "SPOT_FUTURE"          # default
    r = c.post("/api/settings", json={"pairs": {
        "pair_type": "FUTURE_FUTURE", "risk_free_rate": 0.06,
        "sizing_mode": "notional", "hedge_mode": "notional",
        "notional_per_leg_inr": 250000}})
    assert r.get_json()["success"] is True
    g2 = c.get("/api/settings").get_json()["pairs"]
    assert g2["pair_type"] == "FUTURE_FUTURE" and g2["sizing_mode"] == "notional"
    assert g2["risk_free_rate"] == 0.06 and g2["notional_per_leg_inr"] == 250000
    # an invalid pair_type falls back safely
    c.post("/api/settings", json={"pairs": {"pair_type": "BOGUS"}})
    assert c.get("/api/settings").get_json()["pairs"]["pair_type"] == "SPOT_FUTURE"


def test_mcx_price_multiplier_overrides_k_not_order_lot(tmp_path):
    # MCX price multiplier scales the P&L math (k, notionals) but NOT the broker
    # order-lot. Broker lot size = 75; multiplier override = 10 → k follows 10.
    app, broker = _app(tmp_path, mode="dry_run")
    broker.resolve_lot_size = lambda seg, sym: 75            # order-lot (unchanged)
    broker.get_ltp = lambda ins: {"NIFTY30JUN26F": 100.0, "NIFTY28JUL26F": 100.0}
    c = app.test_client()
    # auto (0) → k uses the broker lot size (75)
    a0 = c.get("/api/pair-analytics").get_json()
    assert a0["contract_b"] == 75 and a0["sizing"]["spread_units"] == 75
    # override to 10 → k follows the multiplier, not the lot size
    c.post("/api/settings", json={"pairs": {"multiplier_a": 10, "multiplier_b": 10}})
    a1 = c.get("/api/pair-analytics").get_json()
    assert a1["contract_a"] == 10 and a1["contract_b"] == 10
    assert a1["sizing"]["spread_units"] == 10                # k = 1 lot × 10
    assert a1["sizing"]["leg_b_notional_inr"] == 1000.0      # 1 × 10 × 100


def test_pair_analytics_fair_value_and_sizing(tmp_path):
    # Read-only Phase-6 endpoint: with prices + lot sizes it returns the
    # cost-of-carry fair value, contract-aware sizing (k), and hedge drift.
    app, broker = _app(tmp_path, mode="dry_run")
    broker.resolve_lot_size = lambda seg, sym: 50            # contract size
    broker.get_ltp = lambda instruments: {
        "NIFTY30JUN26F": 100.0, "NIFTY28JUL26F": 101.0}
    from datetime import datetime, timedelta
    from arrow_statarb.web.app import _IST
    far = datetime.now(_IST).date() + timedelta(days=60)
    broker.resolve_expiry_ymd = lambda sym: (far.year, far.month, far.day)

    a = app.test_client().get("/api/pair-analytics").get_json()
    assert a["ok"] is True
    assert a["contract_a"] == 50 and a["contract_b"] == 50
    # sizing resolved: k = leg_b_lots * contract_b
    assert a["sizing"]["spread_units"] == a["sizing"]["leg_b_lots"] * 50
    # fair value present (SPOT_FUTURE carry, expiry in the future)
    fv = a["fair_value"]
    assert fv["fair_value"] is not None
    # Sign convention: fair value is in Arrow's leg_a−leg_b frame, so the gap =
    # (hedge_ratio*leg_a − leg_b) − fair_value. Fair value must be negative here
    # (leg_a compounds ABOVE itself → fair spread leg_a−leg_b < 0), matching a
    # contango calendar rather than the old sign-flipped +value.
    eff_spread = a["hedge_ratio"] * a["leg_a_price"] - a["leg_b_price"]
    assert abs(fv["fair_gap"] - (eff_spread - fv["fair_value"])) < 1e-6
    assert fv["fair_value"] < 0
    # dollar-neutral beta = Pb/Pa = 101/100
    assert abs(a["sizing"]["dollar_neutral_beta"] - 1.01) < 1e-9


def test_session_token_persist_reuse_and_clear(tmp_path):
    import time
    import arrow_statarb.web.app as appmod
    appmod.SESSION_FILE = tmp_path / "arrow_session.json"

    # Nothing saved yet.
    assert appmod._load_session_token("APP1") == ""

    # Save then reuse for the SAME app_id.
    appmod._save_session_token("APP1", "TOKEN-XYZ")
    assert appmod._load_session_token("APP1") == "TOKEN-XYZ"

    # A different app_id must not reuse another account's token.
    assert appmod._load_session_token("APP2") == ""

    # Too old → not reused (Arrow tokens last ~24h).
    assert appmod._load_session_token("APP1", max_age_h=0.0) == ""

    # Clear drops it.
    appmod._clear_session_token()
    assert appmod._load_session_token("APP1") == ""


def test_preflight_checks(tmp_path):
    app, broker = _app(tmp_path, mode="dry_run")
    client = app.test_client()
    p = client.get("/api/preflight").get_json()
    keys = {c["key"] for c in p["checks"]}
    assert keys == {"connected", "legs", "funds", "signal", "caps", "sdk",
                    "entry_guards", "exit_safety"}
    assert "ready" in p and isinstance(p["ready"], bool)
    # the new safety checks are advisory — they must NOT be in the go-live gate
    advisory = {c["key"] for c in p["checks"] if c["key"] in ("entry_guards", "exit_safety")}
    assert advisory == {"entry_guards", "exit_safety"}
    # FakeBroker has get_funds (returns None funds) → not connected-funds-ok → not ready
    conn = next(c for c in p["checks"] if c["key"] == "connected")
    assert conn["status"] == "ok"     # FakeBroker is "connected"


def test_dashboard_serves_w3_and_is_arrow_inr(tmp_path):
    """The primary /dashboard is the W3 design, wired to Arrow/INR: it renders,
    carries the ₹ glyph, and shows no US$/crypto/MT5 idioms in its VISIBLE text
    (tooltips included). /dashboard-w3 is an alias; the first-gen dashboard is
    preserved at /dashboard-legacy."""
    import re
    app, _ = _app(tmp_path, mode="live_sim")
    client = app.test_client()

    primary = client.get("/dashboard")
    alias = client.get("/dashboard-w3")
    legacy = client.get("/dashboard-legacy")
    assert primary.status_code == alias.status_code == legacy.status_code == 200

    html = primary.get_data(as_text=True)
    # Same page served at both /dashboard and its alias.
    assert html == alias.get_data(as_text=True)
    # It IS the W3 design (Nexus logo), not the first-gen dashboard.
    assert "logo-nexus" in html
    assert "logo-nexus" not in legacy.get_data(as_text=True)

    # Visible text + tooltips only (drop <script>, <style>, comments).
    ns = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    ns = re.sub(r"<style\b.*?</style>", "", ns, flags=re.S | re.I)
    ns = re.sub(r"<!--.*?-->", "", ns, flags=re.S)
    tips = " ".join(re.findall(r'title="([^"]*)"', ns))
    visible = re.sub(r"<[^>]+>", " ", ns) + " " + tips

    assert "₹" in visible                       # Indian rupee currency
    for bad in ("$", "USDT", "OKX", "Binance", "liquidation", "Trading-vs-Funding"):
        assert bad not in visible, f"US$/crypto idiom leaked into /dashboard: {bad!r}"


def test_beta_drift_endpoint_wiring(tmp_path):
    """The /api/beta-zscore badge feed is wired to the real beta-drift monitor:
    WARMUP with no window, then a live reading (correct sign, DRIFTING/structural
    flag) once a drifting hedge-ratio window is collected."""
    import arrow_statarb.web.app as appmod

    settings = tmp_path / "settings.yaml"
    # Short beta windows so the test needs only a small, fast window.
    settings.write_text(
        "mode: live_sim\n"
        "broker:\n  name: arrow\n  segments: {nse_fo: NSEFO}\n"
        "signal:\n  window_minutes: 120\n  sample_interval_sec: 0.5\n"
        "  beta_window_sec: 20\n  beta_anchor_window_sec: 20\n"
        "  beta_structural_min_sec: 10\n"
    )
    legs = tmp_path / "leg_assignments.yaml"
    legs.write_text(
        "leg_a:\n  mapping_id: nse_fo|NIFTY30JUN26F\n  ratio: 1\n"
        "leg_b:\n  mapping_id: nse_fo|NIFTY28JUL26F\n  ratio: 1\n"
    )
    appmod.LEG_ASSIGNMENTS_FILE = legs
    appmod.SIGNAL_WINDOW_FILE = tmp_path / "signal_window.json"
    appmod.EXECUTION_LOG_FILE = tmp_path / "execution_log.jsonl"
    app, _sio = appmod.create_app(Config(settings))
    client = app.test_client()

    # Cold: no window yet → WARMUP, with the documented keys present.
    cold = client.get("/api/beta-zscore").get_json()
    assert cold["status"] == "WARMUP" and cold["z"] is None
    for k in ("anchor", "current_beta", "max_abs_z", "minutes_beyond"):
        assert k in cold

    # Feed a window whose hedge ratio drifts UP (leg_b/leg_a 1.00 → 1.03).
    import random
    eng = app.extensions["arrow"]["signal"]
    rng = random.Random(5)
    base, ai = 1_000_000.0, 100.0
    n = 400                              # 400 * 0.5s = 200s ≫ 2×20s windows
    for k in range(n):
        ai += rng.uniform(-0.4, 0.4)
        beta = 1.00 + 0.03 * (k / (n - 1))
        eng.push(ai, beta * ai + rng.uniform(-0.02, 0.02), ts=base + k * 0.5)

    hot = client.get("/api/beta-zscore").get_json()
    assert hot["status"] in ("DRIFTING", "STRUCTURAL_DRIFT")
    assert hot["z"] is not None and hot["z"] > 0          # ratio rose → +z
    assert hot["current_beta"] > hot["anchor"]
    assert hot["max_abs_z"] >= 2.0


def test_volume_endpoint_day_week_month(tmp_path):
    """/api/volume reports ₹ turnover + spread-lots for today/week/month (IST),
    computed from the trade log. Seed the log file so the app loads it on init."""
    import json as _json
    from datetime import datetime, timedelta, timezone
    import arrow_statarb.web.app as appmod

    IST = timezone(timedelta(hours=5, minutes=30))

    def ts(dt):
        return dt.timestamp()

    # Anchor "now" to a fixed IST instant so the periods are deterministic.
    now = datetime(2026, 8, 13, 15, 0, tzinfo=IST)               # Thursday
    def rec(when, action):
        return {"ts": ts(when), "action": action, "status": "LIVE",
                "lots": 1, "lot_size": 100, "leg_a_price": 6000.0,
                "leg_b_price": 6050.0, "direction": "LONG_SPREAD"}

    trades_file = tmp_path / "trades.json"
    trades_file.write_text(_json.dumps([
        rec(now.replace(hour=10), "OPEN"),                       # today
        rec(now.replace(hour=11), "CLOSE"),                      # today
        rec(datetime(2026, 8, 11, 10, tzinfo=IST), "OPEN"),      # this week (Tue)
        rec(datetime(2026, 8, 4, 10, tzinfo=IST), "OPEN"),       # this month
        rec(datetime(2026, 7, 30, 10, tzinfo=IST), "OPEN"),      # last month
    ]))

    settings = tmp_path / "settings.yaml"
    settings.write_text("mode: live_sim\nbroker:\n  name: arrow\n"
                        "  segments: {nse_fo: NSEFO}\n"
                        "signal:\n  window_minutes: 120\n")
    legs = tmp_path / "leg_assignments.yaml"
    legs.write_text("leg_a:\n  mapping_id: nse_fo|A\n  ratio: 1\n"
                    "leg_b:\n  mapping_id: nse_fo|B\n  ratio: 1\n")
    appmod.LEG_ASSIGNMENTS_FILE = legs
    appmod.TRADES_FILE = trades_file
    appmod.SIGNAL_WINDOW_FILE = tmp_path / "sw.json"
    appmod.EXECUTION_LOG_FILE = tmp_path / "execution_log.jsonl"
    app, _sio = appmod.create_app(Config(settings))
    client = app.test_client()

    v = client.get("/api/volume").get_json()
    per_fill = 1 * 100 * (6000.0 + 6050.0)
    # Today = 2 fills, week = 3, month = 4, all-time = 5.
    assert v["day"]["trades"] == 2
    assert v["day"]["turnover_inr"] == round(2 * per_fill, 2)
    assert v["week"]["trades"] == 3
    assert v["month"]["trades"] == 4
    assert v["all_time"]["trades"] == 5
    assert v["day"]["lots"] == 2
    assert len(v["recent_days"]) == 14


def test_dashboard_is_offline_capable_no_cdn(tmp_path):
    """The dashboard must render on a locked-down/offline box: no CDN <script>/
    <link> hosts, and every vendored asset served by the app itself (200)."""
    app, _ = _app(tmp_path, mode="live_sim")
    client = app.test_client()
    html = client.get("/dashboard").get_data(as_text=True)

    # No external CDN hosts referenced anywhere in the page.
    for host in ("cdn.jsdelivr.net", "cdn.socket.io", "unpkg.com", "cdnjs.cloudflare.com"):
        assert host not in html, f"dashboard still references CDN host {host}"

    # Every vendored asset the page needs is served locally with 200.
    for path in ("/static/vendor/chart.umd.min.js",
                 "/static/vendor/bootstrap.min.css",
                 "/static/vendor/bootstrap.bundle.min.js",
                 "/static/vendor/socket.io.min.js",
                 "/static/vendor/bootstrap-icons.css",
                 "/static/vendor/fonts/bootstrap-icons.woff2"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert len(r.get_data()) > 0
    # The icons CSS must load its font locally — no remote url()/@import
    # (a docs/license URL in a comment is fine; a remote resource fetch is not).
    css = client.get("/static/vendor/bootstrap-icons.css").get_data(as_text=True)
    assert "fonts/bootstrap-icons.woff2" in css
    assert "url(https://" not in css.replace(" ", "")
    assert "@import" not in css


# ── Algo switch, Arrow margin card, executable spreads on the status API ─────

def test_dashboard_algo_switch_endpoint_starts_and_stops(tmp_path):
    """The dashboard's switch posted to /api/engine/toggle-algo, which did not
    exist — the algo could not be started from the main dashboard."""
    app, _broker = _app(tmp_path)
    c = app.test_client()
    r = c.post("/api/engine/toggle-algo", json={"enabled": True}).get_json()
    assert r["success"] is True
    assert app.extensions["arrow"]["algo"].get_state()["running"] is True
    r = c.post("/api/engine/toggle-algo", json={"enabled": False}).get_json()
    assert r["success"] is True
    assert app.extensions["arrow"]["algo"].get_state()["running"] is False


def test_algo_switch_refuses_without_a_broker(tmp_path):
    app, _broker = _app(tmp_path)
    app.extensions["arrow"]["active"].set(None)
    r = app.test_client().post("/api/engine/toggle-algo", json={"enabled": True}).get_json()
    assert r["success"] is False and "broker" in r["error"].lower()


def test_arrow_margin_reports_funds_and_pair_basket(tmp_path):
    app, broker = _app(tmp_path)
    seen = {}
    broker.get_funds = lambda: {"cash": 500000.0, "used": 100000.0, "available": 400000.0}

    def pair_margin(legs, product="NRML"):
        seen["legs"] = legs
        return {"basket": {"total": 50000.0, "span": 42000.0, "exposure": 8000.0},
                "legs": [{"total": 130000.0}, {"total": 128000.0}], "error": None}
    broker.get_pair_margin = pair_margin
    d = app.test_client().get("/api/arrow-margin").get_json()
    assert d["utilisation_pct"] == 20.0
    assert d["pair"]["basket_total"] == 50000.0
    assert d["pair"]["spread_benefit"] == 208000.0
    assert d["pair"]["headroom_trades"] == 8
    # both legs, opposite sides, quantity in UNITS (lots × lot size 75)
    assert [(l["side"], l["quantity"]) for l in seen["legs"]] == [("buy", 75), ("sell", 75)]
    assert "leverage" not in str(d).lower()


def test_status_publishes_executable_spreads_and_real_readiness(tmp_path):
    app, _broker = _app(tmp_path)
    eng = app.extensions["arrow"]["signal"]
    for i in range(5):
        eng.push(110.0 + (i % 2), 10.0, ts=1000.0 + i)
    eng._book = {"leg_a": {"bid": 109.0, "ask": 111.0}, "leg_b": {"bid": 9.0, "ask": 11.0}}
    sig = app.test_client().get("/api/engine/status").get_json()["signal"]
    assert sig["sell_spread"] == 98.0 and sig["buy_spread"] == 102.0
    # 4 s sampled of a 2 h warm-up is NOT ready, even though a z exists
    assert sig["zscore"] is not None and sig["data_ready"] is False
    assert sig["history_sec"] == 4.0


def test_every_page_has_the_one_shared_algo_control(tmp_path):
    """One Algo on/off control, identical on every page — no page-specific
    switches that can disagree about whether the algo is running."""
    app, _broker = _app(tmp_path)
    c = app.test_client()
    for page in ("/", "/settings", "/analysis", "/dashboard", "/dashboard-legacy"):
        html = c.get(page).get_data(as_text=True)
        assert html.count('id="algo-ctl"') == 1, page
        # the mode badge is the shared one, filled from the server — never
        # a label baked in when the page was served
        assert html.count('id="mode-ctl"') == 1, page
        assert "PAPER" not in html and "DEMO SERVER" not in html, page
        assert 'class="mode-live"' not in html, page
        for old in ('id="algoSwitch"', 'id="nav-algo-btn"', 'id="algo-toggle"'):
            assert old not in html, (page, old)


def test_algo_state_reports_mode_and_lots(tmp_path):
    app, _broker = _app(tmp_path)
    st = app.test_client().get("/api/algo/state").get_json()
    assert st["running"] is False and st["mode"] == "dry_run" and st["lots"] == 1


def test_trade_direction_setting_round_trip_and_reaches_the_algo(tmp_path):
    app, _broker = _app(tmp_path)
    c = app.test_client()
    assert c.get("/api/settings").get_json()["signal"]["trade_direction"] == "both"
    r = c.post("/api/settings", json={"signal": {"trade_direction": "sell_only"}})
    assert r.status_code == 200
    assert c.get("/api/settings").get_json()["signal"]["trade_direction"] == "sell_only"
    sig = c.get("/api/engine/status").get_json()["signal"]
    assert sig["trade_direction"] == "sell_only"
    algo = app.extensions["arrow"]["algo"]
    assert algo._params()["trade_direction"] == "sell_only"


def test_trade_direction_rejects_unknown_values(tmp_path):
    app, _broker = _app(tmp_path)
    c = app.test_client()
    r = c.post("/api/settings", json={"signal": {"trade_direction": "sideways"}})
    assert r.status_code == 400
    assert c.get("/api/settings").get_json()["signal"]["trade_direction"] == "both"


# ── dashboard Close button: the orders must CLOSE the position that is held ──

import pytest as _pytest


@_pytest.mark.parametrize("held,close_sides", [
    ("LONG_SPREAD", ("sell", "buy")),     # long = bought A / sold B → sell A, buy B
    ("SHORT_SPREAD", ("buy", "sell")),    # short = sold A / bought B → buy A, sell B
])
def test_dashboard_close_closes_the_held_side_at_its_size(tmp_path, held, close_sides):
    app, broker = _app(tmp_path, mode="live")
    algo = app.extensions["arrow"]["algo"]
    algo._pos = {"direction": held, "lots": 2, "entry_z": 2.5, "entry_spread": 100.0,
                 "entry_fill_spread": 100.0, "entry_time": 0.0}
    r = app.test_client().post("/api/engine/close-position", json={}).get_json()
    assert r["success"] is True
    sides = [(o["symbol"], o["side"], o["quantity"]) for o in broker.orders]
    assert sides == [("NIFTY30JUN26F", close_sides[0], 150),       # 2 lots × 75
                     ("NIFTY28JUL26F", close_sides[1], 150)]
    # the algo no longer thinks it holds anything, so it will not "exit" again
    assert algo.get_state()["in_position"] is False


def test_status_names_the_held_side_and_entry_details(tmp_path):
    app, _broker = _app(tmp_path)
    algo = app.extensions["arrow"]["algo"]
    algo._pos = {"direction": "SHORT_SPREAD", "lots": 3, "entry_z": 2.7,
                 "entry_spread": 101.0, "entry_fill_spread": 101.5,
                 "entry_leg_a": 110.0, "entry_leg_b": 8.5, "entry_time": 1_790_000_000.0}
    d = app.test_client().get("/api/engine/status").get_json()
    assert d["position"] == "SHORT" and d["signal"]["current_position"] == "SHORT"
    t = d["open_trade"]
    assert t["position_type"] == "SHORT" and t["quantity"] == 3 and t["entry_zscore"] == 2.7
    assert t["entry_spot_price"] == 110.0 and t["entry_futures_price"] == 8.5
    assert t["entry_time"] == "2026-09-21T14:13:20"    # UTC; the page appends Z



# ── MANUAL / ALGO lock: whole account, both directions, enforced server-side ──

def _lock_app(tmp_path):
    app, broker = _app(tmp_path, mode="live")
    return app, broker, app.test_client(), app.extensions["arrow"]["algo"]


def test_manual_orders_refused_while_algo_is_on(tmp_path):
    app, broker, c, algo = _lock_app(tmp_path)
    assert c.post("/api/algo/start", json={}).get_json()["success"] is True
    for url in ("/api/manual-trade/execute", "/api/manual-trade/close"):
        r = c.post(url, json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
        assert r["success"] is False and "algo is ON" in r["error"], url
    r = c.post("/api/engine/close-position", json={}).get_json()
    assert r["success"] is False
    assert broker.orders == []
    algo.stop()


def test_algo_cannot_start_while_a_manual_position_is_open(tmp_path):
    app, broker, c, algo = _lock_app(tmp_path)
    assert c.post("/api/manual-trade/execute",
                  json={"direction": "SHORT_SPREAD", "lots": 1}).get_json()["success"]
    for url, body in (("/api/algo/start", {}), ("/api/engine/toggle-algo", {"enabled": True})):
        r = c.post(url, json=body).get_json()
        assert r["success"] is False and "MANUAL" in r["error"], url
    assert algo.running is False
    # close it by hand → the algo may start
    assert c.post("/api/manual-trade/close",
                  json={"direction": "SHORT_SPREAD", "lots": 1}).get_json()["success"]
    assert c.post("/api/algo/start", json={}).get_json()["success"] is True
    algo.stop()


def test_algo_entry_refused_while_a_manual_position_is_open(tmp_path):
    """Belt and braces: even if the algo were running, its entry path refuses."""
    app, broker, c, algo = _lock_app(tmp_path)
    assert c.post("/api/manual-trade/execute",
                  json={"direction": "LONG_SPREAD", "lots": 1}).get_json()["success"]
    n = len(broker.orders)
    res = algo._execute("SHORT_SPREAD", 1, source="algo", z=2.5, spread=100.0)
    assert res["success"] is False and "MANUAL" in res["error"]
    assert len(broker.orders) == n


def test_one_position_at_a_time_and_close_must_match(tmp_path):
    app, broker, c, algo = _lock_app(tmp_path)
    r = c.post("/api/manual-trade/close", json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert r["success"] is False and "No open position" in r["error"]
    assert broker.orders == []                      # never opened the opposite
    assert c.post("/api/manual-trade/execute",
                  json={"direction": "LONG_SPREAD", "lots": 1}).get_json()["success"]
    r = c.post("/api/manual-trade/execute", json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert r["success"] is False and "already open" in r["error"]
    r = c.post("/api/manual-trade/close", json={"direction": "SHORT_SPREAD", "lots": 1}).get_json()
    assert r["success"] is False and "not SHORT SPREAD" in r["error"]
    r = c.post("/api/manual-trade/close", json={"direction": "LONG_SPREAD", "lots": 3}).get_json()
    assert r["success"] is False and "Only 1 lot" in r["error"]


def test_every_order_is_tagged_manual_or_algo(tmp_path):
    app, broker, c, algo = _lock_app(tmp_path)
    c.post("/api/manual-trade/execute", json={"direction": "LONG_SPREAD", "lots": 1})
    c.post("/api/manual-trade/close", json={"direction": "LONG_SPREAD", "lots": 1})
    algo._execute("SHORT_SPREAD", 1, source="algo", z=2.5, spread=100.0)
    evs = c.get("/api/execution").get_json()["events"]
    assert [e["source"] for e in reversed(evs)] == ["manual", "manual", "algo"]
    trades = app.extensions["arrow"]["algo"]  # noqa: F841 — journal checked below
    import json as _json
    recs = _json.loads((tmp_path / "trades.json").read_text())
    recs = recs if isinstance(recs, list) else recs.get("trades", [])
    assert [r["source"] for r in recs] == ["manual", "manual", "algo"]


def test_restart_adopts_only_an_ALGO_position(tmp_path):
    import json as _json
    import arrow_statarb.web.app as appmod
    for owner, expect in (("manual", False), ("algo", True)):
        d = tmp_path / owner
        d.mkdir()
        app, broker = _app(d, mode="live")
        c = app.test_client()
        if owner == "manual":
            assert c.post("/api/manual-trade/execute",
                          json={"direction": "LONG_SPREAD", "lots": 1}).get_json()["success"]
        else:
            app.extensions["arrow"]["algo"]._execute("LONG_SPREAD", 1, source="algo",
                                                   z=-2.5, spread=100.0)
        app2, _ = _app(d, mode="live")                 # "restart" on the same journal
        assert app2.extensions["arrow"]["algo"].get_state()["in_position"] is expect, owner


def test_manual_position_is_shown_valued_and_closable(tmp_path):
    app, broker, c, algo = _lock_app(tmp_path)
    assert c.post("/api/manual-trade/execute",
                  json={"direction": "SHORT_SPREAD", "lots": 2}).get_json()["success"]
    d = c.get("/api/engine/status").get_json()
    t = d["open_trade"]
    assert d["position"] == "SHORT" and t["owner"] == "MANUAL" and t["quantity"] == 2
    assert t["spread_levels"]["break_even"] is not None
    assert t["exit_target_usd"] is None and t["exit_stop_usd"] is None   # nothing manages it
    broker.orders.clear()
    r = c.post("/api/engine/close-position", json={}).get_json()
    assert r["success"] is True
    assert [(o["side"], o["quantity"]) for o in broker.orders] == [("buy", 150), ("sell", 150)]
    assert c.get("/api/engine/status").get_json()["position"] == "NONE"


def test_algo_state_reports_the_lock(tmp_path):
    app, broker, c, algo = _lock_app(tmp_path)
    st = c.get("/api/algo/state").get_json()
    assert st["manual_block"] is None and st["algo_block"] is None and st["position_owner"] is None
    c.post("/api/manual-trade/execute", json={"direction": "LONG_SPREAD", "lots": 1})
    st = c.get("/api/algo/state").get_json()
    assert st["position_owner"] == "manual" and "MANUAL" in st["algo_block"]
    c.post("/api/manual-trade/close", json={"direction": "LONG_SPREAD", "lots": 1})
    c.post("/api/algo/start", json={})
    st = c.get("/api/algo/state").get_json()
    assert "algo is ON" in st["manual_block"]
    algo.stop()


def test_resaving_the_same_pair_keeps_the_collected_window(tmp_path):
    """Saving Setup with the SAME legs must not wipe hours of samples and
    restart the warm-up; a real change of leg or ratio does restart it."""
    import time as _t
    app, broker = _app(tmp_path)
    c = app.test_client()
    eng = app.extensions["arrow"]["signal"]
    now = _t.time()
    for i in range(50):
        eng.push(110.0 + i % 3, 10.0, ts=now - 50 + i)
    same = {"leg_a": {"mapping_id": "nse_fo|NIFTY30JUN26F", "ratio": 1},
            "leg_b": {"mapping_id": "nse_fo|NIFTY28JUL26F", "ratio": 1}}
    assert c.post("/api/leg-assignments", json=same).get_json()["success"]
    assert len(eng._samples) == 50
    other = {"leg_b": {"mapping_id": "nse_fo|NIFTY25AUG26F", "ratio": 1}}
    assert c.post("/api/leg-assignments", json=other).get_json()["success"]
    assert len(eng._samples) == 0
    for i in range(5):
        eng.push(110.0, 10.0, ts=now + i)
    assert c.post("/api/leg-assignments",
                  json={"leg_a": {"mapping_id": "nse_fo|NIFTY30JUN26F", "ratio": 2}}).get_json()["success"]
    assert len(eng._samples) == 0          # a ratio change is a new spread
