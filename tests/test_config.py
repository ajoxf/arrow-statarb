"""Config loader + the dry-run/live fail-safe (Arrow fact #1)."""

from arrow_statarb.config.config import Config


def _write(tmp_path, body):
    p = tmp_path / "settings.yaml"
    p.write_text(body)
    return p


def test_dot_access(tmp_path):
    cfg = Config(_write(tmp_path, "signal:\n  entry_zscore: 2.5\n"))
    assert cfg.get("signal.entry_zscore") == 2.5
    assert cfg.get("signal.missing", "x") == "x"


def test_dry_run_default_when_missing(tmp_path):
    cfg = Config(_write(tmp_path, "broker:\n  name: arrow\n"))
    assert cfg.is_dry_run is True            # no mode → fail-safe dry-run


def test_live_mode(tmp_path):
    cfg = Config(_write(tmp_path, "mode: live\n"))
    assert cfg.mode == "live"
    assert cfg.is_dry_run is False


def test_unreadable_mode_is_dry_run(tmp_path):
    cfg = Config(_write(tmp_path, "mode: simulate\n"))
    assert cfg.is_dry_run is True            # anything not exactly "live"


def test_set_mode_persists(tmp_path):
    p = _write(tmp_path, "mode: dry_run\n")
    cfg = Config(p)
    cfg.set_mode("live")
    assert Config(p).mode == "live"
    cfg.set_mode("dry_run")
    assert Config(p).is_dry_run is True
