"""One file. Start the terminal.

    python run_arrowtrader.py

It creates `config.json` and `.env` on a first run, brings the web UI
up FIRST — the credentials are entered on that screen, so it has to be
reachable before there are any — starts the engine, opens the browser,
and restarts a crashed child with backoff.

WHY THE WEB COMES UP FIRST, unchanged from MT5-Trader: the engine will
not start without an Arrow session, the session needs credentials, and
the credentials are typed into the browser. Starting the engine first
deadlocks a fresh install.

WHAT IS NEW: the engine SWEEPS BEFORE IT RECOVERS. On MT5 nothing at
the broker knew what a spread was, so no working order survived a
restart and there was nothing to sweep. A leg order rests at the
exchange and outlives us — and a spread order whose second leg is only
crossed while we are running is, after a restart, an outright waiting
to happen.
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def ensure_files(config_path, env_path):
    """A first run leaves a usable, EMPTY config — never a plausible one."""
    from arrowtrader import config as cfg
    if not os.path.exists(config_path):
        cfg.save_raw(config_path, {
            'account': {'name': 'arrow', 'app_id': None, 'user_id': None,
                        # FALSE, because the safe assumption is the one
                        # that claims less. Declaring it is a decision.
                        'dedicated': False},
            'pairs': {},
            'settings': {},
        })
        logging.info('wrote a fresh %s', config_path)
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as handle:
            handle.write(
                '# Credentials live HERE and nowhere else — never in\n'
                '# config.json, never in code, never in a log line.\n'
                '# The UI writes this file for you.\n'
                '#\n'
                '# ARROW_TOTP_SECRET is the base32 SEED from your\n'
                '# authenticator setup, NOT the 6-digit code.\n'
                'ARROW_APP_ID=""\n'
                'ARROW_USER_ID=""\n'
                'ARROW_PASSWORD=""\n'
                'ARROW_API_SECRET=""\n'
                'ARROW_TOTP_SECRET=""\n')
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        logging.info('wrote a fresh %s', env_path)


def run_web(args):
    from arrowtrader.webapp import create_app
    app = create_app(status_path=args.status, command_path=args.commands,
                     results_path=args.results, config_path=args.config,
                     db_path=args.db, env_path=args.env)
    app.run(host=args.host, port=args.port, threaded=True,
            use_reloader=False)


def run_engine(args, stop):
    """Connect, sweep, recover, then poll — and drain clicks on their
    OWN thread.

    The click thread is separate because it is the latency the trader
    feels: waiting for the next 300ms poll would put a whole interval
    between the click and the order, on a product whose promise is that
    one click is one order.
    """
    from arrowtrader.broker import ArrowSession
    from arrowtrader.commands import CommandRunner
    from arrowtrader.config import TraderConfig
    from arrowtrader.coordinator import Coordinator
    from arrowtrader.database import Store
    from arrowtrader.legs import make_legs
    from arrowtrader.segments import SegmentTable

    config = TraderConfig.from_file(args.config)
    if not config.account.app_id or config.account.missing_secrets():
        logging.warning('the Arrow session is not configured yet — open the '
                        'Exchanges page and enter the credentials. The engine '
                        'will wait.')
        return False

    # STARTING UP IS ITS OWN FAILURE MODE, and it must not take the web
    # server with it. A bad database path, a read-only directory, a
    # broker that will not log in: every one of those used to raise out
    # of here, out of `main`, and end the process — killing the daemon
    # web thread, which is the ONLY place the operator could have read
    # what went wrong. What they got was a traceback in a terminal they
    # may not be looking at and no screen at all.
    try:
        session = ArrowSession(config.account, SegmentTable(
            config.get('SEGMENTS_EXTRA')))
        legs = make_legs([config.account.name], session)
        store = Store(args.db)
        engine = Coordinator(config, legs, status_path=args.status,
                             store=store)
        engine.start()
    except Exception as error:                          # noqa: BLE001
        logging.exception('the engine could not start: %s', error)
        return False

    runner = CommandRunner(engine, args.commands, args.results)
    # PRIMED BEFORE THE FIRST DRAIN. Everything already in the file
    # belongs to a process that is gone: replaying "place this order"
    # is placing another order.
    runner.prime()

    def drain():
        interval = float(config.get('COMMAND_POLL_SEC', 0.02))
        while not stop.is_set():
            try:
                runner.drain()
            except Exception as error:                  # noqa: BLE001
                logging.exception('command drain failed: %s', error)
            time.sleep(interval)

    threading.Thread(target=drain, daemon=True).start()
    interval = float(config.get('POLL_INTERVAL_SEC', 0.3))
    reconcile_every = float(config.get('RECONCILE_INTERVAL_SEC', 20.0))
    last_reconcile = 0.0
    try:
        while not stop.is_set():
            try:
                engine.poll_once()
                engine.run_session_cutoff()
                if time.time() - last_reconcile > reconcile_every:
                    engine.reconcile_if_due()
                    # The broker's own trade book, into the journal.
                    # Every charge figure in every report is read back
                    # from it, so a journal nobody writes is a cost
                    # model with no input.
                    engine.journal()
                    last_reconcile = time.time()
                engine.publish()
            except Exception as error:                  # noqa: BLE001
                logging.exception('poll failed: %s', error)
            time.sleep(interval)
    finally:
        # SWEEP AT SHUTDOWN. A resting leg order outlives this process
        # and its crossing leg does not.
        engine.stop()
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--env', default='.env')
    parser.add_argument('--db', default='arrowtrader.db')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--web-only', action='store_true')
    parser.add_argument('--engine-only', action='store_true')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')
    # Read `.env` OURSELVES rather than through an optional package.
    # Depending on python-dotenv meant that running from outside the
    # virtualenv skipped the file in silence, and every credential came
    # back "not set" with `.env` sitting right there holding all of
    # them. A key already in the environment still wins.
    from arrowtrader.config import load_env
    loaded = load_env(args.env)
    logging.info('%s: %d value(s) read', args.env, len(loaded))

    ensure_files(args.config, args.env)
    stop = threading.Event()

    if args.engine_only:
        return 0 if run_engine(args, stop) else 1

    # THE WEB FIRST. The credentials are entered on that screen.
    threading.Thread(target=run_web, args=(args,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(
            f'http://{args.host}:{args.port}/')).start()
    if args.web_only:
        while not stop.is_set():
            time.sleep(0.5)
        return 0

    backoff = 1.0
    try:
        while not stop.is_set():
            started = time.time()
            if run_engine(args, stop):
                backoff = 1.0
            if stop.is_set():
                break
            # Backoff, so a misconfigured session does not spin.
            backoff = 1.0 if time.time() - started > 30 else min(backoff * 2,
                                                                 30.0)
            logging.info('the engine is not running; retrying in %.0fs',
                         backoff)
            time.sleep(backoff)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == '__main__':
    sys.exit(main())
