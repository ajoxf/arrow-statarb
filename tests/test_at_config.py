"""Accounts, pairs and settings — and the clash that replaced three."""

import json
import os

import pytest

from arrowtrader import config as cfg
from arrowtrader.models import OrderType, OvernightMode


RAW = {
    'account': {'name': 'arrow', 'app_id': 'APP123', 'user_id': 'USER1',
                'dedicated': True},
    'pairs': {
        'GOLD05DEC25F|GOLD05FEB26F': {
            'name': 'Gold Dec/Feb',
            'leg_a': {'account': 'arrow', 'symbol': 'GOLD05DEC25F',
                      'segment': 'mcx_fo'},
            'leg_b': {'account': 'arrow', 'symbol': 'GOLD05FEB26F',
                      'segment': 'mcx_fo'},
            'pair_type': 'FUTURE_FUTURE', 'clip_lots_a': 1, 'clip_lots_b': 1,
        },
    },
    'settings': {'MARKET_PROTECTION_TICKS': 5.0},
}


# -- credentials are NAMES, never values --------------------------------------

def test_the_config_holds_only_the_NAME_of_each_secret():
    """A config file that leaks tells an attacker which keys to look
    for, and nothing else."""
    account = cfg.AccountConfig.from_dict('arrow', RAW['account'])
    saved = json.dumps(account.to_dict())
    assert 'ARROW_PASSWORD' in saved
    for field in ('password', 'api_secret', 'totp_secret'):
        assert field not in json.loads(saved)


def test_a_secret_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv('ARROW_PASSWORD', 'hunter2')
    monkeypatch.setenv('ARROW_TOTP_SECRET', 'JBSWY3DPEHPK3PXP')
    account = cfg.AccountConfig('arrow')
    assert account.password == 'hunter2'
    assert account.totp_secret == 'JBSWY3DPEHPK3PXP'


def test_missing_credentials_are_NAMED_not_counted(monkeypatch):
    """'3 credentials missing' sends the operator hunting; the fix is
    one line per name."""
    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.delenv(key, raising=False)
    missing = cfg.AccountConfig('arrow').missing_secrets()
    assert 'ARROW_TOTP_SECRET' in missing
    # CONTROL: set one and it drops off the list.
    monkeypatch.setenv('ARROW_TOTP_SECRET', 'seed')
    assert 'ARROW_TOTP_SECRET' not in cfg.AccountConfig('arrow').missing_secrets()


# -- the clash that replaced three --------------------------------------------

def test_an_account_is_NOT_dedicated_by_default():
    """There is no magic number on a netted position, so our net and
    the trader's own dealing are one number. The safe assumption is the
    one that claims less."""
    assert cfg.AccountConfig('arrow').dedicated is False
    assert cfg.AccountConfig.from_dict('arrow', RAW['account']).dedicated


def test_both_legs_resolve_to_the_ONE_account():
    """MT5-Trader polices three clashes because it runs a terminal per
    account. None of them can happen here."""
    built = cfg.TraderConfig.from_raw(RAW)
    pair = built.pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.account_a == pair.account_b == 'arrow'
    assert list(built.accounts) == ['arrow']


# -- what an Indian contract has that an MT5 symbol does not ------------------

def test_a_leg_carries_its_SEGMENT():
    """`GOLD05DEC25F` means nothing without `mcx_fo` beside it: the
    master is keyed on the pair, the order carries a different field
    again, and the charge schedule is per segment."""
    pair = cfg.TraderConfig.from_raw(RAW).pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.segment_a == 'mcx_fo' and pair.segment_b == 'mcx_fo'


def test_the_product_defaults_to_NRML():
    """MIS is squared off by the broker near the close without asking,
    and a spread half-squared-off is an outright."""
    pair = cfg.TraderConfig.from_raw(RAW).pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.product == 'NRML'
    assert cfg.product_name('mis') == 'MIS'
    assert cfg.product_name('nonsense') == 'NRML'


