# Arrow Trader — build prompt

**A spread price-ladder trading terminal for MCX, on the Arrow broker.**

The screen is MT5-Trader's screen, unchanged. The engine underneath it
speaks to Arrow instead of to two MetaTrader 5 terminals. MCX first,
NSE F&O after — the exchange is a segment, not a rewrite.

This document is the brief for the whole build. Read it end to end
before writing a line, then read `MT5-Trader/` end to end, then start
at Phase 0.

**The complete screen inventory — every panel, what changes, what is
new — is a companion document: [`ARROW-TRADER-SCREENS.md`](ARROW-TRADER-SCREENS.md).
Read and sign off on that before Phase 7.**

---

## 0. The two rules that bound this work

1. **`ajoxf/MT5-Trader` is READ-ONLY.** It is a live, working product
   on live accounts. Read it, port from it, quote it in comments —
   never edit it, never open a PR against it, never "fix" something you
   find in it. If you find a real bug there, write it down in
   `docs/MT5-FINDINGS.md` in *this* repo and carry on.
2. **The UI is not a redesign.** The trader must not be able to tell
   the two apart by looking. Same window chrome, same taskbar, same
   five-column grid, same colours, same keys, same modal, same sounds,
   same wording — including the tooltips, which are where half the
   product's knowledge lives. Anything that must differ (a rupee sign,
   a lot size, a segment name) differs in the DATA, not in the layout.

---

## 1. What this is, in one paragraph

A human looks at a ladder of **spread** prices, clicks a price, and an
order exists at that price. No strategy, no signals, no automatic
entries or exits, nothing that re-enters by itself. Each ladder trades
one pair of MCX contracts — a GOLD calendar, SILVER near vs far,
CRUDEOIL vs CRUDEOILM — and displays the difference between them as a
single instrument, the way a CQG or TT inter-product ladder does. It is
a manual tool with very good instruments on the dashboard.

Everything the trader is protected by is a *guard*, and a guard may
withhold an ORDER but must never prevent a CLOSE.

---

## 2. The two source repositories

### `ajoxf/MT5-Trader` — the product

This is what you are porting. It is ~29k lines of Python plus a
self-hosted front end, and 262 tests. The parts, and what each one is
worth to you:

| Module | Port it? | Note |
|---|---|---|
| `mt5trader/static/`, `templates/index.html` | **Copy verbatim** | 4,016 lines of `app.js`, 1,178 of `settings.js`, 1,872 of `ladder.css`, 489 of `index.html`. This IS the deliverable's face. |
| `models.py` | Port, adapt | `SpreadSide`, `SyntheticOrder`, `SpreadPosition`, `LegFill`. The `position_tickets` concept changes — see §5.2. |
| `spread.py` | **Port unchanged** | `spread = P_B − β × P_A` from the MID OF THE BOOK; executable touches; `QuoteAgeTracker`, `SpreadJumpTracker`, `LevelSigma`. This is the maths and it is exchange-agnostic. |
| `sizing.py` | Port, re-unit | `k = L_B × C_B`; `clip_plan`. "Lots" becomes "lots", `contract_size` becomes `LotSize` (units per lot). See §5.3. |
| `book.py` | Port, adapt | One-click-one-order, `positions_to_reduce`, `reduce_first`. |
| `executor.py` | Port, adapt | MARKET entry both legs, the 2.0s escalation window, the unwind. The unwind CLOSES rather than offsets — on a netted account that becomes simpler, not harder. |
| `quoter.py` | Port, **simplify** | The synthetic LIMIT path. Arrow/MCX gives you real resting limits AND netting, so the "a closing limit opens a second position" disaster does not exist here. See §5.2. |
| `coordinator.py` | Port | The poll loop, the guards, the sweeps, `snapshot()`. The snapshot's shape is the UI's contract — do not change a key name. |
| `reconcile.py` | Port, rewrite internals | Orphans and ghosts against a NET position, not against tickets. |
| `book`/`database.py` | Port | SQLite WAL, crash-safe positions, fill journal, audit trail. |
| `session.py`, `shutdown.py` | Port, re-time | IST and MCX hours; MIS auto-square-off is a new event. |
| `slippage.py`, `costs.py`, `takeprofit.py`, `fairvalue.py`, `carry.py` | Port, re-model | Indian cost stack and Indian carry — see §5.6, §5.7. |
| `commands.py`, `ipc.py`, `webapp.py`, `atomicfile.py` | Port | Web↔engine bridge. `webapp.py` renders and asks; it never trades. |
| `broker.py`, `leg_runner.py`, `legs.py`, `mt5_errors.py` | **Replace** | This is the whole of the new work. See §4. |
| `tests/` | Port the intent, rewrite the fixtures | `FakeBroker` becomes `FakeArrow`. |

### `ajoxf/arrow-statarb` — the Arrow knowledge

