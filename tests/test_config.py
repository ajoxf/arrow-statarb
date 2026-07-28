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


def test_backfills_missing_keys_from_template(tmp_path):
    # A partial settings.yaml (old version, missing keys) sitting NEXT TO a
    # settings.example.yaml gets the missing keys backfilled from the template,
    # while its own values win. This is the upgrade-safety fix for the
    # example-vs-code default mismatch (e.g. window_minutes).
    (tmp_path / "settings.example.yaml").write_text(
        "signal:\n  window_minutes: 150\n  entry_zscore: 2.0\n"
        "filters:\n  min_edge_multiple: 1.5\n")
    p = tmp_path / "settings.yaml"
    p.write_text("signal:\n  entry_zscore: 3.1\n")     # only overrides entry_zscore
    cfg = Config(p)
    assert cfg.get("signal.entry_zscore") == 3.1        # user's value wins
    assert cfg.get("signal.window_minutes") == 150      # backfilled from template
    assert cfg.get("filters.min_edge_multiple") == 1.5  # whole missing section backfilled


def test_no_backfill_without_sibling_template(tmp_path):
    # No template beside the file (the test/temp case) → no merge, code defaults
    # apply exactly as before — so the test suite is unaffected by the backfill.
    cfg = Config(_write(tmp_path, "signal:\n  entry_zscore: 2.0\n"))
    assert cfg.get("signal.window_minutes", "code-default") == "code-default"


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
