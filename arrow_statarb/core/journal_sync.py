"""Keep the trade journal in step with Arrow.

Three jobs, all on ``TradeLog``'s rows:

1. ``repair_on_start`` — MCX rows journaled at ×1 (the broker ORDER lot)
   are rescaled to the contract multiplier, and every settled round trip is
   re-costed on Arrow's rate card. Idempotent; backs the file up first.

2. ``settle_close`` — a round trip's charges from Arrow's rate card on its
   actual fills (``costs.fills_charges``): brokerage per executed order, txn,
   SEBI, CTT/STT, stamp, GST — the lines of Arrow's charges summary — instead
   of the % estimates the algo plans with.

3. ``plan_import`` / ``apply_import`` — Arrow's tradebook as the source of
   truth. Every LIVE journal leg is matched to the Arrow order that filled it
   (same contract and side; nearest price, then time), correcting fills Nexus
   estimated (e.g. a position 'closed outside Nexus' and marked flat later).
   Whatever Arrow filled that no journal row holds is rebuilt into round
   trips — a trade runs from the first fill out of flat to the fill that
   brings every contract in it back to flat — and added as rows marked
   source 'arrow'. Order ids are recorded on every row they are attributed
   to, so importing the same tradebook again changes nothing.
"""

from __future__ import annotations

import re
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from arrow_statarb.core import costs
from arrow_statarb.core.contracts import mcx_multiplier
from arrow_statarb.core.fill_check import contract_key
from arrow_statarb.core.trade_log import _outcome_tag

_IST = timezone(timedelta(hours=5, minutes=30))
_EPS = 1e-6
#: a journal fill is matched to Arrow orders up to this long BEFORE it was
#: journaled (a position closed in Arrow's app and marked flat in Nexus later)
MATCH_LOOKBACK_SEC = 7 * 86400.0
#: … and this long after (clock skew; the journal writes right after the fill)
MATCH_LOOKAHEAD_SEC = 300.0
IMPORTED_REASON = "outside Nexus — rebuilt from Arrow's tradebook"


