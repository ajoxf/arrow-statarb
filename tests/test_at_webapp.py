"""The web process: it renders, and it asks. It never trades.

Two rules it must keep and one that is new here: setup works with the
ENGINE DOWN, a refusal carries the broker's own words — and a secret
goes IN and never comes OUT.
"""

import json
import time

import pytest

from arrowtrader.webapp import create_app


class FakeSession:
    """A setup session that connects, or refuses with a real sentence."""

    def __init__(self, account, ok=True, error=None):
        self.account = account
        self.connected = False
        self._ok = ok
        self.last_error = error

    def initialize(self):
        self.connected = self._ok
        return self._ok

    def sdk_exchanges(self):
        return ['NSE', 'NFO']

    @property
    def master(self):
        from arrowtrader.instruments import Master
        from arrowtrader.segments import SegmentTable
        from tests.fake_arrow import MASTER
        return Master(MASTER, segments=SegmentTable())

    def find_symbols(self, pattern, limit=40, segment=None, kind=None):
        return [contract.to_dict() for contract
                in self.master.search(pattern, segment=segment, kind=kind,
                                      limit=limit)]

    def symbol_report(self, symbol):
        return self.master.report(symbol)


@pytest.fixture
def paths(tmp_path):
    return {
        'status_path': str(tmp_path / 'status.json'),
        'command_path': str(tmp_path / 'commands.jsonl'),
        'results_path': str(tmp_path / 'results.json'),
        'config_path': str(tmp_path / 'config.json'),
        'db_path': str(tmp_path / 'engine.db'),
        'env_path': str(tmp_path / '.env'),
    }


@pytest.fixture
def client(paths, monkeypatch):
    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.setenv(key, 'set')
    from arrowtrader import config as cfg
    cfg.save_raw(paths['config_path'],
                 {'account': {'name': 'arrow', 'app_id': 'APP',
                              'user_id': 'USER', 'dedicated': True},
                  'pairs': {}, 'settings': {}})
    app = create_app(session_factory=lambda account: FakeSession(account),
                     **paths)
    app.config['TESTING'] = True
    return app.test_client()


def live(paths, **extra):
    """Write a status file that looks like a running engine."""
    payload = {'at': time.time(), 'pairs': {}, **extra}
    with open(paths['status_path'], 'w') as handle:
        json.dump(payload, handle)


# -- a dead engine is not a quiet market ---------------------------------------

def test_no_status_file_reads_as_ENGINE_DOWN(client):
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'down'
    assert 'no click will reach the exchange' in body['engine_note']


def test_a_STALE_status_file_is_not_a_quiet_market(client, paths):
    with open(paths['status_path'], 'w') as handle:
        json.dump({'at': time.time() - 30, 'pairs': {}}, handle)
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'stale'
    assert 'a click now would reach nothing' in body['engine_note']


def test_a_command_is_REFUSED_while_the_engine_is_down(client, paths):
    """A command written while nothing is running would be executed by
    whatever starts next, at prices from another hour."""
    response = client.post('/api/command',
                           json={'kind': 'click',
                                 'payload': {'pair': 'k', 'side': 'BUY',
                                             'level': 1.0}})
    assert response.status_code == 409
    # CONTROL: with the engine up, the same command is accepted.
    live(paths)
    assert client.post('/api/command',
                       json={'kind': 'click',
                             'payload': {'pair': 'k', 'side': 'BUY',
                                         'level': 1.0}}).status_code == 200


def test_a_command_without_a_kind_is_refused(client, paths):
    live(paths)
    assert client.post('/api/command', json={}).status_code == 400


def test_a_command_is_written_to_the_log_not_executed_here(client, paths):
    """This process renders and asks. It never trades."""
    live(paths)
    body = client.post('/api/command',
                       json={'kind': 'flatten_pair',
                             'payload': {'pair': 'k'}}).get_json()
    assert body['ok'] is True and body['id']
    written = open(paths['command_path']).read().strip()
    assert json.loads(written)['kind'] == 'flatten_pair'


# -- a secret goes IN and never comes OUT ---------------------------------------

