# Arrow StatArb

A focused, **Arrow-only** statistical-arbitrage spread trader for the Indian
markets. It trades a 2-leg spread (e.g. a NIFTY calendar: buy near-month / sell
far-month futures) two ways:

* **Manual** — a web dashboard with an instrument picker, live prices, the live
  spread/z signal, current position + P&L, and Execute/Close buttons.
* **Algo** — a server-side auto-trader that enters/exits on the same server-side
  z-score, gated by an OU win-probability / expected-value filter and a
  half-life time-stop.

Everything is config-driven (`config/settings.yaml`) — nothing is hardcoded.
The broker layer is generic (`brokers/base_broker.py` + a registry), but only
**Arrow** (SDK `pyarrow-client`) is implemented today; others (Kotak, etc.) can
be added later with one registry entry.

## Architecture

```
arrow_statarb/
  brokers/
    base_broker.py     generic interface (connect, orders, quotes, stream, picker)
    arrow_broker.py    Arrow implementation (the crown jewel — see the 11 facts)
    registry.py        name → broker class; one active broker at a time
  models/
    spread_calculator.py   rolling spread, z, half-life
    signal_generator.py    z-threshold entry/exit/stop signals
    probability_filter.py  OU gambler's-ruin win-prob + EV/cost gate
  core/
    signal.py          SignalEngine — the SINGLE source of truth for spread/z
    algo.py            ArrowAutoTrader — server-side executor (+ EV gate, time-stop)
  config/config.py     settings loader + dry-run/live fail-safe
  web/app.py           Flask + SocketIO; setup + dashboard + all APIs
  web/templates/       base.html, setup.html, dashboard.html
config/settings.yaml   all tunables
scripts/arrow_test_trade.py   controlled live-order tester for the static-IP VM
run_arrow.py           launches the web app
tests/                 offline verification (mocked SDK — no network/creds)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in your Arrow credentials
python run_arrow.py           # http://localhost:5000
```

### Credentials (environment variables)

Arrow credentials are **never** stored in YAML. Set them in `.env` (loaded
automatically) or the shell:

```
ARROW_APP_ID, ARROW_USER_ID, ARROW_PASSWORD, ARROW_API_SECRET, ARROW_TOTP_SECRET
```

`ARROW_TOTP_SECRET` is the **base32 seed** from your authenticator setup, not the
6-digit code. The host must have its **static IP registered with Arrow** (SEBI
rule) — this won't work from an arbitrary machine.

### Usage

1. Open **Setup** (`/`). Connect Arrow (or rely on the env vars). Wait for the
   instrument master to load.
2. Pick a segment → underlying → contract for **Leg A** and **Leg B**, set
   ratios, **Save**.
3. Open **Dashboard** (`/dashboard`). Watch live prices, the server z-score,
   and your position. Trade manually, or flip **Algorithm Active**.
4. The mode badge shows **DRY-RUN** (default) or **LIVE**. DRY-RUN never
   transmits orders. Verify in DRY-RUN first, then 1 lot live.

### Verifying live orders

Use the controlled tester on your static-IP VM before trusting the dashboard:

```bash
# read-only
python scripts/arrow_test_trade.py --symbol NIFTY30JUN26F --segment nse_fo
# resting limit (placed then cancelled)
python scripts/arrow_test_trade.py --symbol NIFTY30JUN26F --segment nse_fo --place-limit
# REAL market order (guarded)
python scripts/arrow_test_trade.py --symbol NIFTY30JUN26F --segment nse_fo --side buy --place-market --yes
```

## Tests

```bash
pytest        # 53 offline tests; a fake pyarrow_client SDK stands in for Arrow
```

The suite covers instrument parsing (TitleCase / gzip / JSON / CSV), picker
grouping + chronological sorting, lot resolution, order kwargs (incl. `mpp`),
paise→rupee conversion, the z-score lifecycle (entry → hold → exit → stop), the
EV / time-stop gates, the SignalEngine window, and the dry-run vs live gate.

## Signal convention

```
spread = leg_a - leg_b ;  z = (spread - mean) / std
z ≤ -entry  → LONG_SPREAD  (buy A / sell B)
z ≥ +entry  → SHORT_SPREAD (sell A / buy B)
exit when z reverts through exit_z ;  stop when |z| ≥ stop_z
```

