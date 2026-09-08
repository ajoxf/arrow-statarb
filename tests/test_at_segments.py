"""Segments — MCX is expressible, and what is missing is SAID."""

from arrowtrader.segments import SegmentTable, available_segments


def test_mcx_is_in_the_table_at_all():
    """The stat-arb broker refuses MCX outright and has no entry for
    it. Here it is data, so 'MCX first, NSE next' is one switch."""
    table = SegmentTable()
    segment = table.get('mcx_fo')
    assert segment.exch_seg == 'MCXFO'
    assert segment.exchange == 'MCX'


def test_the_master_field_and_the_order_field_are_not_the_same():
    """`ExchSeg` is MCXFO / NSEFO; the order carries MCX / NFO.
    Conflating them sends an order to the wrong exchange."""
    table = SegmentTable()
    assert table.exch_seg_for('nse_fo') == 'NSEFO'
    assert table.exchange_for('nse_fo') == 'NFO'


def test_lookup_is_case_and_whitespace_tolerant():
    table = SegmentTable()
    assert table.get(' MCX_FO ').key == 'mcx_fo'
    assert table.key_for_exch_seg('mcxfo') == 'mcx_fo'


def test_an_unknown_segment_is_none_not_a_guess():
    assert SegmentTable().get('ncdex') is None
    assert SegmentTable().exchange_for('ncdex') is None


def test_config_can_add_a_segment_without_a_code_change():
    table = SegmentTable({'ncdex_fo': {'exch_seg': 'NCDEXFO',
                                       'exchange': 'NCDEX',
                                       'label': 'NCDEX futures'}})
    assert table.exchange_for('ncdex_fo') == 'NCDEX'


def test_a_segment_the_master_has_no_rows_for_is_named_as_such():
    table = SegmentTable()
    report = available_segments(table, ['NSEFO', 'NSECM'], ['NSE', 'NFO'])
    assert report['nse_fo']['ready'] is True
    assert report['mcx_fo']['ready'] is False
    assert 'not be entitled' in report['mcx_fo']['note']


def test_a_segment_the_SDK_cannot_address_is_a_different_fault():
    """Master rows but no enum value is 'upgrade the SDK', not 'ask
    Arrow to enable the segment'. Same symptom, different fix."""
    table = SegmentTable()
    report = available_segments(table, ['MCXFO'], ['NSE', 'NFO'])
    assert report['mcx_fo']['ready'] is False
    assert report['mcx_fo']['in_master'] is True
    assert 'Upgrade the SDK' in report['mcx_fo']['note']


def test_an_unreadable_sdk_enum_is_UNKNOWN_not_a_failure():
    """Unmeasured is not zero: if the enum could not be read we do not
    claim the segment is broken."""
    report = available_segments(SegmentTable(), ['MCXFO'], None)
    assert report['mcx_fo']['in_sdk'] is None
    assert report['mcx_fo']['ready'] is True
