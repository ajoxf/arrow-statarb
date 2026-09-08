"""The web process: it renders, and it asks. It never trades.

Ported from MT5-Trader's `webapp.py`. The browser talks to this Flask
app; the orders are placed by the engine, which is the only process
holding the Arrow session. Everything the UI shows comes from the
engine's status snapshot, and everything it does goes out as a command
through `commands.py` — executed once and never replayed.

Two rules from the original shape the endpoints here:

- **Setup must work with the ENGINE DOWN.** Otherwise the system
  deadlocks: the engine will not start until the credentials and the
  contracts are right, and these are the tools for getting them right.
  So the setup endpoints open their OWN short-lived Arrow session
  rather than asking the engine.
- **Never send the operator to a log for a decision already made.** A
  refusal carries the broker's — or the config's — own words, in the
  response body, for the panel to print.

And one that is new, because the credentials are:

- **A secret goes IN and never comes OUT.** The account endpoints
  accept a password, an API secret and a TOTP seed, write them to
  `.env`, and return only WHETHER each key is now set. Nothing here
  renders a secret, echoes one back, or puts one in a log line.
"""

import json
import logging
import os
import time

from flask import Flask, jsonify, render_template, request

from . import config as cfg, costs, instruments as instr, segments
from .commands import CommandLog

#: How old the status file may be before the UI says the engine is not
#: running. Six polls at the default 0.3s: long enough not to flicker,
#: short enough that a dead engine is not mistaken for a quiet market —
#: which is the one confusion that gets orders clicked into a screen
#: with nothing behind it.
STATUS_STALE_SEC = 2.0


