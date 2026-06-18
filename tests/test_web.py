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
        return {"order_id": "OID", "status": "submitted", **kw}

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
        "signal:\n  window_minutes: 120\n  sample_interval_sec: 0.5\n"
    )
    legs = tmp_path / "leg_assignments.yaml"
    legs.write_text(
        "leg_a:\n  mapping_id: nse_fo|NIFTY30JUN26F\n  ratio: 1\n"
        "leg_b:\n  mapping_id: nse_fo|NIFTY28JUL26F\n  ratio: 1\n"
    )
    # Point the app's leg-assignments path at our temp file.
    import pytest as _pt  # noqa
    appmod.LEG_ASSIGNMENTS_FILE = legs

    cfg = Config(settings)
    app, _sio = appmod.create_app(cfg)
    broker = FakeBroker()
    app.extensions["arrow"]["active"].set(broker)
    return app, broker


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