def test_saving_credentials_writes_ENV_and_returns_only_WHETHER(client,
                                                                paths):
    response = client.post('/api/account',
                           json={'app_id': 'APP2', 'user_id': 'USER2',
                                 'password': 'hunter2',
                                 'totp_secret': 'JBSWY3DPEHPK3PXP'})
    body = response.get_json()
    assert body['ok'] is True
    assert 'hunter2' not in json.dumps(body)
    assert 'JBSWY3DPEHPK3PXP' not in json.dumps(body)
    assert body['secrets']['ARROW_PASSWORD'] is True
    # ...and the secret is in .env, not in config.json.
    assert 'hunter2' in open(paths['env_path']).read()
    assert 'hunter2' not in open(paths['config_path']).read()


def test_the_account_endpoint_never_renders_a_secret(client, monkeypatch):
    monkeypatch.setenv('ARROW_PASSWORD', 'hunter2')
    body = client.get('/api/account').get_json()
    assert 'hunter2' not in json.dumps(body)
    assert body['secrets']['ARROW_PASSWORD'] is True


def test_the_config_endpoint_shows_only_env_KEY_NAMES(client):
    body = client.get('/api/config').get_json()
    assert 'password' not in body['account']
    assert body['account'].get('app_id') == 'APP'


def test_a_missing_credential_is_NAMED(client, paths, monkeypatch):
    monkeypatch.delenv('ARROW_TOTP_SECRET', raising=False)
    body = client.get('/api/account').get_json()
    assert 'ARROW_TOTP_SECRET' in body['missing']


def test_the_dedicated_declaration_is_stored_as_the_operators_word(client,
                                                                   paths):
    """Nothing at the exchange can confirm it. Every reconciler finding
    depends on it."""
    client.post('/api/account', json={'dedicated': False})
    from arrowtrader import config as cfg
    assert cfg.load_raw(paths['config_path'])['account']['dedicated'] is False


# -- setup works with the ENGINE DOWN --------------------------------------------

def test_connecting_works_without_the_engine(client):
    """Otherwise the system deadlocks: the engine will not start until
    the contracts are right and these are the tools for that."""
    body = client.post('/api/connect').get_json()
    assert body['ok'] is True
    assert body['master_rows'] == 7


def test_a_refusal_carries_the_reason_not_check_the_log(client, paths,
                                                        monkeypatch):
    monkeypatch.delenv('ARROW_PASSWORD', raising=False)
    body = client.post('/api/connect').get_json()
    assert body['ok'] is False
    assert 'ARROW_PASSWORD' in body['error']
    assert 'never to config.json' in body['error']


def test_a_broker_refusal_is_passed_through_verbatim(paths, monkeypatch):
    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.setenv(key, 'set')
    from arrowtrader import config as cfg
    cfg.save_raw(paths['config_path'],
                 {'account': {'name': 'arrow'}, 'pairs': {}, 'settings': {}})
    app = create_app(
        session_factory=lambda account: FakeSession(
            account, ok=False,
            error="this host's IP is not registered with Arrow"),
        **paths)
    app.config['TESTING'] = True
    body = app.test_client().post('/api/connect').get_json()
    assert "IP is not registered" in body['error']


def test_the_picker_works_without_the_engine(client):
    body = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
    assert body['ok'] is True
    # FUTURES BY DEFAULT. The master also holds GOLD calls and puts,
    # and a spread ladder is two futures.
    assert {row['trading_symbol'] for row in body['symbols']} == {
        'GOLD05DEC25F', 'GOLD05FEB26F', 'GOLDM05DEC25F'}


def test_the_picker_defaults_to_FUTURES_and_says_so_by_omission(client):
    """MCX lists thousands of options against a handful of futures on
    one underlying. Unfiltered, a search for the underlying came back
    all calls and puts — which is what was on the screen: two options
    in the dropdown and no futures at all."""
    body = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
    kinds = {row['kind'] for row in body['symbols']}
    assert kinds == {'future'}


