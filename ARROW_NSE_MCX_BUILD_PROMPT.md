# Build Prompt — NSE/MCX Market-Neutral Basis Mean-Reversion (Arrow broker)

> Adapts the comprehensive crypto stat-arb system (`Stat_Arb_W3_Wsckt`) to Indian
> NSE + MCX futures via the **Arrow** broker. The STRATEGY, dashboards, settings,
> analysis, backtest and intelligence layers are ported **verbatim** — they are
> venue-agnostic. Only the **venue, instruments, and cost model** change. Every
> "WHY" note in the source `PORTABLE_STRATEGY_PROMPT.md` is load-bearing and is
> inherited unchanged; this document specifies ONLY the deltas.

---

## 0. Target parameters

- PLATFORM: Python (Flask + SocketIO, waitress), same as source.
- BROKER: **Arrow** (pyarrow-client SDK). Segments: **NSE cash (NSECM), NSE F&O
  (NSEFO), MCX (MCXFO)**. No crypto exchanges.
- STRATEGY: same-underlying **basis mean-reversion**. `Spread = k·P_a − P_b`
  where leg_b is the CONTRACT (future) leg and `k` is the hedge ratio.
- PAIR FAMILIES (config-driven, the underlying keeps changing):
  1. **Cash/ETF vs Future** — e.g. NIFTYBEES vs NIFTY future, HDFCBANK cash vs
     HDFCBANK future. `k ≈ future ÷ ETF` (or ≈1 for cash-future).
  2. **MCX calendar / inter-commodity** — near vs far future, same commodity.
- LOOKBACK: **configurable, default ~2 hours** (rolling window in minutes, not
  ticks). Warm-up gate before any trade.
- TICK CADENCE: 0.5s sampling (Arrow LTP cache), 500ms dashboard refresh.

---

## 1. Strategy (inherited verbatim from PORTABLE_STRATEGY_PROMPT.md)

Trade the spread between two cointegrated same-underlying instruments; enter on a
statistically stretched z, exit on **net dollar profit after all costs** (z earns
entries, dollars govern everything after). Port these sections **unchanged**:
§2 signals, §3 entry gates, §4 lattice co-sizing, §5 true-fill per-leg accounting,
§6 exit ladder (dollar stop → TP → gated reversion → max-hold → demoted z-stop;
gate-decay deadlock fix; cost-floor-redundant-for-%-capital fix), §7 execution,
§8 reconciliation + resilience, §9 config principles, §10 observability, §11
acceptance tests, §12 hard warnings. **Do not re-derive or "simplify" these.**

The Indian re-derivations below REPLACE the crypto porting notes in §0 of the
source.

---

## 2. RE-DERIVE for NSE/MCX (never copy the crypto values)

1. **Basis reversion must exist on the chosen pair.** Cash/future and ETF/future
   basis is anchored by cost-of-carry and **converges to 0 at expiry** — strongly
   mean-reverting intraday around fair carry. MCX calendars revert around the
   storage/carry differential. Verify reversion (ADF on the spread) per pair.
2. **Hedge ratio `k` is structural and slowly drifting** (§ our design):
   `k = P_b/P_a ≈ units-scale × carry`. Nearly constant intraday; re-derive
   **daily / on rollover**, NEVER per tick (per-tick `k=P_b/P_a` zeroes the
   spread). Block `k` changes while a position is open. Warn if live `P_b/P_a`
   drifts >2% from the pinned `k` (stale or dislocated).
3. **Per-leg lot sizes & tick sizes per exchange** — NSE F&O, NSE cash (1),
   ETF (1), MCX (commodity-specific). Broker-resolved with config override. Lot
   matching: 1 future lot = `lot_size_b` units; leg_a sized to `lots × lot_size_b
   × k`. (Already built in arrow-statarb.)
4. **REAL Indian costs from a contract note, not the rate card** (§3.3 lesson).
   See §3 — this is the single biggest delta. Per-SEGMENT, per-SIDE.
5. **Arrow order semantics**: NRML product, LIMIT priced off LTP with capped
   offset (slippage source), tick-size rounding (Arrow rejects off-tick),
   limit→market escalation on timeout, verify-flat pre-entry, orphan recovery.
   No post-only/maker-taker, no RFQ. (Already built.)
6. **IST sessions + expiry**: NSE 09:15–15:30; MCX ~09:00–23:30/23:55. No-entry
   buffer before close. **Do not hold across expiry** — roll or flatten; the
   future settles. Configurable trading-hours + close buffer per segment.

---

## 3. Cost model — the big Indian delta (replaces maker/taker/funding)

There is **no funding rate** and **no maker/taker**. Instead a per-segment,
per-side stack of statutory + broker charges, all as % of notional unless flat.
Make every rate **configurable** and **calibrate to a real contract note**
(inflated costs break the edge filter and cost floor — §6.2 of source).

