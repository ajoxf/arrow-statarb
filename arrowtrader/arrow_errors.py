"""What Arrow's refusals mean, and what to actually do about them.

MT5-Trader's rule, kept verbatim: **a refusal carries the broker's own
words**, never "check the log". `10027 AutoTrading disabled by client`
is a sentence an operator can act on; "order failed" is not.

Arrow is harder than MT5 in one respect and easier in another. Harder:
there are no numeric codes. NEST-derived back ends answer with free
text — `rms:blocked for gold05dec25f`, `Price is out of the current
Day's price range`, `Order quantity exceeds freeze quantity` — and the
wording varies by build. Easier: that text is usually already an
English sentence, so the job here is not to translate a number, it is
to recognise the CLASS of refusal and add **the step that fixes it**,
which the broker never supplies.

So: the broker's own words are always carried through verbatim. This
module only ADDS to them.

Nothing here decides anything. A classification is used to phrase a
message and to decide whether a retry could conceivably help — never
to retry automatically, and never to suppress the original text.
"""

import re


#: Classes of refusal. Named for what the OPERATOR must do about them,
#: because that is the only distinction that changes anybody's next
#: action.
AUTH = 'auth'                 # the session is not usable
PERMISSION = 'permission'     # this account may not trade this
MARGIN = 'margin'             # not enough money
PRICE = 'price'               # the price is not acceptable to the exchange
SIZE = 'size'                 # the quantity is not acceptable
CLOSED = 'closed'             # the market is not open
BROKER = 'broker'             # the broker's own gate (RMS, product, mpp)
TRANSPORT = 'transport'       # we could not reach them at all
UNKNOWN = 'unknown'


#: (pattern, class, the step that fixes it). Ordered: the FIRST match
#: wins, so the specific patterns come before the general ones.
#:
#: Every one of these is a guess about wording until it is seen on a
#: live rejection. An unmatched refusal is not swallowed — it is passed
#: through as UNKNOWN with the broker's text intact, which is strictly
#: better than a confident wrong fix.
_RULES = (
    (r'invalid\s+(session|token)|session\s+expired|unauthor|not\s+logged',
     AUTH,
     'The Arrow session has expired — they last about 24 hours. '
     'Reconnect on the Exchanges page; the engine also reconnects by '
     'itself on the next poll.'),
    (r'\bip\b.*(not\s+register|whitelist|allowed)|static\s+ip',
     AUTH,
     "This host's IP is not registered with Arrow. SEBI requires a "
     "registered static IP — register this machine's IP with Arrow and "
     "reconnect. Nothing else will work until it is done."),
    (r'invalid\s+(totp|2fa|otp)|two.?factor',
     AUTH,
     'The TOTP did not validate. ARROW_TOTP_SECRET must be the base32 '
     'SEED from your authenticator setup, not the 6-digit code, and '
     "this machine's clock must be right."),
    (r'not\s+(permitted|allowed|enabled).*(segment|exchange)|'
     r'(segment|exchange).*(not\s+(permitted|allowed|enabled)|disabled)',
     PERMISSION,
     'This account is not enabled for that segment. Ask Arrow to enable '
     'it — for MCX that is the commodity segment, which is a separate '
     'entitlement from NSE F&O.'),
    (r'\bmcx\b.*(not\s+support|unsupport|unavailable)',
     PERMISSION,
     'Arrow reports no MCX support for this account. Confirm the '
     'commodity segment is enabled; if it genuinely is not available, '
     'MCX cannot be traded through this broker.'),
    (r'insufficient|margin\s+(shortfall|not|required)|'
     r'not\s+enough\s+(funds|margin|balance)',
     MARGIN,
     'Not enough margin for this order. Check the Accounts tab — a '
     'calendar spread should attract a margin benefit, so a shortfall '
     'on the second leg often means the first leg was booked as an '
     'outright.'),
    (r'freeze\s+quant|exceeds?\s+.*(freeze|max.*(order|quantity))|'
     r'quantity\s+exceeds',
     SIZE,
     "Over the exchange's freeze quantity for this contract — the "
     'largest single order it accepts. Reduce the Qty; the ladder shows '
     'the size that fits.'),
    (r'(lot|multiple|board\s*lot).*(size|multiple)|'
     r'quantity.*multiple',
     SIZE,
     'The quantity is not a whole number of lots. Units must be '
     'lots x LotSize; the lot size comes from the instrument master and '
     'varies per expiry.'),
    (r'price.*(out\s+of|outside|range|band|circuit|dpr)|'
     r'(dpr|circuit|price\s+band).*(breach|exceed|hit)',
     PRICE,
     "Outside the contract's daily price range (DPR). The exchange "
     'refuses any order beyond the band, whatever the ladder shows — '
     'the band moves with the previous close.'),
    (r'tick\s+size|price.*multiple\s+of',
     PRICE,
     "The price is not a multiple of the contract's tick size. Set the "
     "ladder increment from the master's TickSize."),
    (r'\bmpp\b|market\s+order.*(not\s+allowed|disabled)|'
     r'plain\s+mkt',
     BROKER,
     'Arrow disables plain market orders. A market order must carry '
     'price=0 and mpp=True — this is a bug in the caller, not a '
     'setting to change.'),
    (r'\brms\b|risk\s+manage|blocked\s+for',
     BROKER,
     "The broker's own risk system refused this, not the exchange. Ask "
     'Arrow why this contract is blocked for this account — it is often '
     'a product or a segment limit rather than money.'),
    (r'market\s+(is\s+)?closed|outside\s+(market|trading)\s+hours|'
     r'session\s+(not\s+open|closed)',
     CLOSED,
     'The market is closed for this contract. MCX and NSE keep '
     'different hours, and MCX\'s evening close moves with US daylight '
     'saving — the ladder shows this contract\'s own session.'),
    (r'timeout|timed\s+out|connection|network|unreachable|'
     r'temporarily\s+unavailable|502|503|504',
     TRANSPORT,
     'Arrow could not be reached. THIS IS NOT A REFUSAL — the order may '
     'or may not have arrived. Do not re-send it until the order book '
     'says what happened to it.'),
)

