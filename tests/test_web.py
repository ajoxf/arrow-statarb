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
