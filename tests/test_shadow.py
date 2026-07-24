"""What-if-held shadow tracker — did the spread revert after we exited?"""

from arrow_statarb.core.whatif_shadow import ShadowTracker


def test_arm_skips_clean_target_hit(tmp_path):
    st = ShadowTracker(tmp_path / "s.json")
    assert st.arm(direction="LONG_SPREAD", entry_spread=0.0, lots=1, lot_mult=1.0,
                  cost_inr=0.0, exit_reason="profit_target") is None
    assert st.summary()["active"] == 0


def test_tracks_reversion_to_be_and_target(tmp_path):
    clk = {"t": 1000.0}
    st = ShadowTracker(tmp_path / "s.json", window_sec=3600, clock=lambda: clk["t"])
    # exited a LONG (entry spread 0) on a STOP; the target was +100 net
    st.arm(direction="LONG_SPREAD", entry_spread=0.0, lots=1, lot_mult=1.0,
           cost_inr=0.0, exit_reason="stop", target_net=100.0)

    clk["t"] = 1000 + 600                       # 10 min later: spread +50 → net +50
    st.update(50.0)
    w = st.summary()["watches"][0]
    assert w["reverted_be"] is True and w["be_min"] == 10.0
    assert w["reverted_target"] is False

    clk["t"] = 1000 + 1200                      # 20 min: spread +120 → hits target
    st.update(120.0)
    w = st.summary()["watches"][0]
    assert w["reverted_target"] is True and w["target_min"] == 20.0
    assert w["peak_net"] == 120.0

    clk["t"] = 1000 + 3700                      # window elapsed → finalized
    st.update(120.0)
    s = st.summary()
    assert s["active"] == 0 and s["completed"] == 1
    assert s["reverted_be"] == 1 and s["reverted_target"] == 1
    assert s["revert_target_rate"] == 100.0


def test_short_direction_and_no_revert(tmp_path):
    clk = {"t": 0.0}
    st = ShadowTracker(tmp_path / "s.json", window_sec=100, clock=lambda: clk["t"])
    # SHORT profits when spread FALLS; here it keeps rising → never reverts
    st.arm(direction="SHORT_SPREAD", entry_spread=100.0, lots=1, lot_mult=1.0,
           cost_inr=0.0, exit_reason="dollar_stop", target_net=50.0)
    clk["t"] = 50
    st.update(140.0)                            # spread rose → net negative for SHORT
    clk["t"] = 200                              # window elapsed
    st.update(140.0)
    s = st.summary()
    assert s["completed"] == 1 and s["reverted_be"] == 0
    assert s["revert_be_rate"] == 0.0


def test_persists_and_resumes(tmp_path):
    p = tmp_path / "s.json"
    st = ShadowTracker(p, clock=lambda: 1000.0)
    st.arm(direction="SHORT_SPREAD", entry_spread=100.0, lots=1, lot_mult=1.0,
           cost_inr=0.0, exit_reason="time_stop")
    st2 = ShadowTracker(p, clock=lambda: 1000.0)       # fresh instance reloads the file
    assert st2.summary()["active"] == 1