# ── helpers ───────────────────────────────────────────────────────────────────
def pair_symbols(rec: Dict) -> Optional[Tuple[str, str]]:
    """(leg A, leg B) from a row's 'A − B' name."""
    parts = re.split(r"\s+[−-]\s+", str(rec.get("name") or ""), maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def _leg_sides(direction: str, action: str) -> Tuple[str, str]:
    """(side of leg A, side of leg B) for an OPEN/CLOSE of a direction."""
    long_ = direction == "LONG_SPREAD"
    buy_a = long_ if action == "OPEN" else not long_
    return ("buy", "sell") if buy_a else ("sell", "buy")


def _gross(fills: List[Dict]) -> float:
    return sum((1 if f["side"] == "sell" else -1) * f["units"] * f["price"] for f in fills)


def _pairs(trades: List[Dict]) -> List[Tuple[int, int]]:
    """(open index, close index) for every settled round trip, matched the
    way TradeLog.record matches them: a CLOSE settles the latest earlier
    unmatched OPEN of the same direction."""
    stacks: Dict[str, List[int]] = defaultdict(list)
    out = []
    for i, r in enumerate(trades):
        if r.get("action") == "OPEN":
            stacks[r.get("direction")].append(i)
        elif r.get("action") == "CLOSE" and r.get("entry_spread") is not None:
            st = stacks.get(r.get("direction"))
            if st:
                out.append((st.pop(), i))
    return out


def journal_round_trip_fills(op: Dict, cl: Dict, mult_a: float, mult_b: float) -> List[Dict]:
    """The four leg fills a journal round trip implies (one order each)."""
    syms = pair_symbols(cl) or pair_symbols(op) or ("A", "B")
    lots = float(cl.get("lots") or op.get("lots") or 0)
    out = []
    for rec, action in ((op, "OPEN"), (cl, "CLOSE")):
        sa, sb = _leg_sides(cl.get("direction"), action)
        for sym, side, px, m in ((syms[0], sa, rec.get("leg_a_price"), mult_a),
                                 (syms[1], sb, rec.get("leg_b_price"), mult_b)):
            if px is None:
                return []
            out.append({"symbol": sym, "side": side, "units": lots * m, "price": float(px)})
    return out


def settle_close(cl: Dict, fills: List[Dict], charges: Dict, cgt_pct: float = 0.0,
                 op: Optional[Dict] = None) -> None:
    """Re-settle a CLOSE row in place: gross from the fills, the rate-card
    charges, CGT on a profit, net, and the outcome tag. The OPEN's own
    entry-brokerage placeholder is cleared — the round trip's charges live on
    its CLOSE alone, so day P&L and totals count them once."""
    gross = round(_gross(fills), 2)
    cl["spread_pnl"] = gross
    if cl.get("edge_pnl"):
        cl["slippage_pnl"] = round(gross - float(cl["edge_pnl"]), 2)
    cl["brokerage"] = charges["brokerage"]
    cl["stt"] = charges["stt"]
    cl["other_cost"] = round(charges["txn"] + charges["sebi"] + charges["stamp"] + charges["gst"], 2)
    cl["charges"] = dict(charges)
    cl["charges_model"] = "arrow_rate_card"
    pre = gross - charges["total"]
    cgt = round(float(cgt_pct or 0) / 100.0 * pre, 2) if pre > 0 else 0.0
    cl["cgt"] = cgt
    cl["net_pnl"] = round(pre - cgt, 2)
    cl["outcome"] = _outcome_tag(cl.get("exit_reason", ""), cl["net_pnl"],
                                 cl.get("entry_zscore"), cl.get("exit_zscore"))
    if op is not None:
        op["net_pnl"] = 0.0
        op["charges_on_close"] = True


# ── 1. repair on start ────────────────────────────────────────────────────────
def repair_on_start(trade_log, multiplier_for: Callable[[str], Optional[float]],
                    rates_for: Callable[[Dict], Dict], brokerage_per_order: float,
                    gst_pct: float = costs.GST_PCT, cgt_pct: float = 0.0,
                    backup: bool = True) -> Dict:
    """Bring every row up to date without the operator running anything:
    MCX rows at ×1 rescaled to the contract multiplier (``multiplier_for(
    leg_b symbol)``), then each settled round trip not yet on the rate card
    re-costed from its journaled leg prices (one order per leg per side —
    the tradebook import refines that with Arrow's actual orders)."""
    def mult(rec):
        syms = pair_symbols(rec)
        return multiplier_for(syms[1]) if syms else None

    rescaled = trade_log.rescale_lot_size(mult, capital_gains_pct=cgt_pct, apply=False)
    with trade_log._lock:
        trades = trade_log._trades
        todo = [(o, c) for o, c in _pairs(trades)
                if trades[c].get("charges_model") != "arrow_rate_card"]
    if not rescaled and not todo:
        return {"rescaled": 0, "recosted": 0, "backup": None}
    bak = _backup(trade_log) if backup else None
    trade_log.rescale_lot_size(mult, capital_gains_pct=cgt_pct, apply=True)
    n = 0
    with trade_log._lock:
        trades = trade_log._trades
        for o, c in _pairs(trades):
            op, cl = trades[o], trades[c]
            if cl.get("charges_model") == "arrow_rate_card":
                continue
            m = float(cl.get("lot_size") or 1)
            fills = journal_round_trip_fills(op, cl, m, m)
            if not fills:
                continue
            ch = costs.fills_charges(fills, rates_for, brokerage_per_order, gst_pct)
            settle_close(cl, fills, ch, cgt_pct, op)
            n += 1
        trade_log._save()
    if rescaled or n:
        logger.info("Journal repair: {} row(s) rescaled to the contract multiplier, "
                    "{} round trip(s) re-costed on Arrow's rate card (backup: {})",
                    len(rescaled), n, bak)
    return {"rescaled": len(rescaled), "recosted": n, "backup": str(bak) if bak else None}


def _backup(trade_log, tag: str = "before-repair") -> Optional[Path]:
    path = getattr(trade_log, "path", None)
    if not path or not Path(path).exists():
        return None
    bak = Path(path).with_name(f"{Path(path).stem}.{tag}.{time.strftime('%Y%m%d-%H%M%S')}"
                               f"{Path(path).suffix}")
    shutil.copy2(path, bak)
    return bak


# ── 3. tradebook import ───────────────────────────────────────────────────────
def _orders(fills: List[Dict], attributed: Dict[str, float]) -> List[Dict]:
    """Tradebook fills grouped by Arrow order (one order may fill as several
    trades): units, VWAP, first-fill time — less units already attributed
    to journal rows by an earlier import."""
    by: Dict[str, Dict] = {}
    for i, f in enumerate(fills):
        oid = f.get("order_id") or f"trade-{f.get('trade_id') or i}"
        o = by.setdefault(oid, {"order_id": oid, "key": f["key"], "symbol": f["symbol"],
                                "exchange": f.get("exchange", ""), "side": f["side"],
                                "units": 0.0, "value": 0.0, "ts": f["ts"], "last_ts": f["ts"],
                                "trades": [], "days": defaultdict(float)})
        o["units"] += f["units"]
        o["value"] += f["units"] * f["price"]
        o["ts"], o["last_ts"] = min(o["ts"], f["ts"]), max(o["last_ts"], f["ts"])
        o["trades"].append(f.get("trade_id"))
        o["days"][f.get("date") or datetime.fromtimestamp(f["ts"], _IST).strftime("%Y-%m-%d")] \
            += f["units"] * f["price"]
    out = []
    for o in by.values():
        o["price"] = o["value"] / o["units"]
        o["total_units"] = o["units"]
        o["left"] = o["units"] - float(attributed.get(o["order_id"], 0.0))
        out.append(o)
    return sorted(out, key=lambda o: o["ts"])


def _day_factors(fills: List[Dict], rates_for_exchange: Callable[[str], Dict]) -> Dict[str, Tuple[float, float]]:
    """{day: (stt_factor, stamp_factor)} — Arrow's contract note rounds CTT/STT
    and stamp to whole rupees per day; the factor carries that rounding back
    onto the day's fills so per-trade charges add up to the statement."""
    raw: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0])
    for f in fills:
        r = rates_for_exchange(f) or {}
        t = f["units"] * f["price"]
        if f["side"] == "sell":
            raw[f["date"]][0] += float(r.get("stt_sell_pct", 0) or 0) / 100.0 * t
        else:
            raw[f["date"]][0] += float(r.get("stt_buy_pct", 0) or 0) / 100.0 * t
            raw[f["date"]][1] += float(r.get("stamp_buy_pct", 0) or 0) / 100.0 * t
    return {d: ((round(v[0]) / v[0]) if v[0] > _EPS else 1.0,
                (round(v[1]) / v[1]) if v[1] > _EPS else 1.0) for d, v in raw.items()}


