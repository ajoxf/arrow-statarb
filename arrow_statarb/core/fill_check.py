"""Untracked-fills check: Arrow's fills vs the Nexus trade journal.

The journal's P&L only covers the spread trades Nexus recorded. Anything else
that filled at Arrow (a leg closed by hand, an unwind after an aborted entry, a
Ladder click, an order placed in Arrow's own app) is in Arrow's P&L report but
not in the journal, so the two never agree. This module finds that gap.

Both sides are reduced to the same shape, per contract and side:

    units  — P&L units (the quantity whose value is units × price: barrels for
             CRUDEOILM, i.e. lots × the contract multiplier)
    value  — Σ units × price (₹)

so realised P&L for a contract is sell_value − buy_value once the quantities
net to zero. ``reconcile`` returns, per contract, what Arrow shows, what the
journal implies, and the residual (Arrow − journal) — the fills Nexus never
journaled, with their average prices and the P&L they carry.

Two sources for the Arrow side:
  * ``broker_fills_from_orders`` — the day's order book (live, today only);
  * ``read_pnl_report`` — Arrow's P&L report download, any date range.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional

_IST = timezone(timedelta(hours=5, minutes=30))
_EPS = 1e-6

# BASE + day + month + 2-digit year, in either spelling:
#   CRUDEOILM19NOV26F  ·  CRUDEOILM 19th NOV 26 FUT
_KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9&]*?)[\s-]*(\d{1,2})(?:ST|ND|RD|TH)?[\s-]*([A-Z]{3})[\s-]*'?(\d{4}|\d{2})(?!\d)"
                     r"(?:\s*(?:FUT|F)\b|\s*F(?=$))?\s*(?:(CE|PE|C|P)\s*([\d.]+)|([\d.]+)\s*(CE|PE))?")


def contract_key(symbol: str) -> str:
    """One name per contract, whichever way it is spelled: Arrow's trading
    symbol 'CRUDEOILM19NOV26F' and its report's 'CRUDEOILM 19th NOV 26 FUT'
    both give 'CRUDEOILM 19NOV26'. Unparseable names are returned upper-cased."""
    s = str(symbol or "").upper().strip()
    m = _KEY_RE.match(s)
    if not m:
        return re.sub(r"\s+", " ", s)
    base, day, mon, yr, opt1, k1, k2, opt2 = m.groups()
    key = f"{base} {int(day):02d}{mon}{yr[-2:]}"
    opt, strike = (opt1 or opt2), (k1 or k2)
    if opt and strike:                       # an option is its own contract
        key += f" {float(strike):g}{opt[0]}E"
    return key


def _side(v) -> Optional[str]:
    s = str(v or "").strip().upper()
    if s in ("B", "BUY", "BOT", "BOUGHT"):
        return "buy"
    if s in ("S", "SELL", "SLD", "SOLD"):
        return "sell"
    return None


def _ist_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), _IST).strftime("%Y-%m-%d")


# ── journal side ──────────────────────────────────────────────────────────────
def journal_fills(trades: Iterable[Dict], date_from: Optional[str] = None,
                  date_to: Optional[str] = None, live_only: bool = True,
                  units_per_lot: Optional[Callable[[str, Dict], Optional[float]]] = None
                  ) -> List[Dict]:
    """The per-leg fills the journal implies. Each OPEN/CLOSE row is two leg
    fills: LONG_SPREAD opens buy A / sell B, SHORT_SPREAD opens sell A / buy B,
    a CLOSE reverses. ``date_from``/``date_to`` are IST 'YYYY-MM-DD',
    inclusive. Only LIVE rows count unless ``live_only`` is False (dry-run and
    live-sim never reach the exchange). ``units_per_lot(symbol, rec)`` may
    supply the contract multiplier; default is the row's journaled lot_size."""
    out: List[Dict] = []
    for rec in trades or []:
        action = rec.get("action")
        if action not in ("OPEN", "CLOSE") or rec.get("ts") is None:
            continue
        if live_only and (rec.get("dry_run") or str(rec.get("status", "")).upper() != "LIVE"):
            continue
        day = _ist_day(rec["ts"])
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        name = str(rec.get("name") or "")
        parts = re.split(r"\s+[−-]\s+", name, maxsplit=1)
        if len(parts) != 2:
            continue
        sym_a, sym_b = parts[0].strip(), parts[1].strip()
        long_ = rec.get("direction") == "LONG_SPREAD"
        buy_a = long_ if action == "OPEN" else not long_
        lots_a = float(rec.get("lots") or 0)
        lots_b = float(rec.get("lots_b") or lots_a)      # an unbalanced (imported) trade
        for sym, px, side, lots in ((sym_a, rec.get("leg_a_price"), "buy" if buy_a else "sell", lots_a),
                                    (sym_b, rec.get("leg_b_price"), "sell" if buy_a else "buy", lots_b)):
            if not px or lots <= 0:
                continue
            mult = None
            if units_per_lot is not None:
                try:
                    mult = units_per_lot(sym, rec)
                except Exception:
                    mult = None
            mult = float(mult or rec.get("lot_size") or 1)
            out.append({"key": contract_key(sym), "symbol": sym, "side": side,
                        "units": lots * mult, "price": float(px), "ts": float(rec["ts"]),
                        "action": action, "direction": rec.get("direction"),
                        "estimated": "estimated" in str(rec.get("exit_reason") or "")})
    return out


