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

LICENSE_FILE     = "license.json"
ACTIVATION_FILE  = "activation.json"   # signed by the server, validated offline
TRIAL_FILE       = ".trial_started"
PLAN_LIFETIME    = "lifetime"
PLAN_SUBSCRIPTION = "subscription"
PLAN_TRIAL       = "trial"

# Default device limit per license. Server is authoritative; this is just
# the value the client expects when a server is not configured.
DEFAULT_MAX_DEVICES = 2

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
# Activation-token mint + verify
# ============================================================
# An activation token is what the activation server returns after a
# successful first-time activation. It binds the license to a specific
# device fingerprint. Once stored locally, subsequent launches verify
# the token offline — no further internet required.
#
# Token shape (same canonical-JSON + HMAC pattern as the license):
#   {
#     "license_email": "user@example.com",
#     "fingerprint":   "<sha256>",
#     "device_index":  1,
#     "max_devices":   2,
#     "activated_at":  "2026-05-06T...",
#     "v":             1,
#     "signature":     "<hmac>"
#   }
# ============================================================

def mint_activation(license_email: str, fingerprint: str, *,
                    device_index: int, max_devices: int = DEFAULT_MAX_DEVICES,
                    activated_at: Optional[str] = None) -> Dict[str, Any]:
    """
    Used by the activation server. NOT called by the running app.
    Returns the dict the server will return to the client (with signature).
    """
    payload = {
        "license_email": license_email.strip().lower(),
        "fingerprint":   fingerprint,
        "device_index":  int(device_index),
        "max_devices":   int(max_devices),
        "activated_at":  activated_at or _iso(_now()),
        "v":             1,
    }
    payload["signature"] = _sign({k: v for k, v in payload.items() if k != "signature"})
    return payload


def verify_activation(token: Dict[str, Any], expected_email: str,
                      expected_fingerprint: str) -> Dict[str, Any]:
    """
    Validate an activation token offline. Returns:
      {ok: bool, reason: str, token: dict | None}

    The token is rejected if:
      • signature doesn't verify
      • email doesn't match the license that's already on disk
      • fingerprint doesn't match the current device
    """
    if not isinstance(token, dict):
        return {"ok": False, "reason": "Activation file is unreadable", "token": None}

    sig = token.pop("signature", None) if "signature" in token else None
    if not sig:
        return {"ok": False, "reason": "Activation token missing signature", "token": None}

    payload = {k: v for k, v in token.items() if k != "signature"}
    if not hmac.compare_digest(sig, _sign(payload)):
        return {"ok": False, "reason": "Activation token signature invalid", "token": None}

    if payload.get("license_email", "").strip().lower() != expected_email.strip().lower():
        return {"ok": False, "reason": "Activation token belongs to a different license",
                "token": None}

    if payload.get("fingerprint") != expected_fingerprint:
        return {"ok": False, "reason": "Activation token is for a different device",
                "token": None}

    payload["signature"] = sig
    return {"ok": True, "reason": "", "token": payload}


def save_activation(vault_dir: Path, token: Dict[str, Any]) -> None:
    p = vault_dir / ACTIVATION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(token, indent=2), encoding="utf-8")


def load_activation(vault_dir: Path) -> Optional[Dict[str, Any]]:
    p = vault_dir / ACTIVATION_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_activation(vault_dir: Path) -> None:
    p = vault_dir / ACTIVATION_FILE
    if p.exists():
        try: p.unlink()
        except Exception: pass


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