def _portion(o: Dict, units: float, factors: Dict) -> Dict:
    """A share of an Arrow order, as a costed fill."""
    day = max(o["days"], key=o["days"].get)
    sf, mf = factors.get(day, (1.0, 1.0))
    return {"order_id": o["order_id"], "symbol": o["symbol"], "key": o["key"],
            "exchange": o["exchange"], "side": o["side"], "units": units, "price": o["price"],
            "ts": o["ts"], "order_units": o["total_units"], "stt_factor": sf, "stamp_factor": mf}


def plan_import(trades: List[Dict], fills: List[Dict], *,
                multiplier_for: Callable[[str], Optional[float]],
                rates_for: Callable[[Dict], Dict],
                brokerage_per_order: float = 20.0, gst_pct: float = costs.GST_PCT,
                cgt_pct: float = 0.0, pair: Optional[Tuple[str, str]] = None) -> Dict:
    """What importing ``fills`` (``fill_check.read_tradebook``) would change.
    ``trades`` is the journal, oldest first. Nothing is modified.

    Returns {"corrections": [...], "new_trips": [...], "unmatched_journal":
    [...], "still_open": [...], "arrow": {...}, "journal_before": {...},
    "journal_after": {...}, "updates": {row index: {field: value}},
    "new_rows": [rows to append]}."""
    if not fills:
        return _empty_plan()
    d0, d1 = min(f["date"] for f in fills), max(f["date"] for f in fills)
    attributed: Dict[str, float] = defaultdict(float)
    for r in trades:
        for a in r.get("arrow_orders") or []:
            attributed[str(a.get("order_id"))] += float(a.get("units") or 0)
    orders = _orders(fills, attributed)
    factors = _day_factors(fills, rates_for)

    def mult(sym: str, rec: Optional[Dict] = None) -> float:
        return float(multiplier_for(sym) or (rec or {}).get("lot_size") or 1)

    # ── match every LIVE, not-yet-verified journal leg to Arrow orders
    legs = []        # (row index, 'a'|'b', key, side, units, price, ts)
    for i, r in enumerate(trades):
        if r.get("action") not in ("OPEN", "CLOSE") or r.get("arrow_verified"):
            continue
        if r.get("dry_run") or str(r.get("status", "")).upper() != "LIVE":
            continue
        day = datetime.fromtimestamp(float(r.get("ts") or 0), _IST).strftime("%Y-%m-%d")
        lookback_day = datetime.fromtimestamp(float(r.get("ts") or 0) - MATCH_LOOKBACK_SEC,
                                              _IST).strftime("%Y-%m-%d")
        if day < d0 or lookback_day > d1:
            continue                      # this row is outside the tradebook's dates
        syms = pair_symbols(r)
        if not syms:
            continue
        sa, sb = _leg_sides(r.get("direction"), r["action"])
        lots = float(r.get("lots") or 0)
        for leg, sym, side, px in (("a", syms[0], sa, r.get("leg_a_price")),
                                   ("b", syms[1], sb, r.get("leg_b_price"))):
            if px and lots > 0:
                legs.append((i, leg, contract_key(sym), side, lots * mult(sym, r),
                             float(px), float(r["ts"])))
    matched: Dict[Tuple[int, str], List[Dict]] = defaultdict(list)
    unmatched = []
    for i, leg, key, side, units, px, ts in sorted(legs, key=lambda x: x[6]):
        need = units
        cands = [o for o in orders if o["key"] == key and o["side"] == side and o["left"] > _EPS
                 and ts - MATCH_LOOKBACK_SEC <= o["ts"] <= ts + MATCH_LOOKAHEAD_SEC]
        cands.sort(key=lambda o: (abs(o["price"] - px) / px * 1e4 + abs(o["ts"] - ts) / 3600.0))
        for o in cands:
            take = min(need, o["left"])
            o["left"] -= take
            need -= take
            matched[(i, leg)].append(_portion(o, take, factors))
            if need <= _EPS:
                break
        if need > _EPS:
            # Arrow holds less than the journal says: leave this row as
            # journaled (no correction) and report it. The units it did
            # match stay consumed, so they are not rebuilt into a second,
            # duplicate trade.
            unmatched.append({"row": i, "leg": leg, "symbol": trades[i].get("name"),
                              "side": side, "units": round(need, 4), "price": px,
                              "when": _fmt(ts), "action": trades[i].get("action")})
            matched.pop((i, leg), None)

    # ── corrections to matched rows + their round trips re-settled
    updates: Dict[int, Dict] = {}
    corrections = []
    rows_ok = {i for (i, _l) in matched} - {u["row"] for u in unmatched}
    for i in sorted(rows_ok):
        r = trades[i]
        if (i, "a") not in matched or (i, "b") not in matched:
            continue
        pa, pb = matched[(i, "a")], matched[(i, "b")]
        vw = lambda ps: sum(p["units"] * p["price"] for p in ps) / sum(p["units"] for p in ps)  # noqa: E731
        new_a, new_b = round(vw(pa), 4), round(vw(pb), 4)
        new_ts = max(p["ts"] for p in pa + pb)
        u = {"leg_a_price": new_a, "leg_b_price": new_b, "arrow_verified": True,
             "arrow_orders": [{"order_id": p["order_id"], "units": p["units"]} for p in pa + pb],
             "_arrow_fills": pa + pb}
        changed = []
        if abs(new_a - float(r.get("leg_a_price") or 0)) > 1e-6:
            changed.append(f"leg A {r.get('leg_a_price')} → {new_a:g}")
        if abs(new_b - float(r.get("leg_b_price") or 0)) > 1e-6:
            changed.append(f"leg B {r.get('leg_b_price')} → {new_b:g}")
        if abs(new_ts - float(r["ts"])) > 120:
            u["ts"] = new_ts
            u["time"] = datetime.fromtimestamp(new_ts, _IST).strftime("%H:%M:%S")
            u["journaled_ts"] = r["ts"]
            changed.append(f"time {_fmt(r['ts'])} → {_fmt(new_ts)} (Arrow's fill)")
        updates[i] = u
        if changed:
            corrections.append({"row": i, "action": r["action"], "name": r.get("name"),
                                "when": _fmt(r["ts"]), "changes": changed})

    after = [dict(r, **{k: v for k, v in updates.get(i, {}).items()})
             for i, r in enumerate(trades)]
    before_tot = _journal_totals(trades, d0, d1)
    resettled = []
    for o, c in _pairs(after):
        if o not in updates and c not in updates:
            continue
        op, cl = after[o], after[c]
        m = mult((pair_symbols(cl) or ("", ""))[1], cl)
        if "_arrow_fills" in updates.get(o, {}) and "_arrow_fills" in updates.get(c, {}):
            fills_rt = updates[o]["_arrow_fills"] + updates[c]["_arrow_fills"]
        else:
            fills_rt = journal_round_trip_fills(op, cl, m, m)
        if not fills_rt:
            continue
        cl = dict(cl)
        _refresh_trip_fields(op, cl)
        ch = costs.fills_charges(fills_rt, rates_for, brokerage_per_order, gst_pct)
        op2 = dict(op)
        settle_close(cl, fills_rt, ch, cgt_pct, op2)
        updates.setdefault(c, {}).update({k: cl[k] for k in _SETTLED_FIELDS if k in cl})
        updates.setdefault(o, {}).update(net_pnl=op2["net_pnl"], charges_on_close=True)
        resettled.append(c)

    # ── what is left at Arrow → round trips of its own
    left = [o for o in orders if o["left"] > _EPS]
    new_trips, still_open = _rebuild_trips(left, factors, mult, pair)
    new_rows = []
    for t in new_trips:
        ch = costs.fills_charges(t["fills"], rates_for, brokerage_per_order, gst_pct)
        op, cl = _trip_rows(t, mult)
        settle_close(cl, t["fills"], ch, cgt_pct, op)
        t.update(gross=cl["spread_pnl"], charges=ch["total"], net=cl["net_pnl"])
        new_rows += [op, cl]

    # ── totals: Arrow (its own fills, rate card) vs the journal before / after
    all_portions = [_portion(o, o["total_units"], factors) for o in _orders(fills, {})]
    arrow_ch = costs.fills_charges(all_portions, rates_for, brokerage_per_order, gst_pct)
    arrow = {"gross": round(_gross(all_portions), 2), "charges": arrow_ch["total"],
             "charges_detail": arrow_ch,
             "net": round(_gross(all_portions) - arrow_ch["total"], 2),
             "fills": len(fills), "orders": len(all_portions), "date_from": d0, "date_to": d1}
    merged = []
    for i, r in enumerate(trades):
        x = dict(r)
        x.update({k: v for k, v in updates.get(i, {}).items() if not k.startswith("_")})
        merged.append(x)
    after_tot = _journal_totals(merged + new_rows, d0, d1)
    for u in updates.values():
        u.pop("_arrow_fills", None)
    return {"corrections": corrections, "resettled": len(resettled),
            "new_trips": [{k: v for k, v in t.items() if k != "fills"} for t in new_trips],
            "unmatched_journal": unmatched, "still_open": still_open,
            "arrow": arrow, "journal_before": before_tot, "journal_after": after_tot,
            "updates": updates, "new_rows": new_rows,
            "changes": bool(updates or new_rows)}