This repo already knows how to talk to Arrow, and its README carries
**11 hard-won facts** learned against the live API. Treat that list as
acceptance criteria for the new broker layer. The parts you want:

- `arrow_statarb/brokers/arrow_broker.py` — auth (`auto_login`, the
  `api_secret` not `app_secret` trap), the instrument master (octet
  stream → gzip sniff → JSON/CSV, ~223k rows), the TitleCase schema,
  the picker index, `resolve_lot_size` keyed on `TradingSymbol`, the
  `mpp=True` market-order rule, the WebSocket feed and the **paise →
  rupee** divide-by-100.
- `arrow_statarb/brokers/base_broker.py` — the generic interface.
- `arrow_statarb/core/executor.py` — a two-leg executor with fill
  polling, limit amendment and orphan recovery. Read it for the
  Arrow-specific lifecycle; MT5-Trader's `executor.py` is the better
  design, so port THAT and take the Arrow mechanics from here.
- `tests/conftest.py` — the fake `pyarrow_client` SDK. Extend it; do
  not start a new one.

**Do not port the strategy.** `models/`, `core/signal.py`,
`core/algo.py`, `probability_filter.py`, the z-score, the OU filter and
the auto-trader are exactly what this product does not have. Leave them
where they are.

---

## 3. Blockers to clear BEFORE Phase 1

These three are not implementation details. Each one can invalidate the
plan, and each one needs an answer from Arrow or from the desk. Get
them answered first and write the answers into this file.

### 3.1 Does Arrow support MCX at all?

`arrow_statarb/brokers/arrow_broker.py:749` refuses MCX outright:

```python
if exchange_segment.lower() in ("mcx_fo", "mcx"):
    return {"order_id": None, "status": "error",
            "message": "Arrow does not support MCX."}
```

and `_SEGMENT_MAP` has no MCX entry. The README states it as fact #6.
**The entire brief is MCX-first, so this is blocker zero.**

What to do, in order:
1. Ask Arrow whether the account is enabled for MCX (commodity
   segment), and whether the SDK's `Exchange` enum carries `MCX`.
2. Independently, pull the instrument master and count rows whose
   `ExchSeg` is `MCXFO` (or `MCX`). The master is the truth; the
   refusal in the code may simply be a stale finding from an account
   that had no commodity segment enabled.
3. If MCX is genuinely unavailable on Arrow: **stop and report**. Do
   not build an MCX terminal against a broker that cannot route MCX.
   Options to put to the desk are (a) enable the commodity segment on
   the Arrow account, (b) start with NSE F&O and keep MCX behind the
   same segment abstraction, (c) add a second broker to the registry.

Either way, the segment must become **config-driven and enumerated from
the instrument master** rather than a hardcoded map with a hardcoded
refusal. That is the change that makes "MCX first, then NSE" one
switch instead of a fork.

### 3.2 Arrow exposes only LTP. The ladder needs the BOOK.

This is the single largest gap. MT5-Trader's first hard rule is:

> The spread is `Leg B − β × Leg A`, built from the **MID OF THE
> BOOK**, never `tick.last`.

Today `ArrowBroker.get_ltp()` uses `QuoteMode.LTP` and the stream uses
`DataMode.LTP`. That is `tick.last` and nothing else. Built on LTP:

- there is no mid, so the ladder cannot centre;
- `short_spread` / `long_spread` do not exist, so **every executable
  price on the screen would be a fiction** and the whole
  hit-the-bid/lift-the-offer convention collapses;
- `spread_cost` (one round turn of both bid-asks) is unmeasurable;
- the Bids and Asks size columns have nothing to show;
- a position cannot be marked at the touch it would actually close at,
  which is what makes a position show its true cost the instant it
  opens.

**Required before Phase 2.** Extend the Arrow broker layer to fetch and
stream the order book:

- REST: `QuoteMode.FULL` (or whatever the SDK's full/quote/depth mode
  is called on the installed build) → best bid, best ask, and the
  5-level depth NSE and MCX both publish.
- WebSocket: `DataMode.QUOTE` / `DataMode.DEPTH` alongside
  `DataMode.LTP`, subscribed by integer `Token`.
- Normalise TitleCase keys case-insensitively, exactly as the existing
  code does, and apply the **paise → rupee** conversion consistently.
  Verify per field: it would be entirely characteristic for `Ltp` to
  arrive in paise while a depth level arrives in rupees. Prove it
  against a live tick before trusting either.
- Missing depth comes back as `None` — **never as an invented size from
  one leg, and never as zero**. Where a level has no book, the column
  is empty.

If full depth turns out to be unavailable but best bid/ask is: build on
best bid/ask, leave the size columns empty, and say so on the screen.
If not even bid/ask is available, **stop and report** — a price ladder
without a book is not a price ladder.

### 3.3 Positions on Arrow are NETTED, not hedged tickets.