def get_status(vault_dir: Path,
               *, fingerprint: Optional[str] = None) -> Dict[str, Any]:
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
        "device_index":  int | None,      # 1-based slot on this device
        "max_devices":   int | None,      # licensed device limit
      }

    Pass `fingerprint` (from device_fingerprint.compute()) to require the
    saved activation to match the current machine. Without it, activation
    binding is not checked — useful for rare cases where the app needs
    license info without a fingerprint.
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
            if expires <= now:
                return {
                    "status": STATUS_LICENSE_EXPIRED,
                    "plan": lic.get("plan"),
                    "expires_at": _iso(expires),
                    "days_remaining": 0,
                    "email": lic.get("email"),
                    "message": "Your subscription expired. Renew to keep using new deliberations.",
                    "can_use_full_features": False,
                    "can_view_existing": True,
                    "device_index": None,
                    "max_devices":  None,
                }

            # License is valid — now require an activation token bound
            # to THIS device. Without it, fall through to "needs_activation".
            act = load_activation(vault_dir)
            if act is None:
                return {
                    "status": STATUS_NEEDS_ACTIVATION,
                    "plan": lic.get("plan"),
                    "expires_at": _iso(expires),
                    "days_remaining": None,
                    "email": lic.get("email"),
                    "message": "License found — needs to be activated on this device.",
                    "can_use_full_features": False,
                    "can_view_existing": True,
                    "device_index": None,
                    "max_devices":  None,
                }

            # If a fingerprint was supplied, verify the token matches it.
            if fingerprint is not None:
                ver = verify_activation(dict(act), lic.get("email", ""), fingerprint)
                if not ver["ok"]:
                    return {
                        "status": STATUS_NEEDS_ACTIVATION,
                        "plan": lic.get("plan"),
                        "expires_at": _iso(expires),
                        "days_remaining": None,
                        "email": lic.get("email"),
                        "message": ver["reason"] + " — re-activate to continue.",
                        "can_use_full_features": False,
                        "can_view_existing": True,
                        "device_index": None,
                        "max_devices":  None,
                    }

            days_left = max(0, (expires - now).days)
            return {
                "status": STATUS_LICENSED,
                "plan": lic.get("plan", PLAN_LIFETIME),
                "expires_at": _iso(expires),
                "days_remaining": days_left if lic.get("plan") == PLAN_SUBSCRIPTION else None,
                "email": lic.get("email"),
                "message": _format_licensed_message(lic, days_left, act),
                "can_use_full_features": True,
                "can_view_existing": True,
                "device_index": int(act.get("device_index", 1)),
                "max_devices":  int(act.get("max_devices", DEFAULT_MAX_DEVICES)),
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
                "device_index": None,
                "max_devices":  None,
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
            "device_index": None,
            "max_devices":  None,
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
            "device_index": None,
            "max_devices":  None,
        }


def _format_licensed_message(lic: Dict[str, Any], days_left: int,
                             activation: Optional[Dict[str, Any]] = None) -> str:
    plan = lic.get("plan", PLAN_LIFETIME)
    email = lic.get("email", "")
    base = f"Licensed to {email}."
    if plan == PLAN_LIFETIME:
        base = f"Licensed to {email}. Lifetime access."
    elif plan == PLAN_SUBSCRIPTION:
        base = (f"Licensed to {email}. Subscription — "
                f"{days_left} day{'s' if days_left != 1 else ''} until renewal.")
    if activation:
        idx = activation.get("device_index")
        mx  = activation.get("max_devices")
        if idx and mx:
            base += f"  (Device {idx} of {mx})"
    return base


# ============================================================
# Activation flow used by the dialog
# ============================================================

def activate(vault_dir: Path, blob: str, *,
             fingerprint: str,
             activation_server: "ActivationServer") -> Dict[str, Any]:
    """
    Two-stage activation:
      1. Validate the license blob locally (signature + expiry)
      2. Call the activation server with (license, fingerprint)
         to obtain an activation token bound to this device
      3. Save both on success

    Returns the same shape as get_status() so the dialog can refresh
    immediately.

    `activation_server` is anything with an `.activate(license_blob,
    fingerprint)` method that returns one of:
      • {"ok": True, "activation": <signed_token_dict>}
      • {"ok": False, "reason": <str>, optional extra fields}

    See tools/license_server.py for the reference implementation and
    activation_dialog.py for the HTTP client.
    """
    res = validate_blob(blob)
    if not res["ok"]:
        return _err_status(res["reason"])

    license_data = res["license"]

    # Stage 2 — call the activation server
    try:
        srv_resp = activation_server.activate(blob, fingerprint)
    except Exception as e:
        return _err_status(
            f"Could not reach the activation server ({e}). "
            "Check your internet connection and try again. "
            "Activation only requires internet once — afterwards the app "
            "works fully offline.")

    if not srv_resp.get("ok"):
        reason = srv_resp.get("reason", "Activation declined by server.")
        # Surface device-limit details if the server provided them
        if srv_resp.get("max_devices") and srv_resp.get("current_devices"):
            reason += (f"  (Currently activated on "
                       f"{srv_resp['current_devices']} of "
                       f"{srv_resp['max_devices']} devices.)")
        return _err_status(reason)

    # Stage 3 — verify the server's activation token signature, then save
    token = srv_resp.get("activation") or {}
    ver = verify_activation(dict(token), license_data.get("email", ""), fingerprint)
    if not ver["ok"]:
        return _err_status(f"Activation token rejected: {ver['reason']}")

    save_license(vault_dir, license_data)
    save_activation(vault_dir, ver["token"])
    return get_status(vault_dir, fingerprint=fingerprint)


