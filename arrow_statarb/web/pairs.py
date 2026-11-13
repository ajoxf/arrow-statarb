"""More than one spread — Mirror-MT5's "every pair is its own ladder".

Each pair is a complete engine (signal, algo, executor, journals, studies)
built by ``create_app`` with a folder of its own, and every pair is served
under ``/p/<key>/``. They share the ONE thing that is really one: the Arrow
session (and its positions/funds cache). The main pair keeps the original
files — config/settings.yaml, config/leg_assignments.yaml, data/… — so a
desk that had one pair before has exactly the same pair, history and all.

The one clash refused outright: two pairs on the SAME contract. A pair reads
Arrow's position on its own two legs to decide whether it is flat, closes
against it and reconciles against it; two pairs reading one leg would each
see the other's lots as their own.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from typing import Any, Dict, List, Optional, Tuple

import yaml
from flask import Flask, jsonify, request
from loguru import logger
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from arrow_statarb.config.config import CONFIG_DIR, SETTINGS_FILE, Config

PAIRS_FILE = CONFIG_DIR / "pairs.yaml"
MAIN = "main"
MAX_PAIRS = 12


# ── the registry ─────────────────────────────────────────────────────────────

def load_pairs(path=None) -> List[Dict[str, Any]]:
    """The extra pairs, in the order they were added. The main pair is not
    listed: it always exists."""
    p = path or PAIRS_FILE
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception as exc:                       # noqa: BLE001
        logger.error("pairs.yaml unreadable — {} (no extra pairs loaded)", exc)
        return []
    out = []
    for e in data.get("pairs") or []:
        if isinstance(e, dict) and e.get("key") and e.get("key") != MAIN:
            out.append({"key": str(e["key"]), "name": str(e.get("name") or e["key"]),
                        "enabled": bool(e.get("enabled", True))})
    return out


def save_pairs(pairs: List[Dict[str, Any]], path=None) -> None:
    p = path or PAIRS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump({"pairs": pairs}, sort_keys=False))
    os.replace(tmp, p)


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return (s or "pair")[:32]


def read_legs_file(path) -> Dict[str, str]:
    """{leg_a: SYMBOL, leg_b: SYMBOL} from a leg_assignments.yaml (mapping_id
    is "segment|SYMBOL")."""
    out: Dict[str, str] = {}
    data: Dict[str, Any] = {}
    try:
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
    except Exception:                               # noqa: BLE001
        data = {}
    for lk in ("leg_a", "leg_b"):
        mid = str((data.get(lk) or {}).get("mapping_id") or "")
        if "|" in mid:
            out[lk] = mid.split("|", 1)[1].strip().upper()
    return out


# ── the desk: every pair, mounted ────────────────────────────────────────────

def create_desk_app(config: Optional[Config] = None) -> Tuple[Flask, Any]:
    """The main pair's app, with every enabled extra pair mounted at
    /p/<key>/ and the main pair also reachable at /p/main/ — so the desk can
    address every pair the same way."""
    from arrow_statarb.web import app as appmod

    cfg = config or Config()
    main_app, socketio = appmod.create_app(cfg)
    active = main_app.extensions["arrow"]["active"]
    lock = threading.RLock()
    apps: Dict[str, Flask] = {MAIN: main_app}
    names: Dict[str, str] = {MAIN: ""}
    mounts: Dict[str, Any] = {}

    def legs_of(key: str) -> Dict[str, str]:
        if key == MAIN:
            return read_legs_file(appmod.LEG_ASSIGNMENTS_FILE)
        return read_legs_file(appmod.pair_paths(key)["legs"])

    def clash(key: str, symbols: List[str]) -> Optional[str]:
        """The pair already using one of these contracts, in words — or None."""
        want = {s.upper() for s in symbols if s}
        for other in list(apps) + [p["key"] for p in load_pairs() if p["key"] not in apps]:
            if other == key:
                continue
            used = set(legs_of(other).values()) & want
            if used:
                nm = names.get(other) or other
                return (f"{', '.join(sorted(used))} is already a leg of the pair "
                        f"'{nm or 'main'}'. Two pairs on one contract would each read the "
                        f"other's position as their own — pick another contract.")
        return None

    def other_symbols(key: str) -> set:
        out = set()
        for other in apps:
            if other != key:
                out |= set(legs_of(other).values())
        return out

    def wire(key: str, app: Flask) -> None:
        app.config["LEG_CLASH"] = lambda syms, _k=key: clash(_k, syms)
        app.config["OTHER_PAIR_SYMBOLS"] = lambda _k=key: other_symbols(_k)
        app.config["PAIR_KEY"] = key

    def build(key: str) -> Flask:
        pp = appmod.pair_paths(key)
        if not pp["settings"].exists():
            # A new pair starts from the main pair's settings — the same
            # signal, algo and cost rules — and is tuned from there. The
            # algo is OFF in the copy whatever the main pair has.
            pp["settings"].parent.mkdir(parents=True, exist_ok=True)
            src = cfg.path if cfg.path.exists() else SETTINGS_FILE
            if src.exists():
                shutil.copyfile(src, pp["settings"])
                _forget_the_contract(pp["settings"])
        pcfg = Config(pp["settings"])
        sub, _ = appmod.create_app(pcfg, pair_key=key, shared_active=active)
        wire(key, sub)
        return sub

    wire(MAIN, main_app)
    for p in load_pairs():
        if not p["enabled"]:
            continue
        try:
            apps[p["key"]] = build(p["key"])
            names[p["key"]] = p["name"]
        except Exception as exc:                   # noqa: BLE001
            logger.error("Pair '{}' did not start — {}", p["key"], exc)

    base = main_app.wsgi_app
    mounts["/p/" + MAIN] = base
    for k, a in apps.items():
        if k != MAIN:
            mounts["/p/" + k] = a.wsgi_app
    dispatcher = DispatcherMiddleware(base, mounts)
    main_app.wsgi_app = dispatcher

    def listing() -> List[Dict[str, Any]]:
        main_name = (cfg.get("pairs.name") or "") if hasattr(cfg, "get") else ""
        rows = [{"key": MAIN, "name": main_name or "", "enabled": True, "base": "/p/" + MAIN,
                 "running": True, "legs": legs_of(MAIN), "main": True}]
        for p in load_pairs():
            rows.append({"key": p["key"], "name": p["name"], "enabled": p["enabled"],
                         "base": "/p/" + p["key"], "running": p["key"] in apps,
                         "legs": legs_of(p["key"]), "main": False})
        return rows

    @main_app.route("/api/pairs", methods=["GET"])
    def api_pairs():
        return jsonify({"pairs": listing()})

    @main_app.route("/api/pairs", methods=["POST"])
    def api_pairs_add():
        """A new pair: {name}. Its legs are picked on its own Setup page
        (/p/<key>/), where the same contract can never be picked twice."""
        d = request.get_json(silent=True) or {}
        name = str(d.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "Give the pair a name (e.g. Gold Dec−Feb)."})
        with lock:
            pairs = load_pairs()
            if len(pairs) + 1 >= MAX_PAIRS:
                return jsonify({"success": False, "error": f"At most {MAX_PAIRS} pairs."})
            key, n = slug(name), 2
            taken = {p["key"] for p in pairs} | {MAIN}
            while key in taken:
                key, n = f"{slug(name)}-{n}", n + 1
            try:
                apps[key] = build(key)
            except Exception as exc:               # noqa: BLE001
                return jsonify({"success": False, "error": f"The pair did not start: {exc}"})
            names[key] = name
            mounts["/p/" + key] = apps[key].wsgi_app
            pairs.append({"key": key, "name": name, "enabled": True})
            save_pairs(pairs)
        logger.info("Pair added: {} ({})", name, key)
        return jsonify({"success": True, "pair": {"key": key, "name": name, "base": "/p/" + key}})

    @main_app.route("/api/pairs/<key>", methods=["POST"])
    def api_pairs_update(key):
        """Rename, or switch a pair off / on. Switching OFF is refused while
        the pair has a position or its algo is on — nothing is left running
        with no ladder to see it on."""
        d = request.get_json(silent=True) or {}
        with lock:
            pairs = load_pairs()
            row = next((p for p in pairs if p["key"] == key), None)
            if row is None:
                return jsonify({"success": False, "error": "No such pair (the main pair cannot be switched off)."})
            if "name" in d and str(d["name"]).strip():
                row["name"] = names[key] = str(d["name"]).strip()
            if "enabled" in d and bool(d["enabled"]) != row["enabled"]:
                if not d["enabled"]:
                    a = apps.get(key)
                    if a is not None:
                        why = _busy(a)
                        if why:
                            return jsonify({"success": False, "error": why})
                        _stop(a)
                        apps.pop(key, None)
                        mounts.pop("/p/" + key, None)
                    row["enabled"] = False
                else:
                    try:
                        apps[key] = build(key)
                    except Exception as exc:       # noqa: BLE001
                        return jsonify({"success": False, "error": f"The pair did not start: {exc}"})
                    mounts["/p/" + key] = apps[key].wsgi_app
                    row["enabled"] = True
            save_pairs(pairs)
        return jsonify({"success": True, "pairs": listing()})

    main_app.extensions["arrow"]["pairs"] = {"apps": apps, "listing": listing, "clash": clash}
    return main_app, socketio


#: Settings that describe the MAIN pair's contracts, not a rule: a new pair
#: must not inherit Crude's ₹10/point when it trades Copper at ₹2,500/point.
#: Reset to "auto" — the contract spec, the broker's lot, Arrow's margin.
CONTRACT_KEYS = {("pairs", "multiplier_a"): 0, ("pairs", "multiplier_b"): 0,
                 ("signal", "hedge_ratio"): 1.0, ("risk", "capital_at_risk_inr"): 0}


def _forget_the_contract(path) -> None:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:                               # noqa: BLE001
        return
    for (sec, key), val in CONTRACT_KEYS.items():
        if isinstance(data.get(sec), dict) and key in data[sec]:
            data[sec][key] = val
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    os.replace(tmp, path)


def _busy(app: Flask) -> Optional[str]:
    ext = app.extensions.get("arrow") or {}
    algo = ext.get("algo")
    if algo is not None and getattr(algo, "running", False):
        return "Its algo is ON — turn it off first."
    tl = ext.get("trade_log")
    if tl is not None and tl.open_position():
        return "It has an open position — close it first."
    wo = ext.get("working_orders")
    if wo is not None and wo.active():
        return "It has working orders — cancel them first."
    return None


def _stop(app: Flask) -> None:
    ext = app.extensions.get("arrow") or {}
    for k in ("signal", "algo", "working_orders"):
        obj = ext.get(k)
        if obj is not None and hasattr(obj, "stop"):
            try:
                obj.stop()
            except Exception:                      # noqa: BLE001
                pass
