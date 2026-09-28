"""Traded-volume tracker — day / week / month turnover in IST."""

from datetime import datetime, timedelta, timezone

from arrow_statarb.core import volume as vol


_IST = timezone(timedelta(hours=5, minutes=30))


def _ts(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_IST).timestamp()


def _rec(ts, *, lots=1, mult=100, pa=6000.0, pb=6050.0, action="OPEN",
         status="LIVE"):
    return {"ts": ts, "lots": lots, "lot_size": mult, "leg_a_price": pa,
            "leg_b_price": pb, "action": action, "status": status}


def test_turnover_of_both_legs():
    # 2 lots × 100 mult × (6000 + 6050) = 2,410,000
    assert vol.turnover_of(_rec(0, lots=2, mult=100, pa=6000, pb=6050)) == 2_410_000.0


def test_open_and_close_both_count():
    # A round trip books turnover twice (entry + exit) — that is how brokerage
    # and exchange turnover actually accrue.
    now = _ts(2026, 8, 13, 15, 0)               # a Thursday
    trades = [
        _rec(_ts(2026, 8, 13, 10, 0), action="OPEN", pa=6000, pb=6050),
        _rec(_ts(2026, 8, 13, 11, 0), action="CLOSE", pa=6010, pb=6055),
    ]
    s = vol.volume_summary(trades, now_ts=now)
    assert s["day"]["trades"] == 2
    assert s["day"]["lots"] == 2                 # 1 + 1 spread-lot
    assert s["day"]["turnover_inr"] == round(
        1 * 100 * (6000 + 6050) + 1 * 100 * (6010 + 6055), 2)


def test_rejected_and_nonfill_excluded():
    now = _ts(2026, 8, 13, 15, 0)
    trades = [
        _rec(_ts(2026, 8, 13, 10, 0), action="OPEN", status="rejected"),
        _rec(_ts(2026, 8, 13, 10, 5), action="OPEN", lots=0),        # no lots
        {"ts": _ts(2026, 8, 13, 10, 6), "action": "HEARTBEAT"},      # not a fill
        _rec(_ts(2026, 8, 13, 10, 7), action="CLOSE"),               # the only fill
    ]
    s = vol.volume_summary(trades, now_ts=now)
    assert s["day"]["trades"] == 1


def test_day_week_month_boundaries_ist():
    # "Now" = Thu 2026-08-13 15:00 IST. Week starts Mon 2026-08-10; month 08-01.
    now = _ts(2026, 8, 13, 15, 0)
    trades = [
        _rec(_ts(2026, 8, 13, 9, 30), action="OPEN"),    # today
        _rec(_ts(2026, 8, 11, 9, 30), action="OPEN"),    # this week (Tue), not today
        _rec(_ts(2026, 8, 5, 9, 30), action="OPEN"),     # this month, not this week
        _rec(_ts(2026, 7, 28, 9, 30), action="OPEN"),    # last month
    ]
    s = vol.volume_summary(trades, now_ts=now)
    assert s["day"]["trades"] == 1
    assert s["week"]["trades"] == 2                       # today + Tue
    assert s["month"]["trades"] == 3                      # + Aug 5
    assert s["all_time"]["trades"] == 4
    assert s["week_start"] == "2026-08-10"
    assert s["month_start"] == "2026-08-01"


def test_ist_midnight_boundary_is_exclusive_of_yesterday():
    # 23:00 IST the previous day must NOT count toward today.
    now = _ts(2026, 8, 13, 0, 30)                         # 00:30 IST today
    trades = [
        _rec(_ts(2026, 8, 12, 23, 0), action="OPEN"),     # yesterday 23:00 IST
        _rec(_ts(2026, 8, 13, 0, 15), action="OPEN"),     # today 00:15 IST
    ]
    s = vol.volume_summary(trades, now_ts=now)
    assert s["day"]["trades"] == 1                        # only the 00:15 fill


def test_recent_days_strip_is_contiguous_and_zero_filled():
    now = _ts(2026, 8, 13, 15, 0)
    trades = [_rec(_ts(2026, 8, 13, 10, 0), action="OPEN"),
              _rec(_ts(2026, 8, 10, 10, 0), action="OPEN")]
    s = vol.volume_summary(trades, now_ts=now, recent_days=7)
    dates = [d["date"] for d in s["recent_days"]]
    assert dates == ["2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10",
                     "2026-08-11", "2026-08-12", "2026-08-13"]
    by_date = {d["date"]: d for d in s["recent_days"]}
    assert by_date["2026-08-13"]["trades"] == 1
    assert by_date["2026-08-10"]["trades"] == 1
    assert by_date["2026-08-12"]["trades"] == 0           # zero-filled gap day


def test_empty_is_all_zero():
    s = vol.volume_summary([], now_ts=_ts(2026, 8, 13))
    for k in ("day", "week", "month", "all_time"):
        assert s[k] == {"turnover_inr": 0.0, "lots": 0.0, "trades": 0}