# ── Arrow side ────────────────────────────────────────────────────────────────
def broker_fills_from_orders(orders: Iterable[Dict],
                             units_per_qty: Callable[[Dict], Optional[float]]) -> List[Dict]:
    """Executed fills from Arrow's order book (``ArrowBroker.get_order_book``).
    ``units_per_qty(order)`` converts the order quantity to P&L units (the
    contract multiplier ÷ the broker order lot); None → that symbol's ₹ cannot
    be valued, and its fills are reported by quantity only."""
    out: List[Dict] = []
    for o in orders or []:
        if str(o.get("status", "")).upper() not in ("COMPLETE", "PARTIAL"):
            continue
        side = _side(o.get("side"))
        qty = float(o.get("fill_qty") or 0)
        px = float(o.get("fill_price") or 0)
        if side is None or qty <= 0 or px <= 0:
            continue
        sym = str(o.get("symbol") or "")
        try:
            k = units_per_qty(o)
        except Exception:
            k = None
        out.append({"key": contract_key(sym), "symbol": sym, "side": side,
                    "units": qty * float(k) if k else qty, "valued": bool(k),
                    "price": px, "time": str(o.get("time") or ""),
                    "order_id": str(o.get("order_id") or "")})
    return out


def _num(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace(",", "").replace("₹", "").strip()
    if s in ("", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_header(h) -> str:
    return re.sub(r"[^a-z]", "", str(h or "").lower())


_COLS = {"symbol": ("symbol", "scrip", "instrument", "contract", "tradingsymbol"),
         "buy_qty": ("buyqty", "buyquantity", "boughtqty"),
         "buy_avg": ("buyavg", "buyavgprice", "avgbuyprice", "buyprice", "buyaverage"),
         "buy_value": ("buyvalue", "buyamount", "boughtvalue"),
         "sell_qty": ("sellqty", "sellquantity", "soldqty"),
         "sell_avg": ("sellavg", "sellavgprice", "avgsellprice", "sellprice", "sellaverage"),
         "sell_value": ("sellvalue", "sellamount", "soldvalue"),
         "realised": ("realisedpl", "realizedpl", "realisedpnl", "realizedpnl", "realised", "realized"),
         "charges": ("charges", "totalcharges")}


def _rows_from_table(table: List[List]) -> List[Dict]:
    """Find the header row (it has a symbol column and buy/sell quantities;
    downloads usually put a summary block above it) and read the rows under it."""
    for i, row in enumerate(table):
        heads = [_norm_header(c) for c in row]
        idx = {}
        for field, names in _COLS.items():
            for j, h in enumerate(heads):
                if h in names:
                    idx.setdefault(field, j)
        if {"symbol", "buy_qty", "sell_qty"} <= idx.keys() and (
                ("buy_value" in idx or "buy_avg" in idx) and ("sell_value" in idx or "sell_avg" in idx)):
            out = []
            for r in table[i + 1:]:
                cell = lambda f: r[idx[f]] if f in idx and idx[f] < len(r) else None  # noqa: E731
                sym = str(cell("symbol") or "").strip()
                bq, sq = _num(cell("buy_qty")) or 0.0, _num(cell("sell_qty")) or 0.0
                if not sym or (bq == 0 and sq == 0):
                    continue
                bv, sv = _num(cell("buy_value")), _num(cell("sell_value"))
                if bv is None:
                    bv = bq * (_num(cell("buy_avg")) or 0.0)
                if sv is None:
                    sv = sq * (_num(cell("sell_avg")) or 0.0)
                out.append({"symbol": sym, "key": contract_key(sym),
                            "buy_units": bq, "buy_value": bv, "sell_units": sq, "sell_value": sv,
                            "realised": _num(cell("realised")), "charges": _num(cell("charges"))})
            return out
    raise ValueError("no table with Symbol / Buy Qty / Sell Qty / Buy & Sell Value (or Avg) "
                     "columns was found in the file")


def _tables(data: bytes, filename: str = "") -> List[List[List]]:
    """Every table in a download: the CSV, or each sheet of an XLSX."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        try:
            import openpyxl  # noqa: WPS433 — optional dependency
        except ImportError as exc:
            raise ValueError("reading .xlsx needs openpyxl (pip install openpyxl) — "
                             "or save the file as CSV") from exc
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return [[list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    text = data.decode("utf-8-sig", errors="replace")
    return [list(csv.reader(io.StringIO(text)))]


def read_pnl_report(data: bytes, filename: str = "") -> List[Dict]:
    """Per-contract rows from Arrow's P&L report download (CSV, or XLSX when
    openpyxl is installed). Quantities are as the report shows them — P&L units
    (CRUDEOILM: barrels), so value = qty × price."""
    for table in _tables(data, filename):
        try:
            return _rows_from_table(table)
        except ValueError:
            continue
    raise ValueError("no table with Symbol / Buy Qty / Sell Qty / Buy & Sell Value (or Avg) "
                     "columns was found in the file")


_TB_COLS = {"symbol": ("symbol", "tradingsymbol", "scrip", "instrument", "contract"),
            "exchange": ("exchange", "exch", "segment", "exchangesegment"),
            "date": ("date", "tradedate", "orderdate"),
            "time": ("time", "tradetime", "ordertime", "exchtime", "exchangetime"),
            "side": ("buysell", "side", "transactiontype", "tradetype", "type", "bs"),
            "order_id": ("orderid", "orderno", "ordernumber", "exchangeorderid"),
            "trade_id": ("tradeid", "tradeno", "tradenumber", "fillid"),
            "qty": ("qty", "quantity", "tradedqty", "filledqty"),
            "price": ("avgprice", "price", "tradeprice", "tradedprice", "fillprice", "rate")}

_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%d-%m-%y")


def _parse_ts(date_v, time_v) -> Optional[float]:
    """IST epoch from the tradebook's date and time cells (a date cell may
    already carry the time, or be a datetime from a spreadsheet)."""
    if isinstance(date_v, datetime):
        d = date_v
        if time_v is not None and not isinstance(time_v, str) and hasattr(time_v, "hour"):
            d = d.replace(hour=time_v.hour, minute=time_v.minute, second=time_v.second)
        elif isinstance(time_v, str) and time_v.strip():
            h, m, *sec = [int(float(x)) for x in time_v.strip().split(":")]
            d = d.replace(hour=h, minute=m, second=(sec[0] if sec else 0))
        return d.replace(tzinfo=_IST).timestamp()
    ds = str(date_v or "").strip()
    ts_ = str(time_v or "").strip() if not hasattr(time_v, "hour") else time_v.strftime("%H:%M:%S")
    if " " in ds and not ts_ and re.search(r"\d:\d", ds):
        ds, ts_ = ds.rsplit(" ", 1)
    for fmt in _DATE_FORMATS:
        try:
            day = datetime.strptime(ds, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if ts_:
        parts = [int(float(x)) for x in ts_.split(":")]
        day = day.replace(hour=parts[0], minute=parts[1] if len(parts) > 1 else 0,
                          second=parts[2] if len(parts) > 2 else 0)
    return day.replace(tzinfo=_IST).timestamp()


def _tradebook_from_table(table: List[List]) -> List[Dict]:
    """Fills from Arrow's tradebook download: one row per exchange trade
    (Symbol, Exchange, Date, Time, Buy sell, Order ID, Trade ID, Qty, Avg
    price). Qty is in P&L units, as in the P&L report."""
    for i, row in enumerate(table):
        heads = [_norm_header(c) for c in row]
        idx: Dict[str, int] = {}
        for field, names in _TB_COLS.items():
            for j, h in enumerate(heads):
                if h in names:
                    idx.setdefault(field, j)
        if not {"symbol", "side", "qty", "price", "date"} <= idx.keys():
            continue
        out = []
        for r in table[i + 1:]:
            cell = lambda f: r[idx[f]] if f in idx and idx[f] < len(r) else None  # noqa: E731
            sym = str(cell("symbol") or "").strip()
            side = _side(cell("side"))
            qty, px = _num(cell("qty")), _num(cell("price"))
            ts = _parse_ts(cell("date"), cell("time"))
            if not sym or side is None or not qty or not px or ts is None:
                continue
            out.append({"key": contract_key(sym), "symbol": sym, "side": side,
                        "units": abs(qty), "price": px, "ts": ts,
                        "exchange": str(cell("exchange") or "").strip().upper(),
                        "order_id": str(cell("order_id") or "").strip(),
                        "trade_id": str(cell("trade_id") or "").strip(),
                        "date": _ist_day(ts)})
        out.sort(key=lambda f: (f["ts"], f["trade_id"]))
        return out
    raise ValueError("not a tradebook")


def read_tradebook(data: bytes, filename: str = "") -> List[Dict]:
    """Fills from Arrow's tradebook download (CSV or XLSX)."""
    for table in _tables(data, filename):
        try:
            return _tradebook_from_table(table)
        except ValueError:
            continue
    raise ValueError("no tradebook table (Symbol / Date / Buy-Sell / Qty / Price) was found")


