#!/usr/bin/env python3
"""
Resideo (Honeywell Home) OAuth bootstrap — one-shot CLI to capture a refresh token.

Usage::

    cd apps/optimizer
    uv run scripts/resideo_bootstrap.py \\
        --client-id  YOUR_CONSUMER_KEY \\
        --client-secret YOUR_CONSUMER_SECRET

You get the Consumer Key + Consumer Secret from developer.honeywellhome.com:
sign in → My Apps → create a new app → use ``http://localhost:8765/callback``
as the Redirect URL.

What it does
------------
1. Starts a tiny HTTP server on ``localhost:8765`` to catch the OAuth callback.
2. Opens your default browser to the Honeywell Home authorize URL.
3. You log in to your Honeywell-account in the browser and approve the app.
4. Honeywell redirects back to ``http://localhost:8765/callback`` with a
   ``code``; this script exchanges the code for an access + refresh token
   and prints the refresh token.
5. Store the printed token in Secret Manager::

       printf '%s' '<refresh-token>' | gcloud secrets create resideo-refresh-token --data-file=-

   Then add IAM access for the Cloud Run runtime SA — see ``infra/SETUP.md``.

Re-run only when the refresh token gets revoked (e.g. you reset your password).
Honeywell rotates refresh tokens on every refresh, so under normal operation
the live secret stays valid forever.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser

import httpx

OAUTH2_AUTHORIZE_URL = "https://api.honeywell.com/oauth2/authorize"
OAUTH2_TOKEN_URL = "https://api.honeywell.com/oauth2/token"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8765
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        if "error" in params:
            type(self).error = params["error"][0]
            body = f"<h1>Resideo auth failed</h1><p>{type(self).error}</p>"
        elif "code" in params:
            type(self).code = params["code"][0]
            body = "<h1>Resideo auth complete</h1><p>You can close this tab.</p>"
        else:
            type(self).error = "no code or error in callback"
            body = "<h1>Resideo auth: missing code</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt: str, *args: object) -> None:  # silence default logger
        return None


def _wait_for_code(timeout_s: float = 300.0) -> str:
    server = socketserver.TCPServer((REDIRECT_HOST, REDIRECT_PORT), _CodeCatcher)
    server.timeout = 1.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        elapsed = 0.0
        step = 0.5
        while elapsed < timeout_s:
            if _CodeCatcher.code is not None or _CodeCatcher.error is not None:
                break
            thread.join(step)
            elapsed += step
    finally:
        server.shutdown()
        server.server_close()

    if _CodeCatcher.error is not None:
        raise SystemExit(f"Resideo auth failed: {_CodeCatcher.error}")
    if _CodeCatcher.code is None:
        raise SystemExit(f"timed out after {timeout_s:.0f}s waiting for callback")
    return _CodeCatcher.code


def _exchange(code: str, client_id: str, client_secret: str) -> dict[str, object]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {
        "Authorization": _basic_auth_header(client_id, client_secret),
        "Accept": "application/json",
    }
    resp = httpx.post(OAUTH2_TOKEN_URL, data=payload, headers=headers, timeout=15.0)
    if resp.status_code != 200:
        raise SystemExit(
            f"token exchange failed: {resp.status_code} {resp.text[:300]}"
        )
    body: dict[str, object] = resp.json()
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, help="Honeywell Consumer Key")
    parser.add_argument(
        "--client-secret", required=True, help="Honeywell Consumer Secret"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the auth URL instead of opening a browser.",
    )
    args = parser.parse_args()

    state = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")
    auth_url = (
        OAUTH2_AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": args.client_id,
                "redirect_uri": REDIRECT_URI,
                "state": state,
            }
        )
    )

    print("Opening browser for Honeywell sign-in…", file=sys.stderr)
    print(f"  {auth_url}", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(auth_url)

    print(
        "Waiting for callback on http://localhost:8765/callback "
        "(timeout 5 minutes)…",
        file=sys.stderr,
    )
    code = _wait_for_code()

    tokens = _exchange(code, args.client_id, args.client_secret)
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str):
        raise SystemExit(f"no refresh_token in response: {tokens}")

    print(
        "\n=== Resideo refresh token (store as Secret Manager `resideo-refresh-token`) ===",
        file=sys.stderr,
    )
    print(refresh)
    print(
        "\nNext steps:\n"
        "  printf '%s' '<paste-token>' | gcloud secrets create resideo-refresh-token --data-file=-\n"
        "  printf '%s' '<paste-client-id>' | gcloud secrets create resideo-client-id --data-file=-\n"
        "  printf '%s' '<paste-client-secret>' | gcloud secrets create resideo-client-secret --data-file=-\n"
        "  # then grant the runtime SA access — see infra/SETUP.md.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
