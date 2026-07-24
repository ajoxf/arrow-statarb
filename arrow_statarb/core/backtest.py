"""Backtest harness — replays historical leg prices through the SAME signal and
algo logic the live system uses, so results reflect real behaviour rather than a
re-implementation.

Each bar (timestamp, leg_a price, leg_b price) is pushed into a ``SignalEngine``;
the ``ArrowAutoTrader`` is ticked with an injected clock set to the bar time, so
cooldown / time-stop / holding-time all use historical time. Entries and exits
fill at the bar prices adjusted for slippage, and P&L is settled by the real
``TradeLog`` (Δspread × lots × lot_size − brokerage). Output is a metrics dict:
total / annualized return, win rate, drawdown, exit-reason mix, per-trade stats.

No prediction is implied — the backtest only measures what the strategy WOULD
have done on the data you feed it. Data quality and realistic slippage decide
whether the numbers mean anything.
"""

from __future__ import annotations

import math
import random
from typing import Dict, Iterable, List, Optional, Tuple

from arrow_statarb.core.signal import SignalEngine
from arrow_statarb.core.algo import ArrowAutoTrader
from arrow_statarb.core.trade_log import TradeLog

Bar = Tuple[float, float, float]   # (ts, leg_a, leg_b)


class Backtester:
    def __init__(
        self,
        *,
        signal_params: Optional[Dict] = None,
        strategy_params: Optional[Dict] = None,
        lot_size: int = 65,
        brokerage_per_lot: float = 20.0,
        slippage_per_lot: float = 5.0,
        lots: int = 1,
        capital: Optional[float] = None,
        hedge_ratio: float = 1.0,
        stt_pct: float = 0.0,
        other_cost_pct: float = 0.0,
        capital_gains_pct: float = 0.0,
        stt_a_pct: Optional[float] = None,
        stt_b_pct: Optional[float] = None,
    ):
        self.signal_params = dict(signal_params or {})
        self.strategy_params = dict(strategy_params or {})
        self.strategy_params.setdefault("lots", lots)
        self.lot_size = int(lot_size)
        self.brokerage_per_lot = float(brokerage_per_lot)
        self.slippage_per_lot = float(slippage_per_lot)
        self.lots = int(lots)
        self.capital = capital
        # Non-1:1 pairs: scale leg_a by k so the settled spread matches the signal.
        self.hedge_ratio = float(hedge_ratio)
        self.signal_params.setdefault("hedge_ratio", self.hedge_ratio)
        # Indian costs applied at settlement (STT/CTT + other charges + CGT), so
        # the backtest P&L reflects real net, not just brokerage. 0 = off.
        self.stt_pct = float(stt_pct)
        self.other_cost_pct = float(other_cost_pct)
        self.capital_gains_pct = float(capital_gains_pct)
        self.stt_a_pct = stt_a_pct
        self.stt_b_pct = stt_b_pct

    # ── fills (slippage applied adversely, per leg, in ₹/unit) ────────────────
    def _fill(self, direction: str, a: float, b: float, opening: bool) -> Tuple[float, float]:
        slip = (self.slippage_per_lot / self.lot_size) if self.lot_size else 0.0
        # LONG_SPREAD = buy A / sell B; closing reverses the legs.
        buy_a = (direction == "LONG_SPREAD") if opening else (direction == "SHORT_SPREAD")
        a_fill = a + slip if buy_a else a - slip
        b_fill = b - slip if buy_a else b + slip
        return round(a_fill, 4), round(b_fill, 4)

    def run(self, bars: Iterable[Bar]) -> Dict:
        bars = [(float(ts), float(a), float(b)) for ts, a, b in bars]
        if len(bars) < 2:
            return {"error": "need at least 2 bars"}

        eng = SignalEngine(prices_provider=lambda: (None, None),
                           params_provider=lambda: self.signal_params)
        tlog = TradeLog(path=None, brokerage_per_lot=self.brokerage_per_lot)
        st = {"ts": bars[0][0], "a": bars[0][1], "b": bars[0][2]}
        holds: List[float] = []          # holding time per trade, in BAR seconds
        entry_ts = {"t": None}

        def _record(action: str, direction: str, lots: int, opening: bool,
                    z, reason: str = "") -> None:
            a_fill, b_fill = self._fill(direction, st["a"], st["b"], opening)
            if opening:
                entry_ts["t"] = st["ts"]
            elif entry_ts["t"] is not None:
                holds.append(st["ts"] - entry_ts["t"])
                entry_ts["t"] = None
            # Settle on the hedge-scaled spread (k×a − b) so the P&L matches the
            # z the algo traded, and apply the Indian cost stack on close.
            tlog.record(action=action, direction=direction, lots=lots,
                        spread=round(self.hedge_ratio * a_fill - b_fill, 4),
                        dry_run=True, status="BACKTEST",
                        source="algo", lot_size=self.lot_size, zscore=z,
                        leg_a_price=a_fill, leg_b_price=b_fill, name="backtest",
                        exit_reason=reason,
                        stt_pct=self.stt_pct, other_cost_pct=self.other_cost_pct,
                        capital_gains_pct=self.capital_gains_pct,
                        stt_a_pct=self.stt_a_pct, stt_b_pct=self.stt_b_pct)

        def execute_fn(direction, lots, source=None, z=None, spread=None, **_):
            _record("OPEN", direction, lots, opening=True, z=z)
            return {"success": True, "dry_run": True, "results": []}

        def close_fn(direction, lots, source=None, reason=None, z=None, spread=None, **_):
            _record("CLOSE", direction, lots, opening=False, z=z, reason=reason or "")
            return {"success": True, "results": []}

        algo = ArrowAutoTrader(signal_provider=eng.get_signal,
                               params_provider=lambda: self.strategy_params,
                               execute_fn=execute_fn, close_fn=close_fn,
                               clock=lambda: st["ts"])

        for ts, a, b in bars:
            st.update(ts=ts, a=a, b=b)
            eng.push(a, b, ts=ts)
            algo._tick()

        if algo._pos is not None:        # force-close any open position at the last bar
            pos = algo._pos
            close_fn(pos["direction"], pos["lots"], reason="end",
                     z=eng.get_signal().get("zscore"))

        return self._metrics(tlog, bars, holds)

    # ── metrics ───────────────────────────────────────────────────────────────
    def _metrics(self, tlog: TradeLog, bars: List[Bar], holds: List[float]) -> Dict:
        trips = list(reversed(tlog.round_trips()["trips"]))   # chronological
        pnls = [float(t.get("net_pnl", 0) or 0) for t in trips]
        n = len(trips)
        wins = sum(1 for p in pnls if p > 0)
        total = round(sum(pnls), 2)

        equity, cum, peak, max_dd = [], 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            equity.append(round(cum, 2))
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        reasons: Dict[str, int] = {}
        for t in trips:
            k = t.get("exit_reason") or "—"
            reasons[k] = reasons.get(k, 0) + 1

        span_days = max((bars[-1][0] - bars[0][0]) / 86400.0, 1e-9)
        avg_notional = (sum(0.5 * (b[1] + b[2]) for b in bars) / len(bars)) * self.lot_size * self.lots

        # Dead-market check (reference §12's hardest truth): fraction of trades
        # whose GROSS move was below the round-trip cost. High ⇒ trading toll-
        # sized wiggles that just donate the toll — a selection/regime problem,
        # not a tuning one.
        below = 0
        for t in trips:
            gross = abs(float(t.get("spread_pnl", 0) or 0))
            cost = float(t.get("spread_pnl", 0) or 0) - float(t.get("net_pnl", 0) or 0)
            if gross < cost:
                below += 1

        out = {
            "bars": len(bars), "span_days": round(span_days, 2),
            "trades": n, "wins": wins,
            "win_rate_pct": round(100.0 * wins / n, 1) if n else 0.0,
            "total_pnl": total,
            "avg_pnl_per_trade": round(total / n, 2) if n else 0.0,
            "best": round(max(pnls), 2) if pnls else 0.0,
            "worst": round(min(pnls), 2) if pnls else 0.0,
            "pnl_std": round(_std(pnls), 2),
            "max_drawdown": round(max_dd, 2),
            "avg_held_sec": round(sum(holds) / len(holds), 1) if holds else None,
            "exit_reasons": reasons,
            "avg_notional": round(avg_notional, 2),
            "equity_curve": equity,
            "return_on_notional_pct": round(100.0 * total / avg_notional, 3) if avg_notional else None,
            "below_cost_trades": below,
            "below_cost_pct": round(100.0 * below / n, 1) if n else 0.0,
            # the book on one sheet, in R (win rate, R:R, PF, break-even WR, EV/R)
            "expectancy": tlog.expectancy(),
        }
        if self.capital:
            roi = 100.0 * total / self.capital
            out["capital"] = self.capital
            out["roi_pct"] = round(roi, 3)
            out["annualized_roi_pct"] = round(roi * 365.0 / span_days, 2)
        return out


def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def synthetic_ou(n: int = 5000, *, base_a: float = 24000.0, spread_mean: float = -85.0,
                 theta: float = 0.02, sigma: float = 1.5, dt_sec: float = 0.5,
                 start_ts: float = 0.0, seed: Optional[int] = None) -> List[Bar]:
    """Mean-reverting (Ornstein-Uhlenbeck) spread bars for sanity-testing the
    harness. NOT real data — for plumbing checks and demos only."""
    rng = random.Random(seed)
    bars: List[Bar] = []
    a = base_a
    spread = spread_mean
    for i in range(n):
        a += rng.gauss(0, 0.5)
        spread += theta * (spread_mean - spread) + rng.gauss(0, sigma)
        b = a - spread
        bars.append((start_ts + i * dt_sec, round(a, 2), round(b, 2)))
    return bars
