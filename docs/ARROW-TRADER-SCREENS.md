# Arrow Trader — the screens

Every screen in the product, what is on it, and what changes when the
back end is Arrow/MCX instead of two MT5 terminals.

**The rule: nothing here is a redesign.** The layout, the colours, the
wording and the keys are MT5-Trader's. Where a row is marked
**CHANGES**, the change is in the DATA or the labelling, not in where
things sit. Where a row is marked **NEW**, it is something MCX has that
MT5 does not, and it goes in the space an MT5 concept vacated.

There are **4 top-level surfaces** and **11 panels**.

---

## Surface 1 — The desktop (`/`)

One page. Everything below floats on it as a draggable, resizable
window; positions persist across reloads; **Tidy** puts them back in a
row.

### 1.0 Chrome (always on screen)

| Element | Content | Arrow/MCX |
|---|---|---|
| Brand, top left | `NEXUS · spread terminal`, two-node mark, blue + red | unchanged |
| Engine banner | "the engine is not running" — a dead engine must never look like a quiet market | unchanged |
| Naked-leg banner | red, does not auto-hide, clears when the exposure does | unchanged |
| Unclaimed banner | positions at the broker the book cannot explain, with *adopt* / *close it* | **CHANGES** — a netted position, not a ticket; and it matters more, because without a magic number our position and the trader's own manual one are the same number |
| Same-login banner | "two legs on ONE account: no hedge" | **REMOVED** — on Arrow one login trades both legs; this is the normal case |
| Bottom taskbar | `+` menu, one tab per open panel with a working-order badge, link badge, Sound, Keys, Tidy, Exchanges, loop stat, `?`, **KILL ALL** | unchanged |
| Link badge | green only when the account answered AND every enabled ladder is quoting | **CHANGES** — one session instead of two terminals; wording follows |
| Shared modal | the only dialog. No native `confirm()`/`alert()`/`prompt()`, ever — a test fails the build if they return | unchanged |
| Toasts | errors stay until dismissed | unchanged |
| Help overlay (`?`) | the key list: `B S F X 1-5 0 L M Tab Esc`, and what Work / LTQ / Drag mean | unchanged |

---

### Panel 1 — The Ladder (one window per pair)

The main event. Five columns, `Work │ Bids │ Price │ Asks │ LTQ`, black
inside-market rule, heavy rule on the mid, bid blue, ask red, 17px rows.
The Asks column BUYS the spread; the Bids column sells it.

**Title bar** — swatch, pair name, route, mode badge, ⚙ (ladder
settings), ×.

**Quote strip** — net change, `H: L: O: V1: V2:`, and the `ours` badge
saying whether H/L/O are the legs' own session figures or our own mid
series since start.
- **CHANGES:** MCX publishes real session O/H/L/V per contract, so the
  `ours` badge should be off far more often than it is on MT5 CFDs.

**Left rail**, top to bottom:

| Control | Arrow/MCX |
|---|---|
| Route box — leg A / leg B with the login each is on | **CHANGES** — one login; shows **segment + TradingSymbol** per leg instead (`MCXFO GOLD05DEC25F`) |
| Mode `Limit/Market` + TIF `Day/GTC` | **CHANGES** — GTC comes off unless Arrow emulates it; the tooltip's "nothing at the broker knows what a spread is" is reworded: a resting LEG order now *does* survive at the exchange, but the spread promise still dies with this process |
| Quoting-leg note | unchanged — says which leg will actually show an order |
| **BUY** (red) / **SELL** (blue) | unchanged |
| **CLOSE ALL** | unchanged — crosses now, by net reduction instead of by ticket |
| Close @ LMT + `@ LMT` | **CHANGES for the better** — on a netting account a closing limit genuinely rests at the exchange, instead of being a level held in this process's memory |
| Qty BUY / Qty SELL boxes + keypad `1 5 10 50 100 CLR` | **CHANGES** — whole lots only; the keypad stops offering sizes above the exchange **freeze quantity** |
| Working orders — `S / CXL All / B` with counts | unchanged |
| Increment | **CHANGES** — derived from the two contracts' tick sizes (`max(tick B, β × tick A)`), read from the instrument master |
| Lock / Filter / Centre | unchanged |