def test_the_picker_will_still_show_OPTIONS_when_asked(client):
    """The control. Defaulting to futures is not pretending the options
    are not there."""
    body = client.get(
        '/api/find?q=GOLD&segment=mcx_fo&kind=option').get_json()
    assert body['ok'] is True
    assert body['symbols'], 'asking for options returned none'
    assert {row['kind'] for row in body['symbols']} == {'option'}
    assert {row['trading_symbol'] for row in body['symbols']} == {
        'GOLD05DEC25C120000', 'GOLD05DEC25P118000'}


# -- blocker 3.1 lives on an endpoint ---------------------------------------------

def test_the_segments_endpoint_separates_the_two_MCX_failures(client):
    """The account not being entitled and the SDK having no enum value
    fail with the same symptom and different fixes."""
    body = client.get('/api/segments').get_json()
    mcx = body['segments']['mcx_fo']
    assert mcx['in_master'] is True          # the fake master has MCXFO
    assert mcx['in_sdk'] is False            # ...and the fake SDK does not
    # The VERSION, not just 'upgrade': on the one segment this
    # system exists for, the answer is a single release number.
    assert '1.7.0' in mcx['note']
    assert body['segments']['nse_fo']['ready'] is True


# -- the charge stack says whether anybody filled it in ----------------------------

def test_an_unconfigured_charge_schedule_SAYS_SO(client):
    """Every rate defaults to zero, so an unconfigured desk sees 0.00
    everywhere — and a zero cost is indistinguishable from a free trade
    unless the screen says which it is."""
    body = client.get('/api/charges').get_json()
    assert body['mcx_fo']['configured'] is False
    assert 'not a free trade' in body['mcx_fo']['note']


def test_a_configured_schedule_has_no_warning(client, paths):
    from arrowtrader import config as cfg
    raw = cfg.load_raw(paths['config_path'])
    raw['settings'] = {'CHARGES': {'mcx_fo': {'brokerage_per_order': 20.0}}}
    cfg.save_raw(paths['config_path'], raw)
    body = client.get('/api/charges').get_json()
    assert body['mcx_fo']['configured'] is True
    assert body['mcx_fo']['note'] is None


# -- deriving a pair ----------------------------------------------------------------

def test_deriving_a_pair_shows_the_DERIVATION_beside_each_number(client):
    """Offered as a one-click correction, never applied silently."""
    body = client.post('/api/pair/derive',
                       json={'symbol_a': 'GOLD05DEC25F',
                             'symbol_b': 'GOLD05FEB26F',
                             'hedge_ratio': 1.0}).get_json()
    assert body['ok'] is True
    assert body['increment'] == 1.0
    assert 'max(tick B' in body['increment_note']
    assert 'lots x LotSize' in body['lot_note']
    assert body['hedge_ratio_for'] == 'GOLD05DEC25F|GOLD05FEB26F'


def test_deriving_a_pair_REPORTS_a_leg_it_cannot_find(client):
    body = client.post('/api/pair/derive',
                       json={'symbol_a': 'NOPE',
                             'symbol_b': 'GOLD05FEB26F'}).get_json()
    assert body['legs']['a']['found'] is False
    assert any('instrument master' in problem for problem in body['problems'])


def test_a_contract_reports_its_specs_with_nothing_invented(client):
    """Every number the sizing depends on, read from the master. A
    missing one is a PROBLEM naming the fix, never a default — the
    no-tick-size and no-LotSize paths are pinned in
    test_at_instruments.py."""
    body = client.get('/api/contract/GOLDM05DEC25F').get_json()
    assert body['found'] is True
    assert body['ok'] is True
    assert body['lot_size'] == 10          # units per lot, from the master
    assert body['tick_size'] == 1.0
    assert body['freeze_qty'] == 10000
    assert body['expiry'] == '2025-12-05'


def test_a_contract_that_is_NOT_in_the_master_says_so(client):
    body = client.get('/api/contract/GOLD05JAN99F').get_json()
    assert body['found'] is False
    assert 'instrument master' in body['error']


# -- the roll ------------------------------------------------------------------------

def test_the_roll_offers_the_NEXT_contracts(client):
    """An MCX calendar has to be re-pointed every month or two."""
    body = client.get('/api/roll/GOLD05DEC25F').get_json()
    assert [row['trading_symbol'] for row in body['next']] \
        == ['GOLD05FEB26F']


