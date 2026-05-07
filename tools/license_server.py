#!/usr/bin/env python3
# ============================================================
# license_server.py  —  reference activation server
# ============================================================
# Tiny Flask app implementing the activation protocol the desktop
# client speaks. SQLite storage, stateless container-friendly design.
#
# Endpoints:
#   POST /activate    {license, fingerprint}
#       → {ok, activation, device_index, max_devices}      # success
#       → {ok: false, reason, max_devices, current_devices} # failure
#
#   POST /deactivate  {license, fingerprint}
#       → {ok}        # idempotent: missing slot is also "ok"
#
#   GET /health
#       → {ok: true, db: ok, version: ...}
#
# Deploy options (any of these work):
#   • $5 VPS    — gunicorn license_server:app --bind 0.0.0.0:8080
#   • Docker    — pip install flask gunicorn; CMD gunicorn ...
#   • Heroku    — Procfile: web: gunicorn tools.license_server:app
#   • Cloudflare Workers / AWS Lambda — port to FastAPI / Bottle
#                                         and use the same logic
#
# IMPORTANT: the HMAC secret used here MUST match the secret embedded
# in the desktop build, otherwise activation tokens won't validate.
# Both pull from DI_LICENSE_SECRET. Rotate by env var only.
# ============================================================

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Make `licensing` importable when run from repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import licensing


# ============================================================
# Config (env-driven so deployment is stateless)
# ============================================================

DB_PATH      = os.environ.get("DI_SERVER_DB",          str(_REPO_ROOT / "tools" / "licenses.db"))
MAX_DEVICES  = int(os.environ.get("DI_MAX_DEVICES",    "2"))
PORT         = int(os.environ.get("PORT",              "8080"))
BIND_ADDRESS = os.environ.get("DI_SERVER_BIND",        "127.0.0.1")


# ============================================================
# Storage layer (SQLite — single file, easy backup)
# ============================================================
# Schema is tiny: one row per (license_email, fingerprint) tuple.
# Email is the natural license identifier; fingerprint is the device.

_db_lock = threading.Lock()
_db_initialised = False


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    global _db_initialised
    if _db_initialised:
        return
    with _db_lock, _connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS activations (
                license_email TEXT NOT NULL,
                fingerprint   TEXT NOT NULL,
                device_index  INTEGER NOT NULL,
                activated_at  TEXT NOT NULL,
                PRIMARY KEY (license_email, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_email ON activations(license_email);
        """)
    _db_initialised = True


def _ensure_db():
    """Cheap idempotent check — handle_activate / deactivate call this first."""
    if not _db_initialised:
        _init_db()


def _devices_for(email: str):
    with _db_lock, _connect() as c:
        rows = c.execute(
            "SELECT fingerprint, device_index, activated_at "
            "FROM activations WHERE license_email = ? ORDER BY device_index",
            (email,)).fetchall()
        return [dict(r) for r in rows]


def _record_activation(email: str, fingerprint: str, device_index: int):
    with _db_lock, _connect() as c:
        c.execute(
            "INSERT INTO activations (license_email, fingerprint, device_index, activated_at) "
            "VALUES (?, ?, ?, ?)",
            (email, fingerprint, device_index,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))


def _delete_activation(email: str, fingerprint: str) -> bool:
    with _db_lock, _connect() as c:
        cur = c.execute(
            "DELETE FROM activations WHERE license_email = ? AND fingerprint = ?",
            (email, fingerprint))
        return cur.rowcount > 0


# ============================================================
# Activation logic
# ============================================================

def handle_activate(license_blob: str, fingerprint: str) -> Dict[str, Any]:
    """
    Idempotent activation:
      • Validate the license blob (signature + expiry)
      • If this fingerprint already has a slot for this email → re-issue same token
      • Else, if there's room (< max_devices) → assign next free slot
      • Else → return error with current/max counts
    """
    _ensure_db()
    res = licensing.validate_blob(license_blob)
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}

    email = res["license"]["email"].strip().lower()

    # Check existing
    devices = _devices_for(email)
    for d in devices:
        if d["fingerprint"] == fingerprint:
            # Re-activation of same device — issue a fresh token (idempotent)
            token = licensing.mint_activation(
                license_email=email,
                fingerprint=fingerprint,
                device_index=int(d["device_index"]),
                max_devices=MAX_DEVICES,
                activated_at=d["activated_at"],
            )
            return {"ok": True, "activation": token,
                    "device_index": int(d["device_index"]),
                    "max_devices":  MAX_DEVICES}

    if len(devices) >= MAX_DEVICES:
        return {
            "ok": False,
            "reason": "Activation limit reached for this license. "
                      "Deactivate one of your existing devices and try again.",
            "current_devices": len(devices),
            "max_devices": MAX_DEVICES,
        }

    # Assign next free slot
    used_indices = {int(d["device_index"]) for d in devices}
    next_idx = next(i for i in range(1, MAX_DEVICES + 1) if i not in used_indices)
    _record_activation(email, fingerprint, next_idx)

    token = licensing.mint_activation(
        license_email=email,
        fingerprint=fingerprint,
        device_index=next_idx,
        max_devices=MAX_DEVICES,
    )
    return {"ok": True, "activation": token,
            "device_index": next_idx, "max_devices": MAX_DEVICES}


def handle_deactivate(license_blob: str, fingerprint: str) -> Dict[str, Any]:
    """Free a slot. Idempotent — missing slot is still 'ok'."""
    _ensure_db()
    res = licensing.validate_blob(license_blob)
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"]}
    email = res["license"]["email"].strip().lower()
    _delete_activation(email, fingerprint)
    return {"ok": True}


# ============================================================
# Flask app (only imported when running as a server)
# ============================================================

def make_app():
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        sys.stderr.write(
            "Flask is required to run the activation server.\n"
            "Install with:  pip install flask gunicorn\n"
        )
        raise SystemExit(1)

    app = Flask(__name__)
    _init_db()

    @app.route("/health", methods=["GET"])
    def health():
        try:
            with _db_lock, _connect() as c:
                c.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            db_ok = False
        return jsonify({"ok": True, "db": db_ok,
                        "version": "1.0",
                        "max_devices": MAX_DEVICES})

    @app.route("/activate", methods=["POST"])
    def activate():
        body = request.get_json(silent=True) or {}
        blob = (body.get("license") or "").strip()
        fp   = (body.get("fingerprint") or "").strip()
        if not blob or not fp:
            return jsonify({"ok": False, "reason": "Missing license or fingerprint"}), 400
        result = handle_activate(blob, fp)
        return jsonify(result), (200 if result.get("ok") else 403)

    @app.route("/deactivate", methods=["POST"])
    def deactivate():
        body = request.get_json(silent=True) or {}
        blob = (body.get("license") or "").strip()
        fp   = (body.get("fingerprint") or "").strip()
        if not blob or not fp:
            return jsonify({"ok": False, "reason": "Missing license or fingerprint"}), 400
        return jsonify(handle_deactivate(blob, fp)), 200

    return app


# When run as a script: dev server (gunicorn for production)
if __name__ == "__main__":
    app = make_app()
    app.run(host=BIND_ADDRESS, port=PORT, debug=False)