MT5-Trader's whole close path is built on the fact that its accounts
are hedging mode: an opposite market order opens a SECOND position, so
closes must name a position **ticket**. Indian exchanges net: one net
quantity per (TradingSymbol, product), and `get_positions()` returns
`NetQty` / `AvgPrice`. There are no per-position tickets to name.

This is a **simplification**, and it removes three of MT5-Trader's
sharpest edges — but only if the port is deliberate about it. See §5.2
for the design. Do not leave `close_position_ticket` semantics in place
against a netting broker; the two models cannot be half-merged.

---

## 4. The new broker layer

Everything MT5-specific lives behind one seam in MT5-Trader: the `Leg`
interface (`legs.py`), which the coordinator, the executor, the quoter
and the UI all speak. **Keep that seam exactly.** Then:

```
arrowtrader/
  broker.py        the ONLY module allowed to import pyarrow_client
  legs.py          ArrowLeg — the same method names as MT5-Trader's LocalLeg
  arrow_errors.py  Arrow/NEST rejection codes → the broker's own words
  instruments.py   the master, the picker index, lot sizes, tick sizes
```

`ArrowLeg` must expose, method for method, what `LocalLeg` exposes:

```
connect  close  ping  account_info  ensure_symbol  tick  session_stats
depth  margin_for  resubscribe  order  place_limit  pending_orders
modify_order  cancel_order  order_state  close_ticket  positions
order_log  terminal_report  server_offset  symbol_report  find_symbols
verify_order
```

with the same return shapes, so that `coordinator.py`, `executor.py`
and `quoter.py` port with their logic intact.

**The two conventions that are load-bearing and easy to lose:**

- `positions()` and `pending_orders()` return **`None` for "unknown"**
  (the call failed, the session expired, the socket dropped). `None` is
  NOT "flat" / "no orders". Code that treats `None` as empty will sweep
  a live account clean in its own report while the money sits at the
  exchange.
- **Unmeasured is not zero.** Return `None` and render `—`.

### 4.1 One session, two legs

MT5 forces one process per account because the `MetaTrader5` package
holds one global connection. **Arrow does not.** One authenticated
`ArrowClient` trades both legs of every pair, on one login.

So: keep the `Leg` abstraction (both legs go through it, and every
panel that says "leg A / leg B" keeps working), but back both legs with
**one shared Arrow session**. Delete the `leg_runner` / `ipc` /
`RemoteLeg` machinery, or keep it dormant behind one code path — do not
run two processes for one login.

Two consequences, both good, both to be stated on screen:
- There is **one margin pool**. MT5-Trader's Accounts tab exists to say
  "with two brokers there is no combined margin, and the pair can only
  be carried by the weaker of the two." On Arrow that paragraph is
  wrong and must be replaced, not left to reassure by habit. One
  account, one SPAN+exposure requirement, and (see §5.5) the two legs
  of a calendar may get **margin benefit** — which is a real number the
  desk needs, not a footnote.
- The "two legs on ONE account" red banner in MT5-Trader
  (`#same-login-banner`) becomes **the normal case**. Remove the
  banner; do not leave it firing on every ladder.

### 4.2 The session token

Arrow's token is valid ~24h and there is **no paper/sandbox**. The leg
layer must:
- reconnect on expiry, on its own, and say so in the connection line;
- treat an expired token as **unknown**, not as flat (see above);
- never write the token, the password, the API secret or the TOTP seed
  into a log line, a status file, or the browser.

Credentials live only in `.env`: `ARROW_APP_ID`, `ARROW_USER_ID`,
`ARROW_PASSWORD`, `ARROW_API_SECRET`, `ARROW_TOTP_SECRET`. The TOTP
value is the **base32 seed**, not the 6-digit code. `config.json` holds
the NAME of the env key and never a value — exactly as MT5-Trader does
with `password_env`.

Arrow requires a **registered static IP** (SEBI). The connection
checklist must name that as the fix when auth fails from an
unregistered host, in words, not as "check the log".

---

## 5. The translation table

For each of these, the MT5 concept, the Arrow/MCX reality, and the
decision. Where a decision is marked **ASK**, it goes to the desk or to
Arrow before it is coded.

### 5.1 Instruments

| MT5 | Arrow / MCX |
|---|---|
| `symbol` (broker-named: `XAUUSD`, `GOLD`, `XAUUSD.r`) | `TradingSymbol` from the instrument master (e.g. `GOLD05DEC25F`) |
| `symbols_get()` search | the picker index over ~223k master rows, grouped by `ExchSeg` + kind |
| `trade_contract_size` (100 oz of gold) | `LotSize` — **units per lot**, and it varies per expiry |
| `volume_min` / `volume_step` | 1 lot; quantity is sent in **units** = lots × `LotSize` |
| `volume_max` | the exchange **freeze quantity** per order — see §5.8 |
| `point` / `trade_tick_size` | the contract's tick size (verify from the master; do not hardcode) |
| `expiration_time` | `Expiry` (`30-Jun-2026`), parsed by the existing robust parser |

