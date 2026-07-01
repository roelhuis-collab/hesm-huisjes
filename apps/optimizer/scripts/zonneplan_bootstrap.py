#!/usr/bin/env python3
"""
Zonneplan bootstrap — magic-link + one-time-code exchange for a bearer token.

Zonneplan's auth flow (as of 2026): user provides email, backend sends
a magic link. Clicking the link either sets a browser session or
delivers a one-time code. The Zonneplan mobile-app then exchanges
``(email, code, device_uuid)`` for an access + refresh token pair.

For headless Cloud Run we do the same one-shot dance locally, print the
tokens, and store them in Secret Manager.

Usage::

    cd apps/optimizer
    uv run scripts/zonneplan_bootstrap.py --email 'roelhuis@gmail.com'

The script:
  1. Generates a device UUID (keep it stable across refreshes).
  2. POSTs to ``/auth/request`` to trigger the magic-link email.
  3. Waits for you to paste the one-time code from the email.
  4. POSTs to ``/auth/login-with-token`` to exchange for tokens.
  5. Prints ``access_token`` + ``refresh_token`` + ``device_uuid``.

Store all three in Secret Manager as ``zonneplan-access-token`` /
``zonneplan-refresh-token`` / ``zonneplan-device-uuid``; extend
``cloudbuild.yaml`` per ``infra/SETUP.md``.

Note: Zonneplan's public API is not officially documented; the
endpoints match what the community Home-Assistant integration uses.
If the endpoint shape has drifted, this script is the first place that
will surface the mismatch — the connector proper only sees the token.
"""

from __future__ import annotations

import argparse
import sys
import uuid as uuidlib

import httpx

API_BASE_URL = "https://app-api.zonneplan.nl"
AUTH_REQUEST_PATH = "/auth/request"
AUTH_TOKEN_PATH = "/auth/login-with-token"


def _post(url: str, body: dict[str, object]) -> httpx.Response:
    return httpx.post(url, json=body, timeout=15.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Zonneplan-account e-mail")
    parser.add_argument(
        "--device-uuid",
        default=None,
        help="Reuse a specific device UUID instead of generating a new one",
    )
    args = parser.parse_args()

    device_uuid = args.device_uuid or str(uuidlib.uuid4())

    print(
        f"Requesting magic-link for {args.email} (device_uuid={device_uuid})…",
        file=sys.stderr,
    )
    resp = _post(
        API_BASE_URL + AUTH_REQUEST_PATH,
        {"email": args.email, "method": "email"},
    )
    if resp.status_code >= 400:
        raise SystemExit(
            f"magic-link request failed: {resp.status_code} {resp.text[:200]}"
        )

    print(
        "\n== Check your inbox at "
        f"{args.email}. Zonneplan sent you a one-time code.\n"
        "Open the email, copy the code (usually shown after clicking the link "
        "or embedded as a query param), and paste it below.\n",
        file=sys.stderr,
    )
    code = input("One-time code: ").strip()
    if not code:
        raise SystemExit("no code provided")

    resp = _post(
        API_BASE_URL + AUTH_TOKEN_PATH,
        {"code": code, "device_uuid": device_uuid, "email": args.email},
    )
    if resp.status_code >= 400:
        raise SystemExit(
            f"token exchange failed: {resp.status_code} {resp.text[:200]}"
        )
    body = resp.json()
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise SystemExit(f"missing token(s) in response: {body}")

    print(
        "\n=== Zonneplan tokens (store all three in Secret Manager) ===",
        file=sys.stderr,
    )
    print(f"access_token  = {access}")
    print(f"refresh_token = {refresh}")
    print(f"device_uuid   = {device_uuid}")
    print(
        "\nNext steps:\n"
        "  printf '%s' '<access-token>'  | gcloud secrets create zonneplan-access-token  --data-file=- --project=hesm-huisjes\n"
        "  printf '%s' '<refresh-token>' | gcloud secrets create zonneplan-refresh-token --data-file=- --project=hesm-huisjes\n"
        f"  printf '%s' '{device_uuid}' | gcloud secrets create zonneplan-device-uuid   --data-file=- --project=hesm-huisjes\n"
        "  # then IAM-bindings + cloudbuild.yaml — see infra/SETUP.md.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
