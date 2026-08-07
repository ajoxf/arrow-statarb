"""Multi-asset stat-arb engine loop.

Runs N asset pairs concurrently on ONE broker connection (Arrow needs no
per-account processes). Each asset owns its SpreadStats, position and frozen
exit plan; the shared ZSignalGenerator keys gate-state by asset. Every tick,
per asset: update stats → evaluate EXIT first (risk before opportunity) →
else evaluate ENTRY.

All I/O is injected (price_fn / execute_fn / close_fn / edge_fn / cost_fn), so
the whole lifecycle is testable without a broker — the same pattern as the
existing single-pair ArrowAutoTrader. This is an additive engine; it does not
replace the running single-pair loop until a later wiring phase.

Directions use the exits.py strings: BUY_BASIS = long the spread (profit when it
rises), SELL_BASIS = short (profit when it falls).
"""

from __future__ import annotations

import logging
import time as time_mod

from .spread import SpreadStats
from .signals import ZSignalGenerator
from .exits import ExitLadder, SELL_BASIS, BUY_BASIS, _dir
from . import sizing as sizing_mod

logger = logging.getLogger(__name__)


class AssetState:
    def __init__(self, key, cfg, stats):
        self.key = key
        self.cfg = cfg                  # {contract_a, contract_b, pair_type, ...}
        self.stats = stats
        self.position = None            # dict once open


