#!/usr/bin/env python3
# ============================================================
# generate_license.py  —  developer-only key minter
# ============================================================
# Run from the repo root:
#
#   # Lifetime license
#   python tools/generate_license.py --email customer@example.com
#
#   # Annual subscription (1 year)
#   python tools/generate_license.py --email customer@example.com --plan subscription
#
#   # Subscription with explicit expiry
#   python tools/generate_license.py --email c@x.com --plan subscription \
#       --expires 2027-12-31
#
# The output is a single base64 string. Email it to your customer; they
# paste it into the Activate dialog. NEVER share the secret used to sign
# these — keep _BUILD_SALT in licensing.py rotated and pinned via your
# CI build pipeline.
#
# This script is in tools/ rather than the main package so it never
# ends up bundled into the customer-facing executable.
# ============================================================

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `licensing` importable when this script is run from the repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import licensing


def main() -> int:
    p = argparse.ArgumentParser(
        description="Mint a Data's Inferno license blob.",
    )
    p.add_argument("--email",
                   help="Customer email (lowercased and stored in the license). "
                        "Required for minting; ignored when --check is used.")
    p.add_argument("--plan",
                   choices=[licensing.PLAN_LIFETIME,
                            licensing.PLAN_SUBSCRIPTION],
                   default=licensing.PLAN_LIFETIME,
                   help="License plan. Default: lifetime.")
    p.add_argument("--expires",
                   help="ISO date for expiry (subscription only). "
                        "Default: 1 year from today for subscription, "
                        "9999 for lifetime.")
    p.add_argument("--check",
                   help="Validate an existing license blob instead of minting one.")
    args = p.parse_args()

    if not args.check and not args.email:
        p.error("--email is required when minting a license")

    if args.check:
        result = licensing.validate_blob(args.check)
        if result["ok"]:
            lic = result["license"]
            print("[ok] Valid license")
            print(f"  Email   : {lic.get('email')}")
            print(f"  Plan    : {lic.get('plan')}")
            print(f"  Issued  : {lic.get('issued')}")
            print(f"  Expires : {lic.get('expires')}")
            return 0
        else:
            print(f"[bad] Invalid: {result['reason']}")
            return 1

    blob = licensing.mint_license(
        email=args.email,
        plan=args.plan,
        expires_iso=args.expires + "T23:59:59+00:00" if args.expires else None,
    )
    print("=" * 60)
    print(f"  Plan : {args.plan}")
    print(f"  For  : {args.email}")
    print("=" * 60)
    print()
    print(blob)
    print()
    print("Send this single line to the customer.")
    print("They paste it into Help -> Activate License in the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
