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

def test_the_LADDER_is_the_reference_screen():
    """The trader must not be able to tell the two apart by looking.

    THIS REPLACED A LINE-COUNT ASSERTION, which was the worst kind of
    test: it proved the files had been COPIED and gave no evidence at
    all that they worked. It stayed green through an Exchanges page
    that called three endpoints which did not exist.

    So this checks the things that are actually load-bearing about the
    look — the grid, the colour convention, the row height — and the
    browser tests below check that the panels function.
    """
    css = read('ladder.css')
    # BID IS BLUE, ASK IS RED, and that convention is global: a price
    # must not change colour depending on which table it sits in.
    assert '--bid: #4a9ede' in css
    assert '--ask: #b83232' in css
    assert '--row-h: 17px' in css
    # The five columns, in order, in the LADDER's own header — not the
    # help overlay, which names Work and LTQ first and would let a
    # scrambled grid pass.
    page = read('index.html')
    header = page[page.index('<th class="c-work"'):page.index('</thead>')]
    order = [header.index(f'>{column}<')
             for column in ('Work', 'Bids', 'Price', 'Asks', 'LTQ')]
    assert order == sorted(order), 'the five columns are out of order'


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
    import os
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


# -- the browser test that WOULD have caught it --------------------------------

@pytest.fixture
def panels(tmp_path):
    """A live page, with every network response recorded.

    The old browser test loaded `/`, checked some elements existed, and
    stopped. It never opened the Exchanges page, the Market Grid or the
    Monitor — so it never made one call to a `settings.js` endpoint, and
    stayed green through three 404s that broke half the UI.

    This one opens every panel and watches every response.
    """
    playwright = pytest.importorskip('playwright.sync_api',
                                     reason='playwright is not installed')
    import json
    import os
    import threading
    import time
    from werkzeug.serving import make_server
    from arrowtrader import config as cfg
    from arrowtrader.webapp import create_app

    status = str(tmp_path / 'status.json')
    with open(status, 'w') as handle:
        json.dump({'at': time.time(), 'pairs': {}, 'unresolved': [],
                   'accounts': {}, 'dark_accounts': [], 'currency': 'INR',
                   'single_pool': True, 'dedicated': False,
                   'reconciler': {}, 'recovery': {}, 'session_events': [],
                   'hedge_times_ms': [], 'click_to_on_ms': []}, handle)
    config_path = str(tmp_path / 'config.json')
    cfg.save_raw(config_path, {'account': {'name': 'arrow'}, 'pairs': {},
                               'settings': {}})
    # A session that connects, so the PICKER can be exercised. Without
    # one every search answers "these credentials are not set", and the
    # contract dropdown is empty for a reason that has nothing to do
    # with the code under test.
    from tests.test_at_webapp import FakeSession

    class Connected(FakeSession):
        # The fake SDK used in the webapp tests deliberately has no MCX
        # value, so that the two MCX failure modes can be told apart
        # there. Here the segment has to be READY, or the picker never
        # offers it and the test measures the wrong absence.
        def sdk_exchanges(self):
            return ['NSE', 'NFO', 'MCXFO']

    for key in ('ARROW_PASSWORD', 'ARROW_API_SECRET', 'ARROW_TOTP_SECRET'):
        os.environ[key] = 'set'
    app = create_app(status_path=status,
                     command_path=str(tmp_path / 'commands.jsonl'),
                     results_path=str(tmp_path / 'results.json'),
                     config_path=config_path,
                     db_path=str(tmp_path / 'db.sqlite'),
                     env_path=str(tmp_path / '.env'),
                     session_factory=lambda account: Connected(account))
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
                pytest.skip('no usable Chromium for this playwright build')
            tab = browser.new_page()
            errors, responses = [], []
            tab.on('pageerror', lambda error: errors.append(str(error)))
            tab.on('console', lambda message: errors.append(message.text)
                   if message.type == 'error' else None)
            tab.on('response', lambda response: responses.append(
                (response.url, response.status,
                 response.header_value('content-type') or '')))
            tab.goto(f'http://127.0.0.1:{port}/')
            tab.wait_for_timeout(500)
            yield tab, errors, responses
            browser.close()
    finally:
        server.shutdown()


def _api(responses):
    return [row for row in responses if '/api/' in row[0]]