**Never hardcode a lot size, a tick size or an expiry.** Read them from
the master, on every resolve, and show the derivation on the Exchanges
page beside the number — that is what MT5-Trader's "Read both legs from
MT5" button does, and its Arrow equivalent is "Read both legs from the
instrument master".

MCX pairs worth having working on day one (verify every symbol and
every ratio against the master — these are illustrative, not
authoritative):

- **GOLD calendar** — near vs far month, 1:1.
- **SILVER calendar** — near vs far, 1:1.
- **CRUDEOIL calendar** — monthly, 1:1.
- **GOLD vs GOLDM** — a size ratio, not a calendar. This is exactly
  what `clip_lots_a` / `clip_lots_b` are for: the trader types both
  legs and nothing is derived on their behalf.
- **SILVER vs SILVERM**, **CRUDEOIL vs CRUDEOILM**, **NATURALGAS vs
  NATGASMINI** — same shape.

The ratio pairs are the better first target than the calendars: they
exercise `clip_lots_a`/`clip_lots_b`, unequal `LotSize`, and unequal
tick sizes, all of which are where a sizing bug hides.

### 5.2 Positions, closes, and the hedging→netting change

**Decision.** Keep MT5-Trader's own `SpreadPosition` ledger — the book
is still the system's record of what it opened, at what price, in what
size, on which pair. Change only what a *close* means:

- MT5: `close_position_ticket(symbol, ticket, volume, entry_side)`.
- Arrow: an **opposite-side order for the same units**, on the same
  `product`, which the exchange nets against the open quantity.

Which means:
- `LegFill.position_tickets` becomes the **order id(s)** and is kept for
  the audit trail, not for addressing a close.
- `positions_to_reduce` / `reduce_first` / oldest-first ticket walking
  stays, but it now walks OUR OWN ledger to decide how much to close
  and at what average, not the broker's ticket list.
- `CLOSE_FIRST` becomes structurally true rather than a setting: on a
  netting account an opposite click always reduces. Keep the setting
  name so the settings page does not change shape; make it a no-op that
  says so, or repurpose it and say what it now does.
- **The closing-limit disaster does not exist here.** In MT5, a
  "closing" limit rested as an ordinary limit and OPENED a second
  position (live, 2026-09-02, ticket 2092). On Arrow a resting opposite
  limit *reduces* the net. So `quoter.py`'s asymmetry — entries backed
  by a real pending, closes held only as a level in memory — **can be
  removed**: a closing order can rest at the exchange, survive a
  restart of this process, and be earned rather than crossed. Do this
  deliberately, with a test, and say it on the screen: the tooltip that
  currently explains why a close does not rest must be replaced, not
  left lying.
- **Still never attach exchange-side stops to individual legs.** One
  leg stopping alone converts the hedge into a naked outright. That
  rule survives the port intact.

**The reconciler** changes shape. There are no tickets to match, so:
- our ledger says "this pair should be net −1 lot of `GOLD05DEC25F` and
  +1 of `GOLD05FEB26F`, on `NRML`";
- `get_positions()` says what the net actually is;
- the difference is the finding. An excess is a ghost, a shortfall is a
  position that has gone (auto square-off, a manual close in Arrow's
  own terminal, an RMS action).
- **The reconciler auto-closes NOTHING until recovery says the book is
  complete**, and never touches a position it cannot explain. An
  unexplained net at the broker goes on the red UNCLAIMED banner with
  *adopt* and *close it* for a person to choose. That rule survives
  exactly, and matters more here: on a netting account, our position
  and the trader's own manual position in the same contract are ONE
  number, and there is no magic number to tell them apart.

**Note that loss of the magic number.** MT5-Trader scopes every sweep,
every position read and every pending read by `MAGIC_NUMBER = 24680`,
so it can never touch the trader's own clicks. Arrow has no equivalent
on positions. Mitigations, in order of preference — **ASK the desk
which**:
1. A **dedicated Arrow account** used by nothing else. Cleanest, and
   restores the guarantee.
2. A dedicated `product` (e.g. this system uses only `NRML`, the trader
   only `MIS`) — partial, and fragile.
3. Order-tag / user-reference field if the SDK exposes one — check
   `place_order`'s accepted kwargs on the installed build. It scopes
   ORDERS, which restores the pending sweep, but it does not scope
   POSITIONS.
4. Ledger-only attribution, with the banner saying loudly that a net
   this system cannot explain may be the trader's own.

Whichever is chosen, **the startup and shutdown sweeps stay** (cancel
our resting orders at both ends) and must be scoped as tightly as the
chosen mechanism allows — and must say, on the screen, exactly what
scope they had.

### 5.3 Sizing

`L_B = L_A × C_A / (β × C_B)` and `k = L_B × C_B` port unchanged; only
the units change. On Arrow:

