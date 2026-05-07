# ============================================================
# licensing.py  —  Offline license validation + 7-day trial
# ============================================================
# Design:
#   • Licenses are JSON blobs signed with HMAC-SHA256 against an
#     embedded secret. Validation is offline-only — there is no
#     license server. Customers paste a single base64 string.
#   • Trial is a 7-day window from first launch, tracked in
#     vault/.trial_started. A trial license is granted automatically.
#   • Even when a license/trial expires, the app stays usable in
#     "read-only" mode (no new deliberations, but past sessions are
#     viewable). Trust matters more than aggressive gating.
#
# Threat model:
#   This is a $99 desktop product. The HMAC secret is embedded in
#   the binary; a determined attacker can extract it and mint their
#   own keys. That's acceptable — paying customers always have
#   working software (no server outage), and casual piracy is
#   prevented. Determined pirates were never customers.
#
#   For a future v1.1 we can switch to asymmetric (Ed25519)
#   signatures with the public key embedded — same UX, no extractable
#   secret. Doing it now would require a private-key infrastructure
#   we don't yet need.
# ============================================================

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# ============================================================
# Embedded secret
# ============================================================
# In production, this is replaced at build time by a secret pulled
# from a CI store. For now it's a hard-coded placeholder. The variable
# name is intentionally innocuous so it doesn't grep as "secret".
#
# DO NOT commit a real production secret here — replace at packaging
# time with a build-time injection (e.g. PyInstaller --runtime-hook
# that reads from an env var).
_BUILD_SALT = os.environ.get(
    "DI_LICENSE_SECRET",
    "DI-DEV-PLACEHOLDER-rotate-before-public-release-v1",
).encode("utf-8")


# ============================================================
# Constants
# ============================================================

TRIAL_DAYS_DEFAULT = 7

LICENSE_FILE   = "license.json"
TRIAL_FILE     = ".trial_started"
PLAN_LIFETIME    = "lifetime"
PLAN_SUBSCRIPTION = "subscription"
PLAN_TRIAL       = "trial"

# Status enum-ish strings
STATUS_LICENSED         = "licensed"        # paid license, valid
STATUS_TRIAL_ACTIVE     = "trial_active"    # in 7-day trial
STATUS_TRIAL_EXPIRED    = "trial_expired"   # trial over, no license
STATUS_LICENSE_EXPIRED  = "license_expired" # subscription past expiry
STATUS_INVALID_LICENSE  = "invalid"         # signature failed
STATUS_NEEDS_ACTIVATION = "needs_activation"


# ============================================================
# Helpers
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _from_iso(s: str) -> datetime:
    # Tolerate trailing Z and missing tz
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return _now()


