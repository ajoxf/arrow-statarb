#!/usr/bin/env python3
"""Launch the Arrow StatArb web app (setup + dashboard + auto-trader).

    python run_arrow.py                       # waitress if installed, else dev server
    python run_arrow.py --server werkzeug      # force the Werkzeug dev server
    python run_arrow.py --port 8080
    python run_arrow.py --host 127.0.0.1 --debug   # dev server (auto-reload)

The Werkzeug dev server is single-process and allocates a large buffer per
request, so under the dashboard's polling it can exhaust memory on small/Windows
hosts. By default we serve via the robust multi-threaded waitress server when it
is installed (pip install waitress), and fall back to the dev server otherwise.
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
    p.add_argument("--server", choices=["auto", "werkzeug", "waitress"],
                   default=os.environ.get("ARROW_SERVER", "auto"),
                   help="WSGI server: 'auto' (waitress if installed, else dev), "
                        "'werkzeug' (dev), or 'waitress'")
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

    # Resolve which server to use. Default 'auto' prefers waitress (robust under
    # the dashboard's polling) but falls back to the dev server if it's missing.
    # --debug forces the dev server (waitress has no auto-reload).
    try:
        from waitress import serve as _waitress_serve
    except ImportError:
        _waitress_serve = None

    server = args.server
    if server == "waitress" and _waitress_serve is None:
        logger.error("waitress not installed — run: pip install waitress "
                     "(or use --server werkzeug)")
        return 1
    if server == "auto":
        if args.debug or _waitress_serve is None:
            server = "werkzeug"
        else:
            server = "waitress"

    if server == "waitress":
        # Flask-SocketIO (async_mode='threading') is plain WSGI, so waitress
        # serves both the HTTP API and the socket.io long-polling transport.
        logger.info("Serving via waitress on {}:{} (threads={})",
                    args.host, args.port, args.threads)
        _waitress_serve(app, host=args.host, port=args.port, threads=args.threads)
    else:
        if args.server == "auto" and _waitress_serve is None and not args.debug:
            logger.info("waitress not installed — using the Werkzeug dev server "
                        "(pip install waitress for a sturdier server)")
        why = " (--debug)" if (args.debug and args.server == "auto") else ""
        logger.info("Serving via Werkzeug dev server on {}:{}{}", args.host, args.port, why)
        socketio.run(app, host=args.host, port=args.port, debug=args.debug,
                     allow_unsafe_werkzeug=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
