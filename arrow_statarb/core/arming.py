"""Entry arming — WHEN a stretch becomes an entry. One rule, two users: the
live algo and the Analysis page's signal study each hold their OWN instance
(nothing is shared, so the reading can never steer the trade, and the two can
never disagree about the rule).

Two modes, chosen by ``entry_mode``:

``touch``    enter AT the stretch: |z| ≥ entry_z for ``confirmation_ticks``
             consecutive samples (arrow's original behaviour).

``reentry``  do not sell the touch of the band — wait for the spread to come
             BACK inside it. A side is ARMED once z has stayed beyond the
             entry z for ``confirmation_ticks`` samples; it TRIGGERS when z
             falls back to ``entry_z − reentry_back`` (2.0 armed → 1.5
             enters), but only inside the re-entry WINDOW: down to
             ``back_at × (1 − reentry_window_pct/100)`` (1.5 → 0.75 at 50%).
             A spread that is through the window floor without an entry is
             LOST ("never came back cleanly") — an armed side never enters
             near the mean with nothing left to capture. A trend rides the
             band and never comes back, so it gives no entry at all.

z here is ARROW's z (time window of samples, sample σ, simple mean), so the
levels mean what they mean on arrow's own dashboard.

Arming is NOT carried across a restart, on purpose: the z window starts fresh
after one, and an arming measured against a window that no longer exists must
not fire an entry on the new one. A restart can cost an entry; it can never
make one.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

TOUCH = "touch"
REENTRY = "reentry"
MODES = (TOUCH, REENTRY)


def mode_of(p: Dict, default: str = TOUCH) -> str:
    m = str(p.get("entry_mode") or default).lower()
    return m if m in MODES else default


def reentry_levels(p: Dict) -> Tuple[float, float]:
    """(enters at, window floor) as |z|: 2.0 armed, 1.5 enters, 0.75 floor."""
    entry_z = float(p.get("entry_zscore", 2.0))
    back = min(max(float(p.get("reentry_back", 0.5)), 0.05), entry_z)
    pct = min(max(float(p.get("reentry_window_pct", 50.0)), 5.0), 100.0)
    back_at = entry_z - back
    return back_at, back_at * (1.0 - pct / 100.0)


class Arming:
    """Feed one z per READY sample with ``step``; it says what happened."""

    def __init__(self, default_mode: str = TOUCH):
        self.default_mode = default_mode
        self.reset()

    def reset(self) -> None:
        self.consec_above = self.consec_below = 0
        self.armed: Optional[str] = None          # "SHORT" | "LONG"
        self.armed_ts: Optional[float] = None
        self.armed_z: Optional[float] = None      # z when it armed
        self.peak_z: Optional[float] = None       # furthest z while armed

    def disarm(self) -> None:
        self.armed = self.armed_ts = self.armed_z = self.peak_z = None

    def step(self, z: float, ts: float, p: Dict) -> Dict:
        """Returns {"trigger": side|None, "event": None|"armed"|"lost",
        "lost": {...}|None, "confirming": n}. ``trigger`` is the side to
        enter NOW; the caller disarms once it has actually entered (a gate
        that blocks leaves it armed for the next sample in the window)."""
        entry_z = float(p.get("entry_zscore", 2.0))
        confirm = max(1, int(p.get("confirmation_ticks", 1)))
        if z >= entry_z:
            self.consec_above += 1; self.consec_below = 0
        elif z <= -entry_z:
            self.consec_below += 1; self.consec_above = 0
        else:
            self.consec_above = self.consec_below = 0
        out: Dict = {"trigger": None, "event": None, "lost": None,
                     "confirming": max(self.consec_above, self.consec_below)}

        stretched = ("SHORT" if self.consec_above >= confirm
                     else "LONG" if self.consec_below >= confirm else None)
        if mode_of(p, self.default_mode) == TOUCH:
            out["trigger"] = stretched
            return out

        if stretched and stretched != self.armed:
            self.armed, self.armed_ts, self.armed_z, self.peak_z = stretched, ts, z, z
            out["event"] = "armed"
        if self.armed is None:
            return out
        sign = 1.0 if self.armed == "SHORT" else -1.0
        self.peak_z = z if sign * z > sign * self.peak_z else self.peak_z
        back_at, floor = reentry_levels(p)
        if sign * z <= back_at:                   # back inside the band
            if sign * z >= floor:
                out["trigger"] = self.armed       # in the window: enter now
            else:                                 # through the floor: lost
                out["event"] = "lost"
                out["lost"] = {"side": self.armed, "armed_ts": self.armed_ts,
                               "armed_z": self.armed_z, "peak_z": self.peak_z, "z": z}
                self.disarm()
        return out