- `C` (contract size) is `LotSize` — units per lot.
- `L` (lots) is whole lots. **There is no 0.01 lot.** `volume_step` is
  1, `volume_min` is 1, and `round_step`'s `down=True` on leg B still
  matters: short is the recoverable error.
- The order carries **units**: `quantity = lots × LotSize`. Get this
  wrong by a factor of `LotSize` and the first live order is 100× the
  intended size. Put a test on it named so that it cannot be deleted by
  accident.
- `k` is now **₹ per 1.00 of spread**. Every money figure on the screen
  becomes rupees: `₹`, Indian digit grouping (lakh/crore) where the
  desk wants it — **ASK**, and default to plain `₹1,23,456.78` only if
  the desk asks for it; otherwise `₹123,456.78` and no cleverness.
- `max_qty` reads the **freeze quantity**, not a broker volume cap.
- Because lots are whole, `clip_plan`'s refusals get sharper and more
  common. Every one of them must still name the number that would work
  ("Qty 7 or less fits"), which the existing code already does.

### 5.4 Orders

| MT5 | Arrow |
|---|---|
| `TRADE_ACTION_DEAL` + IOC filling mode | `place_order(order_type=MKT, price=0, mpp=True)` — **plain MKT is disabled; `mpp=True` is mandatory or the order is rejected** |
| filling-mode bitmask, retcode 10030 | not applicable — delete the whole filling-mode dance |
| `deviation` (slippage points) | not a broker parameter. **Slippage protection must be enforced by US**: compare the fill against the clicked price and refuse/report per `MARKET_PROTECTION_TICKS`. It is already the clicked-price guard in `executor.py`; here it is the ONLY one. |
| `TRADE_ACTION_PENDING` limit | a real exchange limit order, `DAY` validity |
| `modify_pending(ticket, price)` | `amend_order(order_id, price=…)` — **re-peg by MODIFY, never cancel-and-replace**, exactly as MT5-Trader does, and for the same reason: one order id for the order's life. Verify the installed SDK actually exposes a modify; `arrow_broker.amend_order` tries three method names and returns `False` if none exists. If none exists, the re-peg path must cancel-and-replace and **say so on the screen**, because it changes queue position on every peg. |
| `order_fill_state(ticket)` | `get_order_status(order_id)` → `PENDING/OPEN/PARTIAL/COMPLETE/REJECTED/CANCELLED/UNKNOWN`. **`UNKNOWN` is unknown**, not "not filled". |
| retcode `10027 AutoTrading disabled by client` | Arrow/NEST rejection strings — RMS margin shortfall, price band / DPR breach, freeze quantity, symbol not permitted, market closed. **A refusal carries the broker's own words**, never "check the log". Build `arrow_errors.py` the way `mt5_errors.py` is built. |

`TimeInForce`: MT5-Trader's honest caveat is that neither DAY nor GTC
survives the process stopping, because nothing at the broker knows what
a spread is. On Arrow, a resting LEG order **does** survive — DAY at the
exchange, dying at the session close. So:
- `DAY` becomes true DAY at the exchange.
- `GTC` — **ASK**: Indian exchanges have no true GTC; brokers emulate
  it. If Arrow does not offer one, remove the GTC option rather than
  offering a promise nothing keeps, and put the reason in the tooltip.
- Either way, the spread-level promise still dies with the process: a
  synthetic spread order rests one LEG at the exchange, and the crossing
  of the other leg only happens while this system is running. **That
  caveat must stay on the screen, reworded to be true here.**

### 5.5 Margin

MT5-Trader prices margin per spread by asking each terminal
`order_calc_margin`, and refuses to derive it from notional because a
number computed here would be a guess presented as a figure. The exit
target (`TP % of margin`) is priced off it.

On Arrow: margin is **SPAN + Exposure**, set by the exchange, and a
calendar spread on the same underlying attracts **spread margin
benefit** — often dramatically lower than two outrights. So:

- Find the SDK's margin/span calculator endpoint and use it, passing
  BOTH legs together so the benefit is included. **ASK Arrow** whether
  the SDK exposes one.
- If there is none, **do not derive margin from notional**. Return
  `None`, show `—`, and disable the `TP % of margin` target with a line
  saying why. Unmeasured is not zero.
- Show the benefit explicitly where it exists: two outrights vs the
  spread, and the difference. It is the main economic reason to trade
  the spread as a spread, and it belongs on the rail.

**ASK also:** does MCX offer **exchange-native calendar spread
contracts**, and does Arrow route them? If yes, that is a materially
different product — no legging risk, one order, exchange margin
benefit — and the desk should decide whether this terminal should
eventually place them instead of, or alongside, two legs. Note it as a
future direction; do not build it in Phase 1.

### 5.6 Costs

MT5's cost stack is `crossing + commission`, with a swap per night.
India's is entirely different and every line of it is real:

