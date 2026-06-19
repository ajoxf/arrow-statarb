"""Configuration loader for the Arrow StatArb app.

Loads ``config/settings.yaml`` and provides dot-notation access plus a few
typed convenience accessors used across the app. Nothing is hardcoded — every
tunable lives in the YAML and is read through here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from loguru import logger

# Repo root = two levels up from this file (arrow_statarb/config/config.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
LEG_ASSIGNMENTS_FILE = CONFIG_DIR / "leg_assignments.yaml"


class Config:
    """Reads ``config/settings.yaml`` with dot-notation ``get`` access."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else SETTINGS_FILE
        self._data: Dict[str, Any] = {}
        self.reload()

    # ── loading ──────────────────────────────────────────────────────────────
    def reload(self) -> None:
        # settings.yaml is a runtime/local file (gitignored, mutated by the UI).
        # On first run, seed it from the tracked settings.example.yaml template so
        # a fresh clone has working defaults and pulls never conflict on it.
        if not self.path.exists():
            example = self.path.with_name(self.path.stem + ".example" + self.path.suffix)
            if example.exists():
                try:
                    import shutil
                    shutil.copyfile(example, self.path)
                    logger.info("Seeded {} from {}", self.path.name, example.name)
                except Exception as exc:
                    logger.warning("Could not seed {} from template — {}", self.path.name, exc)
        if self.path.exists():
            with open(self.path) as f:
                self._data = yaml.safe_load(f) or {}
            logger.debug("Config loaded from {}", self.path)
        else:
            self._data = {}
            logger.warning("Config file not found: {}", self.path)

    # ── access ───────────────────────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        """Dot-notation lookup, e.g. ``config.get('signal.entry_zscore', 2.0)``."""
        value: Any = self._data
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value

    def section(self, name: str) -> Dict[str, Any]:
        sec = self._data.get(name)
        return sec if isinstance(sec, dict) else {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._data

    # ── typed convenience accessors ──────────────────────────────────────────
    @property
    def mode(self) -> str:
        """``"live"`` or ``"dry_run"``. Anything else / unreadable → dry_run."""
        return str(self._data.get("mode", "dry_run")).lower()

    @property
    def is_dry_run(self) -> bool:
        """True unless mode is exactly ``"live"`` — fail-safe so a config glitch
        never silently transmits live orders."""
        return self.mode != "live"

    def set_mode(self, mode: str) -> None:
        """Persist the trading mode back to settings.yaml (dry_run | live_sim | live)."""
        m = str(mode).lower()
        self._data["mode"] = m if m in ("live", "live_sim") else "dry_run"
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

    # ── broker credentials from environment ──────────────────────────────────
    @staticmethod
    def arrow_credentials() -> Dict[str, str]:
        """Arrow credentials from environment variables (never stored in YAML)."""
        return {
            "app_id":      os.environ.get("ARROW_APP_ID", ""),
            "user_id":     os.environ.get("ARROW_USER_ID", ""),
            "password":    os.environ.get("ARROW_PASSWORD", ""),
            "api_secret":  os.environ.get("ARROW_API_SECRET", ""),
            "totp_secret": os.environ.get("ARROW_TOTP_SECRET", ""),
        }


# A process-wide default instance for convenience.
config = Config()