def test_EVERY_panel_opens_and_every_call_answers_JSON(panels):
    """One test for the whole class of bug.

    A 404 comes back as an HTML error page; the browser parses it as
    JSON and the panel shows `Unexpected token '<'`. Anything but a
    200 of JSON on an `/api/` call is that bug, whichever panel it is.
    """
    tab, errors, responses = panels

    # Opened through the app's OWN entry point rather than by hunting
    # for buttons: the `+` menu's items are hidden until it opens, and a
    # test that times out on an invisible element tells you nothing
    # about whether the panel works.
    tab.click('#open-settings')          # Exchanges
    tab.wait_for_timeout(700)
    for panel in ('grid', 'monitor'):
        tab.evaluate(
            "(name) => window.ArrowTrader.openPanel("
            "window.ArrowTrader.panelId(name))", panel)
        tab.wait_for_timeout(500)

    for name in ('positions', 'orders', 'fills', 'slippage', 'accounts',
                 'reconcile'):
        tab_button = tab.query_selector(f'.monitor .tabs button[data-tab="{name}"]')
        if tab_button:
            tab_button.click()
            tab.wait_for_timeout(350)

    bad = [row for row in _api(responses)
           if row[1] != 200 or 'json' not in row[2]]
    assert not bad, (
        'these calls did not answer 200 JSON — a panel parsing one of them '
        'shows "Unexpected token \'<\'" and names nothing: '
        + '; '.join(f'{url} -> {status} {kind}' for url, status, kind in bad))
    assert errors == [], errors


def test_the_panels_actually_CALLED_something(panels):
    """The control for the test above.

    If the clicks silently did nothing, every assertion up there passes
    over an empty list — green, and proving nothing. This is the same
    failure the line-count test had, and it must not come back in a
    different shape.
    """
    tab, _errors, responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    called = {row[0].split('/api/')[1].split('?')[0] for row in _api(responses)}
    # The Exchanges page alone must have asked for all five.
    for endpoint in ('account', 'segments', 'charges', 'settings', 'pairs'):
        assert endpoint in called, f'nothing requested /api/{endpoint}'


def test_the_exchanges_page_shows_ARROW_and_not_a_terminal(panels):
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    body = tab.query_selector('.window.settings').inner_text()
    assert 'Arrow session' in body
    assert 'used by nothing else' in body          # the declaration
    for gone in ('terminal64', 'Runner endpoint', 'Login'):
        assert gone not in body, f'the Exchanges page still shows "{gone}"'


def test_the_exchanges_page_is_actually_LAID_OUT(panels):
    """The styling gap, asserted.

    The rebuilt Exchanges page passed every test above while looking
    like a plain HTML form: the markup emitted new classes and
    `ladder.css` had not one rule for them, so every label sat inline
    with its input on one run-on line. Behaviour tests cannot see that
    — they read `inner_text`, and unstyled text reads the same.

    So this asserts the two things the layout is FOR:

      * the label sits ABOVE the control it names (a label beside its
        input drifts away from it as the column widens, and on this
        page typing a number into the wrong-looking box costs money);
      * the fields are on a grid, in a bordered box, not in the
        document's default inline flow.
    """
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)

    box = tab.evaluate("""() => {
      const field = document.querySelector('.settings-body .sfield');
      if (!field) { return {found: false}; }
      const label = field.querySelector(':scope > span');
      const input = field.querySelector(':scope > input, :scope > select');
      if (!label || !input) { return {found: false}; }
      const l = label.getBoundingClientRect();
      const i = input.getBoundingClientRect();
      const row = document.querySelector('.settings-body .field-row');
      const group = document.querySelector('.settings-body .session-box');
      return {
        found: true,
        labelBottom: l.bottom, inputTop: i.top,
        fieldWidth: field.getBoundingClientRect().width,
        inputWidth: i.width,
        rowDisplay: row ? getComputedStyle(row).display : null,
        boxBorder: group
          ? parseFloat(getComputedStyle(group).borderTopWidth) : 0
      };
    }""")

    assert box['found'], 'the Exchanges page rendered no .sfield at all'
    # Label ABOVE control, not beside it.
    assert box['labelBottom'] <= box['inputTop'] + 1, (
        'the label is on the same line as its input — .sfield has no CSS')
    # And the control fills the column it was given, rather than sitting
    # at the browser's default 20-character width with the next field
    # trailing off the same line.
    assert box['inputWidth'] > box['fieldWidth'] * 0.8, (
        'the control does not fill its field: '
        f"{box['inputWidth']} of {box['fieldWidth']}")
    assert box['rowDisplay'] == 'grid', (
        f"the field rows are not on a grid (display: {box['rowDisplay']})")
    assert box['boxBorder'] > 0, 'the session box has no border — no grouping'