_SETTLED_FIELDS = ("spread_pnl", "slippage_pnl", "brokerage", "stt", "other_cost", "charges",
                   "charges_model", "cgt", "net_pnl", "outcome", "entry_leg_a", "entry_leg_b",
                   "exit_leg_a", "exit_leg_b", "entry_spread", "exit_spread", "held_sec")


def _refresh_trip_fields(op: Dict, cl: Dict) -> None:
    """Entry/exit legs, spreads and holding time from the (corrected) rows."""
    cl["entry_leg_a"], cl["entry_leg_b"] = op.get("leg_a_price"), op.get("leg_b_price")
    cl["exit_leg_a"], cl["exit_leg_b"] = cl.get("leg_a_price"), cl.get("leg_b_price")
    try:
        k = 1.0
        ea, eb = float(op["leg_a_price"]), float(op["leg_b_price"])
        if op.get("entry_spread") is not None and abs((ea - eb) - float(op["entry_spread"])) > 0.5:
            return                                     # a hedge-scaled pair: keep its spreads
        cl["entry_spread"] = round(k * ea - eb, 4)
        cl["exit_spread"] = round(k * float(cl["leg_a_price"]) - float(cl["leg_b_price"]), 4)
    except (TypeError, ValueError, KeyError):
        pass
    cl["held_sec"] = round(float(cl["ts"]) - float(op["ts"]), 1)


