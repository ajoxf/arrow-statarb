"""ZSignalGenerator entry gates (ported, decoupled) — per-asset."""

from arrow_statarb.core.signals import ZSignalGenerator
from arrow_statarb.core.exits import SELL_BASIS, BUY_BASIS


class _Stats:
    def __init__(self, z, sigma=1.0, warm=True, slope=0.0):
        self._z, self.sigma, self._warm, self._slope = z, sigma, warm, slope
    @property
    def z(self):
        return self._z
    @property
    def warm(self):
        return self._warm
    def trend_slope(self):
        return self._slope


CFG = {"ENTRY_Z": 2.0, "MAX_ENTRY_Z": 4.0, "STOP_Z": 4.0, "EXIT_Z": 0.5,
       "TREND_FILTER": False, "ENTRY_COOLDOWN_SEC": 0, "STOP_COOLDOWN_SEC": 0}


def _gen(clock_val=0.0):
    return ZSignalGenerator(dict(CFG), clock=lambda: clock_val)


def test_below_threshold_no_signal():
    g = _gen()
    assert g.entry_signal("A", _Stats(1.0), {}, {}, 1, 1) is None


def test_enters_sell_and_buy_by_z_sign():
    g = _gen()
    assert g.entry_signal("A", _Stats(2.5), {}, {}, 1, 1) == SELL_BASIS
    assert g.entry_signal("A", _Stats(-2.5), {}, {}, 1, 1) == BUY_BASIS


def test_entry_ceiling_blocks():
    g = _gen()
    assert g.entry_signal("A", _Stats(4.5), {}, {}, 1, 1) is None


def test_trend_filter_blocks_against_tape():
    cfg = dict(CFG, TREND_FILTER=True)
    g = ZSignalGenerator(cfg, clock=lambda: 0.0)
    # SELL_BASIS (z>0) blocked while spread rising (slope>0)
    assert g.entry_signal("A", _Stats(2.5, slope=1.0), {}, {}, 1, 1) is None
    # BUY_BASIS (z<0) allowed while rising
    assert g.entry_signal("A", _Stats(-2.5, slope=1.0), {}, {}, 1, 1) == BUY_BASIS


def test_edge_filter_blocks():
    g = _gen()
    edge = lambda z, s, l, c, md: (False, 10.0, 100.0)
    assert g.entry_signal("A", _Stats(2.5), {}, {}, 1, 1, edge_fn=edge) is None


def test_active_position_blocks_entry():
    g = _gen()
    assert g.entry_signal("A", _Stats(2.5), {}, {"pid": 1}, 1, 1) is None


def test_cooldown_and_zreset():
    t = [100.0]
    g = ZSignalGenerator(dict(CFG, ENTRY_COOLDOWN_SEC=60), clock=lambda: t[0])
    g.notify_close("A", "TAKE_PROFIT", SELL_BASIS)
    assert g.entry_signal("A", _Stats(2.5), {}, {}, 1, 1) is None   # in cooldown
    t[0] += 61
    assert g.entry_signal("A", _Stats(2.5), {}, {}, 1, 1) == SELL_BASIS

    # z-reset: a STOP blocks same-direction re-entry until z re-enters the band
    g2 = _gen()
    g2.notify_close("A", "DOLLAR_STOP", SELL_BASIS)
    assert g2.entry_signal("A", _Stats(2.5), {}, {}, 1, 1) is None   # blocked dir
    g2.update("A", 0.1)                                              # z home → reset
    assert g2.entry_signal("A", _Stats(2.5), {}, {}, 1, 1) == SELL_BASIS