def test_the_pair_key_is_the_two_TRADING_SYMBOLS():
    """Two GOLD calendars a month apart are different ladders with
    different positions, and a key that could not tell them apart would
    merge them on the next roll."""
    assert cfg.pair_key('GOLD05DEC25F', 'GOLD05FEB26F') \
        == 'GOLD05DEC25F|GOLD05FEB26F'
    assert cfg.pair_key('GOLD05DEC25F', 'GOLD05FEB26F') \
        != cfg.pair_key('GOLD05FEB26F', 'GOLD05APR26F')


# -- the ladder ---------------------------------------------------------------

def test_the_increment_is_DERIVED_and_none_until_it_can_be():
    pair = cfg.TraderConfig.from_raw(RAW).pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.derived_increment() is None          # no meta yet
    assert pair.effective_increment() is None
    pair.meta_a = {'tick_size': 1.0}
    pair.meta_b = {'tick_size': 1.0}
    assert pair.derived_increment() == 1.0
    # An override wins, and is visible as one.
    pair.increment = 0.5
    assert pair.effective_increment() == 0.5


def test_beta_scales_leg_A_in_the_derived_increment():
    pair = cfg.PairConfig('k', hedge_ratio=2.0)
    pair.meta_a = {'tick_size': 1.0}
    pair.meta_b = {'tick_size': 1.0}
    assert pair.derived_increment() == 2.0


def test_a_RELATED_pair_has_no_carry_to_quote():
    """GOLD vs GOLDM is one underlying but a size RATIO, and its fair
    spread is not a carry."""
    assert cfg.PairConfig('k', pair_type='RELATED').expects_carry() is False
    assert cfg.PairConfig('k', pair_type='FUTURE_FUTURE').expects_carry()
    assert cfg.PairConfig('k', pair_type='SPOT_FUTURE').expects_carry()


def test_an_unknown_pair_type_falls_back_rather_than_raising():
    assert cfg.pair_type_name('calendar') == 'SPOT_FUTURE'
    assert cfg.pair_type_name('FUTURE_FUTURE') == 'FUTURE_FUTURE'


def test_a_bad_enum_in_the_config_does_not_stop_the_engine():
    pair = cfg.PairConfig('k', order_type='Limit ', overnight='nonsense')
    assert pair.order_type is OrderType.LIMIT
    assert pair.overnight is OvernightMode.ALLOW


# -- blank is not zero ---------------------------------------------------------

def test_a_BLANK_override_means_the_default_and_ZERO_is_a_real_number():
    pair = cfg.PairConfig('k', tp_target_pct_of_margin='',
                          slippage_allowance=0)
    settings = {'TP_TARGET_PCT_OF_MARGIN': 2.0, 'SLIPPAGE_ALLOWANCE': 5.0}
    merged = pair.exit_settings(settings)
    assert merged['TP_TARGET_PCT_OF_MARGIN'] == 2.0     # blank -> default
    assert merged['SLIPPAGE_ALLOWANCE'] == 0.0          # 0 is a real number


def test_the_carry_inputs_replaced_the_swap_fields():
    """An Indian future pays no overnight financing; its carry is IN
    the price, and for a physically-settled commodity that carry is
    interest plus storage."""
    pair = cfg.PairConfig('k', carry_rate_pct=6.5, storage_per_unit_year=12.0)
    merged = pair.exit_settings({})
    assert merged['CARRY_RATE_PCT'] == 6.5
    assert merged['STORAGE_PER_UNIT_YEAR'] == 12.0
    assert not any('swap' in name for name in vars(pair))


# -- hot fields ----------------------------------------------------------------

def test_apply_hot_reports_what_actually_CHANGED():
    """So a hot-apply is logged on the change rather than on a clock."""
    pair = cfg.PairConfig('k', rows=30, product='NRML')
    assert pair.apply_hot({'rows': 30}) == []
    assert pair.apply_hot({'rows': 50, 'product': 'MIS'}) == ['product', 'rows']
    assert pair.rows == 50 and pair.product == 'MIS'


