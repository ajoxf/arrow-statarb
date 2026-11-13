"""More than one pair (Mirror-MT5's "every pair is its own ladder"): each pair
is its own engine with its own files, mounted at /p/<key>/, sharing the ONE
Arrow session — and no two pairs may ever share a contract."""

import pytest

from arrow_statarb.config.config import Config
from tests.test_web import FakeBroker


_BUILT = []


@pytest.fixture(autouse=True)
def _stop_every_engine_a_test_started():
    """Each desk here is several engines with background loops; stop them when
    the test ends, so they do not load the rest of the suite."""
    yield
    from arrow_statarb.web.pairs import _stop
    while _BUILT:
        app = _BUILT.pop()
        for a in list(app.extensions["arrow"]["pairs"]["apps"].values()):
            _stop(a)


def _desk(tmp_path):
    import arrow_statarb.web.app as appmod
    from arrow_statarb.web.pairs import create_desk_app
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "mode: dry_run\nbroker:\n  name: arrow\n  segments: {nse_fo: NSEFO, mcx_fo: MCXFO}\n"
        "execution:\n  product: NRML\n  default_lots: 1\n"
        "  use_limit_orders: false\n  verify_flat_before_entry: false\n"
        "signal:\n  window_minutes: 120\n  sample_interval_sec: 0.5\n")
    legs = tmp_path / "leg_assignments.yaml"
    legs.write_text("leg_a:\n  mapping_id: nse_fo|NIFTY30JUN26F\n  ratio: 1\n"
                    "leg_b:\n  mapping_id: nse_fo|NIFTY28JUL26F\n  ratio: 1\n")
    appmod.LEG_ASSIGNMENTS_FILE = legs
    appmod.TRADES_FILE = tmp_path / "trades.json"
    appmod.SHADOW_FILE = tmp_path / "shadow.json"
    appmod.HEARTBEAT_FILE = tmp_path / "heartbeat.txt"
    appmod.SIGNAL_WINDOW_FILE = tmp_path / "signal_window.json"
    appmod.EXECUTION_LOG_FILE = tmp_path / "execution_log.jsonl"
    app, _ = create_desk_app(Config(settings))
    _BUILT.append(app)
    broker = FakeBroker()
    app.extensions["arrow"]["active"].set(broker)
    return app, broker, app.test_client()


def _add(c, name="Gold Dec-Feb"):
    r = c.post("/api/pairs", json={"name": name}).get_json()
    assert r["success"], r
    return r["pair"]


def _legs(c, base, a, b):
    return c.post(base + "/api/leg-assignments", json={
        "leg_a": {"mapping_id": "mcx_fo|" + a, "ratio": 1},
        "leg_b": {"mapping_id": "mcx_fo|" + b, "ratio": 1}}).get_json()


def test_the_main_pair_is_listed_and_reachable_at_its_own_base(tmp_path):
    app, broker, c = _desk(tmp_path)
    (row,) = c.get("/api/pairs").get_json()["pairs"]
    assert row["key"] == "main" and row["base"] == "/p/main"
    assert row["legs"] == {"leg_a": "NIFTY30JUN26F", "leg_b": "NIFTY28JUL26F"}
    assert c.get("/p/main/api/leg-assignments").get_json() == c.get("/api/leg-assignments").get_json()