- **Brokerage** — per order or per lot, per the desk's Arrow contract.
- **Exchange transaction charges** — per exchange, per segment.
- **SEBI turnover fees.**
- **Stamp duty** — buy side only, state-dependent.
- **GST** — 18% on (brokerage + transaction charges).
- **CTT** (Commodities Transaction Tax) — on the **sell** side of MCX
  futures. **Asymmetric**, which the MT5 cost model has no place for.
- **No swap.** Indian futures do not pay overnight financing; the carry
  is *in the price*, which is what the basis is.

**Decision.** Rewrite `costs.py` around a per-segment cost schedule in
config, with the asymmetry (buy vs sell) explicit, and keep the rest of
the system's contract: `crossing_cost` is already in the two prices and
must not be charged twice; `commission` is not and is charged once per
round turn, both legs, both ends. The break-even and take-profit lines
on the rail read the new schedule without changing shape.

Default every rate to **0 with a visible "not configured" note** rather
than to a plausible-looking guess. A fabricated cost is charged against
every trade and the operator cannot tell it was never theirs.

### 5.7 Fair value and carry

MT5-Trader's fair spread is `swap per day × days to expiry`, read from
the two terminals. That mechanism does not exist here.

For an MCX calendar or spot-vs-future, the fair basis is **cost of
carry**: financing at the prevailing rate, plus storage and insurance
for a physically-settled commodity (gold, silver, base metals all are),
over the days to expiry. So:

- Inputs become: **annualised interest rate**, **storage/insurance
  per unit per year**, and **days to expiry** (from the master's
  `Expiry`, not typed).
- `CARRY_RATE_PCT` already exists in the settings as a cross-check;
  here it becomes the primary input rather than the check.
- The pair-type declaration stays exactly as it is —
  `SPOT_FUTURE` / `FUTURE_FUTURE` (calendar) / `RELATED` — and
  `RELATED` still has no fair value, because nothing forces two
  different instruments together. GOLD vs GOLDM is `RELATED` in the
  carry sense even though it is one underlying: it is a size ratio, and
  its fair spread is zero-ish plus the two contracts' own basis. Handle
  it explicitly rather than letting it fall through to a wrong number.
- **A conclusion drawn from an input that can be proven wrong does not
  render at all.** The warning REPLACES the reading and names the field
  that would fix it. That behaviour already exists in the fair window;
  keep it.

Also new and important: **physical delivery**. MCX futures go into a
**tender period** before expiry, after which a position can be assigned
for delivery. The terminal must:
- show days to expiry AND days to tender start on every ladder;
- warn loudly as tender approaches;
- **ASK the desk** whether the system should refuse to open a new
  position in a contract inside its tender period, and default to
  warning-not-refusing until they say otherwise (a guard may withhold an
  order; it must never prevent a close).

### 5.8 Session, hours and the exchange clock

MT5-Trader has one cutoff on the broker's clock, measured from the
terminal rather than configured, and unmeasured means the cutoff does
not fire. Port that discipline exactly, but re-source the clock:

- **Times are IST.** The trading box may be anywhere; the clock that
  matters is the exchange's.