def _rebuild_trips(orders: List[Dict], factors: Dict, mult, pair) -> Tuple[List[Dict], List[Dict]]:
    """Group leftover Arrow orders into round trips: a trade starts with the
    first fill out of flat and ends when every contract in it is flat again."""
    trips, cur, pos = [], None, defaultdict(float)
    for o in sorted(orders, key=lambda o: o["ts"]):
        p = _portion(o, o["left"], factors)
        if cur is None:
            cur = {"fills": [], "keys": []}
        cur["fills"].append(p)
        if p["key"] not in cur["keys"]:
            cur["keys"].append(p["key"])
        pos[p["key"]] += p["units"] if p["side"] == "buy" else -p["units"]
        if all(abs(v) < _EPS for v in pos.values()):
            trips.append(cur)
            cur, pos = None, defaultdict(float)
    still_open = []
    if cur is not None:
        still_open = [{"symbol": f["symbol"], "side": f["side"], "units": f["units"],
                       "price": f["price"], "when": _fmt(f["ts"])} for f in cur["fills"]]
    out = []
    for t in trips:
        syms: Dict[str, str] = {}
        for f in t["fills"]:
            syms.setdefault(f["key"], f["symbol"])
        keys = list(syms)
        if pair and set(contract_key(s) for s in pair) >= set(keys):
            order = [contract_key(s) for s in pair if contract_key(s) in syms]
        else:
            order = sorted(keys, key=_expiry_sort)
        a = order[0]
        b = order[1] if len(order) > 1 else None
        first = t["fills"][0]
        short = (first["key"] == a and first["side"] == "sell") or (first["key"] == b and first["side"] == "buy")
        legs = {}
        for k in order:
            fs = [f for f in t["fills"] if f["key"] == k]
            opening_side = None
            run, peak = 0.0, 0.0
            for f in fs:
                run += f["units"] if f["side"] == "buy" else -f["units"]
                peak = max(peak, abs(run))
            opening_side = fs[0]["side"]
            opn = [f for f in fs if f["side"] == opening_side]
            cls = [f for f in fs if f["side"] != opening_side]
            vw = lambda ps: (sum(p["units"] * p["price"] for p in ps) / sum(p["units"] for p in ps)) if ps else None  # noqa: E731
            m = mult(syms[k])
            legs[k] = {"symbol": syms[k], "entry": vw(opn), "exit": vw(cls),
                       "lots": round(peak / m, 4) if m else peak}
        out.append({"fills": t["fills"], "direction": "SHORT_SPREAD" if short else "LONG_SPREAD",
                    "leg_a": legs[a], "leg_b": legs.get(b) if b else None,
                    "open_ts": t["fills"][0]["ts"], "close_ts": t["fills"][-1]["ts"],
                    "opened": _fmt(t["fills"][0]["ts"]), "closed": _fmt(t["fills"][-1]["ts"]),
                    "name": f"{legs[a]['symbol']} − {legs[b]['symbol']}" if b else legs[a]["symbol"],
                    "orders": sorted({f["order_id"] for f in t["fills"]}),
                    "balanced": (b is None or abs(legs[a]["lots"] - legs[b]["lots"]) < _EPS)})
    return out, still_open


