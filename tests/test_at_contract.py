"""Every endpoint the front end calls must EXIST and answer JSON.

THIS FILE EXISTS BECAUSE THE SUITE MISSED THE OBVIOUS.

`app.js` and `settings.js` were copied across from MT5-Trader and a
different set of endpoints was written behind them. The Exchanges page
called `/api/accounts` and `/api/connection`; the Slippage tab called
`/api/slippage`. None of the three existed. Every one 404'd with an
HTML error page, the browser tried to parse it as JSON, and the panel
showed `Unexpected token '<'`.

The suite was 452 tests green through all of it, because the test that
was supposed to cover the front end asserted **line counts** — proof
the files had been copied, and no evidence at all that they worked.

So this file reads the endpoints out of the SHIPPED JavaScript and
checks each one against the running app. It cannot go stale: add a
`fetch('/api/whatever')` to the front end and this test starts failing
until the endpoint is there.
"""

import json
import os
import re

import pytest

from arrowtrader.webapp import create_app

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(HERE, 'arrowtrader', 'static')

#: `/api/x` in a string literal, and `'/api/x/' + id` — the two shapes
#: the front end uses. The trailing-slash form is a prefix: the test
#: appends a plausible id.
_CALL = re.compile(r"""['"`](/api/[a-zA-Z0-9_/-]*)['"`]""")


def endpoints_called():
    """Every `/api/...` literal in the shipped front end."""
    found = set()
    for name in ('app.js', 'settings.js'):
        with open(os.path.join(STATIC, name), encoding='utf-8') as handle:
            found.update(_CALL.findall(handle.read()))
    return sorted(found)


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        monkeypatch.setenv(key, 'set')
    from arrowtrader import config as cfg
    config_path = str(tmp_path / 'config.json')
    cfg.save_raw(config_path, {'account': {'name': 'arrow'}, 'pairs': {},
                               'settings': {}})
    app = create_app(status_path=str(tmp_path / 'status.json'),
                     command_path=str(tmp_path / 'commands.jsonl'),
                     results_path=str(tmp_path / 'results.json'),
                     config_path=config_path,
                     db_path=str(tmp_path / 'db.sqlite'),
                     env_path=str(tmp_path / '.env'))
    app.config['TESTING'] = True
    return app.test_client()


def rules(client):
    return {str(rule) for rule in client.application.url_map.iter_rules()}


def test_the_front_end_calls_nothing_that_does_not_exist(client):
    """The whole class of bug, in one assertion.

    A 404 comes back as an HTML error page. The browser parses it as
    JSON and the panel shows `Unexpected token '<'` — which tells the
    operator nothing about which endpoint is missing.
    """
    known = rules(client)
    missing = []
    for path in endpoints_called():
        if path.endswith('/'):
            # `'/api/pairs/' + key` — a prefix, not a path.
            if not any(rule.startswith(path) for rule in known):
                missing.append(path + '<id>')
            continue
        if path not in known:
            missing.append(path)
    assert not missing, (
        'the front end calls endpoints the app does not serve: '
        + ', '.join(missing))


@pytest.mark.parametrize('path', [
    '/api/status', '/api/config', '/api/settings', '/api/pairs',
    '/api/account', '/api/segments', '/api/charges', '/api/fills',
    '/api/slippage', '/api/events',
])
def test_every_GET_answers_JSON_even_with_nothing_configured(client, path):
    """A fresh install has no session, no pairs and no database. Every
    panel must still render — with an empty state or a reason, never a
    stack trace and never HTML that a JSON parser chokes on."""
    response = client.get(path)
    assert response.status_code == 200, f'{path} -> {response.status_code}'
    assert response.headers['Content-Type'].startswith('application/json'), (
        f'{path} answered {response.headers["Content-Type"]} — a panel '
        f'parsing that as JSON shows "Unexpected token" and names nothing')
    json.loads(response.data)          # raises if it is not JSON


def test_a_missing_endpoint_would_be_CAUGHT_by_this_file(client):
    """The control. If `/api/slippage` were deleted the first test
    above must go red — otherwise this file is as useless as the
    line-count test it replaces."""
    assert '/api/slippage' in endpoints_called()
    assert '/api/slippage' in rules(client)


def test_the_front_end_carries_no_MT5_vocabulary():
    """Terminal paths, logins, runner endpoints and swaps are the MT5
    model. None of them exists here, and a field asking for one is a
    field the operator will fill in."""
    banned = ('terminal64', 'terminal_path', 'Runner endpoint', 'MetaTrader',
              'Algo Trading', 'swap_a_long', 'swap_b_long', 'from MT5',
              'next_free_port', 'endpoint_clash', 'login_clash')
    for name in ('app.js', 'settings.js'):
        with open(os.path.join(STATIC, name), encoding='utf-8') as handle:
            body = _without_comments(handle.read())
        for word in banned:
            assert word not in body, f'{name} still SHOWS "{word}"'


def _without_comments(source):
    """Strip comments, so this judges what the USER SEES.

    A comment explaining why `terminal64.exe` is gone is not that
    word leaking into the UI, and banning it there would push the
    reasoning out of the one file that has to carry it.
    """
    source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    return re.sub(r'(?m)^\s*//.*$', '', source)


def test_money_on_the_screen_is_RUPEES():
    with open(os.path.join(STATIC, 'app.js'), encoding='utf-8') as handle:
        body = handle.read()
    assert 'k $' not in body, 'the Market Grid still heads its k column in $'
    assert "'\\u20B9'" in body or '₹' in body
