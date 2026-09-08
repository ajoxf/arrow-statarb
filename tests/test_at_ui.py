"""The front end, checked the two ways it can be checked offline.

STATIC checks over the shipped files — no browser needed, and they
catch the things that are true of the SOURCE. The Playwright suite
below reads `pageerror`, because Python cannot see a temporal-dead-zone
ReferenceError that aborts a script block and silently unregisters a
handler — which is exactly what happened in the system this is ported
from. It skips cleanly where no browser is installed.
"""

import os
import re

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(HERE, 'arrowtrader', 'static')
TEMPLATES = os.path.join(HERE, 'arrowtrader', 'templates')


def read(name):
    path = (os.path.join(TEMPLATES, name) if name.endswith('.html')
            else os.path.join(STATIC, name))
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# -- the UI is not a redesign -------------------------------------------------

def test_the_front_end_is_the_SAME_FILES():
    """The trader must not be able to tell the two apart by looking.
    These are MT5-Trader's files; if one has been rewritten rather than
    adapted, the line counts will say so."""
    assert len(read('app.js').splitlines()) > 3900
    assert len(read('ladder.css').splitlines()) > 1800
    assert len(read('settings.js').splitlines()) > 1100
    assert len(read('index.html').splitlines()) > 450


def test_the_five_column_grid_is_intact():
    page = read('index.html')
    for column in ('Work', 'Bids', 'Price', 'Asks', 'LTQ'):
        assert f'>{column}<' in page


def test_the_keyboard_shortcuts_are_unchanged():
    page = read('index.html')
    for key in ('<th>B</th>', '<th>S</th>', '<th>F</th>', '<th>X</th>',
                '<th>L</th>', '<th>M</th>'):
        assert key in page


def test_there_are_NO_native_dialogs():
    """A test fails the build if they come back. The one shared modal
    must work when the network is what failed."""
    body = read('app.js') + read('settings.js')
    stripped = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    stripped = re.sub(r'(?m)^\s*//.*$', '', stripped)
    for banned in ('window.confirm(', 'window.alert(', 'window.prompt('):
        assert banned not in stripped
    for banned in (r'\bconfirm\(', r'\balert\(', r'\bprompt\('):
        assert not re.search(r'(?<![.\w])' + banned, stripped), banned


def test_nothing_is_loaded_from_a_CDN():
    """A blocked CDN has already taken a trading UI down once, and the
    dialog that reports 'could not save' must work when the network is
    what failed."""
    page = read('index.html')
    assert 'http://' not in page
    assert 'https://' not in page


# -- what the DATA required us to change --------------------------------------

def test_money_is_RUPEES_grouped_the_indian_way():
    """en-IN puts the separators at 1,00,000 rather than 100,000. A
    desk reading lakhs off a screen grouped in thousands re-reads every
    figure."""
    body = read('app.js')
    assert "'\\u20B9'" in body
    assert "toLocaleString('en-IN'" in body
    assert "toLocaleString('en-US'" not in body


def test_the_same_login_banner_is_GONE_and_carries_unresolved_instead():
    """Two legs on one login is the normal case here — one Arrow
    session trades both. What replaced it is worse and is specific to
    this venue."""
    body = read('app.js')
    assert 'renderSameLogin' not in body
    assert 'function renderUnresolved(' in body
    assert 'renderUnresolved()' in body


def test_the_unresolved_banner_says_nothing_was_unwound():
    body = read('app.js')
    assert 'Nothing has been unwound' in body
    # Split across a concatenation in the source, so match the
    # fragment that is actually there rather than the rendered phrase.
    assert 'is NAKED until this is ' in body
    # Only a person clears it — and clearing is a COMMAND.
    assert "send('clear_unresolved'" in body


def test_the_ladder_settings_offer_PRODUCT():
    """The exchange nets per (symbol, PRODUCT), and MIS is squared off
    by the broker without asking."""
    page = read('index.html')
    assert 'class="ls-product"' in page
    assert 'NRML (carry)' in page and 'MIS (intraday)' in page
    assert "['.ls-product', 'product', 'live']" in read('app.js')


def test_the_swap_fields_are_GONE_and_carry_replaced_them():
    """An Indian future pays no overnight financing: its carry is IN
    the price, which is what the basis is."""
    page = read('index.html')
    body = read('app.js')
    for gone in ('ls-swap-a-long', 'ls-swap-a-short', 'ls-swap-b-long',
                 'ls-swap-b-short'):
        assert gone not in page, gone
        assert gone not in body, gone
    assert 'Interest % a year' in page
    assert 'Storage / unit / year' in page
    assert "['.ls-storage', 'storage_per_unit_year'" in body


def test_the_lot_size_field_says_LOT_SIZE_and_names_the_master():
    page = read('index.html')
    assert 'Lot size A' in page and 'Lot size B' in page
    assert 'from the master' in page
    assert 'lots x LotSize UNITS' in page