def test_TYPING_a_contract_name_lists_contracts(panels):
    """The bug the operator reported, in one test.

    The search was wired to `change`, which fires on BLUR. They typed
    CRUDEOILSEP26 into the box, looked at the dropdown beside it, and
    it still said "search to list contracts" — because no event had
    fired yet and nothing had been asked of the master.

    And the fix has its own trap: the old handler answered by calling
    `render(true)`, which rewrites the whole Pairs section — including
    the input being typed into. On every keystroke the box would be
    replaced and the caret lost. So this asserts BOTH: that typing
    lists contracts, and that the box survives it.
    """
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    tab.click('.btn.new-pair')
    tab.wait_for_timeout(400)

    box = '.pair-leg[data-leg="a"] .p-search'
    tab.click(box)
    tab.type(box, 'GOLD', delay=40)
    # Longer than the debounce, and no blur anywhere.
    tab.wait_for_timeout(900)

    assert tab.evaluate(
        "() => document.activeElement === document.querySelector("
        f"'{box}')"), 'typing destroyed the search box — the caret is gone'
    assert tab.input_value(box) == 'GOLD', 'the typed text did not survive'

    options = tab.eval_on_selector_all(
        '.pair-leg[data-leg="a"] .p-symbol option',
        'nodes => nodes.map(n => n.value).filter(Boolean)')
    assert options, (
        'nothing was listed for GOLD — the contract dropdown still shows '
        'only its placeholder')


def test_an_EMPTY_dropdown_says_WHICH_of_the_four_reasons_it_is(panels):
    """The control, and the second half of the same bug.

    One sentence — "search to list contracts" — covered four states:
    nothing typed, a search in flight, no match in the master, and a
    search that failed because there is no Arrow session. A broken
    connection looked exactly like an empty search box.
    """
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    tab.click('.btn.new-pair')
    tab.wait_for_timeout(400)

    def placeholder():
        return tab.eval_on_selector(
            '.pair-leg[data-leg="a"] .p-symbol option', 'n => n.textContent')

    before = placeholder()
    assert 'type a name' in before, before

    tab.click('.pair-leg[data-leg="a"] .p-search')
    tab.type('.pair-leg[data-leg="a"] .p-search', 'ZZQQNOTHING', delay=20)
    tab.wait_for_timeout(900)
    after = placeholder()
    assert after != before, 'the dropdown says the same thing either way'
    assert 'ZZQQNOTHING' in after, after


def test_a_new_leg_starts_on_MCX_and_not_on_whatever_SORTS_first(panels):
    """The bug: the operator typed a commodity contract into a leg that
    was set to BSE cash, and found nothing.

    Nobody chose BSE cash. Flask sorts a dict's keys before writing
    JSON, so the segment table's own order — MCX first, because MCX is
    the product — reached the browser as bse_cm, bse_fo, mcx_fo,
    nse_cm, nse_fo. The picker selected the first entry it was handed.
    """
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    tab.click('.btn.new-pair')
    tab.wait_for_timeout(400)
    for leg in ('a', 'b'):
        assert tab.input_value(
            f'.pair-leg[data-leg="{leg}"] .p-segment') == 'mcx_fo'


def test_a_segment_that_is_NOT_ready_is_SHOWN_disabled_not_hidden(panels):
    """Two different problems, two different fixes.

    A segment that is simply absent from the dropdown reads as "this
    terminal does not do MCX". A segment listed and disabled reads as
    "this account is not enabled for it" — which is the true one, and
    the Segments table above says which of the two facts is missing.
    """
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    tab.click('.btn.new-pair')
    tab.wait_for_timeout(400)
    rows = tab.eval_on_selector_all(
        '.pair-leg[data-leg="a"] .p-segment option',
        'nodes => nodes.map(n => ({v: n.value, off: n.disabled, '
        't: n.textContent}))')
    keys = [row['v'] for row in rows]
    assert 'bse_cm' in keys, 'an unavailable segment vanished from the list'
    # THE ORDER IS THE SERVER'S, not the JSON's. Flask sorts a dict's
    # keys, so reading them back off the object gives bse_cm first;
    # `/api/segments` sends the table's own order alongside, and MCX is
    # first in it because MCX is the product.
    assert keys[0] == 'mcx_fo', (
        f'the segments are in the JSON\'s alphabetical order, not the '
        f'table\'s: {keys}')
    off = [row for row in rows if row['off']]
    assert off, 'nothing was disabled — every segment claims to be ready'
    assert all('not ready' in row['t'] for row in off)
    # ...and a disabled one is never the default.
    assert tab.input_value('.pair-leg[data-leg="a"] .p-segment') == 'mcx_fo'