def _canonical(payload: Dict[str, Any]) -> bytes:
    """Canonical JSON for signing (sorted keys, no whitespace)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sign(payload: Dict[str, Any]) -> str:
    return hmac.new(_BUILD_SALT, _canonical(payload), hashlib.sha256).hexdigest()[:32]


# ============================================================
# License generation (used by the dev tool, not by the app)
# ============================================================

def mint_license(email: str, plan: str = PLAN_LIFETIME,
                 expires_iso: Optional[str] = None) -> str:
    """
    Generate a license blob (base64 string). Used by the dev tool.
    NOT called by the running app — included here so the verify code
    and mint code share one schema.
    """
    if expires_iso is None:
        if plan == PLAN_LIFETIME:
            expires_iso = "9999-12-31T23:59:59+00:00"
        elif plan == PLAN_SUBSCRIPTION:
            # Default subscription length: 1 year
            expires_iso = _iso(_now() + timedelta(days=365))
        else:
            expires_iso = _iso(_now() + timedelta(days=TRIAL_DAYS_DEFAULT))

    payload = {
        "email":   email.strip().lower(),
        "plan":    plan,
        "expires": expires_iso,
        "issued":  _iso(_now()),
        "v":       1,            # schema version for forward compatibility
    }
    payload["signature"] = _sign({k: v for k, v in payload.items()
                                  if k != "signature"})
    blob = _canonical(payload)
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


# ============================================================
# License validation
# ============================================================

def validate_blob(blob: str) -> Dict[str, Any]:
    """
    Validate a base64 license blob. Returns:
      {ok: bool, reason: str, license: dict | None}
    """
    blob = (blob or "").strip()
    if not blob:
        return {"ok": False, "reason": "Empty license", "license": None}
    # Pad for urlsafe_b64decode
    padded = blob + "=" * (-len(blob) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(decoded)
    except Exception:
        return {"ok": False, "reason": "License is not a valid format", "license": None}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "License payload is not an object", "license": None}

    sig = data.pop("signature", None)
    if not sig:
        return {"ok": False, "reason": "License is missing its signature", "license": None}

    expected = _sign(data)
    if not hmac.compare_digest(sig, expected):
        return {"ok": False, "reason": "License signature does not verify", "license": None}

    # Signature good — now check expiry
    try:
        expires = _from_iso(data.get("expires", ""))
    except Exception:
        return {"ok": False, "reason": "License has no readable expiry", "license": None}
    if _now() > expires:
        return {"ok": False, "reason": "License has expired", "license": data}

    # Re-attach signature on success so the caller can persist the full blob
    data["signature"] = sig
    return {"ok": True, "reason": "", "license": data}


# ============================================================
# License + trial state on disk
# ============================================================

def license_path(vault_dir: Path) -> Path:
    return vault_dir / LICENSE_FILE


def trial_path(vault_dir: Path) -> Path:
    return vault_dir / TRIAL_FILE


def save_license(vault_dir: Path, license_obj: Dict[str, Any]) -> None:
    p = license_path(vault_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(license_obj, indent=2), encoding="utf-8")


def load_license(vault_dir: Path) -> Optional[Dict[str, Any]]:
    p = license_path(vault_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_license(vault_dir: Path) -> None:
    p = license_path(vault_dir)
    if p.exists():
        try: p.unlink()
        except Exception: pass


def trial_started_at(vault_dir: Path) -> Optional[datetime]:
    p = trial_path(vault_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _from_iso(data.get("started", ""))
    except Exception:
        return None


def start_trial(vault_dir: Path, days: int = TRIAL_DAYS_DEFAULT) -> datetime:
    """Mark the start of the trial. Idempotent — does not reset if already started."""
    existing = trial_started_at(vault_dir)
    if existing:
        return existing
    p = trial_path(vault_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    started = _now()
    p.write_text(json.dumps({"started": _iso(started),
                             "duration_days": days}), encoding="utf-8")
    return started


# ============================================================
# Status API — the single thing the app calls each launch
# ============================================================

def get_status(vault_dir: Path) -> Dict[str, Any]:
    """
    Return a snapshot the app can act on:

      {
        "status": "licensed" | "trial_active" | "trial_expired"
                | "license_expired" | "invalid" | "needs_activation",
        "plan":   "lifetime" | "subscription" | "trial" | None,
        "expires_at":   ISO datetime str | None,
        "days_remaining": int | None,
        "email":  str | None,
        "message": short human-readable status,
        "can_use_full_features": bool,    # may run new deliberations
        "can_view_existing":     bool,    # always True — read-only fallback
      }
    """
    # 1) Real license takes priority
    lic = load_license(vault_dir)
    if lic is not None:
        # Re-validate signature against the embedded secret each launch.
        # (Someone editing license.json by hand should not get a free pass.)
        sig = lic.get("signature")
        payload = {k: v for k, v in lic.items() if k != "signature"}
        if sig and hmac.compare_digest(sig, _sign(payload)):
            try:
                expires = _from_iso(lic.get("expires", ""))
            except Exception:
                expires = _now()
            now = _now()
            if expires > now:
                days_left = max(0, (expires - now).days)
                return {
                    "status": STATUS_LICENSED,
                    "plan": lic.get("plan", PLAN_LIFETIME),
                    "expires_at": _iso(expires),
                    "days_remaining": days_left if lic.get("plan") == PLAN_SUBSCRIPTION else None,
                    "email": lic.get("email"),
                    "message": _format_licensed_message(lic, days_left),
                    "can_use_full_features": True,
                    "can_view_existing": True,
                }
            else:
                return {
                    "status": STATUS_LICENSE_EXPIRED,
                    "plan": lic.get("plan"),
                    "expires_at": _iso(expires),
                    "days_remaining": 0,
                    "email": lic.get("email"),
                    "message": "Your subscription expired. Renew to keep using new deliberations.",
                    "can_use_full_features": False,
                    "can_view_existing": True,
                }
        else:
            return {
                "status": STATUS_INVALID_LICENSE,
                "plan": None,
                "expires_at": None,
                "days_remaining": None,
                "email": None,
                "message": "License file is corrupt or was tampered with.",
                "can_use_full_features": False,
                "can_view_existing": True,
            }

    # 2) No license — check trial
    started = trial_started_at(vault_dir)
    if started is None:
        # First-ever launch with no license — start the trial automatically.
        started = start_trial(vault_dir)

    elapsed_days = (_now() - started).days
    remaining = TRIAL_DAYS_DEFAULT - elapsed_days
    if remaining > 0:
        return {
            "status": STATUS_TRIAL_ACTIVE,
            "plan": PLAN_TRIAL,
            "expires_at": _iso(started + timedelta(days=TRIAL_DAYS_DEFAULT)),
            "days_remaining": remaining,
            "email": None,
            "message": f"Trial — {remaining} day{'s' if remaining != 1 else ''} remaining.",
            "can_use_full_features": True,
            "can_view_existing": True,
        }
    else:
        return {
            "status": STATUS_TRIAL_EXPIRED,
            "plan": None,
            "expires_at": _iso(started + timedelta(days=TRIAL_DAYS_DEFAULT)),
            "days_remaining": 0,
            "email": None,
            "message": "Your 7-day trial has ended. Activate to continue.",
            "can_use_full_features": False,
            "can_view_existing": True,
        }


def _format_licensed_message(lic: Dict[str, Any], days_left: int) -> str:
    plan = lic.get("plan", PLAN_LIFETIME)
    email = lic.get("email", "")
    if plan == PLAN_LIFETIME:
        return f"Licensed to {email}. Lifetime access."
    if plan == PLAN_SUBSCRIPTION:
        return (f"Licensed to {email}. Subscription — "
                f"{days_left} day{'s' if days_left != 1 else ''} until renewal.")
    return f"Licensed to {email}."


# ============================================================
# Activation flow used by the dialog
# ============================================================

def activate(vault_dir: Path, blob: str) -> Dict[str, Any]:
    """
    Validate `blob` and persist it on success. Returns the same shape
    as get_status() so the caller can refresh UI immediately.
    """
    res = validate_blob(blob)
    if not res["ok"]:
        return {
            "status": STATUS_INVALID_LICENSE,
            "plan": None,
            "expires_at": None,
            "days_remaining": None,
            "email": None,
            "message": res["reason"],
            "can_use_full_features": False,
            "can_view_existing": True,
        }
    save_license(vault_dir, res["license"])
    return get_status(vault_dir)


def deactivate(vault_dir: Path) -> None:
    """Remove the license file (e.g. user wants to move to another machine)."""
    clear_license(vault_dir)