The **same** server-side `SignalEngine` feeds both the dashboard (`/api/signal`)
and the algo, so the z they show/act on is always identical.

---

## Migration note — the 11 hard-won Arrow facts

These were learned the hard way against the live Arrow API. They are the
acceptance criteria for the broker layer; `tests/` verifies each offline.

1. **SDK / auth.** `pip install pyarrow-client`, `import pyarrow_client`.
   `ArrowClient(app_id=...)`; `client.auto_login(user_id, password, api_secret,
   totp_secret)` — the installed SDK (1.4.0) parameter is `api_secret` (the docs
   say `app_secret`, which is wrong). Token valid ~24h; reconnect daily. TOTP is
   the base32 seed, not the 6-digit code. Arrow requires a registered **static
   IP** (SEBI) and has **no paper/sandbox** — "dry-run" is a local gate that
   simply does not transmit orders.
2. **Instrument master.** `client.get_instruments()` hits `/all` and returns
   **octet-stream bytes**, not a list. Decode: if gzip magic (`\x1f\x8b`),
   decompress, then sniff JSON vs CSV. ~223k rows.
3. **TitleCase schema.** `ExchSeg` (NSECM/NSEFO/BSECM/BSEFO — the real
   exchange+segment; `Exchange` is just "NSE"), `Symbol` (underlying),
   `TradingSymbol` (the tradeable/order symbol, e.g. `NIFTY30JUN26F`),
   `OptionType` (CE/PE; empty = future), `StrikePrice`, `Expiry`
   (`30-Jun-2026`), `LotSize` (varies per expiry), `Token` (int), `Underlying`,
   `Series`. Read all fields **case-insensitively**.
4. **Picker index (built once).** Group by `ExchSeg` + kind — `cash` (ExchSeg
   ends with CM) / `option` (OptionType in CE,PE) / `future` (FO and not
   option); underlying from `Symbol`/`Underlying`; contract symbol from
   `TradingSymbol`; sort chronologically with a robust expiry parser (ISO
   `2025-06-26`, `DDMonYY` `30JUN26`, `DD-Mon-YYYY`, epoch).
5. **Lot size.** Index by **`TradingSymbol`** (not the underlying — the EQ row
   would poison it). `resolve_lot_size` exact-matches first, then strips
   expiry/FUT tokens iteratively.
6. **Orders.** `place_order(exchange=Exchange.<NFO|NSE|BSE|BFO>,
   symbol=<TradingSymbol>, quantity=<lots×lot_size>, product=ProductType.NRML
   ('M'), order_type=OrderType.MKT, variety=REGULAR,
   transaction_type=BUY/SELL, price=0, validity=DAY, mpp=True)`. Plain MKT is
   disabled on Arrow — you **must** pass `mpp=True` and `price=0` for market
   orders or they're rejected. `quantity` is **units** (lots × lot_size). **MCX
   is not supported** — reject it.
7. **Quotes.** `client.get_quotes(QuoteMode.LTP, [(symbol, Exchange.<...>)])` →
   list of dicts with TitleCase keys (`TradingSymbol`, `Ltp`); parse
   case-insensitively.
8. **WebSocket feed (~50ms).** `ArrowStreams(appID, token)`,
   `streams.connect_data_stream()`, `streams.data_stream.on_ticks = cb`,
   `streams.subscribe_market_data(DataMode.LTP, [<int Token>...])`. `tick.token`
   (int), `tick.ltp`. Prices arrive in **paise** → divide by 100 for rupees.
9. **Positions.** `client.get_positions()` is TitleCase too (`TradingSymbol`,
   `NetQty`, `AvgPrice`, `Ltp`, `Pnl`) — parse case-insensitively; expose net
   qty / avg / ltp / pnl.
10. **Spread/signal convention.** `spread = leg_a − leg_b`; `z =
    (spread−mean)/std`. `z ≤ −entry` ⇒ LONG_SPREAD (buy A / sell B); `z ≥
    +entry` ⇒ SHORT_SPREAD (sell A / buy B). Exit when z reverts through
    `exit_z`; stop when `|z| ≥ stop_z`.
11. **Lot-size mismatch.** NSE revises lot sizes per expiry (e.g. 75 vs 65), so
    a calendar spread with 1 lot each is **not** share-neutral. The dashboard
    surfaces this as a warning.