def test_a_HANDLER_refusal_reaches_the_screen(panels):
    """The command RAN — so the envelope says ok — and the handler
    inside it said no.

    `no such pair`, `that order is already gone`, `nothing unclaimed on
    GOLD05DEC25F`: eight refusals across the engine, all shaped
    `{'ok': False, 'error': ...}` inside `result.data`, and nothing
    read `data.ok`. Every one was silent — the operator clicked, no
    toast appeared, and nothing on the screen changed either, which is
    indistinguishable from the click not registering at all.
    """
    tab, _errors, _responses = panels
    # Read the SCREEN, not a stubbed function: the module toasts
    # through its own closure, so a stub on the exported name would
    # pass while the operator still saw nothing.
    def outcome(payload):
        return tab.evaluate(
            """(payload) => {
              document.querySelectorAll('.toast').forEach(n => n.remove());
              window.ArrowTrader.toastOutcome(payload);
              return Array.from(document.querySelectorAll('.toast'))
                          .map(n => n.textContent);
            }""", payload)

    refused = outcome({'ok': True, 'data': {'ok': False,
                                            'error': 'no such pair'}})
    assert refused and 'no such pair' in refused[0]

    partial = outcome({'ok': True, 'data': {
        'ok': True, 'changed': ['rows'],
        'error': 'these fields were not applied: nope'}})
    assert partial and 'nope' in partial[0]

    # The control: an ordinary success does not put a refusal on screen.
    assert outcome({'ok': True, 'data': {'ok': True}}) == []


def test_PICKING_a_contract_reads_its_specs_and_shows_NO_refusal(panels):
    """"Unable to select the Leg A or Leg B", from a live screen.

    Two faults met there. `Read both legs from the instrument master`
    pressed before anything was chosen answers `leg A has no symbol` —
    correct, and it was then rendered as a red block under each leg and
    LEFT there while the operator searched, which reads as "this leg is
    broken" rather than "press the button again". And the specs the
    block would have carried are READ from the master, so choosing a
    contract is the moment to read them, not a second button press.
    """
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    tab.click('.btn.new-pair')
    tab.wait_for_timeout(400)

    def problems():
        return tab.eval_on_selector_all(
            '.pair-form .spec-problem', 'nodes => nodes.map(n => n.textContent)')

    # Nothing chosen: the form is blank, not refusing.
    assert problems() == []

    # Press the button with nothing picked — it says so, in a toast...
    tab.click('.btn.derive-pair')
    tab.wait_for_timeout(600)
    # ...and NOT as a red block under a leg the operator has not reached.
    assert problems() == [], (
        'a refusal about a leg with no contract was rendered under it')

    # Now pick one, and its specs arrive without a second press.
    box = '.pair-leg[data-leg="a"] .p-search'
    tab.click(box)
    tab.type(box, 'GOLD', delay=30)
    tab.wait_for_timeout(900)
    value = tab.eval_on_selector_all(
        '.pair-leg[data-leg="a"] .p-symbol option',
        'nodes => nodes.map(n => n.value).filter(Boolean)')
    assert value, 'nothing to select'
    tab.select_option('.pair-leg[data-leg="a"] .p-symbol', value[0])
    tab.wait_for_timeout(900)

    spec = tab.eval_on_selector_all(
        '.pair-leg[data-leg="a"] table.spec td',
        'nodes => nodes.map(n => n.textContent)')
    assert spec, 'choosing a contract read no specs from the master'
    assert any('100' in cell for cell in spec), spec       # the lot size
    # ...and leg B, still empty, is still not accused of anything.
    assert tab.eval_on_selector_all(
        '.pair-leg[data-leg="b"] .spec-problem',
        'nodes => nodes.map(n => n.textContent)') == []


def test_the_SEGMENTS_table_counts_the_CONTRACTS_it_found(panels):
    """The Contracts column was read from a key nothing wrote, so it
    was an em dash on every row — including the ready ones, where it is
    the fastest confirmation there is that the master really did arrive
    for the segment being asked about."""
    tab, _errors, _responses = panels
    tab.click('#open-settings')
    tab.wait_for_timeout(700)
    cells = tab.eval_on_selector_all(
        '.segments-table tbody tr td:nth-child(4)',
        'nodes => nodes.map(n => n.textContent.trim())')
    assert cells, 'no segments table'
    assert any(cell and cell[0].isdigit() for cell in cells), (
        f'every Contracts cell is empty: {cells}')
