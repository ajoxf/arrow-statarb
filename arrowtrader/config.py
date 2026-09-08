"""Accounts, pairs and settings — and the clashes that no longer exist.

Ported from MT5-Trader's `config.py`, with the account end rewritten.

MT5-Trader has to police THREE clashes at save time, because it runs
one terminal per account: two accounts on one endpoint, on one login,
or on one terminal folder are each a different way to end up trading
one account while every screen reports two.

**None of them can happen here.** There is ONE Arrow session; both legs
hold it; there are no endpoints, no terminal paths and no second login
to collide with. What replaces them is a different question with the
same weight, and it is asked once rather than enforced per row:

    is this account used by ANYTHING ELSE?

On a netting venue there is no magic number on a position. Our net and
the trader's own manual dealing in the same contract are one number
that nothing at the exchange can separate. A DEDICATED account restores
the guarantee MT5-Trader gets from its magic number; a shared one does
not, and the screen has to say which it is rather than assume.

The pair end is very nearly the MT5 one. What is added is what an
Indian contract has and an MT5 symbol does not — a SEGMENT, which is
part of a symbol's identity, and a PRODUCT, which is part of a
position's. What is removed is the swap fields: there is no overnight
financing here, the carry is in the price, and the fair-value inputs
are an interest rate and a storage cost instead.
"""

import json
import logging
import os
import re

from . import atomicfile
from .models import OrderType, OvernightMode, TimeInForce