def test_a_new_pair_is_its_own_engine_with_its_own_files(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    assert p["key"] == "gold-dec-feb" and p["base"] == "/p/gold-dec-feb"
    assert c.get(p["base"] + "/api/leg-assignments").get_json() == {}   # no legs yet
    assert _legs(c, p["base"], "GOLDM05DEC26F", "GOLDM05FEB27F")["success"]
    rows = {r["key"]: r for r in c.get("/api/pairs").get_json()["pairs"]}
    assert rows["gold-dec-feb"]["legs"] == {"leg_a": "GOLDM05DEC26F", "leg_b": "GOLDM05FEB27F"}
    assert rows["main"]["legs"]["leg_a"] == "NIFTY30JUN26F"          # main untouched
    sub = app.extensions["arrow"]["pairs"]["apps"]["gold-dec-feb"]
    assert sub.extensions["arrow"]["active"] is app.extensions["arrow"]["active"]
    assert sub.extensions["arrow"]["trade_log"] is not app.extensions["arrow"]["trade_log"]


def test_a_trade_on_one_pair_is_journaled_by_that_pair_only(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    _legs(c, p["base"], "GOLDM05DEC26F", "GOLDM05FEB27F")
    sub = app.extensions["arrow"]["pairs"]["apps"]["gold-dec-feb"]
    r = c.post(p["base"] + "/api/manual-trade/execute", json={"direction": "LONG_SPREAD", "lots": 1}).get_json()
    assert r["success"], r
    assert sub.extensions["arrow"]["trade_log"].open_position() is not None
    assert app.extensions["arrow"]["trade_log"].open_position() is None


def test_two_pairs_may_not_share_a_contract(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    r = _legs(c, p["base"], "NIFTY30JUN26F", "GOLDM05FEB27F")     # main's leg A
    assert r["success"] is False and "NIFTY30JUN26F" in r["error"]
    assert c.get(p["base"] + "/api/leg-assignments").get_json() == {}


def test_control_distinct_contracts_are_accepted(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    assert _legs(c, p["base"], "SILVERM28NOV26F", "SILVERM26FEB27F")["success"]


def test_the_main_pair_cannot_take_a_contract_another_pair_has(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    _legs(c, p["base"], "GOLDM05DEC26F", "GOLDM05FEB27F")
    r = c.post("/api/leg-assignments", json={"leg_a": {"mapping_id": "mcx_fo|GOLDM05DEC26F"}}).get_json()
    assert r["success"] is False and "Gold Dec-Feb" in r["error"]


def test_a_pairs_page_talks_to_that_pair(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    html = c.get(p["base"] + "/settings").get_data(as_text=True)
    assert 'window.PAIR_BASE=B' in html and '"/p/gold-dec-feb"' in html


def test_control_the_plain_pages_are_not_rerouted(tmp_path):
    app, broker, c = _desk(tmp_path)
    assert "PAIR_BASE" not in c.get("/settings").get_data(as_text=True)


def test_a_pair_with_a_position_cannot_be_switched_off(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    _legs(c, p["base"], "GOLDM05DEC26F", "GOLDM05FEB27F")
    c.post(p["base"] + "/api/manual-trade/execute", json={"direction": "LONG_SPREAD", "lots": 1})
    r = c.post("/api/pairs/gold-dec-feb", json={"enabled": False}).get_json()
    assert r["success"] is False and "position" in r["error"]
    assert c.get(p["base"] + "/api/leg-assignments").status_code == 200


def test_control_an_idle_pair_switches_off_and_back_on(tmp_path):
    app, broker, c = _desk(tmp_path)
    p = _add(c)
    _legs(c, p["base"], "GOLDM05DEC26F", "GOLDM05FEB27F")
    assert c.post("/api/pairs/gold-dec-feb", json={"enabled": False}).get_json()["success"]
    assert c.get(p["base"] + "/api/leg-assignments").status_code == 404
    assert c.post("/api/pairs/gold-dec-feb", json={"enabled": True}).get_json()["success"]
    assert c.get(p["base"] + "/api/leg-assignments").get_json()["leg_a"]["mapping_id"].endswith("GOLDM05DEC26F")


def test_pairs_survive_a_restart(tmp_path):
    app, broker, c = _desk(tmp_path)
    _add(c, "Silver")
    app2, _, c2 = _desk(tmp_path)
    keys = [r["key"] for r in c2.get("/api/pairs").get_json()["pairs"]]
    assert keys == ["main", "silver"]
    assert c2.get("/p/silver/api/leg-assignments").status_code == 200


def test_one_login_one_positions_poll_for_every_pair(tmp_path):
    app, broker, c = _desk(tmp_path)
    _add(c, "Silver"); _add(c, "Copper")
    import threading
    names = [t.name for t in threading.enumerate()]
    assert names.count("ArrowAutoConnect") <= 1
    active = app.extensions["arrow"]["active"]
    assert active._desk_cache["refresher"] is True


def test_a_new_pair_does_not_inherit_the_main_pairs_contract_multiplier(tmp_path):
    import yaml
    import arrow_statarb.web.app as appmod
    app, broker, c = _desk(tmp_path)
    s = tmp_path / "settings.yaml"
    d = yaml.safe_load(s.read_text()); d["pairs"] = {"multiplier_a": 10, "multiplier_b": 10}
    d.setdefault("signal", {})["hedge_ratio"] = 1.3
    s.write_text(yaml.safe_dump(d))
    _add(c, "Copper")
    new = yaml.safe_load(appmod.pair_paths("copper")["settings"].read_text())
    assert new["pairs"]["multiplier_a"] == 0 and new["pairs"]["multiplier_b"] == 0
    assert new["signal"]["hedge_ratio"] == 1.0


def test_control_a_new_pair_keeps_the_main_pairs_rules(tmp_path):
    import yaml
    import arrow_statarb.web.app as appmod
    app, broker, c = _desk(tmp_path)
    _add(c, "Copper")
    new = yaml.safe_load(appmod.pair_paths("copper")["settings"].read_text())
    assert new["signal"]["window_minutes"] == 120 and new["mode"] == "dry_run"