def _expiry_sort(key: str):
    m = re.search(r"(\d{2})([A-Z]{3})(\d{2})", key)
    if not m:
        return (9999, 99, 99, key)
    try:
        d = datetime.strptime(f"{m.group(1)}{m.group(2).title()}{m.group(3)}", "%d%b%y")
        return (d.year, d.month, d.day, key)
    except ValueError:
        return (9999, 99, 99, key)


def _trip_rows(t: Dict, mult) -> Tuple[Dict, Dict]:
    """OPEN + CLOSE rows for a round trip rebuilt from Arrow's fills."""
    a, b = t["leg_a"], t["leg_b"] or {"symbol": "", "entry": None, "exit": None, "lots": 0}
    lots = a["lots"]
    lots_i = int(round(lots)) if abs(lots - round(lots)) < 1e-6 else lots
    note = "" if t["balanced"] else (f" — unbalanced: leg A {a['lots']:g} lot(s), "
                                     f"leg B {b['lots']:g} lot(s)")
    common = {"source": "arrow", "status": "LIVE", "dry_run": False, "direction": t["direction"],
              "name": t["name"], "lots": lots_i, "lot_size": int(round(mult(a["symbol"]))),
              "imported": True, "arrow_verified": True, "zscore": None,
              "lots_b": b["lots"], "unbalanced": not t["balanced"]}
    op = dict(common, action="OPEN", ts=t["open_ts"],
              time=datetime.fromtimestamp(t["open_ts"], _IST).strftime("%H:%M:%S"),
              leg_a_price=a["entry"], leg_b_price=b["entry"],
              entry_spread=(round(a["entry"] - b["entry"], 4) if b["entry"] is not None else None),
              exit_spread=None, net_pnl=0.0, brokerage=0.0, _closed=True,
              arrow_orders=[{"order_id": f["order_id"], "units": f["units"]} for f in t["fills"]])
    cl = dict(common, action="CLOSE", ts=t["close_ts"],
              time=datetime.fromtimestamp(t["close_ts"], _IST).strftime("%H:%M:%S"),
              leg_a_price=a["exit"], leg_b_price=b["exit"],
              entry_spread=op["entry_spread"],
              exit_spread=(round(a["exit"] - b["exit"], 4)
                           if a["exit"] is not None and b["exit"] is not None else None),
              entry_leg_a=a["entry"], entry_leg_b=b["entry"],
              exit_leg_a=a["exit"], exit_leg_b=b["exit"],
              entry_zscore=None, exit_zscore=None, entry_source="arrow", exit_source="arrow",
              exit_reason=IMPORTED_REASON + note, edge_pnl=0.0, slippage_pnl=0.0,
              held_sec=round(t["close_ts"] - t["open_ts"], 1), arrow_orders=[])
    return op, cl