**Grid** — the ladder itself, plus the `B: S: W:` counts strip.
- Click a price to work an order there; click **Work** to add one more
  at that level; right-click **Work** to pull one.
- A click away from the touch **rests** (`CLICK_AWAY_RESTS`).
- **Bids/Asks carry implied size** — how many spreads the two books can
  do, matched leg against leg, counted once. **CHANGES:** MCX publishes
  5-level depth, so unlike most MT5 CFD accounts these columns will
  actually be populated. Where a level has no book they stay **empty**,
  never zero.

**Footer** — feed/guard line, `↻ Feed`, each leg's own bid/ask and quote
age, position, P&L, the clip derivation (`Qty 1 = 1 lot A / 1 lot B,
₹X per 1.00 of spread`), errors.
- **CHANGES:** money is ₹; the clip line reads lots and **units**
  (lots × LotSize), because units is what goes on the wire.

**NEW on the footer or quote strip:** **days to expiry** and **days to
tender start**, per leg. MCX futures are physically settled; a position
carried into the tender period can be assigned for delivery. It warns
as tender approaches.

---

### Panel 2 — Ladder settings (overlay on the ladder, ⚙)

Per-pair, three groups. Slides over the ladder rather than living in
the rail.

**Trading** — Mode, Time in force, Increment, Rows, Default qty,
**Leg A lots**, **Leg B lots**, Contract A, Contract B, Quoting leg.
- **CHANGES:** "Contract A/B" (MT5's `trade_contract_size`) becomes
  **Lot size A/B** — units per lot, from the instrument master, and it
  varies per expiry. Same override-with-a-warning behaviour.
- Leg A/B lots is unchanged and is the control that makes GOLD-vs-GOLDM
  work: the trader types both, nothing is derived.

**Exit** — Exit type, Overnight, AutoRoute, Comm/lot A, Comm/lot B,
Stale after (s), Slippage allowance, Nights held, TP % of margin.
- **CHANGES:** the commission fields become the Indian cost stack —
  brokerage, exchange transaction charges, SEBI fees, stamp duty, GST,
  and **CTT on the sell side only**. Asymmetric, which MT5's model has
  no place for. Every rate defaults to 0 and says *not configured*.
- **CHANGES:** "Nights held" survives, but there is no swap in India —
  it now drives the **cost of carry**, not a financing charge.
- **NEW:** **Product** — `NRML` (default) or `MIS`. MIS is squared off
  by the broker near the close, without asking; choosing it puts a
  countdown on the ladder.

**Carry** — Show Fair Spread window, Pair type, Expiry A/B, Swap A/B
long/short, Carry rate % a year.
- **CHANGES:** the four **swap fields are replaced** by
  **interest rate % a year** + **storage/insurance per unit per year**.
  Cost of carry is what an MCX basis actually is.
- Expiry A/B become read-only, from the master.
- Pair type stays `Spot vs Future / Calendar / Related`. GOLD vs GOLDM
  is `Related` — a size ratio, not a carry.

---

### Panel 3 — Fair Spread window (per pair, optional, floats)

Off by default. Turned on per ladder, it floats where you put it and
carries the pair name across the top.

- **Fair** — Fair buy/sell, Gap buy/sell, the kind, the note, and the
  warning that **replaces** the reading when an input can be proven
  wrong.
- **Exit** — `B-A Sp`, `Comm`, `Slip`, `Swap`, `P% on M`, `Reach`, then
  `B/E` and `TP` for both directions, then `o/n` and `Auto`.
- **CHANGES:** `Swap` becomes `Carry`. `P% on M` is priced off SPAN +
  exposure margin **with the calendar-spread benefit included** — and
  if Arrow exposes no margin calculator, it shows `—` and the target is
  disabled, never derived from notional.
- It reads. It never trades, and no price on it is ever sent to the
  exchange.

---

### Panel 4 — Market Grid (one window, all pairs)

One row per ladder, sortable at a glance, everything editable inline:

`Contract │ Bid │ Ask │ Last │ Chg │ Incr │ Qty │ k ₹ │ Net │ Work │ Avg │ Open P&L │ Mode │ TIF │ O/N │ Feed │ β │ reason`

- Bid is the short spread (where you can sell it), Ask the long. Click
  the contract name to open that ladder.
- **CHANGES:** `k $` → `k ₹`; TIF loses GTC if Arrow has none.
- **NEW column:** **Expiry / days to tender**, because with six MCX
  calendars open the nearest-expiry leg is the thing you must not lose
  track of.

---

### Panel 5 — Trading Monitor (one window, six tabs)

#### 5a. Positions
Every open spread position: pair, side, quantity, entry spread, closing
spread, gross and net P&L, the exit block, whether it was recovered
from disk, whether the reconciler has confirmed it.
- **CHANGES:** "confirmed" means the **net** at Arrow matches our
  ledger, not that two tickets were found.
- Unmeasured is not zero: one position that cannot be marked makes the
  TOTAL unknown, and it shows `—`, not a total that is silently short.

#### 5b. Working Orders
Every synthetic working order — one per click, individually
cancellable — with its level, side, quantity, filled quantity, state,
whether it is an OPEN or a CLOSE, whether AutoRouting armed it, and the
backing order id. Plus recently-dead orders **with the broker's reason**.
- **CHANGES:** `pending_ticket` → Arrow `order_id`; refusal text is
  Arrow/NEST's own words (RMS margin shortfall, DPR breach, freeze
  quantity, market closed).

