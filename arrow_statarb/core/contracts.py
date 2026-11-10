"""MCX contract price multipliers — ₹ of P&L per ₹1 move in the quoted price.

Arrow reports an MCX order lot of 1, which is the ORDER unit, not the P&L
multiplier: a 1-point move in CRUDEOILM (quoted ₹/bbl, 10 bbl per lot) is worth
₹10 per lot. Without this table the journal, live P&L, break-even and EV all
valued an MCX point at ₹1 — 10× to 1250× too small.

``pairs.multiplier_a/_b`` in settings still override this table; the table only
fills in when they are 0 (auto). Order QUANTITY is never derived from it.
"""

from __future__ import annotations

import re
from typing import Optional

#: Base symbol → units per lot in the quote's unit (contract specs, MCX).
MCX_MULTIPLIERS = {
    "CRUDEOIL": 100,      # 100 bbl, ₹/bbl
    "CRUDEOILM": 10,      # 10 bbl, ₹/bbl
    "NATURALGAS": 1250,   # 1250 mmBtu, ₹/mmBtu
    "NATGASMINI": 250,    # 250 mmBtu, ₹/mmBtu
    "GOLD": 100,          # 1 kg, ₹/10 g
    "GOLDM": 10,          # 100 g, ₹/10 g
    "SILVER": 30,         # 30 kg, ₹/kg
    "SILVERM": 5,         # 5 kg, ₹/kg
    "SILVERMIC": 1,       # 1 kg, ₹/kg
    "COPPER": 2500,       # 2.5 t, ₹/kg
    "ZINC": 5000,         # 5 t, ₹/kg
    "LEAD": 5000,         # 5 t, ₹/kg
    "ALUMINIUM": 5000,    # 5 t, ₹/kg
}

# BASE + DDMONYY + (F | C/P strike …), e.g. CRUDEOILM19NOV26F, CRUDEOILM17SEP26C8950
_SYMBOL_RE = re.compile(r"^([A-Z]+?)(\d{2}[A-Z]{3}\d{2})")


def mcx_base(symbol: str) -> Optional[str]:
    """The underlying of an MCX contract symbol ('CRUDEOILM19NOV26F' →
    'CRUDEOILM'); a bare base symbol is returned as is. None if unparseable."""
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    m = _SYMBOL_RE.match(s)
    if m:
        return m.group(1)
    return s if s.isalpha() else None


def mcx_multiplier(symbol: str) -> Optional[float]:
    """₹ per 1-point move per lot for a known MCX contract, else None."""
    base = mcx_base(symbol)
    v = MCX_MULTIPLIERS.get(base) if base else None
    return float(v) if v else None