Round-trip cost = Σ over the 4 leg-fills of: brokerage + txn + GST + SEBI + stamp,
plus STT/CTT on the sells only. **Per-leg** (legs are different instruments):

| Charge | NSE cash (delivery) | NSE cash (intraday) | ETF (delivery) | NSE F&O future | MCX future (non-agri) |
|---|---|---|---|---|---|
| **STT / CTT** (sell) | 0.1% | 0.025% | **0.001%** | **0.02%** | **CTT 0.01%** |
| STT (buy) | 0.1% | — | — | — | — |
| Brokerage | flat ₹20/order (Arrow) | ₹20 | ₹20 | ₹20 | ₹20 |
| Exchange txn | ~0.00297% | ~0.00297% | ~0.00297% | ~0.0019% | ~0.0021% |
| GST | 18% on (brokerage+txn+SEBI) | ″ | ″ | ″ | ″ |
| SEBI | 0.0001% | 0.0001% | 0.0001% | 0.0001% | 0.0001% |
| Stamp (buy) | 0.015% | 0.003% | 0.015% | 0.002% | 0.002% |
| DP charges (sell, delivery) | ~₹13–25/scrip | — | ~₹13–25 | — | — |

Notes:
- These are **defaults for calibration**, not gospel — rates change (STT on
  futures was raised to 0.02% in 2024; verify current). Surface a **cost-audit**
  (modeled vs realized from the Arrow contract note) exactly like the source's
  fee-split audit; alarm if modeled ≥ 2× realized.
- **Capital-Gains / business-income tax**: F&O is business income (slab); apply
  as a haircut on positive net profit only, per-trade approximation (already
  built as `capital_gains_pct`).
- The source's cost-floor lesson stands: with a %-of-capital or σ-fraction target
  (already fee-net), set `cost_floor_mult = 0`; keep the floor only for a raw
  fixed-₹ target.

---

## 4. What to PRESERVE from W3_Wsckt (port verbatim — this is the "comprehensive" the user wants)

- **Dashboard** (templates/dashboard.html): every panel — live spread/z chart,
  position card with BE/EX/TP/SL levels + net ₹, per-leg change %, age vs
  max-hold; signal card; trade journal; SD-touch analysis; expectancy sheet
  (win rate, R:R, PF, break-even WR, EV/R); regime/drift state; untracked-close
  badge; daily-loss vs limit; hedge-ratio z vs morning anchor.
- **Settings** (templates/settings.html): the full knob set — entry/exit/stop z,
  lookback, stats interval, edge filter, all exit-ladder forms, %-capital
  stop/target, cost floor, regime, reconcile, cooldowns, loss-streak — plus the
  new **cost-model section** (§3) and **hedge-ratio** field.
- **Analysis** (templates/analysis.html) + the intelligence layer:
  `analytics.py`, `drift_analyzer.py`, `post_trade_analyzer.py`, `auto_tuner.py`,
  `ai_monitor.py` — outcome tags, capture/cost ratios, what-if-held shadow,
  lifecycle-extreme percentiles that set take/hold, config-coherence audit.
- **Backtest suite**: `pair_scanner.py`, `signal_replay.py`, `basket_simulator.py`,
  `run_backtest.py`, `fetch_history.py` — re-pointed at Arrow/NSE/MCX history.
- **Resilience**: WS-break-on-timeout, liveness heartbeat, external watchdog,
  crash-safe closes, DB recovery + reconcile-on-restart.
- Optional: Telegram remote tracking/`/restart` (keep; retarget to Arrow).

## 5. What to REMOVE / REPLACE (crypto-only)

- **Remove**: funding-rate arbitrage, RFQ block trading (`rfq_executor.py`,
  okx_rfq_*), maker/taker + VIP fee tiers (`vip.py`), OKX/Binance/Bybit adapters,
  perp/spot-swap symbology, `USE_WEBSOCKET` OKX socket.
- **Replace**: the adapter layer with a single **Arrow adapter** (NSE + MCX) —
  reuse the one already built in arrow-statarb; the fee model with §3; the
  instrument model with cash/ETF/future + MCX contracts; lookback-in-ticks with
  **lookback-in-minutes (configurable, default 120)**.

## 6. Reuse from arrow-statarb (already built + tested)

- Arrow broker integration + segment/exchange enums.
- Full Indian cost model (`_round_trip_cost`, `_live_net_pnl` with STT/other/CGT;
  per-leg STT).
- **Non-1:1 pairs mode** (hedge ratio in spread + sizing + per-leg STT; auto-derive).
- Configurable collection window (2h) + warm-up gate.
- Self-healing reconcile + untracked-close ledger; slippage-abort guard.

## 7. Acceptance tests

Port §11 of the source verbatim (all 20 caught real bugs), plus new Indian tests:
per-segment cost stack reconciles to a contract note to the ₹; hedge-ratio order
sizing (equal exposure); no-per-tick-k regression; expiry no-hold guard; MCX vs
NSE segment routing; ETF STT (0.001%) vs future STT (0.02%) per leg.
