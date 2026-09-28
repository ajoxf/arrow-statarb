"""SQLite persistence for the richer analysis tables.

Ported from the W3 DataLogger (INR), Arrow-adapted: dropped the MT5-specific
broker_orders table; keeps trade_review, sd_touches, shadow_trades,
market_data, position_state, untracked_closes. Thread-safe (the web app is
multi-threaded). Additive — the existing JSON persistence keeps working; the
live engine writes here once wired.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# trade_review columns in write order (INR naming).
_REVIEW_COLS = [
    "position_id", "asset", "entry_z", "exit_z", "entry_sigma",
    "capture_target_inr", "cost_est_inr", "realized_pnl", "exit_reason",
    "outcome", "lots", "opened", "closed", "peak_pnl", "peak_min",
    "trough_pnl", "trough_min", "entry_spread", "exit_spread", "be_spread",
    "ex_spread", "tp_spread", "sl_spread", "notional_inr",
    "entry_cross_spread", "entry_cross_inr", "entry_slip_spread",
    "entry_slip_inr", "exit_cross_spread", "exit_cross_inr",
    "exit_slip_spread", "exit_slip_inr",
]


class Database:
    def __init__(self, path: str | Path = "data/analysis.db"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.init()

    # ── schema ────────────────────────────────────────────────────────────────
    def init(self) -> None:
        with self._lock:
            c = self._conn.cursor()
            c.execute(f"""CREATE TABLE IF NOT EXISTS trade_review (
                {', '.join(col + (' TEXT PRIMARY KEY' if col == 'position_id'
                                  else ' TEXT' if col in ('asset','exit_reason','outcome','opened','closed')
                                  else ' REAL') for col in _REVIEW_COLS)}
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS sd_touches (
                ts TEXT, asset TEXT, sd_level INTEGER, direction TEXT,
                zscore REAL, spread REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS shadow_trades (
                position_id TEXT PRIMARY KEY, asset TEXT, exit_reason TEXT,
                exit_pnl REAL, what_if_net REAL, peak REAL, trough REAL,
                hit_be_min REAL, hit_tp_min REAL, horizon_min REAL,
                verdict TEXT, completed TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS market_data (
                ts REAL, asset TEXT, spread REAL, zscore REAL, series_key TEXT)""")
            c.execute("""CREATE INDEX IF NOT EXISTS idx_md_asset_series
                ON market_data(asset, series_key, ts)""")
            c.execute("""CREATE TABLE IF NOT EXISTS position_state (
                position_id TEXT PRIMARY KEY, asset TEXT, state_json TEXT,
                updated TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS untracked_closes (
                ts TEXT, leg TEXT, symbol TEXT, ticket TEXT, volume REAL,
                price REAL, note TEXT)""")
            self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────────────
    def log_trade_review(self, review: Dict[str, Any], position_id: str,
                         opened: Optional[str] = None,
                         closed: Optional[str] = None) -> None:
        """Insert-or-replace one trade-review row from the Phase-3
        trade_review.build() dict (keys mapped to the INR columns)."""
        r = review or {}
        row = {
            "position_id": position_id, "asset": r.get("asset"),
            "entry_z": r.get("entry_z"), "exit_z": r.get("exit_z"),
            "entry_sigma": r.get("entry_sigma"),
            "capture_target_inr": r.get("capture_target_inr"),
            "cost_est_inr": r.get("cost_est_inr"),
            "realized_pnl": r.get("realized_pnl"),
            "exit_reason": r.get("exit_reason"), "outcome": r.get("outcome"),
            "lots": r.get("lots"), "opened": opened, "closed": closed,
            "peak_pnl": r.get("peak_pnl"), "peak_min": r.get("peak_min"),
            "trough_pnl": r.get("trough_pnl"), "trough_min": r.get("trough_min"),
            "entry_spread": r.get("entry_spread"), "exit_spread": r.get("exit_spread"),
            "be_spread": r.get("be_spread"), "ex_spread": r.get("ex_spread"),
            "tp_spread": r.get("tp_spread"), "sl_spread": r.get("sl_spread"),
            "notional_inr": r.get("notional_inr"),
            "entry_cross_spread": r.get("entry_crossing_spread"),
            "entry_cross_inr": r.get("entry_crossing_inr"),
            "entry_slip_spread": r.get("entry_slippage_spread"),
            "entry_slip_inr": r.get("entry_slippage_inr"),
            "exit_cross_spread": r.get("exit_crossing_spread"),
            "exit_cross_inr": r.get("exit_crossing_inr"),
            "exit_slip_spread": r.get("exit_slippage_spread"),
            "exit_slip_inr": r.get("exit_slippage_inr"),
        }
        vals = [row[c] for c in _REVIEW_COLS]
        ph = ", ".join("?" for _ in _REVIEW_COLS)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO trade_review "
                f"({', '.join(_REVIEW_COLS)}) VALUES ({ph})", vals)
            self._conn.commit()

    def log_sd_touch(self, ts, asset, sd_level, direction, zscore, spread) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO sd_touches VALUES (?,?,?,?,?,?)",
                               (str(ts), asset, int(sd_level), direction,
                                zscore, spread))
            self._conn.commit()

    def log_shadow(self, shadow: Dict[str, Any]) -> None:
        s = shadow or {}
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.get("position_id"), s.get("asset"), s.get("exit_reason"),
                 s.get("exit_pnl"), s.get("what_if_net"), s.get("peak"),
                 s.get("trough"), s.get("hit_be_min"), s.get("hit_tp_min"),
                 s.get("horizon_min"), s.get("verdict"), s.get("completed")))
            self._conn.commit()

    def log_market_data(self, ts, asset, spread, series_key, zscore=None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO market_data VALUES (?,?,?,?,?)",
                               (float(ts), asset, spread, zscore, series_key))
            self._conn.commit()

    def save_position_state(self, position_id, asset, state_json, updated) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO position_state VALUES (?,?,?,?)",
                (position_id, asset, state_json, str(updated)))
            self._conn.commit()

    def clear_position_state(self, position_id) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM position_state WHERE position_id=?",
                               (position_id,))
            self._conn.commit()

    def log_untracked_close(self, ts, leg, symbol, ticket, volume, price, note) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO untracked_closes VALUES (?,?,?,?,?,?,?)",
                               (str(ts), leg, symbol, str(ticket), volume, price, note))
            self._conn.commit()

    # ── reads ─────────────────────────────────────────────────────────────────
    def _rows(self, sql, args=()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def recent_reviews(self, limit=50) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM trade_review ORDER BY closed DESC "
                          "LIMIT ?", (limit,))

    def recent_shadows(self, limit=50) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM shadow_trades ORDER BY completed DESC "
                          "LIMIT ?", (limit,))

    def sd_touch_rows(self, asset=None, limit=200) -> List[Dict[str, Any]]:
        if asset:
            return self._rows("SELECT * FROM sd_touches WHERE asset=? ORDER BY "
                              "ts DESC LIMIT ?", (asset, limit))
        return self._rows("SELECT * FROM sd_touches ORDER BY ts DESC LIMIT ?", (limit,))

    def load_open_position_states(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM position_state")

    def recent_spreads(self, asset, series_key, since) -> List[tuple]:
        """Warm-start seed: (ts, spread) for an asset's series since a cutoff.
        series_key must match EXACTLY so a different symbol/β never seeds the
        window (the multi-asset equivalent of the single-pair guard)."""
        rows = self._rows("SELECT ts, spread FROM market_data WHERE asset=? AND "
                          "series_key=? AND ts>=? ORDER BY ts", (asset, series_key, float(since)))
        return [(r["ts"], r["spread"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