def test_the_expiry_fields_are_READ_ONLY():
    """The instrument master always carries Expiry, unlike MT5 which
    leaves it at 0 on most CFDs. There is nothing to type."""
    page = read('index.html')
    assert page.count('type="text" readonly') >= 2


def test_the_TIF_caveat_says_what_is_TRUE_HERE():
    """MT5's caveat is that no working order survives the process. A
    leg order does here — the SPREAD does not."""
    page = read('index.html')
    assert 'outlives this process' in page
    assert 'OUTRIGHT, not a hedge' in page


def test_the_close_at_limit_tooltip_says_the_order_is_REAL():
    page = read('index.html')
    assert 'REAL order at the exchange' in page


# -- the browser suite ----------------------------------------------------------

def _launchers(driver):
    """Ways to get a Chromium, best first.

    The pinned playwright build and the browsers on the box can be
    different revisions — the pip package wants build N and the image
    ships N-40 — so a bare `launch()` looks for an executable that is
    not there. Falling back to the binary that IS there keeps this
    suite running instead of skipping it silently, which for a suite
    whose whole job is catching errors Python cannot see would be the
    worst kind of green.
    """
    import glob
    import os
    yield driver.chromium.launch
    for pattern in ('/opt/pw-browsers/chromium-*/chrome-linux/chrome',
                    '/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/'
                    'headless_shell'):
        for found in sorted(glob.glob(pattern), reverse=True):
            if os.access(found, os.X_OK):
                yield (lambda path=found:
                       driver.chromium.launch(executable_path=path))


@pytest.fixture
def page(tmp_path):
    """A real Chromium on the real page, or a clean skip."""
    playwright = pytest.importorskip('playwright.sync_api',
                                     reason='playwright is not installed')
    import json
    import threading
    import time
    from werkzeug.serving import make_server
    from arrowtrader.webapp import create_app

    status = str(tmp_path / 'status.json')
    with open(status, 'w') as handle:
        json.dump({'at': time.time(), 'pairs': {}, 'unresolved': [],
                   'accounts': {}, 'dark_accounts': [], 'currency': 'INR',
                   'single_pool': True, 'reconciler': {}, 'recovery': {},
                   'session_events': []}, handle)
    app = create_app(status_path=status,
                     command_path=str(tmp_path / 'commands.jsonl'),
                     results_path=str(tmp_path / 'results.json'),
                     config_path=str(tmp_path / 'config.json'),
                     db_path=str(tmp_path / 'db.sqlite'),
                     env_path=str(tmp_path / '.env'))
    server = make_server('127.0.0.1', 0, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port

    try:
        with playwright.sync_playwright() as driver:
            browser = None
            for launch in _launchers(driver):
                try:
                    browser = launch()
                    break
                except Exception:                       # noqa: BLE001
                    continue
            if browser is None:
                # A CLEAN SKIP, never a failure. A box with no browser
                # must run the whole rest of the suite — these tests
                # are an addition to it, not a prerequisite.
                pytest.skip('no usable Chromium for this playwright build')
            tab = browser.new_page()
            errors = []
            # THE WHOLE POINT OF THIS SUITE. A temporal-dead-zone
            # ReferenceError aborts a script block and silently
            # unregisters every handler after it. Python cannot see
            # that; the page looks fine and the buttons do nothing.
            tab.on('pageerror', lambda error: errors.append(str(error)))
            tab.on('console', lambda message: errors.append(message.text)
                   if message.type == 'error' else None)
            tab.goto(f'http://127.0.0.1:{port}/')
            tab.wait_for_timeout(700)
            yield tab, errors
            browser.close()
    finally:
        server.shutdown()


def test_the_page_loads_with_NO_script_errors(page):
    tab, errors = page
    assert errors == [], errors


def test_the_chrome_is_all_there(page):
    tab, _errors = page
    for selector in ('#brand', '#taskbar', '#modal', '#toasts',
                     '#help-overlay', '#desktop', '#kill',
                     '#engine-banner', '#naked-banner', '#unclaimed-banner',
                     '#same-login-banner'):
        assert tab.query_selector(selector) is not None, selector


def test_the_ladder_template_survived_the_port(page):
    tab, _errors = page
    template = tab.query_selector('#ladder-template')
    assert template is not None
    markup = template.inner_html()
    for piece in ('c-work', 'c-bid', 'c-price', 'c-ask', 'c-ltq',
                  'ls-product', 'ls-storage', 'buy-touch', 'sell-touch',
                  'flatten', 'close-limit-go'):
        assert piece in markup, piece


def test_the_help_overlay_opens_and_closes(page):
    tab, errors = page
    tab.click('#help')
    assert not tab.query_selector('#help-overlay').get_attribute(
        'class').count('hidden')
    tab.click('#help-close')
    assert 'hidden' in tab.query_selector('#help-overlay').get_attribute(
        'class')
    assert errors == [], errors