def read_arrow_file(data: bytes, filename: str = "") -> Dict:
    """Either Arrow download, told apart by its columns:
    {"kind": "tradebook", "fills": [...]} or {"kind": "pnl", "rows": [...]}."""
    try:
        return {"kind": "tradebook", "fills": read_tradebook(data, filename)}
    except ValueError:
        pass
    try:
        return {"kind": "pnl", "rows": read_pnl_report(data, filename)}
    except ValueError:
        raise ValueError("this is neither Arrow's tradebook (Symbol, Date, Time, Buy sell, "
                         "Order ID, Qty, Avg price) nor its P&L report (Symbol, Buy Qty, "
                         "Buy Value/Avg, Sell Qty, Sell Value/Avg)") from None


# ── comparison ────────────────────────────────────────────────────────────────
def _totals(rows: Iterable[Dict]) -> Dict[str, Dict]:
    t: Dict[str, Dict] = {}
    for r in rows:
        d = t.setdefault(r["key"], {"symbol": r.get("symbol", r["key"]), "buy_units": 0.0,
                                    "buy_value": 0.0, "sell_units": 0.0, "sell_value": 0.0})
        if "side" in r:                                  # a single fill
            d[f"{r['side']}_units"] += r["units"]
            d[f"{r['side']}_value"] += r["units"] * r["price"]
        else:                                            # a report row
            for k in ("buy_units", "buy_value", "sell_units", "sell_value"):
                d[k] += float(r.get(k) or 0)
    return t