class MultiAssetEngine:
    def __init__(self, assets, params_provider, price_fn, execute_fn, close_fn,
                 edge_fn=None, cost_fn=None, clock=time_mod.time):
        self.params = params_provider
        self.price_fn = price_fn
        self.execute_fn = execute_fn
        self.close_fn = close_fn
        self.edge_fn = edge_fn
        self.cost_fn = cost_fn or (lambda key, plan, md: 0.0)
        self.clock = clock
        self.algo_enabled = False
        signals_cfg = self._views()[1]
        self.signals = ZSignalGenerator(signals_cfg, clock)
        self.assets = {k: AssetState(k, cfg, SpreadStats(signals_cfg, clock))
                       for k, cfg in assets.items()}
        self.ladders = {k: ExitLadder({}, {}) for k in assets}

    # ── config views (re-read each tick so Settings hot-apply) ────────────────
    def _views(self):
        p = self.params() or {}
        exits = p.get("EXITS", p)
        signals = p.get("SIGNALS", p)
        trading = p.get("TRADING", p)
        tf = float((p.get("COSTS", {}) or {}).get("TARGET_FRACTION",
                                                  p.get("TARGET_FRACTION", 0.5)))
        return exits, signals, trading, tf

    def start(self):
        self.algo_enabled = True

    def stop(self):
        self.algo_enabled = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def tick(self):
        exits_cfg, signals_cfg, trading_cfg, tf = self._views()
        self.signals.cfg = signals_cfg
        for key, st in self.assets.items():
            md = self.price_fn(key)
            if not md or md.get("spread") is None:
                continue
            st.stats.cfg = signals_cfg
            st.stats.update(md["spread"], md.get("quote_id"))
            z = st.stats.z
            self.signals.update(key, z)
            ladder = self.ladders[key]
            ladder.exits, ladder.signals, ladder.target_fraction = (
                exits_cfg, signals_cfg, tf)
            if st.position is not None:
                self._maybe_exit(key, st, md, z, ladder)
            elif self.algo_enabled:
                self._maybe_enter(key, st, md, z, ladder, trading_cfg)

    @staticmethod
    def _gross(position, spread):
        """₹ mark-to-market: Δspread × k, signed by direction."""
        k = position["k"]
        d = position["direction"]
        if d == BUY_BASIS:
            return (spread - position["entry_spread"]) * k
        return (position["entry_spread"] - spread) * k

    def _maybe_enter(self, key, st, md, z, ladder, trading_cfg):
        contract_a = st.cfg.get("contract_a", 1)
        contract_b = st.cfg.get("contract_b", 1)
        direction = self.signals.entry_signal(
            key, st.stats, md, {}, lots=trading_cfg.get("CLIP_LOTS", 1),
            contract_size=contract_b, edge_fn=self.edge_fn)
        if direction is None:
            return

        params = dict(trading_cfg)
        params.setdefault("HEDGE_RATIO", st.cfg.get("hedge_ratio", 1.0))
        size = sizing_mod.plan(params, contract_a, contract_b,
                               md.get("price_a"), md.get("price_b"),
                               meta_a=st.cfg.get("meta_a"),
                               meta_b=st.cfg.get("meta_b"))
        if size.get("reason"):
            logger.info("%s: entry refused — %s", key, size["reason"])
            return
        lots_b = size["leg_b_lots"]
        k = size["spread_units"]
        if lots_b <= 0 or k <= 0:
            return

        rt_cost = self.cost_fn(key, size, md)
        capital = st.cfg.get("capital_at_risk_inr") or params.get(
            "CAPITAL_AT_RISK_INR")
        plan = ladder.build_plan(lots=lots_b, contract_size=contract_b,
                                 entry_z=z, sigma=st.stats.sigma,
                                 half_life_sec=st.stats.half_life_sec,
                                 rt_cost=rt_cost, capital=capital,
                                 entry_mu=st.stats.mu)
        if plan is None:
            logger.info("%s: entry blocked — not viable after cost floor", key)
            return

        res = self.execute_fn(key, direction, size) or {}
        if not res.get("success"):
            return
        entry_spread = res.get("entry_spread", md["spread"])
        levels = ExitLadder.spread_levels(plan, entry_spread, k, direction)
        st.position = {
            "direction": direction, "entry_spread": entry_spread,
            "entry_z": z, "entry_sigma": st.stats.sigma, "entry_time": self.clock(),
            "plan": plan, "k": k, "lots_b": lots_b, "size": size,
            "spread_levels": levels, "peak_pnl": 0.0, "trough_pnl": 0.0,
            "peak_min": 0.0, "trough_min": 0.0,
        }
        logger.info("%s: OPEN %s @ spread %.4f (k=%.2f)", key, direction,
                    entry_spread, k)

    def _maybe_exit(self, key, st, md, z, ladder):
        pos = st.position
        spread = md["spread"]
        gross = self._gross(pos, spread)
        rt = pos["plan"].get("rt_cost_inr", 0.0)
        net = gross - rt
        age = self.clock() - pos["entry_time"]
        held_min = round(age / 60.0, 2)
        if net > pos["peak_pnl"]:
            pos["peak_pnl"], pos["peak_min"] = net, held_min
        if net < pos["trough_pnl"]:
            pos["trough_pnl"], pos["trough_min"] = net, held_min

        reason = ladder.evaluate(pos["direction"], key, pos["plan"], z, gross,
                                 age, spread)
        if not reason:
            return
        res = self.close_fn(key, reason) or {}
        if not res.get("success"):
            return
        self.signals.notify_close(key, reason, pos["direction"])
        logger.info("%s: CLOSE %s (net ₹%.2f, %s)", key, reason, net,
                    pos["direction"])
        st.position = None

    # ── snapshot for the UI ───────────────────────────────────────────────────
    def snapshot(self):
        out = {}
        for key, st in self.assets.items():
            pos = st.position
            gross = net = None
            if pos:
                md = self.price_fn(key) or {}
                if md.get("spread") is not None:
                    gross = self._gross(pos, md["spread"])
                    net = gross - pos["plan"].get("rt_cost_inr", 0.0)
            out[key] = {
                "z": st.stats.z, "spread": st.stats.last_value,
                "mu": st.stats.mu, "sigma": st.stats.sigma,
                "warm": st.stats.warm, "half_life_sec": st.stats.half_life_sec,
                "position": (pos["direction"] if pos else None),
                "entry_spread": (pos["entry_spread"] if pos else None),
                "net_pnl": net, "gross_pnl": gross,
                "levels": (pos["spread_levels"] if pos else None),
            }
        return out
