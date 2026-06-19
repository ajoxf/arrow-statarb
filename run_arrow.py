#!/usr/bin/env python3
"""Launch the Arrow StatArb web app (setup + dashboard + auto-trader).

    python run_arrow.py                       # Werkzeug dev server, 0.0.0.0:5000
    python run_arrow.py --server waitress     # production WSGI server (recommended)
    python run_arrow.py --port 8080
    python run_arrow.py --host 127.0.0.1 --debug

The Werkzeug dev server is single-process and allocates a large buffer per
request, so under the dashboard's polling it can exhaust memory on small/Windows
hosts. ``--server waitress`` serves the same app via a robust multi-threaded WSGI
server (pip install waitress).
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from arrow_statarb.logger import setup_logging
from arrow_statarb.config.config import Config
from arrow_statarb.web.app import create_app


def main() -> int:
    load_dotenv()  # pull ARROW_* creds from a local .env if present
    p = argparse.ArgumentParser(description="Arrow StatArb web app")
    p.add_argument("--host", default=os.environ.get("ARROW_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("ARROW_PORT", 5000)))
    p.add_argument("--debug", action="store_true")
    p.add_argument("--server", choices=["werkzeug", "waitress"],
                   default=os.environ.get("ARROW_SERVER", "werkzeug"),
                   help="WSGI server: 'werkzeug' (dev) or 'waitress' (recommended)")
    p.add_argument("--threads", type=int, default=int(os.environ.get("ARROW_THREADS", 8)),
                   help="Worker threads for the waitress server")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = p.parse_args()

    setup_logging(level=args.log_level)

    cfg = Config()
    from loguru import logger
    logger.info("Arrow StatArb starting — broker={}, mode={}", cfg.get("broker.name", "arrow"), cfg.mode)
    if not cfg.is_dry_run:
        logger.warning("⚠ LIVE MODE — real orders will be transmitted to Arrow.")

    app, socketio = create_app(cfg)

    if args.server == "waitress":
        try:
            from waitress import serve
        except ImportError:
            logger.error("waitress not installed — run: pip install waitress "
                         "(or omit --server waitress to use the dev server)")
            return 1
        # Flask-SocketIO (async_mode='threading') is plain WSGI, so waitress
        # serves both the HTTP API and the socket.io long-polling transport.
        logger.info("Serving via waitress on {}:{} (threads={})",
                    args.host, args.port, args.threads)
        serve(app, host=args.host, port=args.port, threads=args.threads)
    else:
        logger.info("Serving via Werkzeug dev server on {}:{}", args.host, args.port)
        socketio.run(app, host=args.host, port=args.port, debug=args.debug,
                     allow_unsafe_werkzeug=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