#### 5c. Fills — the journal
Every fill the broker reported, read back from the broker's own trade
book rather than from our intentions, keyed by (account, trade id).
Carries the trader's own manual dealing too, marked *not ours*, with
the broker's charges and its own clock beside the offset from ours.
CSV export.
- **CHANGES:** MT5 deal history → Arrow trade book; `is_ours` is
  attributed from our ledger (and from an order tag if the SDK has one)
  rather than from a magic number.

#### 5d. Slippage
The session you are in, cut at the cutoff on the **exchange's** clock.
Entries measured against the touch the click was taken at, exits
anchored on the CLOSING fills. Positive is a cost at both ends, in
spread points and in money through the same `k`. MARKET and LIMIT side
by side. Worst entries ranked in money. Unpriceable fills counted as
**unmeasured**, never averaged in as zero. CSV export with empty cells,
not zeros.
- **CHANGES:** IST session; ₹. Structure unchanged.
- Note: this report is only meaningful once the order book feed exists
  — without bid/ask there is no touch to measure against.

#### 5e. Accounts
- **CHANGES, substantially.** MT5-Trader's Accounts tab exists to say
  *"with two brokers there is no combined margin, so the pair can only
  be carried by the weaker of the two"*. On Arrow that paragraph is
  **wrong** and gets replaced, not left to reassure by habit.
- What it shows instead: one Arrow account — cash, **SPAN margin**,
  **exposure margin**, available, utilised, and this system's own lots
  and units per contract.
- **NEW:** **spread margin benefit** — two outrights vs the spread, and
  the difference. It is the main economic reason to trade the spread as
  a spread, and it belongs on screen.

#### 5f. Reconciler
Orphans, ghosts, three strikes, contract-size-correct P&L, and the
UNCLAIMED list with *adopt* and *close it*.
- **CHANGES:** matching is ledger-vs-net, not ticket-vs-ticket. A
  shortfall may be a MIS auto-square-off or an RMS action, and it says
  which where it can tell.
- Auto-closes **nothing** until recovery says the book is complete.

---

## Surface 2 — Exchanges (`.window.settings`, opens on the desktop)

Title: *Exchanges — accounts, pairs and settings*. Three sections. This
is the one surface that must work with the **engine down**, because
otherwise setup deadlocks.

