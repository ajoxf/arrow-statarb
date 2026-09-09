"""Refusals carry the broker's own words. We only ADD the fix."""

from arrowtrader import arrow_errors as E


def test_the_brokers_words_are_never_replaced():
    said = "rms:blocked for gold05dec25f"
    assert said in E.refusal(said)


def test_an_unrecognised_refusal_gets_no_invented_fix():
    """A confident wrong fix sends the operator to the wrong place."""
    said = 'something nobody has seen before'
    assert E.refusal(said) == said
    assert E.classify(said) == (E.UNKNOWN, None)


def test_the_action_is_named_so_the_toast_says_what_failed():
    out = E.refusal('Invalid session', action='BUY 1 GOLD05DEC25F')
    assert out.startswith('BUY 1 GOLD05DEC25F: Invalid session')


def test_a_silent_refusal_still_says_something_actionable():
    assert 'no reason' in E.refusal('', action='The close')


def test_the_static_ip_refusal_names_the_actual_fix():
    """It is the failure that WILL happen, and the fix is not obvious."""
    out = E.refusal('Your IP is not registered with the exchange')
    assert 'static IP' in out or 'registered static IP' in out


def test_mpp_is_reported_as_OUR_bug_not_a_setting():
    out = E.refusal('Market order not allowed, use mpp')
    assert 'price=0 and mpp=True' in out


def test_a_timeout_is_UNCERTAIN_and_not_a_rejection():
    """Only a REJECTED leg unwinds. Slow is not rejected — and an HTTP
    timeout can hide an order that reached the exchange and filled."""
    assert E.is_uncertain('Read timed out') is True
    assert 'may or may not have arrived' in E.refusal('Read timed out')
    # CONTROL: a real refusal is certain, and does not read as one.
    assert E.is_uncertain('Order quantity exceeds freeze quantity') is False


def test_only_an_expired_session_reads_as_worth_retrying():
    assert E.is_retryable('Invalid session token') is True
    assert E.is_retryable('Insufficient margin') is False
    assert E.is_retryable('Read timed out') is False


def test_each_class_is_recognised():
    for text, kind in (
            ('Invalid session token', E.AUTH),
            ('Segment not enabled for this account', E.PERMISSION),
            ('Insufficient margin for this order', E.MARGIN),
            ("Price is out of the current Day's price range", E.PRICE),
            ('Order quantity exceeds freeze quantity', E.SIZE),
            ('Market is closed for this contract', E.CLOSED),
            ('rms:blocked for symbol', E.BROKER),
            ('Connection timed out', E.TRANSPORT)):
        assert E.classify(text)[0] == kind, text
