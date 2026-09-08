"""Segments — MCX is expressible, and what is missing is SAID."""

from arrowtrader.segments import SegmentTable, available_segments


def test_mcx_is_in_the_table_at_all():
    """The stat-arb broker refuses MCX outright and has no entry for
    it. Here it is data, so 'MCX first, NSE next' is one switch."""
    table = SegmentTable()
    segment = table.get('mcx_fo')
    assert segment.exch_seg == 'MCXFO'
    # MCXFO, not MCX. The SDK carries both and says which is which:
    # `MCX` is for permission checks and the instrument download,
    # `MCXFO` is what an order, a quote and a margin request carry. A
    # quote sent to `MCX` is not refused — it comes back with no book,
    # which on a ladder is indistinguishable from a contract that is
    # not trading.
    assert segment.exchange == 'MCXFO'


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


def test_a_master_spelling_MCX_still_lands_in_the_MCX_segment():
    """The tolerance, and why it exists.

    Arrow documents four `ExchSeg` values — NSECM, NSEFO, BSECM,
    BSEFO — and documents nothing for the commodity segment. If the
    master spells it `MCX` where this build expects `MCXFO`, nothing
    fails loudly: every MCX row lands in `unknown_exch_segs`, the
    picker finds no contracts, and the Exchanges page reports that the
    account is not entitled to a segment it is perfectly entitled to.
    """
    from arrowtrader.segments import SegmentTable, available_segments
    table = SegmentTable()
    assert table.key_for_exch_seg('MCX') == 'mcx_fo'
    assert table.key_for_exch_seg('MCXFO') == 'mcx_fo'
    found = available_segments(table, {'MCX'}, {'MCXFO'})
    assert found['mcx_fo']['in_master'] is True
    assert found['mcx_fo']['ready'] is True


def test_an_EXACT_spelling_always_outranks_another_segments_alias():
    """The control. An alias is a tolerance; a tolerance that can beat
    an exact match is a bug waiting for the first master that carries
    both spellings."""
    from arrowtrader.segments import SegmentTable
    table = SegmentTable()
    # `NFO` is NSE F&O's alias and nobody else's exact value...
    assert table.key_for_exch_seg('NFO') == 'nse_fo'
    # ...and `NSEFO`, an exact value, still resolves to itself.
    assert table.key_for_exch_seg('NSEFO') == 'nse_fo'
    # An unknown segment stays unknown rather than being absorbed.
    assert table.key_for_exch_seg('CDS') is None


def test_a_segment_the_master_does_not_carry_names_EVERY_spelling_it_looked_for():
    """The operator has to be able to check the claim. 'No MCXFO
    contracts' is not checkable if the code also looked for MCX."""
    from arrowtrader.segments import SegmentTable, available_segments
    found = available_segments(SegmentTable(), {'NSEFO'}, {'MCXFO', 'NFO'})
    note = found['mcx_fo']['note']
    assert 'MCXFO' in note and 'MCX' in note
    assert found['mcx_fo']['ready'] is False
