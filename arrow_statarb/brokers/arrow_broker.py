"""Arrow broker integration using the pyarrow-client Python SDK.

Install:  pip install pyarrow-client

Authentication flow
-------------------
Arrow uses a 3-step automated login (handled by ``client.auto_login``):
  1. POST /auth/app/login  →  request_id
  2. Generate TOTP from totp_secret, POST /auth/validate-2fa  →  redirect_url
  3. Extract request_token from redirect_url, generate SHA-256 checksum
     (app_id:api_secret:request_token), POST /auth/app/authenticate-token  →  session token

All subsequent calls carry  ``{"token": session_token, "appID": app_id}``  headers.
The session token is valid ~24h; reconnect daily. The installed SDK (1.4.0)
``auto_login`` parameter is ``api_secret`` (the docs say ``app_secret`` — wrong).
``totp_secret`` is the base32 seed, NOT the 6-digit code. Arrow also requires a
registered static IP (SEBI), and has NO paper/sandbox — "dry-run" is enforced
above this layer by simply not calling ``submit_order``.

Exchange segment mapping (segment key → Arrow Exchange enum)
  nse_cm → NSE ; nse_fo → NFO ; bse_cm → BSE ; bse_fo → BFO ; mcx_fo → MCX

MCX commodity futures are supported from SDK 1.5.x (the ``Exchange`` enum gained
an ``MCX`` member). MCX contracts arrive in the same ``get_instruments`` master
(ExchSeg ``MCXFO``) and are indexed as futures automatically.
"""

from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional

from loguru import logger