# -- pairs -----------------------------------------------------------------------------

def test_saving_and_deleting_a_pair(client, paths):
    key = 'GOLD05DEC25F|GOLD05FEB26F'
    assert client.post(f'/api/pairs/{key}',
                       json={'leg_a': {'symbol': 'GOLD05DEC25F',
                                       'segment': 'mcx_fo'}}).get_json()['ok']
    assert key in client.get('/api/pairs').get_json()
    assert client.delete(f'/api/pairs/{key}').get_json()['ok']
    assert client.get('/api/pairs').get_json() == {}


def test_deleting_a_pair_that_is_not_there_is_a_404(client):
    assert client.delete('/api/pairs/nope').status_code == 404


# -- settings -------------------------------------------------------------------------

def test_a_hot_setting_says_it_applied_NOW(client, paths):
    live(paths)
    body = client.post('/api/settings',
                       json={'fields': {'MARKET_PROTECTION_TICKS': 5.0}}
                       ).get_json()
    assert body['applied_now'] == ['MARKET_PROTECTION_TICKS']
    assert body['restart_required'] == []


def test_a_structural_setting_says_it_needs_a_RESTART(client, paths):
    """Crying restart on every save teaches the operator to ignore the
    line that matters."""
    live(paths)
    body = client.post('/api/settings',
                       json={'fields': {'POLL_INTERVAL_SEC': 1.0}}).get_json()
    assert body['restart_required'] == ['POLL_INTERVAL_SEC']


def test_an_unknown_setting_is_refused_by_name(client):
    body = client.post('/api/settings',
                       json={'fields': {'NOT_A_SETTING': 1}}).get_json()
    assert 'not a setting: NOT_A_SETTING' in body['error']


# -- the page itself --------------------------------------------------------------------

def test_the_page_renders_and_is_never_cached(client):
    response = client.get('/')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert b'NEXUS' in response.data


# -- a failed login is not retried on every keystroke -------------------------

def test_a_REFUSED_login_is_not_re_attempted_on_every_call(paths, monkeypatch):
    """The picker searches AS THE OPERATOR TYPES.

    Every one of those searches needs a session, and a session that is
    not up used to mean a full three-step login — a password POST, a
    TOTP and a token exchange — per pause in typing. Against a broker
    that rate-limits authentication, looking for a contract is then
    enough to get the account locked out.

    The cooldown answers with the SAME WORDS as the first refusal. One
    that said "please wait" instead would hide the reason.
    """
    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.setenv(key, 'set')
    from arrowtrader import config as cfg
    cfg.save_raw(paths['config_path'],
                 {'account': {'name': 'arrow'}, 'pairs': {}, 'settings': {}})
    tries = []

    def refuse(account):
        tries.append(account)
        return FakeSession(account, ok=False,
                           error="this host's IP is not registered with Arrow")

    app = create_app(session_factory=refuse, **paths)
    app.config['TESTING'] = True
    client = app.test_client()

    for _ in range(8):
        body = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
        assert body['ok'] is False
        assert "IP is not registered" in body['error']
    assert len(tries) == 1, (
        f'{len(tries)} login attempts for 8 searches — the picker is '
        f'hammering the broker while somebody types')


def test_pressing_CONNECT_always_tries_AGAIN_immediately(paths, monkeypatch):
    """The control for the cooldown.

    Connect is the operator saying "try again NOW", usually right after
    fixing the thing that was wrong. A cooldown that swallowed it would
    replay the stale refusal and the corrected password would look
    broken too.
    """
    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.setenv(key, 'set')
    from arrowtrader import config as cfg
    cfg.save_raw(paths['config_path'],
                 {'account': {'name': 'arrow'}, 'pairs': {}, 'settings': {}})
    tries = []

    def refuse(account):
        tries.append(account)
        return FakeSession(account, ok=False, error='Invalid OTP')

    app = create_app(session_factory=refuse, **paths)
    app.config['TESTING'] = True
    client = app.test_client()

    for _ in range(3):
        assert client.post('/api/connect').get_json()['ok'] is False
    assert len(tries) == 3


