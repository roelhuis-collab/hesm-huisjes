#!/usr/bin/env python3
"""
WeHeat OAuth bootstrap — one-shot CLI to capture a refresh token.

Usage::

    cd apps/optimizer
    uv run scripts/weheat_bootstrap.py

What it does
------------
1. Generates a PKCE verifier + challenge.
2. Starts a tiny HTTP server on ``localhost:8765`` to catch the OAuth
   callback.
3. Opens your default browser to the WeHeat Keycloak authorize URL.
4. You log in to your WeHeat account in the browser.
5. Keycloak redirects back to ``http://localhost:8765/callback`` with a
   ``code``; this script exchanges the code for an access + refresh
   token and prints the refresh token.
6. Copy the printed token into Secret Manager::

       echo -n "<refresh-token>" | gcloud secrets create weheat-refresh-token \\
           --data-file=-

   (Or ``gcloud secrets versions add weheat-refresh-token --data-file=-``
   to rotate.)

Re-run any time the refresh token gets revoked. Token rotation typically
happens after long inactivity or password change.

Notes
-----
Uses the public Home Assistant OAuth client (``HomeAssistantAPI``). No
secrets in this script — anyone with a WeHeat account can run it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser

import httpx

# Imported in the script-side path; mirrors the constants in the connector.
OAUTH2_AUTHORIZE_URL = (
    "https://auth.weheat.nl/auth/realms/Weheat/protocol/openid-connect/auth/"
)
OAUTH2_TOKEN_URL = (
    "https://auth.weheat.nl/auth/realms/Weheat/protocol/openid-connect/token/"
)
DEFAULT_CLIENT_ID = "HomeAssistantAPI"
DEFAULT_CLIENT_SECRET = "TqpNpiJDKbGXF8jaL9D1Y8yzl1pI1Fly"
SCOPES = "openid offline_access"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8765
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


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
            body = f"<h1>WeHeat auth failed</h1><p>{type(self).error}</p>"
        elif "code" in params:
            type(self).code = params["code"][0]
            body = "<h1>WeHeat auth complete</h1><p>You can close this tab.</p>"
        else:
            type(self).error = "no code or error in callback"
            body = "<h1>WeHeat auth: missing code</h1>"
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
        deadline = timeout_s
        step = 0.5
        elapsed = 0.0
        while elapsed < deadline:
            if _CodeCatcher.code is not None or _CodeCatcher.error is not None:
                break
            thread.join(step)
            elapsed += step
    finally:
        server.shutdown()
        server.server_close()

    if _CodeCatcher.error is not None:
        raise SystemExit(f"WeHeat auth failed: {_CodeCatcher.error}")
    if _CodeCatcher.code is None:
        raise SystemExit(f"timed out after {timeout_s:.0f}s waiting for callback")
    return _CodeCatcher.code


def _exchange(
    code: str, verifier: str, client_id: str, client_secret: str
) -> dict[str, object]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    }
    resp = httpx.post(OAUTH2_TOKEN_URL, data=payload, timeout=15.0)
    if resp.status_code != 200:
        raise SystemExit(
            f"token exchange failed: {resp.status_code} {resp.text[:300]}"
        )
    body: dict[str, object] = resp.json()
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--client-secret", default=DEFAULT_CLIENT_SECRET)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the auth URL instead of opening a browser.",
    )
    args = parser.parse_args()

    verifier, challenge = _make_pkce()
    state = _b64url(secrets.token_bytes(16))
    auth_url = (
        OAUTH2_AUTHORIZE_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": args.client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    print("Opening browser for WeHeat sign-in…", file=sys.stderr)
    print(f"  {auth_url}", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(auth_url)

    print(
        "Waiting for callback on http://localhost:8765/callback "
        "(timeout 5 minutes)…",
        file=sys.stderr,
    )
    code = _wait_for_code()

    tokens = _exchange(code, verifier, args.client_id, args.client_secret)
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str):
        raise SystemExit(f"no refresh_token in response: {tokens}")

    print(
        "\n=== WeHeat refresh token (store as Secret Manager `weheat-refresh-token`) ===",
        file=sys.stderr,
    )
    print(refresh)
    print(
        "\nNext steps:\n"
        "  echo -n '<paste-token>' | gcloud secrets create weheat-refresh-token --data-file=-\n"
        "  # or to rotate:\n"
        "  echo -n '<paste-token>' | gcloud secrets versions add weheat-refresh-token --data-file=-",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