### 2a. Trading (the tunables)
Every setting with its default beside it, and each one saying whether
it applies now or needs a restart. Confirm market clicks, click
convention, close-first, re-centre seconds, click-away-rests, poll
interval, market protection ticks, re-peg dead band, stale-after, jump
sigma, TP % of margin, AutoRoute master switch, shutdown behaviour…
- **CHANGES:** MT5-only knobs go (filling modes, deviation points).
- **NEW:** default **Product** (NRML/MIS), the **cost schedule**
  (brokerage, exchange charges, SEBI, stamp duty by state, GST, CTT),
  and **freeze-quantity behaviour** (refuse, by default).

### 2b. Accounts → **the Arrow connection**
- **CHANGES, most of all.** MT5-Trader has one row per MT5 account:
  terminal path, login, server, port, with **Connect / Test / Diagnose**
  and the three clash refusals.
- Arrow has **one** connection: App ID, User ID, password, API secret,
  TOTP **seed** (base32, not the 6-digit code) — all entered here, all
  written to `.env` under sanitised keys, never to `config.json`, never
  to a log.
- **Connect** — does `auto_login` succeed? **Test** — can it trade
  (segment enabled, MCX permitted, RMS live)? **Diagnose** — everything,
  including whether the two legs fit each other: segments, lot sizes,
  tick sizes, expiries, and a β stamped for the pair it is used on.
- Every failure carries **the step that fixes it**, in words. Including
  the one that will actually happen: *this host's IP is not registered
  with Arrow (SEBI) — register it and reconnect.*
- **NEW:** token age and expiry countdown (~24h), and whether the
  instrument master is loaded (~223k rows) and how old it is.
- One line at the top says **CONNECTED** or names the single thing
  standing in the way, and says it out loud once when it becomes true.
- **NEW:** exchange time and its offset from this machine. Unmeasured is
  not zero: with no measurement the session cutoff does not fire, and
  it says so.

### 2c. Pairs → **New pair**
Pick the segment → underlying → contract for each leg from the
instrument-master picker (grouped by `ExchSeg` and kind, expiries in
chronological order), set the ratios, press **Read both legs from the
instrument master** to derive β, the increment, the matched-minimum
clip, the lot sizes and the minimum notional — each shown **with its
derivation** and offered as a one-click correction, **never applied
silently**. Then Save.
- **CHANGES:** "Find" against a broker's symbol list becomes the master
  picker; "Read both legs from MT5" becomes "…from the instrument
  master".
- The key is built from the two TradingSymbols. The engine picks the
  new pair up within seconds and its ladder appears beside the ones
  already open, with no reload.
- **NEW:** a **roll** affordance. MCX contracts expire monthly or
  bi-monthly; a calendar pair has to be re-pointed at the next pair of
  contracts constantly, and doing that by hand through New Pair every
  month is how the wrong contract gets traded.

---

## Surface 3 — the launcher / desktop shortcut

Not a browser screen, but it is what the trader touches first.
`START-TRADING` checks, before it will start the engine: Python present,
**this host's IP registered with Arrow**, a successful `auto_login`, and
**the test suite passing**. It refuses to start on a failing suite.

---

## Surface 4 — the CSV exports

Fills and Slippage, both already in MT5-Trader, both unchanged in
shape. Empty cells where nothing was measured — never zeros.

---

## Summary of UI deltas

**Removed (3):** the same-login banner; the two-terminal Accounts
narrative; GTC (pending Arrow's answer).

**Reworded, same place (7):** route box → segment + TradingSymbol;
Contract size → Lot size; Swap fields → interest + storage; `$` → `₹`;
the TIF caveat; the closing-limit tooltip; the Accounts tab's margin
paragraph.

**New (7):** days to expiry / days to tender; Product (NRML/MIS) with
its square-off countdown; SPAN + exposure with the **spread margin
benefit**; the Indian cost schedule (incl. sell-side CTT); freeze
quantity in the keypad and the refusals; the token/master status block;
the contract **roll** affordance.

**Everything else is identical**, because it is the same front-end
files.