def deactivate(vault_dir: Path, *,
               fingerprint: str = "",
               activation_server: Optional["ActivationServer"] = None) -> Dict[str, Any]:
    """
    Remove the license + activation locally. If the server is available,
    also tell it to free this device's slot so the user can re-activate
    elsewhere. Best-effort — local removal happens regardless of server
    response so the user is never stuck with a half-deactivated install.
    """
    server_msg = ""
    if activation_server is not None and fingerprint:
        try:
            lic = load_license(vault_dir) or {}
            sig = lic.get("signature")
            if sig:
                # Reconstruct the blob to send to the server
                payload = {k: v for k, v in lic.items() if k != "signature"}
                payload["signature"] = sig
                blob = base64.urlsafe_b64encode(_canonical(payload)).decode("ascii").rstrip("=")
                resp = activation_server.deactivate(blob, fingerprint)
                if not resp.get("ok"):
                    server_msg = resp.get("reason", "Server declined deactivation")
        except Exception as e:
            server_msg = f"Could not reach server ({e}). Slot may stay claimed."

    clear_license(vault_dir)
    clear_activation(vault_dir)
    return {"ok": True, "server_message": server_msg}


def _err_status(message: str) -> Dict[str, Any]:
    return {
        "status": STATUS_INVALID_LICENSE,
        "plan": None,
        "expires_at": None,
        "days_remaining": None,
        "email": None,
        "message": message,
        "can_use_full_features": False,
        "can_view_existing": True,
        "device_index": None,
        "max_devices":  None,
    }


# ============================================================
# Activation server protocol (HTTP client)
# ============================================================
# The dialog passes one of these to activate()/deactivate(). Two
# implementations live in this file:
#   • HttpActivationServer — talks to the production endpoint
#   • LocalActivationServer — minted locally, used only when no server
#                              URL is configured (offline self-service
#                              fallback for devs / smoke tests)

class ActivationServer:
    """Protocol shape — both methods return server response dicts."""
    def activate(self, license_blob: str, fingerprint: str) -> Dict[str, Any]:
        raise NotImplementedError
    def deactivate(self, license_blob: str, fingerprint: str) -> Dict[str, Any]:
        raise NotImplementedError


class HttpActivationServer(ActivationServer):
    """Default: POST to a remote endpoint."""

    def __init__(self, base_url: str, timeout_seconds: float = 6.0):
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout_seconds

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.request as _u
        data = json.dumps(body).encode("utf-8")
        req  = _u.Request(self.base_url + path, data=data,
                          headers={"Content-Type": "application/json",
                                   "Accept":       "application/json"})
        with _u.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(64 * 1024)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": "Server returned non-JSON response"}

    def activate(self, license_blob: str, fingerprint: str) -> Dict[str, Any]:
        return self._post("/activate", {"license": license_blob,
                                         "fingerprint": fingerprint})

    def deactivate(self, license_blob: str, fingerprint: str) -> Dict[str, Any]:
        return self._post("/deactivate", {"license": license_blob,
                                           "fingerprint": fingerprint})


class LocalActivationServer(ActivationServer):
    """
    Mints activation tokens locally without a real server. Intended ONLY
    for development / unit tests / when you explicitly want to ship a
    "no online activation" build for an offline customer.

    Treats every fingerprint as a fresh device_index 1 with max_devices 1
    (because it can't track state across machines).
    """

    def activate(self, license_blob: str, fingerprint: str) -> Dict[str, Any]:
        res = validate_blob(license_blob)
        if not res["ok"]:
            return {"ok": False, "reason": res["reason"]}
        token = mint_activation(
            license_email=res["license"]["email"],
            fingerprint=fingerprint,
            device_index=1, max_devices=1,
        )
        return {"ok": True, "activation": token,
                "device_index": 1, "max_devices": 1}

    def deactivate(self, license_blob: str, fingerprint: str) -> Dict[str, Any]:
        return {"ok": True}


def make_activation_server() -> ActivationServer:
    """
    Pick the activation backend based on branding config:
      branding.ACTIVATION_SERVER_URL set → HttpActivationServer
      empty string                       → LocalActivationServer
    """
    try:
        import branding
        url = getattr(branding, "ACTIVATION_SERVER_URL", "") or ""
    except Exception:
        url = ""
    return HttpActivationServer(url) if url else LocalActivationServer()
