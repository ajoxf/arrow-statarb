# Working on Arrow Trader

A spread price-ladder terminal for MCX (then NSE), on the Arrow broker.
The screen is MT5-Trader's screen; only the engine underneath differs.

`docs/ARROW-TRADER-BUILD-PROMPT.md` is the brief and
`docs/ARROW-TRADER-SCREENS.md` is the UI contract. Read both first.

`ajoxf/MT5-Trader` is the source this is ported from. It is **read-only**:
read it, port from it, quote it — never edit it.

## Hard rules

- **`pytest -q` must pass before any commit**, and LIVE mode must never
  be run without it.
- **The spread is `Leg B - beta x Leg A`**, built from the MID OF THE
  BOOK, never the LTP. Levels and triggers read the EXECUTABLE side for
  their own direction; a position reads the OPPOSITE executable side to
  close.
- **`L_B = L_A x C_A / (beta x C_B)`**, and `k = L_B x C_B` is the one
  multiplier every spread-to-money conversion uses. Here `C` is
  `LotSize` — units per lot.
- **Quantity on the wire is UNITS = lots x LotSize.** Get it wrong and
  the first live order is LotSize times the intended size.
  `test_units_are_lots_times_lot_size` guards it and may not be deleted.
- **A market order is `price=0, mpp=True`.** Plain MKT is rejected by
  Arrow.
- **Arrow gives no slippage parameter.** The clicked price is the only
  guard, and it is enforced here.
- **Never attach exchange-side stops to individual legs.** One leg
  stopping alone converts the hedge into a naked outright.
- **Credentials live only in `.env`** — never in code, config, chat or a
  log line. The session token never leaves the broker module either.
- **Sweep our resting orders at shutdown AND at startup**, scoped as
  tightly as the account allows, and say what scope that was.
- **The book is persisted and recovered.** An empty book at startup
  makes every live position look like an orphan; the reconciler
  auto-closes nothing until recovery says the book is complete, and
  never touches a position it cannot explain.
- **No strategy and no loops.** No signals, no automatic entries or
  exits, nothing that re-enters by itself. (`arrow_statarb/` is the
  stat-arb system; it is a separate product and shares only the SDK
  knowledge.)

## Conventions that are easy to lose in a refactor

- `positions()` and `pending_orders()` return **None for "unknown"**
  (the call failed, the token expired), which is NOT "flat"/"no
  orders". Code that treats None as empty will sweep a live account
  clean in its own report while the money sits at the exchange.
- **Unmeasured is not zero.** Return None and render "—". A lot size
  that cannot be resolved is None and REFUSES; it is never 1.
- Guards may withhold an ORDER. **A guard must never prevent a close.**
- A refusal carries the broker's own words, never "check the log".
- `arrowtrader/broker.py` is the only module allowed to import
  `pyarrow_client`.
- Positions on Indian exchanges NET. There are no per-position tickets:
  a close is an opposite order for the same units on the same product,
  and our own ledger is the record of what we opened.
- Every test that asserts a guard withholds something needs a
  **control** that turns the guard off and asserts the opposite.