def _realised(d: Dict) -> Dict:
    """Realised P&L of a set of buys/sells: matched quantity at the average
    prices; any unmatched quantity is left open and reported, not valued."""
    bu, su = d["buy_units"], d["sell_units"]
    ba = d["buy_value"] / bu if bu > _EPS else None
    sa = d["sell_value"] / su if su > _EPS else None
    matched = min(bu, su)
    pnl = round(matched * (sa - ba), 2) if (ba is not None and sa is not None and matched > _EPS) else 0.0
    return {"buy_units": round(bu, 4), "buy_avg": round(ba, 4) if ba is not None else None,
            "buy_value": round(d["buy_value"], 2),
            "sell_units": round(su, 4), "sell_avg": round(sa, 4) if sa is not None else None,
            "sell_value": round(d["sell_value"], 2),
            "open_units": round(bu - su, 4), "realised": pnl}


def reconcile(arrow_rows: Iterable[Dict], journal: Iterable[Dict]) -> Dict:
    """Arrow vs journal per contract (realised P&L is gross, before charges). ``arrow_rows`` are report rows or order
    book fills; ``journal`` is ``journal_fills(...)``. The residual is what
    Arrow filled that the journal does not hold (negative units = the journal
    holds a fill Arrow does not show, e.g. an exit Nexus estimated)."""
    a, j = _totals(arrow_rows), _totals(journal)
    rows, tot = [], {"arrow": 0.0, "journal": 0.0, "untracked": 0.0}
    for key in sorted(set(a) | set(j)):
        z = {"symbol": key, "buy_units": 0.0, "buy_value": 0.0, "sell_units": 0.0, "sell_value": 0.0}
        av, jv = a.get(key, z), j.get(key, z)
        res = {k: av[k] - jv[k] for k in ("buy_units", "buy_value", "sell_units", "sell_value")}
        ar, jr = _realised(av), _realised(jv)
        rr = _realised({k: max(0.0, v) for k, v in res.items()})
        # Arrow's realised = journal's + the untracked fills' only when both
        # sides net flat; report the identity's remainder so nothing hides.
        untracked = round(ar["realised"] - jr["realised"], 2)
        clean = all(abs(v) < _EPS for v in res.values())
        rows.append({"key": key, "symbol": av.get("symbol") or jv.get("symbol") or key,
                     "arrow": ar, "journal": jr,
                     "residual": {**rr, "buy_units": round(res["buy_units"], 4),
                                  "sell_units": round(res["sell_units"], 4),
                                  "buy_value": round(res["buy_value"], 2),
                                  "sell_value": round(res["sell_value"], 2),
                                  "journal_has_extra": any(v < -_EPS for v in
                                                           (res["buy_units"], res["sell_units"]))},
                     "untracked_pnl": untracked, "matches": clean})
        tot["arrow"] += ar["realised"]
        tot["journal"] += jr["realised"]
        tot["untracked"] += untracked
    return {"contracts": rows, "totals": {k: round(v, 2) for k, v in tot.items()},
            "matches": all(r["matches"] for r in rows)}