try:
    from pyarrow_client import (
        ArrowClient,
        Exchange,
        OrderType,
        ProductType,
        QuoteMode,
        Retention,
        TransactionType,
        Variety,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning(
        "pyarrow-client SDK not found — ArrowBroker unavailable. "
        "Install: pip install pyarrow-client"
    )

from arrow_statarb.brokers.base_broker import BaseBroker

# ── Segment → Arrow Exchange ──────────────────────────────────────────────────
_SEGMENT_MAP: Dict[str, str] = {
    "nse_cm": "NSE",
    "nse_fo": "NFO",
    "bse_cm": "BSE",
    "bse_fo": "BFO",
    "mcx_fo": "MCX",
    "nse":    "NSE",
    "nfo":    "NFO",
    "bse":    "BSE",
    "bfo":    "BFO",
    "mcx":    "MCX",
}

# Segments this broker can actually trade.
SUPPORTED_SEGMENTS = frozenset(_SEGMENT_MAP.keys())

_ORDER_TYPE_MAP: Dict[str, str] = {
    "market": "MKT",
    "limit":  "LMT",
    "sl":     "SL-LMT",
    "sl-m":   "SL-MKT",
}

_PRODUCT_MAP: Dict[str, str] = {
    "NRML": "M",
    "MIS":  "I",
    "CNC":  "C",
    "nrml": "M",
    "mis":  "I",
    "cnc":  "C",
}

# Broker/NEST order-status strings → our normalized lifecycle states. Anything
# unmapped but non-empty is treated as still working (OPEN); empty → UNKNOWN.
_ORDER_STATUS_MAP: Dict[str, str] = {
    "COMPLETE": "COMPLETE", "COMPLETED": "COMPLETE", "FILLED": "COMPLETE",
    "EXECUTED": "COMPLETE", "TRADED": "COMPLETE",
    "REJECTED": "REJECTED", "REJECT": "REJECTED",
    "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
    "PARTIALLY FILLED": "PARTIAL", "PARTIAL": "PARTIAL",
    "OPEN": "OPEN", "VALIDATION PENDING": "PENDING", "PUT ORDER REQ RECEIVED": "PENDING",
    "MODIFY VALIDATION PENDING": "PENDING", "TRIGGER PENDING": "PENDING",
    "TRIGGER_PENDING": "PENDING", "OPEN PENDING": "PENDING", "PENDING": "PENDING",
    "AFTER MARKET ORDER REQ RECEIVED": "PENDING",
}


def _dig(d: Dict, *keys, default=None):
    """Case-insensitive first-present lookup across candidate keys."""
    if not isinstance(d, dict):
        return default
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return default


class ArrowBroker(BaseBroker):
    """
    Arrow broker — NSE, BSE, NFO, BFO and MCX instruments.

    Parameters
    ----------
    config : dict
        Required:
            app_id, user_id, password, api_secret, totp_secret (base32 seed)
        Optional:
            lot_sizes (dict)  ``{symbol: lot_size}`` overrides.
            token (str)       Pre-existing session token to skip the login flow.
    """

    def __init__(self, config: Dict):
        super().__init__(name="Arrow", config=config)

        self.app_id:      str = config.get("app_id", "")
        self.user_id:     str = config.get("user_id", "")
        self.password:    str = config.get("password", "")
        self.api_secret:  str = config.get("api_secret", "")
        self.totp_secret: str = config.get("totp_secret", "")

        # Optional manual lot-size overrides from config
        self._lot_sizes: Dict[str, int] = {
            k.upper(): int(v)
            for k, v in (config.get("lot_sizes") or {}).items()
        }
        # Per-symbol price tick size from the master (e.g. NIFTY fut = 0.10).
        # Orders/amends must be a multiple of this or Arrow rejects them.
        self._tick_sizes: Dict[str, float] = {}

        self._client: Optional[object] = None   # ArrowClient instance

        # Last connect failure, surfaced to the UI so the user sees the real
        # Arrow error (HTTP status + message + which auth step) instead of a
        # generic "login failed".
        self.last_error: str = ""

        # Remembers the funds method+shape we last logged, so the per-poll funds
        # read doesn't spam the log on every dashboard refresh.
        self._funds_sig = None

        # Instrument master fetched lazily from Arrow's /all endpoint
        self._instruments: List[Dict] = []
        self._instruments_ready = threading.Event()

        # Picker index, built once after the master loads (avoids scanning the
        # full ~223k-row master on every dropdown request):
        #   _idx_underlyings: {(exchange, kind): [underlying, ...]}
        #   _idx_contracts:   {(exchange, kind, underlying): [contract dict, ...]}
        # kind ∈ {"future", "option", "cash"}; exchange ∈ {NSE, NFO, BSE, BFO}.
        self._idx_underlyings: Dict = {}
        self._idx_contracts: Dict = {}
        self._sym_token: Dict[str, int] = {}      # TradingSymbol(upper) → integer token
        self._sym_expiry: Dict[str, str] = {}     # TradingSymbol(upper) → raw expiry (info only)

        # Live price stream (Arrow WebSocket DataStream) → latest-LTP cache,
        # keyed by integer token, fed by on_ticks at tick rate (~50ms or faster).
        self._streams = None
        self._stream_ltp: Dict[int, float] = {}
        self._stream_tokens: set = set()
        self._stream_lock = threading.Lock()

        logger.info("ArrowBroker initialised (app_id={})", self.app_id[:8] + "…" if self.app_id else "")

    # ── Connection ────────────────────────────────────────────────────────────

    @staticmethod
    def _format_error(exc: Exception) -> str:
        """Build a concise, user-facing message from an Arrow SDK exception.

        ArrowException carries .code (HTTP status), .message, .method and .url.
        We surface all of them so the UI shows the real reason *and* which auth
        step failed (login vs 2FA vs authenticate-token).
        """
        code   = getattr(exc, "code", None)
        msg    = getattr(exc, "message", None) or str(exc)
        method = getattr(exc, "method", None)
        url    = getattr(exc, "url", None)
        head = f"HTTP {code}: {msg}" if code else str(msg)
        if method and url:
            # Keep just the path so the message stays short but still names the step
            try:
                from urllib.parse import urlparse
                path = urlparse(url).path or url
            except Exception:
                path = url
            head += f"  [{method} {path}]"
        return head

    def connect(self, totp_secret: str = "") -> bool:
        """Authenticate with Arrow and download the instrument master.

        Parameters
        ----------
        totp_secret : str
            Base32 TOTP secret (overrides config if supplied).
        """
        self.last_error = ""

        if not _SDK_AVAILABLE:
            self.last_error = "pyarrow-client not installed — run: pip install pyarrow-client"
            logger.error("ArrowBroker: {}", self.last_error)
            return False

        if not self.app_id:
            self.last_error = "app_id is required"
            logger.error("ArrowBroker: {}", self.last_error)
            return False

        secret = totp_secret or self.totp_secret
        if not all([self.user_id, self.password, self.api_secret, secret]):
            self.last_error = "user_id, password, api_secret, and totp_secret are all required"
            logger.error("ArrowBroker: {}", self.last_error)
            return False

        try:
            client = ArrowClient(app_id=self.app_id)

            # Try with a pre-existing token first (avoids re-login within the session)
            pre_token = self.config.get("token", "")
            if pre_token:
                client.set_token(pre_token)
                try:
                    client.get_user_details()
                    self._client = client
                    self.connected = True
                    logger.info("ArrowBroker: reused existing session token")
                    threading.Thread(
                        target=self._fetch_instruments, daemon=True, name="ArrowInstruments"
                    ).start()
                    return True
                except Exception:
                    logger.debug("ArrowBroker: pre-existing token invalid — logging in fresh")

            # NOTE: the installed SDK's parameter is api_secret (docs say
            # app_secret — that is wrong for 1.4.0).
            client.auto_login(
                user_id=self.user_id,
                password=self.password,
                api_secret=self.api_secret,
                totp_secret=secret,
            )

            self._client = client
            self.connected = True
            logger.info("ArrowBroker: authenticated — token={}", client.token[:12] + "…" if client.token else "?")

            threading.Thread(
                target=self._fetch_instruments, daemon=True, name="ArrowInstruments"
            ).start()
            return True

        except Exception as exc:
            self.last_error = self._format_error(exc)
            logger.error("ArrowBroker: connect failed — {}", exc)
            return False

    def disconnect(self) -> None:
        self.connected = False
        self.stop_price_stream()
        if self._client:
            try:
                self._client.invalidate_session()
            except Exception:
                pass
            self._client = None
        logger.info("ArrowBroker: disconnected")

    def get_session_token(self) -> str:
        """Return the current session token (useful for token reuse without re-login)."""
        if self._client:
            return self._client.token
        return ""

    # ── Instrument master ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_instruments(data) -> List[Dict]:
        """Normalise Arrow's /all instruments response into a list of dicts.

        The SDK returns whatever the endpoint sends. In practice /all comes
        back as ``application/octet-stream`` so the SDK hands us raw ``bytes``;
        depending on Arrow's CDN that payload is gzip-compressed and/or JSON or
        CSV. Handle every plausible shape so lot sizes load regardless.
        """
        # Already structured
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "instruments", "result", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]

        # Bytes / str → decode then sniff JSON vs CSV
        if isinstance(data, (bytes, bytearray, str)):
            raw = data.encode() if isinstance(data, str) else bytes(data)
            # gzip magic number
            if raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                return []
            if text[0] in "[{":
                import json
                obj = json.loads(text)
                return ArrowBroker._parse_instruments(obj)
            # Otherwise assume delimited text (CSV/TSV)
            import csv, io
            sample = text[:4096]
            delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            return [dict(row) for row in reader]

        return []

    @staticmethod
    def _extract_symbol_lot(inst: Dict):
        """Pull (symbol, lot_size, tick_size) from one instrument record, tolerant
        of the many key/column spellings brokers use (symbol/tradingSymbol/…,
        lotSize/lot_size/lotQty/…, tickSize/ticksize/tick/minMove/…). Returns
        (symbol_upper, int|None, float|None)."""
        # Case-insensitive key lookup
        lower = {str(k).lower(): v for k, v in inst.items()}
        sym = ""
        # TradingSymbol is the actual tradeable symbol (HDFCBANK30JUN26C875);
        # Symbol is only the underlying (HDFCBANK) — index lot size by the
        # tradeable symbol so resolve_lot_size(order_symbol) hits (and the EQ
        # row never poisons a derivative's lot size).
        for k in ("tradingsymbol", "trading_symbol", "symbol", "tsym", "scrip"):
            if lower.get(k):
                sym = str(lower[k]).strip().upper()
                break
        ls = None
        for k in ("lotsize", "lot_size", "lotqty", "lot_qty", "lot", "boardlotquantity", "marketlot"):
            if lower.get(k) not in (None, "", "0"):
                try:
                    ls = max(1, int(float(lower[k])))
                except (ValueError, TypeError):
                    ls = None
                if ls:
                    break
        tick = None
        for k in ("ticksize", "tick_size", "tick", "minmove", "min_move", "minimumtick", "pricetick"):
            if lower.get(k) not in (None, "", "0"):
                try:
                    t = float(lower[k])
                    # Some masters quote tick in paise (e.g. 10 = ₹0.10) — keep as
                    # given; resolve_tick_size sanity-checks downstream.
                    if t > 0:
                        tick = t
                        break
                except (ValueError, TypeError):
                    pass
        return sym, ls, tick

    # ── Picker index (underlying / contract lookup) ───────────────────────────

    @staticmethod
    def _field(inst: Dict, *keys):
        """Case-insensitive field lookup tolerant of Arrow's key spellings."""
        low = {str(k).lower(): v for k, v in inst.items()}
        for k in keys:
            v = low.get(k.lower())
            if v not in (None, ""):
                return v
        return None

    @staticmethod
    def _derive_underlying(symbol: str, name=None) -> str:
        """Underlying for grouping. Arrow leaves `name` empty on most stock F&O,
        so derive it from the symbol: the leading alphabetic run before the
        first digit (expiry/strike). e.g. HDFCBANK26JUN25FUT → HDFCBANK,
        NIFTY02JAN25C26000 → NIFTY, RELIANCE-EQ → RELIANCE. Falls back to
        `name`, then the raw symbol."""
        s = (symbol or "").upper().strip()
        if s.endswith("-EQ"):
            return s[:-3]
        m = re.match(r"^([A-Z&]{2,})", s)
        if m:
            return m.group(1)
        if name:
            return str(name).upper().strip()
        return s

    _MONTHS = {m: i for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

    @classmethod
    def _parse_date_tuple(cls, s):
        """Best-effort (year, month, day) from an expiry/symbol string, so
        contracts sort chronologically regardless of Arrow's format
        (ISO 2025-06-26, DDMonYY 30JUN26, DD-Mon-YYYY, or epoch). None if no
        date is found."""
        s = str(s or "").strip().upper()
        if not s:
            return None
        # epoch seconds / milliseconds
        if s.isdigit() and len(s) >= 8:
            try:
                import datetime
                v = int(s)
                if v > 10 ** 11:
                    v //= 1000
                d = datetime.datetime.utcfromtimestamp(v)
                return (d.year, d.month, d.day)
            except Exception:
                pass
        m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)          # 2025-06-26
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})?-?\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-?\s*(\d{2,4})", s)  # 30JUN26
        if m:
            day = int(m.group(1)) if m.group(1) else 1
            yr = int(m.group(3))
            yr += 2000 if yr < 100 else 0
            return (yr, cls._MONTHS[m.group(2)], day)
        m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)          # 26-06-2025
        if m:
            return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return None

    @staticmethod
    def _classify_kind(exch: str, is_opt: bool) -> str:
        if exch in ("NFO", "BFO", "MCX", "MCXFO"):
            return "option" if is_opt else "future"
        if exch in ("NSE", "BSE"):
            return "option" if is_opt else "cash"
        return "other"

    def _build_instrument_index(self) -> None:
        """Index the master once into (exch_seg, kind) → underlyings and
        (exch_seg, kind, underlying) → contracts, so the picker is O(1).

        Arrow's /all master fields (TitleCase):
          ExchSeg       NSECM / NSEFO / BSECM / BSEFO  (exchange + segment)
          TradingSymbol the actual tradeable symbol (HDFCBANK30JUN26C875)
          Symbol        the underlying (HDFCBANK)
          OptionType    CE / PE  (empty ⇒ future)
          Expiry        30-Jun-2026 ; StrikePrice ; LotSize ; Token
        """
        underlyings: Dict = {}
        contracts: Dict = {}
        sym_token: Dict[str, int] = {}
        sym_expiry: Dict[str, str] = {}
        for inst in self._instruments:
            if not isinstance(inst, dict):
                continue
            g = {str(k).lower(): v for k, v in inst.items()}
            exch_seg = str(g.get("exchseg") or g.get("exch_seg") or "").upper()
            tsym     = str(g.get("tradingsymbol") or g.get("trading_symbol") or "").strip()
            if not exch_seg or not tsym:
                continue
            _tok = g.get("token") or g.get("instrument_token")
            if _tok:
                try:
                    sym_token[tsym.upper()] = int(_tok)
                except (ValueError, TypeError):
                    pass
            _exp = g.get("expiry") or g.get("expiry_date")
            if _exp:
                sym_expiry[tsym.upper()] = str(_exp)
            ot  = str(g.get("optiontype") or g.get("option_type") or "").upper()
            und = str(g.get("symbol") or g.get("underlying") or "").strip().upper() \
                  or self._derive_underlying(tsym)

            if exch_seg.endswith("CM"):
                kind = "cash"
            elif ot in ("CE", "PE"):
                kind = "option"
            else:
                kind = "future"

            strike = g.get("strikeprice") or g.get("strike")
            underlyings.setdefault((exch_seg, kind), set()).add(und)
            contracts.setdefault((exch_seg, kind, und), []).append({
                "trading_symbol": tsym,
                "token":          str(g.get("token") or ""),
                "expiry":         str(g.get("expiry") or ""),
                "strike":         str(strike or ""),
                "option_type":    ot,
                "lot_size":       str(g.get("lotsize") or g.get("lot_size") or ""),
            })

        def _sort_key(r):
            # Chronological by expiry (parsed from the expiry field, or the
            # symbol if the field is empty), then strike, then symbol.
            dt = self._parse_date_tuple(r["expiry"]) or self._parse_date_tuple(r["trading_symbol"]) or (9999, 99, 99)
            try:
                sk = float(r["strike"]) if r["strike"] else 0.0
            except (ValueError, TypeError):
                sk = 0.0
            return (dt, sk, r["trading_symbol"])

        for lst in contracts.values():
            lst.sort(key=_sort_key)

        self._idx_underlyings = {k: sorted(v) for k, v in underlyings.items()}
        self._idx_contracts = contracts
        self._sym_token = sym_token
        self._sym_expiry = sym_expiry
        logger.info(
            "ArrowBroker: index built — {} (exchange,kind) groups, {} underlyings total",
            len(self._idx_underlyings), sum(len(v) for v in self._idx_underlyings.values()),
        )

    def list_underlyings(self, exchange: str, kind: str) -> List[str]:
        """Underlyings available for an exchange (NSE/NFO/BSE/BFO) and kind
        (future/option/cash)."""
        return self._idx_underlyings.get((exchange.upper(), kind), [])

    def list_contracts(self, exchange: str, kind: str, underlying: str) -> List[Dict]:
        """Contracts for a given underlying, sorted by expiry then strike."""
        return self._idx_contracts.get((exchange.upper(), kind, underlying.upper()), [])

    def _fetch_instruments(self) -> None:
        """Background thread: download Arrow's instrument list and index lot sizes."""
        try:
            raw = self._client.get_instruments()
            instruments = self._parse_instruments(raw)

            if not instruments:
                # Couldn't parse — log a fingerprint so we can see what arrived
                rawb = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8", "replace")
                head = bytes(rawb[:80])
                is_gzip = head[:2] == b"\x1f\x8b"
                logger.warning(
                    "ArrowBroker: could not parse instruments — type={}, len={}, gzip={}, head={!r}",
                    type(raw).__name__, len(rawb), is_gzip, head,
                )
                return

            self._instruments = instruments
            for inst in instruments:
                if not isinstance(inst, dict):
                    continue
                sym, ls, tick = self._extract_symbol_lot(inst)
                if sym and ls:
                    self._lot_sizes.setdefault(sym, ls)
                if sym and tick:
                    self._tick_sizes.setdefault(sym, tick)

            self._build_instrument_index()

            logger.info(
                "ArrowBroker: instruments ready — {} instruments, {} lot-sizes",
                len(self._instruments), len(self._lot_sizes),
            )
        except Exception as exc:
            logger.warning("ArrowBroker: instrument fetch failed (lot sizes unavailable) — {}", exc)
        finally:
            self._instruments_ready.set()

    def resolve_lot_size(self, exchange_segment: str, symbol: str) -> int:
        """Return the lot size for a symbol.

        Checks (in order):
        1. Exact symbol match in lot-size index
        2. Iteratively strip trailing FUT / expiry tokens, checking the
           lot-size index after each strip (e.g. RELIANCE25JULFUT →
           RELIANCE25JUL → RELIANCE) so config overrides keyed by the base
           symbol still match expiry-coded trading symbols
        3. Falls back to 1 with a warning
        """
        sym = symbol.upper()

        ls = self._lot_sizes.get(sym)
        if ls:
            return ls

        # A single symbol like CRUDEOIL25JULFUT carries both an expiry (25JUL)
        # and FUT, so strip one trailing token at a time until it stops
        # shrinking, checking the index after each strip.
        _suffix = re.compile(r'(FUT|\d{2}[A-Z]{3}\d{2}|\d{2}[A-Z]{3}|\d{6})$')
        base = sym
        prev = None
        while base and base != prev:
            prev = base
            base = _suffix.sub('', base).rstrip(" -")
            ls = self._lot_sizes.get(base)
            if ls:
                return ls

        logger.warning(
            "ArrowBroker: lot size unknown for {}/{} — using 1 (order may fail)",
            exchange_segment, symbol,
        )
        return 1

    def resolve_tick_size(self, exchange_segment: str, symbol: str) -> float:
        """Return the price tick size for a symbol (e.g. NIFTY fut = 0.10).

        Same exact→base resolution as resolve_lot_size. Returns 0.0 when unknown
        so the caller can fall back to its configured default tick.
        """
        sym = symbol.upper()
        t = self._tick_sizes.get(sym)
        if not t:
            _suffix = re.compile(r'(FUT|\d{2}[A-Z]{3}\d{2}|\d{2}[A-Z]{3}|\d{6})$')
            base, prev = sym, None
            while base and base != prev:
                prev = base
                base = _suffix.sub('', base).rstrip(" -")
                t = self._tick_sizes.get(base)
                if t:
                    break
        if not t or t <= 0:
            return 0.0
        # NSE/BSE F&O ticks are sub-rupee (0.01–0.95); a value ≥ 1 there is an
        # ambiguous unit (paise? lots?) → discard so the caller uses its default.
        # MCX commodity ticks are legitimately whole-rupee (CRUDEOIL/GOLD = ₹1,
        # COTTON = ₹10, COPPER = ₹0.05), so trust a wider band for MCX.
        seg = exchange_segment.lower()
        if seg in ("mcx_fo", "mcx"):
            return float(t) if 0 < t <= 50 else 0.0
        return float(t) if 0 < t < 1 else 0.0

    def resolve_expiry_ymd(self, symbol: str):
        """(year, month, day) of a contract's expiry, or None if unknown.

        INFO ONLY — powers the dashboard 'days to expiry' readout. It never
        gates orders or entries; the execution path does not call this.
        Falls back to parsing the expiry encoded in the trading symbol
        (e.g. CRUDEOIL25JULFUT) when the master's Expiry field is absent.
        """
        sym = symbol.upper()
        raw = self._sym_expiry.get(sym)
        dt = self._parse_date_tuple(raw) if raw else None
        if not dt:
            dt = self._parse_date_tuple(sym)
        return dt

    # ── Quotes ────────────────────────────────────────────────────────────────

    def get_ltp(self, instruments: List[Dict]) -> Dict[str, float]:
        """
        Fetch last traded prices.

        Parameters
        ----------
        instruments : list of dict
            Each must have ``exchange_segment`` and ``instrument_token`` (symbol string).

        Returns
        -------
        dict
            ``{symbol: ltp}`` for all instruments with a price.
        """
        if not self.connected or not self._client or not instruments:
            return {}

        pairs = []
        for inst in instruments:
            seg = inst.get("exchange_segment", "")
            sym = inst.get("instrument_token") or inst.get("symbol", "")
            exchange_str = _SEGMENT_MAP.get(seg.lower(), seg.upper())
            try:
                exchange_enum = Exchange(exchange_str)
            except ValueError:
                logger.warning("ArrowBroker: unknown exchange segment '{}' — skipping {}", seg, sym)
                continue
            pairs.append((sym, exchange_enum))

        if not pairs:
            return {}

        def _ci(d: Dict, *keys):
            """Case-insensitive get — Arrow responses are TitleCase
            (TradingSymbol/Symbol/Ltp), so match regardless of casing."""
            low = {str(k).lower(): v for k, v in d.items()}
            for k in keys:
                v = low.get(k.lower())
                if v not in (None, ""):
                    return v
            return None

        try:
            response = self._client.get_quotes(QuoteMode.LTP, pairs)
            result: Dict[str, float] = {}
            if isinstance(response, list):
                for item in response:
                    if not isinstance(item, dict):
                        continue
                    sym = _ci(item, "tradingSymbol", "symbol", "tsym")
                    ltp = _ci(item, "ltp", "lastPrice", "price", "last_traded_price")
                    if sym and ltp is not None:
                        try:
                            result[str(sym).upper()] = float(ltp)
                        except (ValueError, TypeError):
                            pass
            elif isinstance(response, dict):
                for sym_key, val in response.items():
                    if isinstance(val, (int, float, str)):
                        ltp = val
                    elif isinstance(val, dict):
                        ltp = _ci(val, "ltp", "lastPrice", "price")
                    else:
                        ltp = None
                    if ltp is not None:
                        try:
                            result[str(sym_key).upper()] = float(ltp)
                        except (ValueError, TypeError):
                            pass
            return result
        except Exception as exc:
            logger.error("ArrowBroker: LTP fetch failed — {}", exc)
            return {}

    def get_quote(self, exchange_segment: str, symbol: str) -> Dict:
        """Best-effort ``{ltp, bid, ask}`` for one instrument. Tries a richer
        quote mode (FULL/QUOTE/DEPTH) for top-of-book bid/ask; if the SDK/build
        doesn't expose it, falls back to LTP only (bid/ask = None). Tolerant of
        field-name and shape variation — used by the live-sim fill model."""
        out: Dict = {"ltp": None, "bid": None, "ask": None}
        ltp_map = self.get_ltp([{"exchange_segment": exchange_segment,
                                 "instrument_token": symbol}])
        out["ltp"] = ltp_map.get(symbol.upper())
        if not self.connected or not self._client:
            return out
        mode = None
        for name in ("FULL", "QUOTE", "DEPTH", "MARKET_DEPTH", "OHLC"):
            m = getattr(QuoteMode, name, None)
            if m is not None:
                mode = m
                break
        if mode is None:
            return out
        try:
            exch = Exchange(_SEGMENT_MAP.get(exchange_segment.lower(), exchange_segment.upper()))
            resp = self._client.get_quotes(mode, [(symbol, exch)])
            rec = resp[0] if isinstance(resp, list) and resp else (resp if isinstance(resp, dict) else None)
            if isinstance(rec, dict):
                bid = _dig(rec, "bid", "bestBid", "buyPrice", "bidPrice", "bp", "bestBidPrice")
                ask = _dig(rec, "ask", "bestAsk", "sellPrice", "askPrice", "sp", "offer", "bestAskPrice")
                # depth array fallback: [{price, qty, ...}, ...]
                if bid is None or ask is None:
                    buys = _dig(rec, "buy", "bids", "buyDepth", "depthBuy")
                    sells = _dig(rec, "sell", "asks", "sellDepth", "depthSell")
                    if isinstance(buys, list) and buys:
                        bid = bid or _dig(buys[0], "price", "rate", "p")
                    if isinstance(sells, list) and sells:
                        ask = ask or _dig(sells[0], "price", "rate", "p")
                try:
                    out["bid"] = float(bid) if bid not in (None, "") else None
                    out["ask"] = float(ask) if ask not in (None, "") else None
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            logger.debug("ArrowBroker: depth quote unavailable for {} — {}", symbol, exc)
        return out

    # ── Live price stream (WebSocket) ──────────────────────────────────────────

    def start_price_stream(self, symbols: List[str]) -> bool:
        """Subscribe the given trading symbols to Arrow's WebSocket DataStream
        (LTP mode) so prices update at tick rate (~50ms or faster) without any
        per-request REST calls. Idempotent — only subscribes new tokens.
        """
        if not _SDK_AVAILABLE or not self.connected:
            return False
        tokens = []
        for s in symbols:
            tok = self._sym_token.get(str(s).upper())
            if tok:
                tokens.append(int(tok))
        if not tokens:
            return False
        try:
            with self._stream_lock:
                if self._streams is None:
                    from pyarrow_client import ArrowStreams, DataMode
                    self._DataMode = DataMode
                    streams = ArrowStreams(appID=self.app_id, token=self.get_session_token(), debug=False)

                    def _on_tick(tick):
                        try:
                            # Arrow's DataStream sends prices as integers in paise
                            # (1 rupee = 100 paise) — convert to rupees.
                            self._stream_ltp[int(tick.token)] = float(tick.ltp) / 100.0
                        except Exception:
                            pass

                    streams.data_stream.on_ticks = _on_tick
                    streams.connect_data_stream()
                    self._streams = streams
                    logger.info("ArrowBroker: price stream connected")

                new = [t for t in tokens if t not in self._stream_tokens]
                if new:
                    self._streams.subscribe_market_data(self._DataMode.LTP, new)
                    self._stream_tokens.update(new)
                    logger.info("ArrowBroker: streaming {} token(s) (total {})", len(new), len(self._stream_tokens))
            return True
        except Exception as exc:
            logger.warning("ArrowBroker: price stream start failed — {}", exc)
            return False

    def get_streamed_ltp(self, symbols: List[str]) -> Dict[str, float]:
        """Return {symbol: ltp} from the live stream cache for symbols that
        have received at least one tick."""
        out: Dict[str, float] = {}
        for s in symbols:
            tok = self._sym_token.get(str(s).upper())
            if tok is not None and int(tok) in self._stream_ltp:
                out[str(s).upper()] = self._stream_ltp[int(tok)]
        return out

    def stop_price_stream(self) -> None:
        with self._stream_lock:
            if self._streams:
                try:
                    self._streams.disconnect_all()
                except Exception:
                    pass
            self._streams = None
            self._stream_tokens.clear()
            self._stream_ltp.clear()

    # ── Orders ────────────────────────────────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        price: Optional[float] = None,
        exchange_segment: str = "nse_fo",
        product: str = "NRML",
        validity: str = "DAY",
        trigger_price: Optional[float] = None,
        disclosed_qty: int = 0,
        amo: bool = False,
        token: str = "",
    ) -> Dict:
        """
        Place an order via Arrow.

        ``quantity`` is in UNITS (lots × lot_size) — the caller multiplies.
        For a market order Arrow requires ``mpp=True`` and ``price=0`` (plain
        MKT is disabled), so that path is enforced here.
        """
        if not self.connected or not self._client:
            return {"order_id": None, "status": "error", "message": "Not connected"}

        exchange_str = _SEGMENT_MAP.get(exchange_segment.lower(), exchange_segment.upper())
        try:
            exchange_enum = Exchange(exchange_str)
        except ValueError:
            return {
                "order_id": None, "status": "error",
                "message": f"Unknown exchange segment: {exchange_segment}",
            }

        # Order type
        ot_wire   = _ORDER_TYPE_MAP.get(order_type.lower(), "MKT")
        try:
            ot_enum = OrderType(ot_wire)
        except ValueError:
            ot_enum = OrderType.MARKET

        # Transaction type
        tt_enum = TransactionType.BUY if side.lower() == "buy" else TransactionType.SELL

        # Product
        prod_wire = _PRODUCT_MAP.get(product.upper(), "M")
        try:
            prod_enum = ProductType(prod_wire)
        except ValueError:
            prod_enum = ProductType.NRML

        # Validity
        try:
            ret_enum = Retention(validity.upper())
        except ValueError:
            ret_enum = Retention.DAY

        # Plain MKT orders are disabled by default on the Arrow API (regulatory).
        # For a market order we send OrderType.MKT with mpp=True, which prices the
        # order at the Upper Limit / DPR per instrument to mimic market execution.
        is_market = order_type.lower() in ("market", "mkt")
        mpp = is_market
        order_price = 0.0 if is_market else float(price or 0)

        logger.info(
            "ArrowBroker: placing order — {} {} {} {} @ {} ({}{})",
            side.upper(), quantity, symbol, exchange_segment,
            "MKT(mpp)" if is_market else order_price, product,
            ", mpp" if mpp else "",
        )

        try:
            order_id = self._client.place_order(
                exchange=exchange_enum,
                symbol=symbol,
                quantity=quantity,
                disclosed_quantity=disclosed_qty,
                product=prod_enum,
                order_type=ot_enum,
                variety=Variety.REGULAR,
                transaction_type=tt_enum,
                price=order_price,
                validity=ret_enum,
                mpp=mpp,
            )
            logger.info("ArrowBroker: order placed — id={}", order_id)
            return {
                "order_id":   str(order_id),
                "status":     "submitted",
                "symbol":     symbol,
                "side":       side,
                "quantity":   quantity,
                "order_type": order_type,
                "price":      price,
                "raw":        {"orderNo": order_id},
            }
        except Exception as exc:
            logger.error("ArrowBroker: order failed — {}", exc)
            return {"order_id": None, "status": "error", "message": str(exc)}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by order ID."""
        if not self.connected or not self._client:
            logger.error("ArrowBroker: not connected — cannot cancel order")
            return False
        try:
            self._client.cancel_order(order_id)
            logger.info("ArrowBroker: cancelled order {}", order_id)
            return True
        except Exception as exc:
            logger.error("ArrowBroker: cancel order {} failed — {}", order_id, exc)
            return False

    def get_order_status(self, order_id: str) -> Dict:
        """Normalized fill state for one order. Tries the SDK's per-order
        history endpoints first, then falls back to scanning the order book.
        The exact SDK method/field names vary by build, so this is deliberately
        tolerant — anything it can't read degrades to ``UNKNOWN``."""
        unknown = {"order_id": order_id, "status": "UNKNOWN", "filled_qty": 0,
                   "pending_qty": 0, "avg_price": 0.0, "raw": {}}
        if not self.connected or not self._client:
            return unknown

        client = self._client
        raw = None
        for meth in ("get_order_status", "get_order_history", "single_order_history",
                     "order_history"):
            fn = getattr(client, meth, None)
            if fn is None:
                continue
            try:
                raw = fn(order_id)
                break
            except Exception as exc:
                logger.debug("ArrowBroker: {}({}) failed — {}", meth, order_id, exc)
        if raw is None:
            fn = getattr(client, "get_order_book", None) or getattr(client, "get_orders", None)
            if fn is not None:
                try:
                    book = fn() or []
                    raw = [o for o in book if isinstance(o, dict) and str(
                        _dig(o, "orderNo", "order_id", "nestOrderNumber", "norenordno",
                             "id", "ID", default="")) == str(order_id)]
                except Exception as exc:
                    logger.debug("ArrowBroker: order-book scan failed — {}", exc)

        # An order's history is a list of states (newest last); take the latest.
        rec = raw[-1] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else None)
        if not rec:
            return unknown

        status_raw = str(_dig(rec, "status", "orderStatus", "ordStatus", "report_type",
                              default="")).upper().strip()
        status = _ORDER_STATUS_MAP.get(status_raw, "OPEN" if status_raw else "UNKNOWN")
        filled = int(float(_dig(rec, "filledQty", "filled_quantity", "fillshares",
                                "cumQty", "filledQuantity", default=0) or 0))
        pend = int(float(_dig(rec, "pendingQty", "unfilledSize", "pending_quantity",
                              "remainingQuantity", default=0) or 0))
        avg = float(_dig(rec, "avgPrice", "averagePrice", "avgprc", "avg_price",
                         "tradedPrice", default=0) or 0)
        return {"order_id": order_id, "status": status, "filled_qty": filled,
                "pending_qty": pend, "avg_price": avg, "raw": rec}

    def amend_order(
        self,
        order_id: str,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        order_type: Optional[str] = None,
    ) -> bool:
        """Modify a pending order's price / qty / type. Tries the SDK's
        modify/amend method under its various names; returns False if none
        exists or the call is rejected."""
        if not self.connected or not self._client:
            logger.error("ArrowBroker: not connected — cannot amend order")
            return False

        payload: Dict = {}
        if price is not None:
            payload["price"] = float(price)
        if quantity is not None:
            payload["quantity"] = int(quantity)
        if order_type is not None:
            ot_wire = _ORDER_TYPE_MAP.get(order_type.lower(), "LMT")
            try:
                payload["order_type"] = OrderType(ot_wire)
            except ValueError:
                pass

        client = self._client
        for meth in ("modify_order", "amend_order", "update_order"):
            fn = getattr(client, meth, None)
            if fn is None:
                continue
            try:
                fn(order_id, **payload)
            except TypeError:
                try:
                    fn(order_id=order_id, **payload)
                except Exception as exc:
                    logger.error("ArrowBroker: {} {} failed — {}", meth, order_id, exc)
                    return False
            except Exception as exc:
                logger.error("ArrowBroker: {} {} failed — {}", meth, order_id, exc)
                return False
            logger.info("ArrowBroker: amended order {} → {}", order_id, payload)
            return True
        logger.warning("ArrowBroker: SDK exposes no modify/amend method")
        return False

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_positions(self) -> List[Dict]:
        """Return open positions from Arrow (field-name tolerant — the master
        and quotes are TitleCase, so positions likely are too)."""
        if not self.connected or not self._client:
            return []

        def _ci(p, *keys, cast=None, default=None):
            low = {str(k).lower(): v for k, v in p.items()}
            for k in keys:
                v = low.get(k.lower())
                if v not in (None, ""):
                    if cast is None:
                        return v
                    try:
                        return cast(float(v)) if cast is int else cast(v)
                    except (ValueError, TypeError):
                        continue
            return default

        try:
            raw = self._client.get_positions()
            positions = []
            for p in (raw or []):
                if not isinstance(p, dict):
                    continue
                sym  = _ci(p, "tradingSymbol", "tradingsymbol", "symbol", default="")
                exch = _ci(p, "exchSeg", "exchange", default="")
                bq   = _ci(p, "buyQty", "buyQuantity", "cfBuyQty", cast=int, default=0)
                sq   = _ci(p, "sellQty", "sellQuantity", "cfSellQty", cast=int, default=0)
                net  = _ci(p, "netQty", "netQuantity", "quantity", "netqty", cast=int, default=(bq - sq))
                avg  = _ci(p, "avgPrice", "averagePrice", "netAvgPrice", "buyAvgPrice", cast=float, default=0.0)
                ltp  = _ci(p, "ltp", "lastPrice", "lastTradedPrice", cast=float, default=0.0)
                pnl  = _ci(p, "pnl", "unrealizedPnl", "mtm", "mtom", "netPnl", cast=float, default=0.0)
                positions.append({
                    "symbol":        str(sym),
                    "exchange":      str(exch),
                    "product":       str(_ci(p, "product", "productType", default="")),
                    "net_quantity":  net,
                    "buy_quantity":  bq,
                    "sell_quantity": sq,
                    "average_price": avg,
                    "ltp":           ltp,
                    "pnl":           pnl,
                    "raw":           p,
                })
            return positions
        except Exception as exc:
            logger.error("ArrowBroker: get_positions failed — {}", exc)
            return []

    def get_order_book(self) -> List[Dict]:
        """The day's order book from Arrow, normalized for the dashboard's
        Exchange Order Log. Field-name tolerant across SDK builds; returns []
        on any error so the dashboard degrades gracefully."""
        if not self.connected or not self._client:
            return []
        client = self._client
        raw = None
        for meth in ("get_order_book", "get_orders", "order_book", "get_orderbook",
                     "get_order_history", "order_history", "get_trade_book", "get_trades"):
            fn = getattr(client, meth, None)
            if fn is None:
                continue
            try:
                raw = fn()
                if raw is not None:
                    break
            except Exception as exc:
                logger.debug("ArrowBroker: {}() failed — {}", meth, exc)
        # Some SDK builds wrap the list in {data|orders|orderBook: [...]}.
        if isinstance(raw, dict):
            raw = (_dig(raw, "data", "orders", "orderBook", "order_book",
                        "tradeBook", "result", default=None) or [])
        if not isinstance(raw, list):
            return []

        orders: List[Dict] = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            status_raw = str(_dig(o, "status", "orderStatus", "ordStatus",
                                  "report_type", default="")).upper().strip()
            orders.append({
                "time": str(_dig(o, "orderTime", "order_time", "exchTime", "exchangeTime",
                                 "updateTime", "orderEntryTime", "exchUpdateTime", "time",
                                 default="")),
                "symbol": str(_dig(o, "tradingSymbol", "tradingsymbol", "symbol",
                                   "instrument", default="")),
                "exchange": str(_dig(o, "exchSeg", "exchange", "exchangeSegment", default="")),
                "side": str(_dig(o, "transactionType", "transaction_type", "side",
                                 "buyOrSell", "trantype", default="")).upper(),
                "product": str(_dig(o, "product", "productType", "prod", default="")),
                "order_type": str(_dig(o, "orderType", "order_type", "ordType",
                                       "priceType", default="")).upper(),
                "qty": int(float(_dig(o, "quantity", "qty", "orderQty", "totalQty",
                                      "totalQuantity", default=0) or 0)),
                "fill_qty": int(float(_dig(o, "filledQty", "filledQuantity", "filled_quantity",
                                           "cumQty", "fillshares", "tradedQty", default=0) or 0)),
                "fill_price": float(_dig(o, "avgPrice", "averagePrice", "avgprc",
                                         "tradedPrice", "fillPrice", default=0) or 0),
                "price": float(_dig(o, "price", "orderPrice", "limitPrice", default=0) or 0),
                "fee": float(_dig(o, "fee", "brokerage", "charges", "totalCharges",
                                  "transactionCharges", default=0) or 0),
                "pnl": float(_dig(o, "pnl", "realizedPnl", "netPnl", "mtm", default=0) or 0),
                "status": _ORDER_STATUS_MAP.get(status_raw, status_raw or "UNKNOWN"),
                "order_id": str(_dig(o, "orderNo", "order_id", "nestOrderNumber",
                                     "norenordno", "orderId", "id", "ID", default="")),
                "raw": o,
            })
        return orders

    def get_account_info(self) -> Dict:
        """Return account balance and margin info from Arrow.

        The funds/limits endpoint is named differently across SDK builds, so we
        try the known method names in turn and use the first that returns data.
        The raw shape (list vs dict) and field names also vary — get_funds()
        normalizes them; here we just surface the first non-empty payload and
        log which method/keys we got so a mismatch is diagnosable."""
        if not self.connected or not self._client:
            return {}
        for meth in ("get_user_limits", "get_limits", "get_funds", "get_margins",
                     "get_margin", "get_rms_limits", "get_balance", "funds", "limits"):
            fn = getattr(self._client, meth, None)
            if fn is None:
                continue
            try:
                data = fn()
            except Exception as exc:
                logger.debug("ArrowBroker: {}() failed — {}", meth, exc)
                continue
            rec = data[0] if isinstance(data, list) and data else data
            if isinstance(rec, dict) and rec:
                sig = (meth, tuple(sorted(rec.keys())))
                if sig != self._funds_sig:        # log once (or when shape changes)
                    self._funds_sig = sig
                    logger.info("ArrowBroker: funds via {}() — keys: {}", meth, list(rec.keys()))
                return rec
        logger.warning("ArrowBroker: no funds/limits method returned data — "
                       "margin will read as unavailable")
        return {}

    def get_funds(self) -> Dict:
        """Normalized funds/margin from Arrow's user limits. Arrow returns
        ``{"allocations": [...], "margin": {...}}`` with the real figures nested
        under ``margin`` (e.g. usableMargin, utilized, totalCash). We read from
        that sub-dict, falling back to the top level for other SDK shapes. Field
        names vary, so this stays tolerant — anything it can't read is ``None``."""
        raw = self.get_account_info() or {}
        # Real figures live under "margin"; fall back to the whole payload.
        m = raw.get("margin") if isinstance(raw.get("margin"), dict) else raw

        def _f(*keys):
            v = _dig(m, *keys)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        return {
            # usable/free margin to deploy
            "available": _f("usableMargin", "cashAvailableForCNC",
                            "cashAvailableForOptionBuy", "availableMargin",
                            "availablecash", "cashAvailable", "marginAvailable",
                            "availableBalance", "available", "net"),
            # margin currently blocked by positions
            "used": _f("utilized", "cashUsed", "marginUsed", "usedMargin",
                       "utilizedMargin", "totalMargin", "spanMargin"),
            # account balance / total funds
            "equity": _f("totalCash", "allocated", "totalCashEq", "equity",
                         "netWorth", "collateral", "balance"),
            "cash": _f("totalCash", "cashCurrent", "cash", "cashBalance"),
            "raw": raw,
        }

    # ── Token (for symbol lookup — Arrow uses symbols directly) ───────────────

    def resolve_token(self, exchange_segment: str, symbol: str) -> str:
        """Arrow uses symbol strings directly — returns the symbol as-is."""
        return symbol
