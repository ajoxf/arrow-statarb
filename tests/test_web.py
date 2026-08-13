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
    client.post("/api/manual-trade/close",
                json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
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