def unmatched_fills(broker: List[Dict], journal: List[Dict]) -> Dict[str, List[Dict]]:
    """Fill-by-fill view for one day: each journal leg fill consumes the Arrow
    fills of the same contract and side closest in price (an order may be split
    into clips, so one journal fill can span several Arrow orders). What is left
    of Arrow's fills is not in the journal; what is left of the journal's was
    never filled at Arrow (or was estimated)."""
    pool = [dict(f, left=f["units"]) for f in broker]
    missing_at_arrow = []
    for jf in sorted(journal, key=lambda f: f["ts"]):
        need = jf["units"]
        cands = sorted((b for b in pool if b["key"] == jf["key"] and b["side"] == jf["side"]
                        and b["left"] > _EPS), key=lambda b: abs(b["price"] - jf["price"]))
        for b in cands:
            take = min(need, b["left"])
            b["left"] -= take
            need -= take
            if need <= _EPS:
                break
        if need > _EPS:
            missing_at_arrow.append({**jf, "units": round(need, 4)})
    not_in_journal = [{k: v for k, v in b.items() if k != "left"} | {"units": round(b["left"], 4)}
                      for b in pool if b["left"] > _EPS]
    return {"not_in_journal": not_in_journal, "missing_at_arrow": missing_at_arrow}
