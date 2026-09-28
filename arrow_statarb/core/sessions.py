"""Exchange trading sessions in IST, per segment.

Indian exchanges do not share one clock:

  * NSE / BSE (cash and F&O): 09:15 – 15:30
  * MCX commodities:          09:00 – 23:30 while the US is on daylight time
                              (2nd Sunday of March → 1st Sunday of November),
                              09:00 – 23:55 otherwise. MCX shifts its evening
                              close to follow the COMEX/NYMEX session.

A pair is only tradeable while BOTH legs' exchanges are open, so a pair's
session is the intersection of its legs' sessions. Weekends are closed;
exchange holidays are not modelled (the feed simply goes quiet, which the
signal engine's stale-quote guard handles).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)        # first Sunday
    return d + timedelta(weeks=n - 1)


def us_dst_active(day: date) -> bool:
    """US daylight time: 2nd Sunday of March to 1st Sunday of November."""
    return _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)


def is_mcx(segment: str) -> bool:
    return "MCX" in str(segment or "").upper()


def segment_session(segment: str, day: date) -> Tuple[int, int]:
    """(open, close) in minutes after IST midnight for one segment."""
    if is_mcx(segment):
        close = 23 * 60 + (30 if us_dst_active(day) else 55)
        return 9 * 60, close
    return 9 * 60 + 15, 15 * 60 + 30                   # NSE / BSE


def pair_session(segments: Iterable[str], day: date) -> Optional[Tuple[int, int]]:
    """Intersection of the legs' sessions, or None when no leg is known."""
    spans = [segment_session(s, day) for s in segments if s]
    if not spans:
        return None
    return max(o for o, _ in spans), min(c for _, c in spans)


def is_open(segments: Iterable[str], now: Optional[datetime] = None) -> bool:
    """True while every leg's exchange is in session (weekdays only)."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    span = pair_session(list(segments), now.date())
    if span is None:
        return True
    minute = now.hour * 60 + now.minute + now.second / 60.0
    return span[0] <= minute <= span[1]


def close_hm(segments: Iterable[str], day: date) -> Optional[Tuple[int, int]]:
    """The pair's close as (hour, minute), or None when no leg is known."""
    span = pair_session(list(segments), day)
    if span is None:
        return None
    return divmod(span[1], 60)
