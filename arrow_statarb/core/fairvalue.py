"""Theoretical fair value of the spread — REFERENCE / DISPLAY ONLY.

Ported from the W3 basis system. Nothing in the signal, sizing or exit
path may read this module: the engine trades ``spread = leg_b - beta*leg_a``
z-scored against its own rolling mean, and fair value sits beside that
number answering a different question — is the mean the z-score is anchored
on anywhere near what carry says it should be? A large gap usually means the
CONFIGURATION is wrong (wrong contract month, a per-contract quote where a
per-unit one was assumed), not that the trade is good.

Indian markets have no MT5 "swap"; carry is pure **cost-of-carry**
(``price * exp(risk_free_rate * years)``). Returns None whenever it cannot
answer honestly — a missing fair value is fine; a guessed one is not.

Pair types:
  SPOT_FUTURE    Leg A cash/ETF, Leg B a dated future on the same underlying
                 (NSE cash-vs-future). Fair future = spot compounded to expiry.
  FUTURE_FUTURE  Both legs dated futures on the same underlying (a calendar
                 spread — NSE or MCX). Fair far leg = near compounded over the
                 gap between expiries.
  RELATED        Two different instruments (e.g. GOLD vs SILVER). No arbitrage
                 ties them, so there is no fair value — the empirical rolling
                 mean is the only anchor.
"""

from __future__ import annotations

import math
from datetime import datetime

SPOT_FUTURE = "SPOT_FUTURE"
FUTURE_FUTURE = "FUTURE_FUTURE"
RELATED = "RELATED"

PAIR_TYPES = (SPOT_FUTURE, FUTURE_FUTURE, RELATED)
# Only same-underlying pairs have carry tying the legs together.
BASIS_TYPES = (SPOT_FUTURE, FUTURE_FUTURE)

YEAR_SECONDS = 365.25 * 24 * 3600


def _as_datetime(expiry):
    """Accept a datetime or an ISO/date string; return a datetime or None."""
    if expiry is None or expiry == "":
        return None
    if isinstance(expiry, datetime):
        return expiry
    try:
        return datetime.fromisoformat(str(expiry)[:19])
    except ValueError:
        try:
            return datetime.strptime(str(expiry)[:10], "%Y-%m-%d")
        except ValueError:
            return None


def years_until(expiry, now=None):
    """Year fraction to expiry, or None if missing/unparseable/passed."""
    dt = _as_datetime(expiry)
    if dt is None:
        return None
    now = now or datetime.now()
    years = (dt - now).total_seconds() / YEAR_SECONDS
    return years if years > 0 else None


def fair_spread(asset_cfg, spot_price, futures_price, hedge_ratio=1.0, now=None):
    """Theoretical value of ``futures - hedge_ratio * spot``.

    Returns ``(fair_value, detail)``; fair_value is None whenever the inputs
    cannot support an honest answer, with ``detail`` explaining why.
    """
    pair_type = (asset_cfg.get("pair_type") or SPOT_FUTURE).upper()
    if pair_type not in BASIS_TYPES:
        return None, ("two different instruments — no arbitrage forces them "
                      "together, so the rolling mean is the only anchor")

    beta = float(hedge_ratio or 1.0)
    rate = asset_cfg.get("risk_free_rate")
    if rate is None:
        return None, "no carry rate configured — set it in Settings → Pair Selection"
    rate = float(rate)

    far = years_until(asset_cfg.get("futures_expiry"), now)
    if far is None:
        return None, ("Leg B has no expiry in the future — a rolling contract "
                      "has no carry to compute")

    if pair_type == FUTURE_FUTURE:
        near = years_until(asset_cfg.get("spot_expiry"), now)
        if near is None:
            return None, ("Leg A has no expiry — a calendar spread needs both, "
                          "set it on the Setup page")
        gap = far - near
        if gap <= 0:
            return None, "Leg A expires after Leg B — check the symbols"
        fair_far = spot_price * math.exp(rate * gap)
        detail = (f"Leg A {spot_price:.2f} compounded at the {rate * 100:.2f}% "
                  f"carry over {gap * 365.25:.0f} days between expiries")
    else:
        fair_far = spot_price * math.exp(rate * far)
        detail = (f"spot {spot_price:.2f} compounded at the {rate * 100:.2f}% "
                  f"carry over {far * 365.25:.0f} days to expiry")

    return fair_far - beta * spot_price, detail


def fair_value_block(asset_cfg, spot_price, futures_price, spread,
                     hedge_ratio=1.0, now=None):
    """The reference block the dashboard shows under the spread."""
    value, detail = fair_spread(asset_cfg, spot_price, futures_price,
                                hedge_ratio, now)
    return {
        "pair_type": (asset_cfg.get("pair_type") or SPOT_FUTURE).upper(),
        "fair_value": value,
        "fair_gap": (spread - value) if value is not None else None,
        "fair_detail": detail,
    }