def _blank_to_none(value):
    """'' is 'unset'. 0 is a real number and survives."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_WARNED = set()


def _warn_once(template, *args):
    key = template % args
    if key not in _WARNED:
        _WARNED.add(key)
        logging.warning('%s', key)


def _choice(enum, value, default, key, field):
    """An enum member, or the default with a line saying why.

    A config typo must not stop the engine and must not be silent: an
    `order_type` of 'Limit ' becomes LIMIT and says so once.
    """
    try:
        return enum(str(value).strip().upper())
    except (ValueError, AttributeError):
        _warn_once('%s: %s is not a valid %s — using %s',
                   key, value, field, default)
        return enum(default)


PAIR_TYPES = ('SPOT_FUTURE', 'FUTURE_FUTURE', 'RELATED')


def pair_type_name(value):
    """The pair's DECLARED kind. Never inferred.

    Two futures with two expiries are not necessarily a calendar:
    GOLD vs GOLDM share an underlying and are a size RATIO, and CRUDEOIL
    vs NATURALGAS share nothing at all. Nothing in an expiry says
    whether two contracts have a carry between them, so the operator
    says.
    """
    name = str(value or '').strip().upper()
    return name if name in PAIR_TYPES else 'SPOT_FUTURE'


PRODUCTS = ('NRML', 'MIS')


def product_name(value):
    """NRML by default, and deliberately.

    MIS is squared off by the broker near the close, without asking. A
    spread half-squared-off is an outright, so choosing it is a
    decision the desk makes per pair rather than a default they inherit.
    """
    name = str(value or '').strip().upper()
    return name if name in PRODUCTS else 'NRML'


DEFAULT_SETTINGS = {
    # --- the loop -----------------------------------------------------
    'POLL_INTERVAL_SEC': 0.3,
    #: How often clicks are drained, on their own thread. This is the
    #: click-to-order latency the trader feels, and it is deliberately
    #: far shorter than the poll: waiting for the next poll would put a
    #: whole interval between the click and the order, on a product
    #: whose promise is that one click is one order.
    'COMMAND_POLL_SEC': 0.02,
    'SESSION_RETRY_SEC': 5.0,
    'ACCOUNT_INFO_CACHE_SEC': 5.0,

    # --- execution ----------------------------------------------------
    #: The naked window's ceiling. The crossing order goes IMMEDIATELY
    #: on fill; this is a failure-ESCALATION window, not patience.
    'LEG_DEADLINE_SEC': 2.0,
    #: How long a market order may go unresolved before we stop waiting
    #: and SAY SO. Not a rejection window — an order still working at
    #: the deadline is reported unresolved and is never unwound.
    'MARKET_RESOLVE_SEC': 5.0,
    'MIN_MATCHED_FRACTION': 0.4,
    #: How far through the clicked spread a MARKET click may fill before
    #: it is refused, in ladder increments.
    #:
    #: THE ONLY SLIPPAGE PROTECTION IN THE SYSTEM. `mt5.order_send`
    #: takes a `deviation` and the server enforces it; Arrow's
    #: `place_order` takes nothing at all. Set this to 0 and a market
    #: click fills at whatever the touch happens to be.
    'MARKET_PROTECTION_TICKS': 3.0,
    'CONFIRM_MARKET_CLICKS': False,
    'CLICK_AWAY_RESTS': True,
    'RECENTRE_SEC': 5.0,
    'CLICK_CONVENTION': 'TOUCH',
    #: On a netting venue an opposite click ALWAYS reduces — the
    #: exchange sees to that whatever we do. What this setting still
    #: governs is whether our own LEDGER attributes the reduction to an
    #: existing position (on) or records a new opposite one beside it
    #: (off). Off makes the book disagree with the account, so it is on,
    #: and it is kept as a name rather than removed because the settings
    #: page and its tests both know it.
    'CLOSE_FIRST': True,
    'ROW_HEIGHT_PX': 17,
    'REPEG_DEAD_BAND_TICKS': 1.0,
    #: What ONE order may carry. Over the exchange's freeze quantity the
    #: order is rejected outright, so a click over it is refused with
    #: the size that fits rather than sliced — slicing changes what one
    #: click means.
    'SLICE_OVER_FREEZE': False,
    'DEFAULT_PRODUCT': 'NRML',

    # --- guards on the price itself -----------------------------------
    'MAX_QUOTE_AGE_SEC': 15.0,
    'AUTO_REFRESH_STALE_SEC': 20.0,
    'MAX_SPREAD_JUMP_SIGMA': 5.0,      # 0 = off
    'JUMP_SETTLE_SEC': 2.0,
    'SIGMA_WINDOW_QUOTES': 600,

    # --- the session clock --------------------------------------------
    #: How often the exchange's clock offset is re-measured.
    'BROKER_CLOCK_TTL_SEC': 300.0,
    #: The cutoff, on the EXCHANGE's clock, in IST. MCX's evening close
    #: moves with US daylight saving and agri contracts close in the
    #: afternoon, so a single pair of numbers cannot serve every
    #: contract — this is the fallback, and `session.py` prefers the
    #: contract's own published session where there is one.
    'SESSION_CLOSE_HOUR': 23,
    'SESSION_CLOSE_MINUTE': 25,
    'OVERNIGHT_DEFAULT': OvernightMode.ALLOW.value,
    #: How long before a contract's TENDER period the ladder starts
    #: warning. MCX futures are physically settled and a position
    #: carried in can be assigned for delivery.
    'TENDER_WARN_DAYS': 7,
    #: Refuse to OPEN in a contract inside its tender period? A guard
    #: may withhold an order; it never prevents a close, so this can
    #: only ever stop a new position.
    'REFUSE_OPEN_IN_TENDER': False,

    # --- the exit ------------------------------------------------------
    'TP_TARGET_PCT_OF_MARGIN': 2.0,
    'MARGIN_TTL_SEC': 60.0,
    'MARGIN_WARN_LEVEL': 200.0,
    'BREAK_EVEN_NIGHTS': 0.0,
    #: The annualised financing rate the fair basis is priced off, and
    #: the storage/insurance per unit per year beside it. There is NO
    #: SWAP here — a futures carry is in the price, which is what the
    #: basis is — so these replace MT5-Trader's four swap fields.
    'CARRY_RATE_PCT': None,
    'STORAGE_PER_UNIT_YEAR': None,
    'AUTO_ROUTE_ENABLED': False,

    # --- costs ----------------------------------------------------------
    'SPREAD_COST_FACTOR': 1.0,
    #: The Indian charge stack, per segment. EVERY RATE DEFAULTS TO
    #: ZERO and `costs.is_configured` says whether anybody set them: a
    #: fabricated cost is charged against every trade and the operator
    #: cannot tell it was never theirs.
    'CHARGES': {},
    'SLIPPAGE_ALLOWANCE': 0.0,

    # --- housekeeping ---------------------------------------------------
    'RECONCILE_INTERVAL_SEC': 20.0,
    'CLOSE_ATTEMPTS': 3,
    #: 'ask' / 'always' / 'never' — an unanswered prompt means NO.
    'SHUTDOWN_CLOSE_POSITIONS': 'ask',
}

#: Settings the launcher reads at STARTUP. Changing one needs a restart
#: and must SAY so; everything else hot-applies. Crying "restart" on
#: every save teaches the operator to ignore the line that matters.
STRUCTURAL_SETTINGS = ('POLL_INTERVAL_SEC',)


class AccountConfig:
    """The Arrow session's credentials — and whether it is ours alone.

    Every secret is a NAME here, never a value. The values live in
    `.env` under those names and are read from the environment, so a
    config file that leaks tells an attacker only which keys to look
    for.
    """

    #: field -> the `.env` key it is read from.
    SECRETS = {
        'password': 'ARROW_PASSWORD',
        'api_secret': 'ARROW_API_SECRET',
        'totp_secret': 'ARROW_TOTP_SECRET',
    }

    def __init__(self, name='arrow', app_id=None, user_id=None,
                 dedicated=False, password_env=None, api_secret_env=None,
                 totp_secret_env=None):
        self.name = name
        self.app_id = app_id
        self.user_id = user_id
        #: Is this Arrow account used by NOTHING ELSE?
        #:
        #: It is not a nicety. There is no magic number on a netted
        #: position, so our net and the trader's own manual dealing in
        #: the same contract are one number. Declared dedicated, the
        #: reconciler may treat an unexplained net as a fault; shared,
        #: it must say it cannot tell — and the banner says which.
        #:
        #: FALSE BY DEFAULT, because the safe assumption is the one
        #: that claims less.
        self.dedicated = bool(dedicated)
        self.password_env = password_env or self.SECRETS['password']
        self.api_secret_env = api_secret_env or self.SECRETS['api_secret']
        self.totp_secret_env = totp_secret_env or self.SECRETS['totp_secret']

    @property
    def password(self):
        return os.environ.get(self.password_env)

    @property
    def api_secret(self):
        return os.environ.get(self.api_secret_env)

    @property
    def totp_secret(self):
        """The base32 SEED, not the 6-digit code."""
        return os.environ.get(self.totp_secret_env)

    def missing_secrets(self):
        """Which credentials are not set, by the name to set them under.

        Named rather than counted: "3 credentials missing" sends the
        operator hunting, and the fix is one line per name.
        """
        return [key for key in (self.password_env, self.api_secret_env,
                                self.totp_secret_env)
                if not os.environ.get(key)]

    def to_dict(self):
        # NAMES ONLY. A value must never reach this file.
        return {'app_id': self.app_id, 'user_id': self.user_id,
                'dedicated': self.dedicated,
                'password_env': self.password_env,
                'api_secret_env': self.api_secret_env,
                'totp_secret_env': self.totp_secret_env}

    @classmethod
    def from_dict(cls, name, raw):
        raw = raw or {}
        return cls(name, app_id=raw.get('app_id'), user_id=raw.get('user_id'),
                   dedicated=raw.get('dedicated', False),
                   password_env=raw.get('password_env'),
                   api_secret_env=raw.get('api_secret_env'),
                   totp_secret_env=raw.get('totp_secret_env'))


class PairConfig:
    """One ladder: two contracts, and how they are traded.

    Lot sizes and tick sizes are NOT typed in — they are read from the
    instrument master and cached here so the UI can render before the
    session answers. `hedge_ratio_for` stamps beta with the pair it was
    computed for, so a stale beta from the previous contract cannot
    silently define the spread.
    """

    #: This ladder's own overrides of the desk-wide settings. A blank
    #: field is not 0 and not a refusal: it means "whatever the default
    #: is", so a desk that prices every pair the same types nothing.
    EXIT_FIELDS = {
        'slippage_allowance': 'SLIPPAGE_ALLOWANCE',
        'break_even_nights': 'BREAK_EVEN_NIGHTS',
        'tp_target_pct_of_margin': 'TP_TARGET_PCT_OF_MARGIN',
        'carry_rate_pct': 'CARRY_RATE_PCT',
        'storage_per_unit_year': 'STORAGE_PER_UNIT_YEAR',
    }

    def __init__(self, key, name=None, leg_a=None, leg_b=None,
                 hedge_ratio=1.0, hedge_ratio_for=None,
                 pair_type='FUTURE_FUTURE', increment=None,
                 clip_lots_a=1.0, clip_lots_b=1.0,
                 contract_size_a=None, contract_size_b=None,
                 max_quote_age_sec=None, default_quantity=1.0,
                 order_type=OrderType.LIMIT.value,
                 exit_type=OrderType.MARKET.value,
                 time_in_force=TimeInForce.DAY.value,
                 overnight=OvernightMode.ALLOW.value,
                 product=None, quoting_leg=None, enabled=True, rows=30,
                 auto_route=False, slippage_allowance=None,
                 break_even_nights=None, tp_target_pct_of_margin=None,
                 carry_rate_pct=None, storage_per_unit_year=None,
                 algo_window=False):
        self.key = key
        self.name = name or key
        #: {'account': ..., 'symbol': ..., 'segment': ...}
        #:
        #: THE SEGMENT IS PART OF THE SYMBOL'S IDENTITY, in a way it
        #: never was on MT5. `GOLD05DEC25F` means nothing without
        #: `mcx_fo` beside it: the master is keyed on the pair, the
        #: order carries a different field again (`MCX`, not `MCXFO`),
        #: and the charge schedule is per segment because CTT applies
        #: to one of them.
        self.leg_a = dict(leg_a or {})
        self.leg_b = dict(leg_b or {})
        self.hedge_ratio = float(hedge_ratio or 1.0)
        self.hedge_ratio_for = hedge_ratio_for
        self.pair_type = pair_type_name(pair_type)
        #: Spread ticks per ladder row. None = derive it rather than
        #: guess a readable-looking one.
        self.increment = increment
        #: What ONE unit of the Qty box means on each leg, in LOTS.
        #: BOTH are the trader's. Nothing derives leg B — it was
        #: computed from the hedge arithmetic, or lot for lot, or by
        #: equal notional, and every one of those could round a leg to
        #: zero or size a pair the trader had not asked for.
        self.clip_lots_a = float(clip_lots_a or 1.0)
        self.clip_lots_b = float(clip_lots_b or 1.0)
        #: An override of the master's `LotSize`. None, and normally
        #: None: units are lots x LotSize and every money figure runs
        #: through it, so a number somebody typed is a number that can
        #: be wrong. Loud when it IS set.
        self.contract_size_a = _blank_to_none(contract_size_a)
        self.contract_size_b = _blank_to_none(contract_size_b)
        #: How long THIS pair may go unchanged before the spread is
        #: called stale. None = the desk-wide MAX_QUOTE_AGE_SEC. It has
        #: to be per pair: a far-month MCX contract trades a few times
        #: a minute while the near month ticks constantly, and one
        #: number cannot serve both.
        self.max_quote_age_sec = _blank_to_none(max_quote_age_sec)
        self.default_quantity = float(default_quantity or 1.0)
        self.order_type = _choice(OrderType, order_type,
                                  OrderType.LIMIT.value, key, 'order_type')
        self.exit_type = _choice(OrderType, exit_type,
                                 OrderType.MARKET.value, key, 'exit_type')
        self.time_in_force = _choice(TimeInForce, time_in_force,
                                     TimeInForce.DAY.value, key,
                                     'time_in_force')
        self.overnight = _choice(OvernightMode, overnight,
                                 OvernightMode.ALLOW.value, key, 'overnight')
        #: NRML or MIS. Part of a POSITION's identity here: the exchange
        #: nets per (symbol, product), so the same contract held on both
        #: products is two positions and our ledger has to say which.
        self.product = product_name(product)
        self.quoting_leg = quoting_leg
        self.enabled = bool(enabled)
        self.rows = int(rows or 30)
        self.auto_route = bool(auto_route)
        self.slippage_allowance = _blank_to_none(slippage_allowance)
        self.break_even_nights = _blank_to_none(break_even_nights)
        self.tp_target_pct_of_margin = _blank_to_none(tp_target_pct_of_margin)
        #: The two fair-value inputs that replaced MT5's four swap
        #: fields. An Indian future pays no overnight financing; its
        #: carry is IN the price, and for a physically-settled
        #: commodity that carry is interest plus storage.
        self.carry_rate_pct = _blank_to_none(carry_rate_pct)
        self.storage_per_unit_year = _blank_to_none(storage_per_unit_year)
        self.algo_window = bool(algo_window)
        #: Read from the master on every resolve; cached so the UI can
        #: render before the session answers.
        self.meta_a = {}
        self.meta_b = {}

    # -- the legs ---------------------------------------------------------

    @property
    def symbol_a(self):
        return self.leg_a.get('symbol')

    @property
    def symbol_b(self):
        return self.leg_b.get('symbol')

    @property
    def account_a(self):
        return self.leg_a.get('account') or 'arrow'

    @property
    def account_b(self):
        return self.leg_b.get('account') or 'arrow'

    @property
    def segment_a(self):
        return self.leg_a.get('segment')

    @property
    def segment_b(self):
        return self.leg_b.get('segment')

    # -- the ladder --------------------------------------------------------

    def derived_increment(self):
        """`max(tick_B, beta x tick_A)` — the smallest step the spread
        can actually move in.

        None when the tick sizes are not known, which the UI renders as
        an em dash rather than as a number it made up.
        """
        tick_a = (self.meta_a or {}).get('tick_size')
        tick_b = (self.meta_b or {}).get('tick_size')
        if not tick_a or not tick_b:
            return None
        return max(float(tick_b),
                   float(self.hedge_ratio or 1.0) * float(tick_a))

    def effective_increment(self):
        return self.increment or self.derived_increment()

    def expects_carry(self):
        """Should this pair HAVE a fair basis?

        A spot-vs-future and a calendar do. RELATED does not: nothing
        forces two different instruments together, so there is nothing
        to quote — and GOLD vs GOLDM is RELATED in this sense even
        though it is one underlying, because it is a size ratio and its
        fair spread is not a carry.
        """
        return self.pair_type in ('SPOT_FUTURE', 'FUTURE_FUTURE')

    def expiry(self, leg='b'):
        """The contract's expiry, from the MASTER. Never typed.

        MT5-Trader has an expiry FIELD because MT5 leaves
        `expiration_time` at 0 on most CFDs. The instrument master
        always carries `Expiry`, so there is nothing to type and
        nothing to get wrong.
        """
        meta = self.meta_a if leg == 'a' else self.meta_b
        return (meta or {}).get('expiry')

    def days_to_expiry(self, leg='b'):
        meta = self.meta_a if leg == 'a' else self.meta_b
        return (meta or {}).get('days_to_expiry')

    def exit_settings(self, settings):
        """The settings this ladder reads, defaults underneath."""
        merged = dict(settings or {})
        for field, key in self.EXIT_FIELDS.items():
            value = getattr(self, field, None)
            if value is not None:
                merged[key] = value
        return merged

    #: Fields a save applies WITHOUT a restart. Blocking these behind a
    #: restart is what put "a change requires a restart" ten lines above
    #: a live trade while the values sat saved and correct.
    HOT_FIELDS = (('auto_route', 'order_type', 'exit_type', 'time_in_force',
                   'overnight', 'product', 'increment', 'default_quantity',
                   'quoting_leg', 'rows', 'clip_lots_a', 'clip_lots_b',
                   'contract_size_a', 'contract_size_b', 'max_quote_age_sec',
                   'algo_window', 'pair_type')
                  + tuple(EXIT_FIELDS))

    def apply_hot(self, raw):
        """Take the hot fields from a freshly read config.

        Returns the names that actually CHANGED, so a hot-apply is
        logged on the change rather than on a clock.
        """
        changed = []
        for field in self.HOT_FIELDS:
            if field not in (raw or {}):
                continue
            value = raw[field]
            if field in self.EXIT_FIELDS or field in (
                    'contract_size_a', 'contract_size_b',
                    'max_quote_age_sec', 'increment'):
                value = _blank_to_none(value)
            elif field in ('auto_route', 'algo_window'):
                value = bool(value)
            elif field == 'pair_type':
                value = pair_type_name(value)
            elif field == 'product':
                value = product_name(value)
            elif field in ('order_type', 'exit_type'):
                value = _choice(OrderType, value,
                                getattr(self, field).value, self.key, field)
            elif field == 'time_in_force':
                value = _choice(TimeInForce, value,
                                self.time_in_force.value, self.key, field)
            elif field == 'overnight':
                value = _choice(OvernightMode, value,
                                self.overnight.value, self.key, field)
            elif field in ('default_quantity', 'clip_lots_a', 'clip_lots_b'):
                value = float(value or 1.0)
            elif field == 'rows':
                value = int(value or 30)
            if getattr(self, field, None) != value:
                setattr(self, field, value)
                changed.append(field)
        return changed

    def to_dict(self):
        return {
            'name': self.name, 'leg_a': dict(self.leg_a),
            'leg_b': dict(self.leg_b),
            'hedge_ratio': self.hedge_ratio,
            'hedge_ratio_for': self.hedge_ratio_for,
            'pair_type': self.pair_type, 'increment': self.increment,
            'clip_lots_a': self.clip_lots_a, 'clip_lots_b': self.clip_lots_b,
            'contract_size_a': self.contract_size_a,
            'contract_size_b': self.contract_size_b,
            'max_quote_age_sec': self.max_quote_age_sec,
            'default_quantity': self.default_quantity,
            'order_type': self.order_type.value,
            'exit_type': self.exit_type.value,
            'time_in_force': self.time_in_force.value,
            'overnight': self.overnight.value, 'product': self.product,
            'quoting_leg': self.quoting_leg, 'enabled': self.enabled,
            'rows': self.rows, 'auto_route': self.auto_route,
            'slippage_allowance': self.slippage_allowance,
            'break_even_nights': self.break_even_nights,
            'tp_target_pct_of_margin': self.tp_target_pct_of_margin,
            'carry_rate_pct': self.carry_rate_pct,
            'storage_per_unit_year': self.storage_per_unit_year,
            'algo_window': self.algo_window,
        }

    @classmethod
    def from_dict(cls, key, raw):
        raw = dict(raw or {})
        raw.pop('key', None)
        known = cls.__init__.__code__.co_varnames
        return cls(key, **{name: value for name, value in raw.items()
                           if name in known})


class TraderConfig:
    """Everything the engine reads at startup, in one object."""

    def __init__(self, account=None, pairs=None, settings=None, path=None):
        self.account = account or AccountConfig()
        self.pairs = pairs or {}
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.path = path

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def enabled_pairs(self):
        return {key: pair for key, pair in self.pairs.items() if pair.enabled}

    @property
    def accounts(self):
        """Both leg names map to the ONE account.

        Kept as a mapping because the executor and the coordinator
        index legs by name — and because it is the seam where a second
        venue would arrive.
        """
        return {self.account.name: self.account}

    @classmethod
    def from_raw(cls, raw, path=None):
        raw = raw or {}
        return cls(
            account=AccountConfig.from_dict(
                (raw.get('account') or {}).get('name', 'arrow'),
                raw.get('account')),
            pairs={key: PairConfig.from_dict(key, value)
                   for key, value in (raw.get('pairs') or {}).items()},
            settings=raw.get('settings'), path=path)

    @classmethod
    def from_file(cls, path):
        return cls.from_raw(load_raw(path), path=path)

    def to_raw(self):
        return {'account': dict(self.account.to_dict(),
                                name=self.account.name),
                'pairs': {key: pair.to_dict()
                          for key, pair in self.pairs.items()},
                'settings': dict(self.settings)}

    def restart_required(self, fresh):
        """Which STRUCTURAL settings changed, so a save can say whether
        a restart is needed rather than crying it every time."""
        return [key for key in STRUCTURAL_SETTINGS
                if self.get(key) != (fresh or {}).get(key)]


# --- reading and writing, safely -----------------------------------------

def load_raw(path):
    """The config as a plain dict.

    MISSING is legitimately empty (first run). PRESENT-BUT-BROKEN falls
    back to the `.bak` beside it, and failing that RAISES — a tolerant
    reader is precisely wrong here, because returning {} in front of a
    read-modify-write save is how every pair gets deleted.
    """
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        try:
            with open(path + '.bak', 'r', encoding='utf-8') as handle:
                backup = json.load(handle)
        except (OSError, ValueError):
            raise RuntimeError(
                f'{os.path.basename(path)} could not be read ({error}) and '
                f'there is no usable backup beside it. Refusing to continue, '
                f'because saving now would overwrite it with nothing.'
            ) from None
        logging.error('%s unreadable (%s) — using the .bak. The next save '
                      'will rewrite the good copy.', path, error)
        return backup


#: Top-level keys whose disappearance is a catastrophe rather than an
#: edit: they are what lets the engine start at all.
CRITICAL_KEYS = ('account', 'pairs')


def save_raw(path, raw, allow_shrink=False):
    """Write the config, keeping a backup and refusing to gut it.

    `allow_shrink` is for the endpoints that legitimately remove things
    (deleting a pair). Everything else is a partial edit and must not
    be able to drop a section it never meant to touch.
    """
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            current = json.load(handle)
    except (OSError, ValueError):
        current = None
    if current and not allow_shrink:
        lost = [key for key in CRITICAL_KEYS
                if current.get(key) and not raw.get(key)]
        if lost:
            raise RuntimeError(
                'refusing to save a config that would drop '
                + ', '.join(lost)
                + ' — this looks like a partial read, not an edit')
    if current is not None:
        tmp_backup = path + '.bak.tmp'
        with open(tmp_backup, 'w', encoding='utf-8') as handle:
            json.dump(current, handle, indent=2)
        atomicfile.replace(tmp_backup, path + '.bak')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(raw, handle, indent=2)
    atomicfile.replace(tmp, path)


def pair_key(symbol_a, symbol_b):
    """The key a pair is filed under: its two trading symbols.

    Not the underlying and not a name the operator types. Two GOLD
    calendars a month apart are different ladders with different
    positions, and a key that could not tell them apart would merge
    them on the next roll.
    """
    return f'{symbol_a}|{symbol_b}'


def secrets_present():
    """Which Arrow credentials are set in the environment, by name.

    The values are never returned, logged or rendered — only whether
    each key has something in it.
    """
    return {key: bool(os.environ.get(key))
            for key in ('ARROW_APP_ID', 'ARROW_USER_ID', 'ARROW_PASSWORD',
                        'ARROW_API_SECRET', 'ARROW_TOTP_SECRET')}


_ENV_SAFE = re.compile(r'[^A-Z0-9]+')


def env_line(key, value):
    """One `.env` line, quoted so a secret with spaces or `#` survives."""
    escaped = str(value or '').replace('\\', '\\\\').replace('"', '\\"')
    return f'{key}="{escaped}"'


def write_env_value(path, key, value):
    """Set one key in `.env`, leaving the rest of the file alone.

    Written through a tmp file and `os.replace` for the same reason the
    config is: a truncated `.env` is every credential gone.
    """
    lines = []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except OSError:
        pass
    replaced = False
    out = []
    for line in lines:
        if line.split('=', 1)[0].strip() == key:
            if not replaced:
                out.append(env_line(key, value))
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(env_line(key, value))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(out) + '\n')
    atomicfile.replace(tmp, path)
    os.environ[key] = str(value or '')
    try:
        os.chmod(path, 0o600)
    except OSError:               # not every filesystem allows it
        pass
