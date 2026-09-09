# Arrow Trader — the runbook

The page to hand to whoever runs this day to day. It assumes nothing.

---

## Before anything: the three blockers

**None of this works until these are answered, and two of them need
Arrow rather than a developer.** They are set out in full in
`ARROW-TRADER-BUILD-PROMPT.md` §3; this is the short form.

| # | Question | How you know it is answered |
|---|---|---|
| 1 | **Does Arrow route MCX for this account?** | Exchanges → the segment table shows `mcx_fo` as **ready**. If it says *the master carries no MCXFO contracts*, the commodity segment is not enabled — ask Arrow. If it says *the SDK has no MCX value*, upgrade `pyarrow-client`. Same symptom, different fix, and the screen tells them apart. |
| 2 | **Does the SDK give the BOOK, not just the last trade?** | Run the probe (below) with no flags. If it refuses with *"quoting a last trade and no book"*, stop. A ladder without bid and ask is not a ladder. |
| 3 | **Is this Arrow account used by anything else?** | Exchanges → **Dedicated**. It defaults to *no*, and *no* is safe: the reconciler then never calls a difference an orphan, because on a netting account it genuinely cannot tell yours from ours. |

---

## Day one, in order

### 1. A box with a registered IP

SEBI requires it, and Arrow enforces it. Nothing works from an
unregistered host, and the failure looks like a password problem. The
Exchanges page names it in words when it happens.

### 2. Python 3.11 and the dependencies

```
conda create -n arrowtrader python=3.11 -y
conda activate arrowtrader
pip install -r requirements.txt
```

A conda `base` is often Python 3.7, where `flask>=3.0` will not install
at all. `py -3.11` is the python.org launcher and a conda prompt does
not have it.

### 3. The tests, before the engine

```
pytest -q
```

**They must pass before any commit, and LIVE mode must never be run
without them.** They need no network, no credentials and no clock;
everything is faked, and `FakeArrow` keeps a real netted book with real
rejections. If they fail, do not start the engine.

### 4. Start it

```
python run_arrowtrader.py
```

The web UI comes up **first**, on `http://127.0.0.1:5000/`, because the
credentials are typed into that screen and the engine will not start
without them.

### 5. Credentials — Exchanges page

App ID, User ID, password, API secret, and the TOTP **seed**.

> The TOTP field wants the **base32 seed** from your authenticator
> setup — the long string you scanned or copied — **not** the 6-digit
> code. This is the mistake everybody makes once.

They are written to `.env` and never to `config.json`. Nothing on any
screen, in any response, or in any log line ever shows one back.

### 6. Declare whether the account is dedicated

Read blocker 3 above before you tick it. Ticking it when it is not true
means the reconciler will offer to close a net that might be your own
dealing.

### 7. A pair

Exchanges → Pairs → New pair. Pick a segment, an underlying and a
contract for each leg from the master, press **Read both legs**, check
each derived number against its stated derivation, and Save.

Start with a **ratio** pair rather than a calendar — `GOLD` vs `GOLDM`,
or `CRUDEOIL` vs `CRUDEOILM`. Unequal lot sizes are where a sizing bug
hides, so it is the better first target.

---

## The first live trade

Not on the ladder. On the probe, watched, with the broker's own order
book open beside it.

```
# read-only: session, master, book, margin
python scripts/arrow_spread_test.py --leg-a GOLD05DEC25F --leg-b GOLD05FEB26F

# the order lifecycle: place, modify, cancel — nothing crosses
python scripts/arrow_spread_test.py ... --place-limit

# ONE LOT, both legs, then flat. Real money.
python scripts/arrow_spread_test.py ... --round-trip --yes
```

**What to watch** — and it is not the P&L, which proves nothing over
thirty seconds:

- the two fill prices against the two touches printed above them;
- how long leg B took after leg A (the naked window);
- whether the exchange's net matches what the script says it did.

If the script and the broker's screen ever disagree, **the screen is
right.**

---

## Things that will happen, and what they mean

| What you see | What it is |
|---|---|
| **Red banner: UNRESOLVED** | An order is neither filled nor rejected. Arrow returns an order id, not a fill, so this is a real third outcome. **Nothing has been unwound and nothing will be** — trading against a working order opens the opposite position, which then fills against the leg you were cancelling. Open the broker's order book, decide, then click *I have checked it*. |
| **Red banner: NAKED LEG** | One leg is on and its hedge is not. A *known* exposure, unlike the above. Hedge it or flatten it. |
| **Red banner: unclaimed** | The exchange's net does not match our book. On a shared account this may be your own dealing and the system says so rather than guessing. *Adopt* stops asking; *close it* flattens. |
| **"the exchange clock has not been measured yet"** | The session cutoff will **not** fire. That is deliberate: a cutoff on the wrong clock is worse than one that waits for the right one. |
| **"the 23:30 cutoff was not applied on ..."** | The engine was down over the cutoff. MCX closes at 23:30, leaving thirty minutes to midnight — a restart steps straight over it. Nothing is being done now because the market is shut; check yesterday's DAY orders by hand. |
| **A cost of ₹0.00 everywhere** | The charge schedule is not configured. That is an unfilled form, not a free trade — the Exchanges page says so. Take the rates from your Arrow contract note. |
| **An em dash where a number should be** | Unmeasured. It is never rendered as zero, anywhere. |
| **Ladder says "expires in 3 days — DELIVERY"** | MCX settles physically. A position carried into tender can be assigned, which involves a warehouse. |

---

## Stopping it

`Ctrl-C`, or close the window. The engine **sweeps its resting orders
on the way out**, and sweeps again on the way in.

That matters more here than it did on MT5. A leg order rests at the
exchange and outlives this process; the *spread* does not, because the
second leg is only crossed while the engine is running. A resting entry
that fills while you are down leaves an **outright**, not a hedge.

On a **shared** account the sweep can only pull what that run placed —
after a restart, nothing. It says so when that is the case. Check the
broker's own order book.

---

## Where things are

| File | What it is |
|---|---|
| `config.json` | Accounts, pairs, settings. **Secret NAMES only.** |
| `.env` | The secrets. Never committed, mode 600. |
| `arrowtrader.db` | Positions, the journal, events. **The only record that a net at the exchange was ever ours** — the exchange cannot tell you that. Back it up. |
| `status.json` | The engine's snapshot; the UI renders from it. |
| `commands.jsonl` | What the browser asked for. Primed at startup so a restart never replays a command. |

---

## The rules, on one page

- `pytest -q` passes before any commit, and LIVE never runs without it.
- The spread is `Leg B − β × Leg A`, from the **mid of the book**, never
  the LTP.
- Quantity on the wire is **units = lots × LotSize**. A wrong lot size
  does not fail — it fills at the wrong size.
- A market order is `price=0, mpp=True`. Plain MKT is rejected.
- The clicked price is the **only** slippage guard. Arrow has no
  `deviation` parameter.
- Never attach exchange-side stops to individual legs.
- A guard may withhold an **order**. A guard never prevents a **close**.
- `None` means unknown. It is not "flat", and it is not zero.
- A refusal carries the broker's own words.