def create_app(status_path='status.json', command_path='commands.jsonl',
               results_path='results.json', config_path='config.json',
               db_path='arrowtrader.db', env_path='.env', session_factory=None):
    app = Flask(__name__)
    # The TEMPLATE is compiled once and cached for the life of the
    # process unless this is on. A `git pull` therefore updated the CSS
    # and the JS — fetched by URL with a stamp — while the HTML stayed
    # on the version the process started with: new handlers, old
    # markup, and a screen that looks like the pull did not land.
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True
    commands = CommandLog(command_path)
    setup = _Setup(config_path, session_factory)

    def read_json(path, default=None):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def status():
        snapshot = read_json(status_path)
        if not snapshot:
            return {'engine': 'down', 'pairs': {},
                    'engine_note': 'the engine is not running — nothing is '
                                   'watching the market and no click will '
                                   'reach the exchange'}
        age = time.time() - float(snapshot.get('at') or 0)
        if age > STATUS_STALE_SEC:
            snapshot['engine'] = 'stale'
            snapshot['engine_note'] = (
                f'the engine last answered {age:.1f}s ago — a dead engine is '
                f'not a quiet market, and a click now would reach nothing')
        else:
            snapshot['engine'] = 'up'
            snapshot['engine_note'] = None
        return snapshot

    def asset_version():
        stamps = []
        for name in ('static/app.js', 'static/settings.js',
                     'static/ladder.css'):
            try:
                stamps.append(int(os.path.getmtime(
                    os.path.join(os.path.dirname(__file__), name))))
            except OSError:
                pass
        return max(stamps) if stamps else int(time.time())

    # -- the screen ---------------------------------------------------------

    @app.get('/')
    def index():
        response = app.make_response(
            render_template('index.html', asset_version=asset_version()))
        # The PAGE is never cached: it carries the stamp that tells the
        # browser whether its cached CSS and JS are current.
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/status')
    def api_status():
        return jsonify(status())

    @app.get('/api/config')
    def api_config():
        raw = cfg.load_raw(config_path)
        # Secrets are NAMES here, never values — and this endpoint is
        # what proves it: whatever is in the file is what the browser
        # sees.
        return jsonify(raw)

    # -- what a click does ---------------------------------------------------

    @app.post('/api/command')
    def api_command():
        payload = request.get_json(silent=True) or {}
        kind = payload.get('kind')
        if not kind:
            return jsonify({'ok': False,
                            'error': 'a command needs a kind'}), 400
        snapshot = status()
        if snapshot.get('engine') != 'up' and kind not in ('set_pair',):
            # Refuse rather than queue: a command written while nothing
            # is running would be executed by whatever starts next, at
            # prices from another hour.
            return jsonify({'ok': False,
                            'error': snapshot['engine_note']}), 409
        return jsonify({'ok': True,
                        'id': commands.submit(kind, payload.get('payload'))})

    @app.get('/api/result/<command_id>')
    def api_result(command_id):
        results = read_json(results_path, {}) or {}
        found = results.get(command_id)
        if found is None:
            return jsonify({'ok': None, 'pending': True})
        return jsonify(found)

    # -- settings -------------------------------------------------------------

    @app.get('/api/settings')
    def api_settings():
        raw = cfg.load_raw(config_path)
        merged = dict(cfg.DEFAULT_SETTINGS)
        merged.update(raw.get('settings') or {})
        from .commands import CommandRunner
        return jsonify({'settings': merged,
                        'defaults': cfg.DEFAULT_SETTINGS,
                        'hot': sorted(CommandRunner.HOT_SETTINGS),
                        'structural': list(cfg.STRUCTURAL_SETTINGS)})

    @app.post('/api/settings')
    def api_save_settings():
        payload = request.get_json(silent=True) or {}
        fields = payload.get('fields') or {}
        unknown = [name for name in fields
                   if name not in cfg.DEFAULT_SETTINGS]
        if unknown:
            return jsonify({'ok': False,
                            'error': 'not a setting: '
                                     + ', '.join(unknown)}), 400
        raw = cfg.load_raw(config_path)
        raw.setdefault('settings', {}).update(fields)
        cfg.save_raw(config_path, raw)
        from .commands import CommandRunner
        hot = {name: value for name, value in fields.items()
               if name in CommandRunner.HOT_SETTINGS}
        if hot and status().get('engine') == 'up':
            commands.submit('set_setting', {'fields': hot})
        cold = [name for name in fields if name not in hot]
        return jsonify({'ok': True, 'applied_now': sorted(hot),
                        'restart_required': sorted(cold)})

    # -- the charge schedule --------------------------------------------------

    @app.get('/api/charges')
    def api_charges():
        """The Indian charge stack, and whether anybody has set it.

        `configured` is the field that matters. Every rate defaults to
        zero, so an unconfigured desk sees costs of 0.00 everywhere —
        and a zero cost is indistinguishable from a free trade unless
        the screen says which it is.
        """
        raw = cfg.load_raw(config_path)
        settings = dict(cfg.DEFAULT_SETTINGS)
        settings.update(raw.get('settings') or {})
        out = {}
        for segment in segments.SegmentTable():
            rates = costs.schedule_for(settings, segment.key)
            out[segment.key] = {
                'label': segment.label, 'rates': rates,
                'configured': costs.is_configured(rates),
                'note': (None if costs.is_configured(rates) else
                         f'no charges are configured for {segment.label}, so '
                         f'every cost figure on that ladder reads 0.00. That '
                         f'is not a free trade — it is an unfilled form. Take '
                         f'the rates from your Arrow contract note.'),
            }
        return jsonify(out)

    # -- the Arrow connection -------------------------------------------------

    @app.get('/api/account')
    def api_account():
        """Everything about the session EXCEPT the secrets."""
        raw = cfg.load_raw(config_path)
        account = cfg.AccountConfig.from_dict(
            (raw.get('account') or {}).get('name', 'arrow'),
            raw.get('account'))
        return jsonify({
            'name': account.name, 'app_id': account.app_id,
            'user_id': account.user_id, 'dedicated': account.dedicated,
            # WHETHER, never WHAT.
            'secrets': cfg.secrets_present(),
            'missing': account.missing_secrets(),
        })

    @app.post('/api/account')
    def api_save_account():
        """Save the session's identity, and write the secrets to `.env`.

        A secret goes IN and never comes OUT: it is written under its
        env key and the response says only whether the key is now set.
        """
        payload = request.get_json(silent=True) or {}
        raw = cfg.load_raw(config_path)
        account = dict(raw.get('account') or {})
        account['name'] = account.get('name') or 'arrow'
        for field in ('app_id', 'user_id'):
            if field in payload:
                account[field] = payload[field]
        if 'dedicated' in payload:
            # THE DECLARATION EVERY RECONCILER FINDING DEPENDS ON.
            # Nothing at the exchange can confirm it, so it is stored
            # as what it is: the operator's word.
            account['dedicated'] = bool(payload['dedicated'])
        raw['account'] = account
        cfg.save_raw(config_path, raw)
        for field, key in cfg.AccountConfig.SECRETS.items():
            if payload.get(field):
                cfg.write_env_value(env_path, key, payload[field])
        return jsonify({'ok': True, 'secrets': cfg.secrets_present(),
                        'restart_required': True,
                        'note': ('the engine reads the session at startup, '
                                 'so it restarts to pick this up')})

    @app.post('/api/connect')
    def api_connect():
        """Can we log in AT ALL? Works with the engine down.

        Every failure carries the step that fixes it — including the
        one that will actually happen: this host's IP is not registered
        with Arrow.
        """
        built, error = setup.session()
        if built is None:
            return jsonify({'ok': False, 'error': error}), 200
        return jsonify({
            'ok': True,
            'master_rows': built.master.rows,
            'segments': sorted(built.master.exch_segs),
            'unknown_segments': dict(built.master.unknown_exch_segs),
        })

    @app.get('/api/segments')
    def api_segments():
        """Which segments are ACTUALLY usable, and why each other is not.

        BLOCKER 3.1 LIVES ON THIS ENDPOINT. Two independent facts have
        to line up — the account must be entitled to the segment, and
        the SDK must carry an Exchange value for it — and they fail
        with the same symptom and different fixes.
        """
        built, error = setup.session()
        table = segments.SegmentTable()
        if built is None:
            return jsonify({'ok': False, 'error': error,
                            'segments': {}}), 200
        return jsonify({'ok': True, 'segments': segments.available_segments(
            table, built.master.exch_segs, built.sdk_exchanges())})

    @app.get('/api/find')
    def api_find():
        """The instrument picker. Works with the engine down."""
        built, error = setup.session()
        if built is None:
            return jsonify({'ok': False, 'error': error, 'symbols': []}), 200
        found = built.find_symbols(request.args.get('q', ''),
                                   limit=int(request.args.get('limit', 40)),
                                   segment=request.args.get('segment'))
        return jsonify({'ok': True, 'symbols': found or []})

    @app.get('/api/contract/<path:symbol>')
    def api_contract(symbol):
        """One contract's specs, with the derivation beside each number."""
        built, error = setup.session()
        if built is None:
            return jsonify({'ok': False, 'error': error}), 200
        return jsonify(built.symbol_report(symbol))

    # -- pairs -----------------------------------------------------------------

    @app.get('/api/pairs')
    def api_pairs():
        return jsonify(cfg.load_raw(config_path).get('pairs') or {})

    @app.post('/api/pair/derive')
    def api_derive_pair():
        """Read both legs from the instrument master.

        Every number comes back WITH ITS DERIVATION, and is offered as
        a one-click correction rather than applied silently.
        """
        payload = request.get_json(silent=True) or {}
        built, error = setup.session()
        if built is None:
            return jsonify({'ok': False, 'error': error}), 200
        out = {'ok': True, 'legs': {}, 'problems': []}
        metas = {}
        for leg in ('a', 'b'):
            symbol = (payload.get(f'symbol_{leg}') or '').strip()
            report = built.symbol_report(symbol) if symbol else {
                'found': False, 'error': f'leg {leg.upper()} has no symbol'}
            out['legs'][leg] = report
            metas[leg] = report
            if not report.get('found'):
                out['problems'].append(report.get('error'))
            else:
                out['problems'].extend(report.get('problems') or [])
        if all(metas[leg].get('found') for leg in ('a', 'b')):
            tick_a = metas['a'].get('tick_size')
            tick_b = metas['b'].get('tick_size')
            beta = float(payload.get('hedge_ratio') or 1.0)
            out['increment'] = (max(tick_b, beta * tick_a)
                                if tick_a and tick_b else None)
            out['increment_note'] = (
                f'max(tick B {tick_b}, beta {beta:g} x tick A {tick_a})'
                if tick_a and tick_b else
                'neither leg publishes a tick size, so the increment cannot '
                'be derived — set it on the pair')
            out['hedge_ratio'] = beta
            out['hedge_ratio_for'] = cfg.pair_key(
                payload.get('symbol_a'), payload.get('symbol_b'))
            out['lot_note'] = (
                f'one lot is {metas["a"].get("lot_size")} units of leg A and '
                f'{metas["b"].get("lot_size")} of leg B — the order carries '
                f'UNITS, which is lots x LotSize')
        return jsonify(out)

    @app.post('/api/pair/<path:key>')
    def api_save_pair(key):
        payload = request.get_json(silent=True) or {}
        raw = cfg.load_raw(config_path)
        pairs = dict(raw.get('pairs') or {})
        pairs[key] = {**(pairs.get(key) or {}), **payload}
        raw['pairs'] = pairs
        cfg.save_raw(config_path, raw)
        return jsonify({'ok': True, 'key': key})

    @app.delete('/api/pair/<path:key>')
    def api_delete_pair(key):
        raw = cfg.load_raw(config_path)
        pairs = dict(raw.get('pairs') or {})
        if key not in pairs:
            return jsonify({'ok': False, 'error': 'no such pair'}), 404
        pairs.pop(key)
        raw['pairs'] = pairs
        # `allow_shrink`: this endpoint legitimately removes something.
        cfg.save_raw(config_path, raw, allow_shrink=True)
        return jsonify({'ok': True})

    @app.get('/api/roll/<path:symbol>')
    def api_roll(symbol):
        """The contracts AFTER this one on the same underlying.

        An MCX calendar has to be re-pointed every month or two, and
        doing that by hand through New Pair is how the wrong contract
        gets traded.
        """
        built, error = setup.session()
        if built is None:
            return jsonify({'ok': False, 'error': error, 'next': []}), 200
        return jsonify({'ok': True,
                        'next': [contract.to_dict() for contract
                                 in built.master.next_contracts(symbol, 3)]})

    # -- the journal -------------------------------------------------------------

    @app.get('/api/fills')
    def api_fills():
        store = _store(db_path)
        if store is None:
            return jsonify({'ok': False,
                            'error': 'no database yet'}), 200
        rows = store.fills(pair_key=request.args.get('pair'),
                           ours_only=request.args.get('ours') == '1')
        return jsonify({'ok': True, 'fills': rows,
                        # HOW ownership was decided, per row, because
                        # here it is an inference and not a fact.
                        'unattributed': len(store.unattributed_fills())})

    @app.get('/api/events')
    def api_events():
        store = _store(db_path)
        if store is None:
            return jsonify({'ok': False, 'events': []}), 200
        return jsonify({'ok': True,
                        'events': store.events(request.args.get('kind'))})

    return app