def _journal_totals(trades: List[Dict], d0: str, d1: str) -> Dict:
    """Gross / charges / net of LIVE round trips CLOSED within [d0, d1]."""
    g = c = n = 0.0
    k = 0
    for r in trades:
        if r.get("action") != "CLOSE" or r.get("entry_spread") is None:
            continue
        if r.get("dry_run") or str(r.get("status", "")).upper() != "LIVE":
            continue
        day = datetime.fromtimestamp(float(r.get("ts") or 0), _IST).strftime("%Y-%m-%d")
        if not (d0 <= day <= d1):
            continue
        g += float(r.get("spread_pnl") or 0)
        n += float(r.get("net_pnl") or 0)
        k += 1
    c = g - n
    return {"gross": round(g, 2), "charges": round(c, 2), "net": round(n, 2), "trips": k}


def _empty_plan() -> Dict:
    z = {"gross": 0.0, "charges": 0.0, "net": 0.0}
    return {"corrections": [], "resettled": 0, "new_trips": [], "unmatched_journal": [],
            "still_open": [], "arrow": dict(z, fills=0, orders=0), "journal_before": dict(z, trips=0),
            "journal_after": dict(z, trips=0), "updates": {}, "new_rows": [], "changes": False}


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), _IST).strftime("%d %b %H:%M:%S")


def apply_import(trade_log, plan: Dict) -> Dict:
    """Write a plan from ``plan_import`` (computed on the same journal). The
    file is backed up first; rows are re-ordered by time so the Trades table
    and running totals read chronologically."""
    if not plan.get("changes"):
        return {"applied": False, "backup": None}
    bak = _backup(trade_log, "before-arrow-import")
    with trade_log._lock:
        trades = trade_log._trades
        for i, u in plan["updates"].items():
            trades[int(i)].update({k: v for k, v in u.items() if not k.startswith("_")})
        trades.extend(dict(r) for r in plan["new_rows"])
        trades.sort(key=lambda r: float(r.get("ts") or 0))
        trade_log._save()
    logger.info("Arrow import: {} row(s) corrected/re-costed, {} round trip(s) added (backup: {})",
                len(plan["updates"]), len(plan["new_rows"]) // 2, bak)
    return {"applied": True, "backup": str(bak) if bak else None,
            "updated": len(plan["updates"]), "added": len(plan["new_rows"]) // 2}


def default_rates_for(segment_rates: Callable[[str], Optional[str]], overrides: Optional[Dict] = None):
    """rates_for(fill) — the cost table's rates for a fill's segment: the
    current leg's segment when ``segment_rates(symbol)`` knows it, else the
    tradebook's exchange column (MCX → mcx_fo …)."""
    def rates_for(f: Dict) -> Dict:
        key = segment_rates(str(f.get("symbol") or "")) if segment_rates else None
        if not key:
            key = costs.EXCHANGE_COST_KEYS.get(str(f.get("exchange") or "").upper().replace("_", ""))
        if not key and mcx_multiplier(str(f.get("symbol") or "")):
            key = "mcx_fo"
        return costs.resolve_segment_costs(key or "", overrides)
    return rates_for
