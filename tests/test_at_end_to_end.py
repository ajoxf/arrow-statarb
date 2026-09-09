"""A fresh install, walked all the way to a priced ladder.

Every other test file checks one seam. This one is the whole path an
operator takes on day one, in order, through the SAME endpoints the
browser calls and the SAME config file the engine reads:

    no config at all
      -> credentials typed on the Exchanges page
      -> Connect
      -> search the master, pick both contracts
      -> Read both legs (derive beta, the increment, the lot sizes)
      -> Save pair
      -> start the engine against that config file
      -> poll
      -> a spread, and a ladder with sizes on it

It exists because the front end and the back end have already spoken
different languages once in this build — settings.js called three
endpoints that did not exist, and every panel test was green. A test
that walks the seams in order is the only kind that catches that.

The arithmetic at the end is checked against numbers worked out by
hand from the fake's books, not against whatever the code produced.
"""

import json
import sys
import types

import pytest

from tests import fake_arrow as F
from arrowtrader.webapp import create_app


@pytest.fixture
def arrow_sdk(monkeypatch):
    module = types.ModuleType('pyarrow_client')
    for name in ('Exchange', 'OrderType', 'ProductType', 'TransactionType',
                 'Retention', 'Variety', 'QuoteMode', 'DataMode'):
        setattr(module, name, getattr(F, name))
    module.ArrowClient = F.FakeArrowClient
    module.ArrowStreams = F.FakeStreams
    monkeypatch.setitem(sys.modules, 'pyarrow_client', module)
    from arrowtrader import broker
    monkeypatch.setattr(broker, 'arrow', module)
    return module