class _Setup:
    """A short-lived Arrow session for the setup endpoints.

    Held apart from the engine deliberately: symbol setup must work
    with the engine DOWN, or the system deadlocks — the engine will not
    start until the contracts are right and these are the tools for
    getting them right.
    """

    def __init__(self, config_path, factory=None):
        self.config_path = config_path
        self.factory = factory
        self._session = None

    def session(self):
        if self._session is not None and self._session.connected:
            return self._session, None
        raw = cfg.load_raw(self.config_path)
        account = cfg.AccountConfig.from_dict(
            (raw.get('account') or {}).get('name', 'arrow'),
            raw.get('account'))
        missing = account.missing_secrets()
        if missing:
            return None, ('these credentials are not set: '
                          + ', '.join(missing)
                          + '. Enter them on this page — they are written to '
                            '.env and never to config.json.')
        if self.factory is not None:
            built = self.factory(account)
        else:
            from .broker import ArrowSession
            built = ArrowSession(account, segments.SegmentTable(
                (raw.get('settings') or {}).get('SEGMENTS_EXTRA')))
        if not built.initialize():
            return None, built.last_error
        self._session = built
        return built, None


def _store(db_path):
    try:
        from .database import Store
        if not os.path.exists(db_path):
            return None
        return Store(db_path)
    except Exception as error:                      # noqa: BLE001
        logging.warning('database unavailable: %s', error)
        return None