def test_only_the_poll_interval_needs_a_restart():
    """Crying 'restart' on every save teaches the operator to ignore
    the line that matters."""
    built = cfg.TraderConfig.from_raw(RAW)
    assert built.restart_required({'POLL_INTERVAL_SEC': 0.3}) == []
    assert built.restart_required({'POLL_INTERVAL_SEC': 1.0}) \
        == ['POLL_INTERVAL_SEC']


# -- the charge stack starts EMPTY --------------------------------------------

def test_no_charge_rate_is_invented():
    """A fabricated cost is charged against every trade and the
    operator cannot tell it was never theirs."""
    assert cfg.DEFAULT_SETTINGS['CHARGES'] == {}


def test_the_only_slippage_guard_is_ON_by_default():
    assert cfg.DEFAULT_SETTINGS['MARKET_PROTECTION_TICKS'] > 0


# -- reading and writing -------------------------------------------------------

def test_a_missing_config_is_empty_not_an_error(tmp_path):
    assert cfg.load_raw(str(tmp_path / 'nope.json')) == {}


def test_a_BROKEN_config_falls_back_to_the_bak(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{ not json')
    (tmp_path / 'config.json.bak').write_text(json.dumps(RAW))
    assert cfg.load_raw(str(path))['pairs']


def test_a_broken_config_with_NO_backup_RAISES(tmp_path):
    """A tolerant reader is precisely wrong here: returning {} in front
    of a read-modify-write save is how every pair gets deleted."""
    path = tmp_path / 'config.json'
    path.write_text('{ not json')
    with pytest.raises(RuntimeError, match='Refusing to continue'):
        cfg.load_raw(str(path))


def test_a_save_that_would_DROP_the_pairs_is_refused(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, RAW)
    with pytest.raises(RuntimeError, match='partial read'):
        cfg.save_raw(path, {'account': RAW['account']})
    # CONTROL: a deliberate removal is allowed to say so.
    cfg.save_raw(path, {'account': RAW['account'], 'pairs': {}},
                 allow_shrink=True)
    assert cfg.load_raw(path)['pairs'] == {}


def test_a_save_keeps_a_backup(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, RAW)
    cfg.save_raw(path, dict(RAW, settings={'POLL_INTERVAL_SEC': 1.0}))
    assert cfg.load_raw(path + '.bak')['settings']['MARKET_PROTECTION_TICKS'] \
        == 5.0


def test_a_round_trip_keeps_every_field(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, cfg.TraderConfig.from_raw(RAW).to_raw())
    back = cfg.TraderConfig.from_file(path)
    pair = back.pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.segment_a == 'mcx_fo'
    assert back.account.dedicated is True
    assert back.get('MARKET_PROTECTION_TICKS') == 5.0


# -- .env ----------------------------------------------------------------------

def test_a_secret_with_spaces_or_a_hash_survives_the_env_file(tmp_path,
                                                              monkeypatch):
    path = str(tmp_path / '.env')
    cfg.write_env_value(path, 'ARROW_PASSWORD', 'a b#c"d')
    assert os.environ['ARROW_PASSWORD'] == 'a b#c"d'
    assert 'ARROW_PASSWORD="a b#c\\"d"' in open(path).read()


def test_writing_one_key_leaves_the_others_alone(tmp_path):
    """A truncated .env is every credential gone."""
    path = str(tmp_path / '.env')
    cfg.write_env_value(path, 'ARROW_PASSWORD', 'one')
    cfg.write_env_value(path, 'ARROW_API_SECRET', 'two')
    cfg.write_env_value(path, 'ARROW_PASSWORD', 'three')
    body = open(path).read()
    assert 'ARROW_API_SECRET="two"' in body
    assert 'ARROW_PASSWORD="three"' in body
    assert body.count('ARROW_PASSWORD') == 1


def test_secrets_present_reports_WHETHER_never_WHAT(monkeypatch):
    monkeypatch.setenv('ARROW_PASSWORD', 'hunter2')
    monkeypatch.delenv('ARROW_API_SECRET', raising=False)
    found = cfg.secrets_present()
    assert found['ARROW_PASSWORD'] is True
    assert found['ARROW_API_SECRET'] is False
    assert 'hunter2' not in repr(found)