@pytest.fixture
def fresh(tmp_path, arrow_sdk, monkeypatch):
    """A first run: the launcher's files, and nothing filled in."""
    import run_arrowtrader
    paths = {
        'status_path': str(tmp_path / 'status.json'),
        'command_path': str(tmp_path / 'commands.jsonl'),
        'results_path': str(tmp_path / 'results.json'),
        'config_path': str(tmp_path / 'config.json'),
        'db_path': str(tmp_path / 'engine.db'),
        'env_path': str(tmp_path / '.env'),
    }
    # The REAL launcher writes them, not the test.
    run_arrowtrader.ensure_files(paths['config_path'], paths['env_path'])
    for key in ('ARROW_APP_ID', 'ARROW_USER_ID', 'ARROW_PASSWORD',
                'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.delenv(key, raising=False)

    from arrowtrader import config as cfg

    # `.env` is the only place a secret goes. The endpoint writes it;
    # the process has to be told to re-read it, which is what a restart
    # does for real.
    def reload_env():
        for line in open(paths['env_path'], encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            name, _, value = line.partition('=')
            value = value.strip().strip('"').strip("'")
            if value:
                monkeypatch.setenv(name.strip(), value)

    app = create_app(**paths)
    app.config['TESTING'] = True
    return types.SimpleNamespace(client=app.test_client(), paths=paths,
                                 reload_env=reload_env, cfg=cfg)


def test_a_FRESH_INSTALL_walks_all_the_way_to_a_priced_ladder(fresh):
    client, paths = fresh.client, fresh.paths

    # -- 1. nothing is filled in, and the page says exactly that ------------
    account = client.get('/api/account').get_json()
    assert account['app_id'] is None
    assert set(account['missing']) == {'ARROW_PASSWORD', 'ARROW_API_SECRET',
                                       'ARROW_TOTP_SECRET'}
    # ...and the picker refuses rather than pretending, WITHOUT having
    # asked Arrow anything.
    blind = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
    assert blind['ok'] is False and 'not set' in blind['error']

    # -- 2. the credentials, through the endpoint the page posts to --------
    saved = client.post('/api/account', json={
        'app_id': 'APP123', 'user_id': 'USER1', 'dedicated': True,
        'password': 'pw', 'api_secret': 'sec',
        'totp_secret': 'JBSWY3DPEHPK3PXP',
    }).get_json()
    assert saved['ok'] is True
    assert saved['restart_required'] is True

    # THE CREDENTIALS RULE: `.env` holds them and config.json does not.
    env_text = open(paths['env_path'], encoding='utf-8').read()
    assert 'JBSWY3DPEHPK3PXP' in env_text
    config_text = open(paths['config_path'], encoding='utf-8').read()
    for secret in ('pw', 'sec', 'JBSWY3DPEHPK3PXP'):
        assert secret not in config_text, 'a secret reached config.json'
    # ...and a secret goes in and never comes back out.
    back = client.get('/api/account').get_json()
    assert back['secrets'] == {} or 'JBSWY3DPEHPK3PXP' not in json.dumps(back)

    fresh.reload_env()

    # -- 3. Connect ---------------------------------------------------------
    live = client.post('/api/connect').get_json()
    assert live['ok'] is True, live.get('error')
    assert live['master_rows'] == 7
    assert 'MCXFO' in live['segments']

    # -- 4. the segments page, with a real session behind it ---------------
    segments = client.get('/api/segments').get_json()
    assert segments['segments']['mcx_fo']['ready'] is True
    assert segments['order'][0] == 'mcx_fo'

    # -- 5. search, and pick both contracts --------------------------------
    found = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
    assert found['ok'] is True
    symbols = [row['trading_symbol'] for row in found['symbols']]
    assert 'GOLD05DEC25F' in symbols and 'GOLD05FEB26F' in symbols
    # Oldest expiry first — the near month is leg A.
    assert symbols.index('GOLD05DEC25F') < symbols.index('GOLD05FEB26F')

    spec = client.get('/api/contract/GOLD05DEC25F').get_json()
    assert spec['found'] is True
    assert spec['lot_size'] == 100          # READ, never typed

    # -- 6. Read both legs from the master ---------------------------------
    derived = client.post('/api/pair/derive', json={
        'symbol_a': 'GOLD05DEC25F', 'symbol_b': 'GOLD05FEB26F',
        'hedge_ratio': 1.0,
    }).get_json()
    assert derived['ok'] is True
    assert derived['legs']['a']['lot_size'] == 100
    assert derived['legs']['b']['lot_size'] == 100
    assert derived['increment'] == pytest.approx(1.0)   # max(tick_b, b*tick_a)

    # -- 7. Save the pair, with the payload settings.js actually sends -----
    key = 'GOLD05DEC25F|GOLD05FEB26F'
    assert client.post(f'/api/pairs/{key}', json={
        'name': 'GOLD05DEC25F / GOLD05FEB26F',
        'leg_a': {'account': 'arrow', 'symbol': 'GOLD05DEC25F',
                  'segment': 'mcx_fo'},
        'leg_b': {'account': 'arrow', 'symbol': 'GOLD05FEB26F',
                  'segment': 'mcx_fo'},
        'hedge_ratio': derived['hedge_ratio'],
        'hedge_ratio_for': key,
        'increment': derived['increment'],
        'clip_lots_a': 1, 'clip_lots_b': 1,
        'pair_type': 'FUTURE_FUTURE', 'product': 'NRML', 'enabled': True,
    }).get_json()['ok'] is True

    # -- 8. the ENGINE, started from that same file ------------------------
    from arrowtrader.broker import ArrowSession
    from arrowtrader.config import TraderConfig
    from arrowtrader.coordinator import Coordinator
    from arrowtrader.legs import make_legs
    from arrowtrader.segments import SegmentTable

    raw = fresh.cfg.load_raw(paths['config_path'])
    config = TraderConfig.from_raw(raw)
    assert key in config.pairs, (
        'the engine cannot see the pair the page just saved')
    pair = config.pairs[key]
    assert pair.enabled is True
    assert pair.leg_a['symbol'] == 'GOLD05DEC25F'
    assert pair.leg_a['segment'] == 'mcx_fo'
    assert pair.product == 'NRML'

    session = ArrowSession(config.account, SegmentTable())
    legs = make_legs(['arrow'], session)
    engine = Coordinator(config, legs, status_path=paths['status_path'])
    engine.start()
    engine.poll_once()

    # -- 9. the spread, checked against numbers worked out by hand ---------
    md = engine.market[key]
    assert md is not None, engine.errors.get(key)
    # The fake's books, in rupees:
    #   GOLD05DEC25F (leg A)  74999 / 75001
    #   GOLD05FEB26F (leg B)  75499 / 75502
    # spread = B - 1.0 x A, from the MID OF THE BOOK, never the last trade.
    assert md['leg_a_bid'] == pytest.approx(74999.0)
    assert md['leg_b_ask'] == pytest.approx(75502.0)
    assert md['spread'] == pytest.approx(75500.5 - 75000.0)      # 500.5
    # SHORT the spread = sell B, buy A -> hit B's bid, lift A's ask.
    assert md['short_spread'] == pytest.approx(75499.0 - 75001.0)  # 498
    # LONG  the spread = buy B, sell A -> lift B's ask, hit A's bid.
    assert md['long_spread'] == pytest.approx(75502.0 - 74999.0)   # 503
    # One round turn of both books, and the same number two ways.
    assert md['spread_cost'] == pytest.approx(
        (75502.0 - 75499.0) + 1.0 * (75001.0 - 74999.0))           # 5
    assert md['spread_cost'] == pytest.approx(
        md['long_spread'] - md['short_spread'])
    # short <= mid <= long, which everything downstream relies on.
    assert md['short_spread'] <= md['spread'] <= md['long_spread']

    # -- 10. the ladder ----------------------------------------------------
    row = engine.snapshot()['pairs'][key]
    assert row['rows'], 'the ladder is empty'
    levels = [line['level'] for line in row['rows']]
    assert levels == sorted(levels, reverse=True)
    best_bid = next(l for l in row['rows'] if l['is_best_bid'])
    best_ask = next(l for l in row['rows'] if l['is_best_ask'])
    assert best_bid['level'] == pytest.approx(498.0)
    assert best_ask['level'] == pytest.approx(503.0)
    # Sizes came from the DOM, in CLIPS. The fake offers 2 lots of B at
    # its touch against 3 lots of A: the smaller leg decides.
    assert best_ask['ask_size'] == 2
    assert best_bid['bid_size'] == 2
    # Nothing INSIDE the spread's own bid-ask can be filled.
    inside = [l for l in row['rows'] if 498.0 < l['level'] < 503.0]
    assert inside
    assert all(l['bid_size'] is None and l['ask_size'] is None
               for l in inside), 'the ladder priced size inside the spread'

    # -- 11. the money multiplier ------------------------------------------
    # k = L_B x C_B: one point of spread is worth this many rupees.
    assert row['units_b'] == 100
    assert row['spread_units'] == 100

    # -- 12. the snapshot is on disk, and the web app serves it ------------
    engine.publish()
    served = client.get('/api/status').get_json()
    assert served['engine'] == 'up'
    assert served['pairs'][key]['rows']


def test_the_engine_REFUSES_a_pair_whose_beta_was_computed_for_OTHER_contracts(
        fresh):
    """The roll trap. A calendar is re-pointed at next month's
    contracts every few weeks, and a beta left over from the old pair
    silently redefines the spread — every price on the ladder shifts
    and nothing says why."""
    from arrowtrader.config import TraderConfig
    raw = fresh.cfg.load_raw(fresh.paths['config_path'])
    raw['pairs'] = {'GOLD05DEC25F|GOLD05FEB26F': {
        'leg_a': {'account': 'arrow', 'symbol': 'GOLD05DEC25F',
                  'segment': 'mcx_fo'},
        'leg_b': {'account': 'arrow', 'symbol': 'GOLD05FEB26F',
                  'segment': 'mcx_fo'},
        'hedge_ratio': 1.7,
        # Stamped with a pair that is NOT this one.
        'hedge_ratio_for': 'GOLD05OCT25F|GOLD05DEC25F',
        'enabled': True,
    }}
    config = TraderConfig.from_raw(raw)
    pair = config.pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.stale_hedge_ratio() is True
    note = pair.hedge_ratio_note()
    assert 'GOLD05OCT25F' in note and 'NOT for' in note


def test_a_beta_with_NO_stamp_is_said_but_blocks_NOTHING(fresh):
    """The control, and it matters more than it looks.

    A missing stamp is an older config, not a wrong beta. Refusing to
    trade every pair on the first run after an upgrade would be a guard
    doing far more harm than the risk it covers — so this one is said
    on the screen and stops nothing.
    """
    from arrowtrader.config import TraderConfig
    raw = fresh.cfg.load_raw(fresh.paths['config_path'])
    raw['pairs'] = {'GOLD05DEC25F|GOLD05FEB26F': {
        'leg_a': {'account': 'arrow', 'symbol': 'GOLD05DEC25F',
                  'segment': 'mcx_fo'},
        'leg_b': {'account': 'arrow', 'symbol': 'GOLD05FEB26F',
                  'segment': 'mcx_fo'},
        'hedge_ratio': 1.0, 'enabled': True,
    }}
    pair = TraderConfig.from_raw(raw).pairs['GOLD05DEC25F|GOLD05FEB26F']
    assert pair.stale_hedge_ratio() is None
    assert 'not stamped' in pair.hedge_ratio_note()


def test_a_beta_derived_for_THESE_contracts_says_nothing_at_all(fresh):
    from arrowtrader.config import TraderConfig
    key = 'GOLD05DEC25F|GOLD05FEB26F'
    raw = fresh.cfg.load_raw(fresh.paths['config_path'])
    raw['pairs'] = {key: {
        'leg_a': {'account': 'arrow', 'symbol': 'GOLD05DEC25F',
                  'segment': 'mcx_fo'},
        'leg_b': {'account': 'arrow', 'symbol': 'GOLD05FEB26F',
                  'segment': 'mcx_fo'},
        'hedge_ratio': 1.0, 'hedge_ratio_for': key, 'enabled': True,
    }}
    pair = TraderConfig.from_raw(raw).pairs[key]
    assert pair.stale_hedge_ratio() is False
    assert pair.hedge_ratio_note() is None


def test_the_WEB_SURVIVES_an_engine_that_cannot_start(tmp_path, monkeypatch,
                                                      arrow_sdk):
    """The screen is where the operator reads what went wrong.

    A bad database path, a read-only directory, a broker that will not
    log in — every one of those used to raise out of `run_engine`, out
    of `main`, and end the process, killing the daemon web thread with
    it. What was left was a traceback in a terminal nobody may be
    looking at and no screen at all: the one thing that could have
    explained the failure was the thing the failure removed.
    """
    import run_arrowtrader
    from arrowtrader import database

    config_path = str(tmp_path / 'config.json')
    env_path = str(tmp_path / '.env')
    run_arrowtrader.ensure_files(config_path, env_path)
    from arrowtrader import config as cfg
    raw = cfg.load_raw(config_path)
    raw['account'].update({'app_id': 'APP123', 'user_id': 'USER1'})
    cfg.save_raw(config_path, raw)
    for key, value in (('ARROW_PASSWORD', 'pw'), ('ARROW_API_SECRET', 'sec'),
                       ('ARROW_TOTP_SECRET', 'JBSWY3DPEHPK3PXP')):
        monkeypatch.setenv(key, value)

    def broken(*args, **kwargs):
        raise OSError('disk I/O error')

    monkeypatch.setattr(database, 'Store', broken)

    args = type('Args', (), {
        'config': config_path, 'env': env_path,
        'db': str(tmp_path / 'engine.db'),
        'status': str(tmp_path / 'status.json'),
        'commands': str(tmp_path / 'c.jsonl'),
        'results': str(tmp_path / 'r.json'),
    })()
    import threading
    # It RETURNS FALSE — which the launcher's retry loop is built for —
    # rather than raising and taking everything down.
    assert run_arrowtrader.run_engine(args, threading.Event()) is False
