"""Liveness heartbeat + health verdict + watchdog restart decision."""

from arrow_statarb.core.health import Heartbeat, health_verdict, should_restart


def test_heartbeat_writes_and_ages(tmp_path):
    p = tmp_path / "hb.txt"
    hb = Heartbeat(p, clock=lambda: 1000.0)
    hb.beat()
    assert p.read_text().strip() == "1000.0"
    assert Heartbeat.age(p, now=1005.0) == 5.0
    assert Heartbeat.age(tmp_path / "missing.txt") is None      # unreadable → None


def test_health_verdict_separates_freeze_from_feed():
    # both fresh → ok
    assert health_verdict(5.0, 3.0, 60, 120)["ok"] is True
    # heartbeat stale → frozen-loop problem
    v = health_verdict(120.0, 3.0, 60, 120)
    assert v["ok"] is False and "frozen" in v["problems"][0]
    # ticks stale but heartbeat fine → feed problem, distinct message
    v = health_verdict(5.0, 300.0, 60, 120)
    assert v["ok"] is False and "feed" in v["problems"][0]
    # missing heartbeat counts as stale
    assert health_verdict(None, 3.0, 60, 120)["ok"] is False
    # unknown tick age never trips the feed check
    assert health_verdict(5.0, None, 60, 120)["ok"] is True


def test_should_restart_only_on_freeze_or_death():
    assert should_restart(5.0, process_alive=True, max_heartbeat_sec=60) is False
    assert should_restart(120.0, process_alive=True, max_heartbeat_sec=60) is True   # frozen
    assert should_restart(5.0, process_alive=False, max_heartbeat_sec=60) is True    # dead
    assert should_restart(None, process_alive=True, max_heartbeat_sec=60) is True    # no beat