- Measure the offset from the broker (a timestamped response, the
  master's stamps, or an SDK time endpoint — **find one**), do not
  configure it, and if it cannot be measured, **say so and do not fire
  the cutoff**. Unmeasured is not zero.
- **MCX hours differ by season and by commodity.** Non-agri runs from
  the morning into the late evening, and the evening close moves with
  US daylight saving; agri closes in the afternoon. Do not hardcode a
  single `OVERNIGHT_CLOSE_HOUR`. Read the session from the instrument
  master or from a per-segment schedule in config, per contract, and
  show it on the ladder.
- **NSE F&O** is a single, much shorter session. Same mechanism, other
  numbers — which is the point of making it data.
- **MIS auto square-off** is a new class of event with no MT5
  equivalent: the broker flattens intraday positions near the close,
  without asking. If any ladder is on `MIS`, the terminal must show the
  square-off time as a countdown, and must not be surprised when a
  position vanishes (the reconciler must read it as a close, not as a
  ghost). Default the product to **`NRML`** and make `MIS` a deliberate
  per-pair choice.
- **Freeze quantity** — the exchange rejects any single order above a
  per-contract limit. This is the new `volume_max`. Read it (master, or
  the exchange's published list in config), feed it into
  `sizing.max_qty` so the keypad stops offering a size that is a
  guaranteed refusal, and where a desired size exceeds it, **ASK**
  whether to slice into multiple orders or refuse. Default to refusing
  with the number that fits — slicing changes what one click means.
- **Price bands / DPR** — an order outside the daily price range is
  rejected. Read the band, and refuse the click BEFORE it goes, naming
  the band. This is the Indian analogue of MT5's `legal_limit_price`
  and retcode 10015, and it must produce the same quality of refusal.

### 5.9 Everything else, briefly

- **Fills / the journal.** MT5-Trader reads MT5's own deal history,
  keyed by (account, deal id), and includes the trader's own manual
  clicks marked as not ours. The Arrow equivalent is the **trade book**
  (`get_trade_book` / order book), keyed by (account, trade id). Same
  discipline: read back what the broker says happened, not what we
  intended. Exportable as CSV.
- **Slippage report.** Ports unchanged in structure. Entries measured
  against the touch the click was taken at, exits against the CLOSING
  fills. Positive is a cost at both ends, in spread points and in money
  through the same `k`. **A fill that could not be priced is counted as
  unmeasured, never averaged in as zero.** With no depth (blocker 3.2
  unresolved) there is no touch to measure against, and the whole report
  is unmeasured — another reason 3.2 comes first.
- **Depth / implied size.** The Bids and Asks columns show how many
  spreads the two books can actually do, matched leg against leg and
  counted once. MCX publishes 5-level depth, so unlike most CFD
  accounts these columns will actually have something in them. Where a
  level has no book, they stay **empty**.
- **Recovery.** Positions written to the database on every change,
  recovered at startup with their ids and fills. Until recovery has
  completed the book is incomplete and the reconciler auto-closes
  nothing. Ports unchanged and is if anything more important here,
  because netting means an unexplained position cannot be told from the
  trader's own.
- **`START-TRADING.bat` / `deploy/bootstrap.ps1`.** The MT5 version
  checks Python, a running terminal, and a passing test suite. The
  Arrow version checks Python, the **static IP registration**, a
  successful `auto_login`, and a passing test suite. It must refuse to
  start the engine on a failing suite, exactly as now.
- **The launcher.** `start.py` brings up the web UI FIRST (the
  credentials are entered on that screen), then the engine, then opens
  the browser, and restarts a crashed child with backoff. Port it,
  minus the per-account leg runners.

---

## 6. Build order

Each phase ends with a passing `pytest tests/ -q` and a commit. Nothing
touches a live account before Phase 8.

- **Phase 0 — the blockers.** §3.1, §3.2, §3.3 answered in writing, in
  this file, with evidence (a row count from the master, a captured
  quote payload, an SDK method listing). No code beyond throwaway
  probes.
- **Phase 1 — instruments.** The master, the picker index, lot sizes,
  tick sizes, expiries, freeze quantities, price bands. `FakeArrow`
  serves all of it offline. Tests: TitleCase/gzip/JSON/CSV parsing,
  chronological expiry sort, lot resolution keyed on `TradingSymbol`.
- **Phase 2 — the book feed.** Bid/ask and depth over REST and
  WebSocket, paise handling proven per field, `None` for missing.
  Tests: a leg that stops ticking is stale; a missing book is empty,
  not zero.
- **Phase 3 — `broker.py` + `legs.py`.** The full `ArrowLeg` surface
  against `FakeArrow`. Tests: `None` means unknown, on every method
  that can fail.
- **Phase 4 — spread, sizing, book.** `spread.py` ported unchanged;
  `sizing.py` re-united to lots×`LotSize`; `book.py`. Tests: the spread
  definition; **units = lots × LotSize**; whole-lot rounding, down on
  leg B.
- **Phase 5 — execution.** MARKET both legs, the escalation window, the
  unwind, the netted close, `mpp=True`, our own slippage guard. Tests:
  every guard withholds — **and a control that turns the guard off and
  asserts the opposite**, for every one.
- **Phase 6 — coordinator, quoter, reconciler, database, session.** The
  poll loop, the guards, the sweeps, the snapshot. The LIMIT path with
  real resting closes. Recovery. The IST/MCX session clock. Tests: a
  restart recovers the book; the reconciler auto-closes nothing while
  recovery is incomplete.
- **Phase 7 — the UI.** Copy the four front-end files verbatim, wire
  `webapp.py`, and change only what the DATA requires (₹, lots,
  segments, the Accounts tab's one-account wording, the removed
  same-login banner). Tests: the Playwright suite ported, reading
  `pageerror` — Python tests cannot see a temporal-dead-zone
  `ReferenceError` that silently unregisters a handler, and that has
  already happened once in the system this is ported from. **No native
  `confirm()` / `alert()` / `prompt()`, ever — a test fails the build
  if they come back.**
- **Phase 8 — one lot, live, watched.** DRY-RUN first, then a single
  lot on the smallest contract (GOLDPETAL / SILVERMIC scale), on a
  static-IP box, with somebody watching. `scripts/arrow_test_trade.py`
  in this repo is the pattern for a controlled live probe; write its
  spread-aware equivalent.

---

## 7. Hard rules (the `CLAUDE.md` for the new repo)

Write these into `CLAUDE.md` in the new package, adapted from
MT5-Trader's:

- **`pytest tests/ -q` must pass before any commit**, and LIVE mode must
  never be run without it.
- **The spread is `Leg B − β × Leg A`, from the MID OF THE BOOK, never
  the LTP.** Levels and triggers read the EXECUTABLE side for their own
  direction; a position reads the OPPOSITE executable side to close.
- **`L_B = L_A × C_A / (β × C_B)`**, and `k = L_B × C_B` is the one
  multiplier every spread-to-money conversion uses.
- **Quantity on the wire is UNITS = lots × `LotSize`.** One test guards
  this and it may not be deleted.
- **Market orders are `price=0, mpp=True`.** Plain MKT is rejected.
- **Never attach exchange-side stops to individual legs.** One leg
  stopping alone converts the hedge into a naked outright.
- **Credentials live only in `.env`** — never in code, config, chat or a
  log line. Never the session token either.
- **Sweep our resting orders at shutdown AND at startup**, scoped as
  tightly as the chosen attribution mechanism allows, and say what
  scope that was.
- **The book is persisted and recovered.** An empty book at startup
  makes every live position look like an orphan; the reconciler
  auto-closes nothing until recovery says the book is complete, and
  never touches a position it cannot explain.
- **No strategy and no loops.** No signals, no automatic entries or
  exits, nothing that re-enters by itself.
- `positions()` and `pending_orders()` return **`None` for "unknown"**,
  which is NOT "flat" / "no orders".
- **Unmeasured is not zero.** Return `None` and render `—`.
- Guards may withhold an ORDER. **A guard must never prevent a close.**
- A refusal carries **the broker's own words**, never "check the log".
- `arrowtrader/broker.py` is the only module allowed to import
  `pyarrow_client`.
- Every test that asserts a guard withholds something needs a
  **control** that turns the guard off and asserts the opposite.

---

## 8. Testing

Everything is faked. No network, no credentials, no clock.

- `FakeArrow` — extend `arrow-statarb/tests/conftest.py`'s fake SDK
  into a broker that keeps a real **netted** book, a real order book
  with depth, real order lifecycle states, and real rejections
  (RMS shortfall, freeze quantity, DPR breach, market closed, plain-MKT
  refused without `mpp`). It must behave like the exchange, not like a
  yes-man.
- An **end-to-end suite** as MT5-Trader has: the engine with a real
  database, the Flask process and Chromium — a click crosses every
  boundary except Arrow itself, survives an engine restart, and lands
  in the journal.
- **Playwright UI tests reading `pageerror`.** They skip cleanly where
  no browser is installed, and nothing else skips with them.
- Every number that reaches the screen with a unit gets a test that
  asserts the unit.

---

## 9. Open questions — answer before or during Phase 0

1. **Does Arrow route MCX for this account?** (Blocker 3.1.)
2. **Does the SDK expose full quotes / 5-level depth, over REST and
   WebSocket?** (Blocker 3.2.) Which fields are paise and which are
   rupees?
3. **Does the SDK expose a modify/amend on a resting order?** If not,
   the re-peg is cancel-and-replace and the screen must say so.
4. **Is there a SPAN/margin calculator endpoint**, and does it price
   two legs together so the spread benefit shows?
5. **Is there an order tag / user-reference field** on `place_order`,
   to scope our own resting orders?
6. **Will this system have a dedicated Arrow account**, or share one
   with the trader's manual dealing? (Decides §5.2's attribution.)
7. **Does Arrow emulate GTC?** If not, the option comes off the screen.
8. **Freeze quantity: slice or refuse?** Default: refuse, naming the
   size that fits.
9. **Tender period: warn or refuse a new open?** Default: warn.
10. **Does MCX/Arrow offer exchange-native calendar spread orders?**
    Future direction, not Phase 1.
11. **The exact cost schedule** — brokerage, exchange charges, SEBI
    fees, stamp duty by state, GST, CTT — from the desk's Arrow
    contract. Until then every rate is 0 and says "not configured".
12. **Money formatting**: `₹1,23,456.78` (Indian grouping) or
    `₹123,456.78`?
13. **Which repository does this live in?** Recommendation: a new
    top-level package `arrowtrader/` in **this** repo, reusing and
    extending `arrow_statarb/brokers/` — one place for the Arrow
    knowledge, one test suite for the SDK fake, and the stat-arb
    strategy stays cleanly separate from the manual terminal. The
    alternative is a third repository, which duplicates the broker
    layer and guarantees the two copies drift.

---

## 10. What "done" looks like

A trader on a static-IP box double-clicks one shortcut. The web UI
comes up, they enter their Arrow credentials on the Exchanges page, and
the connection line goes green and says so out loud. They add a pair by
picking two MCX contracts from a searchable master, press **Read both
legs**, and the lot sizes, tick sizes, expiries, freeze quantities and
β arrive with their derivations shown. A ladder appears. It centres on
the mid of the two books. They click the Asks column and one lot of the
spread is on, both legs, in tens of milliseconds, with the fill in the
journal and the position on the monitor showing what closing it right
now would cost. They press `F` and it is off.

And it looks exactly like MT5-Trader, because it is.