_COMPILED = tuple((re.compile(pattern, re.I), kind, fix)
                  for pattern, kind, fix in _RULES)


def classify(text):
    """(class, the step that fixes it) for a refusal, or (UNKNOWN, None).

    Never raises, never returns an empty class, and never claims to
    recognise something it does not.
    """
    message = str(text or '')
    if not message.strip():
        return UNKNOWN, None
    for pattern, kind, fix in _COMPILED:
        if pattern.search(message):
            return kind, fix
    return UNKNOWN, None


def refusal(text, action=None):
    """The sentence the ladder prints when the broker says no.

    THE BROKER'S OWN WORDS COME FIRST AND ARE NEVER REPLACED. The fix
    is appended after an em dash where we recognise the class, and
    omitted where we do not — an invented fix is worse than none,
    because it sends the operator to the wrong place with confidence.
    """
    message = str(text or '').strip()
    if not message:
        message = (f'{action or "The order"} was refused and the broker gave '
                   f'no reason')
    kind, fix = classify(message)
    prefix = f'{action}: ' if action else ''
    return f'{prefix}{message}' + (f' — {fix}' if fix else '')


def is_uncertain(text):
    """True where the refusal does NOT establish that nothing happened.

    A timeout is not a rejection. MT5-Trader's equivalent rule is that
    only a REJECTED crossing leg unwinds — slow is not rejected — and
    the same holds here with more force, because an HTTP timeout can
    hide an order that reached the exchange and filled.

    Callers must read the order book before deciding, and must never
    re-send on the strength of one of these.
    """
    return classify(text)[0] == TRANSPORT


def is_retryable(text):
    """True where trying the SAME thing again could plausibly work.

    Deliberately narrow, and deliberately not acted on automatically:
    nothing in this system retries an order by itself. It exists so the
    screen can say "retry" beside a stale session and not beside a
    margin shortfall.
    """
    return classify(text)[0] == AUTH