def test_MISSING_credentials_are_never_put_behind_a_cooldown(paths, monkeypatch):
    """Nothing was asked of Arrow, so there is nothing to back off from
    — and the moment the operator fills the field in, the next call has
    to try."""
    monkeypatch.delenv('ARROW_PASSWORD', raising=False)
    monkeypatch.delenv('ARROW_API_SECRET', raising=False)
    monkeypatch.delenv('ARROW_TOTP_SECRET', raising=False)
    from arrowtrader import config as cfg
    cfg.save_raw(paths['config_path'],
                 {'account': {'name': 'arrow'}, 'pairs': {}, 'settings': {}})
    tries = []
    app = create_app(session_factory=lambda account: tries.append(account)
                     or FakeSession(account), **paths)
    app.config['TESTING'] = True
    client = app.test_client()
    body = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
    assert body['ok'] is False
    assert 'not set' in body['error']
    assert tries == []

    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.setenv(key, 'set')
    body = client.get('/api/find?q=GOLD&segment=mcx_fo').get_json()
    assert body['ok'] is True, 'the next call after fixing it must try'


# -- a command that cannot run says WHY ------------------------------------------

def test_a_command_MISSING_A_FIELD_names_the_field(paths, tmp_path):
    """`str(KeyError('pair'))` is `"'pair'"` — a quoted field name and
    nothing else. It reached the operator as a toast reading `pair`,
    naming neither the command nor what was wrong. Same fault as the
    SDK's bare `KeyError: 'redirectUrl'`, and the same fix."""
    from arrowtrader.commands import CommandRunner

    class Nothing:
        # A coordinator that WOULD take the click. The point is that
        # the payload never gets far enough to reach it.
        def click(self, *args, **kwargs):
            raise AssertionError('a click with no pair must not be sent')

    runner = CommandRunner(Nothing(), str(tmp_path / 'c.jsonl'),
                           str(tmp_path / 'r.json'))
    answer = runner.execute({'id': 'x', 'kind': 'click', 'payload': {}})
    assert answer['ok'] is False
    assert answer['error'] != "'pair'"
    assert 'click' in answer['error'] and 'pair' in answer['error']
    assert 'bug in the page' in answer['error']


def test_a_setting_this_build_CANNOT_APPLY_is_refused_not_ignored(paths,
                                                                  tmp_path):
    """`ok: true, changed: []` said the change was made. A renamed
    field, a typo, or a page newer than the engine is a setting the
    operator watched turn green and which did nothing."""
    from arrowtrader.commands import CommandRunner
    from arrowtrader.config import TraderConfig

    config = TraderConfig.from_raw({
        'account': {'name': 'arrow'},
        'pairs': {'K': {'leg_a': {'account': 'arrow', 'symbol': 'A'},
                        'leg_b': {'account': 'arrow', 'symbol': 'B'}}},
        'settings': {}})

    class Engine:
        pass

    engine = Engine()
    engine.config = config
    runner = CommandRunner(engine, str(tmp_path / 'c.jsonl'),
                           str(tmp_path / 'r.json'))

    bad = runner.execute({'id': 'x', 'kind': 'set_pair', 'payload': {
        'pair': 'K', 'fields': {'no_such_field': 3}}})
    assert bad['data']['ok'] is False
    assert 'no_such_field' in bad['data']['error']

    # The control: a field it CAN set is applied and reported.
    good = runner.execute({'id': 'y', 'kind': 'set_pair', 'payload': {
        'pair': 'K', 'fields': {'rows': 40}}})
    assert good['data']['ok'] is True
    assert good['data']['changed'] == ['rows']
    assert config.pairs['K'].rows == 40

    # And a MIXED one applies what it can and says what it did not.
    mixed = runner.execute({'id': 'z', 'kind': 'set_pair', 'payload': {
        'pair': 'K', 'fields': {'rows': 12, 'nope': 1}}})
    assert mixed['data']['changed'] == ['rows']
    assert 'nope' in mixed['data']['error']
